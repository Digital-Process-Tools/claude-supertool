#!/usr/bin/env python3





from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import (  
    Redactor,
    decline_lines,
    event_content_parts,
    read_jsonl,
    resolve_project_dir,
    source_note,
    trunc,
    wants_raw,
)


def first_user_excerpt(path: Path, red: Redactor, max_chars: int = 100) -> str:





    for ev in read_jsonl(path):

        if ev.get("type") == "queue-operation":
            continue
        msg = ev.get("message", {}) if isinstance(ev.get("message"), dict) else {}
        if msg.get("role") != "user":
            continue
        for part in event_content_parts(ev):
            if part.get("type") == "text":
                txt = part.get("text", "")

                if txt.startswith("<") or txt.startswith("# "):
                    continue
                if txt.strip():
                    return trunc(red(txt.strip()), max_chars)
    return ""


def line_count(path: Path) -> int:

    n = 0
    with path.open("rb") as f:
        for _ in f:
            n += 1
    return n


def turn_count(path: Path) -> int:

    n = 0
    for ev in read_jsonl(path):
        if ev.get("type") in ("user", "assistant"):
            n += 1
    return n


def main() -> int:
    limit = 10
    for a in sys.argv[1:]:
        if a.isdigit():
            limit = int(a)
            break
    red = Redactor(enabled=not wants_raw(sys.argv[1:]))

    source = resolve_project_dir()
    if source.kind == "missing":


        for line in decline_lines(source):
            print(line)
        return 1
    pdir = source.path

    sessions = sorted(
        pdir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]

    if not sessions:
        print(f"No sessions found in {pdir}")
        return 0




    rows: list[str] = []
    for sp in sessions:
        uuid = sp.stem
        mtime = datetime.fromtimestamp(sp.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        lines = line_count(sp)
        turns = turn_count(sp)
        excerpt = first_user_excerpt(sp, red)
        rows.append(f"{uuid}  {mtime}  {turns:>5}  {lines:>6}  {excerpt}")

    print(f"Project: {pdir}")
    note = source_note(source)
    if note:
        print(note)
    print(f"Showing {len(sessions)} most recent sessions (of {len(list(pdir.glob('*.jsonl')))})")
    note = red.note()
    if note:
        print(note)
    print()
    print(f"{'UUID':<36}  {'When':<19}  {'Turns':>5}  {'Lines':>6}  First user message")
    print(f"{'-' * 36}  {'-' * 19}  {'-' * 5}  {'-' * 6}  {'-' * 60}")
    for row in rows:
        print(row)

    return 0


if __name__ == "__main__":
    sys.exit(main())
