"""#1421 (folding #1413) — the guard's view of a command is narrower than
the command, and the gap rendered as OK.

Two instances of one class, and the class is the shared decision: what the
guard says when it cannot see a command. Before this, both rendered
`OK: nothing in this command is replaced by an op loaded here` — byte-identical
to a command that genuinely has no replacement.

**Instance 1 — global options hide the subcommand.** `replaces` argv is matched
from the first token, and git, gh and glab all take options *before* their
subcommand. `git -C P status` walked past the `git status` entry. Measured on
`042c6a9`, the gap is not git-only, which the issue listed as unestablished:
`gh --repo O/R pr view 1`, `gh -R O/R pr view 1` and `glab --repo O/R mr view 1`
were all clean too. So the normaliser is table-driven per command word rather
than git-shaped.

The `gh` half of that resolves differently from the `glab` half, and the
difference was **observed, not reasoned** (gh 2.x / glab 1.86.0 on this box):

    $ gh --repo cli/cli version      -> unknown flag: --repo
    $ glab --repo x/y version        -> glab 1.86.0 (1ef884a6)

`gh` defines `--repo` on its leaf commands, `glab` on its root. So there is no
`gh` command that both carries a pre-subcommand `--repo` and runs, and giving
`gh` an option table containing one would make the guard block an invocation
that cannot execute, naming an op for a call nobody makes. `gh`'s table
therefore carries only what gh's root really accepts, and `gh --repo O/R pr
view 1` is `undecided` — the honest answer for a spelling this matcher has no
grammar for.

**Instance 2 — the hook matched `Bash` only.** Wherever the PowerShell tool is
enabled Claude routes shell commands through it, and a hook that never runs is
indistinguishable at the call site from a hook that ran and approved.

**What is NOT done, and why.** The option table is never guessed. A
pre-subcommand token this matcher does not have a grammar for makes the verdict
`undecided`, never a normalised guess — because the wrong direction here is a
wrong *block*, whose only escape is `raw_command_guard: false` repo-wide. The
issue's own list of traps ("a normaliser that skips leading dashes indefinitely
will skip past the subcommand itself") is structurally impossible here: only
tokens present in the table are ever skipped.

**PowerShell is answered with UNDECIDED, not with a second tokeniser** — option
(2) of the three #1413 lists. And it is answered only when the command text
names a command word some entry declares, because this same file already
records the reason (`_guard_segments`, on `$` in an argument): a disclosure
printed under most commands anyone writes is one nobody reads.

**One claim in the brief is refuted here and the test says so.** The brief asked
that `git -C W push origin v1.2.3` must still run. It must not, and did not
before this change either: `presets/git.json` claims a bare `git push`, and
`guard_command("git push origin v1.2.3")` is BLOCKED on `042c6a9` —
`guard_command`'s own docstring states that outcome deliberately. Leaving it
clean under `-C` would make `-C` a documented bypass for the one op that can
destroy someone else's commits. The negative that does matter is `git -C W tag
v1`: `tag` is mapped by nothing, and a normaliser with a wrong option table is
exactly what would swallow it. Both are pinned below, the second as the
NEGATIVE and the first as a CONSISTENCY control against its own un-prefixed
spelling rather than against an absolute.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent


def _preset(name: str) -> Dict[str, Any]:
    return json.loads((_ROOT / "presets" / (name + ".json")).read_text(
        encoding="utf-8"))["ops"]


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
def shipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """git + gh + glab, the three families that ship `replaces` entries."""
    ops: Dict[str, Any] = {}
    for name in ("git", "github", "gitlab"):
        ops.update(_preset(name))
    _load(tmp_path, monkeypatch, {"ops": ops})
    return tmp_path


def _probe_op(**entry: Any) -> Dict[str, Any]:
    return {"ops": {"probe-op": {
        "safety": "read-only",
        "cmd": "true",
        "description": "a probe",
        "syntax": "probe-op:X",
        "replaces": [entry],
    }}}


# --------------------------------------------------------------------------
# Instance 1 — the option walk. PINS: every one of these was `clean`.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command,op", [
    ("git -C /tmp/x status", "git-status"),
    ("git -C/tmp/x status", "git-status"),
    ("git -c core.pager=cat status", "git-status"),
    ("git --git-dir=/tmp/x/.git status", "git-status"),
    ("git --git-dir /tmp/x/.git status", "git-status"),
    ("git --work-tree /tmp/x status", "git-status"),
    ("git --no-pager status", "git-status"),
    ("git -C /tmp/x -c core.pager=cat --no-pager status", "git-status"),
    ("git -C /tmp/x commit -m wip", "git-commit"),
    ("git -C /tmp/x worktree list", "git-worktrees"),
    ("glab --repo o/r mr view 1", "gl-mr"),
    ("glab --repo=o/r mr view 1", "gl-mr"),
    ("glab -R o/r issue view 1", "gl-issue"),
    ("glab -R o/r ci trace", "gl-job"),
])
def test_a_global_option_no_longer_hides_the_subcommand(shipped, command, op):
    verdict = supertool.guard_command(command)
    assert verdict.state == "blocked", (command, verdict)
    assert any(match.op == op for match in verdict.matches), (
        command, [match.op for match in verdict.matches])


def test_the_refusal_quotes_what_the_caller_typed_not_the_normalised_form(
        shipped):
    verdict = supertool.guard_command("git -C /tmp/x status")
    assert verdict.matches[0].command == "git -C /tmp/x status"


# --------------------------------------------------------------------------
# NEGATIVES — what a wrong option table would swallow
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "git -C /tmp/x tag v1",
    "git -C /tmp/x rebase --continue",
    "git -c user.name=x tag -a v1 -m x",
    "git --git-dir=/tmp/x/.git bisect start",
    "glab --repo o/r release create v1",
    "glab -R o/r ci lint",
])
def test_an_unmapped_subcommand_behind_a_global_option_still_runs(
        shipped, command):
    assert supertool.guard_command(command).state == "clean", command


def test_a_value_taking_option_consumes_a_word_that_looks_like_a_subcommand(
        shipped):
    """`-C status` is a DIRECTORY called `status`, not the subcommand."""
    assert supertool.guard_command("git -C status tag v1").state == "clean"


def test_the_walk_stops_at_a_double_dash(shipped):
    assert supertool.guard_command("git -- status").state == "clean"


def test_a_help_flag_still_unclaims_everything_behind_an_option(shipped):
    assert supertool.guard_command(
        "git -C /tmp/x status --help").state == "clean"
    assert supertool.guard_command("git -C /tmp/x --help").state == "clean"


def test_a_help_flag_also_suppresses_the_undecided_note(shipped):
    """The non-vacuous half of the line above.

    `-C` alone already prevents a match, so `git -C P status --help` is clean
    with or without the early return. What only the early return decides is an
    option the table does NOT know: without it, `git --zonk status --help`
    would be `undecided`, disclosing that the guard could not read a command
    which asks a program to describe itself and runs nothing.
    """
    verdict = supertool.guard_command("git --zonk status --help")
    assert verdict.state == "clean", verdict
    assert verdict.notes == (), verdict.notes


def test_the_ordinary_path_gains_no_note(shipped):
    """A disclosure under every command is one nobody reads."""
    for command in ("git status", "ls -la", "python3 -m pytest -q",
                    "git tag v1", "rg -n pattern ."):
        verdict = supertool.guard_command(command)
        assert verdict.notes == (), (command, verdict.notes)


def test_a_prefixed_push_agrees_with_its_own_unprefixed_spelling(shipped):
    """CONSISTENCY, not an absolute — see the module docstring.

    `git push origin v1.2.3` is BLOCKED on `042c6a9`, by design: no matcher can
    separate a tag push from a branch push on a positional's value. Under `-C`
    it must reach the same verdict, or `-C` is a bypass for `git push`.
    """
    bare = supertool.guard_command("git push origin v1.2.3")
    prefixed = supertool.guard_command("git -C /tmp/x push origin v1.2.3")
    assert bare.state == "blocked", "the premise of this test moved"
    assert prefixed.state == bare.state


# --------------------------------------------------------------------------
# The third state — reached AND rendered
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command,why", [
    ("git --zonk status", "an option no table here knows"),
    ("git --exec-path status", "bare `--exec-path` prints and exits; the "
                               "`=` form takes a value. Guessing either way "
                               "is a wrong block or a swallowed subcommand"),
    ("git -pC /tmp/x status", "a cluster the walk will not take apart"),
    ("git -C", "a value-taking option with no value left"),
    ("gh --hostname h pr view 1", "gh has options this table does not carry"),
    ("gh --repo o/r pr view 1", "gh rejects a pre-subcommand --repo outright, "
                                "so normalising it would block a call that "
                                "cannot run"),
    ("gh -R o/r pr view 1", "same, short spelling"),
])
def test_an_unreadable_option_is_undecided_and_never_ok(shipped, command, why):
    verdict = supertool.guard_command(command)
    assert verdict.state == "undecided", (command, why, verdict)
    rendered = supertool.op_guard(command)
    assert "UNDECIDED:" in rendered, (command, rendered)
    assert "OK: nothing in this command" not in rendered, (command, rendered)
    assert command.split()[1] in "".join(verdict.notes), verdict.notes


def test_the_undecided_note_names_the_command_word(shipped):
    verdict = supertool.guard_command("git --zonk status")
    assert "git" in "".join(verdict.notes)


def test_a_positive_match_elsewhere_still_wins_over_a_note(shipped):
    """A fact about one segment is not unmade by a doubt about another."""
    verdict = supertool.guard_command("git --zonk log && git status")
    assert verdict.state == "blocked"


# --------------------------------------------------------------------------
# It generalises past the three shipped families
# --------------------------------------------------------------------------

def test_a_project_mapping_gets_the_same_third_state(
        tmp_path, monkeypatch):
    _load(tmp_path, monkeypatch, _probe_op(argv="frob widget"))
    assert supertool.guard_command("frob widget").state == "blocked"
    verdict = supertool.guard_command("frob --wibble widget")
    assert verdict.state == "undecided", verdict
    assert "frob" in "".join(verdict.notes)


def test_a_command_word_no_entry_declares_is_left_alone(tmp_path, monkeypatch):
    _load(tmp_path, monkeypatch, _probe_op(argv="frob widget"))
    verdict = supertool.guard_command("dd --wibble if=/dev/zero of=/tmp/x")
    assert verdict.state == "clean", verdict


def test_a_single_token_entry_is_not_a_reason_to_walk(tmp_path, monkeypatch):
    """`{"argv": "frob"}` claims the command word itself; there is nothing
    behind the options to find, so an option must not make it undecided."""
    _load(tmp_path, monkeypatch, _probe_op(argv="frob"))
    assert supertool.guard_command("frob --wibble x").state == "blocked"


# --------------------------------------------------------------------------
# Instance 2 — the hook's matcher (#1413)
# --------------------------------------------------------------------------

def test_hooks_json_matches_powershell_as_well_as_bash():
    hooks = json.loads((_ROOT / "hooks" / "hooks.json").read_text(
        encoding="utf-8"))
    matchers = [entry.get("matcher")
                for entry in hooks["hooks"]["PreToolUse"]]
    assert any(m and "PowerShell" in m for m in matchers), matchers
    assert any(m and "Bash" in m for m in matchers), matchers


def _run_hook(command: str, cwd: Path, tool: str = "Bash") -> Dict[str, Any]:
    payload = json.dumps({"tool_name": tool,
                          "tool_input": {"command": command}})
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(_ROOT)
    proc = subprocess.run(
        [sys.executable, str(_ROOT / "hooks" / "pre_bash_guard.py")],
        input=payload, capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=str(cwd), env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def test_a_powershell_command_naming_a_replaced_binary_is_disclosed(tmp_path):
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": _preset("git")}), encoding="utf-8")
    hook = _run_hook("git status", tmp_path,
                     tool="PowerShell")["hookSpecificOutput"]
    assert hook["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hook, hook
    context = hook["additionalContext"]
    assert "PowerShell" in context, context
    assert "git" in context, context


def test_a_powershell_command_naming_nothing_mapped_stays_silent(tmp_path):
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": _preset("git")}), encoding="utf-8")
    hook = _run_hook("Get-ChildItem -Recurse", tmp_path,
                     tool="PowerShell")["hookSpecificOutput"]
    assert "additionalContext" not in hook, hook
    assert "permissionDecision" not in hook, hook


def test_powershell_is_never_denied_because_it_was_never_tokenised(tmp_path):
    """A POSIX tokeniser on PowerShell quoting produces false denies, and a
    wrong block has no per-command escape. UNDECIDED is the whole answer."""
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": _preset("git")}), encoding="utf-8")
    for command in ("git status", "git -C C:\\\\tmp\\\\x status",
                    "& 'git' status"):
        hook = _run_hook(command, tmp_path,
                         tool="PowerShell")["hookSpecificOutput"]
        assert hook.get("permissionDecision") is None, (command, hook)


@pytest.mark.parametrize("command,disclosed", [
    ("git status", True),
    ("git.exe status", True),
    ("GIT.EXE status", True),
    ("Get-Content gh.log", False),
    ("Get-Content git.py", False),
    ("Get-ChildItem -Filter *.github", False),
])
def test_the_disclosure_matches_a_binary_not_a_filename(
        tmp_path, command, disclosed):
    """A disclosure that fires on a filename is one nobody reads.

    The word may carry a Windows executable suffix -- the same two spellings
    `_guard_command_word` folds together -- and any other dot ends the match.
    """
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": _preset("git")}), encoding="utf-8")
    hook = _run_hook(command, tmp_path,
                     tool="PowerShell")["hookSpecificOutput"]
    assert ("additionalContext" in hook) is disclosed, (command, hook)


def test_the_disclosure_is_off_when_the_gate_is_off(tmp_path):
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": _preset("git"), "raw_command_guard": False}),
        encoding="utf-8")
    hook = _run_hook("git status", tmp_path,
                     tool="PowerShell")["hookSpecificOutput"]
    assert "additionalContext" not in hook, hook


def test_bash_is_unchanged_by_the_widened_matcher(tmp_path):
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": _preset("git")}), encoding="utf-8")
    hook = _run_hook("git status", tmp_path)["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny"


def test_a_tool_with_no_command_says_nothing(tmp_path):
    """The arm keys on a *command string*, not on a list of shell names.

    A tool carrying no command has nothing to disclose about. A tool carrying
    one that is not Bash does, whatever it is called — a fourth shell tool
    landing here must get the third state rather than the silence #1413 is
    about, and a hardcoded roster of shell names is how it would get silence.
    """
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": _preset("git")}), encoding="utf-8")
    payload = json.dumps({"tool_name": "Read",
                          "tool_input": {"file_path": "/tmp/git status"}})
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(_ROOT)
    proc = subprocess.run(
        [sys.executable, str(_ROOT / "hooks" / "pre_bash_guard.py")],
        input=payload, capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=str(tmp_path), env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    hook = json.loads(proc.stdout)["hookSpecificOutput"]
    assert "additionalContext" not in hook, hook
    assert "permissionDecision" not in hook, hook


def test_a_shell_tool_nobody_has_named_yet_still_gets_the_third_state(
        tmp_path):
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": _preset("git")}), encoding="utf-8")
    hook = _run_hook("git status", tmp_path,
                     tool="Nushell")["hookSpecificOutput"]
    assert "Nushell" in hook["additionalContext"]
    assert "permissionDecision" not in hook, hook


def test_the_command_word_list_is_a_public_answer(tmp_path, monkeypatch):
    """The PowerShell disclosure needs the set of declared command words, and
    it must come from the registry rather than a second hardcoded list."""
    _load(tmp_path, monkeypatch, _probe_op(argv="frob widget"))
    assert supertool.guard_command_words() == ("frob",)


def test_the_command_word_list_is_empty_when_the_gate_is_off(
        tmp_path, monkeypatch):
    config = _probe_op(argv="frob widget")
    config["raw_command_guard"] = False
    _load(tmp_path, monkeypatch, config)
    assert supertool.guard_command_words() == ()

# --------------------------------------------------------------------------
# Adjacent, found while adding a `FrozenSet` annotation: two module-level
# annotations named types the module never imported. `from __future__ import
# annotations` stores them as strings, so nothing raised at import and ruff's
# F821 was carried as a pre-existing error — but the module's own type hints
# could not be resolved by anything that asked for them.
# --------------------------------------------------------------------------

def test_the_modules_own_annotations_resolve():
    import typing

    import _supertool

    typing.get_type_hints(_supertool)
