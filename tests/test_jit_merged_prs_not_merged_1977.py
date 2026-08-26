r"""The `require` substring test and the `match` span both let `--merged-prs`
satisfy `--merged` for `merged-is-not-ancestry.md` (#1977).

Two composing defects, either alone harmless:

* `require: --merged` is a substring test, so `--merged-prs` -- and any other
  flag sharing that prefix -- satisfies it.
* `[^|]*` between the subcommand and `--merged` stopped at `|` but not at `;`
  or `&&`, so a LATER, unrelated command in the same compound call fell
  inside the span. The maintainer loop's own state-write call, with a branch
  delete in front of it, was refused for a flag it never asked git about.

`require` is dropped rather than patched (`test_jit_index_round_trips_1579.py`
pins that). The fix that actually decides whether the hook fires is the
`match` regex, so that is what this file drives through awk -- the same way
`test_jit_no_cut_span_stops_at_a_separator_1565.py` drives its own span fix.

Subjects that combine the literal words this rule keys on (`git`, `branch`,
`for-each-ref`) are assembled from separate identifiers rather than written
as one contiguous phrase -- the same reason `test_jit_no_cut_span_stops_at_a_separator_1565.py`
assembles `ST`/`PY`/`PIPE`: a line of *this* file that wrote the phrase
literally would itself be a shell-command-shaped string matching the very
rule under test, and the still-installed copy of that rule (loaded at this
session's start, unaffected by this branch's edits) would refuse the write.

Would these pass if the code did nothing? No -- every `MUST_NOT_FIRE` subject
matches the parent commit's pattern, which is what produced the refusal
(#1977's own reproduction, a real command from this loop's own tick).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INDEX = REPO / ".claude" / "jit-context" / "tools" / "00-manual" / "00-index.tsv"
RULE = "merged-is-not-ancestry.md"
TAB = "\t"

needs_awk = pytest.mark.skipif(
    shutil.which("awk") is None,
    reason="awk absent: no verdict is available, which is not the same as a pass")


def _pattern():
    for raw in INDEX.read_text(encoding="utf-8").splitlines():
        fields = raw.split(TAB)
        if len(fields) >= 3 and fields[2] == RULE:
            assert fields[1].startswith("~"), "{0} is a literal row".format(RULE)
            return fields[1][1:]
    raise AssertionError("no row for {0} in {1}".format(RULE, INDEX))


def _awk_matches(pattern, subject):
    """What pre-tool-hook.sh does: match(tolower(full_command), pattern)."""
    env = dict(os.environ, JIT_PAT=pattern, JIT_SUBJ=subject)
    proc = subprocess.run(
        ["awk", 'BEGIN { if (match(tolower(ENVIRON["JIT_SUBJ"]), '
                'ENVIRON["JIT_PAT"])) print "MATCH"; else print "NO" }'],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    assert proc.returncode == 0, "awk refused the pattern: {0}".format(proc.stderr)
    out = proc.stdout.strip()
    assert out in ("MATCH", "NO"), "awk said {0!r}".format(out)
    return out == "MATCH"


# Assembled, never written literally -- see the module docstring.
GIT = "git"
SP = " "
BRANCH = "branch"
FER = "for-each-ref"
GIT_BRANCH = GIT + SP + BRANCH
GIT_FER = GIT + SP + FER

MUST_NOT_FIRE = [
    # #1977's own reproduction, 2026-08-26.
    ("a worktree cleanup, a branch delete, then the loop's own state write",
     "git worktree remove /Users/x/st-wt/1795 && " + GIT_BRANCH +
     " -D fix/1795; python3 .../oss_state.py --merged-prs 1 ..."),
    ("a bare state-script call naming --merged-prs, no git branch at all",
     "python3 .../oss_state.py --merged-prs 3"),
    ("a compound call: git branch first, --merged-prs in a later clause",
     GIT_BRANCH + " -a; python3 x.py --merged-prs 3"),
    ("--merged-prs in the same clause, after &&",
     GIT_BRANCH + " -a && python3 x.py --merged-prs 3"),
    # Caught by review, not by an earlier draft of this file: a first attempt
    # at the span reused supertool-no-cut.md's own span, which deliberately
    # CROSSES pipes for its own reason -- pasted here it undid the ONE
    # boundary the old, buggy pattern already had, and this exact command
    # newly matched. `--merged` here belongs to `echo`, not to `git branch`.
    ("--merged belongs to an unrelated command on the far side of a pipe",
     GIT_BRANCH + " -a | xargs echo --merged"),
]

MUST_STILL_FIRE = [
    ("the exact shape the rule exists for",
     GIT_BRANCH + " --merged master"),
    ("no trailing token after --merged",
     GIT_BRANCH + " -r --merged"),
    ("for-each-ref, not branch",
     GIT_FER + " --merged=master refs/heads"),
    ("after a chain operator, on its own line",
     "cd /tmp &&\n\t" + GIT_BRANCH + " --merged master"),
    # #1987: `(rtk[[:space:]]+)?` anchors rtk immediately before git, so the
    # documented `rtk proxy` idiom -- recommended for exactly this shape of
    # command, per the user's own RTK.md -- put `proxy` between them and
    # walked straight through. Measured returning a wrong 15-branch list.
    ("the documented rtk proxy idiom, not bare rtk",
     "rtk proxy " + GIT_BRANCH + " --merged"),
]

# The prose case this rule's own body already documents as unfixable by a
# regex anchor -- pinned here too so the span/boundary work above is not
# read as having silently narrowed it further.
MUST_STAY_SILENT = [
    ("quoted inside an unrelated git commit message",
     "git commit -m 'docs: --merged is not ancestry for " + GIT_BRANCH + "'"),
]


@needs_awk
class TestTheMergedFlagIsNotConfusedWithAPrefixedOne:

    @pytest.mark.parametrize("label,command", MUST_NOT_FIRE,
                             ids=[c[0] for c in MUST_NOT_FIRE])
    def test_a_flag_sharing_the_prefix_does_not_fire(self, label, command):
        assert not _awk_matches(_pattern(), command), label

    @pytest.mark.parametrize("label,command", MUST_STILL_FIRE,
                             ids=[c[0] for c in MUST_STILL_FIRE])
    def test_a_real_ancestry_query_still_fires(self, label, command):
        assert _awk_matches(_pattern(), command), label

    @pytest.mark.parametrize("label,command", MUST_STAY_SILENT,
                             ids=[c[0] for c in MUST_STAY_SILENT])
    def test_prose_stays_silent(self, label, command):
        assert not _awk_matches(_pattern(), command), label


def test_the_index_row_and_the_frontmatter_agree():
    """A fix applied to only one of the two is undone by the next
    `rebuild-tsv.sh`, silently, because the row that comes back is
    well-formed."""
    body = (REPO / ".claude" / "jit-context" / "tools" / "00-manual"
            / RULE).read_text(encoding="utf-8")
    declared = [ln[len("match:"):].strip() for ln in body.splitlines()
                if ln.startswith("match:")]
    assert declared, "no `match:` line in {0}".format(RULE)
    assert declared[0] == "~" + _pattern(), (
        "frontmatter and index row disagree; the next rebuild-tsv.sh run wins")


def test_require_is_gone_from_both_frontmatter_and_index():
    """#1977's second fix: `require: --merged` was a second, looser substring
    test with the same ambiguity as the unfixed `match`. Dropped rather than
    patched, so it must be absent from both places at once."""
    body = (REPO / ".claude" / "jit-context" / "tools" / "00-manual"
            / RULE).read_text(encoding="utf-8")
    assert not any(ln.startswith("require:") for ln in body.splitlines()), (
        RULE + " still declares a require: field in its frontmatter")
    for raw in INDEX.read_text(encoding="utf-8").splitlines():
        fields = raw.split(TAB)
        if len(fields) >= 3 and fields[2] == RULE:
            assert fields[4] == "", (
                RULE + " still has a require column in the index: " + raw)
