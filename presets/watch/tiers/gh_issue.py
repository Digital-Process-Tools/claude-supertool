#!/usr/bin/env python3




































































































from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).parent
_WATCH = _HERE.parent

sys.path.insert(0, str(_HERE))
import _radar_errors  



RadarError = _radar_errors.RadarError
RadarUnreachable = _radar_errors.RadarUnreachable
RadarUnconfigured = _radar_errors.RadarUnconfigured


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module








issue_op = _load("radar_github_issue_op", _WATCH.parent / "github" / "issue.py")



snapshot = _load("radar_snapshot", _HERE / "_snapshot.py")

SOURCE = "github-pr"
SNAPSHOT_PREFIX = "supertool-radar-gh-issue"







ONLY_EVENTS = (
    "checks_failed", "checks_succeeded", "checks_pending",
    "review_approved", "review_changes_requested", "comment_added",
    "merged", "closed", "conflicts_appeared", "pr_unreachable",
)





RADAR_OPTIONS = {"quiet_when_healthy"}





RADAR_QUIET_DEFAULT = False




GH_RC_NO_CREDENTIALS = 4






TRANSPORT_MARKERS = (
    "not logged in",
    "http 401",
    "rate limit",
    "http 403",
    "dial tcp",
    "no such host",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "i/o timeout",
    "tls handshake timeout",
    "client.timeout",
    "error connecting to",
)


def _transport_unreachable(err: str) -> bool:

    low = err.lower()
    return any(marker in low for marker in TRANSPORT_MARKERS)


def _no_watch(source: str, scope: str, only: list[str] | None = None) -> str:






    return "failed"


def parse_arg(arg: str) -> str:







    s = (arg or "").strip()
    if s.lower().startswith("gh-issue:"):
        s = s[len("gh-issue:"):]
    s = s.strip().lstrip("#").strip()
    if not s.isdigit():
        raise RadarError(
            "radar: gh-issue tier requires an issue number, e.g. "
            f"radar:gh-issue:2369 (got _arg={arg!r})")
    return s


def _run(args: list[str], number: str, what: str,
        timeout: int = 15) -> "subprocess.CompletedProcess[str]":





    try:
        result = issue_op._gh(args, timeout=timeout)
    except FileNotFoundError as exc:
        raise RadarUnreachable(f"gh not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RadarUnreachable(f"gh timed out {what} for issue #{number}") from exc
    except OSError as exc:
        raise RadarUnreachable(f"gh could not run: {exc}") from exc
    if result.returncode == 0:
        return result
    if result.returncode < 0:



        err = issue_op._untrusted.flat((result.stderr or "").strip()) or "unknown error"
        raise RadarUnreachable(
            f"gh did not finish before it answered {what} for issue "
            f"#{number} (returncode {result.returncode}): {err}")
    err = issue_op._untrusted.flat((result.stderr or "").strip()) or "unknown error"
    if result.returncode == GH_RC_NO_CREDENTIALS:


        raise RadarUnconfigured(
            "gh has no credentials in this environment, so it refused "
            f"before {what} for issue #{number}: {err}")
    if issue_op._auth_probe.says_not_authenticated(err):
        raise RadarUnreachable(
            f"gh says this request was not authenticated (exit "
            f"{result.returncode}) {what} for issue #{number}: {err}. "
            "Run: gh auth login")
    if issue_op._status_probe.says_not_found(err):
        raise RadarError(f"issue #{number} not found: {err}")
    if _transport_unreachable(err):
        raise RadarUnreachable(
            f"gh could not reach the API (exit {result.returncode}) "
            f"{what} for issue #{number}: {err}")
    raise RadarError(
        f"gh did not answer {what} for issue #{number}, and nothing in its "
        f"output says why (exit {result.returncode}): {err}")


def _fetch_issue(number: str) -> dict:
    result = _run(
        ["issue", "view", number, "--json", "number,title,state,labels,url"],
        number, "fetching the issue")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RadarError(f"could not parse gh JSON output for issue #{number}") from exc
    if not isinstance(data, dict):
        raise RadarError(f"gh returned no issue object for issue #{number}")
    return data


def _fetch_closing_prs(number: str, web_url: str) -> list[dict]:
    owner_name = issue_op._owner_repo(web_url)
    if owner_name is None:
        raise RadarError(
            f"could not determine owner/repo for issue #{number}'s linked-PR lookup")
    owner, name = owner_name
    query = issue_op._closing_prs_query(owner, name, number)
    result = _run(["api", "graphql", "-f", f"query={query}"],
                  number, "fetching linked PRs")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RadarError(
            f"could not parse gh GraphQL output for issue #{number}'s linked PRs") from exc
    nodes = issue_op._closing_pr_nodes(payload)
    if nodes is None:
        raise RadarError(
            f"gh returned an unexpected shape for issue #{number}'s linked PRs")
    return nodes


def _number_sort_key(value: str) -> tuple[int, str]:

    return (0, value.rjust(20, "0")) if value.isdigit() else (1, value)


def _check_line(pr: dict) -> str:






    number = pr.get("number", "?")
    return issue_op._check_tally(pr, number)


def radar_report(options: dict | None = None) -> tuple[list[str], bool]:










    options = options or {}
    watch = options.get("_watch") or _no_watch
    number = parse_arg(str(options.get("_arg") or ""))

    issue = _fetch_issue(number)
    web_url = str(issue.get("url") or "")
    closing = _fetch_closing_prs(number, web_url)

    title = issue_op._untrusted.flat(str(issue.get("title") or "?"))
    state = str(issue.get("state") or "?")
    labels = sorted({issue_op._untrusted.flat(str(label.get("name")
                     if isinstance(label, dict) else label))
                     for label in (issue.get("labels") or []) if label})

    open_prs = [pr for pr in closing
                if isinstance(pr, dict) and pr.get("state") == "OPEN"]
    pr_numbers = sorted({str(pr.get("number")) for pr in open_prs
                         if pr.get("number") is not None}, key=_number_sort_key)

    watch_status = {pr_number: watch(SOURCE, pr_number, list(ONLY_EVENTS))
                    for pr_number in pr_numbers}




    uncovered = sorted((pr_number for pr_number, status in watch_status.items()
                        if status in ("failed", "capped", "unclaimable")),
                       key=_number_sort_key)

    digest = snapshot.key(number)
    previous = snapshot.read(SNAPSHOT_PREFIX, digest, "issue")
    prev_entry = (previous or {}).get("issue") if previous else None






    cold_start = prev_entry is None
    reopened = (not cold_start and prev_entry.get("state") == "CLOSED"
               and state == "OPEN")
    prev_labels = set(prev_entry.get("labels") or []) if not cold_start else set()
    labels_added = [] if cold_start else sorted(set(labels) - prev_labels)
    labels_removed = [] if cold_start else sorted(prev_labels - set(labels))
    prev_prs = set(prev_entry.get("prs") or []) if not cold_start else set()
    new_prs = [] if cold_start else sorted(set(pr_numbers) - prev_prs, key=_number_sort_key)
    departed_prs = ([] if cold_start
                    else sorted(prev_prs - set(pr_numbers), key=_number_sort_key))





    lines = [issue_op._untrusted.flat_note("the issue and PR titles"),
             f"gh-issue #{number}: {title}",
             f"  state: {state}" + ("  <-- REOPENED" if reopened else "")]
    lines.append(f"  labels: {', '.join(labels) or 'none'}")
    if labels_added:
        lines.append(f"  labels added: {', '.join(labels_added)}")
    if labels_removed:
        lines.append(f"  labels removed: {', '.join(labels_removed)}")
    if not open_prs:
        lines.append("  open linked PRs: none")
    else:
        lines.append(f"  open linked PRs: {len(open_prs)}")
        for pr in sorted(open_prs, key=lambda p: _number_sort_key(str(p.get("number")))):
            pr_number = str(pr.get("number", "?"))
            pr_title = issue_op._untrusted.flat(str(pr.get("title") or "?"))
            pr_branch = issue_op._untrusted.flat(str(pr.get("headRefName") or "?"))
            lines.append(f"    #{pr_number} {pr_title}")
            lines.append(f"      branch: {pr_branch}")
            lines.append(f"      checks: {_check_line(pr)}")
            lines.append(f"      watcher: {watch_status.get(pr_number, '?')}")
    if new_prs:
        lines.append(f"  new linked PR(s) since last run: "
                     f"{', '.join('#' + n for n in new_prs)}")
    if departed_prs:
        lines.append(f"  no longer linked/open: "
                     f"{', '.join('#' + n for n in departed_prs)}")
    if uncovered:
        lines.append(f"  radar: WARNING — watcher not alive for "
                     f"{', '.join('#' + n for n in uncovered)}")

    snapshot.write(SNAPSHOT_PREFIX, digest,
                   {"state": state, "labels": labels, "prs": pr_numbers}, "issue")

    healthy = not (uncovered or reopened or labels_added or labels_removed
                   or new_prs or departed_prs)
    return lines, healthy


def radar_state(options: dict | None = None) -> list[str]:




    options = options or {}
    try:
        number = parse_arg(str(options.get("_arg") or ""))
    except RadarError as exc:
        return [f"  issue     : REFUSED — {exc}"]
    out = [f"  issue     : #{number}"]
    digest = snapshot.key(number)
    path = snapshot.path(SNAPSHOT_PREFIX, digest)
    previous = snapshot.read(SNAPSHOT_PREFIX, digest, "issue")
    if previous is None:
        out.append(f"  snapshot  : {path} — absent (cold start next run)")
    else:
        entry = previous.get("issue") or {}
        out.append(f"  snapshot  : {path} — state={entry.get('state', '?')}, "
                   f"{len(entry.get('prs') or [])} PR(s), "
                   f"{len(entry.get('labels') or [])} label(s)")
    return out
