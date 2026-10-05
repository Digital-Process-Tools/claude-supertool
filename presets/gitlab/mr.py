#!/usr/bin/env python3





from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent))  
sys.path.insert(0, str(Path(__file__).parent.parent))  
from _console import use_utf8_stdout  



from mrs import _conflict_label  
import _body  
import _untrusted  
import _classify_render  
import _auth_probe  
import _status_probe  
import _checks  
import _branch_locale  
import _refname  
import _repo_target  
import _secrets  

DESCRIPTION_MAX = 2000
COMMENT_MAX = 500
COMMENT_TOTAL_MAX = 2000


_CLASSIFY_LEVEL = _classify_render.level_from_env()
TAIL_COMMENTS = 2
NAMESTATUS_DISPLAY_MAX = 50
NAMESTATUS_FETCH_CAP = 500


def _relative_age(iso: str) -> str:





    if not iso:
        return "?"
    try:
        from datetime import datetime, timezone

        s = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except (ValueError, ImportError):
        return "?"


def _glab(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:





    return subprocess.run(
        ["glab"] + args + _repo_target.gl_args(),
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def _glab_api(endpoint: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:





    return subprocess.run(
        ["glab", "api", _repo_target.gl_api_path(endpoint)],
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )


def _glab_fail_detail(r: subprocess.CompletedProcess[str]) -> str:









    for line in _untrusted.split_lines(r.stderr or ""):
        line = _untrusted.flat(line.strip())
        if line and line != "ERROR":
            return f"glab exit {r.returncode}: {line[:120]}"
    return f"glab exit {r.returncode}"


def _fetch_json(
    endpoint: str, noun: str, timeout: int = 10,
) -> tuple[object | None, str | None]:




















    try:
        r = _glab_api(endpoint, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"{noun} API timed out"
    except OSError as e:  




        return None, f"could not run glab ({e})"
    if r.returncode != 0:
        return None, f"{noun} API failed ({_glab_fail_detail(r)})"
    try:
        return json.loads(r.stdout), None
    except json.JSONDecodeError:
        return None, f"{noun} API returned no parseable JSON"


def _fetch_array(
    endpoint: str, noun: str, timeout: int = 10,
) -> tuple[list | None, str | None]:








    data, reason = _fetch_json(endpoint, noun, timeout)
    if reason is not None:
        return None, reason
    if not isinstance(data, list):
        return None, (f"{noun} API returned a {type(data).__name__}, "
                      f"expected an array")
    return data, None








_PER_PAGE = re.compile(r"[?&]per_page=(\d+)")


def _page_cap(endpoint: str) -> "int | None":








    match = _PER_PAGE.search(endpoint or "")
    return int(match.group(1)) if match else None


def _fetch_tally(
    endpoint: str, noun: str, timeout: int = 10,
) -> "tuple[list | None, str | None, bool]":














    rows, reason = _fetch_array(endpoint, noun, timeout)
    if reason is not None or rows is None:
        return None, reason, False
    cap = _page_cap(endpoint)
    return rows, None, cap is not None and len(rows) >= cap


def _floor(count: int, capped: bool) -> str:






    return f">={count}" if capped else str(count)





_PAGE_FULL = ("  ! PAGE FULL — this came off ONE unpaginated page and GitLab "
              "returned exactly its per_page limit, so the count(s) above are "
              "a LOWER BOUND, not a total. GitLab pages oldest-first, so what "
              "is missing is the newest.")


def _as_dict(value: object) -> dict:







    return value if isinstance(value, dict) else {}


def _dict_elements(seq: object) -> tuple[list[dict], int]:







    if not isinstance(seq, list):
        return [], 0
    kept = [e for e in seq if isinstance(e, dict)]
    return kept, len(seq) - len(kept)


def _array_elements(value: object) -> tuple[list[dict], int, int]:









    if value is None:
        return [], 0, 0
    if not isinstance(value, list):
        return [], 1, 1
    kept, bad = _dict_elements(value)
    return kept, bad, len(value)


def _unreadable(skipped: int, total: int, noun: str) -> str:












    if skipped <= 0:
        return ""
    return f"  ! {skipped} of {total} {noun} had a shape supertool could not read"


def _print_unreadable(skipped: int, total: int, noun: str) -> None:

    note = _unreadable(skipped, total, noun)
    if note:
        print(note)


def _approver_name(entry: object) -> str:

    if not isinstance(entry, dict):
        return "?"
    user = entry.get("user")
    if not isinstance(user, dict):
        return "?"
    name = user.get("username")
    return str(name) if name else "?"


def _approvals_line(iid: str | int) -> str:


























    unknown = "Approved by: UNKNOWN"




    approvals, reason = _fetch_json(
        f"projects/:id/merge_requests/{iid}/approvals", "approvals")
    if reason is not None:
        return f"{unknown} — {reason}"
    if not isinstance(approvals, dict):
        return (f"{unknown} — approvals API returned a "
                f"{type(approvals).__name__}, expected an object")
    if "approved_by" not in approvals:
        note = approvals.get("message") or approvals.get("error") or ""
        detail = f": {str(note)[:120]}" if note else ""
        return f"{unknown} — approvals payload carries no approved_by field{detail}"
    approved_by = approvals["approved_by"]
    if not isinstance(approved_by, list):
        return (f"{unknown} — approved_by is a "
                f"{type(approved_by).__name__}, expected a list")
    if not approved_by:
        return "Approved by: none"
    return f"Approved by: {', '.join(_approver_name(a) for a in approved_by)}"


















_GL_JOB_RESOLVES_ITSELF = {"running", "pending", "created", "scheduled"}





_NO_JOBS = "none — the jobs API reports no job on this pipeline"


def _pipeline_leg_lines(pipe_id: str | int,
                        cap: int = _checks.NAMED_CAP) -> list[str]:





















    if not pipe_id:
        return []
    jobs, reason, capped = _fetch_tally(
        f"projects/:id/pipelines/{pipe_id}/jobs?per_page=100", "jobs")
    if reason is not None:







        return [f"  legs: UNKNOWN — {reason}"]

    entries, skipped = _dict_elements(jobs)






    states = [j.get("status") for j in entries] + [None] * skipped
    lines = [f"  legs: {_checks.summarize(states) if states else _NO_JOBS}"]

    groups: dict[str, list[tuple[str, str]]] = {}
    for j in entries:
        status = str(j.get("status") or "").strip().lower()
        if not status or status == "success" or status in _GL_JOB_RESOLVES_ITSELF:
            continue






        name = _untrusted.flat(str(j.get("name") or "?"))
        job_id = str(j.get("id") or "")
        groups.setdefault(status, []).append((name, job_id))

    named = 0
    for label in sorted(groups):
        items = groups[label]
        shown = items[:cap]
        parts = [f"{n} (job #{jid})" if jid else n for n, jid in shown]
        text = ", ".join(parts)
        if len(items) > cap:
            text += f", +{len(items) - cap} more"
        lines.append(f"  {_untrusted.flat(label)}: {text}")
        named += 1
    if not named and states:
        lines.append("  jobs: none non-passing reported for this pipeline")
    if capped:



        lines.append(_PAGE_FULL)
    note = _unreadable(skipped, len(jobs), "pipeline jobs")
    if note:
        lines.append(note)
    return lines


def _unresolved_thread_lines(discussions: list, capped: bool) -> list[str]:






    threads, bad_threads = _dict_elements(discussions or [])
    bad_notes = notes_total = resolvable = unresolved = 0
    for entry in threads:


        thread_notes, bad, seen = _array_elements(entry.get("notes"))
        bad_notes += bad
        notes_total += seen
        marked = [n for n in thread_notes if n.get("resolvable")]
        if not marked:
            continue
        resolvable += 1
        if not all(n.get("resolved") for n in marked):
            unresolved += 1
    lines = [f"Unresolved threads: {_floor(unresolved, capped)} / "
             f"{_floor(resolvable, capped)}"]
    if capped:
        lines.append(_PAGE_FULL)
    for note in (_unreadable(bad_threads, len(discussions or []), "discussions"),
                 _unreadable(bad_notes, notes_total, "discussion notes")):
        if note:
            lines.append(note)
    return lines


def _failed_jobs_block(jobs: list, capped: bool) -> list[str]:






    named, bad_jobs = _dict_elements(jobs or [])
    if named:
        lines = [f"Failed jobs ({_floor(len(named), capped)}):"]
        lines.extend(_failed_job_lines(named))
    else:
        lines = ["Failed jobs: none — the jobs API reports no failed job "
                 "on this failed pipeline"]
    if capped:
        lines.append(_PAGE_FULL)
    note = _unreadable(bad_jobs, len(jobs or []), "failed jobs")
    if note:
        lines.append(note)
    return lines


def _failed_job_lines(named: list[dict]) -> list[str]:






    flat = _untrusted.flat
    return [
        f"  #{flat(str(j.get('id', '?')))} | {flat(str(j.get('name', '?')))} "
        f"| {flat(str(j.get('stage', '?')))}"
        for j in named
    ]


def _local_branch_check(source: str) -> str:







    return _branch_locale.check(source)


_MERGE_TREE_NOISE_PREFIXES = (
    "Auto-merging ",
    "CONFLICT ",
    "warning:",
    "hint:",
    "error:",
)


def _get_conflicting_files(source: str, target: str) -> list[str]:








    try:
        result = subprocess.run(
            ["git", "merge-tree", "--name-only", "--write-tree",
             f"origin/{target}", f"origin/{source}"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    if result.returncode == 0:
        return []
    files: list[str] = []
    seen: set[str] = set()
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line or line in seen:
            continue
        if re.fullmatch(r"[0-9a-f]{40}", line):

            continue
        if any(line.startswith(p) for p in _MERGE_TREE_NOISE_PREFIXES):

            continue
        files.append(line)
        seen.add(line)
    return files


_MERGE_TREE_HEADER_RE = re.compile(
    r"^(changed in both|added in (?:local|remote)|removed in (?:local|remote))\b"
)
_MERGE_TREE_PATH_RE = re.compile(  
    r"^  (?:base|our|their)\s+\d+\s+[0-9a-f]+\s+(.+)$"
)
HUNK_LINES_PER_FILE = 40
BINARY_HUNK_NOTE = "(binary file — conflict hunks not shown; resolve by picking a version)"

HUNK_TIMEOUT_BASE = 15
HUNK_TIMEOUT_PER_FILE = 5
HUNK_TIMEOUT_MAX = 60


def _hunk_timeout(file_count: int) -> int:










    return min(HUNK_TIMEOUT_MAX, max(HUNK_TIMEOUT_BASE, HUNK_TIMEOUT_PER_FILE * file_count))


def _hunk_display_lines(block: str) -> list[str]:
















    return [_untrusted.visible(line, keep=chr(9))
            for line in _untrusted.split_lines(block)]


def _is_binary_hunk(block: str) -> bool:










    return "\x00" in block or "�" in block


def _get_conflict_hunks(
    source: str, target: str, file_count: int = 0,
) -> tuple[dict[str, str], str | None]:





























    try:
        base_result = subprocess.run(
            ["git", "merge-base", f"origin/{target}", f"origin/{source}"],
            capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {}, "git merge-base timed out after 5s"
    except OSError as exc:
        return {}, f"could not run git: {exc}"
    if base_result.returncode != 0 or not base_result.stdout.strip():
        return {}, (
            "git merge-base found no common ancestor "
            f"(origin/{target} and origin/{source} may not be fetched — try: git fetch origin)"
        )
    base = base_result.stdout.strip()

    timeout = _hunk_timeout(file_count)
    try:
        result = subprocess.run(
            ["git", "merge-tree", base,
             f"origin/{target}", f"origin/{source}"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {}, f"git merge-tree timed out after {timeout}s"
    except OSError as exc:
        return {}, f"could not run git: {exc}"
    if result.returncode != 0 and not result.stdout:





        detail = _untrusted.split_lines((result.stderr or "").strip())
        suffix = f": {_untrusted.flat(detail[0])}" if detail else ""
        return {}, f"git merge-tree failed (exit {result.returncode}){suffix}"
    if not result.stdout:
        return {}, None

    blocks: dict[str, list[str]] = {}
    current_path: str | None = None
    current_lines: list[str] = []

    def _flush() -> None:
        if current_path:
            blocks.setdefault(current_path, []).extend(current_lines)




















    for line in _untrusted.split_lines(result.stdout):
        if _MERGE_TREE_HEADER_RE.match(line):
            _flush()
            current_path = None
            current_lines = []
            continue
        path_match = _MERGE_TREE_PATH_RE.match(line)
        if path_match:

            if current_path is None:
                current_path = path_match.group(1)
            continue
        if current_path is not None:
            current_lines.append(line)
    _flush()

    return (
        {p: "\n".join(lines).strip() for p, lines in blocks.items() if any(lines)},
        None,
    )






_ORDINARY_REF = _refname.ORDINARY_REF
_ordinary_ref = _refname.ordinary
_shell_ref = _refname.shell_ref
_ref_warning = _refname.warning


def _format_error(stderr: str, resource: str, identifier: str) -> str:

    s = stderr.lower()
    if _status_probe.says_not_found(s):
        return (f"ERROR: {resource} #{identifier} not found "
                f"{_repo_target.not_found_scope()}. "
                f"{_repo_target.gl_not_found_hint()}")






    if (_auth_probe.says_not_authenticated(s, _auth_probe.GITLAB_MARKERS)
            or _secrets.mentions_gitlab_token(s)):
        return "ERROR: glab not authenticated. Run: glab auth login"
    if _status_probe.says_forbidden(s):
        return f"ERROR: permission denied for {resource} #{identifier}. Check your GitLab access token permissions."

    return (f"ERROR: glab failed for {resource} #{identifier}: "
            f"{_untrusted.flat(stderr.strip())}")


def _render_note(note: dict, cap: int | None = COMMENT_MAX, *,
                  level: str = _CLASSIFY_LEVEL,
                  budget: "_classify_render.Budget | None" = None) -> str:






























    author = _untrusted.flat(_as_dict(note.get("author")).get("username", "?"))
    body = note.get("body") or ""
    trunc = ""
    if cap is not None and len(body) > cap:
        body = body[:cap]
        trunc = f"\n{_body.comment_cut_notice(cap)}"
    created = (note.get("created_at") or "")[:10]
    if budget is not None:
        verdict = budget.line(body, level=level)
    else:
        verdict = _classify_render.verdict_line(body, level=level)
    return f"\n**{author}** ({created}):\n{_untrusted.fence(body)}{trunc}\n{verdict}\n"


def _fmt_kb(nbytes: int) -> str:
    if nbytes < 1024:
        return f"{nbytes}B"
    return f"{nbytes / 1024:.1f}KB"


def _budgeted_comments(notes: list, budget: int, tail: int, *,
                        classify_level: str = _CLASSIFY_LEVEL,
                        classify_budget: "_classify_render.Budget | None" = None,
                        ) -> tuple[list[str], int, int]:















    rendered_all = [_render_note(n, level=classify_level, budget=classify_budget)
                     for n in notes]
    if not rendered_all:
        return [], 0, 0
    tail_keep = min(tail, len(rendered_all))
    if tail_keep >= len(rendered_all):
        return rendered_all, 0, 0
    tail_slice = rendered_all[-tail_keep:] if tail_keep else []
    head_pool = rendered_all[:-tail_keep] if tail_keep else rendered_all
    tail_size = sum(len(r) for r in tail_slice)
    remaining = max(0, budget - tail_size)
    head_kept: list[str] = []
    used = 0
    for r in head_pool:
        if used + len(r) > remaining:
            break
        head_kept.append(r)
        used += len(r)
    hidden = head_pool[len(head_kept):]
    hidden_bytes = sum(len(r.encode("utf-8")) for r in hidden)
    return head_kept + (["__GAP__"] if hidden else []) + tail_slice, len(hidden), hidden_bytes


def _name_status_flag(f: dict) -> str:

    if f.get("new_file"):
        return "A"
    if f.get("deleted_file"):
        return "D"
    if f.get("renamed_file"):
        return "R"
    return "M"


class _NameStatus(NamedTuple):


    entries: list[tuple[str, str]]
    skipped: int
    reason: str | None = None
    """Why the list is short or absent — `None` when the endpoint answered.

    An empty `entries` with no reason is "this MR changes nothing"; an empty
    `entries` with a reason is "nobody could read the diff". They rendered
    identically — as no `## Files` block at all — until #812."""

    @property
    def total(self) -> int:
        return len(self.entries) + self.skipped


def _get_name_status(iid: str | int, fetch_all: bool) -> _NameStatus:














    entries: list[tuple[str, str]] = []
    skipped = 0
    reason: str | None = None
    page = 1
    while True:
        diffs, reason = _fetch_array(
            f"projects/:id/merge_requests/{iid}/diffs?per_page=100&page={page}",
            "diffs")
        if reason is not None or not diffs:
            break
        files, bad = _dict_elements(diffs)
        skipped += bad
        for f in files:
            flag = _name_status_flag(f)
            new_path = f.get("new_path") or ""
            old_path = f.get("old_path") or ""
            if flag == "R" and old_path and new_path and old_path != new_path:
                path = f"{old_path} → {new_path}"
            else:
                path = new_path or old_path or "?"
            entries.append((flag, path))
        if not fetch_all or len(diffs) < 100 or len(entries) >= NAMESTATUS_FETCH_CAP:
            break
        page += 1
    return _NameStatus(entries, skipped, reason)


def _coerce_count(changes: object) -> int | None:






    m = re.match(r"\d+", str(changes))
    return int(m.group()) if m else None


def _render_name_status(
    name_status: _NameStatus, changes: object, full: bool, iid: str | int
) -> list[str]:













    entries = name_status.entries
    reason = name_status.reason
    if not entries:
        if reason is None:
            return []
        heading = f"\n## Files ({changes})"
        return [heading, f"  ! file list unavailable — {reason}"]
    shown = entries if full else entries[:NAMESTATUS_DISPLAY_MAX]
    total = _coerce_count(changes)
    if total is None or total < len(entries):
        total = len(entries)
    lines = [f"\n## Files ({changes})"]
    lines.extend(f" {flag}  {path}" for flag, path in shown)
    hidden = total - len(shown)
    if hidden > 0:
        if reason is not None:



            lines.append(f" … +{hidden} more not fetched — {reason}")
        elif full:
            lines.append(f" … +{hidden} more (output capped at {NAMESTATUS_FETCH_CAP} files)")
        else:
            lines.append(f" … +{hidden} more (use gl-mr:{iid}:full)")
    elif reason is not None:
        lines.append(f"  ! file list may be incomplete — {reason}")
    note = _unreadable(name_status.skipped, name_status.total, "changed files")
    if note:
        lines.append(note)
    return lines


def main() -> int:
    use_utf8_stdout()
    if len(sys.argv) < 2:
        print("ERROR: usage: mr.py NUMBER [status|full]")
        return 1

    arg = sys.argv[1]
    flags = sys.argv[2:]
    slim = "status" in flags
    full = "full" in flags


    if not arg.isdigit():
        try:
            branch_result = _glab_api(
                f"projects/:id/merge_requests?source_branch={arg}&state=opened&per_page=1"
            )
            if branch_result.returncode == 0:
                mrs = json.loads(branch_result.stdout)
                found, unreadable = _dict_elements(mrs)
                if found:
                    arg = str(found[0].get("iid", arg))
                elif unreadable:
                    print(f"ERROR: branch lookup for {arg!r} returned {unreadable} MR(s) "
                          f"with a shape supertool could not read")
                    return 1
                else:

                    branch_result2 = _glab_api(
                        f"projects/:id/merge_requests?source_branch={arg}&per_page=1"
                    )
                    if branch_result2.returncode == 0:
                        mrs2 = json.loads(branch_result2.stdout)
                        found2, unreadable2 = _dict_elements(mrs2)
                        if found2:
                            arg = str(found2[0].get("iid", arg))
                        elif unreadable2:
                            print(f"ERROR: branch lookup for {arg!r} returned {unreadable2} "
                                  f"MR(s) with a shape supertool could not read")
                            return 1
                        else:
                            print(f"ERROR: no MR found for branch {arg!r}")
                            return 1
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            print(f"ERROR: branch lookup failed: {e}")
            return 1

    try:
        result = _glab(["mr", "view", arg, "--output", "json"])
    except FileNotFoundError:
        print("ERROR: glab not found — install the GitLab CLI")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: glab timed out")
        return 1

    if result.returncode != 0:
        print(_format_error(result.stderr, "MR", arg))
        return 1

    try:
        d = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"ERROR: invalid JSON from glab\n{result.stdout[:500]}")
        return 1





    if not isinstance(d, dict):
        print(f"ERROR: glab mr view returned a {type(d).__name__}, expected an object")
        return 1

    def _latest_pipeline(iid: str | int) -> tuple[dict, str | None]:














        pipes, reason = _fetch_array(
            f"projects/:id/merge_requests/{iid}/pipelines?per_page=1", "pipelines")
        if reason is not None:
            return {}, reason
        found, _ = _dict_elements(pipes or [])
        return (found[0] if found else {}), None

    def _pipe_meta(pipeline: dict) -> str:

        bits = []
        sha = (pipeline.get("sha") or "")[:8]
        if sha:
            bits.append(sha)

        source = pipeline.get("source")
        if source and source not in ("push",):
            bits.append(source)

        user = _as_dict(pipeline.get("user")).get("username")
        if user:
            bits.append(f"by {user}")

        status = pipeline.get("status", "")
        if status == "running":
            started = pipeline.get("started_at") or pipeline.get("created_at")
            if started:
                from datetime import datetime, timezone
                try:
                    dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    elapsed = (datetime.now(timezone.utc) - dt).total_seconds()
                    if elapsed > 60:
                        bits.append(f"running {int(elapsed // 60)}m")
                    else:
                        bits.append(f"running {int(elapsed)}s")
                except (ValueError, TypeError):
                    pass
        else:
            updated = (pipeline.get("updated_at") or pipeline.get("created_at") or "")[:19].replace("T", " ")
            if updated:
                bits.append(updated)
        duration = pipeline.get("duration")
        if isinstance(duration, (int, float)) and duration > 0:
            mins, secs = divmod(int(duration), 60)
            bits.append(f"{mins}m{secs:02d}s" if mins else f"{secs}s")
        coverage = pipeline.get("coverage")
        if coverage is not None:
            bits.append(f"cov {coverage}%")
        return " | ".join(bits)

    if slim:
        iid = d.get("iid", arg)
        state = d.get("state", "?")







        merge_status = (d.get("merge_status")
                        or d.get("detailed_merge_status") or "?")
        has_conflicts = d.get("has_conflicts", False)
        fresh, pipe_reason = _latest_pipeline(iid)
        pipeline = fresh or _as_dict(d.get("pipeline")) or _as_dict(d.get("head_pipeline"))
        pipe_status = pipeline.get("status", "none")
        pipe_id = pipeline.get("id", "")
        merged_at = d.get("merged_at") or "-"
        merge_commit = d.get("merge_commit_sha") or d.get("squash_commit_sha") or ""
        web_url = d.get("web_url", "")
        print(f"!{iid} | state: {state} | merge_status: {merge_status} | conflicts: {'yes' if has_conflicts else 'no'}")
        print(f"branch: {_untrusted.flat(d.get('source_branch') or '?')} -> "
              f"{_untrusted.flat(d.get('target_branch') or '?')}")
















        print(_approvals_line(iid).replace("Approved by:", "approved_by:", 1))
        if pipe_reason is not None and not pipeline:



            print(f"pipeline: UNKNOWN — {pipe_reason}")
        else:
            pipe_str = pipe_status + (f" (#{pipe_id})" if pipe_id else "")
            meta = _pipe_meta(pipeline)
            if meta:
                pipe_str += f" | {meta}"
            print(f"pipeline: {pipe_str}")
            if pipe_reason is not None:
                print(f"  ! live pipeline lookup declined ({pipe_reason}) — status "
                      f"above comes from the MR payload and can be stale")














        for line in _pipeline_leg_lines(pipe_id):
            print(line)
        print(f"merged_at: {merged_at}")
        if merge_commit:
            print(f"merge_commit: {merge_commit[:12]}")
        if web_url:
            print(f"url: {web_url}")
        return 0


    title = _untrusted.flat(d.get("title", "?"))
    state = d.get("state", "?")
    iid = d.get("iid", arg)
    source = _untrusted.flat(d.get("source_branch", "?"))
    target = _untrusted.flat(d.get("target_branch", "?"))
    author = _untrusted.flat(_as_dict(d.get("author")).get("username", "?"))
    web_url = d.get("web_url", "")
    raw_labels = d.get("labels")
    labels = _untrusted.flat(", ".join(
        str(label) for label in raw_labels) if isinstance(raw_labels, list) else "none")
    labels = labels or "none"
    milestone = _untrusted.flat(_as_dict(d.get("milestone")).get("title", "none"))
    merge_status = d.get("merge_status") or d.get("detailed_merge_status") or "?"
    merge_commit = d.get("merge_commit_sha") or d.get("squash_commit_sha") or ""
    draft = d.get("draft", False) or d.get("work_in_progress", False)




    fresh_pipeline, pipe_reason = _latest_pipeline(iid)
    pipeline = (fresh_pipeline or _as_dict(d.get("pipeline"))
                or _as_dict(d.get("head_pipeline")))
    pipe_status = pipeline.get("status", "none")
    pipe_id = pipeline.get("id", "")
    pipe_meta = _pipe_meta(pipeline)



    pipe_unknown = pipe_reason is not None and not pipeline
    pipe_stale = pipe_reason is not None and bool(pipeline)


    changes = d.get("changes_count") or 0
    diff_stats = _as_dict(d.get("diff_stats"))
    additions = diff_stats.get("additions", 0)
    deletions = diff_stats.get("deletions", 0)


    reviewers, reviewers_skipped, reviewers_total = _array_elements(d.get("reviewers"))
    reviewer_names = [r.get("username", "?") for r in reviewers]





    description_raw = d.get("description") or ""
    description_total = len(description_raw)
    description, description_withheld = _body.cut(
        description_raw, None if full else DESCRIPTION_MAX)



    draft_marker = " [DRAFT]" if draft else ""
    print(_untrusted.banner())
    print(f"# !{iid} {title}{draft_marker}")
    print(f"State: {state} | Author: {author}")
    print(f"Branch: {source} -> {target}")
    local_check = _local_branch_check(source)
    if local_check:
        print(local_check)
    print(f"Labels: {labels}")
    print(f"Milestone: {milestone}")
    if description_withheld:


        print(_body.header_notice(
            description, description_total, description_withheld))


    assignees, assignees_skipped, assignees_total = _array_elements(d.get("assignees"))
    assignee_names = [a.get("username", "?") for a in assignees]
    print(f"Assignees: {', '.join(assignee_names) if assignee_names else 'none'}")
    _print_unreadable(assignees_skipped, assignees_total, "assignees")


    print(f"Reviewers: {', '.join(reviewer_names) if reviewer_names else 'none'}")
    _print_unreadable(reviewers_skipped, reviewers_total, "reviewers")


    created_at = d.get("created_at") or ""
    updated_at = d.get("updated_at") or ""
    if created_at:
        age_str = f"Created: {_relative_age(created_at)}"
        if updated_at and updated_at != created_at:
            age_str += f" | Updated: {_relative_age(updated_at)}"
        print(age_str)



    print(_approvals_line(iid))







    discussions, disc_reason, disc_capped = _fetch_tally(
        f"projects/:id/merge_requests/{iid}/discussions?per_page=100", "discussions")
    if disc_reason is not None:
        print(f"Unresolved threads: UNKNOWN — {disc_reason}")
    else:
        for line in _unresolved_thread_lines(discussions, disc_capped):
            print(line)


    if pipe_unknown:
        print(f"Pipeline: UNKNOWN — {pipe_reason}")
    else:
        pipe_str = pipe_status
        if pipe_id:
            pipe_str += f" (#{pipe_id})"
        if pipe_meta:
            pipe_str += f" | {pipe_meta}"
        print(f"Pipeline: {pipe_str}")
        if pipe_stale:
            print(f"  ! live pipeline lookup declined ({pipe_reason}) — status "
                  f"above comes from the MR payload and can be stale")





    if pipe_status == "failed" and pipe_id:
        jobs, jobs_reason, jobs_capped = _fetch_tally(
            f"projects/:id/pipelines/{pipe_id}/jobs?per_page=100&scope=failed",
            "jobs")
        if jobs_reason is not None:
            print(f"Failed jobs: UNKNOWN — {jobs_reason}")
        else:
            for line in _failed_jobs_block(jobs, jobs_capped):
                print(line)

    if additions or deletions:
        print(f"Changes: {changes} files, +{additions} -{deletions}")
    elif changes:

        print(f"Changes: {changes} files (line counts unavailable on large MRs)")



    if changes:
        for line in _render_name_status(_get_name_status(iid, full), changes, full, iid):
            print(line)







    conflict_files: list[str] = []
    conflict_label = _conflict_label({**d, "_diff_refs": d.get("diff_refs")})
    if conflict_label == "conflict":
        conflict_files = _get_conflicting_files(source, target)
        if conflict_files:
            print(f"Conflicts: YES — cannot merge ({len(conflict_files)} file{'s' if len(conflict_files) != 1 else ''})")
        else:
            print("Conflicts: YES — cannot merge")
    elif conflict_label == "empty":
        print("Conflicts: NO — cannot merge: source branch has no commits, so there is nothing to merge")
    else:
        print(f"Merge status: {merge_status}")


    if merge_commit:
        print(f"Merge commit: {merge_commit[:12]}")

    if web_url:
        print(f"URL: {web_url}")


    if conflict_files:
        plural = "s" if len(conflict_files) != 1 else ""
        print(f"\n## Conflicts ({len(conflict_files)} file{plural})")
        for path in conflict_files:
            print(f"  {path}")

        hunks, hunks_skipped = _get_conflict_hunks(source, target, len(conflict_files))
        no_preview: list[str] = []
        for path in conflict_files:
            block = hunks.get(path, "")
            if not block:
                no_preview.append(path)
                continue
            if _is_binary_hunk(block):
                print(f"\n### {path}")
                print(f"  {BINARY_HUNK_NOTE}")
                continue
            lines = _hunk_display_lines(block)
            truncated = ""
            if len(lines) > HUNK_LINES_PER_FILE:
                extra = len(lines) - HUNK_LINES_PER_FILE
                lines = lines[:HUNK_LINES_PER_FILE]
                truncated = f"\n  ... ({extra} more lines)"
            print(f"\n### {path}")
            for line in lines:
                print(f"  {line}")
            if truncated:
                print(truncated)

        if hunks_skipped:



            print(f"\n  Hunk preview unavailable: {hunks_skipped}.")
            print("  The conflicted file list above is still accurate — it comes from a")
            print("  separate `git merge-tree --write-tree --name-only` call that carries")
            print("  no blob content, so it cannot fail this way.")
            print("  To see the hunks, run the merge locally with the commands below.")
        elif no_preview:
            plural_np = "s" if len(no_preview) != 1 else ""
            print(
                f"\n  No hunk preview for {len(no_preview)} file{plural_np}: "
                f"{', '.join(no_preview)}"
            )
            print("  — git merge-tree returned no conflict content there (add/add,")
            print("  delete/modify and rename conflicts have no inline hunks).")

        print("\nTo resolve:")
        ref_warning = _ref_warning([source, target, *conflict_files])
        if ref_warning:
            print(ref_warning)
        print(
            f"  git checkout {_shell_ref(source)} && git fetch origin "
            f"&& git merge origin/{_shell_ref(target)}"
        )
        files_arg = " ".join(_shell_ref(f) for f in conflict_files)
        print(f"  # Resolve <<<<<<< markers in the files above, then:")
        print(f"  git add {files_arg} && git commit && git push")



    issue_match = re.search(r'#(\d{4,})', description_raw)
    if issue_match:
        issue_iid = issue_match.group(1)





        unavailable = f"\nIssue: #{issue_iid} — details unavailable"
        try:
            issue_result = _glab_api(f"projects/:id/issues/{issue_iid}")
            if issue_result.returncode != 0:
                print(f"{unavailable} ({_glab_fail_detail(issue_result)})")
            else:
                issue_data = json.loads(issue_result.stdout)
                if not isinstance(issue_data, dict):
                    print(f"{unavailable} (issues API returned a "
                          f"{type(issue_data).__name__}, expected an object)")
                else:
                    issue_title = issue_data.get("title") or "?"
                    issue_state = issue_data.get("state") or "?"
                    raw_labels = issue_data.get("labels")
                    issue_labels = ", ".join(
                        str(label) for label in raw_labels
                    ) if isinstance(raw_labels, list) and raw_labels else "none"
                    raw_assignees = issue_data.get("assignees")
                    issue_assignees = ", ".join(
                        (a.get("username") or "?") if isinstance(a, dict) else "?"
                        for a in raw_assignees
                    ) if isinstance(raw_assignees, list) and raw_assignees else "none"
                    print(f"\n## Issue #{issue_iid} — {issue_title}")
                    print(f"State: {issue_state} | Labels: {issue_labels} | Assignees: {issue_assignees}")
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            print(f"{unavailable} ({type(e).__name__})")
        except OSError as e:
            print(f"{unavailable} (could not run glab: {e})")



    classify_budget = _classify_render.Budget()


    if description:
        print(f"\n## Description\n{_untrusted.fence(description)}")
        if description_withheld:
            print(f"\n{_body.cut_notice(description_withheld)}")
        print(classify_budget.line(description, level=_CLASSIFY_LEVEL))
    else:
        print("\n## Description\n_(empty)_")



    human_notes: list = []
    notes_skipped = notes_seen = 0
    notes, notes_reason, notes_capped = _fetch_tally(
        f"projects/:id/merge_requests/{iid}/notes?per_page=50&sort=asc", "notes")
    if notes_reason is not None:






        print("\n## Comments (UNKNOWN)")
        print(f"  ! comments could not be read — {notes_reason}")
        return 0
    readable, notes_skipped = _dict_elements(notes or [])
    notes_seen = len(notes or [])
    human_notes = [n for n in readable if not n.get("system", False)]






    print(f"\n## Comments ({_floor(len(human_notes), notes_capped)})")
    if notes_capped:
        print(_PAGE_FULL)
    _print_unreadable(notes_skipped, notes_seen, "comments")
    if full:
        for r in (_render_note(n, None, level=_CLASSIFY_LEVEL, budget=classify_budget)
                  for n in human_notes):
            print(r, end="")
    else:
        rendered, hidden_count, hidden_bytes = _budgeted_comments(
            human_notes, COMMENT_TOTAL_MAX, TAIL_COMMENTS,
            classify_level=_CLASSIFY_LEVEL, classify_budget=classify_budget,
        )
        for r in rendered:
            if r == "__GAP__":
                print(
                    f"\n... {hidden_count} more comment(s) hidden ({_fmt_kb(hidden_bytes)})."
                    f" Use gl-mr:{iid}:full for everything."
                )
                continue
            print(r, end="")

    return 0


if __name__ == "__main__":
    sys.exit(main())
