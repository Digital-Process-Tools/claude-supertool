"""#1794 item 5 -- `literal_backslashes`'s doubled-backslash refusal asks the
same question ("half the run, or the run as written?") of every field, but
the right answer skews hard by the target's own extension: a payload writing
`.json` or `.py` usually WANTS the doubled form (a real backslash landing in
a JSON/Python string literal), while one writing prose usually does not. The
refusal fires correctly every time -- the reporter's own complaint is that it
then makes the caller do the same lookup by hand on every occurrence.

Additive: the hint changes nothing about WHETHER the refusal fires, only
whether it also says which of its two listed fixes is the common case here.
"""

from pathlib import Path

import pytest

import supertool

BS = chr(92)
NL = chr(10)
Q = chr(34)
Q3 = chr(39) * 3


def _write(tmp_path: Path, body: str) -> str:
    p = tmp_path / "p.toml"
    p.write_text(body, encoding="utf-8")
    return "@" + str(p)


def test_a_json_target_is_nudged_toward_the_literal_fix(tmp_path: Path) -> None:
    body = (
        "path = " + Q + "data.json" + Q + NL
        + "content = " + Q3 + "{" + chr(34) + "k" + chr(34) + ": "
        + chr(34) + "a" + BS * 2 + "nb" + chr(34) + "}" + Q3 + NL
    )
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file(_write(tmp_path, body))
    msg = str(excinfo.value)
    assert ".json" in msg, msg
    assert "REAL" in msg, f"no extension-keyed nudge toward the literal fix: {msg}"


def test_a_prose_target_is_nudged_toward_the_half_fix(tmp_path: Path) -> None:
    body = (
        "path = " + Q + "notes.md" + Q + NL
        + "content = " + Q3 + "a" + BS * 2 + "b" + Q3 + NL
    )
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file(_write(tmp_path, body))
    msg = str(excinfo.value)
    assert ".md" in msg, msg
    assert "FIRST fix" in msg, f"no extension-keyed nudge toward the half fix: {msg}"


def test_an_unopinionated_extension_gets_no_hint(tmp_path: Path) -> None:
    """The control: an extension this repo has no view on adds nothing --
    a wrong nudge costs more than a silent one."""
    body = (
        "path = " + Q + "data.bin" + Q + NL
        + "content = " + Q3 + "a" + BS * 2 + "b" + Q3 + NL
    )
    with pytest.raises(ValueError) as excinfo:
        supertool._load_at_file(_write(tmp_path, body))
    msg = str(excinfo.value)
    assert "REAL one" not in msg and "FIRST fix" not in msg, (
        f"an unopinionated extension still got a directional nudge: {msg}")


def test_mixed_extensions_across_a_batch_get_no_hint() -> None:
    """A batch writing both `.json` and `.md` cannot be nudged either way
    without guessing which field the caller is actually asking about."""
    parsed = {"ops": [{"path": "a.json"}, {"path": "b.md"}]}
    assert supertool._payload_extension_bs_hint(parsed) == ""
