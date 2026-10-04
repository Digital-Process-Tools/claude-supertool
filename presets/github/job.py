#!/usr/bin/env python3











from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  
import _repo_target  
import _branch_locale  
import _untrusted  
import _auth_probe  
import _status_probe  
import _job_argv  
from _env import env_int  
from _st_hint import st_hint  


def _api_repo_path(suffix: str) -> str:







    return _repo_target.api_path(suffix)


def _printable_api_repo_path(suffix: str) -> str:







    return _repo_target.api_path_printable(suffix)


def _local_branch_check(source: str) -> str:






    return _branch_locale.check(source)


def _gh_error_kind(stderr: str) -> str:


    s = stderr.lower()
    if "github host" in s or "not a git repository" in s or "git remotes" in s:
        return "repo"
    if _status_probe.says_not_found(s):
        return "notfound"








    if _auth_probe.says_not_authenticated(s):
        return "auth"
    if "rate limit" in s or "429" in s:
        return "ratelimit"
    if _status_probe.says_forbidden(s):
        return "forbidden"





    if "unknown flag" in s and "allow-escape-sequences" in s:
        return "old_gh_no_escape_flag"
    return "other"


def _format_error(stderr: str, resource: str, identifier: str) -> str:

    kind = _gh_error_kind(stderr)
    if kind == "repo":
        return _repo_target.no_repo_error("gh-job:12345:fail")
    if kind == "notfound":
        return f"ERROR: {resource} #{identifier} not found. Check the ID. Use gh-run to list jobs first, then gh-job with the job ID."
    if kind == "auth":
        return f"ERROR: gh CLI not authenticated. Run: gh auth login (verify with: gh auth status)"
    if kind == "ratelimit":
        return "ERROR: GitHub API rate limit exceeded. Wait a few minutes and retry."
    if kind == "forbidden":
        return f"ERROR: permission denied for {resource} #{identifier}. Check repo access (gh auth status)."
    if kind == "old_gh_no_escape_flag":
        return (f"ERROR: this gh CLI does not support --allow-escape-sequences, "
                f"so {resource} #{identifier} cannot be read (its log contains "
                f"terminal escape sequences). Upgrade gh "
                f"and retry.")

    return (f"ERROR: gh failed for {resource} #{identifier}: "
            f"{_untrusted.flat(stderr.strip())}")


def _probe_check_run(job_id: str) -> tuple[str, str, str, dict | None]:




























    try:
        r = subprocess.run(
            ["gh", "api", _api_repo_path(f"check-runs/{job_id}")],
            capture_output=True, text=True, timeout=10, encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return ("unknown", "", f"{type(exc).__name__}: {exc}", None)
    if r.returncode != 0:
        if _gh_error_kind(r.stderr) == "notfound":
            return ("absent", "", "", None)
        return ("unknown", "", r.stderr.strip() or f"gh exited {r.returncode}", None)
    try:
        data = json.loads(r.stdout or "null")
    except (json.JSONDecodeError, ValueError) as exc:
        return ("unknown", "", f"checks API returned unparseable JSON: {exc}", None)
    if not isinstance(data, dict):
        return ("unknown", "", "checks API returned a non-object body", None)
    return ("found", str(data.get("name") or "?"), "", data)


def _load_check_renderer():











    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check.py")
    try:
        spec = importlib.util.spec_from_file_location("_gh_check_render", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except (OSError, ImportError, SyntaxError, ValueError):
        return None
    return mod if hasattr(mod, "render_check") else None


def _log_mode_note(mode: str) -> str:






    if not mode:
        return ""
    return (
        f"Note: `{mode}` slices a job log and this id is a check run, which "
        f"has no log — that mode does not apply here and was not applied. "
        f"What this check reported is in Output and Annotations below."
    )


def _render_routed_check(job_id: str, check: dict, mode: str) -> "int | None":






    mod = _load_check_renderer()
    if mod is None:
        return None
    routed = (
        f"Routed: you called `gh-job:{job_id}`. That id is not an Actions job, "
        f"so this op queried the checks API instead — the same render as "
        f"`gh-check:{job_id}`."
    )
    return mod.render_check(job_id, check, routed_from=routed,
                            mode_note=_log_mode_note(mode))


def _absent_job_message(job_id: str, check: tuple[str, str, str, object]) -> str:








    state, name, error, _data = check
    if state == "found":





        return (
            f"ERROR: No Actions job #{job_id} — but a **check run** with that "
            f"id does exist ({name}). CodeQL, Dependabot and any other GitHub "
            f"App report through the checks API, which is a separate id space "
            f"from Actions jobs, and this op reads only the Actions one. The "
            f"id is right; the op is not. Read it — including the annotations "
            f"where a scanning check keeps its finding — with: "
            f"./supertool 'gh-check:{job_id}'"
        )
    if state == "absent":
        return (
            f"ERROR: Job #{job_id} not found — the Actions job endpoint and "
            f"the checks API both returned 404 for this ID, so it names "
            f"neither an Actions job nor a check run in this repo. Check the "
            f"ID. Use gh-run to list jobs first, then gh-job with the job ID; "
            f"for a check like CodeQL use ./supertool 'gh-check:pr:NUMBER'."
        )
    return (
        f"ERROR: No Actions job #{job_id}, and whether it is a **check run** "
        f"instead is UNKNOWN — the checks API did not answer: {error}. "
        f"A wrong id and a CodeQL/Dependabot-style check run are both still "
        f"possible; this op is not guessing between them. Retry, or read the "
        f"other namespace directly with: ./supertool 'gh-check:{job_id}'"
    )


def _missing_log_message(
    job_id: str, meta: dict | None, meta_absent: bool, meta_error: str,
    probe: "tuple[str, str, str, object] | None" = None,
) -> str:






























    if meta is None:
        if meta_absent:


            return _absent_job_message(
                job_id, probe if probe is not None else _probe_check_run(job_id))
        state_path = _printable_api_repo_path("actions/jobs/" + str(job_id))
        return (
            f"ERROR: Job #{job_id} has no log (HTTP 404), and supertool "
            f"could not tell why — the job endpoint did not answer: "
            f"{meta_error}. A wrong ID, a job still running, and a log that "
            f"was never written or has since expired are all still possible; "
            f"this op is not guessing between them. Read the job state "
            f"directly with: gh api {state_path}"
        )
    name = meta.get("name") or "?"
    status = meta.get("status") or "?"
    conclusion = meta.get("conclusion") or ""
    completed_at = meta.get("completed_at") or "?"
    label = f"Job #{job_id} ({name})"
    if status != "completed":
        return (
            f"ERROR: {label} has no log — its status is `{status}`, so the "
            f"log is not written yet. GitHub writes a job's log when the job "
            f"completes; the ID is correct and there is nothing to fix. "
            f"Retry once it finishes: ./supertool 'gh-job:{job_id}'"
        )
    if conclusion in ("cancelled", "skipped"):
        return (
            f"ERROR: {label} has no log — the job was `{conclusion}` "
            f"(completed_at {completed_at}). GitHub only writes a log for a "
            f"job that ran to completion, so no log was ever written for this "
            f"one and none ever will be. Stop waiting — the ID is correct. "
            f"Sibling jobs on the same run may still have logs."
        )
    return (
        f"ERROR: {label} completed `{conclusion}` at {completed_at}, but its "
        f"log is unavailable — expired or purged (GitHub keeps job logs for a "
        f"limited retention window). The ID is correct; the log is gone, not "
        f"missing from your query."
    )


def _get_config() -> dict:

    return {
        "lines": env_int(os.environ.get("SUPERTOOL_LINES"), "SUPERTOOL_LINES", 80, minimum=1),
        "error_patterns": os.environ.get(
            "SUPERTOOL_ERROR_PATTERNS", "ERROR,FAILED,Error:,Failed,fatal:,##[error]"
        ).split(","),
        "error_context": env_int(os.environ.get("SUPERTOOL_ERROR_CONTEXT"), "SUPERTOOL_ERROR_CONTEXT", 5, minimum=0),
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






















UNCLASSIFIED_HEADER = "## FAILED — supertool could not classify this job"


def _print_unmatched_failure(
    job_id: str, status_label: str, patterns: list[str], lines: list[str], total: int
) -> None:














    tail_n = env_int(os.environ.get("GH_JOB_UNMATCHED_TAIL_LINES"), "GH_JOB_UNMATCHED_TAIL_LINES", 40, minimum=1)
    print("\n" + UNCLASSIFIED_HEADER)
    print(
        f"Job status is `{status_label}`: something did go wrong. supertool "
        "could not classify it, which means a pattern is missing here — "
        "not that the log is clean. Read the tail below before concluding "
        "anything."
    )
    shown = ", ".join(p.strip() for p in patterns if p.strip())
    if shown:
        print(f"Patterns tried: {shown}")
    tail = lines[-tail_n:] if len(lines) > tail_n else lines
    print(f"\n## Log tail (last {len(tail)} lines of {total})")
    start = total - len(tail) + 1
    print(_untrusted.open_marker())
    for i, line in enumerate(tail):
        print(f"  {start + i:>5} | {line}")
    print(_untrusted.close_marker())
    print(
        f"\nNext:  ./supertool 'gh-job:{job_id}:raw'  or  "
        f"'gh-job:{job_id}:grep:PATTERN'  — the whole trace is still there."
    )






_FAIL_SELECTOR_FITS = "failure"


def _selection_mismatch(display_status: str, job_id: str) -> str:





















    if display_status == _FAIL_SELECTOR_FITS:
        return ""
    return (
        f"\n> NOTE: this job's conclusion is `{display_status}`, not `failure`, so "
        f"error-block selection is a poor fit — it can only find lines an error "
        f"pattern marks, and a job that produced no failure puts its diagnostics "
        f"outside them (teardown, orphan processes, the tail). Treat the above as "
        f"the lines that MATCHED, not as what the log contains.\n"
        f"> Read it instead with:\n"
        f">   ./supertool 'gh-job:{job_id}:raw:-80'          # tail, where a "
        f"cancellation's evidence usually sits\n"
        f">   ./supertool 'gh-job:{job_id}:grep:orphan'      # processes alive at "
        f"teardown = a hang, not a slow suite"
    )




















_SUITE_SUMMARY_RE = re.compile(  
    r"^(?:=+[ \t]*)?"
    r"(?P<counts>\d+\s+[A-Za-z]+(?:\s*,\s*\d+\s+[A-Za-z]+)*)"
    r"\s+in\s+[0-9.]+s"
    r"(?:\s*\([^)]*\))?"
    r"[ \t]*=*$"
)









_SUITE_OUTCOMES = (
    "passed", "failed", "error", "errors", "skipped",
    "xfailed", "xpassed", "deselected",
)



_SUITE_BAD = ("failed", "error", "errors")


def _suite_summaries(lines: list[str]) -> list[str]:

    found: list[str] = []
    for line in lines:
        m = _SUITE_SUMMARY_RE.match(line.rstrip())
        if not m:
            continue
        body = m.group("counts")
        low = body.lower()
        if any(word in low for word in _SUITE_OUTCOMES):
            found.append(body)
    return found


def _suite_summary(lines: list[str]) -> str | None:




























    found = _suite_summaries(lines)
    if not found:
        return None
    for body in reversed(found):
        if _suite_bad_count(body):
            return body
    return found[-1]


def _suite_bad_count(summary: str) -> int:







    total = 0
    for count, word in re.findall(r"(\d+)\s+([A-Za-z]+)", summary):
        if word.lower() in _SUITE_BAD:
            total += int(count)
    return total







_CONCLUSIONS_THAT_ANSWER = ("success", "failure")


def suite_line(summary: str, n_summaries: int, job_conclusion: str) -> str:
































    bad = _suite_bad_count(summary)
    conclusion = (job_conclusion or "").strip().lower()
    several = (f" ({n_summaries} pytest summaries in this log — this is the "
               f"last one reporting a failure; `:grep:` for the rest)"
               if n_summaries > 1 else "")
    conflict = ""
    if conclusion in _CONCLUSIONS_THAT_ANSWER and bool(bad) != (
            conclusion == "failure"):
        conflict = (f" The Actions API reports this job concluded "
                    f"`{conclusion}`, which this line does not agree with — the "
                    f"conclusion is not written by the log, so these are two "
                    f"sources and one of them is wrong.")
    return (f"Suite: {_untrusted.flat(summary)} — read out of the job log. The "
            f"log is written by the code the job ran, which on a pull request "
            f"is the pull request's own code, so this is what that log claims "
            f"and not a count supertool made. These count TESTS; a check tally "
            f"counts LEGS.{several}{conflict}")


def _log_lines(log: str) -> list[str]:






























    return [_untrusted.scrub(line)
            for line in _untrusted.split_lines(log)]


def gap_marker(n_lines: int) -> str:























    unit = "line" if n_lines == 1 else "lines"
    return (f"... ({n_lines} {unit} elided by this op — no error pattern "
            f"matched them; the log itself is intact)")


def _find_error_sections(lines: list[str], patterns: list[str], context: int,
                         trailing_gap: bool = False) -> list[tuple[int, str]]:














    matches: set[int] = set()
    for i, line in enumerate(lines):
        for pattern in patterns:
            pattern = pattern.strip()
            if not pattern:
                continue
            if pattern in line:
                for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                    matches.add(j)
                break

    if not matches:
        return []

    result: list[tuple[int, str]] = []
    sorted_matches = sorted(matches)






    prev = -1
    for idx in sorted_matches:
        if idx > prev + 1:
            result.append((-1, gap_marker(idx - prev - 1)))
        result.append((idx + 1, lines[idx]))
        prev = idx





    trailing = len(lines) - 1 - prev
    if trailing_gap and trailing > 0:
        result.append((-1, gap_marker(trailing)))

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


def _job_run_id(job_id: str) -> tuple[str, str]:






    try:
        result = subprocess.run(
            ["gh", "api", _api_repo_path(f"actions/jobs/{job_id}")],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return "", "ERROR: gh not found — install the GitHub CLI"
    except subprocess.TimeoutExpired:
        return "", f"ERROR: gh timed out fetching job #{job_id}"
    if result.returncode != 0:
        if _gh_error_kind(result.stderr) == "notfound":










            probe = _probe_check_run(job_id)
            if probe[0] == "found":
                name = _untrusted.flat(probe[1])
                return "", (
                    f"ERROR: #{job_id} is not an Actions job — it is a "
                    f"check run ({name}), which has no artifacts of its "
                    f"own. Read it with: " + st_hint(f"gh-check:{job_id}")
                )
            if probe[0] == "unknown":
                return "", (
                    f"ERROR: Job #{job_id} has no artifacts endpoint (404), "
                    f"and whether it is a check run instead is UNKNOWN — "
                    f"the checks API did not answer: {probe[2]}. Retry, or "
                    f"read the other namespace directly with: "
                    + st_hint(f"gh-check:{job_id}")
                )
        return "", _format_error(result.stderr, "Job", job_id)
    try:
        meta = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "", f"ERROR: gh returned unparseable JSON for job #{job_id}"
    run_id = str(meta.get("run_id") or "")
    if not run_id:
        return "", f"ERROR: job #{job_id}'s metadata has no run_id — cannot look up its artifacts"
    return run_id, ""


def _run_artifacts(run_id: str) -> tuple["list[dict] | None", str]:







    try:
        result = subprocess.run(
            ["gh", "api", _api_repo_path(f"actions/runs/{run_id}/artifacts?per_page=100")],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return None, "ERROR: gh not found — install the GitHub CLI"
    except subprocess.TimeoutExpired:
        return None, f"ERROR: gh timed out listing artifacts for run #{run_id}"
    if result.returncode != 0:
        return None, _format_error(result.stderr, "Run artifacts", run_id)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, f"ERROR: gh returned unparseable JSON for run #{run_id}'s artifacts"
    artifacts = data.get("artifacts") if isinstance(data, dict) else None
    if not isinstance(artifacts, list):
        return None, f"ERROR: gh returned no artifact list for run #{run_id}"
    return artifacts, ""


def print_artifacts(job_id: str) -> int:







    run_id, error = _job_run_id(job_id)
    if error:
        print(error)
        return 1
    artifacts, error = _run_artifacts(run_id)
    if error:
        print(error)
        return 1
    print(f"# Job #{job_id} artifacts (run #{run_id})")
    if not artifacts:
        print("No artifacts recorded for this run.")
        return 0
    for artifact in artifacts:
        name = _untrusted.flat(str(artifact.get("name") or "?"))
        size = artifact.get("size_in_bytes")
        size_str = (_human_size(int(size)) if isinstance(size, (int, float))
                    else "unknown size")
        expired = artifact.get("expired")
        expires_at = artifact.get("expires_at")
        note = " — EXPIRED" if expired else ""



        expires_note = (
            f", expires {_untrusted.flat(str(expires_at))}"
            if expires_at and not expired else ""
        )
        print(f"- {name} ({size_str}){note}{expires_note}")
    prefix_note = (
        "" if len(artifacts) == 1 else
        " — this run produced more than one, so prefix PATH with the "
        "artifact's own name: NAME/path-inside-the-zip"
    )
    print(
        f"\nGitHub's API does not list the paths inside a zip -- fetch a "
        f"known path (e.g. one a log printed) with: "
        f"gh-job:{job_id}:artifact:PATH{prefix_note}"
    )
    return 0


def _resolve_artifact_entry(
    artifacts: list, path: str,
) -> tuple["dict | None", str, str]:







    if not artifacts:
        return None, "", "ERROR: this run produced no artifacts"
    if len(artifacts) == 1:
        return artifacts[0], path, ""







    candidates = sorted(artifacts, key=lambda a: -len(str(a.get("name") or "")))
    exact_name_match = None
    for artifact in candidates:
        name = str(artifact.get("name") or "")
        if path == name:
            exact_name_match = artifact
        if path.startswith(name + "/"):
            return artifact, path[len(name) + 1:], ""
    if exact_name_match is not None:
        return None, "", (
            f"ERROR: {path!r} is an artifact's name, not a file inside it. "
            f"This run produced more than one artifact, so PATH must be "
            f"NAME/path-inside-the-zip."
        )




    names = ", ".join(_untrusted.flat(str(a.get("name"))) for a in artifacts)
    return None, "", (
        f"ERROR: {path!r} does not start with any artifact's own name "
        f"among this run's artifacts ({names}). This run produced more "
        f"than one, so PATH must be NAME/path-inside-the-zip."
    )


def _fetch_artifact_zip(artifact_id: object) -> tuple["bytes | None", str]:


    try:
        proc = subprocess.run(
            ["gh", "api", _api_repo_path(f"actions/artifacts/{artifact_id}/zip")],
            capture_output=True, timeout=60,
        )
    except FileNotFoundError:
        return None, "ERROR: gh not found — install the GitHub CLI"
    except subprocess.TimeoutExpired:
        return None, f"ERROR: gh timed out downloading artifact #{artifact_id}"
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")
        if _gh_error_kind(stderr) == "notfound":
            return None, (
                f"ERROR: artifact #{artifact_id} not found — it may have "
                f"expired or been deleted."
            )
        if _auth_probe.says_not_authenticated(stderr.lower()):
            return None, (
                f"ERROR: gh could not download artifact #{artifact_id} (not "
                f"authenticated for the artifact endpoint): "
                f"{_untrusted.flat(stderr.strip())}"
            )
        return None, _format_error(stderr, "Artifact", str(artifact_id))
    return proc.stdout, ""


def print_artifact(job_id: str, path: str) -> int:

    run_id, error = _job_run_id(job_id)
    if error:
        print(error)
        return 1
    artifacts, error = _run_artifacts(run_id)
    if error:
        print(error)
        return 1
    artifact, entry_path, error = _resolve_artifact_entry(artifacts, path)
    if error:
        print(error)
        return 1








    archive_size = artifact.get("size_in_bytes")
    download_cap = env_int(os.environ.get("GH_JOB_ARTIFACT_DOWNLOAD_MAX_BYTES"), "GH_JOB_ARTIFACT_DOWNLOAD_MAX_BYTES", 200 * 1024 * 1024,
                           minimum=1)
    if isinstance(archive_size, (int, float)):
        if archive_size > download_cap:
            print(
                f"ERROR: artifact {_untrusted.flat(str(artifact.get('name')))!r} "
                f"is {_human_size(int(archive_size))}, over this op's "
                f"{_human_size(download_cap)} download cap "
                f"(GH_JOB_ARTIFACT_DOWNLOAD_MAX_BYTES) — GitHub has no single-file "
                f"artifact endpoint, so reading one entry means downloading the "
                f"whole archive first, and this one is refused before that "
                f"download rather than after. Raise the cap, or fetch the "
                f"archive another way."
            )
            return 1
    else:







        print(
            f"NOTE: artifact {_untrusted.flat(str(artifact.get('name')))!r} has "
            f"no usable size_in_bytes from GitHub — this op's "
            f"{_human_size(download_cap)} download cap "
            f"(GH_JOB_ARTIFACT_DOWNLOAD_MAX_BYTES) could not be checked before "
            f"the whole archive downloads."
        )
    zip_bytes, error = _fetch_artifact_zip(artifact.get("id"))
    if error:
        print(error)
        return 1
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        print(f"ERROR: gh returned something that is not a zip for artifact "
              f"#{artifact.get('id')}")
        return 1
    try:
        info = zf.getinfo(entry_path)
    except KeyError:




        names = [_untrusted.flat(n) for n in zf.namelist()]
        shown = ", ".join(names[:20])
        more = "" if len(names) <= 20 else f" (+{len(names) - 20} more)"
        print(
            f"ERROR: no file at {entry_path!r} inside artifact "
            f"{_untrusted.flat(str(artifact.get('name')))!r} — check the "
            f"path (case-sensitive, relative to the archive root). "
            f"Contents: {shown}{more}"
        )
        return 1




    cap = env_int(os.environ.get("GH_JOB_ARTIFACT_MAX_BYTES"), "GH_JOB_ARTIFACT_MAX_BYTES", 65536, minimum=1)
    if info.file_size > cap:
        print(
            f"ERROR: {entry_path!r} is {_human_size(info.file_size)}, over "
            f"this op's {_human_size(cap)} print cap "
            f"(GH_JOB_ARTIFACT_MAX_BYTES) — refusing rather than flooding "
            f"context with a file this op was not asked to dump whole."
        )
        return 1
    data = zf.read(entry_path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        print(
            f"ERROR: {entry_path!r} ({_human_size(len(data))}) is not UTF-8 "
            f"text — this op prints text only. Binary artifacts are not "
            f"readable through this op yet (#1796)."
        )
        return 1
    print(f"# Job #{job_id} artifact: {_untrusted.flat(entry_path, disclose_newline=True)}")
    print(f"{_human_size(len(data))}")
    print(_untrusted.banner())
    print(_untrusted.open_marker())
    for line in _untrusted.split_lines(text):
        print(_untrusted.scrub(line))
    print(_untrusted.close_marker())
    return 0


def main() -> int:
    use_utf8_stdout()
    if len(sys.argv) < 2:
        print("ERROR: usage: job.py JOB_ID [raw [START [END]]]")
        return 1




    job_id = sys.argv[1]
    refusal = _job_argv.refuse_job_id("gh-job", "GitHub", job_id)
    if refusal:
        print(refusal)
        return 1
    mode = sys.argv[2] if len(sys.argv) > 2 else ""
    refusal = _job_argv.refuse_mode("gh-job", mode)
    if refusal:
        print(refusal)
        return 1




    if mode == "artifacts":
        if len(sys.argv) > 3:
            print("ERROR: gh-job:ID:artifacts takes no argument after "
                  "'artifacts' — nothing was read. Usage: "
                  "gh-job:JOB_ID:artifacts")
            return 1
        return print_artifacts(job_id)
    if mode == "artifact":
        path, note = _job_argv.artifact_path("gh-job", sys.argv[3:])
        if not path:
            print("ERROR: usage: gh-job:JOB_ID:artifact:PATH")
            return 1
        if note:
            print(note)
        return print_artifact(job_id, path)

    raw_mode = mode == "raw"
    grep_mode = mode == "grep"
    errors_mode = mode in ("errors", "fail")



    grep_pattern, grep_note = (
        _job_argv.grep_pattern("gh-job", sys.argv[3:]) if grep_mode else (None, "")
    )
    if grep_mode and not grep_pattern:
        print("ERROR: usage: gh-job:JOB_ID:grep:PATTERN")
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


    log_mode = ""
    if raw_mode or grep_mode or errors_mode:
        log_mode = mode

    config = _get_config()
    tail_lines = config["lines"]




    job_name = "?"
    job_status = "?"
    job_conclusion = "?"
    job_meta: dict | None = None
    meta_absent = False
    meta_error = ""
    run_id = ""
    pr_title = ""
    pr_number = ""
    pr_branch = ""
    pr_author = ""

    try:

        meta_result = subprocess.run(
            ["gh", "api", _api_repo_path(f"actions/jobs/{job_id}")],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
        )
        if meta_result.returncode != 0:




            if _gh_error_kind(meta_result.stderr) == "notfound":
                meta_absent = True
            else:
                meta_error = (meta_result.stderr.strip()
                              or f"gh exited {meta_result.returncode}")
        if meta_result.returncode == 0:
            meta = json.loads(meta_result.stdout)
            job_meta = meta
            job_name = meta.get("name", "?")
            job_status = meta.get("status", "?")
            job_conclusion = meta.get("conclusion") or "in_progress"
            run_id = str(meta.get("run_id", ""))
            run_url = meta.get("run_url", "")


            if run_id:
                run_result = subprocess.run(
                    ["gh", "run", "view", run_id, *_repo_target.gh_args(), "--json",
                     "headBranch,event,pullRequests"],
                    capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
                )
                if run_result.returncode == 0:
                    run_data = json.loads(run_result.stdout)
                    pr_branch = run_data.get("headBranch", "")
                    prs = run_data.get("pullRequests", [])
                    if prs:
                        pr_number = str(prs[0].get("number", ""))

                        if pr_number:
                            pr_result = subprocess.run(
                                ["gh", "pr", "view", pr_number, *_repo_target.gh_args(), "--json",
                                 "title,author,headRefName,baseRefName,labels"],
                                capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
                            )
                            if pr_result.returncode == 0:
                                pr_data = json.loads(pr_result.stdout)
                                pr_title = pr_data.get("title", "")
                                pr_author = (pr_data.get("author") or {}).get("login", "")
                                pr_branch = pr_data.get("headRefName", pr_branch)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        meta_error = meta_error or f"{type(exc).__name__}: {exc}"


    try:







        log_result = subprocess.run(
            ["gh", "api", "--allow-escape-sequences",
             _api_repo_path(f"actions/jobs/{job_id}/logs")],
            capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("ERROR: gh not found — install the GitHub CLI")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: gh timed out (log)")
        return 1

    if log_result.returncode != 0:
        if _gh_error_kind(log_result.stderr) == "notfound":
            probe = None
            if job_meta is None and meta_absent:





                probe = _probe_check_run(job_id)
                if probe[0] == "found" and isinstance(probe[3], dict):
                    routed = _render_routed_check(job_id, probe[3], log_mode)
                    if routed is not None:
                        return routed
            print(_missing_log_message(job_id, job_meta, meta_absent,
                                       meta_error, probe))
        else:
            print(_format_error(log_result.stderr, "Job log", job_id))
        return 1


    log = log_result.stdout
    log = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', log)

    log = re.sub(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ', '', log, flags=re.MULTILINE)

    lines = _log_lines(log)
    total = len(lines)


    display_status = job_conclusion if job_conclusion != "in_progress" else job_status
    print(f"# Job #{job_id} — {_untrusted.flat(job_name)}")
    print(f"Status: {display_status}")

    if pr_number:
        print(f"\n## PR #{pr_number} — {_untrusted.flat(pr_title)}")
        if pr_author:
            print(f"Author: {_untrusted.flat(pr_author)}")
        if pr_branch:




            print(f"Branch: {_untrusted.flat(pr_branch)}")
            local_check = _local_branch_check(pr_branch)
            if local_check:
                print(local_check)

    if run_id:
        print(f"Run: #{run_id}")

    print(f"Log: {total} lines total")





    suite = _suite_summary(lines)
    if suite:
        print(suite_line(suite, len(_suite_summaries(lines)), job_conclusion))

    if total == 0 and not raw_mode:







        print()
        print("## The log is empty — the fetch succeeded and returned 0 bytes")
        print(f"This is not a missing log: gh returned one, and it has no "
              f"content. Job state: status `{job_status}`, conclusion "
              f"`{job_conclusion}`.")
        logs_path = _printable_api_repo_path(
            "actions/jobs/" + str(job_id) + "/logs")
        print(f"Cross-check the raw bytes with: gh api {logs_path}")
        return 0











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
        shown = lines[start - 1:end]
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
                        env_int(os.environ.get("GH_JOB_GREP_MAX_BYTES"), "GH_JOB_GREP_MAX_BYTES", 65536, minimum=1),
                        "GH_JOB_GREP_MAX_BYTES", shown_pattern, ctx)
        return 0



    patterns, resolution = _select_job_patterns(
        job_name, config["job_patterns"], config["error_patterns"]
    )
    resolution_line = (
        f"Resolve:  ./supertool '{resolution.replace('{id}', job_id)}'"
        if resolution else ""
    )
    error_sections = _find_error_sections(lines, patterns,
                                          config["error_context"],
                                          trailing_gap=errors_mode)


    if errors_mode:
        mismatch = _selection_mismatch(display_status, job_id)
        if not error_sections:
            if display_status == "failure":
                _print_unmatched_failure(job_id, display_status, patterns, lines, total)
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

    if error_sections and display_status == "failure":
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
    elif display_status == "failure":


        _print_unmatched_failure(job_id, display_status, patterns, lines, total)
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
