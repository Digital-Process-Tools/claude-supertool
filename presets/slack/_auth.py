"""Slack bot token resolution.

Resolution order (first hit wins):
1. SLACK_BOT_TOKEN env var
2. ~/.config/slack/bot_token
3. .slack-bot-token in cwd

Create a token at https://api.slack.com/apps -- in a browser, not the
Slack desktop app's own Preferences, which has nothing related. Full
sequence, including the manifest shortcut and the `xoxp-` (user token)
tradeoff, is in `docs/presets/slack.md` (#2041). Short version: OAuth &
Permissions -> Bot Token Scopes -> `chat:write` (to publish) and
`channels:history`/`groups:history` (to poll) -> Install to Workspace ->
Bot User OAuth Token (starts with `xoxb-`).

Tokens never returned to stdout -- only used in HTTP requests. `presets/
_secrets.py` already recognises the `xox[abprse]-` shapes these mint, so any
copy that leaks into an error message downstream is still caught by the
scrub in `presets/slack/_api.py`.
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


def get_bot_token_or_none() -> str | None:
    """`None` rather than exiting -- for a poller, which must keep polling
    and report `slack_unreachable` rather than kill the whole watcher over a
    token that has not been configured yet."""
    return _read_first(
        "SLACK_BOT_TOKEN",
        "~/.config/slack/bot_token",
        ".slack-bot-token",
    )


def get_bot_token() -> str:
    """For a one-shot op (`slack_publish`), where exiting is the right answer."""
    val = get_bot_token_or_none()
    if not val:
        sys.stderr.write(
            "ERROR: Slack bot token not found. Set SLACK_BOT_TOKEN env var, "
            "or write to ~/.config/slack/bot_token, or .slack-bot-token in cwd. "
            "Create one at https://api.slack.com/apps (OAuth & Permissions -> "
            "Bot User OAuth Token).\n"
        )
        sys.exit(2)
    return val
