"""#1707 — the NOT COVERED note prescribed an invocation it could not check.

`git push origin v0.43.0` is `uncovered`, correctly: `git-push` pushes the
current branch and the argv names a tag. The note said so and then closed with

    `git-push` is the same invocation without them, if that is what you meant

which is **false for this argv**. Dropping `v0.43.0` yields `git push origin`,
which publishes a branch. Three recorded instances, the third a force push
(`git push --force-with-lease origin docs/claude-md-git-c`) where the dropped
tokens included the safety on the operation and the note enumerated only the
operands.

The judgment taken: **stop asserting equivalence the guard cannot check.** The
alternative — firing the clause only when the dropped tokens are inert — needs
the guard to know what a refspec is, per utility, which is the case work
`_GUARD_GLOBAL_OPTIONS` refuses to grow for exactly this reason. So the note
now says what the op *performs* and that it is a different command, which is
true of every argv that reaches this branch and asks git nothing.

**Length is load-bearing here and is pinned below.** `_guard_notes` quotes each
note through `_guard_quote(note, _GUARD_DESC_CAP)`, so a note over 320
characters is truncated *from the end* — where this clause lives. The reported
force-push argv was already over the cap before this change; the tag argv was
not, and must stay under it, or the honest sentence is cut off in the one
channel an agent actually reads.

The `--help` instance in comment 1 (`gh run view --help` routed to
`gh-run:NUMBER`) does **not** reproduce: `_guard_help_state` already un-claims
every entry for a help flag standing where a program reads one (#1430), and
that is asserted here so a regression is visible rather than rediscovered.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent
_GIT_OPS = json.loads(
    (_ROOT / "presets" / "git.json").read_text(encoding="utf-8"))["ops"]
_GH_OPS = json.loads(
    (_ROOT / "presets" / "github.json").read_text(encoding="utf-8"))["ops"]

#: The reported argv, verbatim from the issue body.
_TAG_PUSH = "git push origin v0.43.0"
#: The third instance, verbatim from the 2026-08-15 comment.
_FORCE_PUSH = "git push --force-with-lease origin docs/claude-md-git-c"


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
          config: Dict[str, Any]) -> None:
    (tmp_path / ".supertool.json").write_text(
        json.dumps(config), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    supertool._load_config()


@pytest.fixture
def shipped_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    _load(tmp_path, monkeypatch, {"ops": _GIT_OPS})
    return tmp_path


@pytest.fixture
def shipped_gh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    _load(tmp_path, monkeypatch, {"ops": _GH_OPS})
    return tmp_path


def _note(command: str) -> str:
    verdict = supertool.guard_command(command)
    assert verdict.state == "uncovered", (command, verdict)
    assert verdict.uncovered, (
        "no uncovered line at all — every assertion below would pass on "
        "silence: " + repr(verdict))
    return " | ".join(verdict.uncovered)


# --- the equivalence claim is gone, and something true stands in its place --
#
# Each negative below is paired with a positive on the SAME note, because
# "the note does not say X" passes just as well when the note is empty.

@pytest.mark.parametrize("command", [_TAG_PUSH, _FORCE_PUSH])
def test_the_note_no_longer_offers_the_op_as_the_same_invocation(
        shipped_git, command):
    note = _note(command)
    assert "same invocation" not in note, (
        "the note still asserts an equivalence the guard cannot check: "
        + note)
    # Positive control on the same fixture: the note exists and is about the
    # op it declined to prescribe.
    assert "no op covers this form" in note, note
    assert "git-push" in note, note


@pytest.mark.parametrize("command", [_TAG_PUSH, _FORCE_PUSH])
def test_the_note_says_what_the_op_performs_instead(shipped_git, command):
    note = _note(command)
    assert "performs `git push` and nothing more" in note, note
    assert "different command" in note, note


def test_nothing_past_the_prefix_is_promised_to_survive(shipped_git):
    """The third instance: `--force-with-lease` is not an operand.

    The old sentence enumerated the positionals and then offered "the same
    invocation without them", so a flag that changes the operation's failure
    mode was dropped by the suggested equivalent without ever being named.
    "and nothing more" is true of the flag as well as of the refspec, and is
    asserted on the argv that was actually run.
    """
    note = _note(_FORCE_PUSH)
    assert "and nothing more" in note, note
    # Must-fire half: the note is genuinely about this argv, not a stub.
    assert "docs/claude-md-git-c" in note, note


# --- the honest clause has to survive the render, not just the construction --

def test_the_reported_tag_push_note_is_not_truncated_by_the_text_cap(
        shipped_git):
    """`_GUARD_DESC_CAP` cuts from the end, so the fix does not sit there.

    Asserted on the rendered sentence rather than on the constructed line,
    because what an agent reads is `guard_uncovered_note`.
    """
    verdict = supertool.guard_command(_TAG_PUSH)
    line = verdict.uncovered[0]
    assert len(line) <= supertool._GUARD_DESC_CAP, (
        "the note is %d characters against a %d cap: "
        % (len(line), supertool._GUARD_DESC_CAP) + line)
    rendered = supertool.guard_uncovered_note(verdict)
    assert "performs `git push` and nothing more" in rendered, rendered


#: Long enough that the echoed command alone spends the note's whole budget.
#: A refspec is caller-supplied and unbounded; nothing caps it before the note.
_LONG_PUSH = ("git push --force-with-lease origin "
              "refs/heads/docs/claude-md-git-config-super-extra-long-branch")


@pytest.mark.parametrize("command", [_TAG_PUSH, _FORCE_PUSH, _LONG_PUSH])
def test_the_clause_survives_the_cap_for_an_argv_of_any_length(
        shipped_git, command):
    """Found by the #1707 self-review, and it is the whole point of the fix.

    `_guard_notes` quotes each note through `_GUARD_DESC_CAP` and truncates
    **from the end**. The corrective clause was written at the end, so for
    the very argv the third instance reported it arrived cut mid-word, and
    for a longer refspec it did not arrive at all — the honest sentence
    replaced by `… (+N chars)`, which discloses that something was cut and
    not that what was cut was the correction. So the clause is now the
    *head* of the note: what truncation now costs is the echo of the command
    the caller just typed, and the fact that nothing was blocked, which
    `guard_uncovered_note`'s own wrapper states outside the cap.

    Parametrised over all three lengths rather than the short one, because a
    row that only ever runs under the cap asserts nothing about truncation.
    """
    verdict = supertool.guard_command(command)
    assert verdict.state == "uncovered", (command, verdict)
    rendered = supertool.guard_uncovered_note(verdict)
    assert "performs `git push` and nothing more" in rendered, (
        "the clause that replaced the false equivalence did not survive the "
        "%d-character cap for this argv: " % supertool._GUARD_DESC_CAP
        + rendered)


def test_the_longest_argv_really_does_exercise_the_cap(shipped_git):
    """The must-fire control for the row above.

    Without it, `_LONG_PUSH` passing proves nothing: a note that happens to
    fit renders identically to one that was written to survive being cut.
    """
    line = supertool.guard_command(_LONG_PUSH).uncovered[0]
    assert len(line) > supertool._GUARD_DESC_CAP, (
        "the long argv no longer overruns the cap, so the truncation "
        "assertion above is vacuous: %d characters" % len(line))
    assert "(+" in supertool.guard_uncovered_note(
        supertool.guard_command(_LONG_PUSH)), "nothing was truncated at all"


def test_the_op_output_carries_the_same_sentence(shipped_git):
    out = supertool.op_guard(_TAG_PUSH)
    assert "NOT COVERED" in out, out
    assert "same invocation" not in out, out
    assert "performs `git push` and nothing more" in out, out


# --- comment 1: the `--help` instance does not reproduce --------------------

@pytest.mark.parametrize("command", [
    "gh run view --help",
    "gh run view -h",
])
def test_a_help_flag_is_already_un_claimed_everywhere(shipped_gh, command):
    """No `unless_flag: --help` is needed; `_guard_help_state` covers it.

    Paired with the must-fire row below, or "state is clean" would also be
    what a registry that loaded nothing produces.
    """
    assert supertool.guard_command(command).state == "clean", command


def test_the_gh_registry_under_test_does_block_something(shipped_gh):
    """The positive control for the row above."""
    verdict = supertool.guard_command("gh run view 123")
    assert verdict.state == "blocked", verdict
