#!/usr/bin/env python3
"""GitHub Actions job log via gh CLI.

Shows job metadata + smart log output:
1. Searches for error patterns and shows context around them
2. Falls back to last N lines if no patterns found

Config via SUPERTOOL_ env vars (set from .supertool.json):
  SUPERTOOL_LINES           — tail lines (default 80)
  SUPERTOOL_ERROR_PATTERNS  — comma-separated patterns to search
  SUPERTOOL_ERROR_CONTEXT   — lines of context around each error match (default 5)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #1388)
import _repo_target  # noqa: E402  (the repo this call is about, when not the cwd's)
import _branch_locale  # noqa: E402  (where the branch is checked out — shared by all five #850)
import _untrusted  # noqa: E402  (a PR's branch, title and author are the opener's text — #965)
import _auth_probe  # noqa: E402  (does this stderr *state* that the credential is unusable? - #1846)
import _status_probe  # noqa: E402  (does this stderr *state* the target is missing or access denied? - #1864)
import _job_argv  # noqa: E402  (the argv shape both job presets share — #1145)
from _env import env_int  # noqa: E402  (the one numeric-knob reader)


def _api_repo_path(suffix: str) -> str:
    """A `gh api` repo path — the target's, or gh's own cwd placeholders.

    `gh api repos/{owner}/{repo}/…` expands those two literal placeholders from
    the cwd's remote. That expansion is precisely what a repo target has to
    override, so the placeholders are replaced rather than accompanied — there
    is no `--repo` on `gh api` to add beside them (#673).
    """
    return _repo_target.api_path(suffix)


def _printable_api_repo_path(suffix: str) -> str:
    """The same path, **printed for a reader to paste** (#1679).

    Two different consumers of one string, which is the distinction #1670 drew
    in `pr_merge`: gh resolves `{owner}`/`{repo}` from whatever cwd the line is
    pasted into, so the printed command silently names a different repository
    in every checkout. Under a `repo:` target this is `_api_repo_path` exactly.
    """
    return _repo_target.api_path_printable(suffix)


def _local_branch_check(source: str) -> str:
    """Return a one-line local-branch-vs-source check for output.

    Delegated to `_branch_locale` (#850): a branch held by a linked worktree is
    neither a match nor a MISMATCH, and saying MISMATCH there prescribed a
    checkout git refuses.
    """
    return _branch_locale.check(source)


def _gh_error_kind(stderr: str) -> str:
    """Bucket a gh failure. Probe order matches the original _format_error
    chain exactly, so the message any given stderr produces is unchanged."""
    s = stderr.lower()
    if "github host" in s or "not a git repository" in s or "git remotes" in s:
        return "repo"
    if _status_probe.says_not_found(s):
        return "notfound"
    # A status, never a number (#1846). `401` sits inside a GitHub user id
    # (`API rate limit exceeded for user ID 44012345`) and inside a request id,
    # and this arm is above the rate-limit and permission arms -- so a throttle
    # printed `gh auth login`, a remedy for a cause nothing established, and
    # never reached the arm that says "retry".
    # A bare `token` went the same way: `Resource not accessible by personal
    # access token` is a 403 about scopes, and the permission arm below was
    # unreachable for it.
    if _auth_probe.says_not_authenticated(s):
        return "auth"
    if "rate limit" in s or "429" in s:
        return "ratelimit"
    if _status_probe.says_forbidden(s):
        return "forbidden"
    return "other"


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify gh errors into actionable messages for LLMs."""
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
    # The remote host wrote this text — flattened, never relayed raw (#1606).
    return (f"ERROR: gh failed for {resource} #{identifier}: "
            f"{_untrusted.flat(stderr.strip())}")


def _probe_check_run(job_id: str) -> tuple[str, str, str, dict | None]:
    """Ask the *checks* namespace about an id the Actions namespace disowned.

    Returns `(state, name, error, data)` where state is `found` / `absent` /
    `unknown`. Called from exactly one place — the path where both the job
    endpoint and the log endpoint have already 404'd — so it costs **no extra
    request on any path that was working**, including the happy one. That is
    the same trade #723 made for the metadata call it reused.

    **What changed in #827.** #793 used this to fix a *message* and refused to
    let it produce an answer, on the grounds that rendering a check run under a
    `# Job #N` header would be a probe that silently changed which API
    answered. That is right about *silently* and not about *answering*: a
    render whose header says `# Check run #N` and whose second line names the
    routing is labelled, not silent. So the object comes back with the verdict
    now, and `_render_routed_check` renders it in the **check** shape. The
    reader does not have to know GitHub keeps CI in two id namespaces, which
    is the whole of #827 — GitLab needs no equivalent op because its model is
    one pipeline, one hierarchy, one id space.

    **Why answering here is not a guess.** Verified live on 2026-08-05: an
    Actions job's id *is* its check run's id (the job object publishes
    `check_run_url: .../check-runs/<same id>`), and an App's check run 404s in
    the Actions namespace. So Actions-first is a total order, not a
    coin-flip — an answer from Actions is definitive and this probe never
    runs; only a 404 there sends the question here. The genuine uncertainty is
    narrower and it still declines: see the `unknown` state below and
    `_absent_job_message`.
    """
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
    """`presets/github/check.py`, loaded by path. `None` if it cannot be.

    The presets are standalone scripts rather than a package, so this is an
    explicit path load rather than an import — it mutates no `sys.path` and
    cannot collide with a module called `check`. It is lazy: only the routed
    path calls it, so nothing that already worked pays for it.

    A load failure returns `None` and the caller falls back to #793's message,
    which names `gh-check` and exits 1. A routing convenience must not be able
    to turn a legible error into a traceback.
    """
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
    """Say that a requested log mode was not applied, when it cannot be.

    `fail`, `raw` and `grep` all slice a job log, and a check run has no log.
    Dropping the mode silently is the same quiet as rendering an absence: the
    reader asked a question that was never answered and is not told which one.
    """
    if not mode:
        return ""
    return (
        f"Note: `{mode}` slices a job log and this id is a check run, which "
        f"has no log — that mode does not apply here and was not applied. "
        f"What this check reported is in Output and Annotations below."
    )


def _render_routed_check(job_id: str, check: dict, mode: str) -> "int | None":
    """Render a check run that `gh-job` was handed. `None` if it cannot.

    Returning `None` rather than raising keeps the fallback honest: the caller
    prints #793's message and exits 1, which is worse than a render but is
    still a true sentence.
    """
    mod = _load_check_renderer()
    if mod is None:
        return None
    routed = (
        f"Routed: you called `gh-job:{job_id}`. That id is not an Actions job, "
        f"so this op read the checks API instead — the same render as "
        f"`gh-check:{job_id}`."
    )
    return mod.render_check(job_id, check, routed_from=routed,
                            mode_note=_log_mode_note(mode))


def _absent_job_message(job_id: str, check: tuple[str, str, str, object]) -> str:
    """The 404-on-both-endpoints case, split by what the checks API said.

    Before #793 this said "no such job exists in this repo. Check the ID." for
    all three of these. For a CodeQL check run that sentence is simply false —
    the id exists, one namespace over — and it is this repo's own defect class
    landing in its own error path: could-not-find-it-by-my-route published as
    absent-from-the-world (#672).
    """
    state, name, error, _data = check
    if state == "found":
        # Unreachable from `main` since #827 — that state routes and renders
        # rather than printing a sentence. It stays because `_absent_job_message`
        # is also the fallback when the check renderer cannot be loaded, and
        # because a message pointing at the right op is the correct thing to
        # print when the render is what failed.
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
    """Explain a 404 from the logs endpoint by the job's state, not the ID.

    GitHub writes a job's log **on completion**, so `404 BlobNotFound` from
    `actions/jobs/<id>/logs` has four causes that call for four different
    next actions, and the op used to render all four as "Check the ID" — the
    one thing that was demonstrably right in the incident that filed #723.
    Verified live on this repo: a queued job and an in_progress job both
    return `gh: HTTP 404` with a `<Code>BlobNotFound</Code>` body, while the
    *job* endpoint returns the full object for the same ID.

    That job endpoint is what separates them, and `main` already calls it
    before it fetches the log — so this costs **no extra request on any
    path**, including the happy one where the log exists and nobody cares
    about the job's status. Fetching metadata defensively *after* the failure
    would have been the cheap-looking option; there was nothing to buy.

    The cancelled row is the one that saves real time: it is the only state
    whose right response is to stop looking rather than to retry.

    The both-endpoints-404 row grew a second question in #793: an id can be a
    **check run** rather than an Actions job, and saying "no such job exists"
    of one that does is the defect this op is meant to be free of. See
    `_absent_job_message`.

    When the job endpoint itself did not answer, there is nothing to decide
    from. That is the third state and it declines, rather than picking the
    likeliest of four (`docs/validators.md`, "Declining instead of
    guessing"). All four branches stay ERROR and exit 1 — a log that could
    not be read must never soften into an empty log or an ok.
    """
    if meta is None:
        if meta_absent:
            # `probe` is threaded in by the caller when it has already asked
            # the checks namespace (#827), so the question is never put twice.
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
    """Read config from SUPERTOOL_ env vars."""
    return {
        "lines": env_int("SUPERTOOL_LINES", 80, minimum=1),
        "error_patterns": os.environ.get(
            "SUPERTOOL_ERROR_PATTERNS", "ERROR,FAILED,Error:,Failed,fatal:,##[error]"
        ).split(","),
        "error_context": env_int("SUPERTOOL_ERROR_CONTEXT", 5, minimum=0),
        "job_patterns": _parse_job_patterns(os.environ.get("SUPERTOOL_JOB_PATTERNS", "")),
    }


def _parse_job_patterns(raw: str) -> list[dict]:
    """Parse the optional per-job-name pattern table (JSON list).

    Each entry: {"job": <name-regex>, "patterns": [<str>...], "resolution": <op>?}.
    Returns [] on empty or malformed config — the flat error_patterns still apply.
    """
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
    """Pick patterns + resolution op for this job by name.

    First entry whose "job" regex matches job_name wins. No match → the flat
    default_patterns with no resolution (backward compatible).
    """
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


# The refusal header, named rather than spelled inline (#1106). It used to
# read `## FAILED — no error pattern matched`, which is word-for-word the
# clause `gap_marker` uses about the lines it elided *inside a successful
# classification*. One phrase, two renders, opposite meanings: a reader who
# greps it — or a watch rule, or a test asserting `not in out` — cannot tell
# "supertool could not classify this job" from "these lines sat between two
# anchors". #1099 lost two tests to exactly that after rebasing onto #1091.
#
# #1091's wording wins, deliberately: `the log itself is intact` is the only
# clause separating *this op cut lines* from *the log was truncated*, and
# #1014 was filed on that misread. So the refusal moved instead, to a phrase
# that cannot occur inside a normal render.
#
# NOT hoisted into a shared module. A preset runs with `presets/` on
# `sys.path` and cannot import the core, and the two job presets are private
# twins by design — `gap_marker` is duplicated for the same reason. The
# mechanism that keeps them honest is a test, not an import:
# `tests/test_job_refusal_header_collision_1106.py` pins the twins equal to
# each other AND disjoint from `gap_marker`, so the next collision is a red
# build rather than a coincidence two people have to notice.
UNCLASSIFIED_HEADER = "## FAILED — supertool could not classify this job"


def _print_unmatched_failure(
    job_id: str, status_label: str, patterns: list[str], lines: list[str], total: int
) -> None:
    """Report a failed job the patterns could not classify — never as silence.

    `## No error patterns matched` on a job GitHub calls *failure* reads as
    green (#453, mirrors #445/#452 on the GitLab side), which is the worst
    output a failure tool can produce. A failed job always has a reason; not
    finding it is a gap in this tool, so the output says exactly that and
    hands back the raw evidence.

    Unlike gl-job, this does not port a "last section entered" hint: GitHub
    Actions always runs its `if: always()` steps (the junit-summary step,
    "Post job cleanup") after a failure, so the last `##[group]` in the log is
    routinely a step that has nothing to do with the failure — naming it would
    mislead rather than help.
    """
    tail_n = env_int("GH_JOB_UNMATCHED_TAIL_LINES", 40, minimum=1)
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
    for i, line in enumerate(tail):
        print(f"  {start + i:>5} | {line}")
    print(
        f"\nNext:  ./supertool 'gh-job:{job_id}:raw'  or  "
        f"'gh-job:{job_id}:grep:PATTERN'  — the whole trace is still there."
    )


# `:fail` fits exactly one conclusion. Stated as the complement rather than a
# list of the others (`cancelled`, `timed_out`, `skipped`, ...) so a conclusion
# GitHub adds later, or an in-progress job whose display status is `queued`,
# lands on the disclosure by default instead of on the silent overclaim.
_FAIL_SELECTOR_FITS = "failure"


def _selection_mismatch(display_status: str, job_id: str) -> str:
    """Say that error-block selection cannot answer for this job, and what can.

    #916: `:fail` had the job's conclusion in hand — it prints it two lines
    above — and applied the error-block selector anyway, then headlined the
    result as complete. A reader acting reasonably on `All error blocks (11
    lines matched, no tail truncation)` concludes the log holds nothing; on the
    job in the issue the log held six `Terminate orphan process` lines that were
    the strongest available evidence about a hang, and no error pattern could
    ever have matched them.

    Disclosure rather than widening the pattern set, deliberately. Widening
    fixes one log: the next cancelled job's tell is some other unmarked line,
    and a larger set that still misses it produces a longer and more
    confident-looking block. A pattern set cannot be complete, so the honest
    output is that these N lines matched AND that this selector is not where the
    cause lives here — the third state, said out loud rather than implied by an
    empty result.

    Not suppression: the matched lines are still printed. Trading a loud wrong
    answer for no answer is the same defect pointed the other way.
    """
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


# A pytest terminal summary line. Two spellings, both live:
#
#   ==== 6 failed, 7760 passed, 677 skipped in 221.51s ====   (a tty, or -v)
#   6 failed, 7760 passed, 677 skipped, 2 warnings in 221.51s (0:03:41)
#
# The second is what job 93033577461 — #1050's own job — actually contains:
# GitHub Actions is not a tty, so pytest writes no `=` rule. A first pass here
# required the fences, matched the fixture written from the issue's quote, and
# would have found nothing in any real CI log. The fences are therefore
# optional and the anchor is the *shape*: count-noun pairs, then `in <duration>`,
# and nothing else on the line. That rejects prose carrying the same words
# ("the previous run had 6 failed tests") without depending on decoration that
# only appears on a terminal.
#
# Anchored at column 0. `.match(line.strip())` could not tell the job's own
# summary from one echoed inside captured subprocess output, or from a `-s` run
# that reprints a nested pytest indented — and with the selection below that
# nested line could silently replace the outer one.
_SUITE_SUMMARY_RE = re.compile(  # anchored-ok: matched against one rstrip()ed log line
    r"^(?:=+[ \t]*)?"
    r"(?P<counts>\d+\s+[A-Za-z]+(?:\s*,\s*\d+\s+[A-Za-z]+)*)"
    r"\s+in\s+[0-9.]+s"
    r"(?:\s*\([^)]*\))?"
    r"[ \t]*=*$"
)

# At least one of these has to appear, so `= no tests ran in 0.12s =` and a
# banner of `=` around something else are both declined rather than reported as
# a count of nothing.
#
# `warnings` is deliberately NOT here. `2 warnings in 0.30s` is a valid pytest
# summary that counts **zero tests**, and rendering it under a header reading
# "these count TESTS" gives a number more authority than it has — a small
# instance of the same over-claim the whole op is about.
_SUITE_OUTCOMES = (
    "passed", "failed", "error", "errors", "skipped",
    "xfailed", "xpassed", "deselected",
)

# The subset that means "this invocation reported a problem". Used to pick
# between several summaries in one log — see `_suite_summary`.
_SUITE_BAD = ("failed", "error", "errors")


def _suite_summaries(lines: list[str]) -> list[str]:
    """Every pytest terminal summary in the log, in the order they were run."""
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
    """The job's own `N failed, M passed` line, or `None` when it states none.

    #1050. `gh-pr:N:status` said `20 total: 16 passed, 4 failed` and the four
    were **legs**; carried forward as four *tests* it produced a specifically
    misleading picture — three visible names past an elision looked like a
    partial failure, which points at ordering or shared state, while the truth
    was six-of-six uniform, which points at the fixture. The agent handed that
    diagnosis had to correct the premise before it could start.

    The line that settles it in one glance already exists in every one of those
    logs and no op surfaced it. It is authoritative (pytest wrote it, about
    itself), it is one line, and it is the only number in the whole render that
    counts *tests*.

    `None` rather than a zero when there is none: a log from a build step or a
    linter states no test count, and "0 failed" would be a claim this function
    is in no position to make.

    **The last summary in the log is the wrong one to report.** A job that runs
    pytest twice — a second suite step, a `--lf` retry, tox — writes two, and
    taking the trailing one turned `6 failed, 100 passed` followed later by
    `7 passed` into `Suite: 7 passed` on a job with six real failures. That is
    the premise-correction failure #1050 exists to remove, reintroduced by
    #1050's own fix. So the **last one reporting a failure or an error** wins,
    and only when no invocation reported either does the trailing one stand.
    The render discloses the count when there is more than one, because
    "which invocation" is then a question the reader has to be able to ask.
    """
    found = _suite_summaries(lines)
    if not found:
        return None
    for body in reversed(found):
        if _suite_bad_count(body):
            return body
    return found[-1]


def _suite_bad_count(summary: str) -> int:
    """How many failures/errors the summary body actually states.

    `"failed" in body` was the old test and it is wrong in the one direction
    that matters here: `0 failed, 9999 passed` contains the word and reports no
    failure at all. The counts are what the cross-check below compares against
    the API's conclusion, so they have to be read as numbers.
    """
    total = 0
    for count, word in re.findall(r"(\d+)\s+([A-Za-z]+)", summary):
        if word.lower() in _SUITE_BAD:
            total += int(count)
    return total


# The two conclusions that are an answer about the job. Everything else —
# `cancelled`, `timed_out`, `skipped`, `in_progress`, an empty string — is the
# API not having answered, and reading "not success" as "the suite failed"
# would be the same over-claim this function exists to remove, pointed the
# other way.
_CONCLUSIONS_THAT_ANSWER = ("success", "failure")


def suite_line(summary: str, n_summaries: int, job_conclusion: str) -> str:
    """The `Suite:` render — the number, and where the number came from (#1076).

    The count is read out of the job log with a regex anchored at column 0,
    after timestamps and ANSI have been stripped. On a pull request the code
    that writes that log is the pull request's code, so ordinary program output
    satisfies the anchor and the number is the log author's. `flat()` keeps a
    forged *line* out of the render and always did; what it cannot do is make
    the number authoritative, and the wording here used to say "the job's own
    summary line" and "These count TESTS", which claims exactly that.

    Reading the count from `junit.xml` instead — the audit's suggestion, and
    the right instinct — is not available to this op. `.github/scripts/
    junit_summary.py` writes it to the runner's working directory and nothing
    uploads it as an artifact; `gh-job` reads `gh run view --log` and only that,
    and the script's *output* is log text like everything else. A workflow file
    is also part of a pull request's diff, so even that output is not a source
    the log author cannot reach. So the honest fix is provenance, not a
    second parser.

    Two changes, and the second is the one the issue turns on:

      * the provenance clause is **unconditional**. The old multiplicity caveat
        only fired at two summaries or more, so a job that ran no suite at all —
        one stray matching line, `n_summaries == 1` — got the number with
        nothing attached.
      * the count is cross-checked against `job_conclusion`, which comes from
        the Actions API and is the one fact in this render the log does not
        write. `0 failed, 9999 passed` on a job the API calls `failure` is
        precisely what a forged all-green summary looks like, and it is now
        said out loud. Agreement is not narrated: a cross-check that speaks on
        every render is one nobody reads.
    """
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
    """The log's own lines — LF / CR / CRLF — with nothing else honoured.

    #1105, the same forged-boundary class as #1081 one preset over.
    `str.splitlines()` breaks on eight separators a CI log does not define,
    and everything below this anchors at column 0. `_SUITE_SUMMARY_RE` does so
    deliberately: the comment above it says a `.match(line.strip())` could not
    tell the job's own pytest summary from one echoed inside captured
    subprocess output. On a pull request the code writing this log is the pull
    request's code, so `str.splitlines()` handed that anchor to the log's
    author — a `print` carrying U+2028 opened a column-0 line mid-sentence, and
    `Suite:` is read from exactly there. That is the one number in the render
    claiming to count TESTS.

    Narrowing the split alone would trade the forged parse boundary for a
    forged *render* line: the separator would survive into `  1234 | ...` and
    move the terminal's cursor to a fresh row with no gutter, which reads as a
    line supertool wrote (#851, one surface over). So the separators this split
    no longer honours are disclosed as pictures on the way through, and that
    pairing is what makes the narrowing a fix rather than a quieter version of
    the same bug.

    Tabs are kept: a log line is a block and its indentation is the author's
    content, which is the same call `_untrusted.scrub` makes for a fence.
    """
    return [_untrusted.visible(line, keep=chr(9))
            for line in _untrusted.split_lines(log)]


def gap_marker(n_lines: int) -> str:
    """The line that stands where the op cut, saying so and saying how much.

    #1050. This used to be a bare ``...``, which is the *log's* own vocabulary:
    a truncated `AssertionError: ...`, a pytest diff elision, a `gh` field cut
    short. Reading PR #1047's Windows red, the `...` between two matched blocks
    held a `[validators]` section whose single line was the entire
    discriminator between three candidate causes — and it read as part of the
    assertion above it, so it was never looked for. Recovering it cost a second
    call with `:grep:`.

    So the marker states two things a bare ellipsis cannot: that **this op**
    removed the lines, and **how many**. Thirty-four lines and three are the
    same three characters otherwise, and the difference is whether the reader
    reaches for `:raw` next.

    The log is never described as incomplete — it is not. What is incomplete is
    this selection of it.

    `presets/gitlab/job.py` carries a byte-identical copy, because the two ops
    are read interchangeably and one wording for one idea is the whole point
    (#1066). `tests/test_gl_job_gap_marker_twins_1066.py` compares them, so a
    change to this string that does not reach the twin fails there.
    """
    unit = "line" if n_lines == 1 else "lines"
    return (f"... ({n_lines} {unit} elided by this op — no error pattern "
            f"matched them; the log itself is intact)")


def _find_error_sections(lines: list[str], patterns: list[str], context: int,
                         trailing_gap: bool = False) -> list[tuple[int, str]]:
    """Find lines matching error patterns and return them with context.

    Gaps between the context windows carry `gap_marker` rather than a bare
    ``...`` (#1050), and every withheld line is counted by exactly one marker,
    so the numbers in the render account for the whole log.

    `trailing_gap` is off by default and **must stay that way for the default
    render**. That path prints these sections and then `## Tail (last 80
    lines)` immediately below, which contains most of the very lines a trailing
    marker would have declared elided — on a 500-line log whose last match is at
    400, the marker claimed 99 lines were not shown and 80 of them were printed
    three lines later. Only `:fail`, which prints blocks and nothing else, can
    truthfully make that claim.
    """
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
    # `-1`, not `-2`. At `-2` the first index (0) satisfies `idx > prev + 1`
    # and the render opened with an elision marker over nothing at all — an
    # absence the op invented, on a log whose first line was shown directly
    # beneath it. Found by #1050's own test; it is the same defect as the bare
    # `...`, one line up, and it made every `:fail` render look like it had
    # already cut something before it had.
    prev = -1
    for idx in sorted_matches:
        if idx > prev + 1:
            result.append((-1, gap_marker(idx - prev - 1)))
        result.append((idx + 1, lines[idx]))
        prev = idx

    # Anything after the last shown line is withheld too, and the reader
    # deciding whether to call `:raw` needs that number as much as the middle
    # ones. `:fail` says "no tail truncation" about its *blocks*, which has
    # been read as "no tail" about the log.
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
    """Print grep hits under a byte budget, and say so when the budget bit (#622).

    The op used to print every hit, unbounded. That reads as safe — nothing is
    dropped — but a `:grep:` over a CI trace where each match is a whole
    assertion failure with rendered HTML emits hundreds of KB into a consumer
    that cuts at a few tens, so the tail vanished with no marker anywhere. A
    pipeline triage read the surviving head as the whole list and judged the
    blast radius small; it was not. Unbounded-then-cut-downstream is the same
    silence as a limit that does not announce itself, one layer over.

    So the bound moves here, where the true total is already known, and the
    three states stay distinguishable:

      - everything fit: nothing extra is printed, and that silence is the
        positive claim that the list is whole;
      - the budget bit: the shortfall is stated in exact numbers, because
        `match_count` was computed over the whole trace before printing began.
        This is not the streaming case where only "there was more" is knowable
        — do not weaken it to one;
      - and the note names *size* as what cut, plus the knob that governs it.
        Saying "limit N" here would be a confidently wrong disclosure: this op
        has no match limit, and the cut fires far earlier than any count would
        suggest precisely because the lines are enormous. Wrong is worse than
        silent.

    The note is a single bounded line (#605) — one line per dropped match would
    re-spend the budget the bound just saved.
    """
    # Plan first, print second. The note has to go in the HEADER as well as
    # the footer, and the header is written before the body — so what fits
    # must be known before the first byte goes out. A footer-only disclosure
    # is read by nobody in exactly the case it exists for: the reader who is
    # being cut off is cut off before reaching it.
    planned: list[str] = []
    emitted = 0
    shown_matches = 0
    # `-1` for the same reason as `_find_error_sections` (#1050): at `-2` the
    # first hit was preceded by an elision marker covering zero lines.
    prev = -1
    cut = False
    for idx in hit_indexes:
        chunk = (gap_marker(idx - prev - 1) + "\n") if idx > prev + 1 else ""
        chunk += f"  {idx + 1:>5} | {lines[idx]}\n"
        size = len(chunk.encode("utf-8", "replace"))
        # The first hit always goes out whole, however fat: a bound that can
        # return zero matches on a pattern that matched is an absence the op
        # invented, which is the disease itself.
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
    sys.stdout.write("".join(planned))
    if cut:
        print(
            f"... ({shown_matches} of {match_count} matching lines shown — output "
            f"capped at {budget} bytes by size, not by a match count limit; "
            f"raise {knob}=N or narrow the pattern)"
        )


def main() -> int:
    use_utf8_stdout()
    if len(sys.argv) < 2:
        print("ERROR: usage: job.py JOB_ID [raw [START [END]]]")
        return 1

    # #1145 — argv is what core made of the op string, and three shapes of it
    # used to reach a render. Refuse before anything is fetched: an id or a mode
    # the op cannot serve must never appear in output that looks like a read.
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
    raw_mode = mode == "raw"
    grep_mode = mode == "grep"
    errors_mode = mode in ("errors", "fail")
    # Everything right of `grep` is the pattern — this op takes no argument
    # after it — so the pieces core split on ':' rejoin rather than the tail
    # being dropped.
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
                # Tail form — see the gl-job twin of this block (#487).
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
            # An inverted range used to slice to nothing under a header reading
            # "Raw lines 10-9 of 20" — an empty body that reads as an empty
            # stretch of log rather than as a range the op could not serve.
            print(f"ERROR: raw END ({raw_end}) is before START ({raw_start}); "
                  f"ranges are 1-indexed and inclusive")
            return 1
        if raw_tail is not None and raw_end is not None:
            print("ERROR: the raw tail form takes no END — "
                  f"use raw:-{raw_tail} for the last {raw_tail} lines, "
                  "or raw:START:END for an absolute range")
            return 1
    # Which log-slicing mode was asked for, by the name the caller typed. Only
    # used to say it does not apply when the id turns out to be a check run.
    log_mode = ""
    if raw_mode or grep_mode or errors_mode:
        log_mode = mode

    config = _get_config()
    tail_lines = config["lines"]

    # 1. Get job metadata — gh doesn't have a direct "get job by ID" command,
    # but we can get the log which includes the run context
    # First, try to get run info from the job
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
        # gh api to get job details
        meta_result = subprocess.run(
            ["gh", "api", _api_repo_path(f"actions/jobs/{job_id}")],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
        )
        if meta_result.returncode != 0:
            # A 404 *here* is the one case where "check the ID" is the truth:
            # there is no such job. Every other failure means the job's state
            # is unknowable, and #723 is precisely about not converting that
            # into a confident answer further down.
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

            # Get the run to find the PR
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
                        # Get PR details
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

    # 2. Get job log
    try:
        log_result = subprocess.run(
            ["gh", "api", _api_repo_path(f"actions/jobs/{job_id}/logs")],
            capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("ERROR: gh not found — install from https://cli.github.com")
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: gh timed out (log)")
        return 1

    if log_result.returncode != 0:
        if _gh_error_kind(log_result.stderr) == "notfound":
            probe = None
            if job_meta is None and meta_absent:
                # Both endpoints have 404'd. Ask the other namespace once —
                # and this is the branch #827 turned from a signpost into an
                # answer. Only `found` routes; `absent` and `unknown` fall
                # through to the message, so an unanswered probe still
                # declines rather than rendering an empty check.
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

    # Clean timestamps and ANSI codes from log
    log = log_result.stdout
    log = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', log)
    # GitHub Actions prepends timestamps like "2024-01-15T10:30:00.1234567Z "
    log = re.sub(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ', '', log, flags=re.MULTILINE)

    lines = _log_lines(log)
    total = len(lines)

    # Header
    display_status = job_conclusion if job_conclusion != "in_progress" else job_status
    print(f"# Job #{job_id} — {_untrusted.flat(job_name)}")
    print(f"Status: {display_status}")

    if pr_number:
        print(f"\n## PR #{pr_number} — {_untrusted.flat(pr_title)}")
        if pr_author:
            print(f"Author: {_untrusted.flat(pr_author)}")
        if pr_branch:
            # Flattened for the render; `_local_branch_check` below is handed
            # the raw name on purpose — it applies its own `flat` and the #924
            # ordinary-refname rule, and comparing a rewritten name against the
            # local branch would answer about a ref that does not exist.
            print(f"Branch: {_untrusted.flat(pr_branch)}")
            local_check = _local_branch_check(pr_branch)
            if local_check:
                print(local_check)

    if run_id:
        print(f"Run: #{run_id}")

    print(f"Log: {total} lines total")

    # #1050: printed here rather than beside the error blocks, because the
    # question it answers ("four failed legs, or four failed tests?") is asked
    # before the reader reaches them — and because it is a fact about the job,
    # like `Status:` above it, not part of any selection below.
    suite = _suite_summary(lines)
    if suite:
        print(suite_line(suite, len(_suite_summaries(lines)), job_conclusion))

    if total == 0 and not raw_mode:
        # Empty and absent are two different lies this surface tells (#723):
        # `gh run view --log` returns nothing at all for jobs whose log the
        # API serves in full. Absence exits 1 above with a stated cause; a log
        # that was fetched successfully and is genuinely 0 bytes says exactly
        # that, in its own words, and never borrows the vocabulary of a log
        # that could not be read. Without this the run fell through to the
        # pattern search and printed a banner over nothing at all.
        print()
        print("## The log is empty — the fetch succeeded and returned 0 bytes")
        print(f"This is not a missing log: gh returned one, and it has no "
              f"content. Job state: status `{job_status}`, conclusion "
              f"`{job_conclusion}`.")
        logs_path = _printable_api_repo_path(
            "actions/jobs/" + str(job_id) + "/logs")
        print(f"Cross-check the raw bytes with: gh api {logs_path}")
        return 0

    # 3. Raw mode — dump (sliced) trace, skip filters
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
                # Same trade as gl-job (#487): return the tail of the width
                # asked for, and say it is not the range that was requested.
                width = (end - start + 1) if raw_end is not None else tail_lines
                width = max(1, min(width, total))
                print(f"\n## Raw — requested {start}-{end} is past end of log "
                      f"({total} lines); showing the last {width} lines instead")
                start, end = total - width + 1, total
            end = min(end, total)
        shown = lines[start - 1:end]
        print(f"\n## Raw lines {start}-{start + len(shown) - 1} of {total}")
        for i, line in enumerate(shown):
            print(f"  {start + i:>5} | {line}")
        return 0

    # 3b. Grep mode — ad-hoc regex over the log, context + gap markers.
    # Honest primitive: caller's pattern, no config. Regex with literal
    # fallback on re.error (mirrors supertool's grep). Never silent-empty.
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
            for i, line in enumerate(tail):
                print(f"  {start + i:>5} | {line}")
            return 0
        match_count = sum(1 for line in lines if rx.search(line))
        _emit_grep_hits(lines, sorted(hits), rx, match_count,
                        env_int("GH_JOB_GREP_MAX_BYTES", 65536, minimum=1),
                        "GH_JOB_GREP_MAX_BYTES", shown_pattern, ctx)
        return 0

    # 4. Error pattern search. Per-job-name table (if configured) picks tighter
    # patterns + a resolution op; else the flat default applies.
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

    # fail/errors mode — dump ALL matched blocks, no tail cap
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
            # #916. The old header read `All error blocks (N lines matched, no
            # tail truncation)` on a cancelled job — two claims true of the
            # SELECTOR and false of the LOG. A cancellation writes exactly one
            # error line and puts everything diagnostic outside it, so "all" is
            # a statement about a filter that could not have reached the cause.
            # The op already knows: `Status: cancelled` is printed above from
            # this same value.
            print(f"\n## Error blocks ({matched_count} lines matched) — but see below")
        else:
            print(f"\n## All error blocks ({matched_count} lines matched, no tail truncation)")
        for line_num, text in error_sections:
            if line_num == -1:
                print(text)
            else:
                print(f"  {line_num:>5} | {text}")
        if mismatch:
            print(mismatch)
        if resolution_line:
            print(f"\n{resolution_line}")
        return 0

    if error_sections and display_status == "failure":
        print(f"\n## Error context ({len([e for e in error_sections if e[0] > 0])} lines matched)")
        for line_num, text in error_sections:
            if line_num == -1:
                print(text)
            else:
                print(f"  {line_num:>5} | {text}")

        if resolution_line:
            print(f"\n{resolution_line}")

        print(f"\n## Tail (last {tail_lines} lines)")
        shown = lines[-tail_lines:] if len(lines) > tail_lines else lines
        start = total - len(shown) + 1
        for i, line in enumerate(shown):
            print(f"  {start + i:>5} | {line}")
    elif display_status == "failure":
        # Nothing matched on a job that failed — say so, do not just print a
        # tail and let the reader infer the log was clean (#453/#445).
        _print_unmatched_failure(job_id, display_status, patterns, lines, total)
    else:
        # Job didn't fail — just show tail
        shown = lines[-tail_lines:] if len(lines) > tail_lines else lines
        skipped = total - len(shown)
        if skipped > 0:
            print(f"({skipped} lines skipped)")
        print()
        start = total - len(shown) + 1
        for i, line in enumerate(shown):
            print(f"  {start + i:>5} | {line}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
