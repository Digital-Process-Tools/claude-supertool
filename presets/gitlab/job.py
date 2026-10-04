#!/usr/bin/env python3















from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  
from api import path_refusal  
from _env import env_int  
import _branch_locale  
import _untrusted  
import _auth_probe  
import _status_probe  
import _digits  
import _image_root  
import _job_argv  
import _repo_target  
import _secrets  
import _st_hint  
import _maintenance  


def _local_branch_check(source: str, actionable: bool = True) -> str:
























    return _branch_locale.check(source, actionable)


def _format_error(stderr: str, resource: str, identifier: str) -> str:

    s = stderr.lower()
    if _status_probe.says_not_found(s):
        return (f"ERROR: {resource} #{identifier} not found "
                f"{_repo_target.not_found_scope()}. Check the ID. Use "
                "gl-pipeline to list jobs first, then gl-job with the job ID.")






    if (_auth_probe.says_not_authenticated(s, _auth_probe.GITLAB_MARKERS)
            or _secrets.mentions_gitlab_token(s)):
        return "ERROR: glab not authenticated. Run: glab auth login"
    if _status_probe.says_forbidden(s):
        return f"ERROR: permission denied for {resource} #{identifier}. Check your GitLab access token permissions."

    return (f"ERROR: glab failed for {resource} #{identifier}: "
            f"{_untrusted.flat(stderr.strip())}")


def _get_config() -> dict:

    return {
        "lines": env_int(os.environ.get("SUPERTOOL_LINES"), "SUPERTOOL_LINES", 80, minimum=1),
        "error_patterns": os.environ.get(
            "SUPERTOOL_ERROR_PATTERNS",


            "ERROR,FAILURES!,Fatal,Failed asserting,🪪,notSubtype,argument.type,return.type"
        ).split(","),
        "error_context": env_int(os.environ.get("SUPERTOOL_ERROR_CONTEXT"), "SUPERTOOL_ERROR_CONTEXT", 8, minimum=0),
        "job_patterns": _parse_job_patterns(os.environ.get("SUPERTOOL_JOB_PATTERNS", "")),
    }


def _parse_job_patterns(raw: str) -> list[dict]:





    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


def _select_job_patterns(
    job_name: str, job_patterns: list[dict], default_patterns: list[str]
) -> tuple[list[str], str | None]:





    for entry in job_patterns:
        name_re = entry.get("job", "")
        if not name_re:
            continue
        try:
            matched = re.search(name_re, job_name) is not None
        except re.error:
            matched = name_re in job_name
        if matched:
            patterns = entry.get("patterns") or default_patterns
            return patterns, entry.get("resolution")
    return default_patterns, None








_CAUSE_MARKERS = [
    re.compile(r"^\s*Caused by\b"),





    re.compile(r"[\w\\]*(?:Exception|Error):\s"),


    re.compile(r"\bERROR \d+ \(\w+\):"),
    re.compile(r"SQLSTATE\["),
    re.compile(r"^\s*In \S+\.php line \d+:"),
    re.compile(r"Exit Code:\s*\d+"),
    re.compile(r"Fatal error:"),
    re.compile(r"Segmentation (?:fault|violation)"),
    re.compile(r"Allowed memory size of \d+ bytes exhausted"),
    re.compile(r"\w*CrashedException"),
]

_SECTION_START = re.compile(r"^section_start:\d+:(\S+)")


def _cause_lines(lines: list[str]) -> list[int]:

    if os.environ.get("GL_JOB_CAUSE_MARKERS", "1") == "0":
        return []
    return [
        i for i, line in enumerate(lines)
        if any(rx.search(line) for rx in _CAUSE_MARKERS)
    ]


def _last_section(lines: list[str]) -> str | None:

    for line in reversed(lines):
        match = _SECTION_START.match(line)
        if match:
            return match.group(1)
    return None





























_STREAM_PREFIX = r"(?:\S+\s+)?"






_TRAILING = r"[ \t]*\Z"
_BOILERPLATE = [
    re.compile(_STREAM_PREFIX + r"ERROR: Job failed: exit code \d+" + _TRAILING),
    re.compile(_STREAM_PREFIX + r"section_(?:start|end):\d+:\S+" + _TRAILING),
    re.compile(_STREAM_PREFIX +
              r"Cleaning up project directory and file based variables" + _TRAILING),
]


def _is_boilerplate(line: str) -> bool:






    return any(rx.match(line) for rx in _BOILERPLATE)









_FAIL_SELECTOR_FITS = "failed"


def _selection_mismatch(job_status: str, job_id: str) -> str:

















    if job_status == _FAIL_SELECTOR_FITS:
        return ""
    return (
        f"\n> NOTE: this job's status is `{job_status}`, not `failed`, so "
        f"error-block selection is a poor fit — it can only find lines an error "
        f"pattern marks, and a job that produced no failure puts its "
        f"diagnostics outside them (teardown, the point it was stopped, the "
        f"tail). Treat the above as the lines that MATCHED, not as what the log "
        f"contains.\n"
        f"> Read it instead with:\n"
        f">   {_st_hint.st_hint(f'gl-job:{job_id}:raw:-80')}          # tail, "
        f"where a cancellation's evidence usually sits\n"
        f">   {_st_hint.st_hint(f'gl-job:{job_id}:grep:PATTERN')}     # the "
        f"whole trace is still searchable\n"
    )








UNCLASSIFIED_HEADER = "## FAILED — supertool could not classify this job"
BOILERPLATE_ONLY_HEADER = (
    "## FAILED — only boilerplate matched, no cause identified")


def _print_unmatched_failure(
    job_id: str, job_status: str, patterns: list[str], lines: list[str], total: int,
    discounted: list[tuple[int, str]] | None = None,
    finished_at: str | None = None,
    runner_description: str | None = None,
    runner_id: "int | str | None" = None,
) -> None:







    tail_n = env_int(os.environ.get("GL_JOB_UNMATCHED_TAIL_LINES"), "GL_JOB_UNMATCHED_TAIL_LINES", 40, minimum=1)
    if discounted:
        print("\n" + BOILERPLATE_ONLY_HEADER)
    else:
        print("\n" + UNCLASSIFIED_HEADER)
    print(
        f"Job status is `{job_status}`: something did go wrong. supertool "
        "could not classify it, which means a pattern is missing here — "
        "not that the log is clean. Read the tail below before concluding "
        "anything."
    )
    shown = ", ".join(p.strip() for p in patterns if p.strip())
    if shown:
        print(f"Patterns tried: {shown} (+ built-in cause markers)")
    if discounted:
        head = (
            "The one line that matched is a line GitLab writes on every failed job"
            if len(discounted) == 1
            else f"All {len(discounted)} lines that matched are lines GitLab "
                 f"writes on every failed job"
        )
        print(
            f"\n{head} — no cause is named, so this is not a classification. "
            f"Shown, not hidden:"
        )
        print(_untrusted.open_marker())
        for line_num, text in discounted:
            print(f"  {line_num:>5} | {text}")
        print(_untrusted.close_marker())






        exit_code = _maintenance.exit_code_from_texts(
            text for _, text in discounted)
        note = _maintenance.maintenance_note(
            exit_code, finished_at, runner_description, runner_id)
        if note:
            print(f"\n{note}")
    section = _last_section(lines)
    if section:
        print(f"Last step entered: {section}")
    tail = lines[-tail_n:] if len(lines) > tail_n else lines
    print(f"\n## Log tail (last {len(tail)} lines of {total})")
    start = total - len(tail) + 1
    print(_untrusted.open_marker())
    for i, line in enumerate(tail):
        print(f"  {start + i:>5} | {line}")
    print(_untrusted.close_marker())
    print(
        f"\nNext:  {_st_hint.st_hint(f'gl-job:{job_id}:raw')}  or  "
        f"'gl-job:{job_id}:grep:PATTERN'  — the whole trace is still there."
    )


_PHPUNIT_BLOCK_START = re.compile(r'^\s*\d+\)\s+\S+::\S+')
_PHPUNIT_BLOCK_SUMMARY = re.compile(
    r'^\s*(FAILURES!|ERRORS!|WARNINGS!|OK \(|OK, but|There (was|were) \d+)'
)


def _phpunit_blocks(lines: list[str]) -> list[tuple[int, int]]:






    blocks: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if not _PHPUNIT_BLOCK_START.match(line):
            continue
        j = i + 1
        while (
            j < len(lines)
            and not _PHPUNIT_BLOCK_START.match(lines[j])
            and not _PHPUNIT_BLOCK_SUMMARY.match(lines[j])
        ):
            j += 1
        end = j - 1
        while end > i and not lines[end].strip():
            end -= 1
        blocks.append((i, end))
    return blocks


def _expand_phpunit_blocks(
    lines: list[str], matches: set[int], block_max: int, total_max: int
) -> tuple[int, int]:












    touched = [
        (start, end)
        for start, end in _phpunit_blocks(lines)
        if any(idx in matches for idx in range(start, end + 1))
    ]
    budget = total_max
    dropped = 0
    for start, end in touched:
        size = end - start + 1
        cost = min(size, block_max)
        if cost > budget:
            dropped += 1
            continue
        budget -= cost
        if size <= block_max:
            matches.update(range(start, end + 1))
            continue
        head = block_max // 2
        matches.update(range(start, start + head))
        matches.update(range(end - (block_max - head) + 1, end + 1))
    return dropped, len(touched)


def _log_lines(log: str) -> list[str]:


































    return [_untrusted.scrub(line)
            for line in _untrusted.split_lines(log)]


def gap_marker(n_lines: int) -> str:




















    unit = "line" if n_lines == 1 else "lines"
    return (f"... ({n_lines} {unit} elided by this op — no error pattern "
            f"matched them; the log itself is intact)")


def _pattern_anchors(lines: list[str], patterns: list[str]) -> list[int]:







    anchors: list[int] = []
    for i, line in enumerate(lines):
        for pattern in patterns:
            pattern = pattern.strip()
            if pattern and pattern in line:
                anchors.append(i)
                break
    return anchors


def _find_error_sections(lines: list[str], patterns: list[str], context: int,
                         trailing_gap: bool = False) -> list[tuple[int, str]]:














    matches: set[int] = set()
    for i in _pattern_anchors(lines, patterns):

        for j in range(max(0, i - context), min(len(lines), i + context + 1)):
            matches.add(j)





    cause_before = env_int(os.environ.get("GL_JOB_CAUSE_CONTEXT_BEFORE"), "GL_JOB_CAUSE_CONTEXT_BEFORE", 2, minimum=0)
    for i in _cause_lines(lines):
        for j in range(max(0, i - cause_before), min(len(lines), i + context + 1)):
            matches.add(j)

    if not matches:
        return []

    dropped, touched = _expand_phpunit_blocks(
        lines,
        matches,
        env_int(os.environ.get("GL_JOB_PHPUNIT_BLOCK_MAX_LINES"), "GL_JOB_PHPUNIT_BLOCK_MAX_LINES", 500, minimum=1),
        env_int(os.environ.get("GL_JOB_PHPUNIT_TOTAL_MAX_LINES"), "GL_JOB_PHPUNIT_TOTAL_MAX_LINES", 2000, minimum=1),
    )

    result: list[tuple[int, str]] = []
    sorted_matches = sorted(matches)
    prev = -1
    for idx in sorted_matches:
        gap = idx - prev - 1
        if gap > 0:
            result.append((-1, gap_marker(gap)))
        result.append((idx + 1, lines[idx]))  
        prev = idx




    trailing = len(lines) - 1 - prev
    if trailing_gap and trailing > 0:
        result.append((-1, gap_marker(trailing)))

    if dropped:
        plural = "" if dropped == 1 else "s"
        result.append((
            -1,
            f"... ({dropped} of {touched} PHPUnit failure{plural} not shown in full — "
            f"raise GL_JOB_PHPUNIT_TOTAL_MAX_LINES=N)",
        ))

    return result


def _emit_grep_hits(
    lines: list[str],
    hit_indexes: list[int],
    rx: "re.Pattern[str]",
    match_count: int,
    budget: int,
    knob: str,
    shown_pattern: str,
    ctx: int,
) -> None:

































    planned: list[str] = []
    emitted = 0
    shown_matches = 0





    prev = -1
    cut = False
    for idx in hit_indexes:
        chunk = (gap_marker(idx - prev - 1) + "\n") if idx > prev + 1 else ""
        chunk += f"  {idx + 1:>5} | {lines[idx]}\n"
        size = len(chunk.encode("utf-8", "replace"))



        if emitted and emitted + size > budget:
            cut = True
            break
        planned.append(chunk)
        emitted += size
        if rx.search(lines[idx]):
            shown_matches += 1
        prev = idx
    header = (f"\n## grep /{shown_pattern}/ — {match_count} matching lines "
              f"(±{ctx} context)")
    if cut:
        header += (f" [CAPPED: {shown_matches} shown, output limited to {budget} "
                   f"bytes by size — raise {knob}=N]")
    print(header)
    print(_untrusted.open_marker())
    sys.stdout.write("".join(planned))
    print(_untrusted.close_marker())
    if cut:
        print(
            f"... ({shown_matches} of {match_count} matching lines shown — output "
            f"capped at {budget} bytes by size, not by a match count limit; "
            f"raise {knob}=N or narrow the pattern)"
        )


def _human_size(num_bytes: int) -> str:






    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"





def _job_row(job_id: str) -> tuple[dict | None, str]:







    try:
        result = subprocess.run(
            ["glab", "api", _repo_target.gl_api_path(f"projects/:id/jobs/{job_id}")],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return None, "ERROR: glab not found — install the GitLab CLI"
    except subprocess.TimeoutExpired:
        return None, f"ERROR: glab timed out fetching job #{job_id}"
    if result.returncode != 0:
        return None, _format_error(result.stderr, "Job", job_id)
    try:
        return json.loads(result.stdout), ""
    except json.JSONDecodeError:
        return None, f"ERROR: glab returned unparseable JSON for job #{job_id}"


def _is_past(iso_ts: str) -> "bool | None":







    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < datetime.now(timezone.utc)


def print_artifacts(job_id: str) -> int:








    meta, error = _job_row(job_id)
    if error:
        print(error)
        return 1
    print(f"# Job #{job_id} artifacts")
    kinds = meta.get("artifacts")
    if not kinds:
        print("No artifacts recorded for this job.")
        return 0
    print(f"Kinds: {', '.join(str(k) for k in kinds)}")
    archive = meta.get("artifacts_file")
    if isinstance(archive, dict) and archive:
        name = _untrusted.flat(str(archive.get("filename") or "?"))
        size = archive.get("size")
        size_str = _human_size(int(size)) if isinstance(size, (int, float)) else "unknown size"
        print(f"Archive: {name} ({size_str})")
    expire_at = meta.get("artifacts_expire_at")
    if expire_at:
        expired = _is_past(str(expire_at))
        if expired is None:
            note = " (could not tell whether this has expired)"
        elif expired:
            note = " — EXPIRED"
        else:
            note = ""



        print(f"Expires: {_untrusted.flat(str(expire_at))}{note}")
    print(
        f"\nGitLab's job API does not list the paths inside the archive — "
        f"fetch a known path (e.g. one a trace printed) with: "
        f"gl-job:{job_id}:artifact:PATH"
    )
    return 0


def _fetch_artifact_bytes(job_id: str, path: str) -> tuple["bytes | None", str]:















    encoded = "/".join(urllib.parse.quote(seg, safe="") for seg in path.split("/"))
    url = _repo_target.gl_api_path(f"projects/:id/jobs/{job_id}/artifacts/{encoded}")
    refusal = path_refusal(url)
    if refusal:
        return None, refusal
    try:
        proc = subprocess.run(["glab", "api", url], capture_output=True, timeout=30)
    except FileNotFoundError:
        return None, "ERROR: glab not found — install the GitLab CLI"
    except subprocess.TimeoutExpired:
        return None, f"ERROR: glab timed out fetching artifact {path!r} for job #{job_id}"
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")
        flat_err = _untrusted.flat(stderr.strip())
        if _auth_probe.says_not_authenticated(stderr, _auth_probe.GITLAB_MARKERS):








            return None, (
                f"ERROR: GitLab refused to serve job #{job_id}'s artifacts "
                f"(not authenticated for the artifact endpoint, exit "
                f"{proc.returncode}): {flat_err}\n"
                f"This is a separate scope from the one that reads job "
                f"metadata — the same token that lists this job can still be "
                f"refused here. Check the token's scopes, or fetch the "
                f"archive through the GitLab UI instead."
            )
        if _status_probe.says_not_found(stderr):
            return None, (
                f"ERROR: no artifact at {path!r} in job #{job_id}'s archive "
                f"(exit {proc.returncode}): {flat_err}\n"
                f"Check the path — it is relative to the archive root and "
                f"case-sensitive. List what this job produced with "
                f"gl-job:{job_id}:artifacts."
            )
        return None, _format_error(flat_err, "Artifact", job_id)
    return proc.stdout, ""


def print_artifact(job_id: str, path: str) -> int:

    data, error = _fetch_artifact_bytes(job_id, path)
    if error:
        print(error)
        return 1




    cap = env_int(os.environ.get("GL_JOB_ARTIFACT_MAX_BYTES"), "GL_JOB_ARTIFACT_MAX_BYTES", 65536, minimum=1)
    if len(data) > cap:
        print(
            f"ERROR: {path!r} is {_human_size(len(data))}, over this op's "
            f"{_human_size(cap)} print cap "
            f"(GL_JOB_ARTIFACT_MAX_BYTES) — refusing rather than flooding "
            f"context with a file this op was not asked to dump whole."
        )
        return 1
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        print(
            f"ERROR: {path!r} ({_human_size(len(data))}) is not UTF-8 text — "
            f"this op prints text only. Binary artifacts are not readable "
            f"through this op yet (#1796)."
        )
        return 1
    print(f"# Job #{job_id} artifact: {_untrusted.flat(path, disclose_newline=True)}")
    print(f"{_human_size(len(data))}")
    print(_untrusted.banner())
    print(_untrusted.open_marker())
    for line in _untrusted.split_lines(text):
        print(_untrusted.scrub(line))
    print(_untrusted.close_marker())
    return 0


def _fetch_trace_and_meta(job_id: str) -> tuple[str, dict, str]:









    meta: dict = {"name": "?", "status": "?"}
    try:
        meta_result = subprocess.run(
            ["glab", "api",
             _repo_target.gl_api_path(f"projects/:id/jobs/{job_id}")],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
        )
        if meta_result.returncode == 0:
            meta = json.loads(meta_result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    try:
        result = subprocess.run(
            ["glab", "api",
             _repo_target.gl_api_path(f"projects/:id/jobs/{job_id}/trace")],
            capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return "", meta, "ERROR: glab not found — install the GitLab CLI"
    except subprocess.TimeoutExpired:
        return "", meta, f"ERROR: glab timed out fetching the trace for job #{job_id}"

    if result.returncode != 0:
        return "", meta, _format_error(_untrusted.flat(result.stderr), "Job log", job_id)

    log = result.stdout
    log = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', log)
    log = re.sub(r'\x1b\]8;[^;]*;[^\x1b]*\x1b\\', '', log)












    log = chr(10).join(_untrusted.scrub(line)
                        for line in _untrusted.split_lines(log))
    return log, meta, ""






_SUMMARY_FAILURES = re.compile(r'\bFailures:\s*(\d+)')
_SUMMARY_ERRORS = re.compile(r'\bErrors:\s*(\d+)')


def _summary_counts(text: str) -> tuple[int, int] | None:







    fail_hits = list(_SUMMARY_FAILURES.finditer(text))
    err_hits = list(_SUMMARY_ERRORS.finditer(text))
    if not fail_hits or not err_hits:
        return None
    return int(fail_hits[-1].group(1)), int(err_hits[-1].group(1))


def _first_phpunit_failure(text: str) -> str:















    for line in _log_lines(text):
        if _PHPUNIT_BLOCK_START.match(line):
            return line.strip()
    return ""











MAX_TRACE_IDS = env_int(os.environ.get("GL_JOB_TRACE_MAX_IDS"), "GL_JOB_TRACE_MAX_IDS", 6, minimum=1)


def write_traces(job_ids: list[str]) -> int:


























    root, why = _image_root.ensure(_image_root.default_root("-traces"))
    if root is None:
        print(f"ERROR: no trace directory this process owns could be established: {why}")
        return 1
    traces_dir, why = _image_root.ensure(os.path.join(root, "traces"))
    if traces_dir is None:
        print(f"ERROR: the traces directory could not be established: {why}")
        return 1



















    for job_id in job_ids:
        if not _digits.DIGITS.match(job_id):
            print(
                f"ERROR: job id {job_id!r} is not numeric -- nothing was "
                f"written. Trace writing takes GitLab job ids from a job "
                f"listing or fetch, and a non-numeric one means that data "
                f"was not what it should have been; refusing rather than "
                f"building a file path out of it."
            )
            return 1






    skipped_ids: list[str] = []
    if len(job_ids) > MAX_TRACE_IDS:
        skipped_ids = job_ids[MAX_TRACE_IDS:]
        job_ids = job_ids[:MAX_TRACE_IDS]
        print(
            f"[note] {len(skipped_ids)} of {len(skipped_ids) + len(job_ids)} "
            f"requested job(s) were not fetched -- this call is capped at "
            f"{MAX_TRACE_IDS} ids so the fetch stays inside the op's own "
            f"timeout budget (raise with GL_JOB_TRACE_MAX_IDS=N): "
            f"{', '.join(skipped_ids)}"
        )

    sections: list[str] = []
    written_ids: list[str] = []
    empty_ids: list[str] = []
    failed: list[tuple[str, str]] = []
    for job_id in job_ids:
        log, meta, error = _fetch_trace_and_meta(job_id)
        if error:
            failed.append((job_id, error))
            continue
        if not log.strip():
            empty_ids.append(job_id)
            continue
        name = _untrusted.flat(str(meta.get("name", "?")))
        status = meta.get("status", "?")
        header = f"===== job #{job_id} — {name} (status: {status}) =====\n"
        body = log if log.endswith("\n") else log + "\n"
        sections.append(header + body)
        written_ids.append(job_id)

    if not written_ids:
        print(f"## No trace written — {len(job_ids)} job(s) requested, "
              f"{len(empty_ids)} empty, {len(failed)} could not be fetched")
        for job_id, error in failed:
            print(f"  #{job_id}: {error}")
        for job_id in empty_ids:
            print(f"  #{job_id}: trace is empty (0 lines) — the job may not have run yet")
        return 1 if failed else 0

    content = "\n".join(sections) if len(sections) > 1 else sections[0]




    total_lines = content.count("\n")






    if len(written_ids) > 6:
        filename = f"job-{written_ids[0]}+{len(written_ids) - 1}more.log"
    else:
        filename = "job-" + "-".join(written_ids) + ".log"
    path = os.path.join(traces_dir, filename)

    existed_before = os.path.exists(path)
    previous_size = os.path.getsize(path) if existed_before else 0

    try:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
    except OSError as exc:
        print(f"ERROR: could not write {path}: {exc.strerror or exc}")
        return 1

    size = os.path.getsize(path)
    print(f"[trace] {total_lines} lines, {_human_size(size)} -> {path}")
    if existed_before:
        print(f"(overwrote existing file, previously {_human_size(previous_size)})")
    if failed:
        print(f"[note] {len(failed)} of {len(job_ids)} requested job(s) could not be fetched:")
        for job_id, error in failed:
            print(f"  #{job_id}: {error}")
    if empty_ids:
        shown = ", ".join(f"#{j}" for j in empty_ids)
        print(f"[note] {len(empty_ids)} of {len(job_ids)} requested job(s) had an empty trace: {shown}")

    counts = _summary_counts(content)
    if counts is not None:
        failures, errors = counts
        print(f"[failures] {failures} failures, {errors} errors")
    else:
        print("[summary] could not tell — no PHPUnit-style 'Failures: N, Errors: N' "
              "line found in the trace(s); read the file directly")

    first = _first_phpunit_failure(content)
    if first:






        print(f"[first] {_untrusted.flat(first)}")

    return 0


def main() -> int:
    use_utf8_stdout()
    if len(sys.argv) < 2:
        print("ERROR: usage: job.py JOB_ID [raw [START [END]]]")
        return 1




    job_id = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else ""


























    if mode == "trace":
        if os.environ.get("SUPERTOOL_TRACE_OP") != "true":
            print("ERROR: gl-job:ID:trace has moved to its own op with its "
                  "own timeout budget (#2146) — gl-job's declared timeout is "
                  "sized for a single job's fetch, not for a multi-id "
                  "trace fan-out. Nothing was read. Use: "
                  "gl-job-trace:JOB_ID[,JOB_ID...]")
            return 1
        if len(sys.argv) > 3:
            print("ERROR: gl-job-trace:ID takes no argument after the id(s) — "
                  "nothing was read. Usage: gl-job-trace:JOB_ID[,JOB_ID...]")
            return 1
        job_ids, refusal = _job_argv.refuse_job_ids("gl-job-trace", "GitLab", job_id)
        if refusal:
            print(refusal)
            return 1
        return write_traces(job_ids)

    refusal = _job_argv.refuse_job_id("gl-job", "GitLab", job_id)
    if refusal:
        print(refusal)
        return 1
    refusal = _job_argv.refuse_mode("gl-job", mode)
    if refusal:
        print(refusal)
        return 1




    if mode == "artifacts":
        if len(sys.argv) > 3:
            print("ERROR: gl-job:ID:artifacts takes no argument after "
                  "'artifacts' — nothing was read. Usage: "
                  "gl-job:JOB_ID:artifacts")
            return 1
        return print_artifacts(job_id)
    if mode == "artifact":
        path, note = _job_argv.artifact_path("gl-job", sys.argv[3:])
        if not path:
            print("ERROR: usage: gl-job:JOB_ID:artifact:PATH")
            return 1
        if note:
            print(note)
        return print_artifact(job_id, path)

    raw_mode = mode == "raw"
    errors_mode = mode in ("errors", "fail")
    grep_mode = mode == "grep"



    grep_pattern, grep_note = (
        _job_argv.grep_pattern("gl-job", sys.argv[3:]) if grep_mode else (None, "")
    )


    branch_actionable = not (raw_mode or errors_mode or grep_mode)
    if grep_mode and not grep_pattern:
        print("ERROR: usage: gl-job:JOB_ID:grep:PATTERN")
        return 1
    if grep_note:
        print(grep_note)
    raw_start: int | None = None
    raw_end: int | None = None
    raw_tail: int | None = None
    if raw_mode:
        try:
            if len(sys.argv) > 3 and sys.argv[3]:
                first = int(sys.argv[3])





                if first < 0:
                    raw_tail = -first
                else:
                    raw_start = max(1, first)
            if len(sys.argv) > 4 and sys.argv[4]:
                raw_end = int(sys.argv[4])
        except ValueError:
            print("ERROR: raw START/END must be integers")
            return 1
        if raw_start is not None and raw_end is not None and raw_end < raw_start:



            print(f"ERROR: raw END ({raw_end}) is before START ({raw_start}); "
                  f"ranges are 1-indexed and inclusive")
            return 1
        if raw_tail is not None and raw_end is not None:
            print("ERROR: the raw tail form takes no END — "
                  f"use raw:-{raw_tail} for the last {raw_tail} lines, "
                  "or raw:START:END for an absolute range")
            return 1
    config = _get_config()
    tail_lines = config["lines"]


    try:
        meta_result = subprocess.run(
            ["glab", "api",
             _repo_target.gl_api_path(f"projects/:id/jobs/{job_id}")],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("ERROR: glab not found — install the GitLab CLI")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: glab timed out (metadata)")
        return 1

    job_name = "?"
    job_status = "?"
    job_stage = "?"
    job_duration = None
    web_url = ""
    ref = ""
    pipeline_id = ""
    finished_at = None
    runner_description = None
    runner_id = None
    if meta_result.returncode == 0:
        try:
            meta = json.loads(meta_result.stdout)
            job_name = meta.get("name", "?")
            job_status = meta.get("status", "?")
            job_stage = meta.get("stage", "?")
            job_duration = meta.get("duration")
            web_url = meta.get("web_url", "")
            ref = meta.get("ref", "")
            pipeline_id = str((meta.get("pipeline") or {}).get("id", ""))


            finished_at = meta.get("finished_at")
            runner_meta = meta.get("runner")
            if isinstance(runner_meta, dict):
                runner_description = runner_meta.get("description")
                runner_id = runner_meta.get("id")
        except json.JSONDecodeError:
            pass


    try:
        result = subprocess.run(
            ["glab", "api",
             _repo_target.gl_api_path(f"projects/:id/jobs/{job_id}/trace")],
            capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        print("ERROR: glab timed out (trace)")
        return 1

    if result.returncode != 0:
        print(_format_error(result.stderr, "Job log", job_id))
        return 1


    log = result.stdout
    log = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', log)
    log = re.sub(r'\x1b\]8;[^;]*;[^\x1b]*\x1b\\', '', log)

    lines = _log_lines(log)
    total = len(lines)


    duration_str = f"{job_duration:.0f}s" if job_duration else "?"
    print(f"# Job #{job_id} — {_untrusted.flat(job_name)}")
    print(f"Stage: {_untrusted.flat(job_stage)} | Status: {job_status} | "
          f"Duration: {duration_str}")


    if ref:
        mr_match = re.match(r'refs/merge-requests/(\d+)/head', ref)
        if mr_match:
            mr_iid = mr_match.group(1)
            mr_data = {}
            try:
                mr_result = subprocess.run(
                    ["glab", "api", _repo_target.gl_api_path(
                        f"projects/:id/merge_requests/{mr_iid}")],
                    capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
                )
                if mr_result.returncode == 0:
                    mr_data = json.loads(mr_result.stdout)
            except (subprocess.TimeoutExpired, json.JSONDecodeError):
                pass

            mr_title = mr_data.get("title", "")
            mr_branch = mr_data.get("source_branch", "")
            mr_target = mr_data.get("target_branch", "")
            mr_author = (mr_data.get("author") or {}).get("username", "")
            mr_labels = ", ".join(mr_data.get("labels", [])) or ""
            mr_state = mr_data.get("state", "")
            diff_stats = mr_data.get("diff_stats") or {}
            mr_changes = mr_data.get("changes_count", "?")
            mr_additions = diff_stats.get("additions", "?")
            mr_deletions = diff_stats.get("deletions", "?")


            mr_desc = mr_data.get("description") or ""
            issue_match = re.search(r'#(\d{4,})', mr_desc)
            issue_ref = f"#{issue_match.group(1)}" if issue_match else ""

            print(f"\n## MR !{mr_iid} — {_untrusted.flat(mr_title)}")
            print(f"State: {mr_state} | Author: {_untrusted.flat(mr_author)}")



            print(f"Branch: {_untrusted.flat(mr_branch)} -> "
                  f"{_untrusted.flat(mr_target)}")
            local_check = _local_branch_check(mr_branch, branch_actionable)
            if local_check:
                print(local_check)
            if mr_labels:
                print(f"Labels: {_untrusted.flat(mr_labels)}")
            print(f"Changes: {mr_changes} files, +{mr_additions} -{mr_deletions}")
            if issue_ref:
                print(f"Issue: {issue_ref}")
            print(f"Pipeline: #{pipeline_id}")
        else:
            print(f"Branch: {_untrusted.flat(ref)} | Pipeline: #{pipeline_id}")
            local_check = _local_branch_check(ref, branch_actionable)
            if local_check:
                print(local_check)

    if web_url:
        print(f"URL: {web_url}")
    print(f"Log: {total} lines total")










    if total > 0:
        print(_untrusted.banner())


    if raw_mode:
        if total == 0:



            print("\n## Raw — the log is empty (0 lines)")
            return 0
        if raw_tail is not None:
            width = min(raw_tail, total)
            start, end = total - width + 1, total
        else:
            start = raw_start if raw_start is not None else 1
            end = raw_end if raw_end is not None else total
            if start > total:






                width = (end - start + 1) if raw_end is not None else tail_lines
                width = max(1, min(width, total))
                print(f"\n## Raw — requested {start}-{end} is past end of log "
                      f"({total} lines); showing the last {width} lines instead")
                start, end = total - width + 1, total
            end = min(end, total)




        cap = env_int(os.environ.get("GL_JOB_RAW_MAX_LINES"), "GL_JOB_RAW_MAX_LINES", 5000, minimum=1)
        shown = lines[start - 1:end]
        if len(shown) > cap:
            kept = shown[:cap]
            hint = (
                "narrow the slice or raise GL_JOB_RAW_MAX_LINES=N"
                if raw_end is not None
                else "pass START:END to slice further, or raise GL_JOB_RAW_MAX_LINES=N"
            )
            print(
                f"\n## Raw lines {start}-{start + cap - 1} of {total} "
                f"[CAPPED at {cap} — {hint}]"
            )
            print(_untrusted.open_marker())
            for i, line in enumerate(kept):
                print(f"  {start + i:>5} | {line}")
            print(_untrusted.close_marker())
            return 0
        print(f"\n## Raw lines {start}-{start + len(shown) - 1} of {total}")
        print(_untrusted.open_marker())
        for i, line in enumerate(shown):
            print(f"  {start + i:>5} | {line}")
        print(_untrusted.close_marker())
        return 0




    if grep_mode and grep_pattern is not None:
        try:
            rx = re.compile(grep_pattern)
            shown_pattern = grep_pattern
        except re.error:
            rx = re.compile(re.escape(grep_pattern))
            shown_pattern = f"{grep_pattern} (literal match)"
        ctx = config["error_context"]
        hits: set[int] = set()
        for i, line in enumerate(lines):
            if rx.search(line):
                for j in range(max(0, i - ctx), min(len(lines), i + ctx + 1)):
                    hits.add(j)
        if not hits:
            print(f"\n## No lines match /{shown_pattern}/ (searched {total} lines)")
            tail = lines[-tail_lines:] if len(lines) > tail_lines else lines
            print(f"Showing last {len(tail)} lines as fallback:")
            start = total - len(tail) + 1
            print(_untrusted.open_marker())
            for i, line in enumerate(tail):
                print(f"  {start + i:>5} | {line}")
            print(_untrusted.close_marker())
            return 0
        match_count = sum(1 for line in lines if rx.search(line))
        _emit_grep_hits(lines, sorted(hits), rx, match_count,
                        env_int(os.environ.get("GL_JOB_GREP_MAX_BYTES"), "GL_JOB_GREP_MAX_BYTES", 65536, minimum=1),
                        "GL_JOB_GREP_MAX_BYTES", shown_pattern, ctx)
        return 0



    patterns, resolution = _select_job_patterns(
        job_name, config["job_patterns"], config["error_patterns"]
    )
    resolution_line = (
        f"Resolve:  {_st_hint.st_hint(resolution.replace('{id}', job_id))}"
        if resolution else ""
    )
    error_sections = _find_error_sections(lines, patterns,
                                          config["error_context"],
                                          trailing_gap=errors_mode)





    anchors = sorted(set(_pattern_anchors(lines, patterns)) | set(_cause_lines(lines)))
    boilerplate_only = bool(anchors) and all(_is_boilerplate(lines[i]) for i in anchors)
    discounted = [(i + 1, lines[i]) for i in anchors] if boilerplate_only else None


    if errors_mode:
        mismatch = _selection_mismatch(job_status, job_id)
        if not error_sections or (job_status == "failed" and boilerplate_only):
            if job_status == "failed":
                _print_unmatched_failure(job_id, job_status, patterns, lines,
                                         total, discounted,
                                         finished_at, runner_description,
                                         runner_id)
            else:
                print("\n## No error patterns matched")
                if mismatch:
                    print(mismatch)
            return 0
        matched_count = len([e for e in error_sections if e[0] > 0])
        if mismatch:





            print(f"\n## Error blocks ({matched_count} lines matched) — but see below")
        else:
            print(f"\n## All error blocks ({matched_count} lines matched, no tail truncation)")
        print(_untrusted.open_marker())
        for line_num, text in error_sections:
            if line_num == -1:
                print(text)
            else:
                print(f"  {line_num:>5} | {text}")
        print(_untrusted.close_marker())
        if mismatch:
            print(mismatch)
        if resolution_line:
            print(f"\n{resolution_line}")
        return 0

    if error_sections and job_status == "failed" and not boilerplate_only:
        print(f"\n## Error context ({len([e for e in error_sections if e[0] > 0])} lines matched)")
        print(_untrusted.open_marker())
        for line_num, text in error_sections:
            if line_num == -1:
                print(text)  
            else:
                print(f"  {line_num:>5} | {text}")
        print(_untrusted.close_marker())

        if resolution_line:
            print(f"\n{resolution_line}")


        print(f"\n## Tail (last {tail_lines} lines)")
        shown = lines[-tail_lines:] if len(lines) > tail_lines else lines
        start = total - len(shown) + 1
        print(_untrusted.open_marker())
        for i, line in enumerate(shown):
            print(f"  {start + i:>5} | {line}")
        print(_untrusted.close_marker())
    elif job_status == "failed":




        _print_unmatched_failure(job_id, job_status, patterns, lines, total,
                                 discounted, finished_at, runner_description,
                                 runner_id)
    else:

        shown = lines[-tail_lines:] if len(lines) > tail_lines else lines
        skipped = total - len(shown)
        if skipped > 0:
            print(f"({skipped} lines skipped)")
        print()
        start = total - len(shown) + 1
        print(_untrusted.open_marker())
        for i, line in enumerate(shown):
            print(f"  {start + i:>5} | {line}")
        print(_untrusted.close_marker())

    return 0


if __name__ == "__main__":
    sys.exit(main())
