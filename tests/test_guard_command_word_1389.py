"""Six shapes hid a raw command from the guard, all one defect (#1389).

Each row below is the matcher disagreeing with the shell about **where a
command starts**. Every one returned `clean` against `977a34b`, so the hook
stayed silent and the command ran — a block that silently does not fire is
indistinguishable from a command that complied, which is the sentence #1347
opens with, now true of #1347's own implementation.

The table is the point of the file, not the individual rows. The next bypass
is the shape nobody listed, so a new one costs a line here and the fix has to
be structural enough that the line passes.

The bar for every row: the assertion is that the guard **blocks**. An
assertion that some string is absent from the output is how #403 shipped a
filter that did nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

import supertool

NL = chr(10)
BACKSLASH = chr(92)


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
        "gh-pr": {
            "safety": "read-only",
            "cmd": "true",
            "syntax": "gh-pr:NUMBER[:status]",
            "description": "Review a pull request.",
            "replaces": [{"argv": "gh pr view", "use": "gh-pr:NUMBER"}],
        },
        "gh-merge": {
            "safety": "read-only",
            "cmd": "true",
            "syntax": "gh-merge:NUMBER",
            "description": "Merge a pull request.",
            "replaces": [{"argv": "gh pr merge", "use": "gh-merge:NUMBER"}],
        },
    }
}


# Every one of these runs `gh pr view 1` or `gh pr merge 1` as far as the
# shell is concerned. The guard must say so.
HIDDEN = [
    ("a # comment is line-scoped, not command-scoped",
     "cd /tmp # go" + NL + "gh pr merge 1 --squash"),
    ("a # comment before the command hides only its own line",
     "ls # gh" + NL + "gh pr view 1"),
    ("env is a wrapper, the command word is behind it",
     "env gh pr view 1"),
    ("env with an option that takes a value",
     "env -u FOO gh pr view 1"),
    ("env with an assignment and an option",
     "env -i PATH=/bin gh pr view 1"),
    ("timeout takes a duration before the command word",
     "timeout 60 gh pr view 1"),
    ("timeout with its own flag and a duration",
     "timeout -k 5 60 gh pr view 1"),
    ("nice with a level",
     "nice -n 10 gh pr view 1"),
    ("sudo with an option that takes a value",
     "sudo -u somebody gh pr view 1"),
    ("an absolute path to the binary",
     "/opt/homebrew/bin/gh pr view 1"),
    # A Windows path only reaches the shell intact if it is quoted — bash
    # deletes an unquoted backslash, so the unquoted spelling runs
    # `C:toolsgh.exe` and a `clean` verdict on it is correct. Both quoted
    # forms and the forward-slash spelling are commands.
    ("a quoted Windows path to the binary",
     chr(34) + "C:" + BACKSLASH + "tools" + BACKSLASH + "gh.exe" + chr(34)
     + " pr view 1"),
    ("a single-quoted Windows path to the binary",
     chr(39) + "C:" + BACKSLASH + "tools" + BACKSLASH + "gh.exe" + chr(39)
     + " pr view 1"),
    ("a Windows path with forward slashes",
     "C:/tools/gh.exe pr view 1"),
    ("a brace group is a separator, not a command word",
     "{ gh pr view 1; }"),
    ("a subshell group",
     "( gh pr view 1 )"),
    ("a line continuation is deleted by the shell, not tokenised",
     "gh " + BACKSLASH + NL + " pr view 1"),
]


@pytest.mark.parametrize("label,command",
                         HIDDEN, ids=[r[0] for r in HIDDEN])
def test_the_guard_blocks_a_command_the_shell_would_run(
        label, command, tmp_path, guard_config):
    guard_config(tmp_path, _OPS)
    verdict = supertool.guard_command(command)
    assert verdict.state == "blocked", (label, command, verdict)


def test_the_plain_form_is_the_control(tmp_path, guard_config):
    """If this ever goes red the table above proves nothing."""
    guard_config(tmp_path, _OPS)
    assert supertool.guard_command("gh pr merge 1 --squash").state == "blocked"


# The other direction. Widening what counts as a command word buys bypass
# coverage with false positives, so the price is pinned too.
NOT_A_COMMAND = [
    ("a quoted comment is text",
     "echo " + chr(39) + "# gh pr view 1" + chr(39)),
    ("a fragment in a URL is not a comment",
     "curl https://example.com/x#gh-pr-view"),
    ("a bare argument list is not a wrapper",
     "echo gh pr view 1"),
    ("find's placeholder is not a command",
     "find . -name x -exec ls {} " + BACKSLASH + ";"),
]


@pytest.mark.parametrize("label,command",
                         NOT_A_COMMAND, ids=[r[0] for r in NOT_A_COMMAND])
def test_the_guard_stays_out_of_the_way(label, command, tmp_path,
                                        guard_config):
    guard_config(tmp_path, _OPS)
    verdict = supertool.guard_command(command)
    assert verdict.state in ("clean", "undecided"), (label, command, verdict)


def test_a_comment_does_not_swallow_a_later_line_end_to_end(
        tmp_path, guard_config):
    """The hook, not just the matcher: this is what the user actually meets."""
    guard_config(tmp_path, _OPS)
    verdict = supertool.guard_command(
        "cd /tmp # go" + NL + "gh pr merge 1 --squash")
    assert verdict.state == "blocked", verdict
    refusal = supertool.guard_refusal(verdict)
    assert "gh-merge" in refusal, refusal
