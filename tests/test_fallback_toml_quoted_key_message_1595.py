r"""#1595 - the fallback TOML parser accepts a quoted key, same as stdlib.

`_mini_toml_loads` is what parses every `@payload` on Python <3.11, where
stdlib `tomllib` does not exist. Its key scanner used to accept a character
only if it was alnum, `_` or `-`, so a quoted key (`"quoted key" = 1`, valid
TOML and accepted by stdlib on 3.11+) never started, and after #2008 failed
with a refusal naming its own cause ("not supported by the fallback TOML
parser ... use a bare key or JSON"). This is the OTHER half of #1595: the
grammar itself is now widened, so a quoted key parses -- both `"basic"` (with
escapes, via the same machinery string VALUES already use) and `'literal'`
(no escapes) -- matching what stdlib accepts on 3.11+.

Dotted keys (`a.b = 1`) are a separate, still-unsupported construct and are
out of scope here -- the issue's own "two instances" framing names only the
quoted-key grammar gap and the refusal message, both now closed.
"""
import pytest

import supertool


def test_a_double_quoted_key_is_accepted() -> None:
    result = supertool._mini_toml_loads(chr(34) + "quoted key" + chr(34) + " = 1")
    assert result == {"quoted key": 1}


def test_a_double_quoted_key_decodes_escapes() -> None:
    # same basic-string escape machinery string VALUES use
    result = supertool._mini_toml_loads(chr(34) + r"kk\nbb" + chr(34) + " = 1")
    assert result == {"kk\nbb": 1}


def test_a_single_quoted_literal_key_is_accepted() -> None:
    result = supertool._mini_toml_loads(chr(39) + "lit key" + chr(39) + " = 1")
    assert result == {"lit key": 1}


def test_a_single_quoted_literal_key_keeps_backslashes_literal() -> None:
    result = supertool._mini_toml_loads(chr(39) + r"lit\nkey" + chr(39) + " = 1")
    assert result == {"lit\\nkey": 1}


def test_an_unterminated_double_quoted_key_names_its_cause() -> None:
    with pytest.raises(ValueError) as excinfo:
        supertool._mini_toml_loads(chr(34) + "unterminated = 1")
    assert "unterminated quoted key" in str(excinfo.value)


def test_an_actual_bad_key_keeps_the_plain_message() -> None:
    """A genuinely malformed key (a stray punctuation mark) is not a
    quoted-key case and must not be misdiagnosed as one -- the whole point is
    naming the TRUE cause."""
    with pytest.raises(ValueError) as excinfo:
        supertool._mini_toml_loads("@bad = 1")
    msg = str(excinfo.value)
    assert "bad key at offset" in msg, msg
    assert "quoted" not in msg, msg
