"""Resolve auth'd Hashnode username once and cache it.

Cache: ~/.config/hashnode/me_username (one line). Override with
HASHNODE_USERNAME env var if you want to skip the lookup.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _graphql import gql

CACHE_FILE = Path(os.path.expanduser("~/.config/hashnode/me_username"))

ME_QUERY = """
query Me { me { username } }
"""


def get_username(token: str) -> str:
    env = os.environ.get("HASHNODE_USERNAME", "").strip()
    if env:
        return env
    try:
        cached = CACHE_FILE.read_text().strip()
        if cached:
            return cached
    except FileNotFoundError:
        pass
    data = gql(ME_QUERY, {}, token)
    me = data.get("me") or {}
    username = me.get("username") or ""
    if username:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(username + "\n")
    return username
