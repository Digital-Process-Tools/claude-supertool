"""`grep` and `around` read a file the containment gate never saw (#1166).

`_PATH_ARG_POSITIONS` gated `parts[2]` for both ops, but `_parse_grep_args` /
`_parse_around_args` peel trailing ints and then take `path = args[-1]`. A ':'
anywhere in the PATTERN shifts the real path past the gated slot, so the file
is opened and its CONTENTS printed -- with a caller-chosen regex. The same file
named in the gated slot is refused.

The mirror is the same table read from the other side: with a colon in the
pattern, `parts[2]` is a *pattern fragment*, so a pattern that happens to
contain an absolute path refused a legitimate search of a local file.

Every refusal test asserts the bytes stayed unread AND that the message names
containment -- "path not found" is what a build without the hole would say for
an unrelated reason, and asserting "it errored" would pass on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import supertool

NL = chr(10)
MARKER = "TOPSECRET-1166"

# The escaping calls search for `return`, not for MARKER: the header echoes the
# argument verbatim, so a pattern containing MARKER would put it in the output
# on a refusal too and `MARKER not in out` would assert nothing. `return` only
# ever appears in the OUTSIDE file's body, so its presence means bytes moved.


@pytest.fixture
def outside(tmp_path: Path, monkeypatch) -> Path:
    """cwd is a box under tmp_path; the secret sits one level above it.

    conftest sets SUPERTOOL_ALLOW_OUTSIDE_CWD=1 globally so tmp_path fixtures
    work at all, so a containment test has to put it back or it asserts
    nothing.
    """
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    py = tmp_path / "outside.py"
    py.write_text("def secret_fn():" + NL + "    return " + chr(34) + MARKER
                  + chr(34) + NL, encoding="utf-8")
    box = tmp_path / "box"
    box.mkdir()
    # HIT appears only in this file's BODY -- never in an argument -- so
    # asserting on it proves the search ran and returned, where asserting on
    # "local.py" would pass on the header's verbatim echo of the call.
    (box / "local.py").write_text(
        "HIT literal:/nope/secret.txt HIT" + NL + "second line" + NL,
        encoding="utf-8")
    monkeypatch.chdir(box)
    return py


def test_grep_refuses_when_the_pattern_carries_a_colon(outside: Path) -> None:
    """The reported shape: the ':' pushes the path from parts[2] to parts[3]."""
    out = supertool.dispatch("grep:return|:zz:../outside.py")
    assert MARKER not in out, (
        "the file's CONTENTS were returned through parts[3]; the same file at "
        "parts[2] is refused:" + NL + out)
    assert "escapes cwd" in out, out


def test_grep_refuses_an_absolute_escape_through_the_colon(
    outside: Path
) -> None:
    out = supertool.dispatch("grep:return|:zz:" + outside.as_posix())
    assert MARKER not in out, out
    assert "escapes cwd" in out, out


def test_around_refuses_when_the_pattern_carries_a_colon(outside: Path) -> None:
    out = supertool.dispatch("around:return|:zz:../outside.py:2")
    assert MARKER not in out, (
        "around printed the surrounding lines of an outside file:" + NL + out)
    assert "escapes cwd" in out, out


def test_around_refuses_an_absolute_escape_through_the_colon(
    outside: Path
) -> None:
    out = supertool.dispatch(
        "around:return|:zz:" + outside.as_posix() + ":2")
    assert MARKER not in out, out
    assert "escapes cwd" in out, out


def test_grep_still_refuses_the_plain_two_token_form(outside: Path) -> None:
    """Dropping the positional entry must lose no coverage: parts[2] IS
    args[-1] when the pattern has no ':', so the computed guard covers it."""
    out = supertool.dispatch("grep:def:../outside.py")
    assert MARKER not in out, out
    assert "escapes cwd" in out, out


def test_around_still_refuses_the_plain_form(outside: Path) -> None:
    out = supertool.dispatch("around:secret_fn:../outside.py:1")
    assert MARKER not in out, out
    assert "escapes cwd" in out, out


def test_around_line_promotion_is_still_gated(outside: Path) -> None:
    """#1135's conditional guard fires on parts[1] once it becomes a path.

    It lives inside `_around_line_delegation`, not in the table, so dropping
    the `around` table entry must not disturb it.
    """
    out = supertool.dispatch("around:../outside.py:2")
    assert MARKER not in out, out
    assert "escapes cwd" in out, out


def test_grep_no_longer_over_contains_a_path_shaped_pattern(
    outside: Path
) -> None:
    """The mirror: with a ':' in the pattern, parts[2] is a pattern fragment.

    Gating it refused a legitimate search of a local file, naming a file the
    caller never asked to open.
    """
    out = supertool.dispatch("grep:literal:/nope/secret.txt:local.py")
    assert "escapes cwd" not in out, (
        "the only path in this call is local.py, which is inside cwd; the "
        "refusal named a file the caller never asked to open:" + NL + out)
    assert "HIT" in out, ("the search was allowed but returned nothing, so the "
                          "assertion above proved nothing:" + NL + out)


def test_around_no_longer_over_contains_a_path_shaped_pattern(
    outside: Path
) -> None:
    out = supertool.dispatch("around:literal:/nope/secret.txt:local.py:1")
    assert "escapes cwd" not in out, out
    assert "HIT" in out, out


def test_the_colon_disclosure_names_the_path_it_resolved(
    outside: Path
) -> None:
    """The receipt was honest about the split and silent about the path.

    Naming the pattern alone reads as complete while the reader still cannot
    see which of the remaining tokens became the file.
    """
    out = supertool.dispatch("grep:literal:/nope/secret.txt:local.py")
    assert "pattern read as" in out, out
    assert "local.py" in out.split(NL)[1], (
        "the disclosure line names the pattern but not the path it read:"
        + NL + out)
