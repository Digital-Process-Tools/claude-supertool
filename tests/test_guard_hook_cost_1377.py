"""The PreToolUse guard paid its whole cost on every Bash call (#1377).

Measured in this checkout, macOS, median of 15 runs of `hooks/pre-bash-guard.sh`
on `ls -la /tmp` — a command the registry has nothing to say about:

    full wrapper                       301.1 ms
    python3.13 -c pass                  51.8 ms   (interpreter startup floor)
    import _supertool                  193.8 ms   (startup + 142 ms of import)
    import + _load_config              188.7 ms
    import + guard_command             190.5 ms

The issue named three costs and ranked them wrongly. In-process, after the
import: `_load_config` 2.2 ms, `_guard_replacements` 0.1 ms, `guard_command`
0.1 ms. **The registry walk is not a cost at all** — three orders of magnitude
below the term it was listed beside — and the interpreter probe is 52 ms of
301, not "the most expensive of the three". The import is.

So two things are pinned here, and neither of them is a cache.

* **One interpreter spawn, not two.** The ladder used to prove a candidate was
  a Python 3 with a throwaway `-c` run and then spawn it again for the real
  answer. The envelope the real run writes proves the same thing and more, so
  the probe run is gone and the wrapper reads the first bytes of the answer
  instead. #1402's protection against a launcher that writes a preamble
  survives as a prefix test rather than an equality test, and #1390's "ran and
  said nothing" stays reachable: no envelope, no verdict.

* **`_supertool` is not imported when the command names nothing the registry
  replaces.** The word list comes from the shipped presets, read with a flat
  JSON scan that can only over-collect; a project config that declares any
  `replaces` of its own turns the fast path off wholesale rather than being
  parsed. What that costs is disclosure notes on commands that name nothing
  replaced, which is the narrowing #1413 already made for the same reason.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _guard_wire

from test_guard_interpreter_ladder_1390 import _BASH, needs_wrapper

_ROOT = Path(__file__).resolve().parent.parent
_HOOKS = _ROOT / "hooks"
_WRAPPER = _HOOKS / "pre-bash-guard.sh"

sys.path.insert(0, str(_HOOKS))
import pre_bash_guard  # noqa: E402


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project that enables the shipped presets and declares nothing itself.

    Deliberately not the inline-`replaces` fixture the other guard tests use:
    a project config that declares its own mappings turns the fast path off by
    design, so it could not show whether the fast path exists.
    """
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"presets": ["git", "github"]}), encoding="utf-8")
    return tmp_path


def _hook_in_a_fresh_interpreter(command: str, cwd: Path, home: Path = None):
    """Run `main()` and report whether `_supertool` reached `sys.modules`.

    A subprocess rather than a call, because the question is about an import
    and this test process has already imported the module a dozen times over.
    """
    code = (
        "import sys" + chr(10)
        + "sys.path.insert(0, " + repr(str(_HOOKS)) + ")" + chr(10)
        + "import pre_bash_guard" + chr(10)
        + "pre_bash_guard.main()" + chr(10)
        + "sys.stderr.write('IMPORTED' if '_supertool' in sys.modules"
          " else 'ABSENT')" + chr(10))
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(_ROOT)
    if home is not None:
        # Both spellings: `os.path.expanduser` reads USERPROFILE first on
        # Windows and HOME on POSIX, and `_find_preset_file` calls it.
        env["HOME"] = env["USERPROFILE"] = str(home)
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": command}})
    return subprocess.run([sys.executable, "-c", code], input=payload,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(cwd), env=env, timeout=120)


def test_a_replaced_command_still_imports_and_still_denies(project):
    """The control. Without it the row below is satisfied by a broken hook."""
    proc = _hook_in_a_fresh_interpreter("git push", project)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.endswith("IMPORTED"), proc.stderr[-400:]
    hook = _guard_wire.envelope(proc.stdout)["hookSpecificOutput"]
    assert hook.get("permissionDecision") == "deny", proc.stdout


def test_a_command_naming_nothing_replaced_never_imports_supertool(project):
    proc = _hook_in_a_fresh_interpreter("ls -la /tmp", project)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.endswith("ABSENT"), (
        "the 142 ms import was paid for a command the registry cannot "
        "match: " + proc.stderr[-400:])
    # Byte-identical to what the slow path writes on a clean command, so the
    # saving is invisible to the caller rather than a second dialect.
    assert _guard_wire.envelope(proc.stdout) == {
        "hookSpecificOutput": {"hookEventName": "PreToolUse"}}, proc.stdout


def test_a_project_that_declares_replaces_turns_the_fast_path_off(tmp_path):
    """Not parsed, not merged, not cached — the whole fast path is off.

    `.supertool.json` changes under the user's hands between two Bash calls,
    and re-deriving which words a project override adds is `_op_registry`'s
    job (#1356 says so in as many words). The cheap correct check is to notice
    that the file has an opinion and let the real one answer.
    """
    (tmp_path / ".supertool.json").write_text(json.dumps({"ops": {"x": {
        "cmd": "true", "safety": "read-only", "syntax": "x",
        "description": "d",
        "replaces": [{"argv": "hexdump", "use": "x"}]}}}), encoding="utf-8")
    proc = _hook_in_a_fresh_interpreter("hexdump -C /etc/hosts", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.endswith("IMPORTED"), proc.stderr[-400:]
    hook = _guard_wire.envelope(proc.stdout)["hookSpecificOutput"]
    assert hook.get("permissionDecision") == "deny", proc.stdout


def test_a_user_level_preset_is_not_invisible_to_the_screen(tmp_path):
    """`_find_preset_file` searches three directories, and one was missed.

    Project, then `~/.config/supertool/presets/`, then the plugin's own. A
    project that enables a user-level preset declaring `replaces` has no
    literal `"replaces"` anywhere in its own tree, so the project screen sees
    nothing to bail on — and the word list, read from the shipped directory
    alone, does not carry the word either. The command then took the fast path
    and ran unguarded, which is the one direction this screen may not be wrong
    in. Found by review, not by the design.
    """
    home = tmp_path / "home"
    presets = home / ".config" / "supertool" / "presets"
    presets.mkdir(parents=True)
    (presets / "mine.json").write_text(json.dumps({"ops": {"hd": {
        "cmd": "true", "safety": "read-only", "syntax": "hd",
        "description": "d",
        "replaces": [{"argv": "hexdump", "use": "hd"}]}}}), encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".supertool.json").write_text(
        json.dumps({"presets": ["mine"]}), encoding="utf-8")

    proc = _hook_in_a_fresh_interpreter("hexdump -C /etc/hosts", project, home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.endswith("IMPORTED"), proc.stderr[-400:]
    hook = _guard_wire.envelope(proc.stdout)["hookSpecificOutput"]
    assert hook.get("permissionDecision") == "deny", proc.stdout


def test_a_preset_file_that_is_not_an_object_disables_the_fast_path(tmp_path):
    """Unexpected shape means "did not look", at every level of the file."""
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "odd.json").write_text("[]", encoding="utf-8")
    assert pre_bash_guard._replaced_words(str(tmp_path)) is None


def test_the_plugins_own_checkout_still_takes_the_fast_path():
    """The one host the 301 ms was measured on, and it nearly missed it.

    In this repository the project root and the plugin root are the same
    directory, so `{project}/presets/git.json` — which declares `replaces`, as
    every shipped preset does — read as a project opinion and turned the fast
    path off everywhere the saving was measured.
    """
    assert not pre_bash_guard._project_declares_replaces(str(_ROOT),
                                                         str(_ROOT))
    proc = _hook_in_a_fresh_interpreter("ls -la /tmp", _ROOT)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.endswith("ABSENT"), proc.stderr[-400:]


# --------------------------------------------------------------------------
# The screen itself. A necessary condition, so every row asks whether it can
# wrongly answer "nothing here" — the direction where a missed block is silent.
# --------------------------------------------------------------------------

_WORDS = frozenset({"gh", "git"})

_DOLLAR_SUB = "bash -c " + chr(34) + "$(printf gh) pr view" + chr(34)


@pytest.mark.parametrize("command", [
    "git push",
    "GIT push",                      # `_guard_command_word` folds case
    "/opt/homebrew/bin/gh pr view",  # a path still contains the word
    "gh.exe pr view",
    "true && git push",
    "sudo git push",
    "echo hi; gh pr view 1",
    "$GH_BIN pr view",               # expansion could produce one
    chr(96) + "which gh" + chr(96) + " pr view",
    "eval $CMD",
    _DOLLAR_SUB,
])
def test_the_screen_never_waves_through_a_command_that_might_match(command):
    assert pre_bash_guard._may_be_replaced(command, _WORDS), command


@pytest.mark.parametrize("command", [
    "ls -la /tmp",
    "pytest tests/test_x.py -q",
    "cat github.md",        # a longer word is not the word
    "cat gh.log",           # only an executable suffix may follow
    "python3 -m pip list",
    "rg --files",
])
def test_the_screen_waves_through_what_it_should(command):
    assert not pre_bash_guard._may_be_replaced(command, _WORDS), command


def test_an_unreadable_preset_directory_disables_the_fast_path(tmp_path):
    """A word list that could not be built is never read as an empty one.

    The house defect, in the one place it would be silent: `None` here means
    "did not look", and the caller must import and ask the real guard.
    """
    assert pre_bash_guard._replaced_words(str(tmp_path)) is None
    (tmp_path / "presets").mkdir()
    (tmp_path / "presets" / "broken.json").write_text("{", encoding="utf-8")
    assert pre_bash_guard._replaced_words(str(tmp_path)) is None


def test_the_shipped_word_list_covers_every_word_the_registry_declares():
    """The two matchers, compared — the containment #1377 asks about.

    `_replaced_words` reads `presets/*.json` flat, where `guard_command_words`
    goes through `_op_registry` and its override semantics. Over-collecting is
    free; under-collecting is a command that runs unguarded, so the assertion
    is one-directional and the fast path is only ever allowed to be slow.
    """
    sys.path.insert(0, str(_ROOT))
    import _supertool
    words = pre_bash_guard._replaced_words(str(_ROOT))
    assert words is not None
    for preset in ("git", "github", "gitlab"):
        # `_merge_presets` by hand: `guard_command_words` reads a config that
        # has already been through it, and a bare {"presets": [...]} declares
        # nothing at all — a control that would pass against any word list.
        config = {"presets": [preset]}
        _supertool._merge_presets(config, str(_ROOT))
        declared = _supertool.guard_command_words(config)
        assert declared, preset
        assert set(declared) <= words, (preset, sorted(declared), sorted(words))


# --------------------------------------------------------------------------
# One spawn, not two.
# --------------------------------------------------------------------------

@pytest.mark.skipif(
    os.name != "posix",
    reason="the shim is a shebang script on a PATH written with POSIX "
           "literals; under Git Bash it would not be found by `command -v` "
           "and the row would assert against the decline path instead")
@needs_wrapper
def test_the_wrapper_execs_one_interpreter_per_call(tmp_path, project):
    """The probe run and the real run were two spawns of the same binary.

    ~52 ms of the 301, on every Bash call, to learn something the answer
    itself carries. Counted with a shim on PATH rather than timed, so the row
    says what happened rather than how fast the host is.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "spawns.log"
    shim = bindir / "python3.14"
    shim.write_text(
        "#!/bin/bash" + chr(10)
        + "printf x >> " + str(log) + chr(10)
        + "exec " + sys.executable + " " + chr(34) + "$@" + chr(34) + chr(10),
        encoding="utf-8")
    shim.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + "/usr/bin" + os.pathsep + "/bin"
    env["CLAUDE_PLUGIN_ROOT"] = str(_ROOT)
    env.pop("VIRTUAL_ENV", None)
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": "git push"}})
    assert _BASH is not None
    proc = subprocess.run([_BASH, str(_WRAPPER)], input=payload,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(project), env=env,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr
    hook = json.loads(proc.stdout)["hookSpecificOutput"]
    assert hook.get("permissionDecision") == "deny", proc.stdout
    spawns = log.read_text(encoding="utf-8").count("x")
    assert spawns == 1, (
        "the wrapper spawned an interpreter " + str(spawns) + " times for one "
        "verdict")
