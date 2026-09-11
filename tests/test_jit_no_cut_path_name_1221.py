r"""The repo-name-in-a-path false positive #1221 reports does not reproduce (#1221).

The issue's matcher-of-record is the old sketch quoted in its own body,
`supertool[^|]*\|[^|]*(head|tail|sed|cut|awk)`: a bare substring search for
"supertool" anywhere in the command, with no anchor at all. Against that
sketch, `cd ~/Documents/claude-supertool && git status -sb | head -5` matches,
because "supertool" is a substring of the directory name "claude-supertool"
and nothing requires it to be a command word.

That sketch is not what ships. `hooks/pre-tool-hook.sh`'s live pattern (the
`match:` line in `supertool-no-cut.md`, read off `00-index.tsv` the same way
every sibling test in this directory reads it) already requires "supertool"
to sit at **command position** -- immediately after the start of the command
or a `;`/`&`/`|` separator, with only an `rtk [proxy]` prefix, a `python3[.exe]`
launcher and a path-prefix-ending-in-`/` allowed in between. `cd` is none of
those, and there is no wildcard in the pattern that can skip over it, so a
`cd <anything>` can never satisfy the anchor regardless of what the target
path is named. That guarantee came from #1426 ("anchor on command position"),
landed the same day as this issue's first comment, and #1565 and #2257
narrowed the pattern further without reopening it.

## The issue's own reproductions do not reproduce

Checked directly against the live pattern with the same awk harness the three
sibling `test_jit_no_cut_*` files in this directory use (`match(tolower(...))`,
matching what `pre-tool-hook.sh` actually runs): every command quoted in
#1221 and its first two comments -- the `cd ... && git status -sb | head -5`
that opened the issue, the `git worktree add ... | tail -3` from the #1329
agent, and `pytest --no-header | grep` from the #1317 agent, both alone and
prefixed with the same `cd` into this repo's directory -- comes back `NO`.
`PATH_NAME_FALSE_POSITIVES` below pins all of them, both bare and with the
`cd` prefix restored, as a standing correction rather than a delta: none of
these would have failed if this file had never been written, because nothing
in this diff changes the shipped pattern.

The judgment call the issue raises -- whether a path-shaped `supertool`
should still match, because a worktree checkout's own launcher is
`python3 supertool.py` -- is also already answered correctly: `MUST_STILL_FIRE`
includes that exact shape, prefixed with a `cd` into a `claude-supertool`
directory, and it still matches. Detection inside a worktree is not
disarmed by fixing the path-name false positive, because the two are
resolved by the same anchor for different reasons: `cd` never reaches the
anchor at all, while `python3 supertool.py` legitimately IS the command word.

## What #1221 raised that is deliberately not touched here

The issue's third comment (2026-08-14) records a **third**, distinct false
positive: `paste ... | tail -12`, a write op's own receipt being cut, which
the comment itself argues is a different bug -- the rule's stated rationale
is entirely about a *read*'s verdict-then-body layout being cut, and nothing
in that reasoning says why cutting a write's receipt is dangerous. Checked
here too (`WRITE_OP_STILL_BLOCKED`) to confirm it is real and unrelated to
the path-anchor fix: it still matches on the current pattern, exactly as
comment 3 describes, and nothing in this diff addresses it. Per the comment's
own framing -- "a PR should say which of the two it closes" -- this one
reports for filing as its own issue rather than folding it in silently.

Would `test_a_path_name_false_positive_does_not_fire` fail if the code did
something wrong? Yes, confirmed by running four of the seven
`PATH_NAME_FALSE_POSITIVES` rows through the pattern this repo actually
carried one commit before #1426 (`git show eded2baa^:.../supertool-no-cut.md`
-- the literal sketch quoted in the issue body,
`supertool[^|]*\|[^|]*(head|tail|sed|cut|awk)`): they come back `MATCH`
there and `NO` here. That is the red-then-green #1426 already produced;
this file pins the green side so it cannot regress unnoticed, rather than
re-deriving the fix.
"""
from __future__ import annotations

import pytest

from _jit_no_cut_awk import awk_matches as _awk_matches
from _jit_no_cut_awk import needs_awk
from _jit_no_cut_awk import pattern as _pattern


# The exact commands #1221 and its first two comments record as wrongly
# blocked -- no supertool op anywhere in the text, only the substring
# "supertool" inside the repo's own directory name "claude-supertool".
PATH_NAME_FALSE_POSITIVES = [
    ("the issue's own opener",
     "cd ~/Documents/claude-supertool && git status -sb | head -5"),
    ("the opener with its full && chain",
     "cd ~/Documents/claude-supertool && git fetch --all --tags -q "
     "&& git status -sb | head -5"),
    ("#1329's worktree add, bare",
     "git worktree add /Users/x/Documents/st-wt/1329 -b fix/1329 "
     "origin/master 2>&1 | tail -3"),
    ("#1329's worktree add, cd-prefixed",
     "cd ~/Documents/claude-supertool && git worktree add /x -b y "
     "2>&1 | tail -3"),
    ("#1317's pytest --no-header, bare",
     "pytest --no-header | grep foo"),
    ("#1317's pytest --no-header, cd-prefixed",
     "cd ~/Documents/claude-supertool && pytest --no-header | grep foo"),
    ("#1317's git log, cd-prefixed",
     "cd ~/Documents/claude-supertool && git log --oneline -5 | tail -3"),
]

# The positive control: a real op invocation must still be caught, including
# the judgment call the issue calls out by name -- a worktree's own
# `python3 supertool.py` launcher, reached exactly the way #1329's agent
# would have reached it, with the same cd prefix into a path that CONTAINS
# but is not "supertool".
MUST_STILL_FIRE = [
    ("a bare op invocation",
     "supertool 'gh-issue:1221:full' | tail -3"),
    ("the worktree launcher shape, unprefixed",
     "python3 supertool.py 'op' | tail"),
    ("the worktree launcher shape, cd-prefixed into a claude-supertool path",
     "cd ~/Documents/claude-supertool && python3 supertool.py 'op' | tail"),
]

# Comment 3 (2026-08-14): a write op's own receipt, cut. Confirmed real and
# left unfixed here -- the comment argues it is a different bug from the
# path-name anchor this file is about, and this diff does not touch it.
WRITE_OP_STILL_BLOCKED = (
    "python3 supertool.py 'paste:@-' | tail -12")


@needs_awk
class TestPathNameNoLongerFalsePositives:

    @pytest.mark.parametrize("label,command", PATH_NAME_FALSE_POSITIVES,
                             ids=[c[0] for c in PATH_NAME_FALSE_POSITIVES])
    def test_a_path_name_false_positive_does_not_fire(self, label, command):
        assert not _awk_matches(_pattern(), command), label

    @pytest.mark.parametrize("label,command", MUST_STILL_FIRE,
                             ids=[c[0] for c in MUST_STILL_FIRE])
    def test_a_real_op_invocation_still_fires(self, label, command):
        assert _awk_matches(_pattern(), command), label

    def test_the_write_op_residue_is_confirmed_and_left_alone(self):
        """Comment 3's finding, reproduced -- reported for filing, not fixed
        here. If this ever starts returning False, comment 3's own bug is
        gone and this assertion (not the fix) should be revisited."""
        assert _awk_matches(_pattern(), WRITE_OP_STILL_BLOCKED)


def test_the_index_row_and_the_frontmatter_agree():
    """Same drift guard the two sibling files make for the same row, kept
    here too so this file's own claims about the live pattern do not silently
    diverge from the frontmatter the next rebuild-tsv.sh run would restore."""
    from _jit_no_cut_awk import assert_index_and_frontmatter_agree
    assert_index_and_frontmatter_agree()
