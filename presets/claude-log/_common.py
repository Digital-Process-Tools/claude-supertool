
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  
import _secrets  
import _untrusted  


def encode_cwd(cwd: str) -> str:
















    enc = cwd.replace("\\", "/").replace("/", "-").replace(":", "-")
    if not enc.startswith("-"):
        enc = "-" + enc
    return enc


def claude_projects_root() -> Path:

    return Path.home() / ".claude" / "projects"


class ProjectDir(NamedTuple):













    path: Path
    kind: str
    cwd_of: str
    store_count: int
    asked: str


def _ancestor_paths(cwd: str):







    norm = cwd.replace("\\", "/").rstrip("/")
    parts = norm.split("/")
    for i in range(len(parts) - 1, 0, -1):
        yield "/".join(parts[:i]) or "/"


def resolve_project_dir(cwd: str | None = None) -> ProjectDir:


















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


    try:
        return sum(1 for entry in root.iterdir() if entry.is_dir())
    except OSError:
        return 0


def project_dir(cwd: str | None = None) -> Path:







    return resolve_project_dir(cwd).path


def decline_lines(res: ProjectDir) -> list:






    return [
        "no sessions recorded for this directory",
        f"  directory:  {res.cwd_of}",
        f"  looked for: {res.path}",
        f"  {res.store_count} project store(s) exist under {claude_projects_root()};"
        " none is this directory or an ancestor of it",
    ]


def source_note(res: ProjectDir) -> str:






    if res.kind != "ancestor":
        return ""
    return (
        f"Source: ancestor store — no sessions recorded for {res.asked}; "
        f"showing {res.cwd_of}, which is not the same directory"
    )


def _own_store_desc(res: ProjectDir) -> str:








    if res.kind == "direct":
        return f"{res.path} (this directory's own store)"
    if res.kind == "ancestor":
        return f"{res.path} (the store for the ancestor project {res.cwd_of})"
    return f"{res.path} (no store exists for this directory or any ancestor)"


def session_path(uuid: str) -> "tuple[Path, str]":

































    if not uuid or "/" in uuid or "\\" in uuid or ".." in uuid.split("/") or os.path.isabs(uuid):
        raise ValueError(f"invalid session UUID: {uuid!r}")
    own_res = resolve_project_dir()
    own = own_res.path
    direct = own / f"{uuid}.jsonl"
    if direct.is_file():
        return direct, source_note(own_res)
    root = claude_projects_root()
    if root.is_dir():
        for project in root.iterdir():
            if not project.is_dir():
                continue
            candidate = project / f"{uuid}.jsonl"
            if candidate.is_file():
                if project == own:
                    return candidate, source_note(own_res)
                note = (
                    f"Source: cross-project store — this session was not found "
                    f"under {_own_store_desc(own_res)}; resolved instead "
                    f"from {project} by scanning every store under "
                    f"{claude_projects_root()} (a session id is globally unique)"
                )
                return candidate, note
    return direct, ""


def read_jsonl(path: Path):

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










    if s is None:
        return ""
    s = _untrusted.flat(str(s))
    return s if len(s) <= n else s[: n - 1] + "…"


def wants_raw(args) -> bool:





    return any(str(a).strip().lower() == "raw" for a in args)


class Redactor:







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







        if not self.enabled:
            return ""
        return _secrets.disclosure(self.count if count is None else count)

    @staticmethod
    def markers_in(text: str) -> int:

        return text.count(_secrets.MARKER_PREFIX)


def event_role(d: dict) -> str:

    msg = d.get("message", {}) if isinstance(d.get("message"), dict) else {}
    return msg.get("role") or d.get("type", "")


def event_content_parts(d: dict):

    msg = d.get("message", {}) if isinstance(d.get("message"), dict) else {}
    content = msg.get("content")
    if isinstance(content, list):
        yield from content
    elif isinstance(content, str) and content:
        yield {"type": "text", "text": content}
