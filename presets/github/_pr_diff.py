#!/usr/bin/env python3
























































from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _untrusted  
from _env import env_int  



MAX_FILES = 60



MAX_BYTES = 65536


def _path_from_header(line: str) -> str:

    rest = line[len("diff --git "):].strip()
    half = len(rest) // 2
    if rest[half:half + 1] == " ":
        left, right = rest[:half], rest[half + 1:]
        if left.startswith("a/") and right.startswith("b/") and left[2:] == right[2:]:
            return left[2:]
    parts = rest.split(" b/", 1)
    if len(parts) == 2:
        return parts[1]
    return rest


def _strip_prefix(path: str) -> str:
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse(patch: str) -> list[dict]:






    files: list[dict] = []
    current: dict | None = None
    hunk: list[str] | None = None

    def close_hunk() -> None:
        nonlocal hunk
        if current is not None and hunk:
            current["hunks"].append("\n".join(hunk))
        hunk = None












    for line in _untrusted.split_lines(patch or ""):
        if line.startswith("diff --git "):
            close_hunk()
            current = {
                "path": _path_from_header(line),
                "old_path": None,
                "status": "M",
                "added": 0,
                "removed": 0,
                "hunks": [],
                "binary": False,
            }
            files.append(current)
            continue
        if current is None:
            continue
        if hunk is None:
            if line.startswith("new file mode"):
                current["status"] = "A"
                continue
            if line.startswith("deleted file mode"):
                current["status"] = "D"
                continue
            if line.startswith("rename from "):
                current["status"] = "R"
                current["old_path"] = line[len("rename from "):].strip()
                continue
            if line.startswith("rename to "):
                current["status"] = "R"
                current["path"] = line[len("rename to "):].strip()
                continue
            if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                current["binary"] = True
                continue
            if line.startswith("--- "):
                target = line[4:].strip()
                if target != "/dev/null":
                    current["old_path"] = _strip_prefix(target)
                continue
            if line.startswith("+++ "):
                target = line[4:].strip()
                if target != "/dev/null":
                    current["path"] = _strip_prefix(target)
                continue
            if not line.startswith("@@"):
                continue
        if line.startswith("@@"):
            close_hunk()
            hunk = [line]
            continue
        if hunk is not None:
            hunk.append(line)
            if line.startswith("+"):
                current["added"] += 1
            elif line.startswith("-"):
                current["removed"] += 1
    close_hunk()
    return files


def _net_status(prev: str, nxt: str) -> str:






    if nxt == "D":
        return "D"
    if prev == "D":
        return "M"
    if "A" in (prev, nxt):
        return "A"
    if "R" in (prev, nxt):
        return "R"
    return "M"


def coalesce(files: list[dict]) -> list[dict]:

















    merged: dict[str, dict] = {}
    order: list[str] = []
    for entry in files:
        path = str(entry.get("path", ""))
        if path not in merged:
            first = dict(entry)
            first["hunks"] = list(entry.get("hunks") or [])
            first["entries"] = 1
            merged[path] = first
            order.append(path)
            continue
        acc = merged[path]
        acc["entries"] = int(acc.get("entries", 1)) + 1
        acc["added"] = int(acc.get("added", 0)) + int(entry.get("added", 0))
        acc["removed"] = int(acc.get("removed", 0)) + int(entry.get("removed", 0))
        acc["hunks"].extend(entry.get("hunks") or [])
        acc["binary"] = bool(acc.get("binary")) or bool(entry.get("binary"))
        if acc.get("old_path") is None:
            acc["old_path"] = entry.get("old_path")
        acc["status"] = _net_status(str(acc.get("status", "M")),
                                    str(entry.get("status", "M")))
    return [merged[p] for p in order]


def _hunk_signature(hunk: str) -> tuple[tuple[str, ...], tuple[str, ...]]:






    body = _untrusted.split_lines(hunk)[1:]
    removed = tuple(l[1:].strip() for l in body if l.startswith("-"))
    added = tuple(l[1:].strip() for l in body if l.startswith("+"))
    return removed, added


def mechanical_note(entry: dict) -> str | None:







    hunks = entry.get("hunks") or []
    if len(hunks) < 2:
        return None
    signatures = {_hunk_signature(h) for h in hunks}
    if len(signatures) != 1:
        return None
    only = next(iter(signatures))
    if not only[0] and not only[1]:
        return None
    return f"same edit x{len(hunks)}"


def _stat_cell(entry: dict) -> str:
    if entry.get("binary"):
        return "binary"
    return f"+{entry.get('added', 0)} -{entry.get('removed', 0)}"


def _churn(entry: dict) -> int:
    return int(entry.get("added", 0)) + int(entry.get("removed", 0))


def _summary(files: list[dict], header: list[str], number: str | None,
             max_files: int) -> tuple[str, int]:
    out = list(header)
    total = len(files)
    if total == 0:




        out.append("No files changed in this PR (0 files) — "
                   "the diff was read and it is empty.")
        return "\n".join(out), 0

    added = sum(int(f.get("added", 0)) for f in files)
    removed = sum(int(f.get("removed", 0)) for f in files)
    out.append(f"{total} files, +{added} -{removed}")
    out.append("")
    out.append(f"## Files changed ({total})")

    ordered = sorted(files, key=lambda f: (-_churn(f), str(f.get("path", ""))))
    shown = ordered[:max_files]
    width = max((len(_stat_cell(f)) for f in shown), default=0)
    for entry in shown:
        note = mechanical_note(entry)
        suffix = f"  [{note}]" if note else ""
        path = _untrusted.flat(str(entry.get("path", "?")))
        if entry.get("status") == "R" and entry.get("old_path"):
            path = f"{_untrusted.flat(str(entry['old_path']))} -> {path}"
        out.append(f"  {entry.get('status', '?')}  "
                   f"{_stat_cell(entry):>{width}}  {path}{suffix}")
    dropped = total - len(shown)
    if dropped:




        out.append(f"  ... {dropped} more file(s) not shown "
                   f"(cap {max_files}) — raise with GH_PR_DIFF_MAX_FILES=N")

    out.append("")
    ref = number or "N"
    out.append(f"One file's hunks: gh-pr:{ref}:diff:PATH")
    return "\n".join(out), 0


def _entries_sentence(entries: int, *, truncated: bool) -> str:








    head = (f"Assembled from {entries} entries for this path in the fetched "
            f"diff — concatenated")
    if truncated:
        tail = (" in source order, oldest first, so a line changed twice "
                "appears twice, and the current version of it is the last "
                "occurrence in the assembly — which the byte cap below may "
                "not have reached")
    else:
        tail = (" below in source order, oldest first, so a line changed "
                "twice appears twice and the LAST occurrence is the current "
                "one")
    return head + tail + ". A net diff has one entry per path."


def _mechanical_sentence(note: str, *, truncated: bool) -> str:









    if truncated:
        tail = ("the note covers every hunk parsed, but the byte cap below "
                "withheld part of the body, so not all of them follow")
    else:
        tail = "all hunks follow"
    return (f"Note: every hunk in this file is the same edit ({note}) — "
            f"a heuristic, not a filter; {tail}.")


def _one_file(files: list[dict], path: str, header: list[str],
              max_bytes: int) -> tuple[str, int]:
    match = next((f for f in files if str(f.get("path", "")) == path), None)
    if match is None:



        out = list(header)
        out.append(f"Could not show {path!r}: it is not among the "
                   f"{len(files)} file(s) in this PR's diff.")
        out.append("")
        out.append("## Files in this diff")
        for entry in files[:MAX_FILES]:
            out.append(f"  {_untrusted.flat(str(entry.get('path', '?')))}")
        if len(files) > MAX_FILES:
            out.append(f"  ... {len(files) - MAX_FILES} more")
        return "\n".join(out), 1







    renders_hunks = not match.get("binary") and bool(match.get("hunks"))
    body = "\n".join(match["hunks"]) if renders_hunks else ""
    total_bytes = len(body.encode("utf-8", errors="replace"))
    cut = 0
    if renders_hunks and total_bytes > max_bytes:
        body = body.encode("utf-8", errors="replace")[:max_bytes].decode(
            "utf-8", errors="ignore")
        cut = total_bytes - len(body.encode("utf-8", errors="replace"))
    truncated = cut > 0

    out = list(header)
    out.append(f"## {_untrusted.flat(path)}  "
               f"({match.get('status', '?')}, {_stat_cell(match)})")
    entries = int(match.get("entries", 1) or 1)
    if entries > 1:




        out.append(_entries_sentence(entries, truncated=truncated))
    note = mechanical_note(match)
    if note:
        out.append(_mechanical_sentence(note, truncated=truncated))
    if match.get("binary"):
        out.append("Binary file — no textual hunks to show.")
        return "\n".join(out), 0
    if not match.get("hunks"):
        out.append("No hunks in this file's entry "
                   "(mode change or rename with no content change).")
        return "\n".join(out), 0

    if truncated:
        out.append(f"Showing the first {max_bytes} bytes of {total_bytes} — "
                   f"{cut} bytes withheld; raise with GH_PR_DIFF_MAX_BYTES=N")
    out.append(_untrusted.fence(body))
    if truncated:
        out.append(f"{cut} bytes withheld above (cap {max_bytes} bytes of "
                   f"{total_bytes}) — this is NOT the whole file's diff.")
    return "\n".join(out), 0


def render(files: list[dict] | None, *, header: list[str],
           path: str | None = None, reason: str | None = None,
           number: str | None = None,
           max_files: int | None = None,
           max_bytes: int | None = None) -> tuple[str, int]:






    if max_files is None:
        max_files = env_int(os.environ.get("GH_PR_DIFF_MAX_FILES"), "GH_PR_DIFF_MAX_FILES", MAX_FILES, minimum=1)
    if max_bytes is None:
        max_bytes = env_int(os.environ.get("GH_PR_DIFF_MAX_BYTES"), "GH_PR_DIFF_MAX_BYTES", MAX_BYTES, minimum=1)

    if files is None:
        out = list(header)
        out.append("Could not read this PR's diff — the file list below is "
                   "absent because nothing was fetched, NOT because nothing "
                   "changed.")
        out.append(f"Reason: {reason or 'unknown'}")
        out.append("Do not treat this as a reviewed diff.")
        return "\n".join(out), 1

    files = coalesce(files)

    if path:
        return _one_file(files, path, header, max_bytes)
    return _summary(files, header, number, max_files)
