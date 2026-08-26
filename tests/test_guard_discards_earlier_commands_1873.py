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


def test_an_earlier_write_before_the_blocked_command_is_named(
        tmp_path, guard_config):
    """The reported case: a commit that will not run because a later
    `gh pr view` in the same call is refused."""
    guard_config(tmp_path, _TWO_OPS)
    cmd = 'git commit --allow-empty -m "wip" && gh pr view 12'
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    joined = " ".join(verdict.notes)
    assert "1 earlier command" in joined, verdict.notes
    assert "git commit" in joined, verdict.notes
    assert "will NOT run" in joined, verdict.notes
    refusal = supertool.guard_refusal(verdict)
    assert "1 earlier command" in refusal
    assert "git commit" in refusal


def test_the_blocked_command_being_first_names_nothing_extra(
        tmp_path, guard_config):
    """Positive control: nothing precedes the blocked command, so there is
    nothing to discard and no discard note is manufactured."""
    guard_config(tmp_path, _TWO_OPS)
    cmd = 'gh pr view 12 && echo done'
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    joined = " ".join(verdict.notes)
    assert "earlier command" not in joined, verdict.notes


def test_multiple_earlier_commands_are_all_named_and_counted(
        tmp_path, guard_config):
    guard_config(tmp_path, _TWO_OPS)
    cmd = 'echo one; echo two; gh pr view 12'
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    joined = " ".join(verdict.notes)
    assert "2 earlier commands" in joined, verdict.notes
    assert "echo one" in joined, verdict.notes
    assert "echo two" in joined, verdict.notes
