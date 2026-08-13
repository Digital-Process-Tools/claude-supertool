"""The committed JIT indexes must be derivable from the rule frontmatter (#1579).

`.claude/jit-context/*/00-manual/00-index.tsv` is the file both hooks read. It
is built by `rebuild-tsv.sh`, which lives in the sibling **claude-jit-context**
repository -- not here -- so this repository cannot gate, delete or fix that
script from a PR. What it can do is keep its own tree in the shape the script
derives, so that running the script is a no-op instead of a silent rewrite.

Measured 2026-08-13 by running the real generator against a copy of the tree as
committed: four differences, none of which `validators/jit-index` can see,
because every regenerated row is well-formed. It is just not the same file.

  * `merged-is-not-ancestry.md` and `git-C-has-cwd.md` lost their `require:`
    column. Those columns were hand-typed into the TSV; `build_tool_tsv` reads
    `require:` from frontmatter (rebuild-tsv.sh:108) and neither body carries one.
  * `pyproject.toml` -> `version-sites.md` disappeared. One entry file yields
    exactly one paths row, and that mapping was a hand-added second row for the
    same file. **This is the one real loss of coverage**: the rule stops firing
    on pyproject.toml, which is one of the five version sites it exists to name.
  * `once, remind` became `once,remind` -- `jit_frontmatter` strips every space
    out of a `mode` value (common.sh:768).
  * Row order became glob order, in both dimensions.

This test is that derivation, in Python, over the committed frontmatter. It is a
reimplementation rather than a subprocess call to the script because the script
is in another repository: a test that skips when a neighbour is not cloned is a
test that never runs in CI, which is how this drifted in the first place.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = REPO / ".claude" / "jit-context"
TAB = "\t"


def _frontmatter(md, field):
    """`jit_frontmatter FIELD FILE` (common.sh:761), in Python.

    First `---`-delimited block only; the first line whose first bytes are
    `FIELD:`; `^FIELD: *` removed. `mode` loses every space. Every other value
    keeps its bytes, except a pair of double quotes wrapping the whole value.
    """
    depth = 0
    for line in md.read_text(encoding="utf-8").splitlines():
        if line == "---":
            depth += 1
            continue
        if depth != 1 or not line.startswith(field + ":"):
            continue
        value = line[len(field) + 1:].lstrip(" ")
        if field == "mode":
            return value.replace(" ", "")
        value = value.rstrip()
        if len(value) >= 2 and value[0] == value[-1] == '"' and '"' not in value[1:-1]:
            return value[1:-1]
        return value
    return ""


def _entries(dirpath):
    """`for md in "$dir"/*.md`, minus 00-README.md. Byte order, as the shell globs.

    Sorted on `p.name`, never on the `Path`: `Path.__lt__` compares a
    case-folded key on Windows, so `git-C-has-cwd.md` would sort against a
    different string there than the shell used to write the committed file.
    """
    entries = [p for p in dirpath.glob("*.md") if p.name != "00-README.md"]
    return sorted(entries, key=lambda p: p.name)


def _derive_tools(dirpath):
    """`build_tool_tsv` (rebuild-tsv.sh:87). Six columns; `remind` when mode is empty."""
    rows = []
    for md in _entries(dirpath):
        tool = _frontmatter(md, "tool")
        match = _frontmatter(md, "match")
        if not tool or not match:
            continue
        rows.append(TAB.join([
            tool, match, md.name,
            _frontmatter(md, "mode") or "remind",
            _frontmatter(md, "require"),
            _frontmatter(md, "forbid"),
        ]))
    return rows


def _derive_paths(dirpath):
    """`build_path_tsv` (rebuild-tsv.sh:255). Two columns, one row per entry file."""
    rows = []
    for md in _entries(dirpath):
        match = _frontmatter(md, "match")
        if match:
            rows.append(TAB.join([match, md.name]))
    return rows


def _committed(tsv):
    return tsv.read_text(encoding="utf-8").splitlines()


def test_tools_index_is_what_the_frontmatter_derives():
    tsv = BASE / "tools" / "00-manual" / "00-index.tsv"
    assert _committed(tsv) == _derive_tools(tsv.parent), (
        "the committed tools index is not what rebuild-tsv.sh would write from "
        "these bodies, so regenerating it rewrites the gate -- see the module "
        "docstring for the four shapes this has taken"
    )


def test_paths_index_is_what_the_frontmatter_derives():
    tsv = BASE / "paths" / "00-manual" / "00-index.tsv"
    assert _committed(tsv) == _derive_paths(tsv.parent), (
        "the committed paths index is not what rebuild-tsv.sh would write from "
        "these bodies; a row with no `match:` behind it is deleted by the next "
        "regeneration and that rule silently stops firing"
    )


def test_require_columns_survive_the_round_trip():
    """The round trip must be reached by teaching the frontmatter, never by
    deleting a column.

    A `require` on a `mode: block` rule cannot change the verdict -- the block
    is unconditional either way -- but it does decide the reason the caller is
    given, and on a `remind` rule it *is* the verdict. Pinned by name so that a
    future "just regenerate it" cannot satisfy the two tests above by dropping
    them.
    """
    tsv = BASE / "tools" / "00-manual" / "00-index.tsv"
    required = {}
    for row in _committed(tsv):
        fields = row.split(TAB)
        assert len(fields) == 6, "a tools row has six columns: " + row
        if fields[4]:
            required[fields[2]] = fields[4]
    assert required == {
        "merged-is-not-ancestry.md": "--merged",
        "git-C-has-cwd.md": "git -c",
    }


def test_the_paths_the_index_reaches_today_are_still_reached():
    """The `pyproject.toml` case as behaviour, not as bytes.

    Column 1 is handed to awk `match()` (pre-path-hook.sh:105), so one entry can
    claim several paths through an alternation -- which is how a hand-added
    second row is folded back into the field the generator reads. This pins the
    outcome: satisfying the round trip by deleting the row instead goes red here.
    """
    rows = [r.split(TAB) for r in _committed(BASE / "paths" / "00-manual" / "00-index.tsv")]
    for path, entry in [
        ("pyproject.toml", "version-sites.md"),
        (".claude-plugin/plugin.json", "version-sites.md"),
        ("tests/test_x.py", "tests-suite.md"),
        ("validators/common/refusal.py", "validators.md"),
        (".claude/jit-context/paths/00-manual/x.md", "jit-context.md"),
    ]:
        hits = [name for pattern, name in rows if re.search(pattern, path)]
        assert entry in hits, path + " reaches " + repr(hits) + ", not " + entry
