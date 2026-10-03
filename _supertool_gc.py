"""supertool core -- cache GC (#474, split out for #2706).

Split out of _supertool.py to keep that file under the Anthropic plugin
directory's 256 KiB per-file limit. Imported lazily by the op_gc stub
(still in _supertool.py) and by _maybe_auto_gc (also still in
_supertool.py -- its own atexit.register call must stay at
_supertool.py's own import time, not move to this module's lazy import,
or the registration moment and this process's atexit LIFO order both
change).

_cache_root(), _GC_DEFAULT_INTERVAL_SECONDS and _GC_STAMP_NAME stay in
_supertool.py: _cache_root() has 5 unrelated callers elsewhere in that
file (read-elide, paste-backup, vim-cursor/undo caches, the validator
result cache) and the other two are read only by _maybe_auto_gc, which
stays. Every reference this module makes back into _supertool.py uses
the qualified `_supertool.NAME` form, resolved at call time -- the same
reasoning as _supertool_doctor.py's own module docstring, and for the
same reason: tests patch `_gc_sweep_all`/`_cache_root` on the module
that holds the real caller, and that must stay true after the move.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

import _supertool


# ---------------------------------------------------------------------------
# Cache GC (#474)
#
# ~/.cache/supertool grew to 1.0 GB / 242k files in two weeks with no reaper
# anywhere in the tree. Every writer here is supertool's, so the retention
# policy is too.
#
# Two rules the implementation is built around, both learned the hard way:
#   * The unlink happens in Python. BSD `find -delete` and `find -exec rm {} +`
#     silently no-opped on macOS while listing the same files as matching —
#     269 files left untouched, no error, zero exit. A deletion tool that
#     reports success without deleting is the worst possible shape here.
#   * An entry whose age cannot be determined is never removed. Not knowing
#     how old something is is not evidence that it is stale.
# ---------------------------------------------------------------------------

# Per-kind, because the measurement per kind differs. `vim-cursor` and
# `vim-undo` were 99% older than 7 days — that is where the gigabyte lives.
# `validators` was *entirely* hot (zero entries older than 7 days) and is
# keyed by content hash rather than by time, so its window only has to bound
# unbounded growth, not reclaim anything today: 30 days is a no-op against
# the measured population by design. `vi-cursor` is a legacy directory no
# code still writes to.
_GC_DEFAULT_RETENTION_DAYS: Dict[str, float] = {
    # `read-elide` (#1329) is one tiny sidecar per (session, file) and the
    # session key holds a PPID, so yesterday's entries can never match again:
    # 1 day, not 7, because the population is per-session garbage the moment
    # the session ends.
    "read-elide": 1,
    # `paste-backup` (#1650) holds the bytes a full-file rewrite displaced. 7
    # days is the window in which somebody notices a note is gone; past that
    # nobody is coming back for it, and an unreaped writer is how this cache
    # reached 1.0 GB in the first place.
    "paste-backup": 7,
    "vim-cursor": 7,
    "vim-undo": 7,
    "vi-cursor": 7,
    "validators": 30,
    # `presets/_statusline_fragments.py` (#1850) writes one tiny JSON file
    # per worktree `gh-pr` has ever run in and never deletes any of its own
    # -- self-review finding, wired into the existing kind table rather than
    # a second reaper, since this sweep is already generic over
    # `_cache_root() / kind`. 7 days matches vim-cursor/vim-undo's window; a
    # fragment's own staleness is rendered explicitly by `statusline` well
    # inside that (default 300s), so this window only bounds unattributed
    # growth across abandoned worktrees, not staleness during active use.
    "statusline": 7,
}


def _gc_config() -> Dict[str, Any]:
    block = _supertool._load_config().get("gc")
    return block if isinstance(block, dict) else {}


def _gc_retention_seconds(kind: str) -> float:
    """Retention window for `kind`, in seconds. 0 or negative means never."""
    overrides = _gc_config().get("retention_days")
    raw: Any = None
    if isinstance(overrides, dict):
        raw = overrides.get(kind)
    if raw is None:
        raw = _GC_DEFAULT_RETENTION_DAYS.get(kind, 7)
    try:
        days = float(raw)
    except (TypeError, ValueError):
        days = float(_GC_DEFAULT_RETENTION_DAYS.get(kind, 7))
    if days <= 0:
        return float("inf")
    return days * 86400.0


def _gc_sweep_kind(kind: str, retention_seconds: float, dry: bool = True,
                   now: "Optional[float]" = None) -> Dict[str, Any]:
    """Prune one cache-kind directory. Non-recursive, `os.unlink` only.

    Deletes strictly on `age > retention` — an entry exactly at the boundary
    is kept. Anything that is not a plain regular file, whose `stat` fails,
    or whose mtime is in the future is counted in `skipped` and left alone.
    """
    ts = time.time() if now is None else now
    result: Dict[str, Any] = {
        "kind": kind, "removed": 0, "bytes": 0, "kept": 0, "skipped": 0,
        "missing": False, "retention_seconds": retention_seconds,
    }
    try:
        scanner = os.scandir(_supertool._cache_root() / kind)
    except OSError:
        result["missing"] = True
        return result
    with scanner:
        for entry in scanner:
            try:
                if not entry.is_file(follow_symlinks=False):
                    result["skipped"] += 1
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                result["skipped"] += 1
                continue
            age = ts - st.st_mtime
            if age < 0:
                result["skipped"] += 1
                continue
            if age <= retention_seconds:
                result["kept"] += 1
                continue
            if not dry:
                try:
                    os.unlink(entry.path)
                except OSError:
                    result["skipped"] += 1
                    continue
            result["removed"] += 1
            result["bytes"] += st.st_size
    return result


def _gc_sweep_all(kinds: "Optional[List[str]]" = None, dry: bool = True,
                  now: "Optional[float]" = None) -> List[Dict[str, Any]]:
    names = list(kinds) if kinds else list(_GC_DEFAULT_RETENTION_DAYS)
    return [_gc_sweep_kind(k, _gc_retention_seconds(k), dry=dry, now=now)
            for k in names]


def _gc_fmt_bytes(n: float) -> str:
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024.0
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} GB"


def _gc_fmt_window(seconds: float) -> str:
    return "never" if seconds == float("inf") else f"{seconds / 86400:g}d"


def op_gc(mode: str = "", kind: str = "") -> str:
    """`gc` / `gc:dry` preview, `gc:run` delete. Optional third arg: one kind."""
    known = list(_GC_DEFAULT_RETENTION_DAYS)
    mode = (mode or "dry").strip().lower()
    kind = (kind or "").strip()
    if mode in known and not kind:
        mode, kind = "dry", mode
    if mode not in ("dry", "run"):
        return (f"ERROR: unknown gc mode '{mode}' — expected 'dry' (preview, "
                f"the default) or 'run' (delete)\n")
    if kind and kind not in known:
        return (f"ERROR: unknown cache kind '{kind}' — known kinds: "
                f"{', '.join(known)}\n")

    dry = mode == "dry"
    results = _gc_sweep_all([kind] if kind else None, dry=dry)
    verb = "stale" if dry else "removed"

    lines = ["gc — dry run, nothing deleted" if dry else "gc — deleted"]
    total_n = 0
    total_b = 0
    for r in results:
        total_n += int(r["removed"])
        total_b += int(r["bytes"])
        note = "  (no such directory)" if r["missing"] else ""
        lines.append(
            f"  {r['kind']:<12} {r['removed']} {verb} / "
            f"{_gc_fmt_bytes(r['bytes'])}   (kept {r['kept']}, "
            f"skipped {r['skipped']}, retention "
            f"{_gc_fmt_window(r['retention_seconds'])}){note}"
        )
    lines.append(f"  {'total':<12} {total_n} {verb} / {_gc_fmt_bytes(total_b)}")
    if any(r["skipped"] for r in results):
        lines.append("  skipped = not a regular file, stat failed, or mtime in "
                     "the future — age unknown, so never deleted")
    if dry:
        suffix = f":{kind}" if kind else ""
        lines.append(f"  run `gc:run{suffix}` to delete")
    return "\n".join(lines) + "\n"
