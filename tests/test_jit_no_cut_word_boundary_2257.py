r"""The no-cut span's tail keyword had no end boundary, so a substring false-fired (#2257).

`supertool-no-cut.md` ended with a literal alternation with no anchor on its
OWN right edge: `(head|tail|sed|cut|awk)`. `match()` only needs to find the
keyword starting somewhere; it never checked what came after it. So a quoted
op argument that merely CONTAINS "| header", "|cutoff", "|awkward" or "|sedan"
-- ordinary English or identifier text, not a shell pipe at all -- matched
the same span a real `| head` does, and refused a command that piped nothing.

## The issue's own reproduction did not reproduce

#2257 gave `supertool 'grep:_is_past|_summary_counts:presets/gitlab/job.py'`
as the failing call. Run through the same awk harness the sibling test files in
this directory use, it does not match, on the pattern from BEFORE this fix or
after: neither `_is_past` nor `_summary_counts` starts with `head`, `tail`,
`sed`, `cut` or `awk`, so the alternation's own left edge never even engages.
`test_the_cited_example_never_matched` below pins that as a fact, not a claim
this fix changes anything about that subject. What DID reproduce, found while
checking the claim rather than trusting it, is the class one level more
general: a bar inside a quoted argument immediately followed by a STRING that
merely STARTS with one of the five keywords, substring or whole word, with no
shell pipe in the command anywhere. Every `BOUNDARYLESS_SUBSTRINGS` subject
below was checked by hand against the row committed at this repo's
45c3a33b8045506bab04379ee976965d8f6063a7 and matched there -- the parent
commit's pattern, with no trailing anchor -- and none of them match the fixed
row this test reads live off disk.

## What was fixed, and what was not

`(head|tail|sed|cut|awk)` gained `([[:space:];&|><)]|$)` on its right. A
first draft anchored on `[[:space:]]` alone, found in self-review (before
commit) to be its own regression: a shell command word needs no space before
a separator or a redirection, so `'x:.'|head;rm -rf x`, `|head>out.txt`,
`|head&`, `|head)`, `|head&&true` and `|head||true` all stopped matching --
six real cuts the parent commit caught, silently unmatched by the
space-only anchor. `STILL_FIRES` below pins all six against the shipped
pattern so this cannot regress again unnoticed. That is a strictly cheaper
fix than matching quote nesting, and it costs nothing on the rest of the
`MUST_STILL_FIRE` side either -- every case in
`test_jit_no_cut_span_stops_at_a_separator_1565.py` and
`test_jit_block_match_anchored_1415.py` keeps matching, re-run against the
fixed pattern below.

The bracket mixes a class name with literal separator characters rather than
using a negated alpha class: the latter also compiles under Python's `re`,
but `hooks/shipped_rules.py`'s `translate()` only knows `[[:space:]]` as a
POSIX bracket class (see its `_POSIX_CLASSES`) and declines any pattern
containing an unrecognised one -- `supertool-no-cut.md` is the ONE rule in
`SHIPPED`, enforced in every repository that has not carried its own copy
(#1698), so a class `translate()` cannot read does not fail loudly here, it
silently drops this rule everywhere the plugin is installed but this repo.
Caught only because `tests/test_shipped_guard_rules_1698.py` reddened seven
ways when a negated-alpha class was tried first -- kept as the reason a
mixed literal-and-class bracket was chosen and not the other one, not merely
stated. `translate()` handles the mix fine: it substitutes the `[:space:]`
token wherever it occurs and only declines if an unrecognised `[:...:]` token
remains, so literal characters sharing the same brackets pass through
untouched.

**The deeper case in the issue's title is NOT fixed, and is not cheaply
fixable in this dialect.** A bar sitting entirely inside a quoted op argument,
immediately followed by whitespace-then-keyword (`'grep:foo |head some_arg'`,
a pattern with a literal space before a literal alternation branch spelled
"head") is indistinguishable from a real shell pipe to the awk `match()` used
here: telling "inside my own quotes" from "at command position, after the
closing quote" needs quote-depth tracking, which POSIX ERE has no state for
(no backreferences, no lookbehind). This is the same shape the rule's own body
already accepts for `;` and `&&` inside an argument (`'grep:a && b:.'` piped
to `tail` goes unseen, #1565's `KNOWN_RESIDUE`). Per that same file, "a missed
block is the safe direction here; a wrong one teaches routing around the
block" -- so this residue is accepted rather than chased further, and is
recorded here as the deliberate half of this fix rather than left implicit.

Would `test_a_boundaryless_substring_no_longer_fires` pass if the code did
nothing? No -- every subject there matched the pattern committed at
45c3a33b8045506bab04379ee976965d8f6063a7, which is what produced the false
refusal this issue reports.
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
TAB = chr(9)
BAR = chr(124)

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


# The word-boundary false positives: a quoted argument containing "|<word>"
# where <word> merely STARTS with one of the five guarded keywords, no shell
# pipe anywhere in the command. Every one of these matched the pattern
# committed at 45c3a33b8045506bab04379ee976965d8f6063a7 (verified by hand
# against that row before this fix was written) and none match the row this
# test reads live off disk.
BOUNDARYLESS_SUBSTRINGS = [
    ("header, not a real head", "supertool 'grep:foo " + BAR + " header:.'"),
    ("cutoff, not a real cut", "supertool 'grep:foo " + BAR + "cutoff:.'"),
    ("awkward, not a real awk", "supertool 'grep:foo " + BAR + "awkward:.'"),
    ("sedan, not a real sed", "supertool 'grep:foo " + BAR + "sedan:.'"),
    ("tailoff, not a real tail", "supertool 'grep:foo " + BAR + " tailoff:.'"),
]

# The issue's own reproduction -- kept as a standing correction, not a delta:
# it never matched, on either side of this fix.
CITED_EXAMPLE = ("supertool 'grep:_is_past" + BAR
                 + "_summary_counts:presets/gitlab/job.py'")

# A representative slice of the real cuts this rule exists for, re-run against
# the fixed pattern to show the boundary costs nothing on the positive side.
# The exhaustive version of this list lives in
# test_jit_no_cut_span_stops_at_a_separator_1565.py and
# test_jit_block_match_anchored_1415.py; this is not a second copy of either,
# only the shapes a trailing-boundary change could plausibly touch (the
# keyword's own right edge).
STILL_FIRES = [
    ("plain cut, space before the bar", "supertool 'grep:x:.:5' " + BAR + " head -80"),
    ("plain cut, no space before the bar", "supertool 'grep:x:.:5'" + BAR + "head -80"),
    ("cut is not the first pipe",
     "supertool 'gh-job:9:raw' " + BAR + " grep -i fail " + BAR + " awk '{print $1}'"),
    ("the keyword is the very last word, nothing after it",
     "supertool 'gh-job:9:raw'" + BAR + "tail"),
    ("sed with an -n flag", "supertool 'git-status'" + BAR + "sed -n '1,25p'"),
    ("cut with a -c flag", "python3 -m supertool 'read:a'" + BAR + "cut -c1-40"),
    # The regression a first draft of this fix introduced, caught in
    # self-review before commit: anchoring on [[:space:]] alone unblocks
    # every one of these, because a shell command word needs no space before
    # a separator or a redirection. All six matched the pattern committed at
    # 45c3a33b8045506bab04379ee976965d8f6063a7 (no anchor at all) AND the
    # first fix attempt ([[:space:]] only); none may stop matching here.
    ("no space before a semicolon", "supertool 'x:.'" + BAR + "head;echo done"),
    ("no space before a redirect", "supertool 'x:.'" + BAR + "head>out.txt"),
    ("no space before a background &", "supertool 'x:.'" + BAR + "head&"),
    ("no space before a closing paren", "supertool 'x:.'" + BAR + "head)"),
    ("no space before &&", "supertool 'x:.'" + BAR + "head&&true"),
    ("no space before ||", "supertool 'x:.'" + BAR + "head" + BAR + BAR + "true"),
]


@needs_awk
class TestTheKeywordNowNeedsAnEndBoundary:

    @pytest.mark.parametrize("label,command", BOUNDARYLESS_SUBSTRINGS,
                             ids=[c[0] for c in BOUNDARYLESS_SUBSTRINGS])
    def test_a_boundaryless_substring_no_longer_fires(self, label, command):
        assert not _awk_matches(_pattern(), command), label

    @pytest.mark.parametrize("label,command", STILL_FIRES,
                             ids=[c[0] for c in STILL_FIRES])
    def test_a_real_cut_still_fires(self, label, command):
        assert _awk_matches(_pattern(), command), label

    def test_the_cited_example_never_matched(self):
        """#2257's own reproduction, on the fixed pattern -- a correction of
        the issue's framing, not a claim this diff changes anything here."""
        assert not _awk_matches(_pattern(), CITED_EXAMPLE)


def test_the_index_row_and_the_frontmatter_agree():
    """A fix applied to only one of the two is undone by the next
    rebuild, silently, because the row that comes back is well-formed. The
    same check test_jit_no_cut_span_stops_at_a_separator_1565.py makes for
    the same row, kept here too so this file can be read on its own."""
    body = (REPO / ".claude" / "jit-context" / "tools" / "00-manual"
            / RULE).read_text(encoding="utf-8")
    declared = [ln[len("match:"):].strip() for ln in body.splitlines()
                if ln.startswith("match:")]
    assert declared, "no `match:` line in {0}".format(RULE)
    assert declared[0] == "~" + _pattern(), (
        "frontmatter and index row disagree; the next rebuild-tsv.sh run wins")
