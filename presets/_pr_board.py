#!/usr/bin/env python3















































from __future__ import annotations

import json
import subprocess
from typing import Any


def run_pr_list(cmd: list, timeout: int = 30) -> tuple:




























    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout, encoding="utf-8",
                                errors="replace")
    except subprocess.TimeoutExpired:
        return None, f"gh pr list timed out after {timeout}s", None, ""
    except FileNotFoundError:





        binary = cmd[0] if cmd else "gh"
        return None, f"{binary} not found on PATH", None, ""
    except OSError as exc:
        return None, f"gh pr list failed: {exc}", None, ""
    if result.returncode != 0:
        raw = (result.stderr or "").strip()
        detail = raw or (result.stdout or "").strip()
        first = detail.splitlines()[0] if detail else "no output"
        return (None, f"gh pr list exited {result.returncode}: {first}",
                result.returncode, raw)
    try:
        data = json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        return (None, "gh pr list returned unparseable JSON",
                result.returncode, "")
    if not isinstance(data, list):
        return (None, "gh pr list did not return a list",
                result.returncode, "")
    return data, "", result.returncode, ""


def head_and_runs(branch_mod: Any, ref: str) -> tuple:
















    sha, age, err = branch_mod._head_commit(ref)
    if err:
        return sha, age, None, err
    runs, err = branch_mod._run_list(ref)
    if err or runs is None:
        return sha, age, None, err or "run list unreadable"
    return sha, age, runs, ""
