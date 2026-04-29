#!/usr/bin/env python3
"""List N most recent Claude Code sessions for the current project.

For each session, output: UUID, mtime, line count, first user-message excerpt.
Useful to pick the right UUID before running claude-log-tail / claude-log-summary.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import event_content_parts, project_dir, read_jsonl, trunc  # noqa: E402


def first_user_excerpt(path: Path, max_chars: int = 100) -> str:
    """Find the first user-typed text in a session, skipping system prompts."""
    for ev in read_jsonl(path):
        # Skip queue-operation entries (system bootstrap content)
        if ev.get("type") == "queue-operation":
            continue
        msg = ev.get("message", {}) if isinstance(ev.get("message"), dict) else {}
        if msg.get("role") != "user":
            continue
        for part in event_content_parts(ev):
            if part.get("type") == "text":
                txt = part.get("text", "")
                # Skip system reminders / hook context
                if txt.startswith("<") or txt.startswith("# "):
                    continue
                if txt.strip():
                    return trunc(txt.strip(), max_chars)
    return ""


def line_count(path: Path) -> int:
    """Quick line count without loading whole file."""
    n = 0
    with path.open("rb") as f:
        for _ in f:
            n += 1
    return n


def turn_count(path: Path) -> int:
    """Count user + assistant messages (skipping bootstrap entries)."""
    n = 0
    for ev in read_jsonl(path):
        if ev.get("type") in ("user", "assistant"):
            n += 1
    return n


def main() -> int:
    limit = 10
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        limit = int(sys.argv[1])

    pdir = project_dir()
    if not pdir.exists():
        print(f"ERROR: no Claude project log dir at {pdir}")
        return 1

    sessions = sorted(
        pdir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]

    if not sessions:
        print(f"No sessions found in {pdir}")
        return 0

    print(f"Project: {pdir}")
    print(f"Showing {len(sessions)} most recent sessions (of {len(list(pdir.glob('*.jsonl')))})")
    print()
    print(f"{'UUID':<36}  {'When':<19}  {'Turns':>5}  {'Lines':>6}  First user message")
    print(f"{'-' * 36}  {'-' * 19}  {'-' * 5}  {'-' * 6}  {'-' * 60}")

    for sp in sessions:
        uuid = sp.stem
        mtime = datetime.fromtimestamp(sp.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        lines = line_count(sp)
        turns = turn_count(sp)
        excerpt = first_user_excerpt(sp)
        print(f"{uuid}  {mtime}  {turns:>5}  {lines:>6}  {excerpt}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
