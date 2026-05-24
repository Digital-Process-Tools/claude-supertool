"""gitlab-mr watcher source.

Polls a single GitLab merge request via `glab api` and emits events when
status changes. Terminal when the MR is merged or closed.

Reuses `_glab_api` from presets/gitlab/mr.py — no duplicated CLI wrapping.

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

TERMINAL_MR_STATES = {"merged", "closed"}

# Import the existing _glab_api CLI wrapper from the gl-mr op so we share
# one source of truth for glab invocation, error handling, and timeouts.
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


def _fetch(iid: str) -> dict[str, Any] | None:
    data = _glab_api(f"projects/:id/merge_requests/{iid}")
    if not isinstance(data, dict):
        return None
    return data


def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    iid = ctx["id"]
    data = _fetch(iid)
    if data is None:
        return [], state  # transient — try again next tick

    mr_state = str(data.get("state") or "")
    has_conflicts = bool(data.get("has_conflicts"))
    pipeline = data.get("head_pipeline") or data.get("pipeline") or {}
    pipeline_status = str(pipeline.get("status") or "") if isinstance(pipeline, dict) else ""
    pipeline_id = str(pipeline.get("id") or "") if isinstance(pipeline, dict) else ""
    title = str(data.get("title") or f"MR !{iid}")
    web_url = str(data.get("web_url") or "")
    # GitLab's `user_notes_count` counts *all* notes including system notes
    # (pipeline status changes, label edits, assignee changes). `comment_added`
    # will therefore fire on some non-human events — accepted limitation for v1
    # to avoid a second API call to /notes per poll. If the field is absent,
    # keep it None so the rising-edge guard treats the next poll as a baseline
    # rather than locking the count at 0 forever.
    raw_notes = data.get("user_notes_count")
    notes_count = int(raw_notes) if isinstance(raw_notes, int) else None

    events: list[dict] = []
    prev_pipeline = state.get("pipeline_status", "")
    prev_state = state.get("mr_state", "")
    prev_conflicts = bool(state.get("has_conflicts", False))
    prev_notes_count = state.get("notes_count")  # None on first poll

    # Pipeline transitions
    if pipeline_status and pipeline_status != prev_pipeline:
        if pipeline_status == "failed":
            events.append({
                "event": "pipeline_failed",
                "payload": {"pipeline_id": pipeline_id, "url": web_url, "title": title},
                "notify_title": f"!{iid} pipeline failed",
                "notify_message": title,
            })
        elif pipeline_status == "success":
            events.append({
                "event": "pipeline_succeeded",
                "payload": {"pipeline_id": pipeline_id, "url": web_url, "title": title},
                "notify_title": f"!{iid} pipeline ok",
                "notify_message": title,
            })
        elif pipeline_status == "running" and prev_pipeline not in ("running", ""):
            events.append({
                "event": "pipeline_running",
                "payload": {"pipeline_id": pipeline_id, "url": web_url, "title": title},
            })

    # MR state transitions
    if mr_state and mr_state != prev_state:
        if mr_state == "merged":
            events.append({
                "event": "merged",
                "payload": {"url": web_url, "title": title},
                "notify_title": f"!{iid} merged",
                "notify_message": title,
            })
        elif mr_state == "closed":
            events.append({
                "event": "closed",
                "payload": {"url": web_url, "title": title},
                "notify_title": f"!{iid} closed",
                "notify_message": title,
            })

    # Conflict transitions (rising edge only — appeared, not resolved)
    if has_conflicts and not prev_conflicts:
        events.append({
            "event": "conflicts_appeared",
            "payload": {"url": web_url, "title": title},
            "notify_title": f"!{iid} conflicts",
            "notify_message": title,
        })

    # Notes count rising — new comment(s) since last poll. First poll
    # (prev_notes_count is None) records the baseline without firing. If the
    # current poll couldn't read the field (notes_count is None) we skip too
    # so we don't compare against a stale baseline.
    if (
        prev_notes_count is not None
        and notes_count is not None
        and notes_count > prev_notes_count
    ):
        delta = notes_count - prev_notes_count
        events.append({
            "event": "comment_added",
            "payload": {
                "url": web_url,
                "title": title,
                "new_count": delta,
            },
            "notify_title": f"!{iid} new comment{'s' if delta > 1 else ''}",
            "notify_message": title,
        })

    new_state = {
        "mr_state": mr_state,
        "pipeline_status": pipeline_status,
        "pipeline_id": pipeline_id,
        "has_conflicts": has_conflicts,
        "notes_count": notes_count,
        "title": title,
        "web_url": web_url,
    }
    return events, new_state


def is_terminal(state: dict) -> bool:
    if state.get("mr_state") in TERMINAL_MR_STATES:
        return True
    return False
