#!/usr/bin/env python3
"""Tail the last N events of a Claude Code session in compact form.

Output one line per content part:
  [user] TEXT: ...
  [assistant] TOOL Bash: {"command": "..."}
  [result] PASS/FAIL/output: ...
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import event_content_parts, event_role, read_jsonl, session_path, trunc  # noqa: E402


def format_part(role: str, part: dict, width: int) -> str | None:
    """Format one content part as a single compact line."""
    pt = part.get("type")
    if pt == "text":
        txt = part.get("text", "")
        if not txt.strip():
            return None
        return f"[{role}] TEXT: {trunc(txt, width)}"
    if pt == "tool_use":
        name = part.get("name", "?")
        inp = json.dumps(part.get("input", {}), ensure_ascii=False)
        return f"[{role}] TOOL {name}: {trunc(inp, width)}"
    if pt == "tool_result":
        c = part.get("content", "")
        if isinstance(c, list):
            c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
        is_error = part.get("is_error", False)
        prefix = "[result/ERR]" if is_error else "[result]"
        return f"{prefix} {trunc(c, width)}"
    if pt == "thinking":
        # Compact thinking — usually long, mostly skip
        return None
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: claude-log-tail:UUID[:N]")
        return 1

    uuid = sys.argv[1]
    n = 30
    if len(sys.argv) > 2 and sys.argv[2].isdigit():
        n = int(sys.argv[2])

    sp = session_path(uuid)
    if not sp.exists():
        print(f"ERROR: session not found: {sp}")
        return 1

    # Build line list, then keep last N
    lines: list[str] = []
    for ev in read_jsonl(sp):
        if ev.get("type") == "queue-operation":
            # Bootstrap content — show first 200 chars of the original prompt
            content = ev.get("content", "")
            if content:
                lines.append(f"[bootstrap] {trunc(content, 200)}")
            continue
        role = event_role(ev)
        for part in event_content_parts(ev):
            line = format_part(role, part, width=300)
            if line:
                lines.append(line)

    if not lines:
        print(f"No events in {sp}")
        return 0

    tail = lines[-n:]
    total = len(lines)
    print(f"Session: {uuid}")
    print(f"Total events: {total}, showing last {len(tail)}")
    print()
    for ln in tail:
        print(ln)

    return 0


if __name__ == "__main__":
    sys.exit(main())
