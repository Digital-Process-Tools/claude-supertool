











from __future__ import annotations

import importlib.util
import json
import subprocess
import time
from pathlib import Path
from typing import Any

INTERVAL = 30

TERMINAL_MR_STATES = {"merged", "closed"}







SETTLED_MERGE_STATUSES = {"can_be_merged", "cannot_be_merged"}




















NO_DIFF_DETAILED_STATUS = "commits_status"












NOT_APPROVED_STATUS = "not_approved"



_MR_MODULE_PATH = Path(__file__).parents[3] / "gitlab" / "mr.py"
_spec = importlib.util.spec_from_file_location("gitlab_mr_op", _MR_MODULE_PATH)
assert _spec is not None and _spec.loader is not None
_mr_op = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mr_op)
_glab_api_cli = _mr_op._glab_api  





_format_error = _mr_op._format_error  


def _glab_api(endpoint: str, what: str = "MR", identifier: str = "") -> tuple[dict | list | None, str]:











    label = f"#{identifier}" if identifier else what
    try:
        r = _glab_api_cli(endpoint)
    except FileNotFoundError:
        return None, "ERROR: glab not found — install the GitLab CLI"
    except subprocess.TimeoutExpired:
        return None, f"ERROR: glab timed out looking up {what} {label}"
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"ERROR: glab could not run for {what} {label}: {e}"
    if r.returncode != 0:
        return None, _format_error(r.stderr or "", what, identifier)
    try:
        return json.loads(r.stdout), ""
    except json.JSONDecodeError:
        return None, f"ERROR: invalid JSON from glab for {what} {label}"


def _fetch(iid: str) -> tuple[dict[str, Any] | None, str]:
    data, error = _glab_api(f"projects/:id/merge_requests/{iid}", "MR", str(iid))
    if error:
        return None, error
    if not isinstance(data, dict):
        return None, f"ERROR: unexpected payload shape from glab for MR #{iid}"
    return data, ""


def _fetch_approvals(iid: str) -> tuple[bool | None, str]:









    approvals, error = _glab_api(
        f"projects/:id/merge_requests/{iid}/approvals", "approvals", str(iid))
    if error:
        return None, error
    if not isinstance(approvals, dict):
        return None, (f"ERROR: approvals API returned a "
                       f"{type(approvals).__name__}, expected an object")
    if "approved" not in approvals:
        return None, "ERROR: approvals payload carries no approved field"
    return bool(approvals["approved"]), ""




















SNAPSHOT_PREFIX = "observed_"


def _snapshot(data: dict[str, Any], *, mr_state: str, pipeline_status: str,
              pipeline_id: str, pipeline_identity: str,
              has_conflicts: bool) -> dict[str, Any]:












    return {
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "observed_mr_state": mr_state,
        "observed_pipeline_status": pipeline_status,
        "observed_pipeline_id": pipeline_id,
        "observed_pipeline_identity": pipeline_identity,
        "observed_has_conflicts": bool(has_conflicts),
        "observed_source_branch": str(data.get("source_branch") or ""),
        "observed_target_branch": str(data.get("target_branch") or ""),
        "observed_head_sha": str(data.get("sha") or ""),
    }

























IDENTITY_SAME = "same"
IDENTITY_NEW = "new"
IDENTITY_UNKNOWN = "unknown"


def _pipeline_identity(pipeline_id: str, prev_pipeline_id: str,
                       prev_pipeline_status: str) -> str:










    if not prev_pipeline_id and not prev_pipeline_status:
        return IDENTITY_NEW
    if not pipeline_id or not prev_pipeline_id:
        return IDENTITY_UNKNOWN
    return IDENTITY_SAME if pipeline_id == prev_pipeline_id else IDENTITY_NEW






















FAILED_JOBS_MAX = 5
LOOKUP_OK = "ok"
LOOKUP_UNAVAILABLE = "unavailable"


def _failed_job_names(pipeline_id: str) -> list[str] | None:



































    if not pipeline_id:
        return None
    data, error = _glab_api(
        f"projects/:id/pipelines/{pipeline_id}/jobs?scope[]=failed&per_page=100",
        "Pipeline", str(pipeline_id))
    if error or not isinstance(data, list):





        return None
    jobs = [j for j in data if isinstance(j, dict)
            and j.get("status") == "failed" and not j.get("allow_failure")]
    jobs.sort(key=lambda j: (j.get("started_at") is None,
                             str(j.get("started_at") or ""),
                             str(j.get("id") or "")))
    names: list[str] = []
    for job in jobs:
        name = str(job.get("name") or "")



        if name and name not in names:
            names.append(name)
    return names


def _failed_jobs_fields(pipeline_id: str) -> dict[str, str]:







    names = _failed_job_names(pipeline_id)
    if names is None:
        return {
            "observed_failed_jobs": "",
            "observed_failed_job_count": "",
            "observed_failed_jobs_lookup": LOOKUP_UNAVAILABLE,
        }
    shown = names[:FAILED_JOBS_MAX]
    if len(names) > FAILED_JOBS_MAX:



        shown = [*shown, f"+{len(names) - FAILED_JOBS_MAX} more"]
    return {
        "observed_failed_jobs": ",".join(shown),
        "observed_failed_job_count": str(len(names)),
        "observed_failed_jobs_lookup": LOOKUP_OK,
    }


def _has_no_diff(data: dict[str, Any]) -> bool:












    if data.get("detailed_merge_status") == NO_DIFF_DETAILED_STATUS:
        return True
    if "sha" in data and not data.get("sha"):
        return True
    refs = data.get("diff_refs")
    if isinstance(refs, dict) and "head_sha" in refs:
        head = refs.get("head_sha")
        if not head:
            return True
        if head == refs.get("base_sha"):
            return True
    return False


def poll(state: dict, ctx: dict) -> tuple[list[dict], dict]:
    iid = ctx["id"]
    data, error = _fetch(iid)
    if data is None:

























        new_state = {**state, "lookup": LOOKUP_UNAVAILABLE, "error": error}
        if state.get("lookup") == LOOKUP_UNAVAILABLE:
            return [], new_state
        return [{
            "event": "mr_unreachable",
            "payload": {
                "iid": str(iid),
                "error": error,




                "last_known_mr_state": str(state.get("mr_state") or ""),
                "last_known_pipeline_status": str(state.get("pipeline_status") or ""),
                "title": str(state.get("title") or ""),
                "url": str(state.get("web_url") or ""),
            },
            "notify_title": f"!{iid} — cannot tell",
            "notify_message": error,
        }], new_state

    mr_state = str(data.get("state") or "")
    raw_conflicts = bool(data.get("has_conflicts"))
    merge_status = str(data.get("merge_status") or "")
    pipeline = data.get("head_pipeline") or data.get("pipeline") or {}
    pipeline_status = str(pipeline.get("status") or "") if isinstance(pipeline, dict) else ""
    pipeline_id = str(pipeline.get("id") or "") if isinstance(pipeline, dict) else ""
    title = str(data.get("title") or f"MR !{iid}")
    web_url = str(data.get("web_url") or "")


















    raw_notes = data.get("user_notes_count")
    notes_count = int(raw_notes) if isinstance(raw_notes, int) else None
    target_branch = str(data.get("target_branch") or "")
    detailed_status = str(data.get("detailed_merge_status") or "")

    events: list[dict] = []
    prev_pipeline = state.get("pipeline_status", "")





    prev_pipeline_id = str(state.get("pipeline_id") or "")
    prev_identity = str(state.get("pipeline_identity") or "")
    prev_state = state.get("mr_state", "")
    prev_conflicts = bool(state.get("has_conflicts", False))
    prev_notes_count = state.get("notes_count")  
    prev_target_branch = str(state.get("target_branch") or "")
    prev_approved = state.get("approved")  
    prev_detailed_status = str(state.get("detailed_status") or "")





























    if detailed_status == NOT_APPROVED_STATUS:
        approved, _approved_error = False, ""
    elif prev_detailed_status == NOT_APPROVED_STATUS or prev_approved is False:
        approved, _approved_error = _fetch_approvals(iid)
    else:
        approved, _approved_error = None, ""





    conflicts_settled = merge_status in SETTLED_MERGE_STATUSES or not merge_status
    has_conflicts = raw_conflicts if conflicts_settled else prev_conflicts


    if has_conflicts and _has_no_diff(data):
        has_conflicts = False





    identity = _pipeline_identity(pipeline_id, prev_pipeline_id, prev_pipeline)

    snap = _snapshot(
        data,
        mr_state=mr_state,
        pipeline_status=pipeline_status,
        pipeline_id=pipeline_id,
        pipeline_identity=identity,
        has_conflicts=has_conflicts,
    )













    is_transition = bool(pipeline_status) and (
        pipeline_status != prev_pipeline
        or identity == IDENTITY_NEW
        or (identity == IDENTITY_UNKNOWN and prev_identity != IDENTITY_UNKNOWN)
    )
    if is_transition:
        if pipeline_status == "failed":






            events.append({
                "event": "pipeline_failed",
                "payload": {"pipeline_id": pipeline_id, "url": web_url, "title": title,
                            **snap, **_failed_jobs_fields(pipeline_id)},
                "notify_title": f"!{iid} pipeline failed",
                "notify_message": title,
            })
        elif pipeline_status == "success":
            events.append({
                "event": "pipeline_succeeded",
                "payload": {"pipeline_id": pipeline_id, "url": web_url, "title": title, **snap},
                "notify_title": f"!{iid} pipeline ok",
                "notify_message": title,
            })




        elif pipeline_status == "running" and prev_pipeline not in ("running", ""):
            events.append({
                "event": "pipeline_running",
                "payload": {"pipeline_id": pipeline_id, "url": web_url, "title": title, **snap},
            })











    if mr_state and mr_state != prev_state:
        if mr_state == "merged":
            events.append({
                "event": "merged",
                "payload": {"url": web_url, "title": title, **snap},
                "notify_title": f"!{iid} merged",
                "notify_message": title,
            })
        elif mr_state == "closed":
            events.append({
                "event": "closed",
                "payload": {"url": web_url, "title": title, **snap},
                "notify_title": f"!{iid} closed",
                "notify_message": title,
            })





    if has_conflicts and not prev_conflicts:
        events.append({
            "event": "conflicts_appeared",
            "payload": {"url": web_url, "title": title, **snap},
            "notify_title": f"!{iid} conflicts",
            "notify_message": title,
        })





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
                **snap,
            },
            "notify_title": f"!{iid} new comment{'s' if delta > 1 else ''}",
            "notify_message": title,
        })








    new_approved = approved if approved is not None else prev_approved
    if prev_approved is False and approved is True:
        events.append({
            "event": "approved",
            "payload": {"url": web_url, "title": title, **snap},
            "notify_title": f"!{iid} approved",
            "notify_message": title,
        })





    if prev_target_branch and target_branch and target_branch != prev_target_branch:
        events.append({
            "event": "retargeted",
            "payload": {
                "url": web_url, "title": title,
                "from_branch": prev_target_branch, "to_branch": target_branch,
                **snap,
            },
            "notify_title": f"!{iid} retargeted",
            "notify_message": f"{prev_target_branch} -> {target_branch}",
        })

    new_state = {
        "mr_state": mr_state,
        "pipeline_status": pipeline_status,
        "pipeline_id": pipeline_id or prev_pipeline_id,
        "pipeline_identity": identity,
        "has_conflicts": has_conflicts,
        "merge_status": merge_status,
        "notes_count": notes_count,
        "title": title,
        "web_url": web_url,
        "lookup": LOOKUP_OK,
        "target_branch": target_branch or prev_target_branch,
        "approved": new_approved,
        "detailed_status": detailed_status,
    }
    return events, new_state


def is_terminal(state: dict) -> bool:
    if state.get("mr_state") in TERMINAL_MR_STATES:
        return True
    return False
