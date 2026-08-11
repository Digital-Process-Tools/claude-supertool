"""Shared helpers for claude-log ops."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for _secrets (#760)
import _secrets  # noqa: E402


def encode_cwd(cwd: str) -> str:
    """Encode a cwd path to the directory name Claude Code uses under ~/.claude/projects/.

    Claude Code names each project directory by replacing path separators with dashes:
    - POSIX:  '/Users/foo/proj'  -> '-Users-foo-proj'
    - Windows:'C:\\Users\\foo'    -> '-C--Users-foo' (drive colon and backslashes both become '-')

    The exact Windows encoding may vary across Claude Code versions.

    The encoding is LOSSY and not ours: `/`, `-` and `:` all become `-`, so
    `/Users/foo/bar` and `/Users/foo-bar` produce the same store name and
    nothing on disk says which directory wrote it. That ambiguity is a property
    of the name Claude Code chose and applies to a direct hit exactly as much as
    to an ancestor — `resolve_project_dir()` cannot resolve it and does not
    claim to. What it does guarantee is the #1317 half: it never walks to a
    store that is neither this cwd's encoding nor an ancestor's.
    """
    enc = cwd.replace("\\", "/").replace("/", "-").replace(":", "-")
    if not enc.startswith("-"):
        enc = "-" + enc
    return enc


def claude_projects_root() -> Path:
    """Root directory holding all per-project session logs."""
    return Path.home() / ".claude" / "projects"


class ProjectDir(NamedTuple):
    """Where a claude-log op read its sessions from, and how sure that is.

    Three states, never two (`docs/validators.md` — "Declining instead of
    guessing"):

    - `direct`   — this cwd has its own store. The answer.
    - `ancestor` — the cwd is inside a project whose store exists; `cwd_of`
                   names that project. Legitimate, but it is NOT the directory
                   asked about, so every render has to say so.
    - `missing`  — nothing at or above this cwd has a store. `path` is where
                   one would live; it does not exist. Callers decline.
    """

    path: Path
    kind: str
    cwd_of: str
    store_count: int
    asked: str


def _ancestor_paths(cwd: str):
    """Yield the ancestors of `cwd`, nearest first, as encodable path strings.

    Derived from the string, not from `pathlib`: on POSIX a backslash-separated
    Windows path is a single component with no parents, so a `.parents` walk
    would resolve on Windows and decline on macOS for the same input — a
    platform-shaped answer rather than an answer.
    """
    norm = cwd.replace("\\", "/").rstrip("/")
    parts = norm.split("/")
    for i in range(len(parts) - 1, 0, -1):
        yield "/".join(parts[:i]) or "/"


def resolve_project_dir(cwd: str | None = None) -> ProjectDir:
    """Resolve ~/.claude/projects/<encoded-cwd>/ for the given (or current) cwd.

    Upward only. Until #1317 the fallback picked the store whose encoded name
    shared the longest common PREFIX, which walks sideways: from
    `~/Documents/st-wt/1317` with no store of its own it returned
    `-Users-floriandavid-Documents-st-wt-1024`, and every op in the family
    rendered a plausible board about another worktree's sessions with nothing
    in the output naming the substitution.

    A sibling is never an answer to "what happened in this directory".

    Known limit, inherited from `encode_cwd` and not from this walk: the
    encoding collapses `/` and `-`, so a store written by `/Users/foo-bar` is
    indistinguishable from one written by the ancestor `/Users/foo/bar` and
    would be returned as `ancestor`. A `direct` hit is ambiguous in precisely
    the same way, so no resolution strategy over these names can close it;
    pinned by `test_encode_cwd_collision_is_not_closed_by_this_fix`.
    """
    cwd = cwd if cwd is not None else os.getcwd()
    root = claude_projects_root()
    direct = root / encode_cwd(cwd)
    if direct.exists():
        return ProjectDir(direct, "direct", cwd, _store_count(root), cwd)
    for ancestor in _ancestor_paths(cwd):
        candidate = root / encode_cwd(ancestor)
        if candidate != direct and candidate.is_dir():
            return ProjectDir(candidate, "ancestor", ancestor, _store_count(root), cwd)
    return ProjectDir(direct, "missing", cwd, _store_count(root), cwd)


def _store_count(root: Path) -> int:
    """How many project stores exist at all — reported next to a decline so
    "none of them is this directory" is distinguishable from "there are none"."""
    try:
        return sum(1 for entry in root.iterdir() if entry.is_dir())
    except OSError:
        return 0


def project_dir(cwd: str | None = None) -> Path:
    """The store directory for `cwd`, or the path where it would live.

    Kept for callers that only need a path (`session_path`). Anything that
    prints should use `resolve_project_dir()` and render its `kind`: a bare
    Path cannot tell a direct hit from an ancestor, and that difference is the
    whole of #1317.
    """
    return resolve_project_dir(cwd).path


def decline_lines(res: ProjectDir) -> list:
    """The `missing` render, shared by every op in the family.

    Names the directory asked about, the store that would hold its sessions,
    and how many stores exist — so "none of them is yours" cannot be read as
    "there are none", and neither can be read as an empty board.
    """
    return [
        "no sessions recorded for this directory",
        f"  directory:  {res.cwd_of}",
        f"  looked for: {res.path}",
        f"  {res.store_count} project store(s) exist under {claude_projects_root()};"
        " none is this directory or an ancestor of it",
    ]


def source_note(res: ProjectDir) -> str:
    """One line naming an ancestor substitution, or "" for a direct hit.

    An ancestor's store is a legitimate answer and still not the directory the
    caller stood in, so the substitution is stated rather than implied by a
    `Project:` path the reader has to diff against their own cwd.
    """
    if res.kind != "ancestor":
        return ""
    return (
        f"Source: ancestor store — no sessions recorded for {res.asked}; "
        f"showing {res.cwd_of}, which is not the same directory"
    )


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
