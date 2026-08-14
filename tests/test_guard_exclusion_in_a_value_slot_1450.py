"""An `unless_flag` token in a VALUE slot silently un-claimed the entry (#1450).

    $ supertool 'guard:gh pr create -t --dry-run -b y'   ->  clean

That command creates a pull request whose **title is `--dry-run`**. The guard
read the `--dry-run` sitting in `-t`'s value slot as an exclusion and un-claimed
`gh pr create`, so it made the positive claim that nothing here is replaced
about a command that opens a PR.

**The repair is not the one #1449 applied to help flags.** There the ambiguous
slot is *blocked*, because the alternative is a silent `git push`. Here the
ambiguous slot is where previews live:

    $ git push --force-with-lease --dry-run

`--dry-run` is an exclusion sitting immediately after another flag. Blocking
that names `git-push` as the substitute, and an agent obeying the refusal
pushes for real -- the `misdirects` shape the v0.35.0 audit named. Measured:
seven shipped invocations flip clean -> blocked under a naive value-slot rule,
five of them previews of a destructive push. They are the boundary class below.

So the exclusion still stands, nothing new is blocked, and the verdict stops
being `clean`: an exclusion consumed from a possible value slot makes the
segment `undecided` with a note. Three states, not two.

**Only when every matched exclusion is ambiguous.** `git commit --amend
--dry-run` carries `--amend` where a program reads a flag, so the entry is
un-claimed for a reason the guard can state, and it stays silently clean.

Would these pass if the code did nothing? No -- every `undecided` case below is
`clean` at c685eaf, which is what the issue reproduced.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool


@pytest.fixture
def shipped_presets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The real `presets/git.json` and `presets/github.json`, through the loader.

    `tests/conftest.py` disables config discovery for the whole suite, so a
    guard call with no config sees an empty registry and every `undecided`
    assertion below would fail while every "not blocked" one passed vacuously.
    """
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"presets": ["git", "github"]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    return supertool._load_config()


class TestTheDefect:
    """The issue's own reproduction."""

    def test_it_is_no_longer_clean(self, shipped_presets):
        verdict = supertool.guard_command("gh pr create -t --dry-run -b y")
        assert verdict.state == "undecided", verdict

    def test_the_note_names_the_token_and_the_slot(self, shipped_presets):
        verdict = supertool.guard_command("gh pr create -t --dry-run -b y")
        note = " ".join(verdict.notes)
        assert "--dry-run" in note, verdict.notes
        assert "value" in note, verdict.notes

    def test_it_is_not_blocked_and_names_no_substitute(self, shipped_presets):
        # A block here would print `Use: gh-pr-create`, which performs the
        # thing `--dry-run` declines. The disclosure carries no `Use:` line.
        verdict = supertool.guard_command("gh pr create -t --dry-run -b y")
        assert verdict.state != "blocked", verdict
        assert not verdict.matches, verdict


class TestNothingNewIsBlocked:
    """The wrong-block surface of the rejected repair, pinned as a boundary.

    Every one of these is `clean` today and blocks under a naive value-slot
    rule. The `git push` previews are the `misdirects` shape: the refusal would
    name `git-push`, and the op pushes.
    """

    @pytest.mark.parametrize("cmd", [
        "git push --force-with-lease --dry-run",
        "git push --no-verify --dry-run",
        "git push -u --dry-run origin main",
        "git push --set-upstream --dry-run origin main",
        "git push --force-with-lease -n",
        "gh pr checks --required --watch",
        "gh pr create --draft --dry-run",
    ])
    def test_it_is_still_allowed(self, shipped_presets, cmd):
        assert supertool.guard_command(cmd).state != "blocked", cmd

    @pytest.mark.parametrize("cmd", [
        "git push --force-with-lease --dry-run",
        "gh pr create --draft --dry-run",
    ])
    def test_and_the_ambiguity_is_disclosed_rather_than_guessed(
            self, shipped_presets, cmd):
        # The guard cannot tell whether `--dry-run` is the preceding flag's
        # value; if it is, this command performs the real action. Saying so is
        # the whole difference from the `clean` it used to return.
        verdict = supertool.guard_command(cmd)
        assert verdict.state == "undecided", verdict


class TestAnUnambiguousExclusionStaysSilent:
    """No note where the guard *can* answer -- a disclosure under every command
    anyone writes is one nobody reads."""

    @pytest.mark.parametrize("cmd", [
        "git push --dry-run",
        "git push --dry-run origin main",
        "git commit --amend --dry-run",
        "git commit --amend",
        "git status -s -z",
        "gh issue list --web",
        "gh issue list --label bug --web",
        "gh pr create --web",
    ])
    def test_it_is_clean_with_no_notes(self, shipped_presets, cmd):
        verdict = supertool.guard_command(cmd)
        assert verdict.state == "clean", verdict
        assert verdict.notes == (), verdict


class TestTheClassifierItself:
    """`_guard_exclusion_state` as a unit -- three states, named."""

    @staticmethod
    def _entry(*unless):
        return supertool._Replacement(
            op="probe-op", argv=("gh", "pr", "create"), flag=None, value=None,
            use="probe-op", description="", unless_flag=tuple(unless))

    @pytest.mark.parametrize("argv,unless,expected", [
        (["gh", "pr", "create"], ["--dry-run"], "none"),
        (["gh", "pr", "create", "--dry-run"], ["--dry-run"], "excluded"),
        # After a positional, so no option is waiting on a value.
        (["gh", "pr", "create", "x", "--dry-run"], ["--dry-run"], "excluded"),
        # The defect: consumed as `-t`'s value.
        (["gh", "pr", "create", "-t", "--dry-run"], ["--dry-run"], "value"),
        # A long flag carrying its value attached leaves no slot open.
        (["gh", "pr", "create", "--title=x", "--dry-run"],
         ["--dry-run"], "excluded"),
        # One unambiguous exclusion decides the whole argv.
        (["gh", "pr", "create", "--web", "-t", "--dry-run"],
         ["--web", "--dry-run"], "excluded"),
        # `*` is any flag at all, and a flag in a value slot is still a flag
        # the op does not forward -- the #1394 reasoning is unchanged.
        (["gh", "pr", "create", "-t", "--anything"], ["*"], "excluded"),
        # `--` ends the option list (#1394).
        (["gh", "pr", "create", "--", "--dry-run"], ["--dry-run"], "none"),
    ])
    def test_it_names_the_three_cases(self, argv, unless, expected):
        entry = self._entry(*unless)
        assert supertool._guard_exclusion_state(entry, argv) == expected, argv


class TestAntiVacuity:
    """Without these, every assertion above is about a dead registry."""

    @pytest.mark.parametrize("cmd", [
        "gh pr create -t x -b y",
        # `git push origin main` until #1684 — a named refspec is now
        # `uncovered` on arity, which would make this row pass for a reason
        # that has nothing to do with an exclusion.
        "git push origin",
        "git commit -m x",
        "gh pr checks",
    ])
    def test_the_same_command_without_an_exclusion_is_refused(
            self, shipped_presets, cmd):
        assert supertool.guard_command(cmd).state == "blocked", cmd
