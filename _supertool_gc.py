



















from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

import _supertool

























_GC_DEFAULT_RETENTION_DAYS: Dict[str, float] = {




    "read-elide": 1,




    "paste-backup": 7,
    "vim-cursor": 7,
    "vim-undo": 7,
    "vi-cursor": 7,
    "validators": 30,








    "statusline": 7,
}


def _gc_config() -> Dict[str, Any]:
    block = _supertool._load_config().get("gc")
    return block if isinstance(block, dict) else {}


def _gc_retention_seconds(kind: str) -> float:

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
