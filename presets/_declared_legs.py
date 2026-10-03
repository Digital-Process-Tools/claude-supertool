#!/usr/bin/env python3









































from __future__ import annotations

import json
import re
import subprocess
from typing import Sequence





MAX_RECONCILED_RUNS = 4

_OWNER_REPO = re.compile(r"^https?://[^/]+/([^/]+)/([^/]+)(?:/|$)")


def owner_repo(url: str) -> tuple[str, str]:







    text = str(url or "").strip()
    if not text:
        return ("", "")
    if "://" not in text:
        parts = text.split("/")
        if len(parts) == 2 and all(parts):
            return (parts[0], parts[1])
        return ("", "")
    m = _OWNER_REPO.match(text)
    return (m.group(1), m.group(2)) if m else ("", "")


def _run(argv: list[str], timeout: int = 15):
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )


def legs_for_run(owner: str, repo: str, run_id) -> list[str] | None:





    if not owner or not repo or not str(run_id or "").strip():
        return None
    try:
        r = _run([
            "gh", "api",
            f"repos/{owner}/{repo}/actions/runs/{run_id}"
            "/jobs?filter=all&per_page=100",
        ])
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return None
    seen: list[str] = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        name = str(j.get("name") or "?")
        if name not in seen:
            seen.append(name)
    return seen


def legs_for_runs(owner: str, repo: str,
                  run_ids: Sequence) -> tuple[int | None, list[str]]:






    ids = [str(r) for r in run_ids if str(r or "").strip()]
    if not ids or len(ids) > MAX_RECONCILED_RUNS:
        return (None, [])
    names: list[str] = []
    for rid in ids:
        found = legs_for_run(owner, repo, rid)
        if found is None:
            return (None, [])
        names.extend(found)
    return (len(names), names)


def reconcilable(attempt: object) -> bool:













    try:
        return int(attempt) != 1
    except (TypeError, ValueError):
        return True


def missing_names(declared: Sequence[str], found: Sequence[str]) -> list[str]:

    remaining: dict[str, int] = {}
    for n in found:
        remaining[n] = remaining.get(n, 0) + 1
    out: list[str] = []
    for name in declared:
        if remaining.get(name, 0):
            remaining[name] -= 1
        else:
            out.append(name)
    return out
