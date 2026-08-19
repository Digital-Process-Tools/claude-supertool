"""The guard could not read `--amend` behind another flag, and said so (#1816).

    $ supertool 'guard:git commit --no-verify --amend'
    UNDECIDED: ... `--amend` ... sits immediately after another flag, so an
    exclusion that un-claims an op could not be told from that option's value

Reported by the guard itself, which is the right behaviour and why the filing
is about narrowing the window rather than about a wrong answer. Two halves,
independent:

**The parse.** `--no-verify` takes no value — measured on git 2.46.2, not
reasoned (`git commit -h`). So nothing behind it can be that option's value and
`--amend` stands where a program reads a flag. `_GUARD_VALUELESS_FLAGS` carries
that fact for the four git subcommands the shipped presets gate, and **only**
that half: a flag absent from the table stays ambiguous, which is exactly where
#1450 left it. Three states, and the third is the one that was already there.

**The reporting.** The note said the guard had not read something without
saying *what*. It now names the op whose claim went unevaluated and the
re-issue that would be readable, because "a check was skipped" and "whether
`git-commit` replaces this command was never asked" are different sentences to
act on.

No behaviour change on the allow decision: every command below was allowed
before and is allowed after. What changes is how many of them are allowed
*silently and for a stated reason* rather than allowed with a disclosure.

Would these pass if the code did nothing? No — at fa2ba903
`guard_command("git commit --no-verify --amend").state` is `undecided` with a
note, which is the issue's own reproduction.
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
    guard call with no config sees an empty registry, every `clean` assertion
    below would pass vacuously and every note assertion would fail.
    """
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"presets": ["git", "github"]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    return supertool._load_config()


class TestTheDefect:
    """The issue's own reproduction, and the ones next to it in that file."""

    @pytest.mark.parametrize("cmd", [
        "git commit --no-verify --amend",
        "git commit -a --amend",
        "git commit -s --amend",
        "git commit --allow-empty-message --amend",
        # A cluster of short flags, every letter of it valueless.
        "git commit -as --amend",
        # The exclusion behind a valueless flag on the other gated verbs.
        "git push --force-with-lease --dry-run",
        "git push --set-upstream --dry-run origin main",
        "git status --branch --porcelain",
    ])
    def test_it_is_read_rather_than_disclosed(self, shipped_presets, cmd):
        verdict = supertool.guard_command(cmd)
        assert verdict.state == "clean", verdict
        assert verdict.notes == (), verdict


class TestTheWindowStaysOpenWhereItMustFire:
    """The must-fire partner for every silence asserted above.

    Without this class, a guard that had stopped classifying anything at all
    would pass `TestTheDefect` completely.
    """

    @pytest.mark.parametrize("cmd", [
        # `-m` takes a value on git 2.46.2, so `--amend` here may be the
        # message. Reading it as an exclusion is the #1450 defect exactly.
        "git commit -m --amend",
        # `-t` is gh's, and gh is deliberately not in the table.
        "gh pr create -t --dry-run -b y",
        "gh pr create --draft --dry-run",
    ])
    def test_it_is_still_undecided_with_a_note(self, shipped_presets, cmd):
        verdict = supertool.guard_command(cmd)
        assert verdict.state == "undecided", verdict
        assert verdict.notes, verdict

    def test_and_none_of_them_became_a_block(self, shipped_presets):
        # The allow decision is unchanged: an unreadable command is still
        # allowed, never blocked toward an op that would perform it.
        for cmd in ("git commit -m --amend", "gh pr create -t --dry-run -b y"):
            assert supertool.guard_command(cmd).state != "blocked", cmd


class TestTheNoteSaysWhatWentUnread:
    """The reporting half. `a check was skipped` is not a sentence to act on."""

    def test_it_names_the_op_whose_claim_went_unevaluated(
            self, shipped_presets):
        notes = " ".join(supertool.guard_command(
            "gh pr create -t --dry-run -b y").notes)
        assert "gh-pr-create" in notes, notes

    def test_it_names_the_token_and_the_slot_still(self, shipped_presets):
        # #1450's contract, kept: the note must stay specific about which
        # token it could not place.
        notes = " ".join(supertool.guard_command(
            "gh pr create -t --dry-run -b y").notes)
        assert "--dry-run" in notes, notes
        assert "value" in notes, notes

    def test_it_names_a_readable_re_issue(self, shipped_presets):
        notes = " ".join(supertool.guard_command(
            "gh pr create -t --dry-run -b y").notes)
        assert "guard:" in notes, notes

    def test_the_note_still_fits_the_renderers_budget(self, shipped_presets):
        # The old note rendered as `… (+3 chars)` through the hook, i.e. the
        # sentence was being cut in the channel it exists for. A longer note
        # would cut more; this is the assertion that keeps it honest.
        verdict = supertool.guard_command("gh pr create -t --dry-run -b y")
        text = supertool.guard_notes_text(verdict.notes)
        assert "… (+" not in text, text


class TestTheTableItself:
    """`_guard_flag_takes_no_value` as a unit — and its third state."""

    @pytest.mark.parametrize("argv,token,expected", [
        (["git", "commit"], "--no-verify", True),
        (["git", "commit"], "-a", True),
        (["git", "commit"], "-as", True),
        (["git", "commit"], "-m", False),
        (["git", "commit"], "-F", False),
        # Not in the table at all — the third state, rendered as False so the
        # neighbour stays ambiguous rather than being guessed either way.
        (["git", "commit"], "--pathspec-from-file", False),
        (["git", "commit"], "--invented-yesterday", False),
        # A cluster is valueless only if EVERY letter is.
        (["git", "commit"], "-am", False),
        # Scoped per subcommand rather than answered from a union: `-a` is
        # `git commit --all` and git push has no `-a` at all, so the same
        # token answers True on one row and falls into the unknown state on
        # the next.
        (["git", "push"], "-u", True),
        (["git", "push"], "-a", False),
        # A subcommand no preset gates has no row and answers False.
        (["git", "rebase"], "--continue", False),
        (["gh", "pr", "create"], "--draft", False),
    ])
    def test_it_answers_per_subcommand(self, argv, token, expected):
        assert supertool._guard_flag_takes_no_value(argv, token) is expected


class TestAntiVacuity:
    """Without these, every assertion above is about a dead registry."""

    @pytest.mark.parametrize("cmd", [
        "git commit -m x",
        "git status",
        "gh pr create -t x -b y",
    ])
    def test_the_same_verb_with_no_exclusion_is_refused(
            self, shipped_presets, cmd):
        assert supertool.guard_command(cmd).state == "blocked", cmd

    def test_the_table_is_not_empty(self):
        assert supertool._GUARD_VALUELESS_FLAGS
