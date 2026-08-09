"""The payload route has no expression evaluation, and the front page said it did.

`.supertool.json`'s `introduction` is the first text every session reads. It
taught `chr(10)`/`chr(27)` as the way to put a newline or an ESC into payload
content. Measured 2026-08-08: a TOML payload is parsed, not evaluated, so the
seven characters `c h r ( 1 0 )` land in the file verbatim and the write
receipt says `edited`. That is a silently corrupted file behind a green
receipt, arriving through the documentation instead of the validator (#1134).

The advice existed because escape problems are real, so deleting it is not
enough on its own -- the replacement has to answer the same question. It does:
a real newline inside a triple-single-quoted literal block, or a
triple-double-quoted basic block where TOML escapes ARE processed.

Two tests, and the second is the guard the issue asked for:

* the behaviour is measured, not asserted from prose -- `chr(10)` goes through
  the real edit route and the bytes on disk are read back;
* every shipped guidance surface is then scanned for the idiom that
  measurement just proved broken. Prose cannot be diffed against prose, but it
  CAN be checked against a measurement, and that is the direction that catches
  the drift. If expression evaluation is ever added, the first test fails and
  names the second one, so the surfaces get revisited rather than silently
  becoming right again by accident.

CHANGELOG.md and changelog.d/ are deliberately out of scope: a historical
record has to be able to quote the wrong idiom in order to say it was wrong.
"""
from pathlib import Path
from typing import List

import supertool

NL = chr(10)
Q3 = chr(39) * 3
CHR10 = "chr" + chr(40) + "10" + chr(41)
CHR27 = "chr" + chr(40) + "27" + chr(41)

REPO = Path(__file__).resolve().parent.parent

# Every surface that TEACHES the payload route. Not the changelog, which
# records what was once taught.
GUIDANCE_FILES = (".supertool.json", ".supertool.example.json", "README.md")
GUIDANCE_GLOBS = ("docs/**/*.md",)


def _guidance_surfaces() -> List[Path]:
    found = [REPO / name for name in GUIDANCE_FILES]
    for pattern in GUIDANCE_GLOBS:
        found.extend(sorted(REPO.glob(pattern)))
    return [p for p in found if p.is_file()]


def test_chr10_in_a_payload_writes_seven_literal_characters(tmp_path: Path) -> None:
    """The measurement the introduction contradicted.

    Nothing on this route evaluates an expression, so the payload's `chr(10)`
    is text. The assertion is on the bytes, not on the receipt -- the receipt
    said `edited` the whole time.
    """
    target = tmp_path / "t.txt"
    target.write_text("alpha" + NL + "omega" + NL, encoding="utf-8")
    payload = tmp_path / "p.toml"
    payload.write_text(
        "path = " + chr(34) + str(target).replace(chr(92), chr(92) * 2) + chr(34) + NL
        + "old = " + Q3 + "omega" + Q3 + NL
        + "new = " + Q3 + "beta" + CHR10 + "gamma" + Q3 + NL,
        encoding="utf-8",
    )
    supertool.dispatch("edit:@" + str(payload))

    on_disk = target.read_text(encoding="utf-8")
    assert "beta" + CHR10 + "gamma" in on_disk, on_disk
    assert on_disk.count(NL) == 2, (
        "chr(10) produced a line break -- expression evaluation now exists on "
        "the payload route, so the guidance surfaces checked by "
        "test_no_guidance_surface_teaches_an_idiom_the_route_does_not_have "
        "must be revisited: " + repr(on_disk)
    )


def test_a_real_newline_in_a_literal_block_is_the_working_idiom(tmp_path: Path) -> None:
    """The replacement advice, pinned. Deleting the wrong sentence without
    proving the right one leaves the next author to re-invent it."""
    target = tmp_path / "t.txt"
    target.write_text("alpha" + NL + "omega" + NL, encoding="utf-8")
    payload = tmp_path / "p.toml"
    payload.write_text(
        "path = " + chr(34) + str(target).replace(chr(92), chr(92) * 2) + chr(34) + NL
        + "old = " + Q3 + "omega" + Q3 + NL
        + "new = " + Q3 + "beta" + NL + "gamma" + Q3 + NL,
        encoding="utf-8",
    )
    supertool.dispatch("edit:@" + str(payload))

    assert target.read_text(encoding="utf-8") == "alpha" + NL + "beta" + NL + "gamma" + NL


def test_no_guidance_surface_teaches_an_idiom_the_route_does_not_have() -> None:
    """The guard. Two hand-maintained descriptions of one behaviour is how this
    drifted; nothing compared them, so nothing caught it.

    A surface is free to describe the payload route however it likes -- it is
    not free to name an idiom that the route does not implement.
    """
    offenders = []
    for path in _guidance_surfaces():
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            if CHR10 in line or CHR27 in line:
                offenders.append(str(path.relative_to(REPO)) + ":" + str(number))

    assert not offenders, (
        "guidance surfaces name " + CHR10 + "/" + CHR27 + ", which the payload "
        "route does not evaluate -- following them writes those characters "
        "verbatim into the file. Say 'a real newline inside the " + Q3
        + " literal block' instead. Offenders: " + ", ".join(offenders)
    )
