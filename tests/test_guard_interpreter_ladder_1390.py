"""The hook's interpreter ladder was an off-switch and an exec primitive (#1390).

`hooks/pre-bash-guard.sh` prepended `$SUPERTOOL_PYTHON` to its ladder and
`exec`d the first candidate that survived `-c pass`. **Any binary that exits 0
satisfies that test**, so `SUPERTOOL_PYTHON=/usr/bin/true` turned the guard off
with rc 0, empty stdout and no disclosure — the exact environment-variable
hatch `guard_command`'s own docstring says was deliberately refused:

    The escape hatch is deliberately **not** an environment variable. An env
    var that turns a block off is learned once and then prepended forever,
    which is not a block.

Two separable properties are pinned here.

* **A candidate must be proved to be a Python 3**, not merely to exit 0, and
  `SUPERTOOL_PYTHON` does not participate at all. It exists for the tool's own
  spawns; a hook that decides whether a command may run is a different trust
  context, and inheriting the variable is what merged them.
* **"Could not run" is reachable when a candidate ran and produced nothing.**
  Before this the wrapper only disclosed when the ladder was *empty*, so a
  candidate that executed and said nothing rendered exactly like a command
  that complied. That is the three-state contract with a hole in it, inside
  the enforcement layer.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_WRAPPER = _ROOT / "hooks" / "pre-bash-guard.sh"

def _ladder_finds_an_interpreter() -> bool:
    """Does this host have anything the wrapper's ladder will select?

    The ladder is versioned names only (#572) plus an activated venv, and a
    Windows runner typically has `python.exe` and no `python3.13` — so the
    wrapper correctly declines there. Every assertion below is about what the
    wrapper does *once it has an interpreter*, and on a host where it has none
    they would all assert the decline path instead, which is a different test
    wearing this one's name. Skipped rather than allowed to go vacuous.
    """
    if shutil.which("bash") is None:
        return False
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": "ls -la"}})
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(_ROOT)
    try:
        proc = subprocess.run(
            ["bash", str(_WRAPPER)], input=payload, capture_output=True,
            text=True, encoding="utf-8", errors="replace", env=env,
            timeout=120)
    except OSError:
        return False
    return "did not run" not in proc.stdout


pytestmark = pytest.mark.skipif(
    not _ladder_finds_an_interpreter(),
    reason="no bash, or no interpreter the wrapper's ladder selects on this "
           "host — the wrapper declines, which is a different code path")

_OPS = {
    "ops": {
        "gh-pr": {
            "safety": "read-only",
            "cmd": "true",
            "syntax": "gh-pr:NUMBER",
            "description": "Review a pull request.",
            "replaces": [{"argv": "gh pr view", "use": "gh-pr:NUMBER"}],
        },
    }
}


def _run_wrapper(command: str, cwd: Path,
                 extra_env: Dict[str, str] | None = None,
                 plugin_root: str | None = None
                 ) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": command}})
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = plugin_root or str(_ROOT)
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(_WRAPPER)], input=payload, capture_output=True,
        text=True, encoding="utf-8", errors="replace", cwd=str(cwd),
        env=env, timeout=120)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".supertool.json").write_text(json.dumps(_OPS),
                                              encoding="utf-8")
    return tmp_path


def test_the_wrapper_denies_a_replaced_command(project):
    """The control. Without this the rows below prove nothing."""
    proc = _run_wrapper("gh pr view 12", project)
    assert proc.returncode == 0, proc.stderr
    hook = json.loads(proc.stdout)["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny", proc.stdout


@pytest.mark.parametrize("value", ["/usr/bin/true", "/bin/echo"])
def test_an_env_var_cannot_turn_the_guard_off(value, project):
    """`SUPERTOOL_PYTHON=/usr/bin/true` used to be rc 0, no stdout, no note."""
    if not os.path.exists(value):
        pytest.skip(value + " is not present on this host")
    proc = _run_wrapper("gh pr view 12", project,
                        {"SUPERTOOL_PYTHON": value})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), (
        "the guard produced nothing at all: silence from a gate is "
        "indistinguishable from a command that complied")
    hook = json.loads(proc.stdout)["hookSpecificOutput"]
    assert hook.get("permissionDecision") == "deny", proc.stdout


def test_the_wrapper_does_not_read_supertool_python():
    """The variable is out of this trust context entirely, not merely probed.

    A version probe stops `/usr/bin/true` and does not stop a script that
    prints the right line and then does nothing — so the property worth
    pinning is that the hook's ladder is not steerable by this variable.
    """
    wrapper = _WRAPPER.read_text(encoding="utf-8")
    for line in wrapper.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "SUPERTOOL_PYTHON" not in stripped, line


def test_a_candidate_that_ran_and_said_nothing_is_disclosed(project,
                                                            tmp_path):
    """The third state, at the layer that had two.

    A real Python is selected and executed, and it produces nothing because
    the script it was pointed at does not exist. Before #1390 the wrapper
    `exec`d and whatever happened next was the hook's whole output — an empty
    stdout that reads as `clean`.
    """
    empty = tmp_path / "not-an-install"
    empty.mkdir()
    proc = _run_wrapper("gh pr view 12", project,
                        plugin_root=str(empty))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "the wrapper said nothing at all"
    hook = json.loads(proc.stdout)["hookSpecificOutput"]
    assert hook.get("permissionDecision") != "deny"
    assert "did not run" in hook.get("additionalContext", ""), proc.stdout


def test_the_versioned_ladder_survived():
    """#572: the bare name `python3` is never run."""
    wrapper = _WRAPPER.read_text(encoding="utf-8")
    assert "python3.9" in wrapper
    for line in wrapper.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "command -v python3 " not in stripped, line
