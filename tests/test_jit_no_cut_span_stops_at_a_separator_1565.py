r"""The no-cut span crossed a command separator, so a NEXT command's pipe blocked the op (#1565).

`supertool-no-cut.md` puts a wildcard between the op string and the bar. It
excluded only a newline, so it walked straight through `;` and `&&` — and a
compound command whose `tail -2` belonged to the `pytest` *after* the `&&` was
refused, with a refusal naming a cut that was not happening. Observed twice in
one run on 2026-08-13; three wrong blocks that day, on top of the four in one
evening #1433 records.

This is not one of the two residues the rule's own body admits (#1430). Those
are about a separator that is **absent** — a heredoc body line beginning with
the command, an env-var prefix. Here the separator is present and the matcher
did not stop at it, which is the case the anchor work in #1415/#1426/#1430
was meant to settle.

**Two characters #1565 proposed excluding are deliberately kept in the span,
and that is the whole judgment of this file:**

* `|` — a genuine cut of an op's own output routinely has one, because the cut
  need not be the first stage. `gh-job:9:raw` piped to `grep` and then to `awk`
  is a real cut and is already pinned in
  `tests/test_jit_block_match_anchored_1415.py`. Excluding `|` would have
  deleted that case.
* a bare `&` — `2>&1` before the bar is the commonest spelling of a real cut in
  this repo's own transcripts. Only `&` *followed by another* `&` or by
  whitespace is a separator, so the span reads `([^;&\n]|&[^&[:space:]])*`
  rather than a flat negated class.

Narrowing a `block` is only safe against the cases it must keep firing on, so
every `MUST_STILL_FIRE` row below is a regression guard, not a delta. The
`MUST_NOT_FIRE` rows are the delta: all four match the pre-#1565 pattern.

Would these pass if the code did nothing? No — the four `MUST_NOT_FIRE`
subjects match the parent commit's pattern, which is what produced the
refusals.

The subject goes through awk, not `re`: awk is what compiles this and the two
disagree. The pattern arrives via ENVIRON so its escapes are exactly what
`getline` from the TSV delivers.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INDEX = REPO / ".claude" / "jit-context" / "tools" / "00-manual" / "00-index.tsv"
RULE = "supertool-no-cut.md"
TAB = "\t"

needs_awk = pytest.mark.skipif(
    shutil.which("awk") is None,
    reason="awk absent: no verdict is available, which is not the same as a pass")


def _pattern():
    """Column 2 of the live row naming the rule, tilde stripped."""
    for raw in INDEX.read_text(encoding="utf-8").splitlines():
        fields = raw.split(TAB)
        if len(fields) >= 3 and fields[2] == RULE:
            assert fields[1].startswith("~"), "{0} is a literal row".format(RULE)
            return fields[1][1:]
    raise AssertionError("no row for {0} in {1}".format(RULE, INDEX))


def _awk_matches(pattern, subject):
    """What pre-tool-hook.sh:311 does, less one step that cannot bite here.

    The hook matches `jit_fold_latin1(tolower(full_command))` (built at :184)
    against `jit_fold_latin1(pattern)`. The fold is omitted because every
    subject in this file is ASCII, where it is the identity -- but the omission
    is stated rather than left for a reader to assume it is not there. (The
    `:137` cited by the three sibling test files is a comment, not the match.)
    """
    env = dict(os.environ, JIT_PAT=pattern, JIT_SUBJ=subject)
    proc = subprocess.run(
        ["awk", 'BEGIN { if (match(tolower(ENVIRON["JIT_SUBJ"]), '
                'ENVIRON["JIT_PAT"])) print "MATCH"; else print "NO" }'],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    assert proc.returncode == 0, "awk refused the pattern: {0}".format(proc.stderr)
    out = proc.stdout.strip()
    assert out in ("MATCH", "NO"), "awk said {0!r}".format(out)
    return out == "MATCH"


# Assembled, never written literally: a line of this file that *began* with a
# piped invocation would match the rule it is about, and refuse the edit that
# writes it. The 1415 file uses the same constants for the same reason.
PIPE = " | "
BAR = "|"
ST = "supertool "
PY = "python3 supertool.py "

MUST_NOT_FIRE = [
    # The shape observed on 2026-08-13, twice in one run.
    ("the cut belongs to the pytest after the &&",
     "cd /tmp/w && " + PY + "'gh-issue:1565' && pytest -q tests/x.py" + PIPE + "tail -2"),
    ("the cut belongs to the command after the ;",
     ST + "'git-status'; git log --oneline" + PIPE + "head -5"),
    ("the cut belongs to the command after the &&, no pytest",
     "cd /tmp && " + ST + "'read:a' && ls" + PIPE + "head"),
    # `&` alone is a separator only when it is not part of `2>&1`. Kept
    # separate from the `&&` rows because it is the character the span still
    # admits, so it discriminates `&[^&[:space:]]` from a plain `&[^&]`.
    ("the op is backgrounded and the next command pipes",
     ST + "'read:a' & pytest -q" + PIPE + "tail -2"),
]

MUST_STILL_FIRE = [
    ("plain cut", ST + "'grep:x:.:5'" + PIPE + "head -80"),
    ("no space before the bar", ST + "'grep:x:.:5'" + BAR + "head -80"),
    # The `|` case. Excluding the bar from the span, as #1565 proposed, deletes
    # exactly this row -- the cut is real and is the third stage.
    ("the cut is not the first pipe",
     ST + "'gh-job:9:raw'" + PIPE + "grep -i fail" + PIPE + "awk '{print $1}'"),
    # The `&` case. `2>&1` sits between the op string and the bar.
    ("a 2>&1 redirect stands between the op and the bar",
     PY + "'grep:x:.:5' 2>&1" + PIPE + "tail -30"),
    # The op is still the last command before the bar, so the chain operator is
    # BEFORE the invocation and never enters the span.
    ("after a chain operator, the op is the last command",
     "cd /tmp && " + ST + "'git-status'" + PIPE + "sed -n '1,25p'"),
    # Re-anchoring: the separator is inside the subject but the SECOND
    # invocation starts a fresh match at it. This is why excluding `;` and `&&`
    # loses no genuine cut -- a cut of the op's output puts the op last.
    ("the second op in a chain is the one being cut",
     ST + "'read:a' && " + ST + "'read:b'" + PIPE + "head -5"),
    ("the invocation is on the second line of the command",
     "cd /tmp &&\n" + ST + "'git-status'" + PIPE + "head -5"),
    ("a tab-indented continuation line",
     "cd /tmp &&\n\t" + ST + "'git-status'" + PIPE + "head -5"),
    ("run as a module", "python3 -m supertool 'read:a'" + PIPE + "cut -c1-40"),
]


# The price of the narrowing, recorded so it is a known residue rather than a
# surprise. A separator INSIDE the op's own quoted argument ends the span, so
# these genuine cuts stop being blocked -- they matched at the parent commit.
#
# This is not a new class: a bar inside the argument was already unmatched
# (`gh-job:9:grep:head|tail`, pinned in the 1415 file), because only a
# tokeniser can tell an argument from a command and the tokeniser belongs to
# claude-jit-context. The narrowing adds `;` and `&&` to the characters that
# behave that way, symmetrically. A missed block is the safe direction on this
# gate; a wrong block teaches people to route around it.
KNOWN_RESIDUE = [
    ("a semicolon inside the op's own argument",
     ST + "'grep:echo a; echo b:.'" + PIPE + "tail -5"),
    ("a chain operator inside the op's own argument",
     ST + "'grep:a && b:.'" + PIPE + "tail -5"),
]


@needs_awk
class TestTheSpanStopsAtACommandSeparator:

    @pytest.mark.parametrize("label,command", MUST_NOT_FIRE,
                             ids=[c[0] for c in MUST_NOT_FIRE])
    def test_a_separator_ends_the_span(self, label, command):
        assert not _awk_matches(_pattern(), command), label

    @pytest.mark.parametrize("label,command", MUST_STILL_FIRE,
                             ids=[c[0] for c in MUST_STILL_FIRE])
    def test_a_real_cut_is_still_matched(self, label, command):
        assert _awk_matches(_pattern(), command), label

    @pytest.mark.parametrize("label,command", KNOWN_RESIDUE,
                             ids=[c[0] for c in KNOWN_RESIDUE])
    def test_a_separator_inside_the_argument_is_a_disclosed_miss(
            self, label, command):
        """Asserts the loss, so narrowing it further reddens deliberately."""
        assert not _awk_matches(_pattern(), command), label


def test_the_index_row_and_the_frontmatter_agree():
    """A fix applied to only one of the two is undone by the next
    `rebuild-tsv.sh`, silently, because the row that comes back is well-formed.

    Measured 2026-08-13 rather than assumed: a rebuild run against this tree
    reproduces this row's first four columns exactly. It does NOT reproduce the
    whole index -- two rows lose a `require` column that no frontmatter
    declares, and `version-sites.md` loses its second paths row -- so the
    committed index is partly hand-maintained. That is filed separately; it
    does not weaken this assertion, which is about one row's `match`."""
    body = (REPO / ".claude" / "jit-context" / "tools" / "00-manual"
            / RULE).read_text(encoding="utf-8")
    declared = [ln[len("match:"):].strip() for ln in body.splitlines()
                if ln.startswith("match:")]
    assert declared, "no `match:` line in {0}".format(RULE)
    assert declared[0] == "~" + _pattern(), (
        "frontmatter and index row disagree; the next rebuild-tsv.sh run wins")
