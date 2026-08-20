"""#1830 -- `_toml_delimiter_hint` returned early on an EVEN delimiter count.

Parity is the wrong test. What breaks the parse is a delimiter run appearing
**inside a value**: the block closes there, and the remainder of that line is
read as TOML syntax. That happens at any count, and the even one is the likely
shape for a payload that is *about* the literal-block syntax -- one quoted run
in `old`, one in `new`.

The replacement for parity is structural and needs no successful parse: walk
each opener, find its closing run, and look at what is left on that line. Blank
or a `#` comment means the block closed where it was meant to; any other text
means the delimiter closed it early.

The odd-count trigger is kept as well, so #394 behaviour (pinned by
`test_an_odd_delimiter_run_is_already_explained`) is unchanged.

Nothing in this file writes the delimiter literally, for the reason the issue
is about: a source line carrying it would break the very payload that creates
this file. `Q3` is built from `chr(39)`.
"""

from __future__ import annotations

import supertool

Q3 = chr(39) * 3
NL = chr(10)
PATH_LINE = "path = " + chr(34) + "x.py" + chr(34)


def test_two_delimiter_runs_inside_values_fire_the_hint() -> None:
    """The reported payload: an even count, once in `old` and once in `new`.
    Before #1830 this returned the empty string and the caller got a bare
    column number with no mention of the delimiter."""
    raw = (
        PATH_LINE + NL
        + "old = " + Q3 + "quote the " + Q3 + " delimiter" + Q3 + NL
        + "new = " + Q3 + "quote the " + Q3 + " delimiter twice" + Q3 + NL
    )
    assert raw.count(Q3) % 2 == 0, "the point of the case is an even count"
    out = supertool._toml_delimiter_hint(raw)
    assert out != "", "even count, delimiter inside both values, no hint"
    assert Q3 in out, out
    assert "basic" in out, out


def test_the_delimiter_used_correctly_as_a_block_quote_stays_silent() -> None:
    """The must-not-fire control. Without it the assertion above passes on a
    hint that fires on every payload containing three quotes."""
    raw = (
        PATH_LINE + NL
        + "old = " + Q3 + NL + "def f():" + NL + "    return 1" + NL + Q3 + NL
        + "new = " + Q3 + NL + "def f():" + NL + "    return 2" + NL + Q3 + NL
        + "this line is the unrelated syntax error" + NL
    )
    assert raw.count(Q3) % 2 == 0
    out = supertool._toml_delimiter_hint(raw)
    assert out == "", out


def test_a_comment_after_the_closing_run_is_not_content() -> None:
    """A trailing `#` comment is valid TOML, so trailing text is only a finding
    when it is not one."""
    raw = PATH_LINE + NL + "old = " + Q3 + "x" + Q3 + "  # note" + NL
    out = supertool._toml_delimiter_hint(raw)
    assert out == "", out


def test_no_literal_block_opens_stays_silent() -> None:
    """#395 pin, restated because the new walk could reach it: a run carried
    harmlessly inside a basic string is not a delimiter problem."""
    raw = ("new = " + chr(34) + "isn" + chr(39) + "t it" + Q3 + " odd" + chr(34)
           + NL + "bogus key here" + NL)
    out = supertool._toml_delimiter_hint(raw)
    assert out == "", out


def test_the_message_locates_the_run_that_closed_early() -> None:
    """A column number with no line is what the reporter had. The offending run
    is at a known offset the moment it is detected."""
    raw = (
        PATH_LINE + NL
        + "old = " + Q3 + "a" + Q3 + " b" + Q3 + NL
        + "new = " + Q3 + "c" + Q3 + " d" + Q3 + NL
    )
    out = supertool._toml_delimiter_hint(raw)
    assert "line 2" in out, out


def test_the_hint_still_routes_its_glyph_through_mark(monkeypatch) -> None:
    """Same rule as #395 one message over: plain mode must not leak U+21B3."""
    raw = (
        PATH_LINE + NL
        + "old = " + Q3 + "a" + Q3 + " b" + Q3 + NL
        + "new = " + Q3 + "c" + Q3 + " d" + Q3 + NL
    )
    monkeypatch.setenv("SUPERTOOL_PLAIN", "1")
    out = supertool._toml_delimiter_hint(raw)
    assert chr(0x21B3) not in out, out
