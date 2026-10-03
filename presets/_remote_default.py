#!/usr/bin/env python3
"""Resolve a default GitLab project / GitHub repo for preset payload ops.

Shared by ``gitlab/issue_create.py`` (``project``) and
``github/issue_create.py`` (``repo``) so the common case needs no per-payload
boilerplate.

Resolution order (most specific wins):

  1. explicit value in the payload                 (handled by the caller)
  2. ``defaults.<config_key>`` in ``.supertool.json`` (cwd or any parent)
  3. the ``origin`` git remote, when its host matches the platform

Returns ``None`` when nothing resolves, so the caller raises its own
"missing required field" error unchanged.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


def _run_git(args: list[str], timeout: int = 5) -> str | None:
    """Run a git command, returning trimmed stdout or None on any failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def config_default(key: str) -> str | None:
    """Return ``defaults[key]`` from the nearest .supertool.json, or None.

    Walks from cwd up to the filesystem root. The first .supertool.json found
    is authoritative — a malformed file or missing ``defaults`` block yields
    None rather than continuing the search (mirrors core's single-file load).
    """
    cwd = Path.cwd()
    for directory in [cwd, *cwd.parents]:
        candidate = directory / ".supertool.json"
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        defaults = data.get("defaults")
        if isinstance(defaults, dict):
            val = defaults.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None
    return None


def parse_remote(url: str) -> tuple[str, str] | None:
    """Parse a git remote URL into ``(host, 'namespace/repo')``.

    Handles the three forms git emits:
      scp-like  git@host:namespace/repo.git
      ssh://    ssh://git@host[:port]/namespace/repo.git
      https://  https://host[:port]/namespace/repo.git

    Returns None when the URL matches none of them.
    """
    url = url.strip()
    if not url:
        return None
    # scp-like: user@host:path (no scheme, single colon before the path)
    scp = re.match(r"^[\w.+-]+@([^:/]+):(.+)$", url)  # anchored-ok: url is .strip()ed above
    if scp:
        host, path = scp.group(1), scp.group(2)
    else:
        # scheme://[user@]host[:port]/path
        uri = re.match(r"^[a-zA-Z][\w+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", url)  # anchored-ok: url is .strip()ed above
        if not uri:
            return None
        host, path = uri.group(1), uri.group(2)
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    if not host or not path:
        return None
    return host, path


def origin_slug(host_substr: str) -> str | None:
    """Return the ``origin`` remote's 'ns/repo' when its host matches.

    ``host_substr`` is matched as a **substring** of the remote host, not an
    exact or dot-suffix match, so a self-hosted GitLab at ``gitlab.dp.tools``
    matches ``"gitlab"`` -- an exact/suffix test cannot express that without
    the operator naming their own host somewhere.

    That substring test is only safe because the input is operator-controlled:
    ``_run_git(["remote", "get-url", "origin"])`` reads the *local* checkout's
    own ``origin`` remote, which the operator configured, never text from an
    issue body, a PR title or any other attacker-influenced source. Under that
    condition ``host_substr in host`` cannot be tricked into matching a
    lookalike host, because there is no adversary between the caller and the
    value being tested.

    A caller that ever passes this a host or a host-shaped string from
    untrusted input breaks that condition: ``origin_slug("github")`` would
    then match ``evil-github.com``, ``notgithub.com`` and
    ``github.com.attacker.example`` just as readily as the real thing (#1327).
    Do not call this on anything but the operator's own git remote.
    """
    url = _run_git(["remote", "get-url", "origin"])
    if not url:
        return None
    parsed = parse_remote(url)
    if parsed is None:
        return None
    host, path = parsed
    if host_substr in host:
        return path
    return None


def resolve(config_key: str, host_substr: str) -> str | None:
    """Config default wins over git-remote auto-detect. None if neither hits."""
    return config_default(config_key) or origin_slug(host_substr)
