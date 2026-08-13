"""On native Windows the guard is inert and nothing said so (#1378).

`hooks.json` invokes the guard through `bash`. Under `cmd.exe` or PowerShell
with no Git Bash and no WSL the hook never executes, and a session where the
gate never ran is byte-identical to one where it ran and found nothing — the
house defect at the platform layer.

**The disclosure cannot be a hook.** Every hook this plugin ships is a bash
script, `session-start.sh` included, so a line added to any of them is a line
that does not run on precisely the host it would be describing. #1382 has
already ruled out the other shape: a non-zero SessionStart hook is a broken
session on every platform to report a missing interpreter on one.

So the channel is the one the issue names second — a check the user runs —
and it is written in Python, because Python is what such a host has. What is
pinned here is that it can say **could not run**, that it never says
`enforcing` on the strength of bash merely existing, and that it does not
claim the thing it cannot know: whether Claude Code invoked the hook at all.

**Every claim in this file about native Windows is reasoned, not observed**
(the #627 convention). Nobody here has that host. What is observed is that the
wrapper is bash, that `hooks.json` names bash, and that denying this macOS
host a usable bash produces exactly the inert-guard state described.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

from test_guard_interpreter_ladder_1390 import needs_wrapper

_ROOT = Path(__file__).resolve().parent.parent
_SELFTEST = _ROOT / "hooks" / "guard-selftest.py"

# Loaded by path: the file is named with a dash so a user can run it as a
# script without wondering whether it is importable, which is the point of it.
_spec = importlib.util.spec_from_file_location("guard_selftest", _SELFTEST)
selftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(selftest)


def _run(env_overrides=None, cwd=None):
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(_ROOT)
    env.update(env_overrides or {})
    return subprocess.run([sys.executable, str(_SELFTEST)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env,
                          cwd=str(cwd or _ROOT), timeout=180)


@needs_wrapper
def test_the_check_runs_without_a_shell_and_reports_enforcing():
    """The control. A check that can only ever fail is not a check.

    Gated on the same fact `test_guard_interpreter_ladder_1390` gates on — a
    host that cannot run the wrapper at all would fail this row for the
    reason the row is testing for, which is a red that says nothing.
    """
    proc = _run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "enforcing" in proc.stdout, proc.stdout


def test_it_says_could_not_run_when_no_bash_can_run_a_script():
    """The state #1378 is about, produced on a host that does have bash."""
    proc = _run({"SUPERTOOL_SELFTEST_BASH_CANDIDATES": ""})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "could not run" in proc.stdout, proc.stdout
    assert "bash" in proc.stdout, proc.stdout
    assert "enforcing" not in proc.stdout, proc.stdout


def test_a_bash_that_produces_no_verdict_is_never_read_as_enforcing(tmp_path):
    """`bash` on PATH is not the claim; an envelope out of the wrapper is.

    On Windows `bash.exe` is often the WSL launcher, which is not a shell,
    cannot open a script and says so on stdout — #1390's gate learned that on
    four `windows-latest` legs of PR #1399. A check that stopped at "a file
    named bash exists" would report the guard healthy on that host.
    """
    fake = tmp_path / "bash"
    fake.write_text("#!/bin/sh" + chr(10) + "exit 0" + chr(10),
                    encoding="utf-8")
    fake.chmod(0o755)
    proc = _run({"SUPERTOOL_SELFTEST_BASH_CANDIDATES": str(fake)})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "could not run" in proc.stdout, proc.stdout
    assert "enforcing" not in proc.stdout, proc.stdout


@needs_wrapper
def test_it_does_not_claim_the_hook_is_wired():
    """Three states, and the third is about the checker, not about the host.

    Whether Claude Code loaded the plugin and invoked the PreToolUse hook is
    not visible from here. Saying `enforcing` without that caveat would be the
    same absence-read-as-presence one layer up.
    """
    proc = _run()
    assert "cannot tell" in proc.stdout.lower(), proc.stdout
    assert "PreToolUse" in proc.stdout, proc.stdout


def test_a_program_that_is_not_a_shell_is_never_selected():
    assert selftest.first_bash_that_runs_a_script([sys.executable]) is None
    assert selftest.first_bash_that_runs_a_script([None, "", "  "]) is None


def test_the_candidate_list_can_be_overridden_and_is_not_empty_by_default():
    """The override is the test seam; an empty default would be vacuous."""
    assert selftest.bash_candidates()
    assert selftest.bash_candidates({
        "SUPERTOOL_SELFTEST_BASH_CANDIDATES": "/a" + os.pathsep + "/b"}) == [
        "/a", "/b"]
    assert selftest.bash_candidates(
        {"SUPERTOOL_SELFTEST_BASH_CANDIDATES": ""}) == []


def test_the_readme_does_not_say_windows_has_nothing_to_gate():
    """The one channel a native-Windows user has, and it said the opposite.

    `hooks.json` has matched `Bash|PowerShell` since #1413, which exists
    because Claude treats PowerShell as the primary shell where that tool is
    enabled and routes shell commands through it. The README's platform
    section still told the reader there was nothing for the guard to gate on
    such a host — a sentence that makes #1378's inert guard invisible by
    denying that it matters. Graded reasoned, not observed.
    """
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert "nothing for the raw-command guard to gate" not in readme
    assert "hooks/guard-selftest.py" in readme
