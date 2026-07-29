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
import time
from pathlib import Path
from typing import Any

INTERVAL = 30

TERMINAL_MR_STATES = {"merged", "closed"}

# GitLab computes mergeability asynchronously. `has_conflicts` is only an
# answer once the check has settled — in `unchecked`, `checking` and
# `cannot_be_merged_recheck` the field reads False on an MR whose conflict was
# never touched. Every push to the target branch puts every open MR back into
# recheck, so believing False there drops the conflict latch and re-arms the
# rising edge: one standing conflict, one "appeared" per push to master (#463).
SETTLED_MERGE_STATUSES = {"can_be_merged", "cannot_be_merged"}

# ...and even settled, `has_conflicts` is not a conflict field. GitLab's API
# entity exposes it as an alias for `cannot_be_merged?` and says so directly:
#
#     # #cannot_be_merged? is generally indicative of conflicts, and is set via
#     #   MergeRequests::MergeabilityCheckService. However, it can also indicate
#     #   that either #has_no_commits? or #branch_missing? are true.
#     expose :cannot_be_merged?, as: :has_conflicts
#
# So it lies in exactly one direction — an MR with no diff — and the
# discriminator is the diff, not the reason the merge is blocked (#465).
#
# `detailed_merge_status` deliberately is *not* the gate. It reports only the
# first failing check, and conflict is dead last in
# `MergeRequest.all_mergeability_checks` while draft is second, so a conflicted
# draft reports `draft_status`. Requiring `== "conflict"` would silently stop
# reporting real conflicts on drafts, blocked threads and unrebased branches —
# strictly worse than the false positive being fixed. It is used here only in
# the one place it is unambiguous: `commits_status` is GitLab's own identifier
# for "source branch exists and contains commits", i.e. no diff.
NO_DIFF_DETAILED_STATUS = "commits_status"

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


# The whole MR is already in memory every tick, and a consumer that has to call
# back for `mr_state` or a branch name is paying a round-trip for something the
# poller threw away ~20s earlier (#435). What rides along is fixed and small:
# eight fields, ~200 bytes, none of which grows with the size of the MR.
#
# It is a *snapshot*, and this repository's recurring defect is a value that
# reads as authoritative while being an artefact of when the tool happened to
# look. Two properties keep that from happening here:
#
# 1. Every key is `observed_`-prefixed, so the tense is present at the read
#    site and no amount of destructuring can turn one into a bare `mr_state`.
# 2. `observed_at` is always emitted beside them — an absolute instant, not an
#    age. An age is correct for one second and quietly wrong afterwards, which
#    is the exact failure the field exists to prevent. The consumer subtracts.
#
# Flat, not nested, because `notifiers/claude-channel` renders each payload key
# as an XML string attribute via `String(v)` — a nested object would arrive as
# `[object Object]`, i.e. invisible on the one surface #435 was reported from.
SNAPSHOT_PREFIX = "observed_"


def _snapshot(data: dict[str, Any], *, mr_state: str, pipeline_status: str,
              pipeline_id: str, has_conflicts: bool) -> dict[str, Any]:
    """The fetched state, labelled with the instant it was read.

    `has_conflicts` is passed in rather than read off `data` on purpose: it is
    the poller's *corrected* answer, after the unsettled-check carry-forward
    (#463) and the empty-diff guard (#465). Re-reading the raw field here would
    re-export a false positive the poller had just finished suppressing.
    """
    return {
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "observed_mr_state": mr_state,
        "observed_pipeline_status": pipeline_status,
        "observed_pipeline_id": pipeline_id,
        "observed_has_conflicts": bool(has_conflicts),
        "observed_source_branch": str(data.get("source_branch") or ""),
        "observed_target_branch": str(data.get("target_branch") or ""),
        "observed_head_sha": str(data.get("sha") or ""),
    }


def _has_no_diff(data: dict[str, Any]) -> bool:
    """True only on positive evidence that the MR contains no diff.

    Absent fields are not evidence: a payload without `sha`/`diff_refs` leaves
    `has_conflicts` trusted, so the guard can never invent a reason to stay
    quiet about a conflict it simply could not see.

    Three signals, all observed live:
    - `detailed_merge_status: commits_status` — !33194, zero commits.
    - `sha: null` — !33223 as reported in #465, MR opened before any push.
    - `diff_refs.head_sha` null, or equal to `base_sha` — the source branch tip
      *is* the merge base, so it carries nothing the target does not have.
    """
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
    data = _fetch(iid)
    if data is None:
        return [], state  # transient — try again next tick

    mr_state = str(data.get("state") or "")
    raw_conflicts = bool(data.get("has_conflicts"))
    merge_status = str(data.get("merge_status") or "")
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

    # An unsettled check means "not computed yet", not "clean", so the last
    # known answer is carried forward and nothing is emitted. A response with
    # no `merge_status` at all is a different case: it is not evidence of an
    # unsettled check, so `has_conflicts` is taken at face value.
    conflicts_settled = merge_status in SETTLED_MERGE_STATUSES or not merge_status
    has_conflicts = raw_conflicts if conflicts_settled else prev_conflicts
    # No diff, nothing to conflict — whatever `has_conflicts` claims, and
    # whatever was latched before a force-push emptied the source branch.
    if has_conflicts and _has_no_diff(data):
        has_conflicts = False

    # One fetch, one snapshot, shared by every event this tick emits. Built
    # once so two events from the same poll can never disagree about what was
    # observed — the #435 session saw a single tick emit both
    # `pipeline_succeeded` and `merged`, and they describe the same read.
    snap = _snapshot(
        data,
        mr_state=mr_state,
        pipeline_status=pipeline_status,
        pipeline_id=pipeline_id,
        has_conflicts=has_conflicts,
    )

    # Pipeline transitions
    if pipeline_status and pipeline_status != prev_pipeline:
        if pipeline_status == "failed":
            events.append({
                "event": "pipeline_failed",
                "payload": {"pipeline_id": pipeline_id, "url": web_url, "title": title, **snap},
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

    # MR state transitions.
    #
    # #435 asked for a top-level `pipeline_id` here so a merge could be tied to
    # the pipeline that permitted it. The gap is real; a top-level field is the
    # wrong fix. `payload.pipeline_id` is read by `radar.drift()` to decide an
    # event is stale history superseded by a newer pipeline — a merge joining
    # that comparison would put `[drift: A→B]` on the board for a fact nobody
    # reported. And its meaning would then differ per event key: "the pipeline
    # this event is about" on `pipeline_*`, "the head pipeline at the time" here.
    # `observed_pipeline_id` says the second thing, uniformly, on every event.
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

    # Conflict rising edge — appeared, not resolved. The latch is only ever
    # released by a *settled* clean check, which is what keeps this re-armable:
    # a conflict that is genuinely resolved and later returns fires again,
    # while one that merely goes un-recomputed does not.
    if has_conflicts and not prev_conflicts:
        events.append({
            "event": "conflicts_appeared",
            "payload": {"url": web_url, "title": title, **snap},
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
                **snap,
            },
            "notify_title": f"!{iid} new comment{'s' if delta > 1 else ''}",
            "notify_message": title,
        })

    new_state = {
        "mr_state": mr_state,
        "pipeline_status": pipeline_status,
        "pipeline_id": pipeline_id,
        "has_conflicts": has_conflicts,
        "merge_status": merge_status,
        "notes_count": notes_count,
        "title": title,
        "web_url": web_url,
    }
    return events, new_state


def is_terminal(state: dict) -> bool:
    if state.get("mr_state") in TERMINAL_MR_STATES:
        return True
    return False
