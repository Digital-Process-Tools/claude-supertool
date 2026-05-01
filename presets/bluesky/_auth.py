"""Bluesky auth resolution.

Bluesky uses handle + app password (NOT main password). Generate one
at https://bsky.app/settings/app-passwords.

Resolution order (first hit wins) for each value:

handle:
  1. BLUESKY_HANDLE env var
  2. ~/.config/bluesky/handle
  3. .bluesky-handle in cwd

app password:
  1. BLUESKY_APP_PASSWORD env var
  2. ~/.config/bluesky/app_password
  3. .bluesky-app-password in cwd

Tokens never returned to stdout — only used in HTTP requests.
"""
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
            return path.read_text().strip()
    return None


def get_handle() -> str:
    val = _read_first(
        "BLUESKY_HANDLE",
        "~/.config/bluesky/handle",
        ".bluesky-handle",
    )
    if not val:
        sys.stderr.write(
            "ERROR: Bluesky handle not found. Set BLUESKY_HANDLE env var, "
            "or write to ~/.config/bluesky/handle. Example: max-ai-dev.bsky.social\n"
        )
        sys.exit(2)
    return val


def get_app_password() -> str:
    val = _read_first(
        "BLUESKY_APP_PASSWORD",
        "~/.config/bluesky/app_password",
        ".bluesky-app-password",
    )
    if not val:
        sys.stderr.write(
            "ERROR: Bluesky app password not found. Generate at "
            "https://bsky.app/settings/app-passwords (NOT main password). "
            "Then write to ~/.config/bluesky/app_password.\n"
        )
        sys.exit(2)
    return val
