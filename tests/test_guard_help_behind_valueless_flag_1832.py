"""#1832 -- `_guard_help_state` scored `git commit -a -h` as `value` and the
guard refused it toward `git-commit`, an op that commits.

`-a` consumes no separate value, so `-h` behind it is unambiguously a help
flag. `_GUARD_VALUELESS_FLAGS` already carries that fact for the four
subcommands the shipped presets gate; `_guard_help_state` simply did not ask.

This turns a block into an allow, the opposite direction to #1815/#1816. The
sweep of what else it un-blocks: every one of the 101 `git <sub> <flag> -h`
spellings the four table rows generate was run against git 2.46.2 and every one
printed usage and did nothing (exit 129). The value-taking controls -- `-m`,
`-am`, `-c`, `--author`, and `git push origin -o -h main` -- do not, and stay
ambiguous.

The dispatch-level spelling (`guard:...`) is deliberately NOT used: under the
suite no project registry is loaded, so `guard:git commit -m -h` answers `OK`
and every "still blocked" assertion would pass on a guard that refuses nothing.
That was observed on the red run of this file. Each case wires a registry.

This file is deliberately not one of the `tests/test_guard_*.py` files being
converted elsewhere this tick; it is new.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

import supertool

_COMMIT_OP: Dict[str, Any] = {
    "ops": {
        "git-commit": {
            "safety": "write",
            "cmd": "true",
            "syntax": "git-commit:::MESSAGE",
            "description": "Commit MESSAGE (stages PATHS).",
            "replaces": [{"argv": "git commit", "use": "git-commit:::MESSAGE"}],
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
        return supertool._load_config()

    return _load


def test_help_behind_a_valueless_flag_is_not_blocked(tmp_path, guard_config) -> None:
    """The reported command. `git commit -a -h` prints usage and commits
    nothing; the guard refused it and named an op that commits."""
    guard_config(tmp_path, _COMMIT_OP)
    verdict = supertool.guard_command("git commit -a -h")
    assert verdict.state == "clean", verdict
    assert not verdict.matches, verdict


def test_help_behind_a_value_taking_flag_stays_blocked(tmp_path, guard_config) -> None:
    """The must-fire control. Without it the assertion above passes on a guard
    that has stopped refusing anything. `git commit -m -h` commits with the
    message `-h` -- measured on git 2.46.2, exit 1, not usage."""
    guard_config(tmp_path, _COMMIT_OP)
    verdict = supertool.guard_command("git commit -m -h")
    assert verdict.state == "blocked", verdict
    assert [m.op for m in verdict.matches] == ["git-commit"], verdict


def test_a_cluster_ending_in_a_value_taking_flag_stays_blocked(
        tmp_path, guard_config) -> None:
    """`-am` is `-a -m`, and `-m` takes the message. The arity answer has to be
    about the whole cluster, not its first letter."""
    guard_config(tmp_path, _COMMIT_OP)
    verdict = supertool.guard_command("git commit -am -h")
    assert verdict.state == "blocked", verdict


def test_a_valueless_cluster_is_read_as_valueless(tmp_path, guard_config) -> None:
    """`-as` is `-a -s`, both valueless -- measured: usage, exit 129."""
    guard_config(tmp_path, _COMMIT_OP)
    verdict = supertool.guard_command("git commit -as -h")
    assert verdict.state == "clean", verdict


def test_the_note_about_an_unreadable_help_flag_goes_with_the_block(
        tmp_path, guard_config) -> None:
    """The misdirect the issue names is the pair of them: a refusal toward a
    committing op AND a note saying the help flag was not read. Neither may
    survive for `-a`; both must for `-m`."""
    guard_config(tmp_path, _COMMIT_OP)
    allowed = supertool.guard_command("git commit -a -h")
    blocked = supertool.guard_command("git commit -m -h")
    assert not any("help flag" in n for n in allowed.notes), allowed
    assert any("help flag" in n for n in blocked.notes), blocked


def test_the_state_function_answers_directly() -> None:
    """Asserted on `_guard_help_state` as well as through the guard, so a
    failure names the classifier rather than the whole pipeline."""
    assert supertool._guard_help_state(["git", "commit", "-a", "-h"]) == "help"
    assert supertool._guard_help_state(["git", "commit", "-m", "-h"]) == "value"
    assert supertool._guard_help_state(["git", "commit", "-h"]) == "help"
    assert supertool._guard_help_state(["git", "commit", "-m", "x"]) == "none"


def test_every_flag_in_the_table_un_blocks_help_behind_it() -> None:
    """The sweep, as a test rather than a paragraph. Each row's own flags are
    read out of `_GUARD_VALUELESS_FLAGS`, so a flag added to the table later is
    covered without editing this file -- and a flag added there that does take
    a value fails here rather than shipping as a wrong allow."""
    for prefix, flags in supertool._GUARD_VALUELESS_FLAGS:
        for flag in sorted(flags):
            argv = list(prefix) + [flag, "-h"]
            assert supertool._guard_help_state(argv) == "help", argv


def test_a_flag_outside_the_table_stays_ambiguous() -> None:
    """`-o` on `git push` is `--push-option` and takes a value; it is not in the
    `git push` row. A flag that is not in the table must not be read as
    valueless -- the table is the evidence, not the flag shape."""
    assert supertool._guard_help_state(
        ["git", "push", "origin", "-o", "-h", "main"]) == "value"


def test_a_command_with_no_table_row_is_unchanged() -> None:
    """No row means no arity fact, so the ambiguous answer stands."""
    assert supertool._guard_help_state(
        ["gh", "issue", "list", "-L", "-h"]) == "value"


def test_an_attached_value_cluster_is_still_ambiguous() -> None:
    """Known residual, pinned so it is a decision rather than a surprise.
    `git commit -uall -h` prints usage (measured, exit 129) and stays blocked:
    `-uall` is `-u` with an attached value, and telling that from a letter
    cluster needs an arity fact the table does not carry. The miss is in the
    safe direction -- a wrong block, which is legible -- and widening the
    cluster rule to cover it would also swallow `-am`."""
    assert supertool._guard_help_state(
        ["git", "commit", "-uall", "-h"]) == "value"
