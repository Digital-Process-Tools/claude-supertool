"""Autocorrect for over-escaped backslashes in `:s/PAT/REPL/[flags]`.

Kevin keeps writing 4 backslashes in `:s` patterns when 2 are correct.
Bash single-quote passes `\\\\` through as 4 chars; the regex then sees
`\\\\` = match 2 literal backslashes. To match ONE literal backslash
(e.g. a PHP namespace `\\Foo`), the correct bash-quoted form is `\\\\Foo`
(2 backslashes → 2 chars to regex → matches one `\\`).

When the original pattern matches zero times AND contains 4 consecutive
backslashes, the handler retries with each `\\\\\\\\` halved to `\\\\`. If
that retry matches, it's used and a hint is appended to the log so the
caller learns the correct form for next time.

Note about Python string literals in this file: `\\\\\\\\` (8 chars in
source) is 4 literal backslashes at runtime — the form Kevin would type
in bash. `\\\\` (4 chars in source) is 2 literal backslashes — the correct
form. We test both.
"""
from __future__ import annotations

from pathlib import Path

import supertool


def test_over_escaped_backslash_autocorrects(tmp_path: Path) -> None:
    """`:s/\\\\\\\\Http\\\\\\\\Mock/X/g` on file with `\\Http\\Mock` matches via retry."""
    f = tmp_path / "x.php"
    f.write_text("use Acme\\Http\\Mock;\n")
    # Pattern as it would arrive after bash: 4-4-4 backslashes.
    out = supertool.op_vim(str(f), ":s/\\\\\\\\Http\\\\\\\\Mock/X/g")
    assert "ERROR" not in out, out
    assert f.read_text() == "use AcmeX;\n", f.read_text()


def test_correct_two_backslash_form_still_works(tmp_path: Path) -> None:
    """`:s/\\\\Http\\\\Mock/X/g` (correct form, 2 bs each) matches directly — no retry."""
    f = tmp_path / "x.php"
    f.write_text("use Acme\\Http\\Mock;\n")
    out = supertool.op_vim(str(f), ":s/\\\\Http\\\\Mock/X/g")
    assert "ERROR" not in out, out
    assert f.read_text() == "use AcmeX;\n", f.read_text()
    # No retry hint when the original pattern matched.
    assert "autocorrect" not in out.lower()


def test_autocorrect_emits_retry_hint(tmp_path: Path) -> None:
    """When the autocorrect fires, the receipt contains a hint about it."""
    f = tmp_path / "x.php"
    f.write_text("use Acme\\Http\\Mock;\n")
    # Same form as the first test — assert specifically on the hint text.
    out = supertool.op_vim(str(f), ":s/\\\\\\\\Http\\\\\\\\Mock/X/g")
    assert "ERROR" not in out, out
    assert "autocorrect" in out.lower(), out
    # The hint should mention the halved pattern.
    assert "halved" in out.lower(), out


def test_no_backslashes_unchanged(tmp_path: Path) -> None:
    """Regression: plain `:s/foo/X/` with no backslashes is untouched."""
    f = tmp_path / "x.txt"
    f.write_text("foo bar\n")
    out = supertool.op_vim(str(f), ":s/foo/X/")
    assert "ERROR" not in out, out
    assert f.read_text() == "X bar\n"
    assert "autocorrect" not in out.lower()


def test_legit_four_backslash_pattern_when_no_match(tmp_path: Path) -> None:
    """If the file has `\\\\Foo` (literal double backslash) and the pattern is
    `:s/\\\\\\\\\\\\\\\\Foo/X/` (8 bs in bash = needs literal `\\\\` to match),
    the original pattern matches — no retry needed.

    This is the legitimate 4-backslash case. After bash single-quoting,
    8 backslashes become 8 chars; regex sees `\\\\\\\\Foo` = match 4 literal
    backslashes? No — `\\\\\\\\` to the regex = `\\\\\\\\` chars = matches 2
    literal `\\\\` pairs = 2 backslashes. Confusing, but: testing what we do.
    """
    f = tmp_path / "x.txt"
    # File has 2 literal backslashes then Foo.
    f.write_text("\\\\Foo\n")
    # Pattern with 4 runtime backslashes (Python source `\\\\\\\\` = 4 chars).
    # Regex `\\\\\\\\` (4 chars) matches 2 literal backslashes. So it matches.
    out = supertool.op_vim(str(f), ":s/\\\\\\\\Foo/X/")
    assert "ERROR" not in out, out
    assert f.read_text() == "X\n", f.read_text()
    # No retry because the original pattern matched.
    assert "autocorrect" not in out.lower()


def test_autocorrect_with_range(tmp_path: Path) -> None:
    """The autocorrect also fires under a `:%s/...` range when no match."""
    f = tmp_path / "x.php"
    f.write_text("a = Acme\\Foo;\nb = Acme\\Foo;\n")
    out = supertool.op_vim(str(f), ":%s/Acme\\\\\\\\Foo/X/g")
    assert "ERROR" not in out, out
    assert f.read_text() == "a = X;\nb = X;\n", f.read_text()
    assert "autocorrect" in out.lower()
