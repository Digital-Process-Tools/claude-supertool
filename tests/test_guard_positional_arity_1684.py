"""A refusal must never prescribe a remedy the blocked command declined (#1684).

`git push origin v0.2.0` was BLOCKED and told to run `git-push`, which pushes
the **current branch**. A tag and a branch are different refs: obeying the
refusal pushes something the caller never named and the tag still does not
exist, while the command reports success. Class `misdirects` — the prescribed
command succeeds, quietly doing something else.

The discrimination that was called impossible is not the one needed.
`docs/contributing.md` said telling `git push origin master` from
`git push origin v0.34.0` apart "means asking the repository whether a ref is a
tag". It does not: **both** name an explicit refspec and `git-push` names none,
so the arity of the invocation decides it without asking git anything.

`unless_args: N` un-claims an entry carrying more than N positional arguments
past its declared argv. Un-claiming alone would be silence — the same absence
this repo keeps filing — so the verdict is a fourth state, `uncovered`: the
command runs, and the guard says *no op covers this form* rather than naming
one that does something else.

The second reported instance, `git checkout REF -- PATH` prescribed
`git-checkout:REF` (which switches branches), **does not reproduce against the
shipped registry**: no `replaces` entry for `git checkout` exists, in this tree
or anywhere in its history. It is pinned here against an injected entry that
reproduces the reported refusal exactly, so the mechanism is tested even though
the shipped mapping that would trigger it was never written.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

import _guard_wire

import supertool

_ROOT = Path(__file__).resolve().parent.parent
_GIT_OPS = json.loads(
    (_ROOT / "presets" / "git.json").read_text(encoding="utf-8"))["ops"]


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
    """The real git preset as the effective registry."""
    _load(tmp_path, monkeypatch, {"ops": _GIT_OPS})
    return tmp_path


def _notes(verdict) -> str:
    return " | ".join(tuple(verdict.uncovered) + tuple(verdict.notes))


# --------------------------------------------------------------------------
# 1. `git push origin <ref>` — the reported instance
# --------------------------------------------------------------------------

def test_pushing_a_tag_by_name_is_uncovered_not_blocked(shipped_git):
    verdict = supertool.guard_command("git push origin v0.2.0")
    assert verdict.state == "uncovered", verdict
    assert verdict.matches == ()


def test_the_disclosure_names_the_op_it_is_not_prescribing(shipped_git):
    note = _notes(supertool.guard_command("git push origin v0.2.0"))
    assert "no op covers this form" in note, note
    assert "git-push" in note, note
    assert "v0.2.0" in note, note
    # The failure mode being removed: a remedy the caller could paste.
    assert "Use: supertool" not in note, note


def test_it_does_not_read_as_a_guard_that_could_not_answer(shipped_git):
    """`undecided` is "I could not look". Here the guard looked and knows."""
    verdict = supertool.guard_command("git push origin v0.2.0")
    assert verdict.state != "undecided"
    assert "did not run" not in _notes(verdict)


def test_a_branch_named_positionally_is_the_same_shape(shipped_git):
    """`git push origin master` is not what `git-push` does either.

    It pushes `master` whatever branch you are on; `git-push` pushes the
    branch you are on. Blocking it prescribed the same misdirection with a
    value that happens not to be a tag.
    """
    verdict = supertool.guard_command("git push origin master")
    assert verdict.state == "uncovered", verdict


@pytest.mark.parametrize("command,use", [
    ("git push", "git-push"),
    ("git push origin", "git-push"),
    ("git push --force-with-lease", "git-push:force-with-lease"),
    ("git push -u origin", "git-push:set-upstream"),
])
def test_the_invocations_the_op_really_does_replace_still_block(
        shipped_git, command, use):
    """Coverage is narrowed by one dimension, not withdrawn."""
    verdict = supertool.guard_command(command)
    assert verdict.state == "blocked", (command, verdict)
    assert [m.use for m in verdict.matches] == [use], command


def test_a_flag_value_spends_the_positional_budget(shipped_git):
    """The cost of having no per-flag arity, measured rather than assumed.

    `-o ci.skip` is one flag and its value; the guard cannot tell the value
    from a positional, so it spends the one slot. `git push -o ci.skip` is
    still blocked, and `git push -o ci.skip origin` — a remote and no refspec,
    which `git-push` does perform — is un-claimed. A missed block, which is
    the direction this matcher is allowed to be wrong in.
    """
    assert supertool.guard_command("git push -o ci.skip").state == "blocked"
    verdict = supertool.guard_command("git push -o ci.skip origin")
    assert verdict.state == "uncovered", verdict


# --------------------------------------------------------------------------
# 2. `git checkout REF -- PATH` — against an injected entry, see the docstring
# --------------------------------------------------------------------------

_CHECKOUT = {
    "git-checkout": {
        "cmd": "true",
        "description": "Switch to REF",
        "syntax": "git-checkout:REF",
        "replaces": [
            {"argv": "git checkout", "unless_args": 1, "use": "git-checkout:REF"}
        ],
    }
}


@pytest.fixture
def checkout_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    _load(tmp_path, monkeypatch, {"ops": _CHECKOUT})
    return tmp_path


def test_a_pathspec_restore_is_not_a_branch_switch(checkout_registry):
    verdict = supertool.guard_command(
        "git checkout origin/main -- CHANGELOG.md changelog.d/")
    assert verdict.state == "uncovered", verdict
    assert verdict.matches == ()
    assert "no op covers this form" in _notes(verdict)


def test_a_bare_pathspec_restore_is_uncovered_too(checkout_registry):
    verdict = supertool.guard_command("git checkout -- CHANGELOG.md")
    assert verdict.state == "uncovered", verdict


def test_switching_to_a_ref_still_blocks(checkout_registry):
    verdict = supertool.guard_command("git checkout master")
    assert verdict.state == "blocked", verdict
    assert [m.use for m in verdict.matches] == ["git-checkout:REF"]


# --------------------------------------------------------------------------
# 3. The echoed command was not the command that was run
# --------------------------------------------------------------------------

def test_a_redirection_fd_is_not_an_argument(shipped_git):
    """`git push origin v0.2.0 2` — the `2` was the head of a `2>&1` (#1684).

    `shlex` splits `2>&1` into `2`, `>&`, `1`; the operator ends the segment
    and left its file descriptor on the end of the command. A refusal quoting
    a command nobody typed is the guard's own output lying about its input.
    """
    segments, _unread = supertool._guard_segments("git status 2>&1")
    assert [s for s in segments if s] == [["git", "status"]], segments


def test_a_redirection_target_is_not_a_command(shipped_git):
    """`> gh` left `gh` standing where a command word is read."""
    segments, _unread = supertool._guard_segments("git status > gh")
    assert [s for s in segments if s] == [["git", "status"]], segments


def test_a_redirection_does_not_split_the_command_it_sits_in(shipped_git):
    """Found in review: dropping the target while still splitting is worse.

    `gh pr view 1 > f --json state` runs `gh pr view 1 --json state`. Split at
    the operator, the words after the target become a segment of their own and
    the guard scores a command nobody typed — a WRONG block, and those have no
    per-command escape.
    """
    segments, _unread = supertool._guard_segments(
        "gh pr view 1 > f gh issue list")
    assert [s for s in segments if s] == [
        ["gh", "pr", "view", "1", "gh", "issue", "list"]], segments
    for command in ("git status > f && git push origin",
                    "cat < in.txt | git push origin"):
        segments, _unread = supertool._guard_segments(command)
        assert len([s for s in segments if s]) == 2, (command, segments)


def test_an_arity_decline_survives_a_refusal_in_the_same_command(shipped_git):
    """Found in review: `uncovered` was dropped when another segment blocked.

    Nothing runs either way, so the refusal is the right verdict — but the
    push was the half the caller most needed a sentence about, and it got
    none.
    """
    verdict = supertool.guard_command("git status && git push origin v1.0")
    assert verdict.state == "blocked", verdict
    assert any("no op covers this form" in note for note in verdict.notes), (
        verdict)
    text = supertool.guard_refusal(verdict)
    assert "no op covers this form" in text, text


def test_the_refusal_quotes_what_was_typed(shipped_git):
    verdict = supertool.guard_command("git status 2>&1")
    assert verdict.state == "blocked", verdict
    assert [m.command for m in verdict.matches] == ["git status"]


def test_a_positional_that_is_really_a_positional_survives(shipped_git):
    """Only a descriptor immediately before a redirection is dropped."""
    segments, _unread = supertool._guard_segments("git push origin 2")
    assert segments == [["git", "push", "origin", "2"]], segments


# --------------------------------------------------------------------------
# 4. End to end, through the hook the plugin installs
# --------------------------------------------------------------------------

def _run_hook(command: str, cwd: Path) -> Dict[str, Any]:
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": command}})
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(_ROOT)
    proc = subprocess.run(
        [sys.executable, str(_ROOT / "hooks" / "pre_bash_guard.py")],
        input=payload, capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=str(cwd), env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return _guard_wire.envelope(proc.stdout)


def test_the_hook_allows_the_tag_push_and_says_why(tmp_path):
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"presets": ["git"]}), encoding="utf-8")
    out = _run_hook("git push origin v0.2.0", tmp_path)["hookSpecificOutput"]
    assert "permissionDecision" not in out, out
    context = out.get("additionalContext", "")
    assert "no op covers this form" in context, context
    assert "did not run" not in context, context
