"""gh-run watcher source — a GitHub Actions workflow run, by id.

The twin of `gl-pipeline`: `gl-pipeline` watches a *pipeline id* directly and
`gh-run` watches a *run id* directly, both independent of any merge/pull
request. That independence is the whole point of the source. `github-pr`
already reports GitHub CI for runs attached to a PR (`checks_failed`,
`checks_succeeded`, `checks_pending`), so the hole this fills is every run with
no PR to hang off:

  * a `master` run after a merge — the case that bit this repository, where
    master sat red from a merge-order race with nothing watching it
  * a manual `workflow_dispatch`
  * a `gh run rerun`, whose new run id nothing is following

Overlap with `github-pr` for PR-attached runs is expected: the ids differ and
you pick which one to watch. Watching both for the same run means two
notifications. `docs/presets/watch.md` says which to reach for.

`status` is read **before** `conclusion`, never the other way round:
`conclusion` is null for the entire life of the run and only fills in once
`status` reaches `completed`, so a poller that branched on conclusion first
would read every healthy in-flight poll as an unknown outcome.

Source plugin contract:
- INTERVAL: int seconds between polls (30s default)
- poll(state, ctx) -> (events, new_state)
- is_terminal(state) -> bool
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

INTERVAL = 30

# Reuse, not a second copy of the CLI plumbing. Two imports, two jobs:
#   `_gh`            — the shared gh invoker (timeout, encoding, capture), the
#                      same one the github-pr source already borrows.
#   `_format_error`  — the `gh-run` op's own stderr classifier, so a watcher
#                      that cannot look reports "gh CLI not authenticated" or
#                      "run not found" rather than a raw dump.
# The direction is watch-source -> preset, which is the direction github-pr and
# gl-pipeline already established; nothing under presets/github imports back.
_GITHUB_DIR = Path(__file__).parents[3] / "github"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _GITHUB_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_gh = _load("github_pr_op", "pr.py")._gh  # type: ignore[attr-defined]
_format_error = _load("github_run_op", "run.py")._format_error  # type: ignore[attr-defined]

# Fields asked of gh on each poll — one API call, covers every event.
_VIEW_FIELDS = "databaseId,status,conclusion,workflowName,url,headBranch,event"

# GitHub never transitions a run out of `completed`.
TERMINAL_RUN_STATUS = "completed"

# Conclusion -> event. The tail values are not decoration: this repository has
# filed #445 and #454 over a check tally that counted `CANCELLED` as neither
# pass nor pending and a run concluding `failure` read as still waiting. A
# conclusion the map does not name must never become nothing, so anything not
# listed below falls through to `run_inconclusive` flagged `recognised: no`,
# carrying the raw string.
RED_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}
VERDICTLESS_CONCLUSIONS = {"neutral", "skipped", "stale"}

LOOKUP_OK = "ok"
LOOKUP_UNAVAILABLE = "unavailable"


def _fetch(run_id: str) -> tuple[dict[str, Any] | None, str]:
    """`(run, "")` when GitHub answered, `(None, why)` when we could not look.

    **`(None, why)` and a run with nothing new are different answers.** An
    absence produced by the tool is not an absence in the world — a `gh run
    view` that 401s, times out, or returns junk must not be reported as a run
    that is simply still going, which is the defect this repository keeps
    filing. Every failure path therefore returns a non-empty reason, and
    `poll` turns the first one into an event rather than into silence.
    """
    try:
        r = _gh(["run", "view", run_id, "--json", _VIEW_FIELDS], timeout=15)
    except subprocess.TimeoutExpired:
        return None, f"ERROR: gh timed out looking up run #{run_id}"
    except FileNotFoundError:
        return None, "ERROR: gh not found — install from https://cli.github.com"
    except OSError as e:
        return None, f"ERROR: gh could not run for run #{run_id}: {e}"
    if r.returncode != 0:
        return None, _format_error(r.stderr or "", "Workflow run", run_id)
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None, f"ERROR: invalid JSON from gh for run #{run_id}"
    if not isinstance(data, dict):
        return None, f"ERROR: unexpected payload shape from gh for run #{run_id}"
    return data, ""


def _completion_event(conclusion: str, base: dict[str, str], label: str) -> dict:
    """The event for a run that reached `completed`, whatever it concluded."""
    if conclusion == "success":
        return {"event": "run_succeeded", "payload": {**base, "recognised": "yes"},
                "notify_title": f"{label} ok", "notify_message": base["url"] or label}
    if conclusion in RED_CONCLUSIONS:
        return {"event": "run_failed", "payload": {**base, "recognised": "yes"},
                "notify_title": f"{label} {conclusion}",
                "notify_message": base["url"] or label}
    if conclusion == "cancelled":
        return {"event": "run_cancelled", "payload": {**base, "recognised": "yes"},
                "notify_title": f"{label} cancelled",
                "notify_message": base["url"] or label}
    if conclusion == "action_required":
        return {"event": "run_action_required", "payload": {**base, "recognised": "yes"},
                "notify_title": f"{label} needs action",
                "notify_message": base["url"] or label}
    if conclusion in VERDICTLESS_CONCLUSIONS:
        return {"event": "run_inconclusive", "payload": {**base, "recognised": "yes"},
                "notify_title": f"{label} {conclusion}",
                "notify_message": base["url"] or label}
    # Unrecognised — a conclusion GitHub added after this map was written, or an
    # empty one on a run that says it completed. Reported verbatim rather than
    # dropped: the watcher stops here, so silence now means the run is never
    # reported at all.
    shown = conclusion or "no conclusion"
    return {
        "event": "run_inconclusive",
        "payload": {**base, "recognised": "no"},
        "notify_title": f"{label} ended, outcome unrecognised",
        "notify_message": f"conclusion: {shown} — {base['url'] or label}",
    }


def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    run_id = str(ctx["id"])
    data, error = _fetch(run_id)

    if data is None:
        # Three states, not two: ok, a finding, and *cannot tell*. Said out
        # loud once per outage — edge-triggered on the lookup flag, because a
        # signal that repeats every 30s is one people mute, which is trading
        # the loud failure for a quiet one by a longer route. Last-known status
        # is preserved so the watcher stays alive and non-terminal; a network
        # blip must not retire a run nobody is now watching.
        new_state = {**state, "lookup": LOOKUP_UNAVAILABLE, "error": error}
        if state.get("lookup") == LOOKUP_UNAVAILABLE:
            return [], new_state
        label = f"run #{run_id}"
        return [{
            "event": "run_unreachable",
            "payload": {
                "run_id": run_id,
                "error": error,
                "last_known_status": str(state.get("status") or ""),
                "url": str(state.get("url") or ""),
            },
            "notify_title": f"{label} — cannot tell",
            "notify_message": error,
        }], new_state

    status = str(data.get("status") or "")
    conclusion = str(data.get("conclusion") or "")
    workflow = str(data.get("workflowName") or "")
    url = str(data.get("url") or "")
    branch = str(data.get("headBranch") or "")
    label = f"{workflow} #{run_id}" if workflow else f"run #{run_id}"

    prev_status = str(state.get("status") or "")
    prev_conclusion = str(state.get("conclusion") or "")

    events: list[dict] = []
    base = {
        "run_id": run_id,
        "status": status,
        "conclusion": conclusion,
        "workflow": workflow,
        "branch": branch,
        "url": url,
    }

    if status == TERMINAL_RUN_STATUS:
        if status != prev_status or conclusion != prev_conclusion:
            events.append(_completion_event(conclusion, base, label))
    elif status == "in_progress" and prev_status not in ("in_progress", ""):
        events.append({
            "event": "run_started",
            "payload": base,
        })

    new_state = {
        "run_id": run_id,
        "status": status,
        "conclusion": conclusion,
        "workflow": workflow,
        "url": url,
        "lookup": LOOKUP_OK,
    }
    return events, new_state


def is_terminal(state: dict) -> bool:
    return state.get("status") == TERMINAL_RUN_STATUS
