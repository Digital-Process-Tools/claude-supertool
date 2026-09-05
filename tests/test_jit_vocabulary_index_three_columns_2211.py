"""The vocabulary index must carry the same column count claude-jit-context derives (#2211).

`rebuild-tsv.sh`'s `build_vocab_tsv` (claude-jit-context 0.7.1) writes three
tab-separated fields for a vocabulary row -- `keyword`, `file`, `verdict` --
where `verdict` is either the literal string `generic` or empty. It is
`pre-prompt-hook.sh`'s deferred generic-word classifier (#232/#255): a
keyword that appears on the project's generic-word list gets `generic` in
this column, and a match on a `generic`-only keyword downgrades an entry to
its summary rather than showing the full body (the "generic-only" case at
`pre-prompt-hook.sh:305`). This repository's committed
`.claude/jit-context/vocabulary/00-manual/00-index.tsv` carried two fields,
so running the real generator against this tree was not the no-op
`CLAUDE.md` promises: it silently widened every row by a trailing tab, the
same drift #1992 already fixed once for the tools index.

`verdict` is read as `vf[3]` after `split(vl, vf, "\t")` at
`pre-prompt-hook.sh:266`. A short field reads as an empty string there, so
the two-column tree never crashed the hook -- every entry just read as
non-generic, the same safe default an unclassified keyword gets. This test
pins the column count going forward so the next regeneration is a red here
instead of a silent widening found by hand.

`01-paths.tsv` in the same directory is a different builder
(`build_vocab_path_tsv`, module name -> file) and stays at two columns; this
test does not touch it.

Scoped to `00-manual` only: `vocabulary/01-oss/00-index.tsv` is
`/oss:scaffold`-owned and replaced wholesale on every scaffold run, so
pinning its shape here would fight that tool rather than this one (#2211).
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TSV = REPO / ".claude" / "jit-context" / "vocabulary" / "00-manual" / "00-index.tsv"
TAB = "\t"


def test_every_vocabulary_row_has_three_tab_separated_fields():
    rows = [r for r in TSV.read_text(encoding="utf-8").splitlines() if r.strip()]
    assert rows, "vocabulary index is empty; the shape changed"
    for row in rows:
        fields = row.split(TAB)
        assert len(fields) == 3, (
            "claude-jit-context 0.7.1's rebuild-tsv.sh writes 3 fields "
            "(keyword, file, verdict); got {0}: {1!r}".format(len(fields), row)
        )


def test_no_row_is_classified_generic_today():
    rows = [r for r in TSV.read_text(encoding="utf-8").splitlines() if r.strip()]
    for row in rows:
        fields = row.split(TAB)
        assert fields[2] == "", (
            "a row now carries a 'generic' verdict -- extend this test's "
            "coverage instead of just widening the assertion: " + row
        )
