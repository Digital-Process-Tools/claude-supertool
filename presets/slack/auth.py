#!/usr/bin/env python3
"""slack_authorization[:CHANNEL_ID] -- the resolved Slack instruction policy (#2035).

Property 3 of the design: "The effective level is visible at runtime, not
only in a file." A file nobody reads drifting out of sync with what a
channel actually does is this repository's most common defect shape, so
this op exists to be the thing a human or an agent runs instead of hand-
comparing `~/.config/supertool/slack_authorization.json` against a tracked
`.supertool.json`'s narrowing.

With no argument, lists every channel the out-of-repo config names, each
with its resolved level (after any project narrowing) and whether it is
heard/may-instruct. With a channel id, resolves just that one -- including a
channel the config does not mention, which is the ordinary "why is this
still off" question and the one bare listing cannot answer since an absent
channel is not a row.

`slack_authorization:CHANNEL_ID:USER_ID` also resolves whether that one
Slack user id would be authorized under an `allowlist` channel -- USER_ID
must be the `U...` form; see `_authorization.py` for why a handle is not
accepted in its place.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))  # for _console (#415/#1388)
from _authorization import (  # noqa: E402
    LEVELS, config_path, load_project_config, resolve_channel, _load_raw,
)
from _console import use_utf8_stdout  # noqa: E402  (glyphs on a cp437 console -- #415/#1388)


def _list_all() -> int:
    data, state = _load_raw()
    path = config_path()
    if state != "ok":
        print(f"# Slack channel authorization\n"
              f"No usable config at {path} ({state}). Every channel resolves "
              f"to `off` -- absent means refuse, not open.")
        return 0
    channels = data.get("channels") if isinstance(data, dict) else None
    channel_ids = sorted(channels) if isinstance(channels, dict) else []
    project = load_project_config()
    print(f"# Slack channel authorization — {path}")
    if not channel_ids:
        print("no channels declared -- every channel is `off` by default "
              "(property 1: absent means refuse).")
        return 0
    for channel_id in channel_ids:
        d = resolve_channel(channel_id, project_config=project)
        print(f"{channel_id}: {d.level} (heard={d.heard}, "
              f"may_instruct={d.may_instruct}) — {d.detail}")
    return 0


def _one(channel_id: str, user_id: str | None) -> int:
    project = load_project_config()
    d = resolve_channel(channel_id, user_id=user_id, project_config=project)
    print(f"# Slack channel authorization — {channel_id}")
    print(f"level: {d.level}")
    print(f"heard: {d.heard}")
    print(f"may_instruct: {d.may_instruct}"
         + (f" (user {user_id})" if user_id else " (no user_id given)"))
    print(f"why: {d.detail}")
    if d.level == "refused":
        print(f"\nDeclared levels: {', '.join(LEVELS)}. 'open' is declared "
              f"but not implemented in this build.")
    return 0


def main(argv: list[str]) -> int:
    use_utf8_stdout()
    # Core splits `op:CHANNEL_ID:USER_ID` on `:` into separate argv entries
    # before this script ever runs (`gh-job.py`'s own `main()` is the pattern
    # this follows) -- NOT one colon-joined token, which an earlier draft of
    # this function assumed and which silently dropped USER_ID on every call.
    toks = [t for t in argv if t != ""]
    if not toks:
        return _list_all()
    channel_id = toks[0]
    user_id = toks[1] if len(toks) > 1 else None
    return _one(channel_id, user_id)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
