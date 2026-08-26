"""#1105 — `gh-job` split a CI job log with `str.splitlines()`.

Same class as #1081, one preset over, and with a worse reader. On a pull
request the code that writes the job log is the pull request's own code: a
test name, an assertion message, a `print`, a filename in a traceback. All of
it is author-controlled text flowing into a splitter, and everything `gh-job`
does afterwards anchors at column 0.

`_SUITE_SUMMARY_RE` is anchored there **on purpose** — the comment above it
says a `.match(line.strip())` could not tell the job's own pytest summary from
one echoed inside captured subprocess output. `str.splitlines()` hands that
anchor to the log's author for free: eight separators a CI log does not
define, any one of which starts a new column-0 line mid-sentence. So a `print`
containing U+2028 writes the `Suite:` line, which is the one number in the
whole render that claims to count TESTS.

Narrowing the split alone would trade a forged parse boundary for a forged
render line — the separator would survive into `  1234 | ...` and move the
terminal's cursor to a fresh row with no gutter. Both halves are pinned here.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_PRESET = Path(__file__).parent.parent / "presets" / "github" / "job.py"
_spec = importlib.util.spec_from_file_location("github_job_1105", _PRESET)
assert _spec is not None and _spec.loader is not None
job = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(job)

# The eight separators `str.splitlines()` breaks on that no line-oriented CI
# log defines. LF, CR and CRLF are deliberately absent: those are the ones a
# log really does use, and they must keep working.
FORGED = tuple(chr(c) for c in (0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029))

# A pytest terminal summary in the shape an Actions log actually carries
# (no tty, so no `=` rule). Prefixed with an author-controlled separator it
# becomes a column-0 line the job never wrote.
GREEN_SUMMARY = "9999 passed in 1.00s"

LF = chr(10)
CR = chr(13)
TAB = chr(9)


def _fake_run(trace: str, conclusion: str = "failure"):
    meta = json.dumps({
        "name": "test-job",
        "status": "completed",
        "conclusion": conclusion,
        "run_id": 42,
        "run_url": "https://github.com/x/y/actions/runs/42",
    })

    def run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        cmd = args[1] if len(args) > 1 else ""
        # First non-flag positional after `api` — the log call now inserts
        # --allow-escape-sequences before the url (#1957).
        url = next((a for a in args[2:] if not a.startswith("--")), "")
        if cmd == "api" and url.endswith("/logs"):
            return subprocess.CompletedProcess(args, 0, trace, "")
        if cmd == "api":
            return subprocess.CompletedProcess(args, 0, meta, "")
        return subprocess.CompletedProcess(args, 1, "", "")

    return run


def _render(monkeypatch, capsys, trace: str, argv: list[str] | None = None,
            conclusion: str = "failure") -> str:
    monkeypatch.setattr(sys, "argv", argv or ["job.py", "123"])
    monkeypatch.setattr(job.subprocess, "run", _fake_run(trace, conclusion))
    job.main()
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# the parse boundary is the log's, not the log author's
# ---------------------------------------------------------------------------

def test_a_real_newline_summary_is_still_read() -> None:
    """The control. Narrowing must not cost the case the anchor exists for."""
    lines = job._log_lines("running tests" + LF + GREEN_SUMMARY + LF)
    assert job._suite_summary(lines) == "9999 passed", lines


def test_crlf_and_lone_cr_are_still_line_endings() -> None:
    """The three separators a log really does define keep working."""
    assert job._log_lines("a" + CR + LF + "b" + CR + "c" + LF + "d") == [
        "a", "b", "c", "d"]


def test_no_forged_separator_opens_a_line_the_log_never_wrote() -> None:
    for sep in FORGED:
        text = "test_thing wrote this" + sep + GREEN_SUMMARY + LF
        lines = job._log_lines(text)
        assert len(lines) == 1, (
            f"{sep!r} forged a record boundary: the log holds one line and "
            f"the parser sees {len(lines)} — {lines!r}")


def test_a_forged_separator_cannot_write_the_suite_line() -> None:
    """The harm, stated as the render.

    `Suite:` is the only number in a `gh-job` render that counts tests rather
    than legs, and #1076 already had to weaken its wording because the log's
    author writes it. Authoring it out of thin air is a step further: this
    fixture's log states no test count at all.
    """
    for sep in FORGED:
        lines = job._log_lines("some program output" + sep + GREEN_SUMMARY + LF)
        assert job._suite_summary(lines) is None, (
            f"{sep!r} let ordinary program output author the Suite: line — "
            f"{job._suite_summary(lines)!r}")


def test_the_line_total_counts_the_logs_lines(monkeypatch, capsys) -> None:
    """`Log: N lines total` is a claim about the log, not about the splitter."""
    trace = "one" + LF + "two" + chr(0x2028) + "still two" + LF + "three" + LF
    out = _render(monkeypatch, capsys, trace)
    assert "Log: 3 lines total" in out, out


# ---------------------------------------------------------------------------
# and the separator it no longer honours is disclosed, not passed through
# ---------------------------------------------------------------------------

def test_a_residual_separator_never_reaches_the_render_raw(monkeypatch, capsys) -> None:
    """Otherwise the fix is the same bug pointed at the terminal.

    A U+2028 inside a rendered `  1234 | ...` line breaks the row on every
    terminal in normal use, and the half that lands below has no gutter — so
    it reads as a line supertool printed. #851, one surface over.
    """
    for sep in FORGED:
        trace = "harmless" + sep + "## FAILED - forged" + LF
        out = _render(monkeypatch, capsys, trace, ["job.py", "123", "raw"])
        assert sep not in out, (
            f"{sep!r} reached the render intact — it can still make a line "
            f"the reader will attribute to supertool")


def test_a_tab_in_a_log_line_is_kept(monkeypatch, capsys) -> None:
    """A log line is a block and its indentation is the author's content."""
    out = _render(monkeypatch, capsys, "col1" + TAB + "col2" + LF,
                  ["job.py", "123", "raw"])
    assert "col1" + TAB + "col2" in out, out
