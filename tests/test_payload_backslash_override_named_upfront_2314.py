r"""#2314 -- the even-backslash-run refusal already names `literal_backslashes`
and gives a ready-to-paste example, but only at the very end of the message,
after every occurrence has been listed. On a payload with several refused
fields (a shape #2211's lane hit repeatedly) the override is the one thing an
author who already knows they meant the doubled run needs, and it used to
cost a full scroll past located occurrences to find it.

This does not change WHEN the refusal fires -- only where the override is
first mentioned. The "must fire" half lives in test_payload_write_backslash_
refusal_1087.py and test_payload_backslash_occurrences_1808_1814_1819.py;
this file only pins the new position.
"""
from pathlib import Path

import supertool

BS = chr(92)
NL = chr(10)
Q3 = chr(39) * 3


def _payload(tmp_path: Path, body: str, name: str = "p.toml") -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return "@" + str(p)


def _target(tmp_path: Path, body: str, name: str = "t.py") -> Path:
    t = tmp_path / name
    t.write_text(body, encoding="utf-8")
    return t


def _toml_path(target: Path) -> str:
    return chr(34) + str(target).replace(BS, BS * 2) + chr(34)


LITERAL_DOUBLE = "new = " + Q3 + 'PAT = "' + BS * 2 + 'd+"' + Q3


def _edit_body(target: Path) -> str:
    return (
        "path = " + _toml_path(target) + NL
        + "old = " + Q3 + 'PAT = "x"' + Q3 + NL
        + LITERAL_DOUBLE + NL
    )


def test_the_override_is_named_before_the_first_occurrence_is_listed(tmp_path: Path) -> None:
    """MUST fire, and must say `literal_backslashes` early: a caller re-hitting
    this refusal across several payloads should not have to scroll past a
    located occurrence block to find the one line that lets them move on."""
    target = _target(tmp_path, 'PAT = "x"' + NL)
    out = supertool.dispatch("edit:" + _payload(tmp_path, _edit_body(target)))
    assert "literal_backslashes" in out, out
    first_mention = out.index("literal_backslashes")
    first_occurrence = out.index("even backslash run")
    assert first_mention < first_occurrence, (
        "the override is mentioned only after the occurrence list, not before it:\n" + out)


def test_the_full_detail_and_ready_to_paste_example_are_still_present(tmp_path: Path) -> None:
    """MUST NOT regress: the detailed per-occurrence render and the
    ready-to-paste `literal_backslashes = [...]` example this repo already
    relies on (#1839) must survive the reordering unchanged."""
    target = _target(tmp_path, 'PAT = "x"' + NL)
    out = supertool.dispatch("edit:" + _payload(tmp_path, _edit_body(target)))
    assert "TWO OPPOSITE fixes" in out, out
    assert 'literal_backslashes = ["new"]' in out, out
    assert "at payload line" in out, out
