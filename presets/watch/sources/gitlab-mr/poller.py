"""gitlab-mr watcher source.

Polls a single GitLab merge request via `glab api` and emits events when
status changes. Terminal when the MR is merged or closed.

Source plugin contract:
- INTERVAL: int seconds between polls (30s default)
- poll(state, ctx) -> (events, new_state)
- is_terminal(state) -> bool
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

INTERVAL = 30

TERMINAL_MR_STATES = {"merged", "closed"}


def _glab_api(endpoint: str) -> dict | list | None:
    if not shutil.which("glab"):
        return None
    try:
        r = subprocess.run(
            ["glab", "api", endpoint],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
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

    events: list[dict] = []
    prev_pipeline = state.get("pipeline_status", "")
    prev_state = state.get("mr_state", "")
    prev_conflicts = bool(state.get("has_conflicts", False))

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

    new_state = {
        "mr_state": mr_state,
        "pipeline_status": pipeline_status,
        "pipeline_id": pipeline_id,
        "has_conflicts": has_conflicts,
        "title": title,
        "web_url": web_url,
    }
    return events, new_state


def is_terminal(state: dict) -> bool:
    if state.get("mr_state") in TERMINAL_MR_STATES:
        return True
    return False
