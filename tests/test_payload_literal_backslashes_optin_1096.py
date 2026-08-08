"""`literal_backslashes = true` is the safe spelling for a wanted pair (#1096).

The literal route preserves backslashes verbatim, which is right, and the
doubled-backslash detector reads a pair as an escape reflex, which is usually
right. Between them there was no way to say "I meant two characters" -- three
agents lost round-trips to it in one session, and one landed a test that passed
for the wrong reason because a `\\\\n` reached a state file it was never supposed
to reach.

Measured while fixing this, and worth recording because the issue's own
acceptance test reads the other way: the ONE-backslash case already worked.
`\\n` in a literal block reaches the file as backslash-n, no warning, no
doubling. The gap was only ever the pair -- and `"C:\\\\Users"` in Python source,
or a regex matching a literal backslash, are ordinary things to want to write.

So the flag is the precondition for #1087's refusal, not a convenience beside
it. Refusing a pattern that has no fixed position, with no way to opt out,
would make correct content unwritable at every offset -- the loud-for-quiet
trade this repo has a written rule against. The flag turns the refusal into a
decision the author records in the payload.

It is top-level and payload-wide on purpose. A per-field key multiplies with
every content field an op has; a per-op key inside `[[ops]]` reads as scoped to
that op and is not, which is a worse lie than the one being fixed. A payload
whose ops genuinely differ in intent is two payloads.
"""
from pathlib import Path

import supertool

BS = chr(92)
NL = chr(10)
Q3 = chr(39) * 3


def _payload(tmp_path: Path, body: str) -> str:
    p = tmp_path / "p.toml"
    p.write_text(body, encoding="utf-8")
    return "@" + str(p)


def _toml_path(target: Path) -> str:
    return chr(34) + str(target).replace(BS, BS * 2) + chr(34)


WANTED = 'PAT = "' + BS * 2 + 'd+"'


def test_the_flag_lets_a_wanted_pair_through_unchanged(tmp_path: Path) -> None:
    """The acceptance case. One call, no warning, and the bytes on disk are
    exactly the two characters the payload carried."""
    target = tmp_path / "t.py"
    target.write_text('PAT = "x"' + NL, encoding="utf-8")
    body = (
        "literal_backslashes = true" + NL
        + "path = " + _toml_path(target) + NL
        + "old = " + Q3 + 'PAT = "x"' + Q3 + NL
        + "new = " + Q3 + WANTED + Q3 + NL
    )
    out = supertool.dispatch("edit:" + _payload(tmp_path, body))
    assert "ERROR" not in out, out
    assert "literal block" not in out.lower(), "the note fired anyway: " + out
    assert target.read_text(encoding="utf-8") == WANTED + NL


def test_a_single_backslash_needs_no_flag(tmp_path: Path) -> None:
    """Recorded because the issue's own acceptance test assumed otherwise. One
    backslash in a literal block has always gone through clean; a fix that made
    this case require a flag would be a regression dressed as a feature."""
    target = tmp_path / "t.py"
    target.write_text('PAT = "x"' + NL, encoding="utf-8")
    body = (
        "path = " + _toml_path(target) + NL
        + "old = " + Q3 + 'PAT = "x"' + Q3 + NL
        + "new = " + Q3 + 'PAT = "' + BS + 'n"' + Q3 + NL
    )
    out = supertool.dispatch("edit:" + _payload(tmp_path, body))
    assert "ERROR" not in out, out
    assert target.read_text(encoding="utf-8") == 'PAT = "' + BS + 'n"' + NL


def test_the_flag_covers_a_batch(tmp_path: Path) -> None:
    """Top-level, so it applies to every op the payload carries. The whole point
    of the batch route is one parse; the flag lives where the parse happens."""
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    for p in (a, b):
        p.write_text('PAT = "x"' + NL, encoding="utf-8")
    body = "literal_backslashes = true" + NL
    for p in (a, b):
        body += (
            "[[ops]]" + NL
            + 'op = "edit"' + NL
            + "path = " + _toml_path(p) + NL
            + "old = " + Q3 + 'PAT = "x"' + Q3 + NL
            + "new = " + Q3 + WANTED + Q3 + NL
        )
    out = supertool.dispatch("batch:" + _payload(tmp_path, body))
    assert "ERROR" not in out, out
    assert a.read_text(encoding="utf-8") == WANTED + NL
    assert b.read_text(encoding="utf-8") == WANTED + NL


def test_the_flag_inside_an_ops_table_is_refused_not_ignored(tmp_path: Path) -> None:
    """An author who sets it per-op has stated an intent the tool cannot honour
    at that scope. Silently ignoring it would refuse the write while the payload
    says it was allowed -- the receipt and the payload disagreeing, which is the
    family of defect this whole PR is about."""
    target = tmp_path / "t.py"
    target.write_text('PAT = "x"' + NL, encoding="utf-8")
    body = (
        "[[ops]]" + NL
        + 'op = "edit"' + NL
        + "literal_backslashes = true" + NL
        + "path = " + _toml_path(target) + NL
        + "old = " + Q3 + 'PAT = "x"' + Q3 + NL
        + "new = " + Q3 + WANTED + Q3 + NL
    )
    out = supertool.dispatch("batch:" + _payload(tmp_path, body))
    assert "ERROR" in out, out
    assert "top level" in out.lower(), out
    assert target.read_text(encoding="utf-8") == 'PAT = "x"' + NL


def test_the_flag_does_not_disable_the_other_two_backslash_refusals(
    tmp_path: Path,
) -> None:
    """Scope check. #834 (a backslash immediately before the closing quotes) and
    #835 (a shell line ending in one) fire at fixed positions where a second
    spelling always exists, so they were never the thing this flag is for. A
    flag that switched off every backslash guard at once would be a much bigger
    change than the one asked for."""
    target = tmp_path / "t.sh"
    target.write_text("echo hi" + NL, encoding="utf-8")
    body = (
        "literal_backslashes = true" + NL
        + "path = " + _toml_path(target) + NL
        + "old = " + Q3 + "echo hi" + Q3 + NL
        + "new = " + Q3 + "echo a " + BS * 2 + NL + "echo b" + Q3 + NL
    )
    out = supertool.dispatch("edit:" + _payload(tmp_path, body))
    assert "ERROR" in out, out
