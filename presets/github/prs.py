#!/usr/bin/env python3















































from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "watch"))
from _console import use_utf8_stdout  
from pr import (  
    _fetch_review_threads_detailed,
    THREADS_PAGE_MAX,
)
import _board  
import _filter_tokens  
import _untrusted  
import _auth_probe  
from _env import env_int  
import _checks  
import _proc  
import _release_gate  
import _repo_target  
import transport  

WATCH_SOURCE = "github-pr"





STATE_DIR = transport.STATE_DIR
DEFAULT_PER_PAGE = 50
ENRICH_CAP = 40  
ENRICH_WORKERS = 8  








_FLAGS = {"nopipe", "iids", "failed", "anyauthor"}








_FILTER_KEYS = {"author", "assignee", "label", "reviewer", "state", "per",
                "merged-since"}














_NON_NARROWING_KEYS = {"state", "per", "merged-since"}
_NARROWING_KEYS = _FILTER_KEYS - _NON_NARROWING_KEYS


































_FAIL_CONCLUSIONS = {
    "FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE",
}
_FAIL_STATES = {"FAILURE", "ERROR"}










_LIST_FIELDS = (
    "number,title,state,author,headRefName,headRefOid,baseRefName,labels,"
    "isDraft,mergeable,reviewDecision,statusCheckRollup,additions,deletions,"
    "changedFiles,updatedAt,createdAt,assignees,url"
)


def _get_config() -> dict[str, int]:









    return {
        "enrich_workers": env_int("SUPERTOOL_ENRICH_WORKERS", ENRICH_WORKERS, minimum=1),
        "enrich_cap": env_int("SUPERTOOL_ENRICH_CAP", ENRICH_CAP, minimum=0),
        "per_page": env_int("SUPERTOOL_PER_PAGE", DEFAULT_PER_PAGE, minimum=1),
    }



_STATES = {"open", "closed", "merged", "all"}





_VALUE_DOMAINS: dict[str, object] = {
    "state": _STATES,
    "per": _filter_tokens.POSITIVE_INT,
    "merged-since": _filter_tokens.ISO_INSTANT_OR_TAG,
}



_MERGE_BEARING_STATES = {"merged", "closed", "all"}


def _state_conflict(filters: dict[str, str]) -> str | None:









    if "merged-since" not in filters or not filters.get("merged-since"):
        return None
    state = filters.get("state") or "open"
    if state in _MERGE_BEARING_STATES:
        return None
    return (
        f"ERROR: merged-since= asks about merges and state={state} cannot "
        f"contain one — `gh pr list` would be sent `--search merged:>...` over "
        f"{state} PRs, which matches no row whatever this repository looks "
        f"like. An empty board there is a fact about the query, not about the "
        f"repo, so it is refused rather than printed. Add state=merged (or "
        f"state=closed / state=all)."
    )


class _GatePlan(NamedTuple):


    value: str
    is_tag: bool


def _gate_plan(filters: dict[str, str]) -> "_GatePlan | None":














    value = str(filters.get("merged-since") or "")
    if not value:
        return None


    return _GatePlan(value=value,
                     is_tag=_filter_tokens.parse_iso_instant(value) is None)


def _tag_target_conflict(value: str, target: object) -> "str | None":











    plan = _gate_plan({"merged-since": value})
    if plan is None or not plan.is_tag:
        return None
    if not str(target or "").strip():
        return None
    return _release_gate.repo_target_refusal(target, tag=value)


def _boundary_flag_conflict(plan: "_GatePlan | None", flags: set) -> "str | None":








    if plan is None or "failed" not in flags:
        return None
    return (
        "ERROR: `failed` and `merged-since=` cannot be answered together. The "
        "boundary slice is fetched without `statusCheckRollup` — dozens of "
        "check runs per row, over a page of up to 500 — because a merged PR's "
        "rollup is historical and the release gate was deliberately kept off "
        "that field set. With the field absent, `failed` would report every "
        "row as passing, so it is refused rather than answered from data that "
        "was never fetched. Drop one of the two."
    )


def _parse_args(arg_str: str) -> tuple[dict[str, str], set[str], list[str]]:












    return _filter_tokens.parse(arg_str, _FILTER_KEYS, _FLAGS)


def _unknown_error(unknown: list[str]) -> str:

    return _filter_tokens.unknown_error(unknown, _FILTER_KEYS, _FLAGS)


def _bad_values(filters: dict[str, str]) -> list[tuple[str, str, str]]:

    return _filter_tokens.bad_values(filters, _VALUE_DOMAINS)


def _build_list_cmd(filters: dict[str, str], per_page: int, *,
                    fields: str = _LIST_FIELDS) -> list[str]:



























    cmd = (["gh", "pr", "list", "--json", fields, "--limit", str(per_page)]
           + _repo_target.gh_args())







    search: list[str] = []
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
            search.append(f"review-requested:{val}")
        elif key == "merged-since":




            when = _filter_tokens.parse_iso_instant(val)
            if when is not None:
                search.append(
                    "merged:>" + when.strftime("%Y-%m-%dT%H:%M:%S+00:00"))
    if search:
        cmd += ["--search", " ".join(search)]
    return cmd


def _check_failed(c: dict) -> bool:






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

    if not checks:
        return ""
    if any(_check_failed(c) for c in checks):
        return "failed"
    if any(_check_pending(c) for c in checks):
        return "running"
    return "success"


def _failed_check_names(checks: list[dict]) -> list[str]:

    names = []
    for c in checks:
        if _check_failed(c):
            n = c.get("name") or c.get("context") or "check"
            names.append(str(n))
    return names


def _annotate(prs: list[dict]) -> None:

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






















    targets = prs[:cap]
    if not targets:
        return

    def _one(p: dict) -> tuple[int | None, bool]:
        threads, _ = _fetch_review_threads_detailed(
            p.get("url", ""), p.get("number"))
        if threads is None:
            return (None, False)
        return (sum(1 for t in threads if not t.get("isResolved")),
                len(threads) >= THREADS_PAGE_MAX)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        counts = list(ex.map(_one, targets))
    for p, (n, floor) in zip(targets, counts):
        p["_unresolved"] = n
        p["_unresolved_floor"] = floor





_pid_alive = _proc.pid_alive


def _watched_numbers(state_dir: str = STATE_DIR) -> set[str] | None:













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

    state = str(p.get("_checks", ""))
    if state == "failed":
        names = p.get("_failed_checks") or []
        if names:
            extra = f" +{len(names) - 1}" if len(names) > 1 else ""
            return f"✗ {names[0]}{extra}"
        return "✗ failed"
    return _check_glyph(state)


def _appr_cell(p: dict) -> str:

    approved = p.get("_approved")
    if approved is True:
        return "✓"
    if approved is False:
        return "·"
    return " "


def _age(iso: str) -> str:

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

    failed = 0 if p.get("_checks") == "failed" else 1
    return (failed, str(p.get("updatedAt", "")))


def _flags(p: dict) -> str:
    flags = []
    if p.get("isDraft"):
        flags.append("draft")
    if p.get("mergeable") == "CONFLICTING":
        flags.append("conflict")




    if "_unresolved" in p:
        if p["_unresolved"] is None:
            flags.append("threads?")
        elif p["_unresolved"]:




            flags.append("threads")
        elif p.get("_unresolved_floor"):



            flags.append("threads?")
    return f" [{','.join(flags)}]" if flags else ""


TITLE_INDENT = _board.TITLE_INDENT


def _branches(p: dict) -> str:






    return _board.branch_pair(p.get("headRefName"), p.get("baseRefName"))


def _row(p: dict, watched: set[str] | None, suffix: str = "") -> str:






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




    if not prs:
        return "No PRs match."
    return "\n".join(_row(p, watched) for p in sorted(prs, key=_sort_key))


def _floor_note(prs: list[dict]) -> str | None:










    n = sum(1 for p in prs if p.get("_unresolved_floor"))
    if not n:
        return None
    return (f"{n} PR(s) filled the {THREADS_PAGE_MAX}-thread fetch page, so "
            f"their thread counts are floors — a `threads?` there is a zero "
            f"that could not be established, not a call that failed")


def _cap_note(per_page: int | None, fetched: int | None) -> str | None:









    if per_page is None or fetched is None or fetched < per_page:
        return None
    return f"capped at --limit {per_page} — more may exist, raise with per=N"


def _probe_population(filters: dict[str, str], per_page: int
                      ) -> tuple[int | None, str | None]:















    widened = {k: v for k, v in filters.items()
               if k not in ("author", "assignee", "reviewer")}
    cmd = _build_list_cmd(widened, per_page)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                encoding="utf-8", errors="replace")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return None, f"the check itself failed: {exc}"
    if result.returncode != 0:
        return None, f"the check itself failed: {(result.stderr or '').strip()[:100]}"
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "the check returned unreadable JSON"
    if not isinstance(rows, list):
        return None, "the check returned no list"
    return len(rows), None


def _population(filters: dict[str, str]) -> str:








    state = filters.get("state") or "open"
    if state == "all":
        return "PRs in any state"
    return f"{state} PRs"


def _scope_note(filters: dict[str, str], per_page: int, fetched: int
                ) -> str | None:














    population = _population(filters)
    role = sorted(k for k in ("author", "assignee", "reviewer") if k in filters)
    if not role:
        return (f"no author filter (default) — every author's {population} on "
                f"this repo; gh-prs:author=@me for yours")
    spelled = ", ".join(f"{k}={filters[k]}" for k in role)
    if fetched:
        return f"{spelled} — one slice of the repo; gh-prs for all of it"
    count, reason = _probe_population(filters, per_page)
    if count is None:
        return (f"{spelled} applied; whether it excluded anything is "
                f"UNKNOWN — {reason}")
    if count:
        return (f"{spelled} excluded {count} {population} — gh-prs to see them")
    return f"{spelled} excluded none — nothing matches either way"


def _footer(prs: list[dict], watched: set[str] | None,
            notes: list[str] | None = None) -> str:










    failing = [str(p.get("number")) for p in prs if p.get("_checks") == "failed"]
    unapproved = [p for p in prs if p.get("_approved") is False]
    parts = [f"{len(prs)} PR(s)"]

    parts.extend(notes or [])
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


def _boundary_slice(*, rows, plan, boundary, filters, flags, per_page) -> int:












    fetched = len(rows)



    narrowed_by = sorted(k for k in _NARROWING_KEYS if filters.get(k))

    if boundary is None:


        instant = _filter_tokens.parse_iso_instant(plan.value)
        kept, undated = _release_gate.filter_merged(rows, instant)






        lines = ([_release_gate.page_note(page=fetched, limit=per_page)]
                 + _release_gate.not_applicable_note()
                 + _release_gate.unplaced_note(undated))
        if narrowed_by:
            lines.append(f"population: narrowed by {', '.join(narrowed_by)}")
        code = 0
    else:
        kept, lines, code = _release_gate.assess(
            rows=rows, boundary=boundary, per_page=per_page, fetched=fetched,
            narrowed_by=narrowed_by,
            repo_targeted=bool(str(_repo_target.target() or "").strip()))

    if "iids" in flags:



        for line in lines:
            print(f"# {line}")
        for row in kept:
            number = row.get("number")
            if number is not None:
                print(number)
        return code





    cap = _release_gate.page_note(page=fetched, limit=per_page)
    if "PAGE FULL" in cap:
        print(f"({cap})")
    if boundary is None:
        print("\n".join(_release_gate.merge_order_rows(kept)))
    print("\n".join(lines))
    return code


def main_with_args(arg_str: str) -> int:
    filters, flags, unknown_tokens = _parse_args(arg_str)
    if unknown_tokens:
        print(_unknown_error(unknown_tokens), file=sys.stderr)
        return 1
    bad = _bad_values(filters)
    if bad:
        print(_filter_tokens.value_error(bad), file=sys.stderr)
        return 1
    conflict = _state_conflict(filters)
    if conflict:
        print(conflict, file=sys.stderr)
        return 1



    plan = _gate_plan(filters)
    for refusal in (_tag_target_conflict(filters.get("merged-since", ""),
                                         _repo_target.target()),
                    _boundary_flag_conflict(plan, flags)):
        if refusal:
            print(refusal, file=sys.stderr)
            return 1

    iids_only = "iids" in flags
    failed_only = "failed" in flags
    enrich = "nopipe" not in flags




    role = sorted(k for k in ("author", "assignee", "reviewer") if k in filters)
    if "anyauthor" in flags and role:
        print(
            f"ERROR: `anyauthor` and the role filter(s) {', '.join(role)} ask "
            f"for different boards — anyauthor means every author, "
            f"{role[0]}=... means one. Refusing rather than picking one. Drop "
            f"whichever you did not mean.",
            file=sys.stderr,
        )
        return 1






    cfg = _get_config()
    per_page = cfg["per_page"]
    if "per" in filters:
        per_page = int(filters.pop("per"))





    boundary = None
    if plan is not None and plan.is_tag:
        boundary = _release_gate.resolve_boundary(plan.value)
        if boundary.state != _release_gate.BOUNDARY_RESOLVED:
            print(boundary.refusal, file=sys.stderr)
            return 1
        filters["merged-since"] = boundary.stamp

    try:
        result = subprocess.run(
            _build_list_cmd(filters, per_page,
                            fields=(_LIST_FIELDS if plan is None
                                    else _release_gate.PR_LIST_FIELDS)),
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        print(f"ERROR: gh pr list failed: {exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:



        err = _untrusted.flat(result.stderr.strip()) or "unknown error"
        low = err.lower()


        if _auth_probe.says_not_authenticated(err):
            print("ERROR: gh not authenticated. Run: gh auth login", file=sys.stderr)
        elif ("github host" in low or "not a git repository" in low
                or "git remotes" in low):
            print(_repo_target.no_repo_error("gh-prs"), file=sys.stderr)
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

    if plan is not None:
        return _boundary_slice(rows=prs, plan=plan, boundary=boundary,
                               filters=filters, flags=flags, per_page=per_page)

    _annotate(prs)


    fetched = len(prs)
    if failed_only:
        prs = [p for p in prs if p.get("_checks") == "failed"]












    notes: list[str] = []
    absent: list[str] = []

    def _note(text: str | None, states_an_absence: bool) -> None:
        if not text:
            return
        notes.append(text)
        if states_an_absence:
            absent.append(text)

    _note(_cap_note(per_page, fetched), True)


    _note(_scope_note(filters, per_page, fetched), bool(role) and not fetched)
    if failed_only and fetched > len(prs):
        _note(f"failed excluded {fetched - len(prs)} of {fetched} fetched", False)















    if iids_only:
        for note in absent:
            print(f"# {note}")
        for p in prs:
            number = p.get("number")
            if number is not None:
                print(number)
        return 0

    if enrich:
        _enrich(prs, cfg["enrich_cap"], cfg["enrich_workers"])
        if len(prs) > cfg["enrich_cap"]:
            print(f"(review-thread enrichment capped at {cfg['enrich_cap']} PRs)")
        floor_note = _floor_note(prs)
        if floor_note:
            print(f"({floor_note})")




    watched = _watched_numbers(transport.STATE_DIR)
    if prs:








        for note in absent:
            print(f"({note})")

        print(_untrusted.flat_note("PR titles"))
    print(_render_table(prs, watched))
    footer = _footer(prs, watched, notes)
    if footer:
        print(f"\n{footer}")
    return 0







_COLON_HINT = (
    "The one value on this op that wants a ':' is the boundary, and it has two "
    "colon-free spellings. `merged-since=TAG` (v0.34.0) is the one that keeps "
    "SECOND precision — the tag's own commit instant. `merged-since=YYYY-MM-DD` "
    "is that day at 00:00:00 UTC, which for a release boundary is wrong by up "
    "to a day in the direction of over-counting: measured against v0.35.0, the "
    "bare date returned 75 PRs where the tag's instant returned 20 (#1405).")


def _extra_segments_error(argv: list[str]) -> str | None:

    return _filter_tokens.extra_segments_error(argv, "gh-prs", _COLON_HINT)


def main() -> int:
    use_utf8_stdout()
    extra = _extra_segments_error(sys.argv)
    if extra:
        print(extra, file=sys.stderr)
        return 1
    return main_with_args(sys.argv[1] if len(sys.argv) > 1 else "")


if __name__ == "__main__":
    sys.exit(main())
