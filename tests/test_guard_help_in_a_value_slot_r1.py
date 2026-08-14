"""A help flag in a VALUE slot un-claimed all 28 mappings (v0.36.0 round-1 audit).

#1430 made "asking a program to describe itself" a property of the guard rather
than a key each `replaces` entry repeats. It implemented that by scanning
`_guard_options(argv)` — which is the argv slice up to a bare `--`, i.e. **every**
token, not only flag-shaped ones — for `-h` or `--help`. So any `-h` anywhere
before the `--` un-claimed the whole registry, including one the program never
reads as a request for help:

    $ git commit -m -h          # commits, with the message `-h`
    $ git push origin -o -h x   # pushes, with the push-option `-h`

Both were `clean`, and `clean` is a positive claim that nothing here is replaced.

**What the programs actually do**, measured on this box (git 2.46.2, gh 2.50.0),
because the fix has to agree with them rather than with a guess:

    $ git commit -m -h          [master (root-commit) 645ea8c] -h        <- RAN
    $ git push origin -h        usage: git push [<options>] ...          <- help
    $ git push -h               usage: git push [<options>] ...          <- help
    $ git status -h             usage: git status [<options>] ...        <- help
    $ gh issue list -h          List issues in a GitHub repository.      <- help

So `git push origin -h` — the auditor's headline example — is a **genuine help
invocation** and its `clean` verdict was already right. The single discriminator
that separates the two columns is whether the help token was consumed as the
value of the option before it. A help flag after a *positional* is still help.

Three states, not two, because the third is honest and the other two are not:

* preceded by a positional (or nothing) -> HELP, the entry is un-claimed;
* preceded by a flag-shaped token       -> the guard cannot tell a value from a
  request without per-subcommand option arity it deliberately does not carry, so
  it scores the argv and blocks. A wrong block here is legible and one flag away
  from a working `git push -h`; the other direction is a silent `git push`.
* no help token at all                  -> unchanged.

Would these pass if the code did nothing? No. Every `blocked` case below is
`clean` at 709270d — which is what the audit reproduced.
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
    guard call with no config sees an empty registry and every `blocked`
    assertion below would fail for the wrong reason while every `clean` one
    passed vacuously. Naming the presets in a tmp config resolves them from the
    install directory, which is this checkout.
    """
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"presets": ["git", "github"]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    return supertool._load_config()


class TestAHelpTokenInAValueSlotIsNotAHelpRequest:
    """The defect, stated as the verdict rather than as a string."""

    @pytest.mark.parametrize("cmd", [
        # The auditor's reproduction: `-h` is `-m`'s value and the commit runs.
        "git commit -m -h",
        # Long spelling of the same option, same value slot.
        "git commit --message -h",
        # `--help` is no safer in a value slot than `-h` is.
        "git commit -m --help",
        # `-o` is `git push --push-option`. This one pushes. Written without a
        # refspec since #1684: `origin -o -h main` is un-claimed on arity
        # before the help classifier is reached, so it would pass this row
        # while saying nothing about a help token in a value slot.
        "git push origin -o -h",
        # Behind a global option, so the normaliser has to run first (#1421).
        "git -C /tmp/x commit -m -h",
    ])
    def test_the_registry_still_claims_the_invocation(self, shipped_presets, cmd):
        verdict = supertool.guard_command(cmd)
        assert verdict.state == "blocked", verdict

    def test_the_block_says_why_it_did_not_honour_the_help_flag(
            self, shipped_presets):
        # A block whose reason is invisible teaches people to route around it,
        # and this is the one block a reader will believe is a bug.
        verdict = supertool.guard_command("git commit -m -h")
        assert any("value" in note for note in verdict.notes), verdict
        assert "-h" in supertool.guard_refusal(verdict), verdict


class TestARealHelpInvocationIsStillUnClaimed:
    """The boundary. #1430 exists for these and must keep working."""

    @pytest.mark.parametrize("cmd", [
        "git commit --help",
        "git commit -h",
        "git status -h",
        "git push -h",
        # After a positional, which is where the auditor expected a hole and
        # git disagrees: this prints usage and pushes nothing.
        "git push origin -h",
        "gh issue list -h",
        "gh pr create --help",
        # A help flag after other *positionals* is still the first flag.
        "gh pr view 1 --help",
        # An ambiguous help token followed by an unambiguous one.
        "git commit -m -h --help",
    ])
    def test_it_is_not_blocked(self, shipped_presets, cmd):
        verdict = supertool.guard_command(cmd)
        assert verdict.state != "blocked", verdict


class TestTheRefusalStaysBounded:
    """#1391's budget covers the notes too, or it is not a budget.

    Each ambiguity note quotes its own segment, so a chained command yields one
    distinct note per segment. Rendering them uncapped is the per-match
    multiplication `_GUARD_TEXT_BUDGET` was introduced to remove, re-entered
    through a second door.
    """

    def test_two_hundred_ambiguous_segments_do_not_multiply_the_refusal(
            self, shipped_presets):
        command = " && ".join(
            f"git commit -m -h -m tag{i:05d}" for i in range(200))
        verdict = supertool.guard_command(command)
        assert verdict.state == "blocked", verdict.state
        refusal = supertool.guard_refusal(verdict)
        # Generous: the matches alone are allowed 1200 characters of registry
        # text plus their own framing. The point is that it is bounded at all.
        assert len(refusal) < 6000, len(refusal)

    def test_the_notes_it_dropped_are_counted_rather_than_silently_cut(
            self, shipped_presets):
        command = " && ".join(
            f"git commit -m -h -m tag{i:05d}" for i in range(200))
        refusal = supertool.guard_refusal(supertool.guard_command(command))
        assert "further note" in refusal, refusal[-800:]


class TestAntiVacuity:
    """Without these, every assertion above is a statement about a dead registry."""

    @pytest.mark.parametrize("cmd", [
        "git commit -m x",
        # `git push origin main` until #1684 — see the note above.
        "git push origin",
        "gh issue list --state open",
        "git -C /tmp/x commit -m x",
    ])
    def test_the_same_command_without_a_help_token_is_refused(
            self, shipped_presets, cmd):
        assert supertool.guard_command(cmd).state == "blocked", cmd


class TestTheClassifierItself:
    """`_guard_help_state` as a unit — three states, named."""

    @pytest.mark.parametrize("argv,expected", [
        (["git", "commit", "-m", "x"], "none"),
        (["git", "commit", "--help"], "help"),
        (["git", "push", "origin", "-h"], "help"),
        (["git", "commit", "-m", "-h"], "value"),
        (["git", "commit", "-m", "--help"], "value"),
        # `--` ends the option list, so a file named `--help` is a positional
        # and nothing here is a help request at all (the #1394 rule).
        (["gh", "pr", "create", "--", "--help"], "none"),
        # Not cluster-expanded: `-dh` un-claiming every mapping in the
        # repository is the failure mode `_GUARD_HELP_FLAGS` calls out.
        (["gh", "pr", "create", "-dh"], "none"),
        # One unambiguous help token beats an ambiguous one: git prints the
        # commit man page for this.
        (["git", "commit", "--help", "-m", "-h"], "help"),
        # And in the other order, which is where the first cut of this
        # classifier was order-dependent while its docstring said it was not.
        # A help flag takes no value, so a help token behind one is not in a
        # value slot — measured, `git commit -m -h --help` prints usage and
        # commits nothing (exit 129).
        (["git", "commit", "-m", "-h", "--help"], "help"),
        (["git", "commit", "-m", "-h", "-h"], "help"),
        ([], "none"),
        (["-h"], "help"),
    ])
    def test_it_names_the_three_cases(self, argv, expected):
        assert supertool._guard_help_state(argv) == expected, argv
