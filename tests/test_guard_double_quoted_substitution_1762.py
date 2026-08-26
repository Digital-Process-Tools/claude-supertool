"""A double-quoted command substitution is now read, not just disclosed (#1762).

The bare form `$(gh run view 123)` was always tokenised and checked. The
double-quoted form `"$(gh run view 123)"` -- the spelling shellcheck requires,
because an unquoted `$(cmd)` in a test word-splits -- took the `undecided`
arm instead: the guard said plainly that it had not read the command, but a
substitution wrapped in the idiom careful authors are told to write was the
one shape that never actually got checked.

The fix recovers the substitution's *interior* when the enclosing double
quotes are the only obstacle: `_guard_find_substitution_end` finds the
matching `)` by tracking nested `()`/quote depth, and the recovered text is
handed back through `_guard_open_substitutions` itself (nested command
substitutions, embedded quotes, all of it) as an additional segment. What it
still cannot balance -- an unterminated substitution, one hidden behind a
second layer this matcher does not model -- keeps the `undecided` arm exactly
as before, and every row below pairs a must-fire case with a must-stay-quiet
control so the fix cannot silently swallow the whole class into `clean`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

import supertool

NL = chr(10)
DQ = chr(34)


@pytest.fixture
def guard_config(monkeypatch: pytest.MonkeyPatch):
    def _load(tmp_path: Path, config: Dict[str, Any]) -> Dict[str, Any]:
        (tmp_path / ".supertool.json").write_text(
            json.dumps(config), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(supertool, "_CONFIG", None)
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
        monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
        return supertool._load_config()

    return _load


_OPS = {
    "ops": {
        "gh-run": {
            "safety": "read-only",
            "cmd": "true",
            "syntax": "gh-run:ID",
            "description": "Check a workflow run.",
            "replaces": [{"argv": "gh run view", "use": "gh-run:ID"}],
        },
    }
}


# Every one of these runs `gh run view 123` as far as the shell is concerned,
# each wrapped the way the issue reports it actually arriving.
BLOCKED_INSIDE_DOUBLE_QUOTES = [
    ("the exact issue shape: assigned to a variable",
     "x=" + DQ + "$(gh run view 123)" + DQ),
    ("the exact issue shape: a bare double-quoted word",
     DQ + "$(gh run view 123 --json status --jq .status)" + DQ),
    ("inside a test, the idiom shellcheck requires",
     "until [ " + DQ + "$(gh run view 123 --json status --jq .status)" + DQ
     + " = " + DQ + "completed" + DQ + " ]; do sleep 30; done"),
    ("as a flag value alongside other text",
     "note --detail " + DQ + "state: $(gh run view 123)" + DQ),
    ("nested inside another double-quoted substitution",
     "x=" + DQ + "$(echo " + DQ + "$(gh run view 123)" + DQ + ")" + DQ),
]


@pytest.mark.parametrize(
    "label,command", BLOCKED_INSIDE_DOUBLE_QUOTES,
    ids=[r[0] for r in BLOCKED_INSIDE_DOUBLE_QUOTES])
def test_a_double_quoted_substitution_is_now_blocked(
        label, command, tmp_path, guard_config):
    guard_config(tmp_path, _OPS)
    verdict = supertool.guard_command(command)
    assert verdict.state == "blocked", (label, command, verdict)


def test_the_unquoted_form_is_the_positive_control(tmp_path, guard_config):
    """The shape that already worked. The fix must not break it."""
    guard_config(tmp_path, _OPS)
    verdict = supertool.guard_command("x=$(gh run view 123)")
    assert verdict.state == "blocked", verdict


# What the fix does NOT claim to read: a substitution this matcher genuinely
# cannot balance, or one hidden behind a second layer of indirection it does
# not model. These must stay `undecided`, never silently `clean`.
STILL_UNDECIDED = [
    ("an unterminated substitution -- no matching close paren",
     "x=" + DQ + "$(gh run view 123" + DQ),
    ("a substitution behind a computed binary name",
     "x=" + DQ + "$($GH_BIN run view 123)" + DQ),
]


@pytest.mark.parametrize(
    "label,command", STILL_UNDECIDED, ids=[r[0] for r in STILL_UNDECIDED])
def test_what_the_matcher_still_cannot_balance_stays_undecided(
        label, command, tmp_path, guard_config):
    guard_config(tmp_path, _OPS)
    verdict = supertool.guard_command(command)
    assert verdict.state == "undecided", (label, command, verdict)
    assert verdict.notes, (label, command)


# The harmless control from the issue's own second comment: a double-quoted
# substitution wrapping a command no op supersedes must stay clean, not turn
# every quoted substitution into a disclosure nobody reads.
def test_a_harmless_double_quoted_substitution_stays_clean(
        tmp_path, guard_config):
    guard_config(tmp_path, _OPS)
    verdict = supertool.guard_command(
        "note --detail " + DQ + "$(cat detail.json)" + DQ)
    assert verdict.state == "clean", verdict


def test_the_disclosure_still_names_the_command_when_unreadable(
        tmp_path, guard_config):
    """Pinned against #1762's own ask: say which binary it declined to read."""
    guard_config(tmp_path, _OPS)
    verdict = supertool.guard_command(
        "x=" + DQ + "$($GH_BIN run view 123)" + DQ)
    assert verdict.state == "undecided", verdict
    assert verdict.notes, verdict
