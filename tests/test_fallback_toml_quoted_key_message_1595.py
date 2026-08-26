r"""#1595 (part two) - a quoted TOML key names its own cause on the fallback parser.

`_mini_toml_loads` is what parses every `@payload` on Python <3.11, where
stdlib `tomllib` does not exist. Its key scanner accepts a character only if
it is alnum, `_` or `-`, so a quoted key (`"quoted key" = 1`, valid TOML and
accepted by stdlib on 3.11+) never starts, and used to fail with
`bad key at offset N` -- indistinguishable from an actual typo, on every
Python version, including the ones where the input is in fact valid.

This does not widen the grammar (that is the OTHER half of #1595, deliberately
left out here per the issue's own split): a quoted key is still refused below
3.11. What changes is that the refusal now says so, mirroring the message the
same function already gives for a bare `[table]` header four screens up:
cause, version boundary, escape hatch.
"""
import pytest

import supertool


def test_a_double_quoted_key_names_its_cause() -> None:
    with pytest.raises(ValueError) as excinfo:
        supertool._mini_toml_loads(chr(34) + "quoted key" + chr(34) + " = 1")
    msg = str(excinfo.value)
    assert "fallback TOML parser" in msg, msg
    assert "3.11" in msg, msg
    assert "JSON" in msg, msg


def test_a_single_quoted_literal_key_names_its_cause() -> None:
    with pytest.raises(ValueError) as excinfo:
        supertool._mini_toml_loads(chr(39) + "lit key" + chr(39) + " = 1")
    msg = str(excinfo.value)
    assert "fallback TOML parser" in msg, msg
    assert "3.11" in msg, msg
    assert "JSON" in msg, msg


def test_an_actual_bad_key_keeps_the_plain_message() -> None:
    """A genuinely malformed key (a stray punctuation mark) is not a
    quoted-key case and must not be misdiagnosed as one -- the whole point is
    naming the TRUE cause."""
    with pytest.raises(ValueError) as excinfo:
        supertool._mini_toml_loads("@bad = 1")
    msg = str(excinfo.value)
    assert "bad key at offset" in msg, msg
    assert "quoted" not in msg, msg
