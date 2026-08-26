"""#1873 — a blocked compound call also discards its earlier commands.

A `PreToolUse` decision is made on the whole Bash call, so a refused
`A && B` never runs `A` either — even when `A` was a plain write that named
no op the registry replaces. The refusal used to speak only about `B`, so
"the guard stopped me from running `B`" was read as "the guard stopped me
from running `B`" and not "... and also discarded `A`", the exact
misdirection #1873's own two instances describe.

The fix does not change what is blocked: it names, on a `blocked` verdict,
any earlier segment(s) in the same call that will not run either, because
nothing in the call runs at all once the whole thing is denied.

The discard text lives on `GuardVerdict.discarded`, a field of its own,
NOT folded into `verdict.notes`. The first cut of this fix did fold it in,
and a spawned reviewer caught the bug that made: `notes` shares a fixed
`_GUARD_MAX_NOTES` (3) slots with #1450's per-op ambiguity disclosures, so a
command already carrying three of THOSE had one silently dropped the moment
a discard note took a fourth slot -- an absence produced by the tool, read
as an absence in the world, reproduced one line under this very fix.
`test_a_saturated_notes_budget_still_shows_every_ambiguity_note` pins that.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

import supertool

_TWO_OPS = {
    "ops": {
        "gh-pr": {
            "safety": "read-only",
            "cmd": "true",
            "syntax": "gh-pr:NUMBER[:status|:diff]",
            "description": "Review a pull request: checks, reviews, diff stat.",
            "replaces": [
                {"argv": "gh pr view", "use": "gh-pr:NUMBER"},
            ],
        },
    }
}


@pytest.fixture
def guard_config(monkeypatch: pytest.MonkeyPatch):
    """A config carrying `replaces` entries, loaded through the real loader."""

    def _load(tmp_path: Path, config: Dict[str, Any]) -> Dict[str, Any]:
        (tmp_path / ".supertool.json").write_text(
            json.dumps(config), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(supertool, "_CONFIG", None)
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
        monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
        return config

    return _load


#: A minimal #1450 shape -- an `unless_flag` entry whose exclusion sits in a
#: VALUE slot -- with SHORT descriptions on purpose. The real shipped
#: `git-commit`/`gh-pr-create` entries carry long descriptions that are
#: themselves truncated by `_GUARD_DESC_CAP` and eat most of
#: `_GUARD_TEXT_BUDGET` before a single note is rendered, which would make a
#: budget-cap test about THIS fix indistinguishable from the pre-existing,
#: unrelated total-length truncation `_GUARD_TEXT_BUDGET` already applies
#: everywhere (#1449). Short descriptions isolate the count-based defect this
#: fix repairs from that length-based one, which this fix does not touch.
_TOY_AMBIGUOUS = {
    "ops": {
        "toy-a": {
            "safety": "writes", "cmd": "true", "syntax": "toy-a",
            "description": "toy op a",
            "replaces": [{"argv": "toy cmd", "unless_flag": "--dry-run"}],
        },
        "toy-b": {
            "safety": "writes", "cmd": "true", "syntax": "toy-b",
            "description": "toy op b",
            "replaces": [{"argv": "toy run"}],
        },
    }
}


def test_an_earlier_write_before_the_blocked_command_is_named(
        tmp_path, guard_config):
    """The reported case: a commit that will not run because a later
    `gh pr view` in the same call is refused."""
    guard_config(tmp_path, _TWO_OPS)
    cmd = 'git commit --allow-empty -m "wip" && gh pr view 12'
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    joined = " ".join(verdict.discarded)
    assert "git commit" in joined, verdict.discarded
    refusal = supertool.guard_refusal(verdict)
    assert "1 earlier command" in refusal
    assert "git commit" in refusal
    assert "will NOT run" in refusal


def test_the_blocked_command_being_first_names_nothing_extra(
        tmp_path, guard_config):
    """Positive control: nothing precedes the blocked command, so there is
    nothing to discard and no discard note is manufactured."""
    guard_config(tmp_path, _TWO_OPS)
    cmd = 'gh pr view 12 && echo done'
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    assert verdict.discarded == (), verdict.discarded
    assert "earlier command" not in supertool.guard_refusal(verdict)


def test_multiple_earlier_commands_are_all_named_and_counted(
        tmp_path, guard_config):
    guard_config(tmp_path, _TWO_OPS)
    cmd = 'echo one; echo two; gh pr view 12'
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    assert verdict.discarded == ("echo one", "echo two"), verdict.discarded
    refusal = supertool.guard_refusal(verdict)
    assert "2 earlier commands" in refusal, refusal
    assert "echo one" in refusal, refusal
    assert "echo two" in refusal, refusal


def test_a_saturated_notes_budget_still_shows_every_ambiguity_note(
        tmp_path, guard_config):
    """The regression a reviewer caught in the first cut of this fix.

    Three `#1450` value-slot ambiguities exactly fill `_GUARD_MAX_NOTES` (3),
    the same as they did before #1873's discard field existed -- confirmed
    against the pre-fix revision of this file, where all three render with
    none hidden. The FIRST cut of this fix folded a discard note directly
    into that same capped `notes` list, taking a fourth slot and silently
    replacing the third ambiguity note with a generic "N further note(s) not
    shown" line -- an absence produced by the guard's OWN new disclosure,
    about a DIFFERENT and unrelated disclosure, reproduced by constructing
    the old (buggy) verdict by hand: `verdict._replace(notes=(discard,) +
    verdict.notes)` then rendering it hides the third note, where the fixed
    `verdict.discarded` field (asserted below) does not.
    """
    guard_config(tmp_path, _TOY_AMBIGUOUS)
    cmd = ("toy cmd -t --dry-run a && toy cmd -t --dry-run b && "
           "toy cmd -t --dry-run c && toy run")
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    assert len(verdict.discarded) == 3, verdict.discarded
    assert len(verdict.notes) == 3, verdict.notes
    refusal = supertool.guard_refusal(verdict)
    assert refusal.count("sits after another flag") == 3, refusal
    assert "further note(s)" not in refusal, refusal
    assert "3 earlier commands" in refusal, refusal
