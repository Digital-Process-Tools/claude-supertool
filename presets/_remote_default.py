#!/usr/bin/env python3















from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


def _run_git(args: list[str], timeout: int = 5) -> str | None:

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









    url = url.strip()
    if not url:
        return None

    scp = re.match(r"^[\w.+-]+@([^:/]+):(.+)$", url)  
    if scp:
        host, path = scp.group(1), scp.group(2)
    else:

        uri = re.match(r"^[a-zA-Z][\w+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", url)  
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

    return config_default(config_key) or origin_slug(host_substr)
