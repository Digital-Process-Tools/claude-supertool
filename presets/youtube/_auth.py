"""YouTube Data API v3 auth resolution -- read ops only (API key, no OAuth2).

Resolution order for the API key (first hit wins):
  1. YOUTUBE_API_KEY env var
  2. ~/.config/youtube/api_key
  3. .youtube-api-key in cwd

This mirrors presets/bluesky/_auth.py's resolution order (env var, then
~/.config/<service>/<name>, then a cwd dotfile) rather than the bare
`~/.config/youtube/api_key`-only path #227 proposed on its own: every other
credential-backed preset in this repo (bluesky, devto, hashnode) resolves this
way, and a fourth preset with a different loading convention is a worse outcome
than diverging from the issue's own literal wording.

Write ops (youtube_comment/reply/like, OAuth2) are out of scope for this
preset slice and are not implemented here -- see #227's own "Implementation
order" split.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _read_first(env: str, *paths: str) -> str | None:
    val = os.environ.get(env)
    if val:
        return val.strip()
    for p in paths:
        path = Path(os.path.expanduser(p))
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    return None


def get_api_key() -> str:
    val = _read_first(
        "YOUTUBE_API_KEY",
        "~/.config/youtube/api_key",
        ".youtube-api-key",
    )
    if not val:
        sys.stderr.write(
            "ERROR: YouTube API key not found. Set YOUTUBE_API_KEY env var, "
            "or write to ~/.config/youtube/api_key. Create one at "
            "https://console.cloud.google.com/apis/credentials (enable the "
            "YouTube Data API v3 on the project first).\n"
        )
        sys.exit(2)
    return val
