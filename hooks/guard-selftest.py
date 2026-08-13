#!/usr/bin/env python
"""Is the raw-command guard actually enforcing on this host? (#1378)

    py -3 hooks/guard-selftest.py          # Windows, no bash needed
    python3 hooks/guard-selftest.py        # anywhere else

`hooks.json` invokes the guard through `bash`. Under native `cmd.exe` or
PowerShell with no Git Bash and no WSL that hook does not execute, every raw
command an op supersedes runs unguarded, and the session is byte-identical to
one where the guard ran and had nothing to say. That is this repository's
house defect at the platform layer, and it had **no disclosure channel at
all**: every hook the plugin ships is a bash script, so a line added to any of
them is a line that cannot run on the host it would be describing, and #1382
already ruled out a hook that fails loudly — a non-zero SessionStart hook is a
broken session on every platform, to report a missing interpreter on one.

So this is a check the user runs, written in Python because Python is what
such a host has. Three states, the same three the guard itself uses:

    enforcing       the wrapper ran here and denied a command the registry
                    replaces, end to end through the shell it will really use
    could not run   named, with what was tried
    nothing to test the registry replaces nothing, so there is no gate to
                    exercise and "enforcing" would be an empty claim

**`could not run` for want of a bash is a statement about both shipped hooks,
and is reported as one** (#1401). The same missing shell kills
`hooks/session-start.sh`, and that half is the one a user actually notices:
`PreToolUse` is tool-gated, so where there is no Bash tool the guard is never
asked, while `SessionStart` is not gated and fires regardless - leaving a
session with no `./supertool` wrapper and no op roster. The report names the
way back, which needs no shell: run `supertool.py` by path.

**It cannot tell you whether Claude Code invoked the hook.** Plugin
installation, `hooks.json` registration and the PreToolUse dispatch are not
observable from here, and saying `enforcing` without that caveat would be the
same absence-read-as-presence one layer up. It answers "this host can run the
gate", which is the half that was silent.

A `bash` is chosen by what it does, not by what it is called: on Windows
`bash.exe` on PATH is commonly the WSL launcher, which is not a shell, cannot
open a script, and writes a UTF-16 complaint to stdout while exiting 1. That
cost PR #1399 four `windows-latest` legs, and the rule is copied from the gate
#1390 grew afterwards rather than reasoned again.

Windows evidence grade, the #627 convention: everything here about native
`cmd.exe` and PowerShell is **reasoned, not observed** — nobody on this
project has that host. What is observed is that the wrapper is bash, that
`hooks.json` names bash, and that denying a POSIX host a usable bash produces
exactly the state described.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

#: One string a candidate has to print exactly. Same shape as the interpreter
#: ladder's probe and for the same reason: exiting 0 is a property of every
#: binary on the box.
_BASH_PROBE = "supertool-bash-ok"

#: The test seam. A candidate list, `os.pathsep`-separated; empty means "no
#: candidates", which is how the inert-guard state is reproduced on a host
#: that does have bash.
_CANDIDATES_ENV = "SUPERTOOL_SELFTEST_BASH_CANDIDATES"

_BACKSLASH = chr(92)


def bash_candidates(environ=None):
    """Where a bash that runs scripts might be, most likely first."""
    environ = os.environ if environ is None else environ
    override = environ.get(_CANDIDATES_ENV)
    if override is not None:
        return [part for part in override.split(os.pathsep) if part]
    git_bin = "C:" + _BACKSLASH + "Program Files" + _BACKSLASH + "Git"
    return [shutil.which("bash"),
            git_bin + _BACKSLASH + "bin" + _BACKSLASH + "bash.exe",
            git_bin + _BACKSLASH + "usr" + _BACKSLASH + "bin"
            + _BACKSLASH + "bash.exe",
            "/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash"]


def first_bash_that_runs_a_script(candidates):
    """A bash chosen by what it does. None when nothing here is a shell."""
    for candidate in candidates:
        if not candidate or not candidate.strip():
            continue
        try:
            proc = subprocess.run(
                [candidate, "-c", "printf %s " + _BASH_PROBE],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and proc.stdout.strip() == _BASH_PROBE:
            return candidate
    return None


def a_command_the_registry_replaces(root):
    """One argv the guard should deny, or None if the registry claims none.

    Taken from the registry rather than hardcoded: a user who enables no
    preset that declares `replaces` has nothing to exercise, and a check that
    asserted `git push` regardless would report a broken guard on a perfectly
    healthy install.
    """
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import _supertool
    except Exception as exc:  # pragma: no cover - a broken install
        return None, "supertool could not be imported from %s (%s)" % (
            root, exc)
    try:
        replacements, _ = _supertool._guard_replacements()
    except Exception as exc:  # pragma: no cover - defensive
        return None, "the registry could not be read (%s)" % (exc,)
    for replacement in replacements:
        command = " ".join(replacement.argv)
        try:
            if _supertool.guard_command(command).state == "blocked":
                return command, ""
        except Exception:  # pragma: no cover - defensive
            continue
    return None, ""


def wrapper_denies(bash, wrapper, root, command):
    """Run the real wrapper the way Claude Code does. (verdict, detail)"""
    event = json.dumps({"tool_name": "Bash",
                        "tool_input": {"command": command}})
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = root
    try:
        proc = subprocess.run([bash, wrapper], input=event,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env,
                              timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "the wrapper could not be spawned: %s" % (exc,)
    if proc.returncode != 0:
        return False, "the wrapper exited %d and produced %r" % (
            proc.returncode, proc.stdout[:120])
    try:
        hook = json.loads(proc.stdout)["hookSpecificOutput"]
    except (ValueError, KeyError, TypeError):
        return False, "no hook envelope in %r" % (proc.stdout[:120],)
    if not isinstance(hook, dict):
        return False, "the envelope is not an object: %r" % (
            proc.stdout[:120],)
    if hook.get("permissionDecision") == "deny":
        return True, ""
    note = hook.get("additionalContext") or "no decision and no note"
    return False, note[:400]


def report(root, environ=None):
    """(lines, exit code). Never a clean shape for a state it could not reach."""
    wrapper = os.path.join(root, "hooks", "pre-bash-guard.sh")
    lines = ["supertool raw-command guard, self-check",
             "  plugin root : " + root]

    command, why = a_command_the_registry_replaces(root)
    if why:
        lines.append("  state       : could not run - " + why)
        return lines, 1
    if command is None:
        lines.append("  state       : nothing to test - no op in the "
                     "effective registry declares a `replaces`, so there is "
                     "no raw command for the guard to deny here")
        return lines, 0

    candidates = bash_candidates(environ)
    bash = first_bash_that_runs_a_script(candidates)
    if bash is None:
        lines.append("  state       : could not run - nothing on this host "
                     "runs a bash script, so hooks.json's `bash "
                     "pre-bash-guard.sh` never executes")
        lines.append("  tried       : " + (", ".join(
            str(c) for c in candidates if c) or "no candidates"))
        lines.append("  meaning     : every raw command an op supersedes runs "
                     "unguarded in this shell, and nothing in the transcript "
                     "will say so. Install Git Bash or use WSL, or accept "
                     "that the gate is off here.")
        # One fact, two dead hooks (#1401). `pre-bash-guard.sh` is tool-gated
        # and `session-start.sh` is not, so on the host with no Bash tool the
        # guard is never even asked while the session hook fires and cannot
        # run. Reporting only the guard leaves the more visible loss - no
        # wrapper, no roster - as an absence with no account of itself.
        lines.append("  also        : hooks.json runs hooks/session-start.sh "
                     "through the same bash, so it does not execute here "
                     "either. SessionStart is not tool-gated: it fires on "
                     "this host regardless. The session gets no ./supertool "
                     "wrapper and no op roster.")
        lines.append("  instead     : call the tool by path, which needs no "
                     "shell - py -3 supertool.py 'ops:roster' on Windows, "
                     "python3 supertool.py 'ops:roster' elsewhere - and read "
                     "./supertool in the docs as that path (#1401).")
        return lines, 1

    lines.append("  bash        : " + bash)
    ok, detail = wrapper_denies(bash, wrapper, root, command)
    if not ok:
        lines.append("  state       : could not run - the wrapper did not "
                     "deny " + repr(command) + ": " + detail)
        return lines, 1
    lines.append("  state       : enforcing - the wrapper denied "
                 + repr(command) + " through the shell it really uses")
    lines.append("  cannot tell : whether Claude Code has this plugin "
                 "installed and invokes the PreToolUse hook. That is not "
                 "observable from here; this says the host can run the gate, "
                 "not that the gate was asked.")
    return lines, 0


def main(argv=None):
    root = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    lines, code = report(root)
    sys.stdout.write(chr(10).join(lines) + chr(10))
    return code


if __name__ == "__main__":
    sys.exit(main())
