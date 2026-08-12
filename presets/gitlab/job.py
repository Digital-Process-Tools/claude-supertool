#!/usr/bin/env python3
"""GitLab CI job log via glab CLI.

Shows job metadata + smart log output:
1. Searches for error patterns and shows context around them
2. Falls back to last N lines if no patterns found

Config via SUPERTOOL_ env vars (set from .supertool.json):
  SUPERTOOL_LINES           — tail lines (default 80)
  SUPERTOOL_ERROR_PATTERNS  — comma-separated substrings to search. Default:
                              ERROR,FAILURES!,Fatal,Failed asserting,🪪,
                              notSubtype,argument.type,return.type — the bare
                              `ERROR` there is why GitLab's own terminal line
                              needs discounting (see _BOILERPLATE).
  SUPERTOOL_ERROR_CONTEXT   — lines of context around each error match (default 8)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import env_int  # noqa: E402  (the one numeric-knob reader)
import _branch_locale  # noqa: E402  (where the branch is checked out — shared by all five #850)
import _untrusted  # noqa: E402  (an MR's branch, title and author are the opener's text — #965)
import _job_argv  # noqa: E402  (the argv shape both job presets share — #1145)
import _repo_target  # noqa: E402  (the project this call is about, if not cwd's — #676)


def _local_branch_check(source: str, actionable: bool = True) -> str:
    """Return a one-line local-branch-vs-source check for output.

    Empty string when not in a git repo, detached HEAD, or source is empty.
    Used after the 'Branch:' line to flag editing on the wrong branch.

    `actionable=False` keeps the mismatch and drops the `git-checkout`
    suggestion (#531). The read-only sub-ops — `raw`, `fail`/`errors`, `grep` —
    are what a radar session runs, and moving HEAD is the one action such a
    session must never take: fixes go to a worktree so the checkout stays put.
    Suggesting it on every call is not noise, it is wrong advice at volume.

    Only the imperative is withheld, never the state. The line is printed
    either way and always says which of ✓ / MISMATCH it is, so a missing
    checkout command can never be misread as "you are on the right branch" —
    which is the failure mode a blanket suppression would have introduced.

    Defaults to True: a caller that has not thought about it gets the hint,
    because the cost of an unwanted suggestion is smaller than the cost of a
    silently missing one.

    Delegated to `_branch_locale` (#850), which adds the third state #531 could
    not see: a branch held by a linked worktree is neither a match nor a
    MISMATCH. `actionable` still governs only the imperative.
    """
    return _branch_locale.check(source, actionable)


def _format_error(stderr: str, resource: str, identifier: str) -> str:
    """Classify glab errors into actionable messages for LLMs."""
    s = stderr.lower()
    if "404" in s or "not found" in s or "could not resolve" in s:
        return (f"ERROR: {resource} #{identifier} not found "
                f"{_repo_target.not_found_scope()}. Check the ID. Use "
                "gl-pipeline to list jobs first, then gl-job with the job ID.")
    if "401" in s or "unauthorized" in s or "glpat_" in s or "authenticate" in s or "bad token" in s or "token expired" in s:
        return "ERROR: glab not authenticated. Run: glab auth login"
    if "403" in s or "forbidden" in s:
        return f"ERROR: permission denied for {resource} #{identifier}. Check your GitLab access token permissions."
    # The remote host wrote this text — flattened, never relayed raw (#1485).
    return (f"ERROR: glab failed for {resource} #{identifier}: "
            f"{_untrusted.flat(stderr.strip())}")


def _get_config() -> dict:
    """Read config from SUPERTOOL_ env vars."""
    return {
        "lines": env_int("SUPERTOOL_LINES", 80, minimum=1),
        "error_patterns": os.environ.get(
            "SUPERTOOL_ERROR_PATTERNS",
            # ERROR/FAIL: generic. 🪪: phpstan identifier marker (every phpstan error).
            # notSubtype/argument.type/return.type: phpstan identifiers as text fallback.
            "ERROR,FAILURES!,Fatal,Failed asserting,🪪,notSubtype,argument.type,return.type"
        ).split(","),
        "error_context": env_int("SUPERTOOL_ERROR_CONTEXT", 8, minimum=0),
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


# Lines that state a *reason* for the failure. A stack trace is the consequence;
# the line saying why sits above it (#444), and some failures never produce a
# trace at all (#445). These are matched in addition to — and independently of —
# the configured error_patterns, so a tighter per-job pattern table can narrow
# the surrounding noise without being able to hide the cause.
# Set GL_JOB_CAUSE_MARKERS=0 to disable.
_CAUSE_MARKERS = [
    re.compile(r"^\s*Caused by\b"),
    # `[\w\\]*`, not `+`: a bare `Error: ...` has nothing before it, and that is
    # node's and Playwright's usual shape. The `+` form is why job 7021139's
    # `Error: JS errors detected:` was invisible on a job whose per-job pattern
    # table had narrowed the set to PHPUnit (#1097) — the marker list is the
    # floor that narrowing cannot remove, so a hole in it is a hole in the floor.
    re.compile(r"[\w\\]*(?:Exception|Error):\s"),
    # The MySQL client: `ERROR 2026 (HY000): TLS/SSL error: ...`, the cause on
    # line 108 of job 7125000 and the shape of no configured pattern.
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
    """Indices of lines that state a reason for the failure."""
    if os.environ.get("GL_JOB_CAUSE_MARKERS", "1") == "0":
        return []
    return [
        i for i, line in enumerate(lines)
        if any(rx.search(line) for rx in _CAUSE_MARKERS)
    ]


def _last_section(lines: list[str]) -> str | None:
    """Name of the last CI section the runner entered — i.e. the failing step."""
    for line in reversed(lines):
        match = _SECTION_START.match(line)
        if match:
            return match.group(1)
    return None


# Lines GitLab or its runner writes on **every** failed job. The default
# `error_patterns` contain the bare substring `ERROR`, so the terminal line
# alone is enough to anchor a block; `error_context` then drew the section
# markers and the cleanup line in around it, and the result was the six-line
# "All error blocks" that job 7021139 produced while the real cause — a
# Playwright assertion — never appeared (#1097).
#
# Only the first entry can fire under the default patterns: classification
# looks at anchors, and nothing in the default set matches a bare
# `section_start:` or `Cleaning up project directory`. The other two are for a
# project whose `error_patterns` do — someone hunting teardown by name gets the
# same overclaim otherwise — and are inert, not dead, until then.
#
# Only `exit code N`. `ERROR: Job failed (system failure): ...` and
# `ERROR: Job failed: execution took longer than ...` DO say why the job died,
# and discounting those would trade this loud bug for a quiet one.
#
# `search`, not `match`: the trace on job 7125000 carries a stream prefix
# (`00O `) ahead of the line which the ANSI cleanup above does not remove, so
# anchoring at the start of the line would miss it on exactly the log this was
# filed from.
_BOILERPLATE = [
    re.compile(r"\bERROR: Job failed: exit code \d+\s*$"),
    re.compile(r"\bsection_(?:start|end):\d+:"),
    re.compile(r"\bCleaning up project directory"),
]


def _is_boilerplate(line: str) -> bool:
    """True for a line that is present because the job ended, not because it failed."""
    return any(rx.search(line) for rx in _BOILERPLATE)


# `:fail` fits exactly one status. Written as the complement rather than as a
# list of the others (`canceled`, `skipped`, `manual`, ...) so a status GitLab
# adds later, or a job still `running`, lands on the disclosure by default
# instead of on the silent overclaim. The GitHub twin holds the same constant
# with the value `failure` (#916): the vocabularies differ, the rule does not,
# and `tests/test_gl_job_fail_honesty_1095_1097.py` drives both to keep it that
# way.
_FAIL_SELECTOR_FITS = "failed"


def _selection_mismatch(job_status: str, job_id: str) -> str:
    """Say that error-block selection cannot answer for this job, and what can.

    #1095. `## All error blocks (N lines matched, no tail truncation)` is a
    claim of completeness: the reader is told the selector found everything
    there was and that nothing was cut. Both are true of the *selector* and
    neither is true of the *log* on a job that was killed before it produced
    the failure it was killed during — its diagnostics sit in teardown and in
    the tail, where no error pattern can reach them.

    The op already knows: `Status: canceled` is printed from this same value
    nine lines above the header that contradicts it.

    Disclosure rather than a wider pattern set, deliberately — a pattern set
    cannot be complete, so a wider one that still misses produces a longer and
    more confident-looking block. And the matched lines are still printed:
    trading a loud wrong answer for no answer is the same defect reversed.
    """
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
        f">   ./supertool 'gl-job:{job_id}:raw:-80'          # tail, where a "
        f"cancellation's evidence usually sits\n"
        f">   ./supertool 'gl-job:{job_id}:grep:PATTERN'     # the whole trace "
        f"is still searchable\n"
    )


# The refusal headers, named rather than spelled inline (#1106). The first
# used to read `## FAILED — no error pattern matched`, which is word-for-word
# the clause `gap_marker` uses about the lines it elided *inside a successful
# classification* — one phrase, two renders, opposite meanings. See the twin
# in `presets/github/job.py` for the full argument and for why this is a
# duplicated constant pinned by a test rather than a shared import.
UNCLASSIFIED_HEADER = "## FAILED — supertool could not classify this job"
BOILERPLATE_ONLY_HEADER = (
    "## FAILED — only boilerplate matched, no cause identified")


def _print_unmatched_failure(
    job_id: str, job_status: str, patterns: list[str], lines: list[str], total: int,
    discounted: list[tuple[int, str]] | None = None,
) -> None:
    """Report a failed job the patterns could not classify — never as silence.

    `## No error patterns matched` on a job GitLab calls *failed* reads as green
    (#445), which is the worst output a failure tool can produce. A failed job
    always has a reason; not finding it is a gap in this tool, so the output
    says exactly that and hands back the raw evidence.
    """
    tail_n = env_int("GL_JOB_UNMATCHED_TAIL_LINES", 40, minimum=1)
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
        for line_num, text in discounted:
            print(f"  {line_num:>5} | {text}")
    section = _last_section(lines)
    if section:
        print(f"Last step entered: {section}")
    tail = lines[-tail_n:] if len(lines) > tail_n else lines
    print(f"\n## Log tail (last {len(tail)} lines of {total})")
    start = total - len(tail) + 1
    for i, line in enumerate(tail):
        print(f"  {start + i:>5} | {line}")
    print(
        f"\nNext:  ./supertool 'gl-job:{job_id}:raw'  or  "
        f"'gl-job:{job_id}:grep:PATTERN'  — the whole trace is still there."
    )


_PHPUNIT_BLOCK_START = re.compile(r'^\s*\d+\)\s+\S+::\S+')
_PHPUNIT_BLOCK_SUMMARY = re.compile(
    r'^\s*(FAILURES!|ERRORS!|WARNINGS!|OK \(|OK, but|There (was|were) \d+)'
)


def _phpunit_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """Locate PHPUnit failure blocks as (start, end) inclusive 0-based indices.

    A block runs from its `N) Class::method` header to the last non-blank line
    before the next header or the run summary — typically the trailing
    `/path/File.php:LINE` frames.
    """
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
    """Widen any touched PHPUnit failure to its whole block, in place.

    The assertion diff / rendered artifact sits in the middle of the block, so
    a pattern window centred on `Failed asserting` drops exactly the evidence.
    Blocks longer than block_max keep their head and tail; the elision is then
    reported by the gap marker.

    total_max budgets expansion across all blocks. Past it whole failures are
    dropped rather than gutted — a dropped block keeps whatever its pattern
    windows already selected, so no input returns less than it did before.
    Returns (dropped, touched) for the caller to announce.
    """
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
    """The trace's own lines — LF / CR / CRLF — with nothing else honoured.

    #1119, and #1105 one preset over: the same defect in the other private
    twin, found by the #1105 agent and filed rather than swept, because a
    title naming one preset must not carry a change to two.

    `str.splitlines()` breaks on eight separators no CI trace defines, and
    everything below this anchors at column 0. `_SECTION_START` feeds
    `Last step entered:`, which is supertool's own claim about WHICH step
    failed and the first thing a reader of a refusal acts on;
    `_PHPUNIT_BLOCK_START` decides what counts as a failure block and so what
    the render shows and what it elides; and the tail header's `of {total}` is
    arithmetic over this list. A GitLab trace is written by the branch's own
    `.gitlab-ci.yml` and the branch's own code, so `str.splitlines()` handed
    all three to the trace's author: an `echo` carrying U+2028 opened a
    column-0 line mid-sentence and named the failing step.

    Narrowing the split alone would trade the forged parse boundary for a
    forged *render* line — the separator would survive into `  1234 | ...` and
    move the terminal to a fresh row with no gutter, which reads as a line
    supertool wrote (#851, one surface over). So the separators this split no
    longer honours are disclosed as pictures on the way through, and that
    pairing is what makes the narrowing a fix rather than a quieter version of
    the same bug.

    Tabs are kept: a trace line is a block and its indentation is the author's
    content, which is the same call `_untrusted.scrub` makes for a fence.
    """
    return [_untrusted.visible(line, keep=chr(9))
            for line in _untrusted.split_lines(log)]


def gap_marker(n_lines: int) -> str:
    """The line that stands where the op cut, saying so and saying how much.

    Byte-identical to `presets/github/job.py:gap_marker`, and that is the
    point (#1066). These two files are private twins: the GitLab side got a
    counted marker in #409 and the GitHub side rediscovered the same defect
    640 issues later as #1050, fixing it with different words. Two vocabularies
    for one idea, in two ops a reader uses interchangeably depending on which
    forge they are looking at, is worse than either wording alone — someone who
    learns one and meets the other has to work out whether the difference means
    something.

    The longer GitHub wording won because its extra clause is the one that was
    being misread: `... ` alone does not distinguish *this op elided lines*
    from *the log itself was truncated*, and a whole issue (#1014) was filed
    against the second reading of the first fact. The cost is a longer line
    when a render carries several markers; that is the trade, taken knowingly.

    `tests/test_gl_job_gap_marker_twins_1066.py` compares the twins directly,
    so the next divergence fails loudly instead of sitting unmirrored.
    """
    unit = "line" if n_lines == 1 else "lines"
    return (f"... ({n_lines} {unit} elided by this op — no error pattern "
            f"matched them; the log itself is intact)")


def _pattern_anchors(lines: list[str], patterns: list[str]) -> list[int]:
    """Indices of lines a configured error pattern matched directly.

    The anchors, not the context windows drawn around them: what the selector
    *found*, as against what it decided to print. #1097 needs the distinction —
    a block of eight lines can rest on one anchor, and whether that anchor says
    anything is the question the header answers wrongly today.
    """
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
    """Find lines matching error patterns and return them with context.

    Returns list of (line_number, line_text) tuples, deduplicated and sorted.

    Gaps carry `gap_marker`, so every withheld line is accounted for by exactly
    one marker — including, when `trailing_gap` is set, the run after the last
    shown line (#1066).

    `trailing_gap` is off by default and **must stay that way for the default
    render**, for the reason the GitHub twin gives: that path prints these
    sections and then `## Tail (last N lines)` immediately below, which holds
    most of the very lines a trailing marker would have declared elided. Only
    `:fail`, which prints blocks and nothing else, can truthfully claim it.
    """
    matches: set[int] = set()
    for i in _pattern_anchors(lines, patterns):
        # Add the match and surrounding context
        for j in range(max(0, i - context), min(len(lines), i + context + 1)):
            matches.add(j)

    # Cause markers anchor on the line that states *why*, not on the wreckage it
    # produced. They run whatever the configured patterns are, and get their
    # context asymmetrically: a cause is followed by its message body (the
    # indented exception text, the `Exit Code:` line), so the window leans down.
    cause_before = env_int("GL_JOB_CAUSE_CONTEXT_BEFORE", 2, minimum=0)
    for i in _cause_lines(lines):
        for j in range(max(0, i - cause_before), min(len(lines), i + context + 1)):
            matches.add(j)

    if not matches:
        return []

    dropped, touched = _expand_phpunit_blocks(
        lines,
        matches,
        env_int("GL_JOB_PHPUNIT_BLOCK_MAX_LINES", 500, minimum=1),
        env_int("GL_JOB_PHPUNIT_TOTAL_MAX_LINES", 2000, minimum=1),
    )

    result: list[tuple[int, str]] = []
    sorted_matches = sorted(matches)
    prev = -1
    for idx in sorted_matches:
        gap = idx - prev - 1
        if gap > 0:
            result.append((-1, gap_marker(gap)))
        result.append((idx + 1, lines[idx]))  # 1-indexed line numbers
        prev = idx

    # Anything after the last shown line is withheld too, and the reader
    # deciding whether to call `:raw` needs that number as much as the middle
    # ones. Only `:fail` may say it — see the docstring.
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
    # `-1`, not `-2`. At `-2` the first hit (index 0) satisfies `idx > prev + 1`
    # and the render opened with an elision marker covering zero lines, above a
    # line printed directly beneath it — an absence the op invented. The GitHub
    # twin was corrected by #1050; this copy was not, and sat that way for 640
    # issues because nothing compared them (#1066).
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
    if len(sys.argv) < 2:
        print("ERROR: usage: job.py JOB_ID [raw [START [END]]]")
        return 1

    # #1145 — argv is what core made of the op string, and three shapes of it
    # used to reach a render. Refuse before anything is fetched: an id or a mode
    # the op cannot serve must never appear in output that looks like a read.
    job_id = sys.argv[1]
    refusal = _job_argv.refuse_job_id("gl-job", "GitLab", job_id)
    if refusal:
        print(refusal)
        return 1
    mode = sys.argv[2] if len(sys.argv) > 2 else ""
    refusal = _job_argv.refuse_mode("gl-job", mode)
    if refusal:
        print(refusal)
        return 1
    raw_mode = mode == "raw"
    errors_mode = mode in ("errors", "fail")
    grep_mode = mode == "grep"
    # Everything right of `grep` is the pattern — this op takes no argument
    # after it — so the pieces core split on ':' rejoin rather than the tail
    # being dropped.
    grep_pattern, grep_note = (
        _job_argv.grep_pattern("gl-job", sys.argv[3:]) if grep_mode else (None, "")
    )
    # Read-only sub-ops: you are inspecting a job, not preparing to work on its
    # branch. The branch check still prints; only the checkout advice does not.
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
                # A negative START is the tail form: `raw:-40` is the last 40
                # lines. `raw` is reached as a fallback when `:fail` was
                # unhelpful, and what a caller wants at that point is almost
                # always the end of the log — which previously could not be
                # asked for without first spending a call to learn the total.
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
    config = _get_config()
    tail_lines = config["lines"]

    # 1. Get job metadata
    try:
        meta_result = subprocess.run(
            ["glab", "api",
             _repo_target.gl_api_path(f"projects/:id/jobs/{job_id}")],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("ERROR: glab not found — install from https://gitlab.com/gitlab-org/cli")
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
        except json.JSONDecodeError:
            pass

    # 2. Get job trace (log)
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

    # Clean ANSI escape codes
    log = result.stdout
    log = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', log)
    log = re.sub(r'\x1b\]8;[^;]*;[^\x1b]*\x1b\\', '', log)

    lines = _log_lines(log)
    total = len(lines)

    # Header
    duration_str = f"{job_duration:.0f}s" if job_duration else "?"
    print(f"# Job #{job_id} — {_untrusted.flat(job_name)}")
    print(f"Stage: {_untrusted.flat(job_stage)} | Status: {job_status} | "
          f"Duration: {duration_str}")

    # Parse ref to show branch or MR (with details)
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

            # Extract related issue from description (#NUMBER pattern)
            mr_desc = mr_data.get("description") or ""
            issue_match = re.search(r'#(\d{4,})', mr_desc)
            issue_ref = f"#{issue_match.group(1)}" if issue_match else ""

            print(f"\n## MR !{mr_iid} — {_untrusted.flat(mr_title)}")
            print(f"State: {mr_state} | Author: {_untrusted.flat(mr_author)}")
            # Raw `mr_branch` still goes to `_local_branch_check` below: it
            # applies its own `flat` and #924's refname rule, and a rewritten
            # name would name a ref that does not exist.
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

    # 3. Raw mode — dump (sliced) trace, skip filters
    if raw_mode:
        if total == 0:
            # Distinct from an out-of-range request: this is an absence in the
            # world, not one the op produced. Saying "nothing to show" for both
            # is what cost the round-trip in #487.
            print("\n## Raw — the log is empty (0 lines)")
            return 0
        if raw_tail is not None:
            width = min(raw_tail, total)
            start, end = total - width + 1, total
        else:
            start = raw_start if raw_start is not None else 1
            end = raw_end if raw_end is not None else total
            if start > total:
                # The bound is already printed one line above, so declining
                # here only buys the caller a second call to re-read it. Return
                # the tail of the width that was asked for — and say plainly
                # that these are not the lines requested, because a clamp
                # nobody is told about hands back different data than was
                # asked for, which is the same disease one level down.
                width = (end - start + 1) if raw_end is not None else tail_lines
                width = max(1, min(width, total))
                print(f"\n## Raw — requested {start}-{end} is past end of log "
                      f"({total} lines); showing the last {width} lines instead")
                start, end = total - width + 1, total
            end = min(end, total)
        # Cap raw dumps that exceed GL_JOB_RAW_MAX_LINES, regardless of whether
        # the user passed an explicit START:END. A user can still defeat the
        # cap by raising the env var, but a 99999-line slice no longer
        # silently dumps 10MB into validator output.
        cap = env_int("GL_JOB_RAW_MAX_LINES", 5000, minimum=1)
        shown = lines[start - 1:end]
        if len(shown) > cap:
            kept = shown[:cap]
            hint = (
                "narrow the slice or raise GL_JOB_RAW_MAX_LINES=N"
                if raw_end is not None
                else "pass START:END to slice further, or set GL_JOB_RAW_MAX_LINES=N"
            )
            print(
                f"\n## Raw lines {start}-{start + cap - 1} of {total} "
                f"[CAPPED at {cap} — {hint}]"
            )
            for i, line in enumerate(kept):
                print(f"  {start + i:>5} | {line}")
            return 0
        print(f"\n## Raw lines {start}-{start + len(shown) - 1} of {total}")
        for i, line in enumerate(shown):
            print(f"  {start + i:>5} | {line}")
        return 0

    # 3b. Grep mode — ad-hoc regex over the trace, context + gap markers.
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
                        env_int("GL_JOB_GREP_MAX_BYTES", 65536, minimum=1),
                        "GL_JOB_GREP_MAX_BYTES", shown_pattern, ctx)
        return 0

    # 4. Try error pattern search first. Per-job-name table (if configured)
    # picks tighter patterns + a resolution op; else the flat default applies.
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

    # What the selector landed on, as against the windows it drew around it. A
    # selection whose every anchor is a line GitLab writes on every failed job
    # is not a classification (#1097) — it is the same absence the empty case
    # already reports honestly, wearing a header that claims otherwise.
    anchors = sorted(set(_pattern_anchors(lines, patterns)) | set(_cause_lines(lines)))
    boilerplate_only = bool(anchors) and all(_is_boilerplate(lines[i]) for i in anchors)
    discounted = [(i + 1, lines[i]) for i in anchors] if boilerplate_only else None

    # errors mode — dump ALL matched blocks, no tail cap
    if errors_mode:
        mismatch = _selection_mismatch(job_status, job_id)
        if not error_sections or (job_status == "failed" and boilerplate_only):
            if job_status == "failed":
                _print_unmatched_failure(job_id, job_status, patterns, lines,
                                         total, discounted)
            else:
                print("\n## No error patterns matched")
                if mismatch:
                    print(mismatch)
            return 0
        matched_count = len([e for e in error_sections if e[0] > 0])
        if mismatch:
            # #1095. The old header read `All error blocks (N lines matched, no
            # tail truncation)` on a canceled job — two claims true of the
            # SELECTOR and false of the LOG. `Status: canceled` is printed above
            # from this same value, so the op held the fact and applied the
            # selector anyway.
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

    if error_sections and job_status == "failed" and not boilerplate_only:
        print(f"\n## Error context ({len([e for e in error_sections if e[0] > 0])} lines matched)")
        for line_num, text in error_sections:
            if line_num == -1:
                print(text)  # gap marker
            else:
                print(f"  {line_num:>5} | {text}")

        if resolution_line:
            print(f"\n{resolution_line}")

        # Also show tail for full context
        print(f"\n## Tail (last {tail_lines} lines)")
        shown = lines[-tail_lines:] if len(lines) > tail_lines else lines
        start = total - len(shown) + 1
        for i, line in enumerate(shown):
            print(f"  {start + i:>5} | {line}")
    elif job_status == "failed":
        # Nothing matched on a job that failed — say so, do not just print a tail
        # and let the reader infer the log was clean (#445). Reached now also
        # when every anchor was boilerplate (#1097) — same absence, and the
        # discounted lines travel with it.
        _print_unmatched_failure(job_id, job_status, patterns, lines, total,
                                 discounted)
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
