"""Slack channel authorization -- out-of-repo, default-closed (#2035).

Delivery of a Slack channel's messages into a live session (`#2031`) is not
the same question as whether a message may INSTRUCT the agent sitting in
that session -- treating the two as one question is exactly what this issue
exists to prevent. This module answers only the second one, and it is built
around four properties the issue's own design spells out:

1. **Default is the closed end.** No configuration, or a channel this
   configuration does not name, resolves to `off` -- never a permissive
   default. Absent means refuse.
2. **Opening is a machine-owner decision.** The only place a channel can be
   raised above `off` is the out-of-repo file this module reads
   (`~/.config/supertool/slack_authorization.json`, the same directory
   `_supertool.py::_find_preset_file` already uses for user-level presets).
   A tracked `.supertool.json` may narrow a channel further -- e.g. force
   `off` in this repository regardless of what the machine-owner's file
   says -- and MAY NOT widen it. An attempted widening is ignored, not
   silently honoured, and the ignored attempt is named in the decision.
3. **The effective level is visible at runtime.** `resolve_channel` returns
   the level actually in force plus a `detail` string that says which file
   (or absence) produced it, so a caller (`slack_authorization`, the op)
   can print it rather than needing a second, hand-run comparison to notice
   drift.
4. **A level that is not yet implemented is refused, not silently
   downgraded or granted.** `open` requires resolving "anyone who can post
   in the channel", which this module does not implement; declaring it
   fails the whole channel closed with a stated reason rather than quietly
   behaving like `context`. Reading a config wrong in the permissive
   direction is the more expensive mistake.

**What this module does not do, on purpose.** It does not gate delivery in
`presets/watch/sources/slack/poller.py` -- that wiring, and the live-group
membership diff the issue's own comments describe ("pin the group, then
diff it"), are follow-on work tracked separately. This module is the
authorization decision function and its visibility op; a caller that never
calls it gets exactly today's behaviour, unchanged.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NamedTuple, Optional


def load_project_config() -> dict:
    """The nearest tracked `.supertool.json`'s `slack` block, walking up from cwd.

    Same walk-and-stop-at-first-file shape as `_remote_default.config_default`
    -- the first `.supertool.json` found is authoritative, and a malformed one
    yields `{}` rather than continuing the search or raising. `{}` reads as
    "no project narrowing configured", never as a widening: `resolve_channel`
    only ever narrows on what this returns.
    """
    cwd = Path.cwd()
    for directory in [cwd, *cwd.parents]:
        candidate = directory / ".supertool.json"
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


class Decision(NamedTuple):
    level: str
    heard: bool
    may_instruct: bool
    detail: str


#: Ascending strictness. `open` is declared but refused below (property 4).
LEVELS = ("off", "context", "allowlist", "open")
_RANK = {name: i for i, name in enumerate(LEVELS)}

#: `~/.config/supertool/slack_authorization.json` -- the machine-owner's own
#: file, alongside `~/.config/supertool/presets/` (see `_find_preset_file`'s
#: own docstring in `_supertool.py`). A function, not a constant, so a test
#: can point `HOME`/`expanduser` elsewhere without patching a module-level
#: path computed at import time.
def config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".config", "supertool",
                        "slack_authorization.json")


def _load_raw() -> tuple[Optional[dict], str]:
    """(config dict, state) -- state is 'ok', 'absent', or 'invalid'.

    Both non-'ok' states are handled identically by every caller below
    (fail closed), and are kept apart only so `detail` can say which one
    happened -- the same three-state discipline this repo asks of every
    other checker: a missing file and a broken one must not read as the
    same "nothing to report" if a human ever has to debug why a channel
    that should be open is not.
    """
    path = config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return None, "absent"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, "invalid"
    if not isinstance(data, dict):
        return None, "invalid"
    return data, "ok"


def _project_level(project_config: Optional[dict], channel_id: str) -> Optional[str]:
    if not project_config:
        return None
    slack_cfg = project_config.get("slack")
    if not isinstance(slack_cfg, dict):
        return None
    channels = slack_cfg.get("channels")
    if not isinstance(channels, dict):
        return None
    value = channels.get(channel_id)
    return value if isinstance(value, str) else None


def resolve_channel(channel_id: str, user_id: Optional[str] = None, *,
                    project_config: Optional[dict] = None) -> Decision:
    """The authorization decision for one channel (and, under `allowlist`,
    one user).

    `user_id` must be the poller's own computed identity for the message's
    author -- Slack's `U...` ID -- never a value read out of the message
    body. This function does not fetch or verify identity; that is the
    caller's job, same division as `author_is_viewer` in the poller.
    """
    data, state = _load_raw()
    path = config_path()
    if state == "absent":
        base_level = "off"
        base_detail = f"no config at {path} — absent means refuse (property 1)"
        entry: dict = {}
    elif state == "invalid":
        base_level = "off"
        base_detail = (f"{path} exists but could not be parsed as a JSON "
                       f"object — a broken config fails closed, the same as "
                       f"no config, rather than falling back to any other "
                       f"channel's level")
        entry = {}
    else:
        channels = data.get("channels") if isinstance(data, dict) else None
        entry = (channels or {}).get(channel_id) if isinstance(channels, dict) else None
        if not isinstance(entry, dict):
            base_level = "off"
            base_detail = (f"{path} does not list channel {channel_id!r} — "
                           f"absent means refuse (property 1)")
            entry = {}
        else:
            level = entry.get("level")
            if level not in LEVELS:
                base_level = "off"
                base_detail = (f"{path} names channel {channel_id!r} with "
                               f"level {level!r}, which is not one of "
                               f"{LEVELS} — refusing rather than guessing "
                               f"which one was meant")
            else:
                base_level = level
                base_detail = f"{path}: channel {channel_id!r} is {level!r}"

    proj_level = _project_level(project_config, channel_id)
    effective_level = base_level
    detail = base_detail
    if proj_level is not None:
        if proj_level not in LEVELS:
            detail += (f"; the project's .supertool.json names an unrecognised "
                       f"level {proj_level!r} for this channel — ignored")
        elif _RANK[proj_level] <= _RANK[base_level]:
            effective_level = proj_level
            detail += f"; narrowed to {proj_level!r} by .supertool.json"
        else:
            detail += (f"; .supertool.json asked for {proj_level!r}, which "
                       f"is WIDER than {base_level!r} — a project may narrow "
                       f"a channel, never widen it, so this was ignored "
                       f"(property 2)")

    if effective_level == "open":
        return Decision(level="refused", heard=False, may_instruct=False,
                        detail=(detail + "; 'open' is declared but not yet "
                               "implemented in this build (#2035) — "
                               "refusing the whole channel rather than "
                               "silently granting or downgrading it. "
                               "Configure 'allowlist' or 'context' instead."))
    if effective_level == "off":
        return Decision(level="off", heard=False, may_instruct=False, detail=detail)
    if effective_level == "context":
        return Decision(level="context", heard=True, may_instruct=False, detail=detail)
    # allowlist
    pinned = entry.get("users") if base_level == effective_level else []
    pinned = [str(u) for u in pinned] if isinstance(pinned, list) else []
    may_instruct = bool(user_id) and user_id in pinned
    return Decision(level="allowlist", heard=True, may_instruct=may_instruct,
                    detail=detail + f" (pinned: {len(pinned)} user id(s))")
