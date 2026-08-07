#!/usr/bin/env python3
"""GitHub pull request list via gh CLI — triage board.

The gl-mrs twin. `gh pr list` shows a flat table; this op adds the things it
omits but you need next: check rollup (a failure shows the failing check's
*name* — the failure class in one word), approval state, age, diff size,
watch-state cross-reference (which PRs already have a live `watch` poller),
and an actionable footer. "Mine" is just the default filter (author=@me),
not a special mode — compose any filter.

Unlike GitLab (where pipeline status needed a per-MR fetch), GitHub returns
`statusCheckRollup` + `reviewDecision` straight from `gh pr list --json`, so
the core board costs a single call. The only optional second wave is
unresolved review-thread counts (GraphQL, per PR) — skip it with `nopipe`.

Usage:
    gh-prs                          my open PRs, check-enriched
    gh-prs:reviewer=@me             where review is requested from me
    gh-prs:author=@me,state=merged  filter composition
    gh-prs:label=bug                reusable beyond watching
    gh-prs:nopipe                   skip review-thread enrichment (faster)
    gh-prs:iids                     bare number list (for piping into watch)
    gh-prs:failed                   only PRs whose checks are failing
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pr import _gh, _fetch_review_threads  # noqa: E402  (reuse the gh-pr helpers)
import _board  # noqa: E402  (the board layout shared with gl-mrs / radar)
import _filter_tokens  # noqa: E402  (the one tokenizer + refusal, shared with gh-issues / gl-mrs)
import _untrusted  # noqa: E402  (the repo's remote-text convention)
from _env import env_int  # noqa: E402  (the one numeric-knob reader)
import _checks  # noqa: E402  (the one check classifier, shared with gh-pr / gl-mrs)
import _proc  # noqa: E402  (the one liveness probe, shared with watch / gl-mrs)
import _repo_target  # noqa: E402  (the repo this call is about, when not the cwd's)

WATCH_SOURCE = "github-pr"
STATE_DIR = "/tmp"
DEFAULT_PER_PAGE = 50
ENRICH_CAP = 40  # never fire more than this many per-PR thread fetches
ENRICH_WORKERS = 8  # parallel thread fetches

# Tokens that are flags, not key=value filters.
_FLAGS = {"nopipe", "iids", "failed"}

# Filter keys this op forwards. Anything else is refused rather than dropped:
# `_build_list_cmd` builds its argv from a ladder with no `else`, so
# `milestone=nonexistent` — a key `gh pr list` has no flag for at all — used to
# return every open PR, rendered exactly as though that were one milestone's
# contents (#939). `gh-issues` grew this refusal in #864; the two sibling ops
# then disagreed about whether a typo was an error or a silent full board,
# which is worse than the original bug.
_FILTER_KEYS = {"author", "assignee", "label", "reviewer", "state", "per"}

# Check conclusions that mean "this PR is red".
_FAIL_CONCLUSIONS = {
    "FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE",
}
_FAIL_STATES = {"FAILURE", "ERROR"}

# Fields pulled in the single list call — everything the board needs except
# review threads (not exposed by `gh pr list --json`, fetched in wave 2).
#
# `headRefOid` is not rendered by this op. It is here because the radar
# `gh-prs` tier keys its snapshot on it (#859): a push that lands a new head
# commit re-runs everything, and without the SHA a "failed → failed" delta
# across two different commits reads as *no change*. It also feeds `gh-pr`'s
# declared-leg reconciliation, which resolves runs by head SHA. Free — one more
# field on a response already being fetched.
_LIST_FIELDS = (
    "number,title,state,author,headRefName,headRefOid,baseRefName,labels,"
    "isDraft,mergeable,reviewDecision,statusCheckRollup,additions,deletions,"
    "changedFiles,updatedAt,createdAt,assignees,url"
)


def _get_config() -> dict[str, int]:
    """Read tunable knobs from SUPERTOOL_ env vars (set from .supertool.json).

    SUPERTOOL_ENRICH_WORKERS — parallel review-thread fetches (default 8)
    SUPERTOOL_ENRICH_CAP     — max PRs to thread-enrich (default 40)
    SUPERTOOL_PER_PAGE       — PRs fetched from the list endpoint (default 50)
    """
    # Was a second private copy of gl-mrs's `_int` — same silent tolerate, same
    # silent clamp, duplicated. One reader now, and it says what it could not
    # honour (#654).
    return {
        "enrich_workers": env_int("SUPERTOOL_ENRICH_WORKERS", ENRICH_WORKERS, minimum=1),
        "enrich_cap": env_int("SUPERTOOL_ENRICH_CAP", ENRICH_CAP, minimum=0),
        "per_page": env_int("SUPERTOOL_PER_PAGE", DEFAULT_PER_PAGE, minimum=1),
    }


# state=X maps to a gh list flag value. open is gh's default.
_STATES = {"open", "closed", "merged", "all"}

# Keys whose value this op maps rather than forwards. An unmapped value is the
# same defect on a key that is known: `state=mergd` fails the `val in _STATES`
# test below, emits no `--state`, and the *open* board renders as the merged
# one (#939).
_VALUE_DOMAINS: dict[str, object] = {
    "state": _STATES,
    "per": _filter_tokens.POSITIVE_INT,
}


def _parse_args(arg_str: str) -> tuple[dict[str, str], set[str], list[str]]:
    """Split a comma-separated arg string into (filters, flags, unrecognised).

    Comma-separated so the single supertool arg segment never collides with
    the ':' op tokenizer. 'author=@me,state=merged,nopipe' becomes
    ({'author': '@me', 'state': 'merged'}, {'nopipe'}, []).

    The third return value is the one that matters, and it is why the tokenizer
    now lives in `_filter_tokens` rather than here: a token this op cannot place
    is handed back so `main` can refuse, instead of falling off the end of the
    loop and leaving the caller reading an unnarrowed board as the answer to a
    narrowing question.
    """
    return _filter_tokens.parse(arg_str, _FILTER_KEYS, _FLAGS)


def _unknown_error(unknown: list[str]) -> str:
    """Name every token that was not applied, and what would have been."""
    return _filter_tokens.unknown_error(unknown, _FILTER_KEYS, _FLAGS)


def _bad_values(filters: dict[str, str]) -> list[tuple[str, str, str]]:
    """Known keys carrying a value this op has no mapping for."""
    return _filter_tokens.bad_values(filters, _VALUE_DOMAINS)


def _build_list_cmd(filters: dict[str, str], per_page: int) -> list[str]:
    """Build the `gh pr list ... --json` argv from parsed filters.

    Defaults to author=@me when no role filter is given so the bare `gh-prs`
    means 'mine'. state=open is gh's default (no flag emitted). reviewer has
    no list flag on gh, so it routes through --search review-requested:USER.
    """
    has_role = any(k in filters for k in ("author", "assignee", "reviewer"))
    cmd = (["gh", "pr", "list", "--json", _LIST_FIELDS, "--limit", str(per_page)]
           + _repo_target.gh_args())
    if not has_role:
        cmd += ["--author", "@me"]
    for key, val in filters.items():
        if not val:
            continue
        if key == "state":
            if val in _STATES and val != "open":
                cmd += ["--state", val]
        elif key == "author":
            cmd += ["--author", val]
        elif key == "assignee":
            cmd += ["--assignee", val]
        elif key == "label":
            cmd += ["--label", val]
        elif key == "reviewer":
            cmd += ["--search", f"review-requested:{val}"]
    return cmd


def _check_failed(c: dict) -> bool:
    """A single rollup entry is red — handles CheckRun and StatusContext shapes.

    Delegates to the shared classifier so the board and the `gh-pr` tally
    cannot disagree about what CANCELLED means (#454). Unrecognised states
    count as red there, which is the safe default for a failing-first board.
    """
    concl = str(c.get("conclusion") or "").upper()
    state = str(c.get("state") or "").upper()
    if concl in _FAIL_CONCLUSIONS or state in _FAIL_STATES:
        return True
    return _checks.is_red(_checks.github_state(c))


def _check_pending(c: dict) -> bool:
    status = str(c.get("status") or "").upper()
    state = str(c.get("state") or "").upper()
    if status in {"IN_PROGRESS", "QUEUED", "WAITING", "PENDING", "REQUESTED"}:
        return True
    if c.get("conclusion") is None and status != "COMPLETED" and state in {"", "PENDING", "EXPECTED"}:
        return True
    return False


def _rollup_state(checks: list[dict]) -> str:
    """Reduce statusCheckRollup to one word: failed / running / success / ''."""
    if not checks:
        return ""
    if any(_check_failed(c) for c in checks):
        return "failed"
    if any(_check_pending(c) for c in checks):
        return "running"
    return "success"


def _failed_check_names(checks: list[dict]) -> list[str]:
    """Names of the failing checks — the failure class without a gh-job call."""
    names = []
    for c in checks:
        if _check_failed(c):
            n = c.get("name") or c.get("context") or "check"
            names.append(str(n))
    return names


def _annotate(prs: list[dict]) -> None:
    """Derive board fields from the free list data (no extra API calls)."""
    for p in prs:
        checks = p.get("statusCheckRollup") or []
        if not isinstance(checks, list):
            checks = []
        p["_checks"] = _rollup_state(checks)
        p["_failed_checks"] = _failed_check_names(checks)
        add = p.get("additions")
        dele = p.get("deletions")
        try:
            p["_changes"] = int(add) + int(dele) if add is not None and dele is not None else None
        except (ValueError, TypeError):
            p["_changes"] = None
        decision = str(p.get("reviewDecision") or "").upper()
        if decision == "APPROVED":
            p["_approved"] = True
        elif decision in {"CHANGES_REQUESTED", "REVIEW_REQUIRED"}:
            p["_approved"] = False
        else:
            p["_approved"] = None


def _enrich(prs: list[dict], cap: int = ENRICH_CAP, workers: int = ENRICH_WORKERS) -> None:
    """Wave 2: unresolved review-thread counts, fetched in parallel via GraphQL.

    The one thing `gh pr list --json` can't give us. Capped and skippable
    (`nopipe`) because it costs one call per PR.
    """
    targets = prs[:cap]
    if not targets:
        return

    def _one(p: dict) -> int:
        threads = _fetch_review_threads(p.get("url", ""), p.get("number"))
        return sum(1 for t in threads if not t.get("isResolved"))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        counts = list(ex.map(_one, targets))
    for p, n in zip(targets, counts):
        p["_unresolved"] = n


# One shared probe (presets/_proc.py). `os.kill(pid, 0)` used to live here,
# which on Windows terminates the process it was asked about rather than
# reporting on it — a read-only status query must never be able to kill (#429).
_pid_alive = _proc.pid_alive


def _watched_numbers(state_dir: str = STATE_DIR) -> set[str] | None:
    """PR numbers that currently have a live github-pr watch poller.

    Reads PID files written by the watch dispatcher
    (/tmp/supertool-watch-github-pr__{number}.pid). A stale file whose process
    is dead does not count as watched.

    None under a repo target (#673): that filename is keyed by PR number and
    carries no repo, so a live poller for #12 of whatever repo the watcher was
    started in cannot be told apart from #12 of the repo this board is about.
    Returning the set anyway would mark the wrong rows watched; returning an
    empty set would assert that none are. Neither is knowable here, so the
    board is told so and says `?`.
    """
    if _repo_target.target():
        return None
    prefix = f"supertool-watch-{WATCH_SOURCE}__"
    watched: set[str] = set()
    for path in glob.glob(os.path.join(state_dir, f"{prefix}*.pid")):
        name = os.path.basename(path)
        number = name[len(prefix):-len(".pid")]
        try:
            with open(path, encoding="utf-8") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            continue
        if _pid_alive(pid):
            watched.add(number)
    return watched


_CHECK_GLYPH = {
    "failed": "✗ failed",
    "running": "● running",
    "success": "✓ ok",
}


def _check_glyph(state: str) -> str:
    if not state:
        return "? none"
    return _CHECK_GLYPH.get(state, state)


def _check_cell(p: dict) -> str:
    """Checks cell — for failures, the failing check name(s) are the class."""
    state = str(p.get("_checks", ""))
    if state == "failed":
        names = p.get("_failed_checks") or []
        if names:
            extra = f" +{len(names) - 1}" if len(names) > 1 else ""
            return f"✗ {names[0]}{extra}"
        return "✗ failed"
    return _check_glyph(state)


def _appr_cell(p: dict) -> str:
    """Approval mark: ✓ approved, · not yet, blank when unknown/none required."""
    approved = p.get("_approved")
    if approved is True:
        return "✓"
    if approved is False:
        return "·"
    return " "


def _age(iso: str) -> str:
    """ISO timestamp → 'Nd'/'Nh'/'Nm'. '' on parse failure, 'now' on skew."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 0:
        return "now"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _sort_key(p: dict) -> tuple[int, str]:
    """Failing PRs first, then stalest (oldest updatedAt) within each group."""
    failed = 0 if p.get("_checks") == "failed" else 1
    return (failed, str(p.get("updatedAt", "")))


def _flags(p: dict) -> str:
    flags = []
    if p.get("isDraft"):
        flags.append("draft")
    if p.get("mergeable") == "CONFLICTING":
        flags.append("conflict")
    if p.get("_unresolved", 0):
        flags.append("threads")
    return f" [{','.join(flags)}]" if flags else ""


TITLE_INDENT = _board.TITLE_INDENT


def _branches(p: dict) -> str:
    """`head -> base`, the field a human acts on (checkout, worktree add).

    Both keys already ship in the single `gh pr list --json` response this op
    parses (`headRefName`/`baseRefName` are in _LIST_FIELDS), so the branch
    pair costs no extra API call.
    """
    return _board.branch_pair(p.get("headRefName"), p.get("baseRefName"))


def _row(p: dict, watched: set[str] | None, suffix: str = "") -> str:
    """One triage row, rendered through the shared board layout so `gh-prs`,
    `gl-mrs` and `radar` cannot drift apart.

    Everything GitHub-specific — the number, the check-rollup cell, the ref
    names — is resolved here; `_board.render_row` only decides the shape.
    """
    chg = p.get("_changes")
    return _board.render_row(
        sigil="#",
        ident=str(p.get("number", "?")),
        watched=None if watched is None else str(p.get("number", "?")) in watched,
        status=_check_cell(p),
        appr=_appr_cell(p),
        age=_age(str(p.get("updatedAt", ""))),
        changes=f"{chg}Δ" if isinstance(chg, int) else "",
        branches=_branches(p),
        flags=_flags(p),
        title=str(p.get("title", "")),
        suffix=suffix,
    )


def _render_table(prs: list[dict], watched: set[str] | None) -> str:
    """Triage table: checks (+ failed check), approval, age, diff size, flags.

    Sorted failing-first then stalest so what needs you is at the top.
    """
    if not prs:
        return "No PRs match."
    return "\n".join(_row(p, watched) for p in sorted(prs, key=_sort_key))


def _footer(prs: list[dict], watched: set[str] | None) -> str:
    """Actionable summary: failing, unapproved, and the watch command.

    `watched=None` (a repo target, see `_watched_numbers`) drops the watch
    clause rather than guessing at it. The clause is not decoration: it emits a
    ready-to-run `watch:github-pr:N`, and watch state is keyed by number alone,
    so under a target that command would start polling *this* repo's #N while
    the board it came from was about another. A suggestion that does the wrong
    thing is worse than no suggestion, and going silent about it would leave
    the reader thinking nothing needs watching — so it says which it is.
    """
    failing = [str(p.get("number")) for p in prs if p.get("_checks") == "failed"]
    unapproved = [p for p in prs if p.get("_approved") is False]
    parts = [f"{len(prs)} PR(s)"]
    if failing:
        parts.append(f"{len(failing)} failing")
    if unapproved:
        parts.append(f"{len(unapproved)} unapproved")
    if watched is None:
        parts.append("watch state unknown for a repo target (keyed by number "
                     "only) — watch from a clone of that repo")
    else:
        unwatched_fail = [n for n in failing if n not in watched]
        if unwatched_fail:
            parts.append(
                f"{len(unwatched_fail)} unwatched → watch:{WATCH_SOURCE}:{unwatched_fail[0]}"
            )
    return " | ".join(parts)


def main() -> int:
    extra = _filter_tokens.extra_segments_error(sys.argv, "gh-prs")
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
    enrich = "nopipe" not in flags

    cfg = _get_config()
    per_page = cfg["per_page"]
    if "per" in filters:
        per_page = int(filters.pop("per"))

    try:
        result = subprocess.run(
            _build_list_cmd(filters, per_page),
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        print(f"ERROR: gh pr list failed: {exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:
        err = result.stderr.strip() or "unknown error"
        low = err.lower()
        if "not logged in" in low or "401" in err:
            print("ERROR: gh not authenticated. Run: gh auth login", file=sys.stderr)
        elif ("github host" in low or "not a git repository" in low
                or "git remotes" in low):
            print(_repo_target.no_repo_error("gh-prs:author=@me"), file=sys.stderr)
        else:
            print(f"ERROR: gh pr list: {err}", file=sys.stderr)
        return 1

    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("ERROR: could not parse gh JSON output", file=sys.stderr)
        return 1
    if not isinstance(prs, list):
        prs = []

    _annotate(prs)
    if failed_only:
        prs = [p for p in prs if p.get("_checks") == "failed"]

    # Bare number list — for piping into the watch supervisor.
    if iids_only:
        for p in prs:
            number = p.get("number")
            if number is not None:
                print(number)
        return 0

    if enrich:
        _enrich(prs, cfg["enrich_cap"], cfg["enrich_workers"])
        if len(prs) > cfg["enrich_cap"]:
            print(f"(review-thread enrichment capped at {cfg['enrich_cap']} PRs)")

    watched = _watched_numbers()
    # One disclosure line above the board — see `gl-mrs.main` (#819).
    if prs:
        print(_untrusted.flat_note("PR titles"))
    print(_render_table(prs, watched))
    footer = _footer(prs, watched)
    if footer:
        print(f"\n{footer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
