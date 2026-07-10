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
from pathlib import Path
from typing import Any

INTERVAL = 30

# Final pipeline states — GitLab never transitions out of these.
TERMINAL_PIPELINE_STATES = {"success", "failed", "canceled", "skipped"}

# Import the existing _glab_api CLI wrapper from the gl-mr op so we share one
# source of truth for glab invocation, error handling, and timeouts.
_MR_MODULE_PATH = Path(__file__).parents[3] / "gitlab" / "mr.py"
_spec = importlib.util.spec_from_file_location("gitlab_mr_op", _MR_MODULE_PATH)
assert _spec is not None and _spec.loader is not None
_mr_op = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mr_op)
_glab_api_cli = _mr_op._glab_api  # type: ignore[attr-defined]


def _glab_api(endpoint: str) -> dict | list | None:
    """JSON-decode an _glab_api CLI call. None on any failure."""
    try:
        r = _glab_api_cli(endpoint)
    except (FileNotFoundError, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def _fetch(pipeline_id: str) -> dict[str, Any] | None:
    data = _glab_api(f"projects/:id/pipelines/{pipeline_id}")
    if not isinstance(data, dict):
        return None
    return data


def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    pipeline_id = ctx["id"]
    data = _fetch(pipeline_id)
    if data is None:
        return [], state  # transient — try again next tick

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
    }
    return events, new_state


def is_terminal(state: dict) -> bool:
    return state.get("status") in TERMINAL_PIPELINE_STATES
