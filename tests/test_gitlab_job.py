"""Unit tests for presets/gitlab/job.py — :raw mode and line slicing."""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "job.py"
_spec = importlib.util.spec_from_file_location("gitlab_job", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
job = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(job)


def _make_fake_run(trace_lines: list[str], status: str = "failed"):
    """Return a fake subprocess.run that handles glab metadata + trace calls."""
    trace = "\n".join(trace_lines) + "\n"
    meta = json.dumps({
        "name": "test-job",
        "status": status,
        "stage": "test",
        "duration": 12.0,
        "web_url": "https://gitlab.example/job/1",
        "ref": "feature/x",
        "pipeline": {"id": 999},
    })

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        url = args[2] if len(args) > 2 else ""
        if url.endswith("/trace"):
            stdout = trace
        else:
            stdout = meta
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=stdout, stderr=""
        )

    return fake_run


def _run_main(
    monkeypatch, argv: list[str], trace_lines: list[str], status: str = "failed"
) -> int:
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(job.subprocess, "run", _make_fake_run(trace_lines, status))
    return job.main()


def test_raw_dumps_full_trace(monkeypatch, capsys) -> None:
    lines = [f"line{i}" for i in range(1, 11)]
    rc = _run_main(monkeypatch, ["job.py", "123", "raw"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Raw lines 1-10 of 10" in out
    assert "    1 | line1" in out
    assert "   10 | line10" in out
    # No smart-mode artefacts
    assert "Error context" not in out
    assert "Tail (last" not in out


def test_raw_slice_with_start_and_end(monkeypatch, capsys) -> None:
    lines = [f"line{i}" for i in range(1, 21)]
    rc = _run_main(monkeypatch, ["job.py", "123", "raw", "5", "8"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Raw lines 5-8 of 20" in out
    assert "    5 | line5" in out
    assert "    8 | line8" in out
    assert "line4" not in out
    assert "line9" not in out


def test_raw_slice_with_start_only_runs_to_end(monkeypatch, capsys) -> None:
    lines = [f"line{i}" for i in range(1, 11)]
    rc = _run_main(monkeypatch, ["job.py", "123", "raw", "7"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Raw lines 7-10 of 10" in out
    assert "    7 | line7" in out
    assert "   10 | line10" in out
    assert "line6" not in out


# ---------------------------------------------------------------------------
# out-of-range START and the tail form (#487)
#
# `raw` is reached as a fallback when `:fail` was unhelpful, so START is a
# guess and the guess is nearly always aimed at the tail. Declining an
# overshoot costs a whole round-trip to learn a bound the same response has
# already printed. Returning the tail instead is only safe while the response
# says, in the same breath, that it is not the range that was asked for —
# silently handing back different lines is the same disease one level down.
# ---------------------------------------------------------------------------

def test_raw_start_beyond_total_returns_the_tail_of_the_requested_width(
    monkeypatch, capsys
) -> None:
    lines = [f"line{i}" for i in range(1, 21)]
    rc = _run_main(monkeypatch, ["job.py", "123", "raw", "100", "104"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Raw lines 16-20 of 20" in out
    assert "   20 | line20" in out
    assert "   15 | line15" not in out


def test_raw_start_beyond_total_says_it_did_not_return_what_was_asked_for(
    monkeypatch, capsys
) -> None:
    """A clamp nobody is told about hands back different data than requested."""
    lines = [f"line{i}" for i in range(1, 21)]
    rc = _run_main(monkeypatch, ["job.py", "123", "raw", "100", "104"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "requested 100-104" in out
    assert "past end of log" in out
    assert "20 lines" in out


def test_raw_start_beyond_total_without_an_end_returns_the_default_tail(
    monkeypatch, capsys
) -> None:
    lines = [f"line{i}" for i in range(1, 201)]
    rc = _run_main(monkeypatch, ["job.py", "123", "raw", "500"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Raw lines 121-200 of 200" in out
    assert "requested 500-200" in out


def test_raw_tail_form_returns_the_last_n_lines(monkeypatch, capsys) -> None:
    """The bound no longer has to be known before the tail can be asked for."""
    lines = [f"line{i}" for i in range(1, 21)]
    rc = _run_main(monkeypatch, ["job.py", "123", "raw", "-5"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Raw lines 16-20 of 20" in out
    assert "   16 | line16" in out
    assert "line15" not in out


def test_raw_tail_form_longer_than_the_log_returns_the_whole_log(
    monkeypatch, capsys
) -> None:
    lines = [f"line{i}" for i in range(1, 6)]
    rc = _run_main(monkeypatch, ["job.py", "123", "raw", "-100"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Raw lines 1-5 of 5" in out


def test_raw_tail_form_refuses_an_end(monkeypatch, capsys) -> None:
    """-N:END has two contradictory anchors; guessing which one wins is how a
    caller gets lines it never asked for."""
    lines = [f"line{i}" for i in range(1, 21)]
    rc = _run_main(monkeypatch, ["job.py", "123", "raw", "-5", "9"], lines)
    out = capsys.readouterr().out
    assert rc == 1
    assert "tail form" in out


def test_raw_refuses_an_inverted_range(monkeypatch, capsys) -> None:
    """It used to print `## Raw lines 10-9 of 20` and no lines at all — a
    garbled header over an empty body, which reads as "that part of the log is
    empty" rather than "that range is backwards"."""
    lines = [f"line{i}" for i in range(1, 21)]
    rc = _run_main(monkeypatch, ["job.py", "123", "raw", "10", "5"], lines)
    out = capsys.readouterr().out
    assert rc == 1
    assert "END (5) is before START (10)" in out


def test_raw_on_an_empty_log_says_the_log_is_empty(monkeypatch, capsys) -> None:
    """"Nothing to show" must mean the log is empty, never that the tool
    declined — the two are the absence this repo keeps confusing."""
    fake = _make_fake_run(["placeholder"])

    def empty_trace(args, **kw):
        result = fake(args, **kw)
        if len(args) > 2 and args[2].endswith("/trace"):
            result.stdout = ""
        return result

    monkeypatch.setattr(sys, "argv", ["job.py", "123", "raw", "100"])
    monkeypatch.setattr(job.subprocess, "run", empty_trace)
    rc = job.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "log is empty" in out


def test_raw_end_clamped_to_total(monkeypatch, capsys) -> None:
    lines = [f"line{i}" for i in range(1, 6)]
    rc = _run_main(monkeypatch, ["job.py", "123", "raw", "3", "999"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Raw lines 3-5 of 5" in out


def test_raw_invalid_start_returns_error(monkeypatch, capsys) -> None:
    rc = _run_main(monkeypatch, ["job.py", "123", "raw", "abc"], ["x"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "START/END must be integers" in out


def test_default_mode_runs_smart_filter(monkeypatch, capsys) -> None:
    """Sanity — without :raw, the smart filter path is taken."""
    lines = ["build started", "ERROR: thing exploded", "build done"]
    rc = _run_main(monkeypatch, ["job.py", "123"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Raw lines" not in out
    # Default path prints a header with job + log line count
    assert "Log: 3 lines total" in out


def test_errors_mode_shows_all_matched_blocks(monkeypatch, capsys) -> None:
    """errors mode shows every error block with no tail truncation."""
    # Simulate phpstan output: 200 lines with errors scattered
    lines = ["build start"] + [f"line {i}" for i in range(150)] + [
        " ------ ---- ",
        " Line   Dvsi/foo/Bar.class.php ",
        " 42     Type mismatch ",
        "    🪪  generics.notSubtype ",
        " ------ ---- ",
    ] + [f"trail {i}" for i in range(40)]
    rc = _run_main(monkeypatch, ["job.py", "123", "errors"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "All error blocks" in out
    # Should include the early-in-log error block, not just tail
    assert "generics.notSubtype" in out
    assert "Line   Dvsi/foo/Bar" in out


def test_errors_mode_no_matches(monkeypatch, capsys) -> None:
    """Nothing matched on a *failed* job is a tool gap, and must read as one."""
    lines = ["build started", "all good", "build done"]
    rc = _run_main(monkeypatch, ["job.py", "123", "errors"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "## FAILED — no error pattern matched" in out
    assert "not that the log is clean" in out


def test_fail_is_alias_of_errors(monkeypatch, capsys) -> None:
    """`:fail` produces byte-identical output to `:errors` — pure alias."""
    lines = ["build start"] + [f"line {i}" for i in range(150)] + [
        " ------ ---- ",
        "    🪪  generics.notSubtype ",
        " ------ ---- ",
    ] + [f"trail {i}" for i in range(40)]
    _run_main(monkeypatch, ["job.py", "123", "errors"], lines)
    errors_out = capsys.readouterr().out
    _run_main(monkeypatch, ["job.py", "123", "fail"], lines)
    fail_out = capsys.readouterr().out
    assert fail_out == errors_out
    assert "All error blocks" in fail_out
    assert "generics.notSubtype" in fail_out


def test_phpstan_identifier_marker_matches_default(monkeypatch, capsys) -> None:
    """🪪 marker (phpstan identifier) is in default patterns."""
    lines = [
        "noise",
        "noise",
        " 12  some phpstan error ",
        "    🪪  generics.notSubtype ",
        "noise",
    ]
    rc = _run_main(monkeypatch, ["job.py", "123"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    # The phpstan error should appear in error context section
    assert "generics.notSubtype" in out
    assert "Error context" in out


def test_grep_matches_with_context(monkeypatch, capsys) -> None:
    """grep mode returns matching lines plus surrounding context."""
    lines = [f"line {i}" for i in range(20)] + [
        '    "applied_rectors": [',
        '        "AddTestCommentToClassRector"',
    ] + [f"trail {i}" for i in range(20)]
    rc = _run_main(monkeypatch, ["job.py", "123", "grep", "applied_rectors"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "grep /applied_rectors/ — 1 matching lines" in out
    assert "applied_rectors" in out
    assert "Raw lines" not in out


def test_grep_no_match_falls_back_to_tail(monkeypatch, capsys) -> None:
    """No match never returns empty — names the pattern and shows a tail."""
    lines = [f"line {i}" for i in range(100)]
    rc = _run_main(monkeypatch, ["job.py", "123", "grep", "zzz_nonexistent"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "No lines match /zzz_nonexistent/ (searched 100 lines)" in out
    assert "Showing last" in out
    assert "fallback" in out


def test_grep_bad_regex_falls_back_to_literal(monkeypatch, capsys) -> None:
    """An invalid regex is matched literally instead of crashing."""
    lines = ["before", "1) FooTest::testBar", "after"]
    rc = _run_main(monkeypatch, ["job.py", "123", "grep", "1)"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "(literal match)" in out
    assert "regex failed to compile" not in out
    assert "1) FooTest::testBar" in out


def test_grep_missing_pattern_returns_error(monkeypatch, capsys) -> None:
    """grep with no pattern is a usage error."""
    rc = _run_main(monkeypatch, ["job.py", "123", "grep"], ["x"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "usage: gl-job:JOB_ID:grep:PATTERN" in out


def _phpunit_failure_trace() -> list[str]:
    """A realistic PHPUnit failure: a rendered HTML artifact between head and tail."""
    html = ['<tr data-url="/absence/5" data-entity-id="5" class="list-row">']
    html += [f'<td class="cell-{i}"><span class="value">value {i}</span></td>' for i in range(12)]
    html += ['<td class="num-days"><span class="value">2.5</span></td>']
    html += [f'<td class="cell-{i}"><span class="value">tail {i}</span></td>' for i in range(12)]
    html += ["</tr>"]
    return (
        ["PHPUnit 10.5.0 by Sebastian Bergmann and contributors."]
        + [f"runner noise {i}" for i in range(40)]
        + [
            "",
            "There was 1 failure:",
            "",
            "1) SiAbsence\\Components\\AbsenceUserDaysHistoryTrListRendererTest::testBasic",
            "Failed asserting that '" + html[0],
        ]
        + html[1:]
        + [
            "' [ASCII](length: 2170) does not contain \"5</\" [ASCII](length: 3).",
            "",
            "/builds/dvsi/tests/unit/AbsenceUserDaysHistoryTrListRendererTest.php:42",
            "",
            "FAILURES!",
            "Tests: 120, Assertions: 340, Failures: 1.",
        ]
    )


def _shown_line_numbers(out: str) -> list[int]:
    """Line numbers of the trace lines actually printed by a smart-mode dump."""
    numbers = []
    for line in out.splitlines():
        m = re.match(r"^\s+(\d+) \| ", line)
        if m:
            numbers.append(int(m.group(1)))
    return numbers


def test_fail_keeps_the_whole_phpunit_failure_block(monkeypatch, capsys) -> None:
    """The evidence inside a PHPUnit failure (issue #404) survives :fail."""
    lines = _phpunit_failure_trace()
    rc = _run_main(monkeypatch, ["job.py", "123", "fail"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "1) SiAbsence\\Components\\AbsenceUserDaysHistoryTrListRendererTest::testBasic" in out
    assert '<td class="num-days"><span class="value">2.5</span></td>' in out
    assert '<td class="cell-6"><span class="value">value 6</span></td>' in out
    assert '<td class="cell-6"><span class="value">tail 6</span></td>' in out
    assert "' [ASCII](length: 2170) does not contain \"5</\" [ASCII](length: 3)." in out
    assert "/builds/dvsi/tests/unit/AbsenceUserDaysHistoryTrListRendererTest.php:42" in out

    block_start = lines.index(
        "1) SiAbsence\\Components\\AbsenceUserDaysHistoryTrListRendererTest::testBasic"
    ) + 1
    block_end = lines.index(
        "/builds/dvsi/tests/unit/AbsenceUserDaysHistoryTrListRendererTest.php:42"
    ) + 1
    shown = _shown_line_numbers(out)
    assert [n for n in range(block_start, block_end + 1)] == [
        n for n in shown if block_start <= n <= block_end
    ]


def test_fail_gap_marker_states_how_many_lines_were_elided(monkeypatch, capsys) -> None:
    """Elision is never silent — the marker carries the exact dropped count."""
    lines = ["ERROR: first boom"] + [f"quiet {i}" for i in range(60)] + ["ERROR: second boom"]
    rc = _run_main(monkeypatch, ["job.py", "123", "fail"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "ERROR: first boom" in out
    assert "ERROR: second boom" in out
    shown = _shown_line_numbers(out)
    assert shown == list(range(1, 10)) + list(range(54, 63))
    assert "... (44 lines elided)" in out
    assert "..." not in out.replace("... (44 lines elided)", "")


def test_fail_no_gap_marker_when_nothing_is_dropped(monkeypatch, capsys) -> None:
    """A contiguous match set prints no elision marker at all."""
    lines = ["ERROR: boom", "detail one", "detail two"]
    rc = _run_main(monkeypatch, ["job.py", "123", "fail"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert _shown_line_numbers(out) == [1, 2, 3]
    assert "elided" not in out
    assert "..." not in out


def test_fail_oversize_phpunit_block_elides_visibly(monkeypatch, capsys) -> None:
    """Past the block cap, head and tail are kept and the drop is announced."""
    monkeypatch.setenv("GL_JOB_PHPUNIT_BLOCK_MAX_LINES", "20")
    lines = (
        ["1) FooTest::testBar", "Failed asserting that '<html>"]
        + [f"<div>row {i}</div>" for i in range(100)]
        + [
            "' does not contain \"needle\".",
            "/builds/tests/FooTest.php:99",
            "",
            "FAILURES!",
        ]
    )
    rc = _run_main(monkeypatch, ["job.py", "123", "fail"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "1) FooTest::testBar" in out
    assert "<div>row 0</div>" in out
    assert "' does not contain \"needle\"." in out
    assert "/builds/tests/FooTest.php:99" in out
    assert "<div>row 50</div>" not in out
    assert "lines elided)" in out


def _failure_block(n: int) -> list[str]:
    """One 25-line PHPUnit failure whose middle sits outside any ±8 window."""
    return (
        [f"{n}) FooTest::testCase{n}", f"Failed asserting that '<html-{n}>"]
        + [f"<div>block {n} row {i}</div>" for i in range(20)]
        + [f"' does not contain \"needle-{n}\".", "", f"/builds/tests/FooTest{n}.php:{n}0"]
    )


def _many_failures_trace(count: int) -> list[str]:
    lines = [f"runner noise {i}" for i in range(20)] + ["", f"There were {count} failures:", ""]
    for n in range(1, count + 1):
        lines += _failure_block(n)
    return lines + ["", "FAILURES!", f"Tests: 40, Failures: {count}."]


def _block_span(lines: list[str], n: int) -> tuple[int, int]:
    """1-indexed (first, last) printed line numbers of failure block n."""
    start = lines.index(f"{n}) FooTest::testCase{n}") + 1
    end = lines.index(f"/builds/tests/FooTest{n}.php:{n}0") + 1
    return start, end


def test_fail_over_budget_drops_whole_failures_and_says_how_many(monkeypatch, capsys) -> None:
    """Past the aggregate budget, failures are dropped whole — never gutted."""
    monkeypatch.setenv("GL_JOB_PHPUNIT_TOTAL_MAX_LINES", "60")
    lines = _many_failures_trace(4)
    rc = _run_main(monkeypatch, ["job.py", "123", "fail"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert (
        "... (2 of 4 PHPUnit failures not shown in full "
        "— raise GL_JOB_PHPUNIT_TOTAL_MAX_LINES=N)"
    ) in out

    shown = _shown_line_numbers(out)
    for kept in (1, 2):
        first, last = _block_span(lines, kept)
        assert [n for n in shown if first <= n <= last] == list(range(first, last + 1))
        assert f"<div>block {kept} row 12</div>" in out
        assert f'does not contain "needle-{kept}"' in out

    for skipped in (3, 4):
        assert f"<div>block {skipped} row 12</div>" not in out
        assert f"{skipped}) FooTest::testCase{skipped}" in out
        assert f"<div>block {skipped} row 0</div>" in out


def test_fail_under_budget_keeps_every_failure_whole(monkeypatch, capsys) -> None:
    """Inside the budget there is no announcement and nothing is dropped."""
    lines = _many_failures_trace(3)
    rc = _run_main(monkeypatch, ["job.py", "123", "fail"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "not shown in full" not in out
    shown = _shown_line_numbers(out)
    for n in (1, 2, 3):
        first, last = _block_span(lines, n)
        assert [i for i in shown if first <= i <= last] == list(range(first, last + 1))
        assert f"<div>block {n} row 12</div>" in out


def test_fail_exactly_at_budget_drops_nothing(monkeypatch, capsys) -> None:
    """A budget equal to the total cost keeps both failures, silently."""
    monkeypatch.setenv("GL_JOB_PHPUNIT_TOTAL_MAX_LINES", "50")
    lines = _many_failures_trace(2)
    rc = _run_main(monkeypatch, ["job.py", "123", "fail"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "not shown in full" not in out
    shown = _shown_line_numbers(out)
    for n in (1, 2):
        first, last = _block_span(lines, n)
        assert [i for i in shown if first <= i <= last] == list(range(first, last + 1))
    assert "<div>block 1 row 12</div>" in out
    assert "<div>block 2 row 12</div>" in out


def test_expand_phpunit_blocks_never_removes_pattern_window_lines() -> None:
    """Union-only: a zero budget leaves the caller's own selection intact."""
    lines = _many_failures_trace(2)
    outside = {5, 6, 7}
    dropped, touched = job._expand_phpunit_blocks(lines, outside, 500, 0)
    assert (dropped, touched) == (0, 0)
    assert outside == {5, 6, 7}

    first, _ = _block_span(lines, 1)
    inside = {first}
    dropped, touched = job._expand_phpunit_blocks(lines, inside, 500, 0)
    assert (dropped, touched) == (1, 1)
    assert inside == {first}


def test_phpunit_blocks_bounds() -> None:
    lines = [
        "There was 1 failure:",
        "1) FooTest::testA",
        "Failed asserting that false is true.",
        "",
        "/builds/FooTest.php:10",
        "",
        "2) FooTest::testB",
        "boom",
        "/builds/FooTest.php:20",
        "",
        "FAILURES!",
        "Tests: 2.",
    ]
    assert job._phpunit_blocks(lines) == [(1, 4), (6, 8)]


def test_phpunit_blocks_ignores_non_phpunit_numbering() -> None:
    assert job._phpunit_blocks(["1) do a thing", "2. and another"]) == []


def test_select_job_patterns_matches_by_name() -> None:
    table = [
        {"job": "^phpstan", "patterns": ["🪪"]},
        {"job": "rector", "patterns": ["applied_rectors"], "resolution": "rector_ci_apply:{id}"},
    ]
    patterns, resolution = job._select_job_patterns("rector", table, ["DEFAULT"])
    assert patterns == ["applied_rectors"]
    assert resolution == "rector_ci_apply:{id}"


def test_select_job_patterns_no_match_falls_back() -> None:
    patterns, resolution = job._select_job_patterns(
        "mystery", [{"job": "rector", "patterns": ["x"]}], ["DEFAULT"]
    )
    assert patterns == ["DEFAULT"]
    assert resolution is None


def test_parse_job_patterns_malformed_returns_empty() -> None:
    assert job._parse_job_patterns("not json") == []
    assert job._parse_job_patterns("") == []
    assert job._parse_job_patterns('{"not": "a list"}') == []


def test_job_patterns_shows_resolution_with_interpolated_id(monkeypatch, capsys) -> None:
    """A matched job prints its resolution op with {id} replaced by the job id."""
    monkeypatch.setenv("SUPERTOOL_JOB_PATTERNS", json.dumps([
        {"job": "test-job", "patterns": ["BOOM"], "resolution": "rector_ci_apply:{id}"}
    ]))
    lines = ["noise", "BOOM goes the build", "noise"]
    rc = _run_main(monkeypatch, ["job.py", "777"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "BOOM goes the build" in out
    assert "Resolve:  ./supertool 'rector_ci_apply:777'" in out


def _cause_above_trace_log() -> list[str]:
    """Issue #444: job 6931305 — the reason sits above the frames it caused.

    A stack trace is the consequence; the line that states why precedes it. The
    PHPUnit-listed variant of this shape is already held whole by the block
    expansion from #404, so the fixture uses the shape that is not: a bare
    exception dump followed by frames, anchored only by the runner's trailing
    `ERROR:` line, with the cause ~23 lines above it.
    """
    frames = [
        f"/builds/dvsi/src2/SiCore/BusinessEntityCommands/Frame{i}.class.php:{100 + i}"
        for i in range(18)
    ]
    return (
        [
            "Running with gitlab-runner 16.11.0 (~/dvsi)",
            "$ php scripts.php script=ScriptRunMigration",
        ]
        + [f"Migrating V17/V9/V0/V17615623{i:02d} ... done" for i in range(60)]
        + [
            "",
            "Fwk\\Foundations\\Exceptions\\FwkException: Unable to persist entity Lead",
            "",
            "Caused by",
            "PDOException: SQLSTATE[23000]: Integrity constraint violation: 1452 "
            "Cannot add or update a child row: a foreign key constraint fails "
            "(`dvsi_2`.`lead`, CONSTRAINT `lead_location_fk1` FOREIGN KEY "
            "(`location_id`) REFERENCES `location` (`id`))",
            "",
        ]
        + frames
        + [
            "/builds/dvsi/src2/SiCore/BusinessEntityCommands/CommandAbstractParseCSV.class.php:99",
            "/builds/dvsi/tests/unit/SiBrief/CommandImportBriefsCSVTest.php:114",
            "",
            "Cleaning up project directory and file based variables",
            "ERROR: Job failed: exit code 255",
        ]
    )


def _worker_crash_log() -> list[str]:
    """Issue #445: job 6929217 — a paratest worker segfaulted around line 1616."""
    return (
        ["Running with gitlab-runner 16.11.0 (~/dvsi)"]
        + [f"Processing test suite chunk {i} ..." for i in range(1600)]
        + [
            "",
            "In WorkerCrashedException.php line 41:",
            "",
            """  The test "PARATEST='1' TEST_TOKEN='2' UNIQUE_TEST_TOKEN='2_6a688660c3fab'""",
            '  Dvsi/dvsi-private/tests/unit/SiUser/Components/UserModularForm02Test.php" failed.',
            "",
            "  Exit Code: 139(Segmentation violation)",
            "",
            "paratest [--processes PROCESSES] [--path PATH]",
            "",
            "Cleaning up project directory and file based variables",
        ]
    )


def _unclassifiable_failure_log() -> list[str]:
    """A genuinely failed job whose log matches no pattern the tool knows."""
    return (
        [
            "Running with gitlab-runner 16.11.0 (~/dvsi)",
            "section_start:1753600000:prepare_executor",
            'Preparing the "docker" executor',
            "section_end:1753600002:prepare_executor",
            "section_start:1753600003:build_assets",
            "$ make assets",
        ]
        + [f"Compiling module {i} ..." for i in range(60)]
        + [
            "make: *** [Makefile:12: assets] Error 2",
            "section_end:1753600200:build_assets",
            "Cleaning up project directory and file based variables",
        ]
    )


def test_fail_surfaces_the_cause_line_above_the_stack_trace(monkeypatch, capsys) -> None:
    """Issue #444: the line that says *why* must be shown, not just the frames."""
    lines = _cause_above_trace_log()
    rc = _run_main(monkeypatch, ["job.py", "6931305", "fail"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Caused by" in out
    assert "SQLSTATE[23000]: Integrity constraint violation: 1452" in out
    assert "lead_location_fk1" in out
    assert "Fwk\\Foundations\\Exceptions\\FwkException: Unable to persist entity Lead" in out


def test_fail_surfaces_the_cause_even_with_tight_job_patterns(monkeypatch, capsys) -> None:
    """A per-job pattern table tightens the noise; it must not hide the reason."""
    monkeypatch.setenv("SUPERTOOL_JOB_PATTERNS", json.dumps([
        {"job": "test-job", "patterns": ["Job failed"]}
    ]))
    lines = _cause_above_trace_log()
    rc = _run_main(monkeypatch, ["job.py", "6931305", "fail"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "SQLSTATE[23000]: Integrity constraint violation: 1452" in out


def test_fail_surfaces_a_crashed_paratest_worker(monkeypatch, capsys) -> None:
    """Issue #445: the segfaulted worker is the reason and must be reported as one."""
    lines = _worker_crash_log()
    rc = _run_main(monkeypatch, ["job.py", "6929217", "fail"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "No error patterns matched" not in out
    assert "In WorkerCrashedException.php line 41:" in out
    assert "Exit Code: 139(Segmentation violation)" in out
    assert "UserModularForm02Test.php" in out


def test_fail_on_unmatched_failed_job_cannot_be_read_as_success(monkeypatch, capsys) -> None:
    """Issue #445: zero matches on a failed job states the tool gap and shows the tail."""
    lines = _unclassifiable_failure_log()
    rc = _run_main(monkeypatch, ["job.py", "6929217", "fail"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "## FAILED — no error pattern matched" in out
    assert "could not classify" in out
    assert "not that the log is clean" in out
    assert "make: *** [Makefile:12: assets] Error 2" in out
    assert "Log tail" in out
    assert "gl-job:6929217:raw" in out


def test_default_mode_on_unmatched_failed_job_states_the_gap(monkeypatch, capsys) -> None:
    """The same honesty in the default view, which used to print a bare tail."""
    lines = _unclassifiable_failure_log()
    rc = _run_main(monkeypatch, ["job.py", "6929217"], lines)
    out = capsys.readouterr().out
    assert rc == 0
    assert "## FAILED — no error pattern matched" in out
    assert "make: *** [Makefile:12: assets] Error 2" in out


def test_unmatched_job_that_did_not_fail_gets_no_failure_banner(monkeypatch, capsys) -> None:
    """A green job with no matches is genuinely clean — do not cry wolf."""
    lines = ["build started", "all good", "build done"]
    rc = _run_main(monkeypatch, ["job.py", "123", "fail"], lines, status="success")
    out = capsys.readouterr().out
    assert rc == 0
    assert "FAILED" not in out
    assert "No error patterns matched" in out
