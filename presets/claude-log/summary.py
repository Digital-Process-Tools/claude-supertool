#!/usr/bin/env python3
"""Whole-session digest: model, duration, tokens, tool calls, errors, final text."""
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import event_content_parts, event_role, read_jsonl, session_path, trunc  # noqa: E402


def _parse_ts(ts: str) -> datetime | None:
    """Parse an ISO-8601 timestamp ending in 'Z' or with offset."""
    if not ts:
        return None
    try:
        # Python's fromisoformat accepts offsets but not the 'Z' suffix prior to 3.11
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, seconds = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: usage: claude-log-summary:UUID")
        return 1

    uuid = sys.argv[1]
    sp = session_path(uuid)
    if not sp.exists():
        print(f"ERROR: session not found: {sp}")
        return 1

    tool_counts: Counter[str] = Counter()
    error_results = 0
    error_by_tool_position: list[str] = []  # tool name preceding each error result
    last_tool_name: str | None = None
    user_msgs = 0
    assistant_msgs = 0
    bootstrap_chars = 0
    last_assistant_text = ""
    first_user_text = ""

    # Token + meta tracking
    tokens_in = 0
    tokens_out = 0
    tokens_cache_read = 0
    tokens_cache_creation = 0
    model = ""
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    for ev in read_jsonl(sp):
        if ev.get("type") == "queue-operation":
            bootstrap_chars += len(ev.get("content", ""))
            continue

        ts = _parse_ts(ev.get("timestamp", ""))
        if ts is not None:
            if first_ts is None:
                first_ts = ts
            last_ts = ts

        msg = ev.get("message", {}) if isinstance(ev.get("message"), dict) else {}
        if not model and msg.get("model"):
            model = msg["model"]

        usage = msg.get("usage")
        if isinstance(usage, dict):
            tokens_in += int(usage.get("input_tokens") or 0)
            tokens_out += int(usage.get("output_tokens") or 0)
            tokens_cache_read += int(usage.get("cache_read_input_tokens") or 0)
            tokens_cache_creation += int(usage.get("cache_creation_input_tokens") or 0)

        role = event_role(ev)
        if role == "user":
            user_msgs += 1
        elif role == "assistant":
            assistant_msgs += 1

        for part in event_content_parts(ev):
            pt = part.get("type")
            if pt == "tool_use":
                name = part.get("name", "?")
                tool_counts[name] += 1
                last_tool_name = name
            elif pt == "tool_result":
                if part.get("is_error"):
                    error_results += 1
                    if last_tool_name:
                        error_by_tool_position.append(last_tool_name)
            elif pt == "text":
                txt = part.get("text", "")
                if role == "assistant" and txt.strip():
                    last_assistant_text = txt
                elif role == "user" and txt.strip() and not first_user_text:
                    if not (txt.startswith("<") or txt.startswith("# ")):
                        first_user_text = txt

    total_tools = sum(tool_counts.values())
    total_tokens = tokens_in + tokens_out + tokens_cache_read + tokens_cache_creation
    cache_inputs = tokens_cache_read + tokens_cache_creation
    cache_hit_pct = (tokens_cache_read / cache_inputs * 100) if cache_inputs else 0.0
    duration = (last_ts - first_ts).total_seconds() if first_ts and last_ts else 0.0
    error_tool_counts = Counter(error_by_tool_position)

    print(f"Session: {uuid}")
    print(f"File:    {sp}")
    print()
    if model:
        print(f"Model:           {model}")
    if duration:
        print(f"Duration:        {_format_duration(duration)}")
    print(f"Turns:           user={user_msgs}  assistant={assistant_msgs}")
    print(f"Tool calls:      {total_tools}")
    print(f"Tool errors:     {error_results}")
    if bootstrap_chars:
        print(f"Bootstrap chars: {bootstrap_chars}")
    print()

    if total_tokens:
        print("Tokens:")
        print(f"  input:          {_format_tokens(tokens_in)}")
        print(f"  output:         {_format_tokens(tokens_out)}")
        print(f"  cache read:     {_format_tokens(tokens_cache_read)}")
        print(f"  cache create:   {_format_tokens(tokens_cache_creation)}")
        print(f"  total:          {_format_tokens(total_tokens)}")
        if cache_inputs:
            print(f"  cache hit:      {cache_hit_pct:.1f}%")
        print()

    if tool_counts:
        print("Tool usage:")
        for name, count in tool_counts.most_common():
            err = error_tool_counts.get(name, 0)
            tag = f" ({err} err)" if err else ""
            print(f"  {count:>4}  {name}{tag}")
        print()

    if first_user_text:
        print("First user message:")
        print(f"  {trunc(first_user_text, 300)}")
        print()

    if last_assistant_text:
        print("Final assistant text:")
        for line in last_assistant_text.splitlines()[:20]:
            print(f"  {line}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
