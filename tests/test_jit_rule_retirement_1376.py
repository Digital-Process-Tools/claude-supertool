"""Retiring a `block` rule is gated on its replacement covering its own case (#1376).

#1347 shipped the registry-driven guard and #1384 grew its mappings to 22 ops.
Three hand-written markdown rules under `.claude/jit-context/tools/00-manual/`
now cover ground the guard covers, and #1376 asks when they may go.

**The premise the issue was filed on has to be dealt with first, because it
makes the retirement unsafe.** The shipped guard lives in the plugin's
`hooks.json`; the plugin install this repository's own sessions run is
v0.25.0, whose `hooks/` holds `session-start.sh` and no `pre-bash-guard.sh` at
all. So deleting the markdown removed the only enforcement these sessions had
in exchange for one that reaches nobody until the plugin install updates --
and #1376's three orderings are all ways of choosing which side of that gap to
stand on.

The fourth ordering is to make the premise false: `.claude/settings.json` wires
the repository's own `hooks/pre-bash-guard.sh`, this repo dogfoods the guard it
ships, and the duplicate rules go in the same commit with nothing uncovered in
between. That also permanently kills #1376's option 3 -- one rule firing twice
with two different messages -- rather than accepting it for one release.

**The retirement gate is per-rule and is `guard:`, not the registry.** A rule's
replacement existing is not the same as its replacement covering the rule's own
case, so every row below asserts the verdict on the command the rule forbids.
Three rules stay, and each one's `clean` verdict is asserted just as hard: when
#1421 lands and `git -C /tmp/x status` starts blocking, this file goes red and
the retirement decision is forced rather than forgotten.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import supertool

REPO = Path(__file__).resolve().parents[1]
MANUAL = REPO / ".claude" / "jit-context" / "tools" / "00-manual"
INDEX = MANUAL / "00-index.tsv"
SETTINGS = REPO / ".claude" / "settings.json"
GUARD = "hooks/pre-bash-guard.sh"
TAB = "\t"


needs_awk = pytest.mark.skipif(
    shutil.which("awk") is None,
    reason="awk absent: no verdict is available, which is not the same as a pass")


def _pattern_for(rule_file):
    """Column 2 of the live row naming RULE_FILE, tilde stripped."""
    for raw in INDEX.read_text(encoding="utf-8").splitlines():
        fields = raw.split(TAB)
        if len(fields) >= 3 and fields[2] == rule_file:
            assert fields[1].startswith("~"), rule_file
            return fields[1][1:]
    raise AssertionError("no row for {0} in {1}".format(rule_file, INDEX))


def _awk_matches(pattern, subject):
    """Exactly what pre-tool-hook.sh:137 does: match(tolower(cmd), pat).

    Through awk rather than `re` because awk is what compiles these and the
    two disagree; via ENVIRON rather than `-v` so escapes arrive intact.
    """
    env = dict(os.environ, JIT_PAT=pattern, JIT_SUBJ=subject)
    proc = subprocess.run(
        ["awk", 'BEGIN { if (match(tolower(ENVIRON["JIT_SUBJ"]), '
                'ENVIRON["JIT_PAT"])) print "MATCH"; else print "NO" }'],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env)
    assert proc.returncode == 0, "awk refused the pattern: " + proc.stderr
    out = proc.stdout.strip()
    assert out in ("MATCH", "NO"), "awk said {0!r}".format(out)
    return out == "MATCH"


def _indexed_files():
    out = []
    for raw in INDEX.read_text(encoding="utf-8").splitlines():
        fields = raw.split(TAB)
        if len(fields) >= 3 and fields[2]:
            out.append(fields[2])
    return out


@pytest.fixture
def shipped_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    ops = {}
    for path in sorted((REPO / "presets").glob("*.json")):
        ops.update(json.loads(path.read_text(encoding="utf-8")).get("ops")
                   or {})
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": ops}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    supertool._load_config()
    return tmp_path


# --------------------------------------------------------------------------
# The premise: this repository runs the guard it ships
# --------------------------------------------------------------------------

def test_this_repo_wires_its_own_shipped_bash_guard():
    """Without this the retirement below is a net loss of enforcement.

    `${CLAUDE_PLUGIN_ROOT}` is a plugin-install variable and is unset for a
    project hook, so the wiring uses `$CLAUDE_PROJECT_DIR` -- which in a git
    worktree is that worktree, so each branch dogfoods its own presets.
    """
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    entries = settings["hooks"]["PreToolUse"]
    wired = [h["command"] for entry in entries for h in entry["hooks"]
             if GUARD in h["command"]]
    assert len(wired) == 1, [h["command"] for e in entries
                             for h in e["hooks"]]
    assert "CLAUDE_PROJECT_DIR" in wired[0], wired[0]
    assert (REPO / GUARD).is_file()


def test_the_guard_is_its_own_entry_and_shares_its_command_list_with_nothing():
    """Two PreToolUse hooks on the same tool have to compose, not replace.

    Claude Code runs every matching PreToolUse hook; a `deny` from any one of
    them stops the call. So the guard is registered as its own entry rather
    than appended to another -- an entry whose command list grew a second
    member would make one script's failure the other's silence.

    This asserted, until #1726, that the `pre-tool-hook.sh` and
    `pre-path-hook.sh` registrations sat beside it in this file. They no longer
    do, and the two assertions were dropped rather than the property weakened:
    those hooks are registered by the `claude-jit-context` plugin's own
    `hooks.json` through `${CLAUDE_PLUGIN_ROOT}`, and the copies here reached
    into `$HOME/Documents/claude-jit-context`, so they were dead in every clone
    but one and a duplicate in that one. What this test is actually about --
    one entry, one command, no shadowing -- is unchanged and still checked
    against every entry in the file, including any the plugin does not own.
    """
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    entries = settings["hooks"]["PreToolUse"]
    for entry in entries:
        assert len(entry["hooks"]) == 1, entry
    commands = [h["command"] for entry in entries for h in entry["hooks"]]
    assert any(GUARD in c for c in commands), commands


def test_the_guard_entry_matches_every_shell_tool():
    """`Bash` alone until #1413, which was the same defect one layer out.

    Wherever the PowerShell tool is enabled Claude routes shell commands
    through it, so a `Bash`-only matcher meant this repository's own dogfooded
    gate never ran and said nothing -- indistinguishable from a clean pass.
    Kept as a membership test rather than an equality one so a third shell
    tool is added without rewriting the assertion.
    """
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    for entry in settings["hooks"]["PreToolUse"]:
        if any(GUARD in h["command"] for h in entry["hooks"]):
            matcher = entry.get("matcher") or ""
            assert "Bash" in matcher, entry
            assert "PowerShell" in matcher, entry
            return
    raise AssertionError("no entry wires " + GUARD)


# --------------------------------------------------------------------------
# Retired: the guard blocks the command the rule forbade
# --------------------------------------------------------------------------

_RETIRED = {
    "gh-pr-view-merge-have-ops.md": [
        ("gh pr view 1424", "gh-pr"),
        ("gh pr merge 1424 --squash", "gh-pr-merge"),
        ("gh pr create --title x", "gh-pr-create"),
    ],
    "gh-list-limit.md": [
        ("gh issue list --limit 50", "gh-issues"),
        # The half that only became true in #1384 step 3. Retiring this rule
        # on the strength of `gh issue list` alone would have dropped the
        # `gh pr list` warning with nothing behind it.
        ("gh pr list --state open", "gh-prs"),
    ],
    "git-push-has-an-op.md": [
        ("git push", "git-push"),
        ("git push --force-with-lease", "git-push"),
        # #1684 moved these two from `blocked` to `uncovered`: a named refspec
        # un-claims every `git push` entry on arity, because `git-push` pushes
        # the branch you are on and takes no ref. The retirement gate is
        # weakened, deliberately and on the record — the rule stopped the
        # command AND named the op, and the guard now only names the op, on
        # every such call, in the transcript. What was traded for it is the
        # refusal that told a caller pushing a TAG to run an op that pushes a
        # branch: it succeeded, and published a ref nobody had typed.
        ("git push origin master", "git-push", "uncovered"),
        ("git push origin v0.35.0", "git-push", "uncovered"),
    ],
}


@pytest.mark.parametrize("rule", sorted(_RETIRED))
def test_a_retired_rule_is_gone_from_both_the_index_and_the_disk(rule):
    """Deleting one and not the other leaves a rule that never runs."""
    assert rule not in _indexed_files(), rule
    assert not (MANUAL / rule).exists(), rule


@pytest.mark.parametrize("rule,command,op,state", [
    (rule, case[0], case[1], case[2] if len(case) > 2 else "blocked")
    for rule, cases in sorted(_RETIRED.items()) for case in cases])
def test_the_guard_covers_what_each_retired_rule_forbade(
        shipped_registry, rule, command, op, state):
    """The gate itself: `guard:`, on the rule's own command.

    Reading the registry is not this check. `git-C-has-cwd.md` would look
    retireable from the mapping list alone -- the git family is declared --
    and the command it exists for returns `clean`.
    """
    verdict = supertool.guard_command(command)
    assert verdict.state == state, (rule, command, verdict)
    if state == "blocked":
        assert op in {m.op for m in verdict.matches}, (rule, command,
                                                       verdict.matches)
    else:
        # `uncovered` still has to NAME the op, or the retired rule's whole
        # payload is gone rather than moved (#1684).
        assert op in " ".join(verdict.uncovered), (rule, command, verdict)


# --------------------------------------------------------------------------
# Kept: each for a reason that is itself asserted
# --------------------------------------------------------------------------

_KEPT_UNCOVERED = {
    # rule -> (command it forbids, why `replaces` cannot reach it)
    # #1421 closed the half this row used to cite: `git -C P status` is now
    # BLOCKED, because the matcher strips git's global options before scoring.
    # The rule is still kept, and the reason moved rather than disappeared --
    # it also covers `git -C W diff` and `git -C W log`, and `git diff` /
    # `git log` are recorded ABSENCES in presets/git.json (the ops answer
    # narrower questions), so no mapping will ever claim them.
    # #1438 then narrowed the `match` itself to those shapes, so `status`,
    # `commit`, `push` and `worktree list` under `-C` are the guard's alone.
    # This row asserting `clean` is no longer the whole gate -- the section at
    # the foot of this file asserts the other side, that nothing the registry
    # claims is still matched by the rule.
    "git-C-has-cwd.md": (
        "git -C /tmp/x diff",
        "git-diff answers a narrower question than raw git diff and is a "
        "recorded absence, so the -C rows the guard now covers are not the "
        "whole rule"),
    "merged-is-not-ancestry.md": (
        "git branch --merged master",
        "git-worktrees answers merge state for a WORKTREE branch, not for an "
        "arbitrary one, so a mapping would name an op with a different "
        "population"),
    "supertool-no-cut.md": (
        "python3 supertool.py 'gh-pr:1424:status' | tail -3",
        "it is about piping an OP's own output, which `replaces` cannot "
        "express at any spelling -- the guard's subject is the raw command "
        "an op supersedes"),
}

_KEPT_NOT_BASH = {
    "harness-tools-blocked.md": "it fires on Read/Edit/Write/Grep/Glob, and "
                                "the guard is a PreToolUse(Bash) hook",
    "op-defaults-that-narrow.md": "it fires on a supertool op string, not on "
                                  "a raw command",
}


@pytest.mark.parametrize(
    "rule", sorted(_KEPT_UNCOVERED) + sorted(_KEPT_NOT_BASH))
def test_a_kept_rule_is_still_indexed_and_still_on_disk(rule):
    assert rule in _indexed_files(), rule
    assert (MANUAL / rule).is_file(), rule


@pytest.mark.parametrize("rule,command,why", [
    (rule, command, why)
    for rule, (command, why) in sorted(_KEPT_UNCOVERED.items())])
def test_a_kept_rule_forbids_something_the_guard_still_lets_through(
        shipped_registry, rule, command, why):
    """The other half of the gate, and the one that expires.

    Each of these is `clean` today, which is exactly why the rule stays. When
    a mapping starts covering one, this goes red and names the rule whose
    retirement is now due -- rather than the rule quietly becoming a second
    refusal with a different message, which is #1376's option 3.
    """
    assert supertool.guard_command(command).state == "clean", (
        rule, command, why)


def test_the_index_and_the_directory_still_agree():
    """A row with no file, or a file with no row, is a rule that never runs."""
    indexed = set(_indexed_files())
    on_disk = {p.name for p in MANUAL.glob("*.md")}
    assert indexed == on_disk, {"indexed only": sorted(indexed - on_disk),
                                "on disk only": sorted(on_disk - indexed)}


# --------------------------------------------------------------------------
# Nothing may be refused twice (#1438)
# --------------------------------------------------------------------------
#
# `git-C-has-cwd.md` is kept for the shapes no `replaces` entry claims, which
# means its `match` is now coupled to the registry: the moment a mapping grows
# to cover a subcommand the regex still fires on, that command is refused twice
# with two different messages -- #1376's option 3, which #1428 and #1437 were
# supposed to have ended. Nothing about adding a `replaces` entry makes anyone
# open the TSV, so the coupling is enforced here instead of remembered.
#
# The commands are DERIVED from the shipped registry rather than listed, so a
# new git mapping enters this test by existing. `unless_flag` spellings are
# derived too: they are the half that is guard-clean today and the half most
# likely to flip.

GIT_C = "git -C /tmp/wt "


def _git_commands_the_registry_claims():
    """(op, `git -C PATH ...` form) for every git `replaces` argv shipped."""
    out = set()
    for path in sorted((REPO / "presets").glob("*.json")):
        ops = json.loads(path.read_text(encoding="utf-8")).get("ops") or {}
        for op, spec in ops.items():
            for entry in spec.get("replaces") or []:
                argv = entry.get("argv") or ""
                if not argv.startswith("git "):
                    continue
                tail = argv[len("git "):]
                out.add((op, GIT_C + tail))
                if entry.get("flag"):
                    out.add((op, GIT_C + tail + " " + entry["flag"]))
                for flag in entry.get("unless_flag") or []:
                    out.add((op, GIT_C + tail + " " + flag))
    assert out, "no git `replaces` entry found -- this test proved nothing"
    return sorted(out)


@needs_awk
@pytest.mark.parametrize("op,command", _git_commands_the_registry_claims())
def test_no_command_is_blocked_by_both_the_guard_and_the_rule(
        shipped_registry, op, command):
    """The coupling, made loud.

    Read the failure as: the registry now claims this subcommand, so
    `git-C-has-cwd.md`'s `match` has to stop firing on it -- not as a bug in
    the mapping.

    The guard-CLEAN half of these cases -- every `unless_flag` spelling -- is
    VACUOUS today and is here for the day it stops being: `not (blocked and
    fires)` holds whatever the rule does when the guard says nothing. What
    stops that reading as coverage is `test_the_double_block_gate_is_not_
    vacuous` below, and `_RULE_STILL_FIRES`, which asserts the flagged shapes
    positively.
    """
    blocked = supertool.guard_command(command).state == "blocked"
    fires = _awk_matches(_pattern_for("git-C-has-cwd.md"), command)
    assert not (blocked and fires), (
        "{0!r} is refused twice: the shipped guard (op {1}) and "
        "git-C-has-cwd.md both answer it with different messages. Narrow the "
        "rule's match in 00-index.tsv.".format(command, op))


# The other direction, without which narrowing the pattern to nothing at all
# would satisfy every case above. Each is guard-clean because `git diff` and
# `git log` are recorded ABSENCES in presets/git.json, and because every flag
# below sits in git-status's `unless_flag` -- the guard declines a flagged
# `status` outright, so those shapes are this rule's or nobody's.
_RULE_STILL_FIRES = [
    ("git -C /tmp/wt diff", "git-diff answers a narrower question"),
    ("git -C /tmp/wt log master..HEAD", "git-diverge answers a range"),
    # Every `unless_flag` git-status carries, not just the one the rule's table
    # happens to name: the guard declines all four, so all four are this rule's
    # or nobody's, and the first draft of the narrowing left three unrefused.
    ("git -C /tmp/wt status --porcelain", "git-worktrees answers busy-ness"),
    ("git -C /tmp/wt status --short", "same question, shorter flag"),
    ("git -C /tmp/wt status -s", "same question, shortest flag"),
    ("git -C /tmp/wt status -z", "same question, NUL-separated"),
]


@needs_awk
def test_the_double_block_gate_is_not_vacuous(shipped_registry):
    """At least one derived command must actually reach the guard.

    Without this, a registry that claimed no git command at all -- or a
    `guard_command` that stopped answering -- would satisfy every case above
    by making `blocked` false everywhere, and the gate would read as green
    while checking nothing.
    """
    blocked = [c for _, c in _git_commands_the_registry_claims()
               if supertool.guard_command(c).state == "blocked"]
    assert len(blocked) >= 4, blocked


@needs_awk
@pytest.mark.parametrize("command,why", _RULE_STILL_FIRES)
def test_the_rule_still_fires_on_what_no_mapping_claims(
        shipped_registry, command, why):
    assert supertool.guard_command(command).state == "clean", (command, why)
    assert _awk_matches(_pattern_for("git-C-has-cwd.md"), command), (
        command, why)
