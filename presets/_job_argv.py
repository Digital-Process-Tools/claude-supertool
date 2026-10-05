























from __future__ import annotations

import re

import _digits  
















_PYTEST_FAILED_LINE_RE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)






MODES = ("fail", "errors", "raw", "grep", "artifacts", "artifact")



_DIGITS = _digits.DIGITS


def refuse_job_id(op: str, forge: str, job_id: str) -> str:






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




    if not mode or mode in MODES:
        return ""
    return (
        f"ERROR: {op} does not have a {mode!r} mode.\n"
        f"Nothing was read. This used to fall through to the default view — "
        f"metadata plus the log tail, exit 0 — which reads as an answer to the "
        f"question you asked rather than as a mode that was never applied.\n"
        f"Modes: fail (alias errors), raw, grep, artifacts, artifact. Usage: "
        f"{op}:JOB_ID:fail | {op}:JOB_ID:raw[:-N|:START[:END]] | "
        f"{op}:JOB_ID:grep:PATTERN | {op}:JOB_ID:artifacts | "
        f"{op}:JOB_ID:artifact:PATH"
    )


def refuse_job_ids(op: str, forge: str, job_ids: str) -> tuple[list[str], str]:












    pieces = job_ids.split(",")
    if not job_ids or any(not p for p in pieces):
        return [], (
            f"ERROR: {op} takes one or more comma-separated numeric job ids "
            f"and got {job_ids!r}. Nothing was read.\n"





            f"Usage: {op}:ID1[,ID2,...]"
        )
    seen: list[str] = []
    for piece in pieces:
        refusal = refuse_job_id(op, forge, piece)
        if refusal:
            return [], refusal
        if piece not in seen:
            seen.append(piece)
    return seen, ""


def grep_pattern(op: str, tokens: list[str]) -> tuple[str, str]:










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


def artifact_path(op: str, tokens: list[str]) -> tuple[str, str]:









    path = ":".join(tokens)
    if len(tokens) < 2:
        return path, ""
    return path, (
        f"Note: this path contains ':', which is also {op}'s own argument "
        f"separator, so core delivered it as {len(tokens)} pieces and they "
        f"were rejoined — the path was read as {path!r}. Nothing follows the "
        f"path in this op, so that is the only reading that keeps it whole; "
        f"check that it says what you meant."
    )


def classify_unit_test_failure(text: str) -> "str | None":






























    for match in _PYTEST_FAILED_LINE_RE.finditer(text):
        token = match.group(1)
        if "::" in token:
            return "MANUAL"
        segments = token.split(".")
        if len(segments) >= 2 and any(
            seg.lower().startswith("test") for seg in segments
        ):
            return "MANUAL"
    return None
