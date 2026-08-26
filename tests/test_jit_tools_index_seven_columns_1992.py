"""The tools index must carry the same column count claude-jit-context derives (#1992).

`rebuild-tsv.sh:308` in the installed claude-jit-context 0.6.0 writes seven
tab-separated fields for a tools row -- `tool`, `match`, `file`, `mode`,
`require`, `forbid`, `requires` -- where `requires` names a binary a rule's
own enforcement depends on (#203 there). This repository's committed
`.claude/jit-context/tools/00-manual/00-index.tsv` carried six, so running
the real generator against this tree was not the no-op `CLAUDE.md` promised:
it silently widened every row by one column.

`requires` is read by `pre-tool-hook.sh` as `tf[7]` (r_requires). A short
field reads as an empty string there -- `split(tline, tf, "\t")` leaves an
unset `tf[7]` empty in awk -- so the six-column tree never crashed the hook;
it just could not be regenerated without changing shape. This test pins the
column count going forward so that gap comes back as a red here instead of
as a hand-edit the next lane has to perform and CLAUDE.md has to keep
promising away.

No frontmatter file in this tree declares `requires:` today, so every row's
7th field is empty; that is asserted explicitly so a future rule that adds
one is exercised by this same test rather than by a first user in the wild.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TSV = REPO / ".claude" / "jit-context" / "tools" / "00-manual" / "00-index.tsv"
TAB = "\t"


def test_every_tools_row_has_seven_tab_separated_fields():
    rows = [r for r in TSV.read_text(encoding="utf-8").splitlines() if r.strip()]
    assert rows, "tools index is empty; the shape changed"
    for row in rows:
        fields = row.split(TAB)
        assert len(fields) == 7, (
            "claude-jit-context 0.6.0's rebuild-tsv.sh writes 7 fields "
            "(tool, match, file, mode, require, forbid, requires); got "
            "{0}: {1!r}".format(len(fields), row)
        )


def test_no_row_declares_a_requires_binary_today():
    rows = [r for r in TSV.read_text(encoding="utf-8").splitlines() if r.strip()]
    for row in rows:
        fields = row.split(TAB)
        assert fields[6] == "", (
            "a row now names a requires: binary -- extend this test's "
            "coverage instead of just widening the assertion: " + row
        )
