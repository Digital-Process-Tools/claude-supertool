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
from pathlib import Path

import pytest

import supertool

REPO = Path(__file__).resolve().parents[1]
MANUAL = REPO / ".claude" / "jit-context" / "tools" / "00-manual"
INDEX = MANUAL / "00-index.tsv"
SETTINGS = REPO / ".claude" / "settings.json"
GUARD = "hooks/pre-bash-guard.sh"
TAB = "\t"


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


def test_the_guard_is_a_third_entry_and_shadows_neither_jit_hook():
    """Two PreToolUse hooks on Bash have to compose, not replace each other.

    Claude Code runs every matching PreToolUse hook; a `deny` from any one of
    them stops the call. So the guard is added as its own entry rather than
    appended to the jit-context ones -- an entry whose command list grew a
    second member would make one script's failure the other's silence.
    """
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    entries = settings["hooks"]["PreToolUse"]
    for entry in entries:
        assert len(entry["hooks"]) == 1, entry
    commands = [h["command"] for entry in entries for h in entry["hooks"]]
    assert any("pre-tool-hook.sh" in c for c in commands), commands
    assert any("pre-path-hook.sh" in c for c in commands), commands
    assert any(GUARD in c for c in commands), commands


def test_the_guard_entry_matches_bash_only():
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    for entry in settings["hooks"]["PreToolUse"]:
        if any(GUARD in h["command"] for h in entry["hooks"]):
            assert entry.get("matcher") == "Bash", entry
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
        ("git push origin master", "git-push"),
        ("git push --force-with-lease", "git-push"),
        ("git push origin v0.35.0", "git-push"),
    ],
}


@pytest.mark.parametrize("rule", sorted(_RETIRED))
def test_a_retired_rule_is_gone_from_both_the_index_and_the_disk(rule):
    """Deleting one and not the other leaves a rule that never runs."""
    assert rule not in _indexed_files(), rule
    assert not (MANUAL / rule).exists(), rule


@pytest.mark.parametrize("rule,command,op", [
    (rule, command, op)
    for rule, cases in sorted(_RETIRED.items()) for command, op in cases])
def test_the_guard_covers_what_each_retired_rule_forbade(
        shipped_registry, rule, command, op):
    """The gate itself: `guard:`, on the rule's own command.

    Reading the registry is not this check. `git-C-has-cwd.md` would look
    retireable from the mapping list alone -- the git family is declared --
    and the command it exists for returns `clean`.
    """
    verdict = supertool.guard_command(command)
    assert verdict.state == "blocked", (rule, command, verdict)
    assert op in {m.op for m in verdict.matches}, (rule, command,
                                                   verdict.matches)


# --------------------------------------------------------------------------
# Kept: each for a reason that is itself asserted
# --------------------------------------------------------------------------

_KEPT_UNCOVERED = {
    # rule -> (command it forbids, why `replaces` cannot reach it)
    "git-C-has-cwd.md": (
        "git -C /tmp/x status",
        "argv matches a contiguous token prefix and -C's value sits inside "
        "it; no mapping can cover this until #1421"),
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
