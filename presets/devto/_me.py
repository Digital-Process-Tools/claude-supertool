"""Resolve auth'd Dev.to username once and cache it.

Cache: ~/.config/devto/me_username (one line). Override with
DEVTO_USERNAME env var if you want to skip the lookup.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _rest import request

CACHE_FILE = Path(os.path.expanduser("~/.config/devto/me_username"))


def get_username(api_key: str) -> str:
    env = os.environ.get("DEVTO_USERNAME", "").strip()
    if env:
        return env
    try:
        cached = CACHE_FILE.read_text().strip()
        if cached:
            return cached
    except FileNotFoundError:
        pass
    data = request("GET", "/users/me", api_key)
    username = (data or {}).get("username", "") if isinstance(data, dict) else ""
    if username:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(username + "\n")
    return username
