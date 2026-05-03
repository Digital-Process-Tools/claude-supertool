"""Hashnode auth resolution.

Resolution order (first hit wins):
1. Env var: HASHNODE_TOKEN, HASHNODE_PUBLICATION_ID
2. Config file: ~/.config/hashnode/{token,publication_id}
3. Cwd file: .hashnode-token, .hashnode-publication-id

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


def get_token() -> str:
    val = _read_first(
        "HASHNODE_TOKEN",
        "~/.config/hashnode/token",
        ".hashnode-token",
    )
    if not val:
        sys.stderr.write(
            "ERROR: Hashnode token not found. Set HASHNODE_TOKEN env var, "
            "or write to ~/.config/hashnode/token, or .hashnode-token in cwd.\n"
        )
        sys.exit(2)
    return val


def get_publication_id() -> str:
    val = _read_first(
        "HASHNODE_PUBLICATION_ID",
        "~/.config/hashnode/publication_id",
        ".hashnode-publication-id",
    )
    if not val:
        sys.stderr.write(
            "ERROR: Hashnode publication ID not found. Set HASHNODE_PUBLICATION_ID, "
            "or write to ~/.config/hashnode/publication_id, or .hashnode-publication-id.\n"
        )
        sys.exit(2)
    return val


def env_truthy(name: str) -> bool:
    """Return True if env var `name` is set to a truthy value.

    Truthy values: 'true', '1', 'yes', 'on' (case-insensitive). Anything else
    returns False. Used by hashnode_react and hashnode_comment to honor the
    `auto_force` config opt-in via SUPERTOOL_AUTO_FORCE env var.
    """
    val = os.environ.get(name, "").strip().lower()
    return val in {"true", "1", "yes", "on"}
