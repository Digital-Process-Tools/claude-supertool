#!/usr/bin/env python3
















from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  
sys.path.insert(0, str(Path(__file__).parent.parent / "watch"))  

import _board  
import _filter_tokens  
import _untrusted  
import _auth_probe  
from _env import env_int  
import _checks  
import _proc  
import transport  

WATCH_SOURCE = "gitlab-mr"





STATE_DIR = transport.STATE_DIR
DEFAULT_PER_PAGE = 50
ENRICH_CAP = 40  
ENRICH_WORKERS = 8  


_FLAGS = {"nopipe", "iids", "failed"}


def _get_config() -> dict[str, int]:










    return {
        "enrich_workers": env_int("SUPERTOOL_ENRICH_WORKERS", ENRICH_WORKERS, minimum=1),
        "enrich_cap": env_int("SUPERTOOL_ENRICH_CAP", ENRICH_CAP, minimum=0),
        "per_page": env_int("SUPERTOOL_PER_PAGE", DEFAULT_PER_PAGE, minimum=1),
    }


_STATE_FLAG = {"merged": "--merged", "closed": "--closed", "all": "--all"}


_FILTER_FLAG = {
    "author": "--author",
    "assignee": "--assignee",
    "reviewer": "--reviewer",
    "label": "--label",
    "milestone": "--milestone",
    "source-branch": "--source-branch",
    "target-branch": "--target-branch",


    "search": "--search",
}










SEARCH_ENGINE = "GitLab search"
SEARCH_SCOPE = "title and description only — comments are NOT searched"




_STATES = {"opened"} | set(_STATE_FLAG)







_FILTER_KEYS = set(_FILTER_FLAG) | {"state", "per"}


_VALUE_DOMAINS: dict[str, object] = {
    "state": _STATES,
    "per": _filter_tokens.POSITIVE_INT,
}


def _parse_multi(arg_str: str) -> tuple[dict[str, list[str]], set[str], list[str]]:













    return _filter_tokens.parse_multi(arg_str, _FILTER_KEYS, _FLAGS)


def _parse_args(arg_str: str) -> tuple[dict[str, str], set[str], list[str]]:






    return _filter_tokens.parse(arg_str, _FILTER_KEYS, _FLAGS)


def _unknown_error(unknown: list[str]) -> str:

    return _filter_tokens.unknown_error(unknown, _FILTER_KEYS, _FLAGS)


def _bad_values(filters: dict[str, str]) -> list[tuple[str, str, str]]:

    return _filter_tokens.bad_values(filters, _VALUE_DOMAINS)


def _expand_filters(multi: dict[str, list[str]]) -> list[dict[str, str]]:







    combos: list[dict[str, str]] = [{}]
    for key, values in multi.items():
        combos = [{**combo, key: val} for combo in combos for val in values]
    return combos


def _build_list_cmd(filters: dict[str, str], per_page: int) -> list[str]:





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





    return f"search {query!r} — {SEARCH_ENGINE} over {SEARCH_SCOPE}"


def _run(cmd: list[str], timeout: int = 25) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")


def _api_json(endpoint: str, timeout: int = 10):

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





    data = _api_json(f"projects/:id/merge_requests/{iid}")
    return data if isinstance(data, dict) else {}


def _fetch_approvals(iid: str) -> dict:

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
        raw_changes = detail.get("changes_count")  
        try:
            m["_changes"] = int(raw_changes) if raw_changes is not None else None
        except (ValueError, TypeError):
            m["_changes"] = None



        refs = detail.get("diff_refs")
        m["_diff_refs"] = refs if isinstance(refs, dict) else None
        m["_approved"] = appr.get("approved")
        m["_approved_by"] = appr.get("approved_by") or []
        m["_failed_jobs"] = []










        m["_enriched"] = bool(detail)

    failing = [m for m in targets if m.get("_pipeline") == "failed" and m.get("_pipeline_id")]
    if failing:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            jobs = list(ex.map(lambda m: _fetch_failed_jobs(m["_pipeline_id"]), failing))
        for m, names in zip(failing, jobs):
            m["_failed_jobs"] = names





_pid_alive = _proc.pid_alive


def _watched_iids(state_dir: str = STATE_DIR) -> set[str]:






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

    approved = m.get("_approved")
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


def _is_failing(m: dict) -> bool:






    status = str(m.get("_pipeline") or "")
    return bool(status) and _checks.is_red(status)


def _sort_key(m: dict) -> tuple[int, str]:

    failed = 0 if _is_failing(m) else 1
    return (failed, str(m.get("updated_at", "")))





NO_DIFF_DETAILED_STATUS = "commits_status"


def _has_no_diff(m: dict) -> bool:













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





    return _board.branch_pair(m.get("source_branch"), m.get("target_branch"))


def _row(m: dict, watched: set[str], show_pipe: bool, suffix: str = "") -> str:






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










    if not mrs:
        if search is not None:
            return (f"No MRs match {_search_note(search)}. "
                    "The search ran and matched nothing — an empty result, "
                    "not a lookup that failed.")
        return "No MRs match."
    return "\n".join(_row(m, watched, show_pipe) for m in sorted(mrs, key=_sort_key))


ENRICH_CAP_KNOB = "SUPERTOOL_ENRICH_CAP"


def _unchecked_count(mrs: list[dict]) -> int:






    return sum(1 for m in mrs if not m.get("_enriched"))


def _cap_notice(unchecked: int, total: int, cap: int, failed_only: bool) -> str:












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




    if total > cap:
        body += f". Enrichment cap is {cap}; raise {ENRICH_CAP_KNOB}=N"
    return f"({body})"


def _footer(mrs: list[dict], watched: set[str], show_pipe: bool, unchecked: int = 0) -> str:





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

    show_pipe = failed_only or "nopipe" not in flags

    cfg = _get_config()
    per_page = cfg["per_page"]
    if "per" in filters:
        per_page = int(filters.pop("per"))


    search = filters.get("search")

    try:
        result = _run(_build_list_cmd(filters, per_page))
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        print(f"ERROR: glab mr list failed: {exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:



        err = _untrusted.flat(result.stderr.strip()) or "unknown error"



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




    unchecked = _unchecked_count(mrs) if show_pipe else 0
    notice = _cap_notice(unchecked, total, cfg["enrich_cap"], failed_only)
    if failed_only:
        mrs = [m for m in mrs if _is_failing(m)]





    if iids_only:




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




    watched = _watched_iids(transport.STATE_DIR)





    if mrs:





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
