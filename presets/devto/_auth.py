"""Dev.to auth resolution.

Resolution order (first hit wins):
1. Env var: DEVTO_API_KEY
2. Config file: ~/.config/devto/token
3. Cwd file: .devto-token

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


def get_api_key() -> str:
    val = _read_first(
        "DEVTO_API_KEY",
        "~/.config/devto/token",
        ".devto-token",
    )
    if not val:
        sys.stderr.write(
            "ERROR: Dev.to API key not found. Set DEVTO_API_KEY env var, "
            "or write to ~/.config/devto/token, or .devto-token in cwd.\n"
        )
        sys.exit(2)
    return val
