"""grep answers the pattern that was written (#1065, #987, #988).

Three defects, one op, three separate mechanisms:

* #1065 — `grep:re:Checks|failed:PATH` rejoins to the pattern `re:Checks|failed`,
  whose first alternation branch is the literal `re:Checks`. The op did exactly
  what it documents; the caller could not see it. The report now names the
  pattern it actually ran.
* #987  — with rtk delegation on (the default everywhere except this repo's own
  `.supertool.json`), the pattern was handed to the system grep as a POSIX BRE,
  where `|`, `+`, `?`, `(` and `{` are literal. A regex whose BRE reading
  happens to match something came back smaller and confidently wrong.
* #988  — count mode rendered `PATH:N`, indistinguishable from `PATH:LINE`.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import supertool


# ---------------------------------------------------------------------------
# #1065 — the pattern that ran is disclosed
# ---------------------------------------------------------------------------

def test_colon_pattern_report_names_the_pattern_that_ran(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("alpha\nbeta\n")
    out = supertool.op_grep("re:alpha|beta", str(f), limit=10)
    assert "re:alpha|beta" in out, (
        "a rejoined pattern must be echoed, or a caller cannot see that "
        "`re:` became the first alternation branch: " + repr(out))


def test_leading_re_prefix_is_named_as_literal(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("alpha\nbeta\n")
    out = supertool.op_grep("re:alpha|beta", str(f), limit=10)
    assert "no `re:` prefix" in out, (
        "`between` has a re: prefix and grep does not; the op that absorbs it "
        "silently is the one that has to say so: " + repr(out))


def test_plain_pattern_gets_no_note(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("alpha\nbeta\n")
    out = supertool.op_grep("alpha", str(f), limit=10)
    assert "pattern read as" not in out


def test_colon_note_survives_count_mode(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("Foo::BAR\n")
    out = supertool.op_grep("Foo::BAR", str(f), limit=10, count_only=True)
    assert "pattern read as" in out and "Foo::BAR" in out


# ---------------------------------------------------------------------------
# #987 — delegation must not change the regex dialect
# ---------------------------------------------------------------------------

class _FakeRtk:
    """Stands in for `rtk grep`, honouring the POSIX/ERE distinction.

    Without `-E` the system grep reads a BRE, where `|`, `+`, `?` and `(` are
    ordinary characters. That is the whole defect, so the fake has to model it
    rather than being a pass-through that agrees with Python by construction.
    """

    def __init__(self) -> None:
        self.calls = []

    def greps(self):
        """Only the delegated *searches* — `read`/`wc` delegate through here
        too, and the auto-read of a matched single file is not the thing under
        test."""
        return [c for c in self.calls if c and c[0] == "grep"]

    def __call__(self, args, timeout: int = 30):
        self.calls.append(list(args))
        pattern, path = args[-2], args[-1]
        if "-E" not in args:
            pattern = re.sub(r"([|+?(){}])", r"\\\1", pattern)
        out = []
        text = Path(path).read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(pattern, line):
                out.append(path + ":" + str(i) + ":" + line)
        return "\n".join(out) + "\n" if out else None


@pytest.fixture()
def rtk_on(monkeypatch: pytest.MonkeyPatch) -> _FakeRtk:
    fake = _FakeRtk()
    monkeypatch.setattr(supertool, "_rtk_enabled", lambda: True)
    monkeypatch.setattr(supertool, "_has_rtk", lambda: "/usr/bin/rtk")
    monkeypatch.setattr(supertool, "_rtk_run", fake)
    return fake


def test_delegated_alternation_answers_the_written_pattern(
        tmp_path: Path, rtk_on: _FakeRtk) -> None:
    f = tmp_path / "code.py"
    f.write_text("alpha\nbeta\ncc|dd\n")
    out = supertool.op_grep("alpha|beta", str(f), limit=10)
    assert "1:alpha" in out and "2:beta" in out, (
        "the BRE reading of `alpha|beta` is the literal text and matches "
        "nothing; the ERE reading is what was written: " + repr(out))
    assert "3:cc|dd" not in out


def test_delegated_quantifier_does_not_answer_the_bre_reading(
        tmp_path: Path, rtk_on: _FakeRtk) -> None:
    """The reported shape: the BRE reading matched, so nothing fell through.

    `ab+c` is one-or-more `b` in Python and the literal text `ab+c` in a BRE,
    and both readings match a line here — so the wrong answer is non-empty and
    never reaches the native walker.
    """
    f = tmp_path / "code.py"
    f.write_text("abbc\nab+c\n")
    out = supertool.op_grep("ab+c", str(f), limit=10)
    assert "1:abbc" in out, repr(out)
    assert "2:ab+c" not in out, (
        "the literal-pipe/plus line is the BRE answer to a question nobody "
        "asked: " + repr(out))


def test_python_only_escapes_are_not_delegated(
        tmp_path: Path, rtk_on: _FakeRtk) -> None:
    f = tmp_path / "code.py"
    f.write_text("x1\nyy\n")
    out = supertool.op_grep(r"\d", str(f), limit=10)
    assert rtk_on.greps() == [], (
        "ERE has no backslash-d; a pattern the delegate cannot express must "
        "not be handed to it")
    assert "1:x1" in out
    assert "delegated to rtk" not in out


def test_lookaround_is_not_delegated(tmp_path: Path, rtk_on: _FakeRtk) -> None:
    f = tmp_path / "code.py"
    f.write_text("foobar\nfoobaz\n")
    out = supertool.op_grep("foo(?=bar)", str(f), limit=10)
    assert rtk_on.greps() == []
    assert "1:foobar" in out and "2:foobaz" not in out


def test_posix_class_is_not_delegated(tmp_path: Path, rtk_on: _FakeRtk) -> None:
    f = tmp_path / "code.py"
    f.write_text("abc\n[:alpha:]\n")
    supertool.op_grep("[[:alpha:]]", str(f), limit=10)
    assert rtk_on.greps() == []


def test_gnu_word_boundary_escape_is_not_delegated(
        tmp_path: Path, rtk_on: _FakeRtk) -> None:
    """`\\<` and `\\>` are GNU word boundaries and Python literals.

    `grep -E '\\<cat\\>'` matches `cat` and not `concatenate`; Python reads the
    same pattern as the literal text `<cat>` and matches neither. Backslash
    followed by punctuation is not automatically shared, so the gate keys off
    the escapes ERE and Python agree on rather than off `\\<alnum>`.
    """
    f = tmp_path / "code.py"
    f.write_text("cat\nconcatenate\n<cat>\n")
    out = supertool.op_grep(r"\<cat\>", str(f), limit=10)
    assert rtk_on.greps() == [], (
        "GNU word boundaries mean something in ERE and nothing in Python")
    assert "3:<cat>" in out, repr(out)


def test_posix_equivalence_class_is_not_delegated(
        tmp_path: Path, rtk_on: _FakeRtk) -> None:
    """`[[=a=]]` and `[[.a.]]` are POSIX bracket constructs, like `[[:alpha:]]`."""
    f = tmp_path / "code.py"
    f.write_text("abc\n")
    supertool.op_grep("[[=a=]]", str(f), limit=10)
    assert rtk_on.greps() == []


def test_dispatch_discloses_the_pattern_it_rejoined(tmp_path: Path) -> None:
    """#1065 was filed against the colon CLI, so pin the tokenizer end to end."""
    f = tmp_path / "code.py"
    f.write_text("alpha\nbeta\n")
    out = supertool.dispatch("grep:re:alpha|beta:" + str(f) + ":10:0")
    assert "pattern read as 're:alpha|beta'" in out, repr(out)
    assert "no `re:` prefix" in out


def test_plain_literal_still_delegates(tmp_path: Path, rtk_on: _FakeRtk) -> None:
    f = tmp_path / "code.py"
    f.write_text("alpha\nbeta\n")
    out = supertool.op_grep("alpha", str(f), limit=10)
    assert rtk_on.greps(), "a literal is exactly what delegation is for"
    assert "delegated to rtk" in out


# ---------------------------------------------------------------------------
# #988 — a count is not a line number
# ---------------------------------------------------------------------------

def test_count_render_cannot_be_read_as_a_line_number(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("import os\nimport sys\ndef main():\n    pass\n")
    out = supertool.op_grep("import", str(f), count_only=True)
    fwd = str(f).replace("\\", "/")
    assert (fwd + ":2\n") not in out, (
        "PATH:2 is indistinguishable from a match at line 2: " + repr(out))
    assert (fwd + ": 2 matches") in out


def test_count_render_is_singular_for_one_match(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("import os\ndef main():\n")
    out = supertool.op_grep("import", str(f), count_only=True)
    fwd = str(f).replace("\\", "/")
    assert (fwd + ": 1 match\n") in out
