"""A `paths/` rule's `mode:` is never read, so writing one states a lie (#1442).

#1442 asked which of two `paths/` rules should gain `once`, on the premise that
the two lacking it "re-inject on every matching call" while the nine carrying it
cost their body once per session. **The premise does not survive contact**, and
the whole point of this file is to stop it being re-asked:

* `rebuild-tsv.sh:build_path_tsv` reads exactly one frontmatter field for a
  paths rule -- `match:` -- and writes `pattern<TAB>file`. There is no mode
  column for a paths row, and `validators/jit-index/jit-index.py` refuses a
  paths row that has anything other than two fields.
* `pre-path-hook.sh` dedups **unconditionally**: `if (rule_file in shown)
  continue` before the pattern is even tried, and every delivered rule is
  marked. Every paths rule is once-per-session; none can be per-match; and
  `mode:` in a paths body reaches no reader at all.

The measurement that settles it, from this repo's own `hooks.log` on
2026-08-12: `jit-context.md` (`mode: remind`, no `once`) fired at 23:04:18,
23:04:27, 23:04:42, 23:05:16 and 23:07:03 -- the five firings #1442 was filed
on. `docs-index.md` (`mode: once, remind`) fired at three of those same five
timestamps. Opposite frontmatter, identical behaviour, because the repeats are
one-per-session across parallel agents and `once` was never the variable.

So the answer to #1442 is neither reading it offers: the field is inert and the
two spellings sitting side by side are what produced the question. They are
removed, and this refuses the next one -- a stale line claiming a capability the
tool does not have is this repo's most expensive shape, because it suppresses
the check that would disprove it.

Would this pass if the code did nothing? No: eleven paths rules carried a
`mode:` line at the parent commit.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
JIT = REPO / ".claude" / "jit-context"
PATHS = JIT / "paths" / "00-manual"
TOOLS = JIT / "tools" / "00-manual"
TAB = "\t"


def _rule_files(directory):
    return sorted(p for p in directory.glob("*.md") if p.name != "00-README.md")


def _frontmatter_lines(path):
    """Lines of the first `---` block, or [] when the file has no frontmatter."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    out = []
    for line in lines[1:]:
        if line.strip() == "---":
            return out
        out.append(line)
    return []


def test_paths_rules_are_found_at_all():
    """Vacuity guard: an empty glob would absolve every assertion below."""
    files = _rule_files(PATHS)
    assert len(files) >= 8, "found {0} paths rules".format(len(files))
    for path in files:
        assert _frontmatter_lines(path), "{0} has no frontmatter".format(path.name)


def test_no_paths_rule_declares_a_mode():
    """The delta. Eleven carried one and none was read."""
    offenders = [p.name for p in _rule_files(PATHS)
                 if any(ln.startswith("mode:") for ln in _frontmatter_lines(p))]
    assert not offenders, (
        "paths rules declaring a `mode:`, which no reader consumes -- "
        "rebuild-tsv.sh writes `pattern<TAB>file` for paths and pre-path-hook.sh "
        "dedups every rule per session regardless: {0}".format(", ".join(offenders)))


def test_the_paths_index_has_no_column_a_mode_could_live_in():
    """Why the field is inert, asserted rather than asserted-about.

    Not a restatement of the test above: that one is about what is written,
    this one is about what could ever be read. If the index shape gains a
    third column, this reddens and the rule above is the one to revisit.
    """
    rows = [r for r in (PATHS / "00-index.tsv").read_text(
        encoding="utf-8").splitlines() if r.strip()]
    assert rows, "paths index is empty; the shape changed"
    for row in rows:
        assert len(row.split(TAB)) == 2, (
            "paths row is `pattern<TAB>file` (2 fields), got {0}: {1!r}".format(
                len(row.split(TAB)), row))


def test_the_tools_index_still_does_carry_a_mode_column():
    """The asymmetry is the point, and a test that only ever looked at `paths/`
    would read as 'jit rules have no modes'. A tools row is 6 fields with the
    mode in column 4, and `block` genuinely lives there."""
    rows = [r for r in (TOOLS / "00-index.tsv").read_text(
        encoding="utf-8").splitlines() if r.strip()]
    assert rows, "tools index is empty; the shape changed"
    modes = set()
    for row in rows:
        fields = row.split(TAB)
        assert len(fields) == 6, (
            "tools row is 6 fields, got {0}: {1!r}".format(len(fields), row))
        modes.add(fields[3])
    assert any("block" in m for m in modes), (
        "no tools row declares `block`; column 4 stopped being the mode")
