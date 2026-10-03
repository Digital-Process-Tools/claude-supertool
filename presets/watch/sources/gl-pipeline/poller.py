

















from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

INTERVAL = 30


TERMINAL_PIPELINE_STATES = {"success", "failed", "canceled", "skipped"}









_MR_MODULE_PATH = Path(__file__).parents[3] / "gitlab" / "mr.py"
_spec = importlib.util.spec_from_file_location("gitlab_mr_op", _MR_MODULE_PATH)
assert _spec is not None and _spec.loader is not None
_mr_op = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mr_op)
_glab_api_cli = _mr_op._glab_api  
_format_error = _mr_op._format_error  

LOOKUP_OK = "ok"
LOOKUP_UNAVAILABLE = "unavailable"


def _glab_api(endpoint: str, pipeline_id: str) -> tuple[dict | list | None, str]:










    try:
        r = _glab_api_cli(endpoint)
    except FileNotFoundError:
        return None, "ERROR: glab not found — install the GitLab CLI"
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










        new_state = {**state, "lookup": LOOKUP_UNAVAILABLE, "error": error}
        if state.get("lookup") == LOOKUP_UNAVAILABLE:
            return [], new_state
        return [{
            "event": "pipeline_unreachable",
            "payload": {
                "pipeline_id": pipeline_id,
                "error": error,



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
