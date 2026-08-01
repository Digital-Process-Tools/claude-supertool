from __future__ import annotations

from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# #734 — `around:PATTERN:PATH` with a swapped-order call (line number where
# PATH belongs) said "wrong CWD?" — advice that cannot help, because the cwd
# was never the problem. When the unresolved path is all-digits, name the
# mistake and point at `around_line` instead of the useless cwd hint.
# ---------------------------------------------------------------------------

def test_around_digit_path_suggests_around_line_no_n() -> None:
    """Three-token shape from the issue: around:PATTERN:PATH, N omitted."""
    out = supertool.op_around("presets/gitlab/mr.py", "681")
    assert "ERROR: file not found: 681" in out
    assert "wrong CWD?" not in out
    assert "around_line:presets/gitlab/mr.py:681" in out


def test_around_digit_path_suggests_around_line_with_n() -> None:
    """Four-token shape: around:PATTERN:PATH:N, matching the CHANGELOG repro."""
    out = supertool.op_around("CHANGELOG.md", "1", 25)
    assert "ERROR: file not found: 1" in out
    assert "wrong CWD?" not in out
    assert "around_line:CHANGELOG.md:1" in out


def test_around_dispatch_digit_path_suggests_around_line() -> None:
    """Same check through the real CLI colon-parsing path, not just the
    function called directly — dispatch is where #734 was actually hit."""
    out = supertool.dispatch("around:presets/gitlab/mr.py:681:8")
    assert "wrong CWD?" not in out
    assert "around_line:presets/gitlab/mr.py:681" in out


def test_around_non_digit_missing_path_keeps_cwd_hint(tmp_path: Path,
                                                        monkeypatch) -> None:
    """A genuinely missing, non-numeric path is the case the cwd hint is
    actually useful for — it must not be swallowed by the new branch."""
    monkeypatch.chdir(tmp_path)
    out = supertool.op_around("TODO", "no/such/dir/file.py")
    assert "ERROR: file not found: no/such/dir/file.py" in out
    assert "wrong CWD?" in out
    assert "around_line" not in out


def test_around_digit_named_file_that_exists_reads_normally(
    tmp_path: Path,
) -> None:
    """A repo can genuinely contain a digit-named file. The suggestion must
    only ever fire on a *failed* resolution — it must never redirect a call
    whose PATH argument happens to resolve."""
    f = tmp_path / "681"
    f.write_text("before\ntarget\nafter\n")
    out = supertool.op_around("target", str(f))
    assert "ERROR" not in out
    assert "target" in out
    assert "around_line" not in out


def test_grep_digit_path_does_not_suggest_around_line() -> None:
    """`grep` is pattern-first like `around`, but has no PATH:LINE sibling
    with reversed argument order — there is nothing to suggest. The digit
    heuristic is scoped to `around`, not applied blanket to every caller of
    the shared _path_not_found() helper."""
    out = supertool.op_grep("TODO", "681")
    assert "ERROR: path not found: 681" in out
    assert "around_line" not in out
    assert "wrong CWD?" in out
