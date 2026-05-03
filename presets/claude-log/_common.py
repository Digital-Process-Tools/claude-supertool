"""Shared helpers for claude-log ops."""
import json
import os
from pathlib import Path


def encode_cwd(cwd: str) -> str:
    """Encode a cwd path to the directory name Claude Code uses under ~/.claude/projects/.

    Claude Code names each project directory by replacing path separators with dashes:
    - POSIX:  '/Users/foo/proj'  -> '-Users-foo-proj'
    - Windows:'C:\\Users\\foo'    -> '-C--Users-foo' (drive colon and backslashes both become '-')

    The exact Windows encoding may vary across Claude Code versions; if `project_dir()`
    cannot find a matching directory, callers should fall back to scanning siblings.
    """
    enc = cwd.replace("\\", "/").replace("/", "-").replace(":", "-")
    if not enc.startswith("-"):
        enc = "-" + enc
    return enc


def claude_projects_root() -> Path:
    """Root directory holding all per-project session logs."""
    return Path.home() / ".claude" / "projects"


def project_dir(cwd: str | None = None) -> Path:
    """Resolve the ~/.claude/projects/<encoded-cwd>/ directory for the given (or current) cwd.

    If the directly-encoded directory does not exist, fall back to the closest match
    among siblings (longest common prefix). Returns the encoded path even when missing
    so callers can produce a clear error.
    """
    cwd = cwd if cwd is not None else os.getcwd()
    encoded = encode_cwd(cwd)
    root = claude_projects_root()
    direct = root / encoded
    if direct.exists() or not root.exists():
        return direct
    # Fallback: pick the sibling whose name has the longest common prefix with `encoded`
    best: Path | None = None
    best_len = 0
    for sibling in root.iterdir():
        if not sibling.is_dir():
            continue
        n = _common_prefix_len(sibling.name, encoded)
        if n > best_len:
            best_len = n
            best = sibling
    return best if best is not None else direct


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def session_path(uuid: str) -> Path:
    """Path to a session jsonl file. Prefers the current project; falls back
    to scanning all projects under ~/.claude/projects/ if the UUID is not found
    locally — useful when inspecting sessions from worktrees or other projects
    without changing cwd.
    """
    direct = project_dir() / f"{uuid}.jsonl"
    if direct.is_file():
        return direct
    root = claude_projects_root()
    if root.is_dir():
        for project in root.iterdir():
            if not project.is_dir():
                continue
            candidate = project / f"{uuid}.jsonl"
            if candidate.is_file():
                return candidate
    return direct


def read_jsonl(path: Path):
    """Yield decoded JSON objects from a jsonl file, skipping bad lines."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def trunc(s: str, n: int) -> str:
    """Truncate a string with an ellipsis if it exceeds n chars."""
    if s is None:
        return ""
    s = str(s).replace("\n", " ").replace("\r", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def event_role(d: dict) -> str:
    """Best-effort role extraction from an event."""
    msg = d.get("message", {}) if isinstance(d.get("message"), dict) else {}
    return msg.get("role") or d.get("type", "")


def event_content_parts(d: dict):
    """Yield content parts from a message event (handles list and string content)."""
    msg = d.get("message", {}) if isinstance(d.get("message"), dict) else {}
    content = msg.get("content")
    if isinstance(content, list):
        yield from content
    elif isinstance(content, str) and content:
        yield {"type": "text", "text": content}
