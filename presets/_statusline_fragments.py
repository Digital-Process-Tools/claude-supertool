#!/usr/bin/env python3

























from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from typing import Any, Dict, Optional, Tuple



_ENV_DIR = "SUPERTOOL_STATUSLINE_CACHE_DIR"


def cache_dir() -> str:

    override = os.environ.get(_ENV_DIR, "").strip()
    if override:
        return override
    base = os.environ.get("XDG_CACHE_HOME", "").strip()
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "supertool", "statusline")


def _worktree_root(path: str) -> str:






















    real = os.path.realpath(path or os.getcwd())
    cur = real
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return real
        cur = parent


def _key(worktree_dir: str) -> str:







    real = _worktree_root(worktree_dir or os.getcwd())
    return hashlib.sha256(real.encode("utf-8", "surrogateescape")).hexdigest()[:20]


def _safe_op_name(op: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(op))


def _path(op: str, worktree_dir: str) -> str:
    return os.path.join(cache_dir(), f"{_key(worktree_dir)}.{_safe_op_name(op)}.json")


def publish(op: str, worktree_dir: str, data: Dict[str, Any]) -> Optional[str]:








    try:
        directory = cache_dir()
        os.makedirs(directory, exist_ok=True)
        payload = dict(data)
        payload["_ts"] = time.time()
        payload["_dir"] = os.path.realpath(worktree_dir or os.getcwd())
        target = _path(op, worktree_dir)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-statusline-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return target
    except Exception:
        return None


def read(op: str, worktree_dir: str) -> Tuple[Optional[Dict[str, Any]], str]:








    path = _path(op, worktree_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None, "not-published"
    except OSError:
        return None, "unreadable"
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None, "unreadable"
    if not isinstance(data, dict):
        return None, "unreadable"
    return data, "ok"


def age_seconds(data: Dict[str, Any]) -> Optional[float]:

    ts = data.get("_ts")
    if not isinstance(ts, (int, float)):
        return None
    return max(0.0, time.time() - float(ts))
