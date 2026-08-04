"""Shared helpers for claude-log ops."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for _secrets (#760)
import _secrets  # noqa: E402


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

    Security: reject UUIDs that contain path separators, traversal segments,
    or an absolute-path prefix. Python's pathlib treats `Path("a") / "/abs"`
    as `/abs` (left side discarded), so an unvalidated UUID like
    `/tmp/anything` would let the op read `/tmp/anything.jsonl` — any .jsonl
    on the filesystem. Reject anything that isn't a plain identifier-like
    string before constructing the path.
    """
    if not uuid or "/" in uuid or "\\" in uuid or ".." in uuid.split("/") or os.path.isabs(uuid):
        raise ValueError(f"invalid session UUID: {uuid!r}")
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
    """Truncate a string with an ellipsis if it exceeds n chars.

    Deliberately does NOT redact. #760 named this function as one of four
    leaking surfaces, but it is a formatter, not a surface — its callers are.
    Redacting here would be invisible from any call site, would double-count
    values that pass through twice, and would run AFTER the caller had already
    chosen what to print. Callers redact first, then truncate: truncating
    first can cut a key in half and leave its head in the output, which reads
    as safe and is not.
    """
    if s is None:
        return ""
    s = str(s).replace("\n", " ").replace("\r", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def wants_raw(args) -> bool:
    """True when the caller passed the `raw` token, opting out of redaction.

    Accepted in any argument position so it composes with the optional numeric
    argument the ops already take: `UUID:50:raw` and `UUID:raw` both work.
    """
    return any(str(a).strip().lower() == "raw" for a in args)


class Redactor:
    """Applies `_secrets.redact` to every string an op is about to print, and
    keeps the running count that the disclosure line reports.

    Disabled (`raw`) it is the identity function, so the pre-#760 verbatim
    contract is one keystroke away rather than gone.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.count = 0

    def __call__(self, s):
        if s is None:
            return ""
        if not self.enabled:
            return s
        out, n = _secrets.redact(s)
        self.count += n
        return out

    def note(self, count=None) -> str:
        """Disclosure line, or "" when nothing was redacted.

        `count` overrides the running total. tail.py needs that: it redacts
        while building every line, then prints only the last N, so the running
        total can promise markers that were scrolled off. Reporting a number
        larger than what is on screen is its own small lie.
        """
        if not self.enabled:
            return ""
        return _secrets.disclosure(self.count if count is None else count)

    @staticmethod
    def markers_in(text: str) -> int:
        """How many redaction markers a rendered chunk actually contains."""
        return text.count(_secrets.MARKER_PREFIX)


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
