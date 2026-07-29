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
import subprocess
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
    """JSON-decode an _glab_api CLI call. None on any failure.

    `TimeoutExpired` is caught with the rest: it is a `SubprocessError`, not an
    `OSError`, so it used to propagate out of `poll()` and kill the tick. That
    was survivable while `_fetch` was the only call; it stops being survivable
    once a failure transition makes a second one. None is not swallowing the
    failure — every caller here turns it into a reported "could not tell".
    """
    try:
        r = _glab_api_cli(endpoint)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
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
              pipeline_id: str, pipeline_identity: str,
              has_conflicts: bool) -> dict[str, Any]:
    """The fetched state, labelled with the instant it was read.

    `has_conflicts` is passed in rather than read off `data` on purpose: it is
    the poller's *corrected* answer, after the unsettled-check carry-forward
    (#463) and the empty-diff guard (#465). Re-reading the raw field here would
    re-export a false positive the poller had just finished suppressing.

    `pipeline_identity` is not an MR fact at all — it is a fact about the
    *read*: whether this poll could tell the head pipeline apart from the one
    the previous poll saw (#537). It rides on every event for the reason
    `observed_pipeline_id` does: one meaning, uniformly, on every key.
    """
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


# Which pipeline is this, compared to the one the last poll saw (#537).
#
# The pipeline events used to edge-trigger on the status *string* alone, with no
# pipeline identity in the comparison. A second pipeline that also ended
# `failed`, with no `running` tick observed in between, therefore fired nothing:
# the previous status was already `"failed"`, the inequality was False, and the
# MR was red for a new reason in silence. Not a wrong value — no output at all
# to be suspicious of, which is this repository's house defect at its purest.
#
# **The retry question, settled against the live API rather than the docs,
# because getting it wrong would trade a silent miss for a duplicate.** GitLab
# does **not** mint a new pipeline id when a job is retried. Pipeline 154635 was
# observed mid-retry: `test_unit_pavillon` failed as job 6966698, was retried as
# job 6967497, and `head_pipeline` went on reporting id **154635** with its
# status flipped back to `running`. A retry moves the status under a *stable*
# id — which is the edge the old code already computed correctly — so adding an
# id comparison cannot double-fire it. A genuinely new pipeline comes from a new
# push or trigger and takes a new id (MR !33194: 154628 red, push, 154636 red).
#
# Three verdicts, not two. `unknown` exists because the id can be missing from a
# payload, or absent from a state file written before this field was persisted,
# and "we could not tell" must not resolve to "same pipeline, stay quiet" —
# that is the silence the fix is about, reintroduced one level down.
IDENTITY_SAME = "same"
IDENTITY_NEW = "new"
IDENTITY_UNKNOWN = "unknown"


def _pipeline_identity(pipeline_id: str, prev_pipeline_id: str,
                       prev_pipeline_status: str) -> str:
    """`same`, `new` or `unknown` — never a guess dressed as one of the first two.

    `unknown` is returned whenever either side has no id to compare, and it is
    an *answer*, not a shrug: the caller announces it once (see `poll`) rather
    than folding it into "nothing changed".

    The first pipeline ever seen for an MR is `new`, not `unknown` — there is no
    previous read to be uncertain about, and calling that case unknown would put
    a permanent caveat on the very first event of every watch.
    """
    if not prev_pipeline_id and not prev_pipeline_status:
        return IDENTITY_NEW
    if not pipeline_id or not prev_pipeline_id:
        return IDENTITY_UNKNOWN
    return IDENTITY_SAME if pipeline_id == prev_pipeline_id else IDENTITY_NEW


# The failing job names on `pipeline_failed` (#509, option 3 of #435).
#
# This is the ONLY thing in the payload that is not free. Everything #508 added
# was already in memory; this costs one `?scope[]=failed` request. Two choices
# keep that bounded, and both are load-bearing:
#
# 1. **The call lives inside the `pipeline_status == "failed"` transition
#    branch**, which is edge-triggered — so a pipeline that sits red for an hour
#    is looked up once, not 120 times. Nothing on the common path pays.
# 2. **`?scope[]=failed`** rather than the full job list. Real pipelines here run
#    114-139 jobs, which is two paginated pages of mostly `created`/`manual`
#    bulk; the scoped query is one page and usually a handful of rows.
#
# What comes back is a *set of parallel failures* far more often than a single
# cause — one observed pipeline had eight `test_unit_*` jobs fail within 2.4
# seconds of each other. So no single job is elected "the" failure: naming one
# of eight would be arbitrary and yet read as authoritative, which is this
# repository's house defect wearing a new hat. A bounded, ordered list ships
# instead, flat-encoded as a joined string because `notifiers/claude-channel`
# stringifies each payload key into an XML attribute.
FAILED_JOBS_MAX = 5
LOOKUP_OK = "ok"
LOOKUP_UNAVAILABLE = "unavailable"


def _failed_job_names(pipeline_id: str) -> list[str] | None:
    """Names of the jobs that made the pipeline red. `None` when we could not look.

    **`None` and `[]` are different answers and callers must not merge them.**
    `[]` means GitLab answered and recorded no pipeline-failing job; `None`
    means the request failed, timed out, returned junk, or was never sent. An
    absence produced by the tool is not an absence in the world, and a red
    pipeline reporting "0 jobs failed" because a request fell over is exactly
    the defect this repository keeps filing.

    Two filters, both from live data:

    - `allow_failure: true` jobs are dropped. They fail without making the
      pipeline red, so naming one sends the reader to the wrong log. Observed
      on pipeline 154527, where the allow-failure job sorts *first* by every
      candidate ordering and would therefore have been the name on the wire.
    - `status == "failed"` is re-checked even though the query asked for it.
      The scope filter is a request, not a guarantee, and an unfiltered
      response would otherwise turn every green job into a reported failure.

    Ordering is **ascending `started_at`**, jobs that never started last, job id
    as a deterministic tiebreak. The two alternatives were checked and rejected
    against the live API rather than by taste:

    - *GitLab's own order* is descending id — it hands back the **last** failure
      first (pipeline 154599: `test_unit_dpt` at :18.595 ahead of
      `test_unit_modular` at :15.216). Wrong end.
    - *Ascending job id* is not stage order. Pipeline 154527 allocates
      `conformity_basic` a **lower** id (6953208) than the `unit` jobs
      (6953222+) while running it six minutes **later**, so sorting by id would
      claim a stage ran first that demonstrably ran second.

    Start order is still chronology, not causality — parallel jobs that fail
    together are not a cascade, and the list is not a claim about which broke
    which.
    """
    if not pipeline_id:
        return None
    data = _glab_api(
        f"projects/:id/pipelines/{pipeline_id}/jobs?scope[]=failed&per_page=100")
    if not isinstance(data, list):
        return None
    jobs = [j for j in data if isinstance(j, dict)
            and j.get("status") == "failed" and not j.get("allow_failure")]
    jobs.sort(key=lambda j: (j.get("started_at") is None,
                             str(j.get("started_at") or ""),
                             str(j.get("id") or "")))
    names: list[str] = []
    for job in jobs:
        name = str(job.get("name") or "")
        # A retried job keeps its name and takes a new id, so both attempts come
        # back failed. The reader wants the set of broken things, not a tally of
        # attempts at the same one.
        if name and name not in names:
            names.append(name)
    return names


def _failed_jobs_fields(pipeline_id: str) -> dict[str, str]:
    """The three flat `observed_failed_job*` keys, in all three states.

    `observed_failed_job_count` is `""` — never `"0"` — when the lookup failed,
    so a consumer that reads only the count still cannot mistake "we could not
    look" for "nothing failed". `observed_failed_jobs_lookup` says which of the
    two it was in one word, for a consumer that would rather branch than guess.
    """
    names = _failed_job_names(pipeline_id)
    if names is None:
        return {
            "observed_failed_jobs": "",
            "observed_failed_job_count": "",
            "observed_failed_jobs_lookup": LOOKUP_UNAVAILABLE,
        }
    shown = names[:FAILED_JOBS_MAX]
    if len(names) > FAILED_JOBS_MAX:
        # The overflow marker goes *inside* the joined string, not only in the
        # count: a surface rendering this single attribute would otherwise read
        # five names as the whole story.
        shown = [*shown, f"+{len(names) - FAILED_JOBS_MAX} more"]
    return {
        "observed_failed_jobs": ",".join(shown),
        "observed_failed_job_count": str(len(names)),
        "observed_failed_jobs_lookup": LOOKUP_OK,
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
    # `user_notes_count` counts **human** notes only — GitLab scopes it over
    # `Note.user`, which is `where(system: false)`, so label edits, assignee
    # changes, approvals and time-tracking entries do not move it.
    #
    # This comment used to claim the opposite, and nobody checked. That claim
    # kept `comment_added` out of the default event set and produced two filed
    # issues (#417 item 3, #519), the second proposing a `/notes?system=false`
    # call on every poll of every watched MR to fix a defect that did not
    # exist. Re-derived against the live instance (GitLab 18.11.7) over twelve
    # merge requests; `user_notes_count` equalled the number of `system: false`
    # notes on all twelve, including !19509 — 75 system notes, count 0.
    #
    # What the count still cannot do is say *who* commented, so `comment_added`
    # fires on your own comments too. That is a real limit, documented in
    # docs/presets/watch.md, and it is not what the event was held back for.
    #
    # If the field is absent, keep it None so the rising-edge guard treats the
    # next poll as a baseline rather than locking the count at 0 forever.
    raw_notes = data.get("user_notes_count")
    notes_count = int(raw_notes) if isinstance(raw_notes, int) else None

    events: list[dict] = []
    prev_pipeline = state.get("pipeline_status", "")
    # The last id we could actually *read*, not the last poll's raw value. A
    # poll that reports no id carries the previous one forward, the same shape
    # the unsettled-conflict check uses below: an unreadable field is not
    # evidence that the world changed, and overwriting with `""` would make the
    # next readable id incomparable and so silently un-announceable.
    prev_pipeline_id = str(state.get("pipeline_id") or "")
    prev_identity = str(state.get("pipeline_identity") or "")
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
    identity = _pipeline_identity(pipeline_id, prev_pipeline_id, prev_pipeline)

    snap = _snapshot(
        data,
        mr_state=mr_state,
        pipeline_status=pipeline_status,
        pipeline_id=pipeline_id,
        pipeline_identity=identity,
        has_conflicts=has_conflicts,
    )

    # Pipeline transitions (#537).
    #
    # The edge is still an edge — a pipeline sitting red across a hundred polls
    # is announced once, which is what keeps a long-lived radar session from
    # filling with repeats. What changed is what the edge is computed *from*:
    # the pair (status, which pipeline), not the status string alone.
    #
    # `unknown` fires once per streak, not once per poll. Announcing it every
    # tick would trade the silent failure for a flood, and a flood gets muted,
    # which is the silence again by a longer route. `prev_identity` is what
    # makes it once — and the id carry-forward above is what makes it re-armable
    # when the id becomes readable again.
    is_transition = bool(pipeline_status) and (
        pipeline_status != prev_pipeline
        or identity == IDENTITY_NEW
        or (identity == IDENTITY_UNKNOWN and prev_identity != IDENTITY_UNKNOWN)
    )
    if is_transition:
        if pipeline_status == "failed":
            # The one extra request, and it is made here rather than beside the
            # snapshot so that its cost is exactly the shape of this branch:
            # once per transition into red, never on a green or idle tick. The
            # three keys ride on `pipeline_failed` alone — `merged` has no
            # failing-job concept, and three blank attributes on every event
            # would be wire noise that invites a blank to be read as a fact.
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
        # Deliberately unchanged by #537: a new pipeline that starts while the
        # previous one was still running stays quiet. `pipeline_running` is not
        # in `DEFAULT_ONLY` precisely because you just pushed and it carries no
        # information; a second one carries no more.
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
        "pipeline_id": pipeline_id or prev_pipeline_id,
        "pipeline_identity": identity,
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
