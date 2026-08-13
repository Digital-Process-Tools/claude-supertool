"""The half of #1401 that survived its own refutation (#1401).

The issue was filed about `bash` in `hooks.json` being resolved by
`CreateProcess`. That is refuted and the refutation is pinned in
`tests/test_hook_interpreter_windows_1401_1402.py`: a `"type": "command"` hook
with no `args` is **shell form**, so the string goes to a shell - Git Bash on
Windows, PowerShell where Git Bash is absent - and adding `args` would switch
to exec form and *introduce* the PATH search the issue described.

What survives is a different defect, and it is the one this file is about.
`PreToolUse` is tool-gated, so on a native Windows host with no Git for
Windows the guard hook never fires - there is no `Bash` tool for it to match.
**`SessionStart` is not tool-gated.** It fires on that host, `bash` is not
there to run it, and the session loses the `./supertool` wrapper and the op
roster with nothing else failing to mask it.

**That gap is accepted and disclosed rather than fixed**, and the disclosure
is what is pinned here. Every candidate repair is a change to an untestable
host made from a Mac: `args` is the exec-form PATH search this repo already
refuses; a second PowerShell entry is a non-zero hook on every POSIX session
to serve one Windows one; a command string valid under both `sh -c` and
PowerShell is a polyglot shipped to every plugin user; and no interpreter name
is portable enough for exec form to carry - `python`/`python3` are the App
Execution Alias stubs #572 banned for blocking rather than erroring, the
versioned names are absent on Windows (#1402), and `py -3` is absent
everywhere else. The ladder that resolves that is `hooks/python-ladder.sh`,
which is itself a shell script, so it cannot bootstrap a shell-less host.

So the deliverable is that the one bash-free channel this repo has says the
whole truth. `hooks/guard-selftest.py` already reports `could not run` when
nothing here runs a bash script - but that single fact kills **both** shipped
hooks, and the report named only the guard. A reader on the affected host
learned that raw commands run unguarded and did not learn why the wrapper and
the roster were missing. #1401's own words: "the next person fixes one and
reads the other as already correct."

**Every claim here about native Windows is reasoned, not observed** (the #627
convention). Nobody on this project has that host. What is observed is that
`hooks.json` names `bash` for both entries, that `SessionStart` carries no
matcher, and that denying this macOS host a usable bash produces exactly the
state described.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SELFTEST = _ROOT / "hooks" / "guard-selftest.py"

#: The pointer the README used to give a native-Windows reader: one cost, when
#: there are two. Asserted absent rather than paraphrased, because the whole
#: defect is that the sentence reads as complete.
_HALF_A_POINTER = "see the raw-command guard note below for what that costs"


def _no_bash_report() -> str:
    """The self-check on a host where nothing runs a bash script."""
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(_ROOT)
    env["SUPERTOOL_SELFTEST_BASH_CANDIDATES"] = ""
    proc = subprocess.run([sys.executable, str(_SELFTEST)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=str(_ROOT),
                          timeout=180)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "could not run" in proc.stdout, proc.stdout
    return proc.stdout


def test_the_no_bash_state_names_the_session_start_hook_as_well():
    """One fact, two dead hooks. Reporting one of them is the defect.

    `SessionStart` is not tool-gated, so it is the half that actually fires
    on the host with no bash - and it was the half the report was silent
    about, while spending four lines on the guard.
    """
    report = _no_bash_report()
    assert "session-start.sh" in report, report
    assert "pre-bash-guard.sh" in report, report


def test_the_no_bash_state_names_what_the_session_loses_and_the_way_back():
    """An absence a reader can act on, not just an absence.

    What is lost is the `./supertool` wrapper and the op roster. Both are
    reachable without any shell, by calling the tool the plugin already
    installed - so a disclosure that stops at "it did not run" is the
    absence-read-as-absence one layer up.
    """
    report = _no_bash_report()
    assert "roster" in report, report
    assert "ops:roster" in report, report


def test_session_start_is_not_tool_gated():
    """The one structural fact the rest of #1401 turns on.

    `PreToolUse` matches `Bash|PowerShell`, so on a host with no `Bash` tool
    the guard hook is never asked. `SessionStart` has no matcher and cannot
    have one - it is not a tool event - so it fires there regardless. A future
    reader must not conclude the two entries fail alike.
    """
    hooks = json.loads((_ROOT / "hooks" / "hooks.json").read_text(
        encoding="utf-8"))["hooks"]
    for group in hooks["SessionStart"]:
        assert "matcher" not in group, (
            "SessionStart is not a tool event; a matcher here would read as a "
            "gate that does not exist: " + json.dumps(group))
    assert any("matcher" in group for group in hooks["PreToolUse"]), hooks


def test_the_readme_does_not_send_the_reader_only_to_the_guard_note():
    """The user-facing half of the same silence.

    The platform section said native Windows without bash "won't fire the
    hooks - see the raw-command guard note below for what that costs", and
    that note is about the guard alone. The other cost is a session with no
    wrapper and no roster, which is the more visible one and was unwritten.
    """
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert _HALF_A_POINTER not in readme
    assert "hooks/session-start.sh" in readme
    assert "ops:roster" in readme
