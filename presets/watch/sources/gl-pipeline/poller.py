"""gl-pipeline watcher source.

Polls a single GitLab CI pipeline via `glab api` and emits events when the
pipeline status changes. Terminal when the pipeline reaches a final state
(success/failed/canceled/skipped) — at which point the watcher stops on its
own.

Reuses `_glab_api` from presets/gitlab/mr.py — same source of truth for glab
invocation, error handling, and timeouts as the gitlab-mr source. We fetch the
pipeline object itself (`projects/:id/pipelines/{id}`), which is a single JSON
object carrying `status` directly — so this source is unaffected by the
paginated-JSON parse bug on the `/jobs` endpoint (issues #352/#359).

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

# Final pipeline states — GitLab never transitions out of these.
TERMINAL_PIPELINE_STATES = {"success", "failed", "canceled", "skipped"}

# Import the existing _glab_api CLI wrapper from the gl-mr op so we share one
# source of truth for glab invocation, error handling, and timeouts.
#
# `_format_error` comes from the same module — the classifier the `gl-mr` op
# already uses to turn glab stderr into "glab not authenticated. Run: glab auth
# login" rather than a raw dump. Borrowing it rather than writing a second one
# keeps one vocabulary for GitLab failures across the op and the watcher, which
# is the same reasoning that made `gh-run` borrow `presets/github/run.py`'s.
_MR_MODULE_PATH = Path(__file__).parents[3] / "gitlab" / "mr.py"
_spec = importlib.util.spec_from_file_location("gitlab_mr_op", _MR_MODULE_PATH)
assert _spec is not None and _spec.loader is not None
_mr_op = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mr_op)
_glab_api_cli = _mr_op._glab_api  # type: ignore[attr-defined]
_format_error = _mr_op._format_error  # type: ignore[attr-defined]

LOOKUP_OK = "ok"
LOOKUP_UNAVAILABLE = "unavailable"


def _glab_api(endpoint: str, pipeline_id: str) -> tuple[dict | list | None, str]:
    """`(payload, "")` when glab answered, `(None, why)` when we could not look.

    Every failure path returns a non-empty reason. Collapsing them all to `None`
    was the #541 defect: a 404 on a deleted pipeline, an expired token and a
    missing binary produced the same answer as a healthy poll with nothing new.

    `TimeoutExpired` is caught with the rest. It is a `SubprocessError`, not an
    `OSError`, so it used to escape `poll()` entirely — the dispatcher logged it
    to `last_error` in the state file and slept, which nobody reads.
    """
    try:
        r = _glab_api_cli(endpoint)
    except FileNotFoundError:
        return None, "ERROR: glab not found — install from https://gitlab.com/gitlab-org/cli"
    except subprocess.TimeoutExpired:
        return None, f"ERROR: glab timed out looking up pipeline #{pipeline_id}"
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"ERROR: glab could not run for pipeline #{pipeline_id}: {e}"
    if r.returncode != 0:
        return None, _format_error(r.stderr or "", "Pipeline", pipeline_id)
    try:
        return json.loads(r.stdout), ""
    except json.JSONDecodeError:
        return None, f"ERROR: invalid JSON from glab for pipeline #{pipeline_id}"


def _fetch(pipeline_id: str) -> tuple[dict[str, Any] | None, str]:
    data, error = _glab_api(f"projects/:id/pipelines/{pipeline_id}", pipeline_id)
    if error:
        return None, error
    if not isinstance(data, dict):
        return None, f"ERROR: unexpected payload shape from glab for pipeline #{pipeline_id}"
    return data, ""


def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    pipeline_id = ctx["id"]
    data, error = _fetch(pipeline_id)
    if data is None:
        # Three answers, not two: ok, a finding, and *cannot tell*. Silence on a
        # blip is still right — this fires once per outage, edge-triggered on
        # the lookup flag, because an alert repeating every 30s is one people
        # mute, which is the quiet failure again by a longer route.
        #
        # `{**state, ...}` is what makes recovery work: status, id and url are
        # carried forward untouched, so a pipeline that went red *during* the
        # blindness is still a transition against the last status we could read
        # and is announced on the first poll that succeeds. Rebuilding state
        # here would erase the baseline and swallow it as already-seen.
        new_state = {**state, "lookup": LOOKUP_UNAVAILABLE, "error": error}
        if state.get("lookup") == LOOKUP_UNAVAILABLE:
            return [], new_state
        return [{
            "event": "pipeline_unreachable",
            "payload": {
                "pipeline_id": pipeline_id,
                "error": error,
                # `last_known_`, not the bare field name: this is the last thing
                # we could see, not what the pipeline is doing now, and the
                # whole event exists because those two had become the same word.
                "last_known_status": str(state.get("status") or ""),
                "url": str(state.get("web_url") or ""),
            },
            "notify_title": f"pipeline #{pipeline_id} — cannot tell",
            "notify_message": error,
        }], new_state

    status = str(data.get("status") or "")
    web_url = str(data.get("web_url") or "")

    events: list[dict] = []
    prev_status = state.get("status", "")

    if status and status != prev_status:
        if status == "success":
            events.append({
                "event": "pipeline_succeeded",
                "payload": {"pipeline_id": pipeline_id, "url": web_url, "status": status},
                "notify_title": f"pipeline #{pipeline_id} ok",
                "notify_message": web_url or f"pipeline {pipeline_id}",
            })
        elif status == "failed":
            events.append({
                "event": "pipeline_failed",
                "payload": {"pipeline_id": pipeline_id, "url": web_url, "status": status},
                "notify_title": f"pipeline #{pipeline_id} failed",
                "notify_message": web_url or f"pipeline {pipeline_id}",
            })
        elif status == "canceled":
            events.append({
                "event": "pipeline_canceled",
                "payload": {"pipeline_id": pipeline_id, "url": web_url, "status": status},
                "notify_title": f"pipeline #{pipeline_id} canceled",
                "notify_message": web_url or f"pipeline {pipeline_id}",
            })
        elif status == "running" and prev_status not in ("running", ""):
            events.append({
                "event": "pipeline_running",
                "payload": {"pipeline_id": pipeline_id, "url": web_url, "status": status},
            })

    new_state = {
        "status": status,
        "pipeline_id": pipeline_id,
        "web_url": web_url,
        "lookup": LOOKUP_OK,
    }
    return events, new_state


def is_terminal(state: dict) -> bool:
    return state.get("status") in TERMINAL_PIPELINE_STATES
