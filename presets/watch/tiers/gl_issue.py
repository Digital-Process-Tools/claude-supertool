#!/usr/bin/env python3




















































































from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).parent
_WATCH = _HERE.parent

sys.path.insert(0, str(_WATCH))
import defaults  

sys.path.insert(0, str(_HERE))
import _radar_errors  



RadarError = _radar_errors.RadarError
RadarUnreachable = _radar_errors.RadarUnreachable


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module






issue_op = _load("radar_gitlab_issue_op", _WATCH.parent / "gitlab" / "issue.py")



snapshot = _load("radar_snapshot", _HERE / "_snapshot.py")

SOURCE = defaults.DEFAULT_SOURCE
SNAPSHOT_PREFIX = "supertool-radar-gl-issue"





RADAR_OPTIONS = {"quiet_when_healthy"}





RADAR_QUIET_DEFAULT = False





TRANSPORT_MARKERS = (
    "dial tcp",
    "no such host",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "i/o timeout",
    "tls handshake timeout",
    "client.timeout",
    "error connecting to",
    "429 too many requests",
    "rate limit",
)


def _transport_unreachable(err: str) -> bool:

    low = err.lower()
    return any(marker in low for marker in TRANSPORT_MARKERS)


def _no_watch(source: str, scope: str, only: list[str] | None = None) -> str:






    return "failed"


def parse_arg(arg: str) -> str:







    s = (arg or "").strip()
    if s.lower().startswith("gl-issue:"):
        s = s[len("gl-issue:"):]
    s = s.strip().lstrip("#").strip()
    if not s.isdigit():
        raise RadarError(
            "radar: gl-issue tier requires an issue id, e.g. "
            f"radar:gl-issue:12657 (got _arg={arg!r})")
    return s


def _get(endpoint: str, identifier: str) -> dict | list:







    try:
        result = issue_op._glab_api(endpoint)
    except FileNotFoundError as exc:
        raise RadarUnreachable(f"glab not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RadarUnreachable(
            f"glab timed out fetching issue #{identifier}") from exc
    except OSError as exc:
        raise RadarUnreachable(f"glab could not run: {exc}") from exc
    if result.returncode < 0:



        err = issue_op._untrusted.flat((result.stderr or "").strip()) or "unknown error"
        raise RadarUnreachable(
            f"glab did not finish before it answered fetching issue "
            f"#{identifier} (returncode {result.returncode}): {err}")
    if result.returncode != 0:
        err = issue_op._untrusted.flat((result.stderr or "").strip()) or "unknown error"
        if issue_op._auth_probe.says_not_authenticated(
                err, issue_op._auth_probe.GITLAB_MARKERS):
            raise RadarUnreachable(
                f"glab says this request was not authenticated (exit "
                f"{result.returncode}): {err}. Run: glab auth login")
        if issue_op._status_probe.says_not_found(err):
            raise RadarError(f"issue #{identifier} not found: {err}")
        if _transport_unreachable(err):
            raise RadarUnreachable(
                f"glab could not reach the API (exit {result.returncode}): {err}")
        raise RadarError(
            f"glab did not answer fetching issue #{identifier}, and nothing "
            f"in its output says why (exit {result.returncode}): {err}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RadarError(
            f"could not parse glab JSON output for issue #{identifier}") from exc


def _fetch_issue(iid: str) -> dict:
    data = _get(f"projects/:id/issues/{iid}", iid)
    if not isinstance(data, dict):
        raise RadarError(f"glab returned no issue object for issue #{iid}")
    return data


def _fetch_related_mrs(iid: str) -> list[dict]:
    data = _get(f"projects/:id/issues/{iid}/related_merge_requests", iid)
    if not isinstance(data, list):
        raise RadarError(
            f"glab returned no MR list for issue #{iid}'s related MRs")
    return data


def _iid_sort_key(value: str) -> tuple[int, str]:

    return (0, value.rjust(20, "0")) if value.isdigit() else (1, value)


def _pipeline_line(mr: dict) -> str:






    checks = issue_op._checks
    pipeline = mr.get("head_pipeline")
    status = pipeline.get("status") if isinstance(pipeline, dict) else None
    if not status:
        return f"pipeline: {checks.NO_PIPELINE}"
    pid = pipeline.get("id")
    marker = "" if checks.bucket(status) == "passed" else f" {checks.NOT_GREEN}"
    ref = f" (#{pid})" if pid else ""
    return f"pipeline: {issue_op._untrusted.flat(str(status))}{ref}{marker}"


def radar_report(options: dict | None = None) -> tuple[list[str], bool]:










    options = options or {}
    watch = options.get("_watch") or _no_watch
    iid = parse_arg(str(options.get("_arg") or ""))

    issue = _fetch_issue(iid)
    related = _fetch_related_mrs(iid)

    title = issue_op._untrusted.flat(str(issue.get("title") or "?"))
    state = str(issue.get("state") or "?")
    labels = sorted({issue_op._untrusted.flat(str(label))
                     for label in (issue.get("labels") or []) if label})

    open_mrs = [mr for mr in related
                if isinstance(mr, dict) and mr.get("state") == "opened"]
    mr_iids = sorted({str(mr.get("iid")) for mr in open_mrs
                      if mr.get("iid") is not None}, key=_iid_sort_key)

    only = [event for event in defaults.DEFAULT_ONLY.split(",") if event]
    watch_status = {mr_iid: watch(SOURCE, mr_iid, only) for mr_iid in mr_iids}








    uncovered = sorted((mr_iid for mr_iid, status in watch_status.items()
                        if status in ("failed", "capped", "unclaimable")),
                       key=_iid_sort_key)

    digest = snapshot.key(iid)
    previous = snapshot.read(SNAPSHOT_PREFIX, digest, "issue")
    prev_entry = (previous or {}).get("issue") if previous else None






    cold_start = prev_entry is None
    reopened = (not cold_start and prev_entry.get("state") == "closed"
               and state == "opened")
    prev_labels = set(prev_entry.get("labels") or []) if not cold_start else set()
    labels_added = [] if cold_start else sorted(set(labels) - prev_labels)
    labels_removed = [] if cold_start else sorted(prev_labels - set(labels))
    prev_mrs = set(prev_entry.get("mrs") or []) if not cold_start else set()
    new_mrs = [] if cold_start else sorted(set(mr_iids) - prev_mrs, key=_iid_sort_key)
    departed_mrs = ([] if cold_start
                    else sorted(prev_mrs - set(mr_iids), key=_iid_sort_key))








    lines = [issue_op._untrusted.flat_note("the issue and MR titles"),
             f"gl-issue #{iid}: {title}",
             f"  state: {state}" + ("  <-- REOPENED" if reopened else "")]
    lines.append(f"  labels: {', '.join(labels) or 'none'}")
    if labels_added:
        lines.append(f"  labels added: {', '.join(labels_added)}")
    if labels_removed:
        lines.append(f"  labels removed: {', '.join(labels_removed)}")
    if not open_mrs:
        lines.append("  open related MRs: none")
    else:
        lines.append(f"  open related MRs: {len(open_mrs)}")
        for mr in sorted(open_mrs, key=lambda m: _iid_sort_key(str(m.get("iid")))):
            mr_iid = str(mr.get("iid", "?"))
            mr_title = issue_op._untrusted.flat(str(mr.get("title") or "?"))
            mr_branch = issue_op._untrusted.flat(str(mr.get("source_branch") or "?"))
            lines.append(f"    !{mr_iid} {mr_title}")
            lines.append(f"      branch: {mr_branch}")
            lines.append(f"      {_pipeline_line(mr)}")
            lines.append(f"      watcher: {watch_status.get(mr_iid, '?')}")
    if new_mrs:
        lines.append(f"  new related MR(s) since last run: "
                     f"{', '.join('!' + i for i in new_mrs)}")
    if departed_mrs:
        lines.append(f"  no longer related/open: "
                     f"{', '.join('!' + i for i in departed_mrs)}")
    if uncovered:
        lines.append(f"  radar: WARNING — watcher not alive for "
                     f"{', '.join('!' + i for i in uncovered)}")

    snapshot.write(SNAPSHOT_PREFIX, digest,
                   {"state": state, "labels": labels, "mrs": mr_iids}, "issue")

    healthy = not (uncovered or reopened or labels_added or labels_removed
                   or new_mrs or departed_mrs)
    return lines, healthy


def radar_state(options: dict | None = None) -> list[str]:




    options = options or {}
    try:
        iid = parse_arg(str(options.get("_arg") or ""))
    except RadarError as exc:
        return [f"  issue     : REFUSED — {exc}"]
    out = [f"  issue     : #{iid}"]
    digest = snapshot.key(iid)
    path = snapshot.path(SNAPSHOT_PREFIX, digest)
    previous = snapshot.read(SNAPSHOT_PREFIX, digest, "issue")
    if previous is None:
        out.append(f"  snapshot  : {path} — absent (cold start next run)")
    else:
        entry = previous.get("issue") or {}
        out.append(f"  snapshot  : {path} — state={entry.get('state', '?')}, "
                   f"{len(entry.get('mrs') or [])} MR(s), "
                   f"{len(entry.get('labels') or [])} label(s)")
    return out
