"""#1119 - `gl-job` split a CI trace with `str.splitlines()`.

#1105 in the twin, filed separately because the two job presets are private
twins by design: a preset runs with `presets/` on `sys.path` and cannot import
the core, so `gap_marker` and friends are duplicated on purpose, and a defect
found in one does not reach the other unless someone files it.

The GitLab reader is anchored at column 0 in three places that matter:

* `_SECTION_START` feeds `Last step entered:` - supertool's own claim about
  WHICH CI step failed, printed at the top of a refusal a reader uses to
  decide where to look.
* `_PHPUNIT_BLOCK_START` decides what counts as a failure block, and therefore
  what the render shows and what it elides.
* `## Log tail (last N lines of TOTAL)` is arithmetic over the split.

A GitLab CI trace is written by the branch's own `.gitlab-ci.yml` and the
branch's own code. `str.splitlines()` breaks on eight separators no CI trace
defines, so an `echo` carrying U+2028 opened a column-0 line the trace never
wrote - and named the failing step.

Narrowing the split alone would trade a forged parse boundary for a forged
render line: the separator would survive into `  1234 | ...` and move the
terminal to a fresh row with no gutter, which reads as a line supertool wrote
(#851). Both halves are pinned here.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_PRESET = Path(__file__).parent.parent / "presets" / "gitlab" / "job.py"
_spec = importlib.util.spec_from_file_location("gitlab_job_1119", _PRESET)
assert _spec is not None and _spec.loader is not None
job = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(job)

# The eight separators `str.splitlines()` honours that no line-oriented CI
# trace defines. LF, CR and CRLF are deliberately absent - those are the ones a
# trace really does use, and they must keep working.
FORGED = tuple(chr(c) for c in (0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029))

LF = chr(10)
CR = chr(13)
TAB = chr(9)

FORGED_SECTION = "section_start:1700000000:forged_step"
FORGED_PHPUNIT = "1) ForgedTest::testNothing"


def _fake_run(trace: str, status: str = "failed"):
    meta = json.dumps({
        "name": "test-job",
        "status": status,
        "stage": "test",
        "duration": 12.0,
        "web_url": "https://gitlab.example/job/1",
        "ref": "feature/x",
        "pipeline": {"id": 999},
    })

    def run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        url = args[2] if len(args) > 2 else ""
        return subprocess.CompletedProcess(
            args=args, returncode=0,
            stdout=trace if url.endswith("/trace") else meta, stderr="",
        )

    return run


def _render(monkeypatch, capsys, trace: str, argv: list[str] | None = None,
            status: str = "failed") -> str:
    monkeypatch.setattr(sys, "argv", argv or ["job.py", "123"])
    monkeypatch.setattr(job.subprocess, "run", _fake_run(trace, status))
    job.main()
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# the parse boundary is the trace's, not the trace author's
# ---------------------------------------------------------------------------

def test_a_real_section_marker_is_still_read() -> None:
    """The control. Narrowing must not cost the case the anchor exists for."""
    lines = job._log_lines(
        "running" + LF + "section_start:1700000000:real_step" + LF + "boom" + LF
    )
    assert job._last_section(lines) == "real_step", lines


def test_crlf_and_cr_are_still_line_endings() -> None:
    """A runner on a Windows shell writes CRLF; a progress bar writes CR."""
    assert job._log_lines("a" + CR + LF + "b" + CR + "c" + LF) == ["a", "b", "c"]


def test_a_forged_separator_cannot_name_the_failing_step() -> None:
    """`Last step entered:` is supertool's claim, not the trace author's."""
    for sep in FORGED:
        lines = job._log_lines("echo hello" + sep + FORGED_SECTION + LF)
        assert job._last_section(lines) is None, (
            f"{sep!r} opened a column-0 section marker: {lines!r}"
        )


def test_a_forged_separator_cannot_open_a_phpunit_failure_block() -> None:
    """A block header decides what the render shows and what it elides."""
    for sep in FORGED:
        lines = job._log_lines(
            "assertion text" + sep + FORGED_PHPUNIT + LF + "  frame" + LF
        )
        assert job._phpunit_blocks(lines) == [], (
            f"{sep!r} opened a PHPUnit failure block: {lines!r}"
        )


def test_the_trace_line_count_is_the_traces_own() -> None:
    """`(last N lines of TOTAL)` is arithmetic supertool presents as its own."""
    for sep in FORGED:
        assert len(job._log_lines("one" + sep + "two" + LF)) == 1, sep


# ---------------------------------------------------------------------------
# narrowing alone would move the forgery into the render - the other half
# ---------------------------------------------------------------------------

def test_a_separator_the_split_no_longer_honours_is_disclosed() -> None:
    """It must not reach the gutter render as a live cursor movement."""
    for sep in FORGED:
        line = job._log_lines("one" + sep + "two" + LF)[0]
        assert sep not in line, (
            f"{sep!r} survived into a line the render prints under a gutter"
        )


def test_a_tab_survives_because_indentation_is_the_authors_content() -> None:
    assert job._log_lines("a" + TAB + "b" + LF) == ["a" + TAB + "b"]


# ---------------------------------------------------------------------------
# end to end, through the render the reader actually sees
# ---------------------------------------------------------------------------

def test_render_does_not_report_a_forged_step_as_the_failing_one(
    monkeypatch, capsys
) -> None:
    trace = "starting" + LF + "echo done" + chr(0x2028) + FORGED_SECTION + LF
    out = _render(monkeypatch, capsys, trace)
    assert "Last step entered: forged_step" not in out, out


def test_render_line_total_counts_only_real_lines(monkeypatch, capsys) -> None:
    trace = LF.join(f"line{i}" for i in range(1, 6)) + chr(0x2028) + "forged" + LF
    out = _render(monkeypatch, capsys, trace, argv=["job.py", "123", "raw"])
    assert "of 5" in out, out
