

























from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _untrusted  
import _refname  
import _st_hint  



_TIMEOUT = 3


def _run(args: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=_TIMEOUT,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def current_branch() -> str | None:

    r = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if r is None or r.returncode != 0:
        return None
    local = r.stdout.strip()
    if not local or local == "HEAD":
        return None
    return local


def holding_worktree(source: str) -> tuple[str, str]:










    r = _run(["git", "worktree", "list", "--porcelain"])
    if r is None:
        return "", "git worktree list could not be run"
    if r.returncode != 0:
        why = (r.stderr or r.stdout).strip().splitlines()
        return "", (why[0] if why else f"git worktree list exited {r.returncode}")

    path = ""
    for block in r.stdout.split("\n\n"):
        entry_path = ""
        entry_branch = ""
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                entry_path = value
            elif key == "branch":
                entry_branch = value.replace("refs/heads/", "", 1)
        if entry_branch and entry_branch == source and entry_path:
            path = entry_path
            break
    return path, ""


def check(source: str, actionable: bool = True) -> str:













    if not source or source == "?":
        return ""
    raw_local = current_branch()
    if raw_local is None:
        return ""
    if raw_local == source:
        return f"You are on: {_untrusted.flat(raw_local)} ✓"





    local = _untrusted.flat(raw_local)
    named = _untrusted.flat(source)

    path, why = holding_worktree(source)
    if why:
        return (f"You are on: {local} — whether {named} is checked out in "
                f"another worktree is UNKNOWN ({_untrusted.flat(why)}), so "
                f"no switch is suggested")
    if path:
        return (f"You are on: {local} — {named} is checked out in another "
                f"worktree: {_untrusted.flat(path)} (cd there; a checkout "
                f"here would be refused)")
    if not actionable:
        return (f"You are on: {local} ⚠ MISMATCH — this is {named}; "
                f"read-only op, HEAD left alone")
    if not _refname.ordinary(source):




















        return (f"You are on: {local} ⚠ MISMATCH — this is {named}, a name "
                f"outside the ordinary-refname set (letters, digits, "
                f"`. _ / -`, no leading `-`), so no switch command is "
                f"suggested — check it out yourself, deliberately")
    return (f"You are on: {local} ⚠ MISMATCH — switch with: "
            f"{_st_hint.st_hint('git-checkout:' + named)}")


def describe(source: str) -> str:




























    if not source or source == "?":
        return ""
    raw_local = current_branch()
    if raw_local is None:
        return ""
    if raw_local == source:
        return f"You are on: {_untrusted.flat(raw_local)} ✓"



    return (f"You are on: {_untrusted.flat(raw_local)} — this run is from "
            f"{_untrusted.flat(source)}; reading a run needs no checkout")
