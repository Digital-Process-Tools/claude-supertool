#!/usr/bin/env python




















































from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "validators", "common"))
try:
    from spawnable import which_excluding_cwd  
    _CWD_GUARD_AVAILABLE = True
except ImportError:










    which_excluding_cwd = shutil.which
    _CWD_GUARD_AVAILABLE = False




_BASH_PROBE = "supertool-bash-ok"




_CANDIDATES_ENV = "SUPERTOOL_SELFTEST_BASH_CANDIDATES"

_BACKSLASH = chr(92)


def bash_candidates(environ=None):











    environ = os.environ if environ is None else environ
    override = environ.get(_CANDIDATES_ENV)
    if override is not None:
        return [part for part in override.split(os.pathsep) if part]
    git_bin = "C:" + _BACKSLASH + "Program Files" + _BACKSLASH + "Git"
    return [which_excluding_cwd("bash"),
            git_bin + _BACKSLASH + "bin" + _BACKSLASH + "bash.exe",
            git_bin + _BACKSLASH + "usr" + _BACKSLASH + "bin"
            + _BACKSLASH + "bash.exe",
            "/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash"]


def first_bash_that_runs_a_script(candidates):

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







    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import _supertool
    except Exception as exc:  
        return None, "supertool could not be imported from %s (%s)" % (
            root, exc)
    try:
        replacements, _ = _supertool._guard_replacements()
    except Exception as exc:  
        return None, "the registry could not be read (%s)" % (exc,)
    for replacement in replacements:
        command = " ".join(replacement.argv)
        try:
            if _supertool.guard_command(command).state == "blocked":
                return command, ""
        except Exception:  
            continue
    return None, ""


def wrapper_denies(bash, wrapper, root, command):

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


def rule_inventory(root, environ=None):










    hooks = os.path.join(root, "hooks")
    if hooks not in sys.path:
        sys.path.insert(0, hooks)
    try:
        import shipped_rules
    except Exception as exc:
        return ["  rules       : could not run - hooks/shipped_rules.py "
                "could not be imported from " + hooks + " (" + str(exc)
                + "), so nothing here says which rules this install ships"]
    environ = os.environ if environ is None else environ
    project = environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        return shipped_rules.inventory(root, project)
    except Exception as exc:  
        return ["  rules       : could not run - the rule inventory raised "
                + type(exc).__name__ + ": " + str(exc)]


def report(root, environ=None):

    wrapper = os.path.join(root, "hooks", "pre-bash-guard.sh")
    lines = ["supertool raw-command guard, self-check",
             "  plugin root : " + root]
    if _CWD_GUARD_AVAILABLE:
        lines.append("  cwd guard   : available - bash resolution excludes "
                     "a cwd-only match (#2610)")
    else:
        lines.append("  cwd guard   : NOT available - validators/common/"
                     "spawnable.py could not be imported, so bash "
                     "resolution fell back to raw shutil.which(), which "
                     "does not exclude a cwd-only match (#2610)")
    lines.extend(rule_inventory(root, environ))

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
