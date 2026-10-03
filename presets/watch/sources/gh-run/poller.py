



























from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

INTERVAL = 30









_GITHUB_DIR = Path(__file__).parents[3] / "github"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _GITHUB_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_gh = _load("github_pr_op", "pr.py")._gh  
_format_error = _load("github_run_op", "run.py")._format_error  

_WATCH_DIR = Path(__file__).parents[2]


def _load_watch(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _WATCH_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod





ratelimit = _load_watch("watch_gh_run_ratelimit", "ratelimit.py")


_VIEW_FIELDS = "databaseId,status,conclusion,workflowName,url,headBranch,event"


TERMINAL_RUN_STATUS = "completed"







RED_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}
VERDICTLESS_CONCLUSIONS = {"neutral", "skipped", "stale"}

LOOKUP_OK = "ok"
LOOKUP_UNAVAILABLE = "unavailable"


def _fetch(run_id: str) -> tuple[dict[str, Any] | None, str]:









    try:
        r = _gh(["run", "view", run_id, "--json", _VIEW_FIELDS], timeout=15)
    except subprocess.TimeoutExpired:
        return None, f"ERROR: gh timed out looking up run #{run_id}"
    except FileNotFoundError:
        return None, "ERROR: gh not found — install the GitHub CLI"
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









        extra = ratelimit.unreachable_extra(error)
        new_state = {**state, "lookup": LOOKUP_UNAVAILABLE, "error": error, **extra}
        if state.get("lookup") == LOOKUP_UNAVAILABLE:
            return [], new_state
        label = f"run #{run_id}"
        payload = {
            "run_id": run_id,
            "error": error,
            "last_known_status": str(state.get("status") or ""),
            "url": str(state.get("url") or ""),
        }
        if "retry_after" in extra:
            payload["retry_after"] = extra["retry_after"]
        return [{
            "event": "run_unreachable",
            "payload": payload,
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
