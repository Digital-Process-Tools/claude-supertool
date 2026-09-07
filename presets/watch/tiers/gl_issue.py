#!/usr/bin/env python3
"""gl-issue — a radar tier scoped to one GitLab issue, not a population (#898).

Every existing tier answers a *population* question: "how are all my open
MRs" (`gl-mrs`), "how is the runner fleet" (`gl-runners`), "how are all my
open PRs" (`gh-prs`). Nothing answers "how is issue #12657" — its own related
MRs, its label changes, its reopens. That is this tier, registered by name
like any other:

    {"ops": {"radar": {"radar_tiers": {"gl-issue": {}}}}}

    radar:gl-issue:12657

`_arg` carries the whole invocation string after `radar:`, so this tier
accepts either `"gl-issue:12657"` (the shape shown above, self-prefixed) or a
bare `"12657"` (in case a caller ever renames the registered key and still
wants to pass the id through unprefixed). Anything else is refused with the
syntax restated, never guessed at.

Why no feed, no exclusions, no filter vocabulary
-------------------------------------------------

`gl-mrs` and `gh-prs` both carry a *discovery* problem: their population is
"every open MR/PR matching a filter", which can gain a member between one
radar run and the next, so both keep a feed poller alive so a session idle
for an hour still learns about something opened fifty minutes ago.

This tier has no such problem. Its population is "issue #12657's own related
MRs", and that is answered by one live query scoped to the issue's own id on
every single run — there is nothing to discover that a fresh `related_merge_
requests` call would not already show. So there is no feed, and reconcile is
simpler by construction rather than by omission.

The same argument removes the filter vocabulary `gl-mrs` needs: there is
nothing to filter. The one parameter this tier takes is *which issue*, and
that arrives through `_arg`, not through `RADAR_OPTIONS`.

What this tier watches, and what it does not (yet)
----------------------------------------------------

For every one of the issue's related MRs still `opened`, this tier ensures a
`gitlab-mr` watcher — the same source and the same `defaults.DEFAULT_ONLY`
event set `gl-mrs` heals onto every open MR it tracks. A closed or merged
related MR is not watched: its own terminal event already fired before this
tier's report next runs, and asking `gitlab-mr` to keep polling a merged MR
would just accumulate dead pollers.

**Deliberately out of scope for this issue**, named rather than silently
dropped (see the issue's own comments #2 and #3):

  * no new `gitlab-mr-note` source for review comments / requested changes —
    the existing `gitlab-mr` source already emits `comment_added`, which
    `defaults.DEFAULT_ONLY` already includes, so the noisier tail this tier
    would want (approvals, retargets) has nowhere to come from yet; adding a
    source is its own change with its own tests.
  * no per-tier/per-source policy markdown (`RADAR_POLICY`) — the issue's own
    second comment asks that the registry-and-policy mechanism be shared with
    the separate `dashboard` op (#953) before either grows it, and building it
    here first would either duplicate that decision or pre-empt it.
  * no per-category `history.md` ledger — needs the policy layer above it to
    mean anything (a ledger with nowhere to promote a pattern to is just an
    unbounded log), so it waits on the same decision.

What this tier does add on top of the shared per-MR heal, because it is what
a single-issue focus actually asks for: the **issue's own** state and label
set are tracked across runs (`tiers/_snapshot.py`, keyed on the issue id), so
a reopen and a label change are named on the board the run they happen,
exactly the way `gl-mrs` names a departed MR — as a *moved* event, which is
why they count against `healthy` below even though nothing here is asserting
the world is broken.

A caveat inherited from how `_arg` reaches every registered tier
------------------------------------------------------------------

Radar passes the *same* `_arg` string to every tier configured in
`ops.radar.radar_tiers` (`tier_reports`, one loop, one `arg`). Registering
`gl-issue` alongside `gl-mrs` and invoking `radar:gl-issue:12657` therefore
also hands `"gl-issue:12657"` to `gl-mrs`'s own `resolve_filter`, which does
not recognise that shape and raises — reported as a failure for that tier
alone, never fatal to this one. That is an existing property of the tier
contract, not something introduced here, and it is the reason a focused radar
session is expected to register `gl-issue` on its own rather than beside a
population tier that shares the argument slot.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).parent
_WATCH = _HERE.parent

sys.path.insert(0, str(_WATCH))
import defaults  # noqa: E402

sys.path.insert(0, str(_HERE))
import _radar_errors  # noqa: E402  (one copy, one class identity — #1847)

#: This tier's failure vocabulary, from the one copy `gl-mrs` and `gh-prs`
#: already share. See `_radar_errors.py` for why it is `import`, never `_load`.
RadarError = _radar_errors.RadarError
RadarUnreachable = _radar_errors.RadarUnreachable


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The `gl-issue` *op*'s own module — reused for its `_glab_api` CLI wrapper,
# and for the `_auth_probe`/`_status_probe`/`_untrusted`/`_checks` names it
# already imports, exactly as `gl_mrs.py` reuses `presets/gitlab/mrs.py`'s.
# One CLI-wrapping call site, not two, is the whole reason.
issue_op = _load("radar_gitlab_issue_op", _WATCH.parent / "gitlab" / "issue.py")

# The snapshot store, shared with every other tier since #859: a previous
# board keyed by the population it describes, so a delta cannot lie.
snapshot = _load("radar_snapshot", _HERE / "_snapshot.py")

SOURCE = defaults.DEFAULT_SOURCE
SNAPSHOT_PREFIX = "supertool-radar-gl-issue"

# This tier takes no config beyond what radar itself injects (`_arg`,
# `_watch`). `quiet_when_healthy` is accepted for symmetry with every other
# tier even though `RADAR_QUIET_DEFAULT` below is False by default, the same
# reasoning `gl_mrs.py` gives for carrying it.
RADAR_OPTIONS = {"quiet_when_healthy"}

# A focused board's report *is* the board — the same reasoning `gl_mrs.py`
# gives: total silence on a quiet issue is indistinguishable from a radar
# that failed to run, and this tier exists precisely so an issue you are
# focused on is never silently unwatched.
RADAR_QUIET_DEFAULT = False

# Substrings of `glab`'s own stderr that mean the request never landed. Its
# own copy rather than a shared one — `gl_mrs.py`'s docstring and
# `_auth_probe.py`'s both give the same reason: one CLI's transport
# vocabulary can widen without touching whatever else carries a copy of it.
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
    """Does this `glab` stderr describe a request that never landed?"""
    low = err.lower()
    return any(marker in low for marker in TRANSPORT_MARKERS)


def _no_watch(source: str, scope: str, only: list[str] | None = None) -> str:
    """Fallback `_watch` when a caller supplied none.

    "failed", never "alive" — see `gl_mrs._no_watch` for the argument: a tier
    asked to reconcile without a way to spawn cannot report coverage that
    nobody handed it a spawner to establish.
    """
    return "failed"


def parse_arg(arg: str) -> str:
    """The issue id `_arg` names, or `RadarError` with the syntax restated.

    Accepts `"gl-issue:12657"` (this tier's own name as a prefix, the shape
    the module docstring shows) or a bare `"12657"`. A leading `#` is
    stripped either way, since that is how an issue id is written everywhere
    else in this repo's own tracker prose.
    """
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
    """One `glab api` call. `RadarError`/`RadarUnreachable` on any failure,
    never a guessed-at payload.

    Mirrors `gl_mrs._query`'s three-way split (unreachable / product error /
    parse failure) over the one CLI call this tier needs, rather than the
    per-page list machinery that tier owns and this one has no use for.
    """
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
        # Not a finished answer — same predicate as `gl_mrs._query`, ported
        # here rather than shared (see that function's own long comment on
        # why the Windows side of this is reasoned, not observed).
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
    """Numeric where possible, so `!9` sorts before `!10`."""
    return (0, value.rjust(20, "0")) if value.isdigit() else (1, value)


def _pipeline_line(mr: dict) -> str:
    """One related MR's pipeline verdict — three states, never two.

    Reuses `presets/gitlab/issue.py`'s own vocabulary (`_checks.NO_PIPELINE`,
    `_checks.NOT_GREEN`) so this tier and the `gl-issue`-adjacent `gh-issue`/
    `gl-issue` ops read one sentence for "no pipeline", not two.
    """
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
    """(lines, healthy) — one issue's own board, as radar's tier contract wants it.

    `healthy` means "this tier could tell you the truth", not "the issue is
    in a good state" — the same distinction `gl_mrs.radar_report` draws. A
    watcher that stopped being coverable counts against it; so does a reopen,
    a label change or an MR set that moved, for the same reason a departed MR
    counts against `gl_mrs`'s health: `quiet_when_healthy` drops the whole
    report when healthy, and an event this board exists to surface must not
    be the thing that makes it go quiet.
    """
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
                        if status in ("failed", "capped")), key=_iid_sort_key)

    digest = snapshot.key(iid)
    previous = snapshot.read(SNAPSHOT_PREFIX, digest, "issue")
    prev_entry = (previous or {}).get("issue") if previous else None

    # Cold start (no previous entry) has nothing to diff against — "moved"
    # is a claim about a comparison, and a comparison against nothing is not
    # one. Without this guard every related MR on the very first run reads
    # as "new", which is simply what the board looks like the first time it
    # is ever built, not an event worth an unhealthy flag.
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

    lines = [f"gl-issue #{iid}: {title}",
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
    """What this tier knows, without spawning or calling GitLab (#859's
    `radar_state` pattern, reused here for the same reason: looking must not
    cost what acting costs).
    """
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
