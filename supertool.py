#!/usr/bin/env python3
"""
supertool — Batch file operations for autonomous Claude Code runs.

WHY THIS EXISTS
---------------
Each separate tool round-trip re-pays the cached prefix (system prompt +
rules + tool schemas + prior turns). Anthropic prompt caching is real and
billed at 10% of input price, so re-pay is NOT free but also NOT full
re-pay. Still worth batching.

Per saved round-trip (3 separate reads vs 1 SuperTool call, 50K prefix,
2K per file):
    Cache reads:    156.9K → 50K      (-106.9K raw, -10.7K effective at 10%)
    Output tokens:  900 → 400         (not cached, billed at 5x input rate)
    Round-trips:    3 → 1             (-2-6s wall time)
    Final context:  identical         (same file bytes either way)

Dollars per batch: ~$0.04 Sonnet, ~$0.19 Opus. Compounds across many
batches per autonomous run.

USAGE — BATCH AS MANY OPS AS YOU CAN ANTICIPATE
-----------------------------------------------
There is no limit on ops per call. Pack every read, grep, and glob you
expect to need this turn. Two ops is NOT the cap — six is routine.

Realistic batch (7 ops, 1 round-trip) — ALWAYS quote args to prevent
shell glob expansion:
    supertool \\
        'read:src/SiX/SiXModule.py' \\
        'read:src/SiX/SiXPermissions.py' \\
        'read:src/SiX/SiXOptions.py' \\
        'grep:extends:src/SiX/:20' \\
        'grep:@related:src/SiX/:10' \\
        'glob:src/SiX/Components/**/*.xml' \\
        'glob:src/SiX/EventsManagers/*.py'

OPERATIONS
----------
    read:PATH                  Read file (first 300 lines, 20KB cap)
    read:PATH:OFFSET:LIMIT     Read with offset and line limit
    grep:PATTERN:PATH          Search pattern (10 results default).
                                Auto-reads full file if PATH is a concrete
                                file < 20KB with a match.
    grep:PATTERN:PATH:LIMIT    Search with custom result limit
    glob:PATTERN               Find files matching pattern (** supported).
                                Auto-reads if PATTERN is a concrete file
                                path with no wildcards.
    ls:PATH                    List directory entries
    tail:PATH:N                Last N lines (default 20)
    head:PATH:N                First N lines (default 20)

Output: structured text with --- separators per operation.
Calls logged to {tempdir}/supertool-calls.log for per-turn analysis
(macOS: /var/folders/.../T/, Linux: /tmp/, Windows: %TEMP%).
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

MAX_READ_LINES = 300
MAX_READ_BYTES = 20000  # ~20KB cap — prevents Claude Code "Output too large"
MAX_GREP_RESULTS = 10
MAX_GLOB_RESULTS = 50
LOG_FILE = os.path.join(tempfile.gettempdir(), "supertool-calls.log")
GREP_FILE_INCLUDES = ("*.php", "*.xml", "*.py", "*.js", "*.ts", "*.md")
WILDCARD_CHARS = re.compile(r"[*?\[]")

# Enforcement — pre-tool-block hook reads this state file (absent = permissive)
ENFORCE_STATE_FILE = os.path.expanduser("~/.claude/supertool-enforced")

# Tools blocked when enforcement is active
BLOCKED_TOOLS = {"Grep", "Glob", "LS"}
BLOCKED_BASH_COMMANDS = {"cat", "find", "grep", "ls", "sed", "awk", "tail", "head"}


# ---------------------------------------------------------------------------
# Core operations (pure functions — all return the string to emit)
# ---------------------------------------------------------------------------

def render_file(path: str, offset: int = 0, limit: int = MAX_READ_LINES) -> str:
    """Emit a file's contents with line numbers, truncated at caps.

    Shared by read: and by grep/glob auto-promote branches.
    """
    if not path or not os.path.isfile(path):
        return f"ERROR: file not found: {path}\n"

    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            raw_lines = f.read().splitlines(keepends=True)
    except OSError as e:
        return f"ERROR: could not read {path}: {e}\n"

    line_count = len(raw_lines)
    out = [f"({line_count} lines, {size} bytes)\n"]
    bytes_emitted = 0
    printed = 0
    end = min(offset + limit, line_count)

    for i in range(offset, end):
        try:
            line = raw_lines[i].decode("utf-8", errors="replace")
        except Exception:
            line = "<binary line>\n"
        numbered = f"{i + 1:>6}→{line}"
        out.append(numbered)
        bytes_emitted += len(numbered)
        printed += 1
        if bytes_emitted >= MAX_READ_BYTES:
            break

    if bytes_emitted >= MAX_READ_BYTES:
        out.append(f"... (truncated at {MAX_READ_BYTES} bytes — use "
                   "read:PATH:OFFSET:LIMIT to get more)\n")
    elif offset + printed < line_count:
        out.append(f"... ({line_count - offset - printed} more lines)\n")
    out.append("\n")
    return "".join(out)


def op_read(path: str, offset: int = 0, limit: int = MAX_READ_LINES) -> str:
    return render_file(path, offset, limit)


def op_grep(pattern: str, path: str = ".", limit: int = MAX_GREP_RESULTS) -> str:
    """Search pattern recursively. Auto-reads small single file on match."""
    if not pattern:
        return "ERROR: empty pattern\n"

    hits = _grep_recursive(pattern, path, limit)
    count = len(hits)

    out = [f"({count} results, limit {limit})\n"]
    for hit in hits:
        out.append(hit + "\n")
    out.append("\n")

    # Auto-read: single small file + at least one match → emit full file
    if (count > 0
            and os.path.isfile(path)
            and os.path.getsize(path) < MAX_READ_BYTES):
        out.append(f"[auto-read: single file < {MAX_READ_BYTES} bytes, "
                   "match found]\n")
        out.append(render_file(path, 0, MAX_READ_LINES))

    return "".join(out)


def op_glob(pattern: str) -> str:
    """Find files matching pattern. Auto-reads concrete file paths."""
    if not pattern:
        return "ERROR: empty pattern\n"

    # Auto-promote: concrete path with no wildcards that points to a file
    if not WILDCARD_CHARS.search(pattern) and os.path.isfile(pattern):
        return ("[auto-read: concrete path, no wildcards]\n"
                + render_file(pattern, 0, MAX_READ_LINES))

    files = _glob_files(pattern)
    out = [f"({len(files)} files)\n"]
    for f in files:
        out.append(f + "\n")
    out.append("\n")
    return "".join(out)


def op_ls(path: str = ".") -> str:
    if not os.path.isdir(path):
        return f"ERROR: not a directory: {path}\n"
    try:
        items = sorted(os.listdir(path))
    except OSError as e:
        return f"ERROR: could not list {path}: {e}\n"
    out = [f"({len(items)} items)\n"]
    for item in items:
        full = os.path.join(path, item)
        marker = "/" if os.path.isdir(full) else ""
        out.append(f"{item}{marker}\n")
    out.append("\n")
    return "".join(out)


def op_tail(path: str, n: int = 20) -> str:
    if not path or not os.path.isfile(path):
        return f"ERROR: file not found: {path}\n"
    with open(path, "rb") as f:
        raw_lines = f.read().splitlines(keepends=True)
    total = len(raw_lines)
    start = max(0, total - n)
    out = [f"({total} lines total, showing last {n})\n"]
    for i in range(start, total):
        try:
            line = raw_lines[i].decode("utf-8", errors="replace")
        except Exception:
            line = "<binary line>\n"
        out.append(f"{i + 1:>6}→{line}")
    out.append("\n")
    return "".join(out)


def op_head(path: str, n: int = 20) -> str:
    if not path or not os.path.isfile(path):
        return f"ERROR: file not found: {path}\n"
    with open(path, "rb") as f:
        raw_lines = f.read().splitlines(keepends=True)
    total = len(raw_lines)
    limit = min(n, total)
    out = [f"({total} lines total, showing first {limit})\n"]
    for i in range(limit):
        try:
            line = raw_lines[i].decode("utf-8", errors="replace")
        except Exception:
            line = "<binary line>\n"
        out.append(f"{i + 1:>6}→{line}")
    out.append("\n")
    return "".join(out)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _grep_recursive(pattern: str, path: str, limit: int) -> List[str]:
    """Return up to `limit` match lines as 'path:lineno:content' strings.

    Filters by common code/doc extensions when walking directories.
    Always searches when `path` is a single file.
    """
    try:
        regex = re.compile(pattern)
    except re.error:
        # Fall back to literal substring
        regex = re.compile(re.escape(pattern))

    results: List[str] = []
    candidates: List[str] = []

    if os.path.isfile(path):
        candidates.append(path)
    elif os.path.isdir(path):
        for root, _dirs, files in os.walk(path):
            for name in files:
                if any(name.endswith(ext.lstrip("*")) for ext in GREP_FILE_INCLUDES):
                    candidates.append(os.path.join(root, name))
    else:
        return results

    for file_path in candidates:
        if len(results) >= limit:
            break
        try:
            with open(file_path, "rb") as f:
                for lineno, raw in enumerate(f, start=1):
                    try:
                        line = raw.decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    if regex.search(line):
                        results.append(f"{file_path}:{lineno}:{line.rstrip()}")
                        if len(results) >= limit:
                            break
        except OSError:
            continue
    return results


def _glob_files(pattern: str) -> List[str]:
    """Glob matching files, supports ** recursive. Returns up to MAX_GLOB_RESULTS."""
    from glob import glob
    matches = sorted(glob(pattern, recursive=True))
    files = [m for m in matches if os.path.isfile(m)]
    return files[:MAX_GLOB_RESULTS]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_DRIVE_LETTER = re.compile(r"^[A-Za-z]$")


def _split_arg(arg: str) -> List[str]:
    """Split 'op:arg1:arg2:arg3' by ':' but reassemble Windows drive letters.

    Splits on every ':' (no limit) then merges single-letter pieces that
    are followed by a slash/backslash-starting piece — that's a drive letter.

    Examples:
        'read:foo.py'              → ['read', 'foo.py']
        'read:C:\\Users\\file.py'  → ['read', 'C:\\Users\\file.py']
        'grep:pat:C:/src:20'       → ['grep', 'pat', 'C:/src', '20']
        'grep:pat:C:\\src'         → ['grep', 'pat', 'C:\\src']
    """
    raw = arg.split(":")  # Full split — drive letters will be rejoined below
    tokens: List[str] = []
    i = 0
    while i < len(raw):
        piece = raw[i]
        # Drive-letter detection: single letter + next piece begins with / or \
        if (i + 1 < len(raw)
                and _DRIVE_LETTER.match(piece)
                and raw[i + 1]
                and raw[i + 1][0] in ("/", "\\")):
            tokens.append(f"{piece}:{raw[i + 1]}")
            i += 2
        else:
            tokens.append(piece)
            i += 1
    return tokens


def dispatch(arg: str) -> str:
    """Parse 'op:arg1:arg2:...' and route to the matching op function."""
    header = f"--- {arg} ---\n"
    parts = _split_arg(arg)
    op = parts[0] if parts else ""

    try:
        if op == "read":
            path = parts[1] if len(parts) > 1 else ""
            offset = int(parts[2]) if len(parts) > 2 and parts[2] else 0
            limit = int(parts[3]) if len(parts) > 3 and parts[3] else MAX_READ_LINES
            body = op_read(path, offset, limit)
        elif op == "grep":
            pattern = parts[1] if len(parts) > 1 else ""
            path = parts[2] if len(parts) > 2 and parts[2] else "."
            limit = int(parts[3]) if len(parts) > 3 and parts[3] else MAX_GREP_RESULTS
            body = op_grep(pattern, path, limit)
        elif op == "glob":
            pattern = parts[1] if len(parts) > 1 else ""
            body = op_glob(pattern)
        elif op == "ls":
            path = parts[1] if len(parts) > 1 and parts[1] else "."
            body = op_ls(path)
        elif op == "tail":
            path = parts[1] if len(parts) > 1 else ""
            n = int(parts[2]) if len(parts) > 2 and parts[2] else 20
            body = op_tail(path, n)
        elif op == "head":
            path = parts[1] if len(parts) > 1 else ""
            n = int(parts[2]) if len(parts) > 2 and parts[2] else 20
            body = op_head(path, n)
        else:
            body = (f"ERROR: unknown operation: {op}\n"
                    f"Valid operations: read, grep, glob, ls, tail, head\n")
    except (ValueError, IndexError) as e:
        body = f"ERROR: argument parsing: {e}\n"

    return header + body


def caller_tag() -> str:
    """Build a short caller identity string for the log line.

    Claude Code doesn't expose session_id in env to Bash tools (it only
    appears in hook stdin payloads). The best session-stable proxy we have
    is PPID — the parent bash's PID stays the same within one Claude Code
    session, so grouping by ppid gives per-session totals.
    """
    user = os.environ.get("USER", "?")
    ppid = os.getppid()
    entry = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "?")
    return f"user={user} ppid={ppid} entry={entry}"


# ---------------------------------------------------------------------------
# PreToolUse hook — pure logic, testable without stdin/env
# ---------------------------------------------------------------------------

def pre_tool_hook(payload: Dict[str, Any], enforced: bool) -> Tuple[int, str]:
    """Decide whether a tool call should be blocked.

    Args:
        payload: Claude Code hook payload (parsed JSON). Interesting keys:
            - tool_name: str (e.g. "Grep", "Bash")
            - tool_input.command: str (for Bash, the shell command)
        enforced: Whether enforcement is active (state file present).

    Returns:
        (exit_code, stderr_message). exit_code 0 = allow, 2 = block.
        stderr_message is shown to the model when blocked.
    """
    # Permissive mode: never block.
    if not enforced:
        return 0, ""

    tool_name = payload.get("tool_name", "")

    # Direct tool blocks
    if tool_name in BLOCKED_TOOLS:
        return 2, (
            f"Use ./supertool instead of {tool_name}.\n\n"
            "  ./supertool 'grep:PATTERN:PATH:LIMIT'\n"
            "  ./supertool 'glob:PATTERN'   (supports **)\n"
            "  ./supertool 'ls:PATH'\n\n"
            "Batch multiple ops in one call: "
            "./supertool 'read:A' 'read:B' 'grep:X:src/' 'glob:**/*.md'\n\n"
            "Disable enforcement: /supertool off\n"
        )

    # Bash command inspection
    if tool_name == "Bash":
        command = payload.get("tool_input", {}).get("command", "")
        # First token is the binary being invoked; handle leading whitespace.
        first_token = command.strip().split()[0] if command.strip() else ""
        # Strip leading env-var assignments (e.g. "FOO=1 grep ...") — check
        # the first real command token.
        while "=" in first_token and not first_token.startswith("="):
            # Looks like VAR=value; advance to next token
            tokens = command.strip().split()
            if len(tokens) < 2:
                break
            command = " ".join(tokens[1:])
            first_token = tokens[1]
        if first_token in BLOCKED_BASH_COMMANDS:
            return 2, (
                f"Bash({first_token} ...) is blocked while supertool "
                "enforcement is active.\n\n"
                "Use ./supertool instead:\n"
                "  cat FILE         → ./supertool 'read:FILE'\n"
                "  grep PAT PATH    → ./supertool 'grep:PAT:PATH:LIMIT'\n"
                "  find/glob        → ./supertool 'glob:PATTERN'\n"
                "  ls PATH          → ./supertool 'ls:PATH'\n"
                "  tail -N FILE     → ./supertool 'tail:FILE:N'\n"
                "  head -N FILE     → ./supertool 'head:FILE:N'\n"
                "  sed -n X,Yp FILE → ./supertool 'read:FILE:X:Y-X'\n\n"
                "Batch multiple ops in one call. "
                "Disable enforcement: /supertool off\n"
            )

    return 0, ""


def is_enforced() -> bool:
    """Check whether the enforcement state file is present."""
    return os.path.isfile(ENFORCE_STATE_FILE)


def log_call(args: List[str], out_bytes: int) -> None:
    """Append timestamped call log with caller id + output size.

    The ops count and out_bytes let post-analysis compute per-call cost and
    estimate round-trips saved vs a naive (one-op-per-call) baseline.
    """
    try:
        with open(LOG_FILE, "a") as f:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            meta = f"ops={len(args)} out={out_bytes}b"
            f.write(f"{timestamp} | {caller_tag()} | {meta} | {' '.join(args)}\n")
    except OSError:
        pass  # Logging is best-effort


def main(argv: List[str]) -> int:
    if not argv:
        sys.stderr.write(
            "Usage: supertool op:args [op:args ...]\n"
            "       supertool 'read:file.py' 'grep:foo:src/:20' 'glob:**/*.md'\n"
            "       supertool --pre-tool-hook  (reads hook payload from stdin)\n"
        )
        return 1

    # Plugin hook mode — invoked by Claude Code's PreToolUse hook
    if argv[0] == "--pre-tool-hook":
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError:
            # Malformed input — allow the tool call to proceed (fail-open)
            return 0
        code, message = pre_tool_hook(payload, is_enforced())
        if message:
            sys.stderr.write(message)
        return code

    # Normal batched-ops mode
    total_out_bytes = 0
    for arg in argv:
        body = dispatch(arg)
        sys.stdout.write(body)
        total_out_bytes += len(body.encode("utf-8"))
    log_call(argv, total_out_bytes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
