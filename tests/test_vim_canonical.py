"""Canonical vim behavior parity test.

Compiled from vim's own tutorial and quick-reference (`:help motion.txt`,
`:help change.txt`, `vimtutor`). Each case mirrors what a real vim user
would type and expect. Run with:

    pytest tests/test_vim_canonical.py --no-cov -v

Cases the impl doesn't support yet show up as xfail or fail — those are
the gap list for the next iteration.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import supertool


# (name, initial content, vi script, expected content after)
CASES = [
    # --- Motion ---
    ("gg goes to top",          "a\nb\nc\n",          "G␞gg␞iX",      "Xa\nb\nc\n"),
    ("G goes to last line",     "a\nb\nc\n",          "G␞iEND",       "a\nb\nENDc\n"),
    ("3G goes to line 3",       "a\nb\nc\nd\n",       "3G␞i*",        "a\nb\n*c\nd\n"),
    ("0 jumps to BOL",          "hello\n",            "$␞0␞iX",       "Xhello\n"),
    ("$ jumps to last char",    "hello\n",            "$␞iX",         "hellXo\n"),
    ("/PAT finds forward",      "alpha beta gamma\n", "/beta␞iX",     "alpha Xbeta gamma\n"),

    # --- Inserts ---
    ("i inserts before cursor", "world\n",            "ihello ",      "hello world\n"),
    ("a appends after",         "ab\n",               "ax",           "axb\n"),
    ("I inserts at BOL",        "  text\n",           "$␞I>",         ">  text\n"),
    ("A appends at EOL",        "hello\n",            "A!",           "hello!\n"),
    ("o opens line below",      "first\nsecond\n",    "gg␞onew",      "first\nnew\nsecond\n"),
    ("O opens line above",      "second\n",           "Oabove",       "above\nsecond\n"),

    # --- Deletes ---
    ("x deletes char at cursor","abc\n",              "x",            "bc\n"),
    ("3x deletes 3 chars",      "abcdef\n",           "3x",           "def\n"),
    ("dd deletes line",         "a\nb\nc\n",          "2G␞dd",        "a\nc\n"),
    ("3dd deletes 3 lines",     "a\nb\nc\nd\ne\n",    "2G␞3dd",       "a\ne\n"),
    ("D deletes to EOL",        "keep|drop\n",        "/|␞D",         "keep\n"),
    ("dw deletes word",         "foo bar baz\n",      "dw",           "bar baz\n"),
    ("d$ deletes to EOL",       "keep drop\n",        "/d␞d$",        "keep \n"),

    # --- Change verbs ---
    ("ciw changes inner word",  "foo bar baz\n",      "/bar␞ciwZZ",   "foo ZZ baz\n"),
    ("cw changes to word end",  "foo bar baz\n",      "/bar␞cwZZ",    "foo ZZ baz\n"),
    ("cc rewrites line",        "old line\nkeep\n",   "ccnew",        "new\nkeep\n"),
    ("c$ changes to EOL",       "keep drop\n",        "/d␞c$NEW",     "keep NEW\n"),
    ("ci\" changes inside \"",  'x = "old"\n',         '/"␞ci"new',   'x = "new"\n'),
    ("ci' changes inside '",    "x = 'old'\n",         "/'␞ci'new",   "x = 'new'\n"),
    ("ci( changes inside ()",   "f(a, b)\n",           "/(␞ci(x",     "f(x)\n"),
    ("ci[ changes inside []",   "a = [1,2]\n",         "/[␞ci[9",     "a = [9]\n"),
    ("ci{ changes inside {}",   "d = {x:1}\n",         "/{␞ci{y:2",   "d = {y:2}\n"),

    # --- Char-find ---
    ("f<c> finds char on line", "abcdef\n",           "fc␞iX",        "abXcdef\n"),
    ("F<c> finds back on line", "abcdef\n",           "$␞Fc␞iX",      "abXcdef\n"),
    ("t<c> stops before char",  "abcdef\n",           "tc␞iX",        "aXbcdef\n"),

    # --- Word motion (vimhelp.org/usr_02.txt) ---
    ("w moves to next word",    "hello world\n",      "w␞iX",         "hello Xworld\n"),
    ("b moves back one word",   "hello world\n",      "$␞b␞iX",       "hello Xworld\n"),
    ("e moves to word end",     "hello world\n",      "e␞aX",          "helloX world\n"),
    ("^ moves to first non-blank", "   text\n",       "^␞iX",         "   Xtext\n"),

    # --- Yank/paste ---
    ("yy + p duplicates line",  "hello\nworld\n",     "yy␞p",         "hello\nhello\nworld\n"),
    ("yw + p pastes word",      "foo bar\n",          "yw␞$␞p",       "foo barfoo\n"),

    # --- Replace ---
    ("r replaces single char",  "abc\n",              "rX",           "Xbc\n"),

    # --- Join ---
    ("J joins next line",       "foo\nbar\nbaz\n",    "J",            "foo bar\nbaz\n"),

    # --- Ex substitute ---
    (":s replaces first match", "foo foo\n",          ":s/foo/X/",    "X foo\n"),
    (":s/g replaces all",       "foo foo\n",          ":s/foo/X/g",   "X X\n"),
    (":%s alias works",         "foo foo\n",          ":%s/foo/X/g",  "X X\n"),
    ("%s bare alias works",     "foo foo\n",          "%s/foo/X/g",   "X X\n"),
    (":s/gi case-insensitive",  "Foo FOO foo\n",      ":s/foo/X/gi",  "X X X\n"),
    (":s backref",              "a=1\n",              ":s/(\\w+)=(\\d+)/\\2:\\1/", "1:a\n"),

    # --- Counts ---
    ("5i- repeats insert 5x",   "ok\n",               "5i-",          "-----ok\n"),

    # --- :r FILE ---
    # covered separately in test_vi.py with tmp file
]


@pytest.mark.parametrize("name, initial, script, expected", CASES, ids=[c[0] for c in CASES])
def test_canonical_vim_behavior(
    name: str, initial: str, script: str, expected: str, tmp_path: Path, monkeypatch
) -> None:
    """Each canonical vim case: write `initial`, run `script`, check output."""
    # Isolate cursor cache so cases don't pollute each other
    monkeypatch.setenv("SUPERTOOL_VIM_NO_PERSIST", "1")
    f = tmp_path / "x.txt"
    f.write_text(initial)
    out = supertool.op_vim(str(f), script)
    actual = f.read_text(encoding="utf-8")
    assert actual == expected, f"{name}\n  script: {script!r}\n  out: {out}\n  got: {actual!r}\n  want: {expected!r}"
