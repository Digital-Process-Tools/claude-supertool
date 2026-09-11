#!/usr/bin/env python3
"""Atomic, worktree-keyed fragment cache for the `statusline` op (#1850).

A network-backed op (`gh-pr` today) publishes its own tally as a side effect
of being run normally — the reconciliation it already performed via
`presets/_checks.py`, not a second one the statusline op would have to
recompute and could get out of sync with `radar`/`gh-pr` itself ("share the
model, not the op", #1850). The statusline op only ever *reads* what this
module wrote; it never talks to the network.

Three properties this module exists to hold, all load-bearing:

* **Atomic.** Temp file + `os.replace`, never read-modify-write, so a reader
  can never observe a half-written fragment — the harness re-runs the render
  on a 300ms debounce, so a torn read would not be rare.
* **Keyed by worktree.** Sibling worktrees of the same repo must not share
  one cache slot (#2034's contention shape, and the contamination case where
  one tree's branch leaks into another tree's bar). The key is a hash of the
  *resolved* directory, so `workspace.current_dir` from the hook's stdin
  blob is what selects the slot.
* **Three states on read, never two.** `not-published` (this op has never run
  in this worktree this session — the normal, expected case) must never
  render the same as `unreadable` (a fragment exists but is corrupt) — the
  first is silence, the second is a finding. Collapsing them was the
  defect class named throughout #1850's design discussion.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from typing import Any, Dict, Optional, Tuple

#: Overrides the whole cache root. Mainly for tests — production callers get
#: a per-user XDG-ish default below.
_ENV_DIR = "SUPERTOOL_STATUSLINE_CACHE_DIR"


def cache_dir() -> str:
    """Where fragments live. `SUPERTOOL_STATUSLINE_CACHE_DIR` wins outright."""
    override = os.environ.get(_ENV_DIR, "").strip()
    if override:
        return override
    base = os.environ.get("XDG_CACHE_HOME", "").strip()
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "supertool", "statusline")


def _worktree_root(path: str) -> str:
    """Walk up from `path` to the nearest directory holding a `.git` entry.

    Self-review finding (#1850): `gh-pr` publishes under whatever
    `os.getcwd()` happens to be when it is invoked, while `statusline` reads
    under Claude Code's reported `workspace.current_dir` -- typically the
    project root. A monorepo subpackage, or any wrapper script that `cd`s
    before invoking supertool, makes those two different strings for the
    SAME worktree, and keying on the raw string silently split one worktree
    into two cache slots that could never find each other.

    `.git` is present both in an ordinary clone (a directory) and in a
    linked worktree (a file pointing at the shared gitdir), so a plain
    `os.path.exists` check covers both without a `git` subprocess -- kept
    off this path deliberately, since `read()` sits on the statusline op's
    render budget and a subprocess per read would eat into it for no benefit
    a filesystem walk does not already give.

    A directory with no `.git` anywhere above it (every other test fixture
    in this module, and any caller outside a git repository) resolves to its
    own realpath, unchanged -- this must never WIDEN the key for two
    genuinely unrelated directories that happen to share a distant ancestor.
    """
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
    """A stable, filesystem-safe slot name for one worktree.

    Resolved to the enclosing git worktree root (see `_worktree_root`) so a
    fragment published from a subdirectory and one read from the project
    root land in the same slot, and resolved with `os.path.realpath` first so
    a symlinked path and its target do too.
    """
    real = _worktree_root(worktree_dir or os.getcwd())
    return hashlib.sha256(real.encode("utf-8", "surrogateescape")).hexdigest()[:20]


def _safe_op_name(op: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(op))


def _path(op: str, worktree_dir: str) -> str:
    return os.path.join(cache_dir(), f"{_key(worktree_dir)}.{_safe_op_name(op)}.json")


def publish(op: str, worktree_dir: str, data: Dict[str, Any]) -> Optional[str]:
    """Write `data` as `op`'s fragment for `worktree_dir`. Best-effort.

    Returns the path written on success, `None` on any failure. Publishing a
    fragment is a side effect of a normal op run and must never be the thing
    that turns a working `gh-pr` call into a failing one — a read-only op
    gaining a hard dependency on a writable cache directory would be a
    regression the statusline feature is not worth causing.
    """
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
    """`(data, state)` — state is `ok`, `not-published` or `unreadable`.

    `not-published` means the file is simply not there, which is the normal
    state for any op that has not been run in this worktree this session.
    `unreadable` means something *is* there and could not be trusted — wrong
    permissions, truncated write, not a JSON object — and a caller must never
    fold that into `not-published`: one is silence, the other is a finding.
    """
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
    """Seconds since `publish()` wrote this fragment, or `None` if unknown."""
    ts = data.get("_ts")
    if not isinstance(ts, (int, float)):
        return None
    return max(0.0, time.time() - float(ts))
