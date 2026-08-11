r"""A blocking jit rule that matches a substring anywhere fires on prose (#1415).

`00-index.tsv` column 2 is handed to awk's `match(tolower(full_command), pat)`
against the WHOLE command text (`claude-jit-context/scripts/pre-tool-hook.sh:137`),
with no notion of where a shell would find a command word. Two rules carried an
unanchored pattern and produced **seven false refusals on 2026-08-11** — this
repository's own directory name appearing in a path (x3), a shell variable, and a
quoted heredoc body twice, once while filing the issue about it. Writing this
file was refused too, which is the eighth.

The repo already owned the fix one file over: `gh-pr-view-merge-have-ops.md` opens
`(^|[;&|\n] *)`, which pins the match to command position.

**This does not make the patterns precise**, and the residue is named here rather
than discovered later: a heredoc body line that *begins* a line with a
command-shaped string still matches, because `^`-alternation cannot tell a heredoc
from a command. Only a tokeniser can, and the tokeniser is `claude-jit-context`'s,
a separate repository.

Would these pass if the code did nothing? No — every `must not match` case below
matches the pre-#1415 patterns, and that is what produced the refusals.

The subject is exercised through awk itself rather than through `re`, because awk
is what compiles these and the two disagree (`\d`, `\b`, leftmost-longest). The
pattern reaches it via ENVIRON, not `-v`, so its escapes arrive intact exactly as
`getline` from the TSV delivers them.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INDEX = REPO / ".claude" / "jit-context" / "tools" / "00-manual" / "00-index.tsv"

needs_awk = pytest.mark.skipif(
    shutil.which("awk") is None,
    reason="awk absent: no verdict is available, which is not the same as a pass")

# NOT `str(REPO)`: pytest runs from a worktree at st-wt/NNN, whose path does not
# contain the word this rule keys on, so every directory-name case would have
# passed vacuously. The clone the maintainer actually stands in is this one.
HERE = "~/Documents/claude-supertool"
TAB = "\t"


def _pattern_for(rule_file):
    """Column 2 of the live row naming RULE_FILE, tilde stripped."""
    for raw in INDEX.read_text(encoding="utf-8").splitlines():
        fields = raw.split(TAB)
        if len(fields) >= 3 and fields[2] == rule_file:
            assert fields[1].startswith("~"), (
                "{0} is a literal row, not a regex row".format(rule_file))
            return fields[1][1:]
    raise AssertionError("no row for {0} in {1}".format(rule_file, INDEX))


def _awk_matches(pattern, subject):
    """Exactly what pre-tool-hook.sh:137 does: match(tolower(cmd), pat)."""
    env = dict(os.environ, JIT_PAT=pattern, JIT_SUBJ=subject)
    proc = subprocess.run(
        # `print x > y` is a REDIRECTION to awk, so the comparison lives in an
        # if, not in the print. Written the other way it is a syntax error and
        # every case reddens identically -- which is not a verdict about a pattern.
        ["awk", 'BEGIN { if (match(tolower(ENVIRON["JIT_SUBJ"]), '
                'ENVIRON["JIT_PAT"])) print "MATCH"; else print "NO" }'],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    assert proc.returncode == 0, "awk refused the pattern: {0}".format(proc.stderr)
    out = proc.stdout.strip()
    assert out in ("MATCH", "NO"), "awk said {0!r}".format(out)
    return out == "MATCH"


PIPE = " | "
BAR = "|"
APOS = "'"
GIL = "gh issue list"
GPL = "gh pr list"

NO_CUT_FIRES = [
    ("plain op piped to head", "supertool 'grep:x:.:5'" + PIPE + "head -80"),
    ("branch worktree form",
     "python3 supertool.py 'gh-issue:1415:full'" + PIPE + "tail -20"),
    ("after a chain operator",
     "cd " + HERE + " && supertool 'git-status'" + PIPE + "sed -n '1,25p'"),
    ("relative path form", "./supertool 'read:a'" + PIPE + "cut -c1-40"),
    ("the cut is not the first pipe",
     "supertool 'gh-job:9:raw'" + PIPE + "grep -i fail" + PIPE + "awk '{print $1}'"),
    # Each of these discriminates the anchor actually written from a cruder one
    # that would pass every case above. Without the newline alternative the
    # first is missed; with " *" instead of "[[:space:]]*" the second is; and
    # with a one-level "./" instead of a path segment the last two are.
    ("the invocation is on the second line of the command",
     "cd /tmp &&\nsupertool 'git-status'" + PIPE + "head -5"),
    ("a tab-indented continuation line",
     "cd /tmp &&\n\tsupertool 'git-status'" + PIPE + "head -5"),
    ("run through a multi-segment relative path",
     "../../supertool 'read:a'" + PIPE + "cut -c1-40"),
    ("run through an absolute path",
     "/usr/local/bin/supertool 'read:a'" + PIPE + "cut -c1-40"),
]

NO_CUT_SILENT = [
    # The shapes that fired on 2026-08-11.
    ("this repo's directory name in a path",
     "cd " + HERE + " && pytest -q tests/test_ops.py" + PIPE + "tail -20"),
    ("directory name plus an unrelated worktree pipe",
     "cd " + HERE + " && git worktree add ../st-wt/1415 -b fix/1415 master"
     + PIPE + "tail -5"),
    ("a bar inside the op's own argument",
     "supertool 'gh-job:9:grep:head" + BAR + "tail'"),
    ("a heredoc body quoting a piped example",
     "python3 supertool.py 'gh-issue-create:@-' <<'EOF'\n"
     "See supertool 'grep:x'" + PIPE + "head -80 for the shape.\nEOF"),
    # A different binary that merely STARTS with the guarded name is the same
    # mere-mention defect one word to the right, so the name must end at a space.
    ("a different binary sharing the name's prefix",
     "supertoolkit 'x'" + PIPE + "head -5"),
    ("a sibling script sharing the name's prefix",
     "./supertool-benchmark.sh 'x'" + PIPE + "tail -5"),
]

LIST_LIMIT_FIRES = [
    ("bare invocation", GIL + " --state open"),
    ("pr form after a chain operator", "cd /tmp && " + GPL + " --state open"),
    ("rtk-wrapped", "rtk " + GIL + " --milestone v0.35.0"),
    ("a tab-indented continuation line", "cd /tmp &&\n\t" + GPL + " --state open"),
]

LIST_LIMIT_SILENT = [
    # The shape that refused the #1395 PR body and opened this issue.
    ("prose naming the command inside a quoted heredoc",
     "gh-pr-create:@- <<'EOF'\nThe guard measured a raw " + GIL
     + " call at 31 against a true 72.\nEOF"),
    ("named mid-sentence in a commit message",
     "git commit -m 'docs: say why " + GIL + " is capped'"),
]


@needs_awk
class TestSupertoolNoCut:

    @pytest.mark.parametrize("label,command", NO_CUT_FIRES,
                             ids=[c[0] for c in NO_CUT_FIRES])
    def test_a_real_cut_is_still_matched(self, label, command):
        assert _awk_matches(_pattern_for("supertool-no-cut.md"), command), label

    @pytest.mark.parametrize("label,command", NO_CUT_SILENT,
                             ids=[c[0] for c in NO_CUT_SILENT])
    def test_prose_and_paths_are_not_matched(self, label, command):
        assert not _awk_matches(_pattern_for("supertool-no-cut.md"), command), label


# The other rules in the index, one line each. Every `fires` case puts the
# invocation on a TAB-indented continuation line: that is the shape ` *` misses
# and `[[:space:]]*` catches, so each row fails under the older idiom and under
# no other difference. Every `silent` case names the command inside a quoted
# argument, which is the shape the anchor exists for.
OTHER_RULES = [
    ("merged-is-not-ancestry.md",
     "cd /tmp &&\n\tgit branch --merged master",
     "git commit -m " + APOS + "docs: --merged is not ancestry for git branch" + APOS),
    ("gh-pr-view-merge-have-ops.md",
     "cd /tmp &&\n\tgh pr view 1415 --json body",
     "echo " + APOS + "gh pr view has an op" + APOS),
    ("git-push-has-an-op.md",
     "cd /tmp &&\n\tgit push origin HEAD",
     "echo " + APOS + "git push has an op" + APOS),
    ("git-C-has-cwd.md",
     "cd /tmp &&\n\tgit -C /Users/x/repo status",
     "echo " + APOS + "git -C /tmp is not a cwd" + APOS),
    ("op-defaults-that-narrow.md",
     "cd /tmp &&\n\tsupertool " + APOS + "gh-prs:state=open" + APOS,
     "echo the supertool op " + APOS + "gh-prs" + APOS + " reads the whole repo"),
]


@needs_awk
@pytest.mark.parametrize("rule,fires,silent", OTHER_RULES,
                         ids=[r[0] for r in OTHER_RULES])
class TestTheRestOfTheIndex:
    """Same two directions, for the rules #1415 was widened to cover.

    Four `block` rules and one `remind` rule were already anchored, but with a
    ` *` that admits only spaces -- so a tab-indented continuation line, an
    ordinary way to write a multi-line command, walked through all five. A
    missed block is the safe direction on this gate, which is exactly why it
    went unnoticed; the `remind` one is the opposite, and fired on a mere
    mention of an op name in a test fixture while this file was being written.
    """

    def test_a_tab_indented_invocation_is_matched(self, rule, fires, silent):
        assert _awk_matches(_pattern_for(rule), fires), rule

    def test_the_command_merely_named_is_not_matched(self, rule, fires, silent):
        assert not _awk_matches(_pattern_for(rule), silent), rule


@needs_awk
class TestGhListLimit:

    @pytest.mark.parametrize("label,command", LIST_LIMIT_FIRES,
                             ids=[c[0] for c in LIST_LIMIT_FIRES])
    def test_a_real_invocation_is_still_matched(self, label, command):
        assert _awk_matches(_pattern_for("gh-list-limit.md"), command), label

    @pytest.mark.parametrize("label,command", LIST_LIMIT_SILENT,
                             ids=[c[0] for c in LIST_LIMIT_SILENT])
    def test_prose_naming_the_command_is_not_matched(self, label, command):
        assert not _awk_matches(_pattern_for("gh-list-limit.md"), command), label


class TestEveryBlockingRuleIsAnchored:
    """The class, not the two instances.

    Deliberately NOT behind `needs_awk`: it reads the index and never runs a
    regex, so gating it on awk would make it skip on the one platform where awk
    is usually absent -- a guard that reports coverage it does not have.

    A row is blocking when column 4 names `block` or column 5 sets `require` —
    `require` sets `blocked` regardless of the mode (pre-tool-hook.sh:161), which
    is why `gh-list-limit` refused a command while its frontmatter said `remind`.

    `harness-tools-blocked.md` is the one deliberate exception: its `~.` matches
    every subject on purpose, because the tools it names have no legitimate use
    here at all. It is exempted by name and by that reasoning, not by a laxer
    pattern test that would quietly absolve a future unanchored rule.
    """

    # The whole opening, not a prefix: every regex row in this index now spells
    # it identically. `[[:space:]]*` rather than ` *` because the latter admits
    # no tab, and it applies to the `^` branch too -- a command indented inside
    # a script is still a command.
    ANCHOR = r"(^|[;&|\n])[[:space:]]*"
    EXEMPT = {"harness-tools-blocked.md": "matches everything on purpose"}

    def test_live_rows(self):
        rows = []
        for raw in INDEX.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            f = raw.split(TAB)
            if len(f) < 5 or not f[1].startswith("~"):
                continue
            if f[2] not in self.EXEMPT:
                rows.append((f[2], f[1]))

        assert rows, "no regex rows found; the index shape changed"
        unanchored = [name for name, pat in rows if not pat[1:].startswith(self.ANCHOR)]
        assert not unanchored, (
            "rules whose match is not pinned to command position, so they fire on "
            "a mere mention: {0}".format(", ".join(unanchored)))


def test_the_change_is_findable():
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from _changelog_findable import assert_change_is_findable
    assert_change_is_findable(1415, REPO)
