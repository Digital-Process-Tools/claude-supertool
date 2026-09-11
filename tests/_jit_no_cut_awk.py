r"""One awk-matching harness for the `supertool-no-cut` regression suite (#2505).

`test_jit_no_cut_word_boundary_2257.py`, `test_jit_no_cut_span_stops_at_a_separator_1565.py`
and `test_jit_no_cut_path_name_1221.py` each carried their own copy of `needs_awk`,
`_pattern()` and `_awk_matches()` -- `needs_awk` and `_pattern()` byte-identical
in all three files, checked by hand before this module existed. `_awk_matches()`
was behaviourally identical (same subprocess call, same assertions) in all
three but carried a longer, file-specific docstring in the span-stops file --
folded into this module's own `awk_matches()` below rather than dropped, so
the correction it recorded survives the merge. That meant the defect #2505
reports (`needs_awk` skips the WHOLE file -- positive controls included -- on
any runner with no `awk` on PATH, and a green run reads identical whether
every regression ran or none of them did) had to be reasoned about, and
fixed, in three places rather than one. This module is the one copy; the
three files import from it instead of redefining it, and any future
`test_jit_no_cut_*` file gets the fix for free by importing from here rather
than pasting the block again.

Deliberately NOT a `conftest.py` fixture: these are ordinary module-level
helpers a handful of test files import by name (the same shape as this
directory's other `_*.py` shared modules, e.g. `_git_decline.py`), not
something pytest auto-injects, so a reader of one of the three files can see
exactly where `_pattern`/`_awk_matches` come from rather than needing to know
fixture-discovery rules.

This module answers only "how do we ask awk", never "should this CI leg have
awk" -- that policy question, and the loud-if-missing-on-CI check #2505 asks
for, lives in `test_jit_no_cut_awk_present_2505.py`, kept separate so a
reader auditing "is awk actually guaranteed here" does not have to wade
through the matching harness to find it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INDEX = REPO / ".claude" / "jit-context" / "tools" / "00-manual" / "00-index.tsv"
RULE = "supertool-no-cut.md"
TAB = chr(9)

AWK = shutil.which("awk")

needs_awk = pytest.mark.skipif(
    AWK is None,
    reason="awk absent: no verdict is available, which is not the same as a pass")


def pattern():
    """Column 2 of the live row naming `supertool-no-cut.md`, tilde stripped."""
    for raw in INDEX.read_text(encoding="utf-8").splitlines():
        fields = raw.split(TAB)
        if len(fields) >= 3 and fields[2] == RULE:
            assert fields[1].startswith("~"), "{0} is a literal row".format(RULE)
            return fields[1][1:]
    raise AssertionError("no row for {0} in {1}".format(RULE, INDEX))


def awk_matches(pat, subject):
    r"""What pre-tool-hook.sh does: match(tolower(full_command), pattern).

    Goes through a real subprocess `awk`, not Python `re` -- the two dialects
    disagree on some escapes (`\s` compiles clean under awk into the wrong
    thing rather than failing, see `docs/validators.md`), so a Python
    re-implementation of this check would stop being a test of what
    `pre-tool-hook.sh` actually enforces.

    The hook's real match is `jit_fold_latin1(tolower(full_command))` against
    `jit_fold_latin1(pattern)` (`pre-tool-hook.sh:311`). The fold is omitted
    here because every subject any `test_jit_no_cut_*` file uses is ASCII,
    where it is the identity -- stated rather than left for a reader to
    assume it is not there. `:137` is a comment in that script, not the
    match site; two other files under this directory
    (`test_jit_block_match_anchored_1415.py`,
    `test_jit_rule_retirement_1376.py`) still cite `:137` as if it were, and
    this note is the only place in the tree that flags that citation as
    inaccurate -- it lived only in `test_jit_no_cut_span_stops_at_a_separator_1565.py`
    before this module existed, and moved here rather than being dropped when
    that file's own copy of this function was replaced by an import (#2505).
    """
    env = dict(os.environ, JIT_PAT=pat, JIT_SUBJ=subject)
    proc = subprocess.run(
        ["awk", 'BEGIN { if (match(tolower(ENVIRON["JIT_SUBJ"]), '
                'ENVIRON["JIT_PAT"])) print "MATCH"; else print "NO" }'],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    assert proc.returncode == 0, "awk refused the pattern: {0}".format(proc.stderr)
    out = proc.stdout.strip()
    assert out in ("MATCH", "NO"), "awk said {0!r}".format(out)
    return out == "MATCH"


def assert_index_and_frontmatter_agree():
    """Same drift guard all three `test_jit_no_cut_*` files made independently
    for the same `supertool-no-cut.md` row: a fix applied to only one of
    `00-index.tsv` or the rule's own frontmatter is undone by the next
    `rebuild-tsv.sh` run, silently, because the row that comes back is
    well-formed."""
    body = (REPO / ".claude" / "jit-context" / "tools" / "00-manual"
            / RULE).read_text(encoding="utf-8")
    declared = [ln[len("match:"):].strip() for ln in body.splitlines()
                if ln.startswith("match:")]
    assert declared, "no `match:` line in {0}".format(RULE)
    assert declared[0] == "~" + pattern(), (
        "frontmatter and index row disagree; the next rebuild-tsv.sh run wins")
