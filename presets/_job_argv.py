"""Argv shape for the two job-log presets (#1145).

`gh-job` and `gl-job` take the same three-part op — `OP:ID[:MODE[:ARG...]]` —
and core hands it to them as argv after splitting the op string on every `:`.
Three readings of that argv used to end in a confident answer to a question
nobody asked:

* a MODE token the preset did not recognise fell through to the default render
  and exited 0, so a log tail read as "the slice you asked for found nothing";
* an ID with trailing non-digits was rendered in the header as the job being
  read — GitHub's REST API coerces `actions/jobs/123ep` to job 123 and answers
  200, so the API cannot be relied on to reject one;
* a `:` inside a grep PATTERN split it into two argv entries and only the first
  was used.

The resolutions here are the ones the codebase already reached elsewhere rather
than new ones: refuse and name what broke, and for the colon carry the pattern
and echo how it was read, which is core `grep`'s `_colon_split_hint`. Refusing
`:` outright was considered and rejected — nothing follows PATTERN in either
op, so rejoining is not a guess between two readings, it is the only one.

`|` is deliberately absent from all of this. Core shell-quotes each part before
substituting `{args}`, so alternation reaches the preset intact and always did.
"""
from __future__ import annotations

import re

# `raw` is here too, though its own START/END parsing lives in each preset.
MODES = ("fail", "errors", "raw", "grep")

#: `\Z` and not `$`: Python's `$` also matches immediately before a final
#: newline, so `^[0-9]+$` accepted `"5\n"` — through the one guard whose whole
#: purpose is to refuse before anything is fetched (#1188).
_DIGITS = re.compile(r"^[0-9]+\Z")


def refuse_job_id(op: str, forge: str, job_id: str) -> str:
    """Message refusing a job id that is not one. Empty string when it is.

    `str.isdigit()` is not the test: it accepts Arabic-Indic digits and
    superscripts, neither of which is an id either forge will answer for, and a
    check that passes text the API then rejects is worse than no check.
    """
    if _DIGITS.match(job_id):
        return ""
    stray = "".join(sorted({c for c in job_id if not _DIGITS.match(c)}))
    digits = "".join(c for c in job_id if _DIGITS.match(c)) or "JOB_ID"
    return (
        f"ERROR: {op} takes a numeric job id and got {job_id!r} "
        f"(not a digit: {stray!r}).\n"
        f"Nothing was read. A non-numeric id is the tell that the op string was "
        f"mangled before it arrived, and it has to be caught here, because "
        f"{forge} answers 200 for a numeric id with trailing text by coercing "
        f"it back to the number. Rendering it in the header would publish a "
        f"corrupted identifier as the job that was read.\n"
        f"Re-run with the digits alone: {op}:{digits}"
    )


def refuse_mode(op: str, mode: str) -> str:
    """Message refusing an unrecognised MODE token. Empty string when it is one.

    An empty MODE is the bare `OP:ID` form and is not a mode at all.
    """
    if not mode or mode in MODES:
        return ""
    return (
        f"ERROR: {op} does not have a {mode!r} mode.\n"
        f"Nothing was read. This used to fall through to the default view — "
        f"metadata plus the log tail, exit 0 — which reads as an answer to the "
        f"question you asked rather than as a mode that was never applied.\n"
        f"Modes: fail (alias errors), raw, grep. Usage: "
        f"{op}:JOB_ID:fail | {op}:JOB_ID:raw[:-N|:START[:END]] | "
        f"{op}:JOB_ID:grep:PATTERN"
    )


def grep_pattern(op: str, tokens: list[str]) -> tuple[str, str]:
    """`(pattern, disclosure)` for the argv entries right of `grep`.

    Core split the op on every `:`, so a pattern containing one arrives as
    several entries. Nothing follows PATTERN in either op, so they rejoin with
    the `:` that separated them and the whole pattern survives.

    The disclosure is empty for the ordinary one-token case. Saying it on every
    grep would make it worth nothing on the call that needs it — the same
    reason `_colon_split_hint` returns nothing for an ordinary typo.
    """
    pattern = ":".join(tokens)
    if len(tokens) < 2:
        return pattern, ""
    return pattern, (
        f"Note: this pattern contains ':', which is also {op}'s own argument "
        f"separator, so core delivered it as {len(tokens)} pieces and they were "
        f"rejoined — the pattern was read as /{pattern}/. Nothing follows the "
        f"pattern in this op, so that is the only reading that keeps it whole; "
        f"check that it says what you meant."
    )
