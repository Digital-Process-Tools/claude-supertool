"""Shared safety helpers for social-publishing ops (closes #149).

Three guards centralised here so each platform's publish/comment script
gets the same protections:

1. **`file://` allowlist** — `safe_resolve_body_path` forces body files to
   live under `.max/`, `drafts/`, `posts/`, or `blog/` (relative to cwd).
   Closes the credential-exfil vector: `bluesky_publish:file:///Users/.../
   bluesky/app_password` is rejected before the file is read.
2. **Confirmation gate** — `require_confirm` blocks single-shot publishing.
   `|force` per-call or env / JSON opt-out bypasses for batch use.
3. **Token file mode** — `check_token_file_mode` refuses to load a token
   that's group/world-readable (mirrors what `git` enforces for SSH keys).

Each guard supports the same opt-out lookup order Florian asked for:
env var > project `.supertool.json` > default (strict).
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Optional


def _supertool_config() -> dict:
    """Walk up from cwd to find `.supertool.json`. Cached per process."""
    global _CACHED_CONFIG
    try:
        return _CACHED_CONFIG  # type: ignore[has-type]
    except NameError:
        pass
    cfg: dict = {}
    d = Path.cwd().resolve()
    while True:
        candidate = d / ".supertool.json"
        if candidate.is_file():
            try:
                cfg = json.loads(candidate.read_text())
                if not isinstance(cfg, dict):
                    cfg = {}
            except (OSError, json.JSONDecodeError):
                cfg = {}
            break
        if d.parent == d:
            break
        d = d.parent
    _CACHED_CONFIG = cfg  # type: ignore[name-defined]
    return cfg


# --- file:// allowlist ---------------------------------------------------

_DEFAULT_BODY_ALLOWLIST: tuple[str, ...] = (
    ".max/",
    "drafts/",
    "posts/",
    "blog/",
)


def _body_allowlist() -> tuple[Path, ...]:
    """Allowlist of dirs (resolved absolute) where publish bodies may live.

    Additive sources:
      1. `_DEFAULT_BODY_ALLOWLIST`
      2. `$SUPERTOOL_PUBLISH_BODY_ALLOWLIST` env (colon-separated)
      3. `"publish_body_allowlist": [...]` in `.supertool.json`
    """
    paths = list(_DEFAULT_BODY_ALLOWLIST)
    extra = os.environ.get("SUPERTOOL_PUBLISH_BODY_ALLOWLIST", "")
    if extra:
        paths.extend(p for p in extra.split(":") if p)
    cfg_extra = _supertool_config().get("publish_body_allowlist")
    if isinstance(cfg_extra, list):
        paths.extend(str(p) for p in cfg_extra if isinstance(p, str))
    cwd = Path.cwd().resolve()
    out: list[Path] = []
    for p in paths:
        try:
            out.append((cwd / p).resolve())
        except OSError:
            continue
    return tuple(out)


def safe_resolve_body_path(arg: str) -> Path:
    """Resolve a `file://` or bare path to a Path inside the allowlist.

    Exits cleanly (sys.exit(2)) when the resolved path escapes the allowlist.
    Caller is responsible for verifying the path exists.
    """
    raw_path = arg[len("file://"):] if arg.startswith("file://") else arg
    try:
        resolved = Path(raw_path).resolve()
    except OSError:
        sys.stderr.write(f"ERROR: cannot resolve body path: {raw_path!r}\n")
        sys.exit(2)
    for allowed in _body_allowlist():
        try:
            resolved.relative_to(allowed)
            return resolved
        except ValueError:
            continue
    rel = ", ".join(p.name + "/" for p in _body_allowlist()[:4])
    sys.stderr.write(
        f"ERROR: publish body path escapes the safety allowlist: {raw_path!r}\n"
        f"  resolved to: {resolved}\n"
        f"  allowed dirs (relative to cwd): {rel}\n"
        f"  Extend (additive): SUPERTOOL_PUBLISH_BODY_ALLOWLIST=path1:path2\n"
        f"    or `\"publish_body_allowlist\": [\"path1\"]` in .supertool.json\n"
    )
    sys.exit(2)


# --- confirmation gate ---------------------------------------------------

def require_confirm(action: str, preview: str, *, force: bool = False) -> None:
    """Bail unless explicitly opted out — blocks single-shot LLM publish.

    Opt-out (any of):
      1. `force=True` per-call (existing `|force` suffix)
      2. `SUPERTOOL_NO_PUBLISH_CONFIRM=1` env
      3. `"no_publish_confirm": true` in `.supertool.json`
    """
    if force:
        return
    if os.environ.get("SUPERTOOL_NO_PUBLISH_CONFIRM") == "1":
        return
    if bool(_supertool_config().get("no_publish_confirm")):
        return
    head = preview if len(preview) <= 200 else preview[:197] + "..."
    sys.stderr.write(
        f"ERROR: {action} requires explicit confirmation.\n"
        f"  Preview: {head!r}\n"
        f"  To proceed: append |force, set SUPERTOOL_NO_PUBLISH_CONFIRM=1,\n"
        f"  or add `\"no_publish_confirm\": true` to .supertool.json.\n"
    )
    sys.exit(2)


# --- token file mode check -----------------------------------------------

def check_token_file_mode(path: Path) -> None:
    """Exit cleanly if a credential file is group/world-readable.

    Mirrors `ssh`'s refusal to use an insecure key. Caller passes a Path —
    we no-op if the file doesn't exist (caller surfaces the right error).
    """
    try:
        st = os.stat(path)
    except OSError:
        return
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        sys.stderr.write(
            f"ERROR: token file {path} has loose permissions ({oct(mode)}).\n"
            f"  Tighten with: chmod 600 {path}\n"
            f"  (Other users on this machine can currently read your token.)\n"
        )
        sys.exit(2)
