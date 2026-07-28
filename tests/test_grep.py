from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import supertool


def test_grep_finds_match_in_single_file(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("class Foo:\n    pass\n\nclass Bar:\n    pass\n")
    out = supertool.op_grep("class", str(f))
    assert "(2 results" in out
    assert "src.py\n" in out
    assert "  1:class Foo:" in out
    assert "  4:class Bar:" in out


def test_grep_no_match_returns_zero(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("class Foo:\n")
    out = supertool.op_grep("NOTHINGMATCHES_XYZZY", str(f))
    assert "(0 results" in out


def test_grep_empty_pattern_errors() -> None:
    out = supertool.op_grep("", "/tmp")
    assert "ERROR: empty pattern" in out


def test_grep_missing_path_error_includes_cwd() -> None:
    """A non-existent path errors with the CWD, so 'wrong path' vs 'wrong CWD' is
    distinguishable — the recurring trap where a stale cwd makes a relative path
    silently miss."""
    out = supertool.op_grep("anything", "does-not-exist-xyzzy.json")
    assert "ERROR: path not found" in out
    assert os.getcwd() in out


def test_grep_respects_limit(tmp_path: Path) -> None:
    f = tmp_path / "many.py"
    content = "\n".join(f"match line {i}" for i in range(1, 20)) + "\n"
    f.write_text(content)
    out = supertool.op_grep("match", str(f), limit=3)
    assert "limit 3" in out
    # Count actual result lines (path:lineno:content format)
    result_lines = [ln for ln in out.split("\n") if ":" in ln and "match line" in ln]
    assert len(result_lines) == 3


def test_grep_on_directory_searches_all_files_by_default(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("needle = 1\n")
    (tmp_path / "doc.md").write_text("needle in docs\n")
    (tmp_path / "log.log").write_text("needle in log\n")
    # Reset cached extensions so default-all kicks in
    supertool._GREP_EXTENSIONS_EFFECTIVE = None
    out = supertool.op_grep("needle", str(tmp_path), limit=10)
    assert "code.py" in out
    assert "doc.md" in out
    assert "log.log" in out  # default: search all files


def test_grep_on_directory_respects_config_extensions(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "code.py").write_text("needle = 1\n")
    (tmp_path / "doc.md").write_text("needle in docs\n")
    (tmp_path / "log.log").write_text("needle in log\n")
    # Configure extensions to restrict to .py only
    monkeypatch.setattr(supertool, "_GREP_EXTENSIONS_EFFECTIVE", None)
    monkeypatch.setattr(supertool, "_load_config", lambda: {
        "builtin-ops": {"grep": {"extensions": ["*.py"]}}
    })
    out = supertool.op_grep("needle", str(tmp_path), limit=10)
    assert "code.py" in out
    assert "doc.md" not in out
    assert "log.log" not in out


# --- Auto-read on grep (small single file + match) ---

def test_grep_auto_reads_small_single_file_on_match(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("found_it = True\n")
    out = supertool.op_grep("found_it", str(f))
    assert "[auto-read:" in out
    assert "(1 lines" in out  # The render_file output
    assert "     1→found_it = True" in out


def test_grep_no_auto_read_when_no_match(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("nothing_here = True\n")
    out = supertool.op_grep("XXX_NO_MATCH", str(f))
    assert "[auto-read:" not in out


def test_grep_no_auto_read_on_directory(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("foo = 1\n")
    out = supertool.op_grep("foo", str(tmp_path))
    # Matched in file a.py, but path is the directory
    assert "[auto-read:" not in out


def test_grep_no_auto_read_on_large_file(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "big.py"
    f.write_text("needle\n" + "x" * 30000)
    out = supertool.op_grep("needle", str(f))
    assert "[auto-read:" not in out


def test_grep_no_auto_read_flag_suppresses_auto_read(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("found_it = True\n")
    out = supertool.op_grep("found_it", str(f), no_auto_read=True)
    # Match still reported, but the full file is not dumped
    assert "[auto-read:" not in out
    assert "found_it" in out
    assert "1:found_it = True" in out


def test_grep_dispatch_no_auto_read_flag(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("found_it = True\n")
    out = supertool.dispatch(f"grep:found_it:{f}:no-auto-read")
    assert "[auto-read:" not in out
    assert "found_it" in out


def test_grep_dispatch_no_auto_read_with_limit(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("found_it = True\n")
    out = supertool.dispatch(f"grep:found_it:{f}:5:no-auto-read")
    assert "[auto-read:" not in out
    assert "found_it" in out


def test_grep_dispatch_default_still_auto_reads(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("found_it = True\n")
    out = supertool.dispatch(f"grep:found_it:{f}")
    assert "[auto-read:" in out


def test_parse_grep_args_peels_no_auto_read(tmp_path: Path) -> None:
    pattern, path, limit, context, count_only, no_auto_read = \
        supertool._parse_grep_args(["grep", "needle", "src/", "no-auto-read"])
    assert pattern == "needle"
    assert path == "src/"
    assert no_auto_read is True
    assert count_only is False


def test_parse_grep_args_no_auto_read_with_count_and_ints(tmp_path: Path) -> None:
    pattern, path, limit, context, count_only, no_auto_read = \
        supertool._parse_grep_args(
            ["grep", "needle", "src/", "5", "2", "count", "no-auto-read"])
    assert pattern == "needle"
    assert path == "src/"
    assert limit == 5
    assert context == 2
    assert count_only is True
    assert no_auto_read is True


# ---------------------------------------------------------------------------
# op_grep with context lines
# ---------------------------------------------------------------------------

def test_grep_context_zero_same_as_no_context(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("class Foo:\n    pass\n\nclass Bar:\n    pass\n")
    out_plain = supertool.op_grep("class", str(f), limit=10, context=0)
    out_ctx = supertool.op_grep("class", str(f), limit=10)
    assert out_plain == out_ctx


def test_grep_context_includes_surrounding_lines(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    # Match at line 5, 2 lines of context → lines 3-7 shown; lines 1-2 and 8-10 excluded
    f.write_text("skip1\nskip2\nctx_before2\nctx_before1\nMATCH\nctx_after1\nctx_after2\nskip3\nskip4\n")
    out = supertool.op_grep("MATCH", str(f), limit=10, context=2)
    # File path appears once as header (forward-slash normalized for cross-platform output)
    assert str(f).replace(os.sep, "/") + "\n" in out
    # Match line uses colon separator, indented
    assert "  5:MATCH" in out
    # Context lines use dash separator, indented
    assert "  4-ctx_before1" in out
    assert "  6-ctx_after1" in out
    assert "  3-ctx_before2" in out
    assert "  7-ctx_after2" in out
    # Lines beyond context are not included
    assert "skip1" not in out
    assert "skip2" not in out
    assert "skip3" not in out


def test_grep_context_header_shows_context_value(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("MATCH\n")
    out = supertool.op_grep("MATCH", str(f), limit=10, context=3)
    assert "context 3" in out


def test_grep_context_separator_between_nonadjacent_groups(tmp_path: Path) -> None:
    lines = [f"line{i}" for i in range(1, 21)]
    lines[3] = "MATCH_A"   # line 4
    lines[16] = "MATCH_B"  # line 17
    f = tmp_path / "src.py"
    f.write_text("\n".join(lines) + "\n")
    out = supertool.op_grep("MATCH", str(f), limit=10, context=1)
    # Groups should be separated by --
    assert "--\n" in out
    assert "MATCH_A" in out
    assert "MATCH_B" in out


def test_grep_context_no_separator_for_adjacent_matches(tmp_path: Path) -> None:
    lines = ["before", "MATCH_A", "MATCH_B", "after"]
    f = tmp_path / "src.py"
    f.write_text("\n".join(lines) + "\n")
    out = supertool.op_grep("MATCH", str(f), limit=10, context=1)
    # Adjacent matches → merged group → no -- separator
    assert "--\n" not in out
    assert "MATCH_A" in out
    assert "MATCH_B" in out


def test_grep_context_overlapping_windows_merge(tmp_path: Path) -> None:
    # Two matches close enough that context windows overlap
    lines = ["a", "MATCH_A", "b", "MATCH_B", "c"]
    f = tmp_path / "src.py"
    f.write_text("\n".join(lines) + "\n")
    out = supertool.op_grep("MATCH", str(f), limit=10, context=2)
    # With context=2: window A covers lines 1-3, window B covers lines 2-5
    # They overlap → one group, no --
    assert "--\n" not in out
    assert "MATCH_A" in out
    assert "MATCH_B" in out


def test_grep_context_clamps_to_file_boundaries(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("MATCH\nline2\nline3\n")
    # Match at line 1 with context=5 — should not go negative
    out = supertool.op_grep("MATCH", str(f), limit=10, context=5)
    assert "MATCH" in out
    assert "ERROR" not in out


def test_grep_context_no_auto_read(tmp_path: Path) -> None:
    f = tmp_path / "small.py"
    f.write_text("MATCH\n")
    out = supertool.op_grep("MATCH", str(f), limit=10, context=1)
    # Auto-read should be skipped when context is active
    assert "[auto-read:" not in out


# ---------------------------------------------------------------------------
# grep count mode
# ---------------------------------------------------------------------------

def test_grep_count_single_file(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("import os\nimport sys\ndef main():\n    pass\n")
    out = supertool.op_grep("import", str(f), count_only=True)
    assert "2 total matches across 1 files" in out
    assert f"{str(f).replace(os.sep, '/')}:2" in out


def test_grep_count_multiple_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("foo\nbar\n")
    (tmp_path / "b.py").write_text("foo\nfoo\n")
    out = supertool.op_grep("foo", str(tmp_path), count_only=True)
    assert "3 total matches across 2 files" in out


def test_grep_count_no_matches(tmp_path: Path) -> None:
    f = tmp_path / "empty.py"
    f.write_text("nothing here\n")
    out = supertool.op_grep("ZZZZZZ", str(f), count_only=True)
    assert "0 total matches across 0 files" in out


def test_grep_count_empty_pattern() -> None:
    out = supertool.op_grep("", ".", count_only=True)
    assert "ERROR" in out


# ---------------------------------------------------------------------------
# _grep_recursive — additional coverage
# ---------------------------------------------------------------------------

def test_grep_recursive_invalid_regex(tmp_path: Path) -> None:
    """_grep_recursive falls back to escaped pattern on invalid regex."""
    f = tmp_path / "test.php"
    f.write_text("array[0]\narray[1]\nnope\n")
    results = supertool._grep_recursive("[invalid", str(f), 10)
    # Should not crash — falls back to literal match
    assert isinstance(results, list)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows filesystem ignores chmod 0o000 — file stays readable.",
)
def test_grep_recursive_unreadable_file(tmp_path: Path) -> None:
    """_grep_recursive skips unreadable files."""
    f = tmp_path / "secret.php"
    f.write_text("class Foo {}\n")
    f.chmod(0o000)
    try:
        results = supertool._grep_recursive("class", str(f), 10)
        assert results == []
    finally:
        f.chmod(0o644)


def test_grep_recursive_respects_limit(tmp_path: Path) -> None:
    """_grep_recursive stops at limit."""
    f = tmp_path / "many.php"
    f.write_text("match\n" * 20)
    results = supertool._grep_recursive("match", str(f), 3)
    assert len(results) == 3


def test_grep_recursive_limit_across_files(tmp_path: Path) -> None:
    """_grep_recursive stops scanning files after limit reached."""
    for i in range(5):
        (tmp_path / f"f{i}.php").write_text("match\nmatch\nmatch\n")
    results = supertool._grep_recursive("match", str(tmp_path), 4)
    assert len(results) == 4


# ---------------------------------------------------------------------------
# _grep_count — additional coverage
# ---------------------------------------------------------------------------

def test_grep_count_invalid_regex(tmp_path: Path) -> None:
    """_grep_count falls back to escaped pattern on invalid regex."""
    f = tmp_path / "test.php"
    f.write_text("array[0]\narray[1]\nnope\n")
    counts = supertool._grep_count("[invalid", str(f), 10)
    # Should match literally as escaped pattern — "[invalid" won't match anything
    # but the function should not crash
    assert isinstance(counts, dict)


def test_grep_count_unreadable_file(tmp_path: Path) -> None:
    """_grep_count skips unreadable files."""
    f = tmp_path / "secret.php"
    f.write_text("class Foo {}\n")
    f.chmod(0o000)
    try:
        counts = supertool._grep_count("class", str(f), 10)
        assert isinstance(counts, dict)
    finally:
        f.chmod(0o644)


# ---------------------------------------------------------------------------
# _grep_recursive_context — additional coverage
# ---------------------------------------------------------------------------

def test_grep_recursive_context_invalid_regex(tmp_path: Path) -> None:
    """_grep_recursive_context falls back to escaped pattern on invalid regex."""
    f = tmp_path / "test.php"
    f.write_text("array[0]\narray[1]\nnope\n")
    groups = supertool._grep_recursive_context("[invalid", str(f), 10, 1)
    assert isinstance(groups, list)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows filesystem ignores chmod 0o000 — file stays readable.",
)
def test_grep_recursive_context_unreadable_file(tmp_path: Path) -> None:
    """_grep_recursive_context skips unreadable files."""
    f = tmp_path / "secret.php"
    f.write_text("class Foo {}\n")
    f.chmod(0o000)
    try:
        groups = supertool._grep_recursive_context("class", str(f), 10, 1)
        assert groups == []
    finally:
        f.chmod(0o644)


def test_grep_recursive_context_no_match(tmp_path: Path) -> None:
    """_grep_recursive_context returns empty for no matches."""
    f = tmp_path / "test.php"
    f.write_text("nothing here\njust text\n")
    groups = supertool._grep_recursive_context("zzz_no_match", str(f), 10, 1)
    assert groups == []


def test_grep_recursive_context_respects_limit(tmp_path: Path) -> None:
    """_grep_recursive_context stops collecting after limit matches."""
    f = tmp_path / "many.php"
    # Spread matches so they form separate groups
    lines = []
    for i in range(20):
        if i % 5 == 0:
            lines.append("MATCH_LINE")
        else:
            lines.append("filler")
    f.write_text("\n".join(lines) + "\n")
    groups = supertool._grep_recursive_context("MATCH_LINE", str(f), 2, 1)
    match_count = sum(1 for g in groups for line in g if line[2] == "match")
    assert match_count == 2


def test_grep_recursive_context_limit_across_files(tmp_path: Path) -> None:
    """_grep_recursive_context stops scanning files after limit reached."""
    for i in range(5):
        (tmp_path / f"f{i}.php").write_text("MATCH\nfiller\nMATCH\n")
    groups = supertool._grep_recursive_context("MATCH", str(tmp_path), 3, 0)
    match_count = sum(1 for g in groups for line in g if line[2] == "match")
    assert match_count == 3


def test_grep_header_includes_file_count(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("MATCH\nMATCH\n")
    (tmp_path / "b.txt").write_text("MATCH\n")
    out = supertool.op_grep("MATCH", str(tmp_path))
    assert "(3 results in 2 files," in out


def test_grep_header_file_count_with_context(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello\nMATCH\nworld\n")
    (tmp_path / "b.txt").write_text("MATCH\n")
    out = supertool.op_grep("MATCH", str(tmp_path), context=1)
    assert "results in 2 files" in out
    assert "context 1" in out


# ---------------------------------------------------------------------------
# grep_around dispatch — alias to grep with default context
# ---------------------------------------------------------------------------

def test_dispatch_grep_around_default_context(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("a\nb\nc\nMATCH\nd\ne\nf\n")
    out = supertool.dispatch(f"grep_around:MATCH:{f}")
    # Default context=3 → lines 1-7 all visible around match at 4
    assert "context 3" in out
    assert "  4:MATCH" in out
    assert "  1-a" in out
    assert "  7-f" in out


def test_dispatch_grep_around_custom_n(tmp_path: Path) -> None:
    f = tmp_path / "src.py"
    f.write_text("a\nb\nMATCH\nd\ne\n")
    out = supertool.dispatch(f"grep_around:MATCH:{f}:1")
    assert "context 1" in out
    assert "  3:MATCH" in out
    assert "  2-b" in out
    assert "  4-d" in out
    # Beyond context not shown
    assert "  1-a" not in out
    assert "  5-e" not in out


# ---------------------------------------------------------------------------
# Scanned-file count on zero results (#407)
# ---------------------------------------------------------------------------

def test_grep_zero_results_reports_scanned_count(tmp_path: Path) -> None:
    """0 results over 3 real files must say 3 were scanned, not read like an
    empty/wrong-path search."""
    (tmp_path / "a.py").write_text("nothing here\n")
    (tmp_path / "b.py").write_text("still nothing\n")
    (tmp_path / "c.py").write_text("more nothing\n")
    out = supertool.op_grep("NOTHINGMATCHES_XYZZY", str(tmp_path))
    assert "(0 results in 0 files, scanned 3 files, limit 10)\n" in out
    assert "nothing matched the path/glob" not in out


def test_grep_zero_results_on_empty_dir_flags_nothing_scanned(tmp_path: Path) -> None:
    """0 results over an empty directory must be distinguishable from 0
    results over a directory that was actually searched."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    out = supertool.op_grep("anything", str(empty_dir))
    assert ("(0 results in 0 files, scanned 0 files "
            "— nothing matched the path/glob, limit 10)\n") in out


def test_grep_nonzero_results_still_reports_scanned_count(tmp_path: Path) -> None:
    """Existing output format for real matches is not disturbed — the scanned
    count is appended, not swapped in."""
    (tmp_path / "a.txt").write_text("MATCH\n")
    (tmp_path / "b.txt").write_text("no match here\n")
    out = supertool.op_grep("MATCH", str(tmp_path))
    assert "(1 results in 1 files, scanned 2 files, limit 10)\n" in out
    assert "a.txt\n  1:MATCH\n" in out


def test_grep_count_only_zero_results_reports_scanned_count(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("nothing here\n")
    (tmp_path / "b.py").write_text("still nothing\n")
    out = supertool.op_grep("NOTHINGMATCHES_XYZZY", str(tmp_path), count_only=True)
    assert "(0 total matches across 0 files, scanned 2 files)\n" in out


def test_grep_count_only_empty_dir_flags_nothing_scanned(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    out = supertool.op_grep("anything", str(empty_dir), count_only=True)
    assert ("(0 total matches across 0 files, scanned 0 files "
            "— nothing matched the path/glob)\n") in out


def test_grep_context_zero_results_reports_scanned_count(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("nothing here\n")
    (tmp_path / "b.py").write_text("still nothing\n")
    (tmp_path / "c.py").write_text("more nothing\n")
    out = supertool.op_grep("NOTHINGMATCHES_XYZZY", str(tmp_path), context=1)
    assert "(0 results in 0 files, scanned 3 files, limit 10, context 1)\n" in out


def test_grep_context_empty_dir_flags_nothing_scanned(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    out = supertool.op_grep("anything", str(empty_dir), context=1)
    assert ("(0 results in 0 files, scanned 0 files "
            "— nothing matched the path/glob, limit 10, context 1)\n") in out


# ---------------------------------------------------------------------------
# The rtk-delegated path: report line and scanned denominator (#414)
# ---------------------------------------------------------------------------


class _RtkStub:
    """Stand-in for ``_rtk_run`` that records the argv it was handed."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.output: str | None = ""

    def __call__(self, args, timeout: int = 30) -> str | None:
        self.calls.append(list(args))
        return self.output


@pytest.fixture
def rtk(monkeypatch: pytest.MonkeyPatch) -> _RtkStub:
    """Drive `op_grep`'s rtk-delegated branch — which nothing else in the suite can.

    conftest's autouse `_disable_rtk_and_config` pins `_RTK_PATH = None` and
    `_CONFIG = {}` for the whole run, so before #414 no test reached the
    delegated branch at all. That is one of the three independent reasons the
    divergence stayed invisible (the others: a project config carrying
    multi-segment exclude prefixes always falls through to the native walker,
    and this repo's own `.supertool.json` sets `rtk: false`).

    Stubbing `_rtk_run` rather than requiring the real binary is deliberate:
    CI runners have no rtk, and a `skipif`-on-missing test is green everywhere
    and pins nothing. Every test using this fixture asserts `rtk.calls` so it
    cannot pass while the branch it exists to cover is skipped.
    """
    stub = _RtkStub()
    monkeypatch.setattr(supertool, "_CONFIG", {"rtk": True})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_PATH", "/fake/bin/rtk")
    monkeypatch.setattr(supertool, "_rtk_run", stub)
    return stub


def test_rtk_fixture_reaches_the_delegated_branch(tmp_path: Path, rtk: _RtkStub) -> None:
    """Guard on every other test in this block: if delegation is never
    attempted they silently pin the native walker instead."""
    a = tmp_path / "a.txt"
    a.write_text("alpha\n")
    rtk.output = f"{a}:1:alpha\n"
    supertool.op_grep("alpha", str(tmp_path))
    assert rtk.calls, "op_grep never called _rtk_run — delegated branch skipped"
    assert rtk.calls[0][:4] == ["grep", "-rn", "-m", "10"]
    assert rtk.calls[0][-2:] == ["alpha", str(tmp_path)]


def test_grep_delegated_emits_report_line_with_scanned_denominator(
    tmp_path: Path, rtk: _RtkStub
) -> None:
    """rtk returns bare `path:lineno:content` and no report line at all, so a
    delegated grep dropped the result count, the limit disclosure and #407's
    denominator together."""
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("alpha\nalpha again\n")
    b.write_text("alpha too\n")
    rtk.output = f"{a}:1:alpha\n{a}:2:alpha again\n{b}:1:alpha too\n"
    out = supertool.op_grep("alpha", str(tmp_path))
    assert rtk.calls, "delegated branch not taken — this test pins nothing"
    assert ("(3 results in 2 files, scanned ? files — delegated to rtk, "
            "limit 10)\n") in out
    assert f"{a}:1:alpha\n" in out
    assert f"{b}:1:alpha too\n" in out


def test_grep_delegated_zero_result_falls_back_to_a_real_scanned_count(
    tmp_path: Path, rtk: _RtkStub
) -> None:
    """A zero result is the case #407 exists for, so it must never carry the
    `?` denominator. rtk exiting 0 with empty stdout used to return a bare
    newline — no count, no denominator, nothing."""
    (tmp_path / "a.py").write_text("nothing here\n")
    (tmp_path / "b.py").write_text("still nothing\n")
    (tmp_path / "c.py").write_text("more nothing\n")
    rtk.output = ""
    out = supertool.op_grep("NOTHINGMATCHES_XYZZY", str(tmp_path))
    assert rtk.calls, "delegated branch not taken — this test pins nothing"
    assert "(0 results in 0 files, scanned 3 files, limit 10)\n" in out
    assert "?" not in out
    assert "nothing matched the path/glob" not in out


def test_grep_delegated_zero_result_on_empty_dir_flags_nothing_scanned(
    tmp_path: Path, rtk: _RtkStub
) -> None:
    """The two zero cases stay distinguishable through the delegated path:
    searched three files and found nothing vs searched nothing at all."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    rtk.output = ""
    out = supertool.op_grep("NOTHINGMATCHES_XYZZY", str(empty_dir))
    assert rtk.calls, "delegated branch not taken — this test pins nothing"
    assert ("(0 results in 0 files, scanned 0 files "
            "— nothing matched the path/glob, limit 10)\n") in out


def test_grep_delegated_rtk_failure_falls_back_to_native(
    tmp_path: Path, rtk: _RtkStub
) -> None:
    """rtk unavailable mid-flight (non-zero exit → `_rtk_run` returns None)
    still produces a full native report rather than an empty one."""
    (tmp_path / "a.txt").write_text("alpha\n")
    rtk.output = None
    out = supertool.op_grep("alpha", str(tmp_path))
    assert rtk.calls, "delegated branch not taken — this test pins nothing"
    assert "(1 results in 1 files, scanned 1 files, limit 10)\n" in out


def test_grep_delegated_threads_single_segment_excludes(
    tmp_path: Path, rtk: _RtkStub
) -> None:
    """Excludes reach rtk as --exclude-dir; the report line does not disturb it."""
    (tmp_path / "a.txt").write_text("alpha\n")
    supertool._CONFIG = {"rtk": True, "exclude_paths": ["node_modules/", ".git/"]}
    rtk.output = f"{tmp_path / 'a.txt'}:1:alpha\n"
    out = supertool.op_grep("alpha", str(tmp_path))
    assert rtk.calls, "delegated branch not taken — this test pins nothing"
    assert "--exclude-dir=node_modules" in rtk.calls[0]
    assert "scanned ? files — delegated to rtk" in out


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="shebang stub is POSIX-only; the in-process delegated tests above "
           "cover the same branch on every OS",
)
def test_grep_delegated_end_to_end_with_rtk_on_path(tmp_path: Path) -> None:
    """The whole wiring with a real executable named `rtk` on PATH: config
    lookup, `which`, subprocess, report line. The in-process tests stub
    `_rtk_run`, so nothing else proves the branch survives a real spawn."""
    import subprocess

    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "rtk"
    stub.write_text(
        "#!" + sys.executable + "\n"
        "import sys\n"
        "sys.stdout.write('src/a.txt:1:alpha\\nsrc/a.txt:3:alpha again\\n')\n"
    )
    stub.chmod(0o755)

    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "a.txt").write_text("alpha\nbeta\nalpha again\n")
    (proj / ".supertool.json").write_text('{"rtk": true}\n')

    env = dict(os.environ)
    env.pop("SUPERTOOL_NO_RTK", None)
    env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
    result = subprocess.run(
        [sys.executable, str(Path(supertool.__file__)), "grep:alpha:src:10"],
        capture_output=True, text=True, cwd=str(proj), env=env, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert ("(2 results in 1 files, scanned ? files — delegated to rtk, "
            "limit 10)") in result.stdout
    assert "src/a.txt:1:alpha" in result.stdout
