#!/usr/bin/env python3
"""GitLab merge request list via glab CLI — triage board.

`glab mr list` shows a flat table. This op adds the three things it omits
but you need next: per-MR pipeline status (enriched), watch-state cross-
reference (which MRs already have a `watch` poller running), and an
actionable footer hint. "Mine" is just the default filter (author=@me),
not a special mode — compose any filter.

Usage:
    gl-mrs                          my open MRs, pipeline-enriched
    gl-mrs:reviewer=@me             where I'm reviewer
    gl-mrs:author=@me,state=merged  filter composition
    gl-mrs:milestone=v18.9          reusable beyond watching
    gl-mrs:nopipe                   skip pipeline enrichment (fast, 1 call)
    gl-mrs:iids                     bare iid list (for piping into watch)
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # for _board, _proc

import _board  # noqa: E402
import _filter_tokens  # noqa: E402  (the one tokenizer + refusal, shared with gh-issues / gh-prs)
import _untrusted  # noqa: E402  (the repo's remote-text convention)
import _auth_probe  # noqa: E402  (does this stderr *state* that the credential is unusable? - #1846)
from _env import env_int  # noqa: E402  (the one numeric-knob reader)
import _checks  # noqa: E402  (the one check classifier, shared with gh-pr / gh-prs)
import _proc  # noqa: E402  (the one liveness probe, shared with watch / gh-prs)

WATCH_SOURCE = "gitlab-mr"
STATE_DIR = "/tmp"
DEFAULT_PER_PAGE = 50
ENRICH_CAP = 40  # never fire more than this many per-MR pipeline calls
ENRICH_WORKERS = 8  # parallel pipeline fetches

# Tokens that are flags, not key=value filters.
_FLAGS = {"nopipe", "iids", "failed"}


def _get_config() -> dict[str, int]:
    """Read tunable knobs from SUPERTOOL_ env vars (set from .supertool.json).

    SUPERTOOL_ENRICH_WORKERS — parallel pipeline fetches (default 8)
    SUPERTOOL_ENRICH_CAP     — max MRs to pipeline-enrich (default 40)
    SUPERTOOL_PER_PAGE       — MRs fetched from the list endpoint (default 50)
    """
    # The local `_int` this replaces tolerated junk in silence and then clamped
    # in silence, so a caller who set SUPERTOOL_ENRICH_CAP=-1 got "no enrichment"
    # with nothing said. `env_int` keeps the floors as validated minimums and
    # says which knob it could not honour and what it used instead (#654).
    return {
        "enrich_workers": env_int("SUPERTOOL_ENRICH_WORKERS", ENRICH_WORKERS, minimum=1),
        "enrich_cap": env_int("SUPERTOOL_ENRICH_CAP", ENRICH_CAP, minimum=0),
        "per_page": env_int("SUPERTOOL_PER_PAGE", DEFAULT_PER_PAGE, minimum=1),
    }

# state=X maps to a glab list flag. opened is the glab default (no flag).
_STATE_FLAG = {"merged": "--merged", "closed": "--closed", "all": "--all"}

# Filter key -> glab list flag.
_FILTER_FLAG = {
    "author": "--author",
    "assignee": "--assignee",
    "reviewer": "--reviewer",
    "label": "--label",
    "milestone": "--milestone",
    "source-branch": "--source-branch",
    "target-branch": "--target-branch",
    # `glab mr list --help`: "Filter by <string> in title and description."
    # Server-side, so it narrows the population rather than the page (#1395).
    "search": "--search",
}

# What `search=` is here, and what it is NOT (#1395).
#
# `gh-issues` spells the same key `search=` and runs a different engine. The
# shared spelling is honest only because the scope is stated: GitHub's issue
# search reads comments, GitLab's `search=` does not, and a caller who learned
# the key on one side would otherwise carry the wrong expectation to the other
# and read a zero as "nobody has mentioned this". Naming the half that is not
# covered is the whole point — an unstated narrowing is an absence produced by
# the tool, read as an absence in the world.
SEARCH_ENGINE = "GitLab search"
SEARCH_SCOPE = "title and description only — comments are NOT searched"

# opened is glab's default and therefore has no flag, which is exactly why a
# value with no mapping is dangerous: it emits nothing and the default board
# renders as the filtered one.
_STATES = {"opened"} | set(_STATE_FLAG)

# Filter keys this op forwards. Anything else is refused rather than dropped —
# `_build_list_cmd` skips a key it does not know, so `milestne=v18.9` used to
# return the whole board as that milestone's contents (#939). GitLab's filter
# surface is wider than GitHub's (milestone and the branch pair have list flags
# here and do not on `gh pr list`), so the vocabularies differ per op even
# though the grammar and the refusal are shared (#628).
_FILTER_KEYS = set(_FILTER_FLAG) | {"state", "per"}

# Keys whose value this op maps rather than forwards.
_VALUE_DOMAINS: dict[str, object] = {
    "state": _STATES,
    "per": _filter_tokens.POSITIVE_INT,
}


def _parse_multi(arg_str: str) -> tuple[dict[str, list[str]], set[str], list[str]]:
    """Tokenise a comma-separated arg string, keeping every value of a key.

    Comma-separated so the single supertool arg segment never collides with
    the ':' op tokenizer. A repeated key accumulates rather than overwriting,
    which is how a caller asks for more than one author: GitLab's list
    endpoint takes one `author_username`, so `author=a,author=b` is two
    queries unioned, not one. `_parse_args` is the scalar view of this, so
    both readings of an arg string come from one tokenizer.

    The third return value is every token that was placed nowhere. It used to
    be discarded here, which is how a typo'd key produced the unfiltered board
    with nothing said (#939).
    """
    return _filter_tokens.parse_multi(arg_str, _FILTER_KEYS, _FLAGS)


def _parse_args(arg_str: str) -> tuple[dict[str, str], set[str], list[str]]:
    """Split a comma-separated arg string into (filters, flags, unrecognised).

    'author=@me,state=merged,nopipe' becomes
    ({'author': '@me', 'state': 'merged'}, {'nopipe'}, []).
    A repeated key resolves to its last value — one query, one value.
    """
    return _filter_tokens.parse(arg_str, _FILTER_KEYS, _FLAGS)


def _unknown_error(unknown: list[str]) -> str:
    """Name every token that was not applied, and what would have been."""
    return _filter_tokens.unknown_error(unknown, _FILTER_KEYS, _FLAGS)


def _bad_values(filters: dict[str, str]) -> list[tuple[str, str, str]]:
    """Known keys carrying a value this op has no mapping for."""
    return _filter_tokens.bad_values(filters, _VALUE_DOMAINS)


def _expand_filters(multi: dict[str, list[str]]) -> list[dict[str, str]]:
    """One scalar filter dict per combination of the multi-valued keys.

    The list endpoint accepts a single value per key, so a key carrying N
    values fans out into N queries whose results are unioned by iid. Key order
    is preserved so a single-valued arg string produces exactly the dict
    `_parse_args` would have produced, and therefore exactly the same argv.
    """
    combos: list[dict[str, str]] = [{}]
    for key, values in multi.items():
        combos = [{**combo, key: val} for combo in combos for val in values]
    return combos


def _build_list_cmd(filters: dict[str, str], per_page: int) -> list[str]:
    """Build the `glab mr list ... -F json` argv from parsed filters.

    Defaults to author=@me when no role filter is given so the bare `gl-mrs`
    means 'mine'. state=opened is glab's default (no flag emitted).
    """
    has_role = any(k in filters for k in ("author", "assignee", "reviewer"))
    cmd = ["glab", "mr", "list", "-F", "json", "-P", str(per_page)]
    if not has_role:
        cmd += ["--author", "@me"]
    for key, val in filters.items():
        if key == "state":
            flag = _STATE_FLAG.get(val)
            if flag:
                cmd.append(flag)
        elif key in _FILTER_FLAG and val:
            cmd += [_FILTER_FLAG[key], val]
    return cmd


def _search_note(query: str) -> str:
    """Which engine answered, and what it looked at — one wording, one source.

    Printed once per render: above the board when there are rows, or inside
    the `No MRs match ...` line when there are none. Never both.
    """
    return f"search {query!r} — {SEARCH_ENGINE} over {SEARCH_SCOPE}"


def _run(cmd: list[str], timeout: int = 25) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")


def _api_json(endpoint: str, timeout: int = 10):
    """GET a glab api endpoint, JSON-decoded. None on any failure."""
    try:
        r = _run(["glab", "api", endpoint], timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def _fetch_mr_detail(iid: str) -> dict:
    """Full MR object (pipeline, changes_count, …). {} on failure.

    The list endpoint omits these, so this is the one unavoidable call per MR
    — but it carries pipeline status+url AND diff size in a single response.
    """
    data = _api_json(f"projects/:id/merge_requests/{iid}")
    return data if isinstance(data, dict) else {}


def _fetch_approvals(iid: str) -> dict:
    """Approval state for one MR: {approved: bool, approved_by: [user]}."""
    data = _api_json(f"projects/:id/merge_requests/{iid}/approvals")
    if not isinstance(data, dict):
        return {}
    by = [
        (e.get("user") or {}).get("username")
        for e in (data.get("approved_by") or [])
        if isinstance(e, dict)
    ]
    return {"approved": data.get("approved"), "approved_by": [u for u in by if u]}


def _fetch_failed_jobs(pipeline_id: str) -> list[str]:
    """Names of failed jobs in a pipeline — the failure class in one word.

    `?scope=failed` is the reliable filter; the unscoped jobs list can page
    past the failed job. The job name (phpstan2, test_unit_dpt, rector) tells
    you *what* broke without a follow-up gl-job call.
    """
    data = _api_json(f"projects/:id/pipelines/{pipeline_id}/jobs?scope=failed&per_page=100")
    if not isinstance(data, list):
        return []
    return [str(j.get("name")) for j in data if isinstance(j, dict) and j.get("name")]


def _enrich(
    mrs: list[dict],
    cap: int = ENRICH_CAP,
    workers: int = ENRICH_WORKERS,
    with_approvals: bool = True,
) -> None:
    """Fill enrichment fields on up to `cap` MRs, fetched in parallel.

    Wave 1: MR detail (+ approvals) per MR — pipeline status/url, diff size,
    approval state. Wave 2: failed-job names, but only for MRs whose pipeline
    actually failed (keeps the extra calls proportional to the problem).
    """
    targets = mrs[:cap]
    if not targets:
        return

    def _one(m: dict) -> tuple[dict, dict]:
        iid = str(m.get("iid"))
        detail = _fetch_mr_detail(iid)
        appr = _fetch_approvals(iid) if with_approvals else {}
        return detail, appr

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(_one, targets))

    for m, (detail, appr) in zip(targets, results):
        pipe = detail.get("head_pipeline") or detail.get("pipeline") or {}
        if not isinstance(pipe, dict):
            pipe = {}
        m["_pipeline"] = str(pipe.get("status") or "")
        m["_pipeline_url"] = pipe.get("web_url")
        m["_pipeline_id"] = str(pipe.get("id") or "")
        raw_changes = detail.get("changes_count")  # GitLab returns this as a string
        try:
            m["_changes"] = int(raw_changes) if raw_changes is not None else None
        except (ValueError, TypeError):
            m["_changes"] = None
        # The list row never carries diff_refs; the detail response does, and
        # it is the strongest empty-diff signal (#471). Kept only when it is
        # the shape the guard reads — a junk value must not read as evidence.
        refs = detail.get("diff_refs")
        m["_diff_refs"] = refs if isinstance(refs, dict) else None
        m["_approved"] = appr.get("approved")
        m["_approved_by"] = appr.get("approved_by") or []
        m["_failed_jobs"] = []
        # The marker every downstream three-state read depends on. Without it,
        # "no pipeline status" is indistinguishable from "reached, and green",
        # and the difference is the whole of #652.
        #
        # `bool(detail)` rather than True, because "reached" is not the property
        # that matters — "we read this MR's status" is. `_fetch_mr_detail`
        # returns {} on a timeout, a 5xx or an unparseable body, which lands as
        # `_pipeline = ""` and reads as green to `_is_failing` exactly like an
        # MR past the cap. A marker set unconditionally would certify the one
        # unchecked MR the cap notice could never account for.
        m["_enriched"] = bool(detail)

    failing = [m for m in targets if m.get("_pipeline") == "failed" and m.get("_pipeline_id")]
    if failing:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            jobs = list(ex.map(lambda m: _fetch_failed_jobs(m["_pipeline_id"]), failing))
        for m, names in zip(failing, jobs):
            m["_failed_jobs"] = names


# One shared probe (presets/_proc.py). `os.kill(pid, 0)` used to live here,
# which on Windows terminates the process it was asked about rather than
# reporting on it — a read-only status query must never be able to kill (#429).
_pid_alive = _proc.pid_alive


def _watched_iids(state_dir: str = STATE_DIR) -> set[str]:
    """iids that currently have a live gitlab-mr watch poller.

    Reads PID files written by the watch dispatcher
    (/tmp/supertool-watch-gitlab-mr__{iid}.pid). A stale file whose process
    is dead does not count as watched.
    """
    prefix = f"supertool-watch-{WATCH_SOURCE}__"
    watched: set[str] = set()
    for path in glob.glob(os.path.join(state_dir, f"{prefix}*.pid")):
        name = os.path.basename(path)
        iid = name[len(prefix):-len(".pid")]
        try:
            with open(path, encoding="utf-8") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            continue
        if _pid_alive(pid):
            watched.add(iid)
    return watched


_PIPE_GLYPH = {
    "failed": "✗ failed",
    "running": "● running",
    "success": "✓ ok",
    "pending": "◌ pending",
    "canceled": "⊘ canceled",
    "manual": "✋ manual",
    "skipped": "» skipped",
    "created": "◌ created",
}


def _pipe_glyph(status: str, show_pipe: bool) -> str:
    if not show_pipe:
        return "—"
    if not status:
        return "? none"
    return _PIPE_GLYPH.get(status, status)


def _pipe_cell(m: dict, show_pipe: bool) -> str:
    """Pipeline cell — for failures, the failed job name(s) are the class."""
    if not show_pipe:
        return "—"
    status = str(m.get("_pipeline", ""))
    if status == "failed":
        jobs = m.get("_failed_jobs") or []
        if jobs:
            extra = f" +{len(jobs) - 1}" if len(jobs) > 1 else ""
            return f"✗ {jobs[0]}{extra}"
        return "✗ failed"
    return _pipe_glyph(status, show_pipe)


def _appr_cell(m: dict) -> str:
    """Approval mark: ✓ approved, · not yet, blank when unknown/not fetched."""
    approved = m.get("_approved")
    if approved is True:
        return "✓"
    if approved is False:
        return "·"
    return " "


def _age(iso: str) -> str:
    """ISO timestamp → 'Nd'/'Nh'/'Nm' ago. '' on parse failure."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 0:  # future timestamp (clock skew) — don't render "-1d"
        return "now"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _is_failing(m: dict) -> bool:
    """True when this MR's pipeline should be treated as red.

    `canceled`, `timed out` and anything GitLab adds later are red here, not
    just the literal `failed` — a board that sorts failing-first cannot
    surface a run whose state its own classifier threw away (#454).
    """
    status = str(m.get("_pipeline") or "")
    return bool(status) and _checks.is_red(status)


def _sort_key(m: dict) -> tuple[int, str]:
    """Failing MRs first, then stalest (oldest updated_at) within each group."""
    failed = 0 if _is_failing(m) else 1
    return (failed, str(m.get("updated_at", "")))


# GitLab's detailed_merge_status identifier for "source branch exists and
# contains commits" — i.e. the check failed, so there are no commits, so there
# is no diff. Same constant as the gitlab-mr poller (#465).
NO_DIFF_DETAILED_STATUS = "commits_status"


def _has_no_diff(m: dict) -> bool:
    """True only on positive evidence that this MR contains no diff.

    Absent fields are never evidence. A row that carries none of these signals
    leaves `has_conflicts` trusted, so the guard can never invent a reason to
    downgrade a conflict it simply could not see — the same bias as the poller.

    Two of the three signals ship on every `glab mr list` row. The third,
    `diff_refs`, never does — but both surfaces that call this already fetch
    the per-MR detail endpoint through `_enrich`, which stashes it as
    `_diff_refs`, so the poller's strongest signal costs no extra request.
    Beyond `enrich_cap`, or with `nopipe`, `_diff_refs` is simply absent and
    the check degrades to the two list-row signals — never to a false negative.
    """
    if m.get("detailed_merge_status") == NO_DIFF_DETAILED_STATUS:
        return True
    if "sha" in m and not m.get("sha"):
        return True
    refs = m.get("_diff_refs")
    if isinstance(refs, dict) and "head_sha" in refs:
        head = refs.get("head_sha")
        if not head:
            return True
        if head == refs.get("base_sha"):
            return True
    return False


def _conflict_label(m: dict) -> str:
    """"conflict", "empty", or "" — what blocks this MR from merging.

    `has_conflicts` is not a conflict field. It is a straight alias for
    `cannot_be_merged?`, and GitLab annotates the exposure itself: it "is
    generally indicative of conflicts … However, it can also indicate that
    either #has_no_commits? or #branch_missing? are true". Rendering it as
    `[conflict]` reports a merge conflict on an MR that has no diff at all
    (#471, the same false positive #465 fixed in the event stream).

    So a blocked MR with positive evidence of an empty diff is reported as
    `[empty]` rather than suppressed. Suppression would be the other defect:
    the MR really is unmergeable, and a triage board that prints nothing for
    an unmergeable MR is a silent omission, which this repo rates as strictly
    worse than a mislabel.
    """
    if m.get("detailed_merge_status") == "conflict":
        return "conflict"
    if not m.get("has_conflicts"):
        return ""
    return "empty" if _has_no_diff(m) else "conflict"


def _flags(m: dict) -> str:
    flags = []
    if m.get("draft"):
        flags.append("draft")
    blocked = _conflict_label(m)
    if blocked:
        flags.append(blocked)
    if m.get("blocking_discussions_resolved") is False:
        flags.append("threads")
    return f" [{','.join(flags)}]" if flags else ""


TITLE_INDENT = _board.TITLE_INDENT


def _branches(m: dict) -> str:
    """`source -> target`, the field a human acts on (checkout, worktree add).

    Both keys ship in the `glab mr list` response this op already parses, so
    the branch pair costs no extra API call.
    """
    return _board.branch_pair(m.get("source_branch"), m.get("target_branch"))


def _row(m: dict, watched: set[str], show_pipe: bool, suffix: str = "") -> str:
    """One triage row, rendered through the shared board layout so `gl-mrs`,
    `radar` and `gh-prs` cannot drift apart.

    Everything GitLab-specific — iid, pipeline cell, branch keys — is resolved
    here; `_board.render_row` only decides the shape.
    """
    chg = m.get("_changes")
    return _board.render_row(
        sigil="!",
        ident=str(m.get("iid", "?")),
        watched=str(m.get("iid", "?")) in watched,
        status=_pipe_cell(m, show_pipe),
        appr=_appr_cell(m),
        age=_age(str(m.get("updated_at", ""))),
        changes=f"{chg}Δ" if isinstance(chg, int) else "",
        branches=_branches(m),
        flags=_flags(m),
        title=str(m.get("title", "")),
        suffix=suffix,
    )


def _render_table(mrs: list[dict], watched: set[str], show_pipe: bool,
                  search: str | None = None) -> str:
    """Triage table: pipeline (+ failed job), approval, age, diff size, flags.

    Sorted failing-first then stalest so what needs you is at the top.

    `No MRs match.` under a `search=` would be read as a statement about the
    project rather than about a substring match over titles and descriptions.
    A search that could not run never reaches here — it keeps its non-zero
    exit above — so this sentence may say the search RAN, which is the fact
    that tells the empty result from the failed lookup (#1395).
    """
    if not mrs:
        if search is not None:
            return (f"No MRs match {_search_note(search)}. "
                    "The search ran and matched nothing — an empty result, "
                    "not a lookup that failed.")
        return "No MRs match."
    return "\n".join(_row(m, watched, show_pipe) for m in sorted(mrs, key=_sort_key))


ENRICH_CAP_KNOB = "SUPERTOOL_ENRICH_CAP"


def _unchecked_count(mrs: list[dict]) -> int:
    """How many MRs `_enrich` never reached — pipeline unknown, not "green".

    Read off the MRs themselves rather than recomputed as `total - cap`. A
    re-derivation would go stale the moment `_enrich` changes which rows it
    reaches, and it would go stale silently, which is the failure being fixed.
    """
    return sum(1 for m in mrs if not m.get("_enriched"))


def _cap_notice(unchecked: int, total: int, cap: int, failed_only: bool) -> str:
    """Disclose the MRs whose pipeline was never fetched. '' when there are none.

    The empty return is load-bearing: on a fully-checked board the absence of a
    marker is how the op claims it saw everything, so nothing extra may print.

    The `failed` wording is stronger than the default board's on purpose. On
    the default board an unchecked MR is still listed, rendered `? none`, so
    the reader can see it. `failed` *drops* it — `_is_failing` reads `_pipeline`
    and an unenriched MR has none — so the omission leaves no trace in the
    table at all, and the notice is the only thing standing between a
    truncated board and a reader concluding nothing is failing (#652).
    """
    if unchecked <= 0:
        return ""
    consequence = (
        ", so a failing MR among them cannot appear on this board"
        if failed_only else ""
    )
    body = (
        f"{unchecked} of {total} MRs not checked — "
        f"pipeline status unavailable{consequence}"
    )
    # The cap is named as the escape only when the cap is what cut. Below the
    # cap the unchecked MRs are failed lookups, and pointing at a limit that
    # never applied is advice that cannot work — a confidently wrong cause,
    # which this repo rates as worse than saying less.
    if total > cap:
        body += f". Enrichment cap is {cap}; raise {ENRICH_CAP_KNOB}=N"
    return f"({body})"


def _footer(mrs: list[dict], watched: set[str], show_pipe: bool, unchecked: int = 0) -> str:
    """Actionable summary: failing, unapproved, and the watch command.

    `unchecked` sits next to the failing count because it qualifies it: "2
    failing" is a floor, not a total, whenever anything went unchecked.
    """
    if not show_pipe:
        return ""
    failing = [str(m.get("iid")) for m in mrs if _is_failing(m)]
    unwatched_fail = [i for i in failing if i not in watched]
    unapproved = [m for m in mrs if m.get("_approved") is False]
    parts = [f"{len(mrs)} MR(s)"]
    if failing:
        parts.append(f"{len(failing)} failing")
    if unchecked:
        parts.append(f"{unchecked} unchecked")
    if unapproved:
        parts.append(f"{len(unapproved)} unapproved")
    if unwatched_fail:
        parts.append(
            f"{len(unwatched_fail)} unwatched → watch:{WATCH_SOURCE}:{unwatched_fail[0]}"
        )
    return " | ".join(parts)


def main() -> int:
    extra = _filter_tokens.extra_segments_error(sys.argv, "gl-mrs")
    if extra:
        print(extra, file=sys.stderr)
        return 1
    arg_str = sys.argv[1] if len(sys.argv) > 1 else ""
    filters, flags, unknown_tokens = _parse_args(arg_str)
    if unknown_tokens:
        print(_unknown_error(unknown_tokens), file=sys.stderr)
        return 1
    bad = _bad_values(filters)
    if bad:
        print(_filter_tokens.value_error(bad), file=sys.stderr)
        return 1
    iids_only = "iids" in flags
    failed_only = "failed" in flags
    # failed-only needs pipeline data to filter on, so it overrides nopipe.
    show_pipe = failed_only or "nopipe" not in flags

    cfg = _get_config()
    per_page = cfg["per_page"]
    if "per" in filters:
        per_page = int(filters.pop("per"))
    # NOT popped — `_build_list_cmd` forwards it to `glab mr list --search`.
    # Read here so every render below can name the query and the scope.
    search = filters.get("search")

    try:
        result = _run(_build_list_cmd(filters, per_page))
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        print(f"ERROR: glab mr list failed: {exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:
        # Flattened before either use: this reaches a receipt, because the core
        # appends a failing preset's stderr to it (`_supertool.py`), and the
        # writer of the text is the GitLab API (#1485).
        err = _untrusted.flat(result.stderr.strip()) or "unknown error"
        # A status, never a number (#1846). go-gitlab echoes the request URL into
        # every error string, so a project, job or pipeline id containing `401`
        # made a 500 or a throttle render as a missing credential.
        if _auth_probe.says_not_authenticated(err, _auth_probe.GITLAB_MARKERS):
            print("ERROR: glab not authenticated. Run: glab auth login", file=sys.stderr)
        else:
            print(f"ERROR: glab mr list: {err}", file=sys.stderr)
        return 1

    try:
        mrs = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("ERROR: could not parse glab JSON output", file=sys.stderr)
        return 1
    if not isinstance(mrs, list):
        mrs = []

    total = len(mrs)
    if show_pipe:
        _enrich(mrs, cfg["enrich_cap"], cfg["enrich_workers"])
    # Both counted BEFORE the filter. The filter rebinds `mrs` to a list that
    # is nearly always far shorter than the cap, so a notice computed after it
    # could not fire in `failed` mode — the one mode where the truncation is
    # invisible in the table itself (#652).
    unchecked = _unchecked_count(mrs) if show_pipe else 0
    notice = _cap_notice(unchecked, total, cfg["enrich_cap"], failed_only)
    if failed_only:
        mrs = [m for m in mrs if _is_failing(m)]

    # Bare iid list — for piping into the watch supervisor. The disclosure goes
    # to stderr: stdout has to stay a parseable id feed, but this is the channel
    # that starts background pollers, so an MR dropped here is one nobody looks
    # at again. Losing it on both streams is the one option not available.
    if iids_only:
        # The search disclosure rides the piped shape too: a list of iids
        # produced by a title/description match is not the same population as
        # one from a full-text search, and a consumer that cannot see which it
        # got reads the shorter list as the project being quieter.
        if search is not None:
            print(f"({_search_note(search)})", file=sys.stderr)
        if notice:
            print(notice, file=sys.stderr)
        for m in mrs:
            iid = m.get("iid")
            if iid is not None:
                print(iid)
        return 0

    if notice:
        print(notice)

    watched = _watched_iids()
    # One disclosure line above the board, not two marker lines around each
    # title (#819). The titles are the MR authors' words; on a fifty-row board
    # a per-row fence is the noise that gets a convention switched off, and an
    # unreadable board is one nobody reads. `_board.render_row` carries the
    # structural half — after it no title can reach column 0.
    if mrs:
        # Guarded on `mrs`, the same way `gh-issues` guards its header notes:
        # the empty render already carries the whole sentence inside its own
        # `No MRs match ...` line, and printing it again immediately above is
        # the same disclosure twice, adjacent. A note repeated verbatim is the
        # one that gets skimmed.
        if search is not None:
            print(f"({_search_note(search)})")
        print(_untrusted.flat_note("MR titles"))
    print(_render_table(mrs, watched, show_pipe, search))
    footer = _footer(mrs, watched, show_pipe, unchecked)
    if footer:
        print(f"\n{footer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
