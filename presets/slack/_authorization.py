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
import stat
import sys
from pathlib import Path
from typing import NamedTuple, Optional


def _config_trust_violation(candidate: Path) -> Optional[str]:
    """POSIX ownership/permission guard, mirroring `_supertool._load_config`'s
    own #695 hardening (and `presets/worktree/_common.py`'s #2370 /
    `presets/gitlab/_maintenance.py`'s #2365 copies of it -- presets cannot
    import the core module, so each walk-up loader in this codebase
    re-implements the same check rather than sharing it).

    `load_project_config`'s result can NARROW a Slack channel's authorization
    level (property 2 above), so a group/world-writable `.supertool.json`,
    or one owned by a different local user, is exactly the file another
    local account could rewrite between the moment it was reviewed and the
    moment this module reads it -- the same TOCTOU shape #695 closed for
    the core loader every other op goes through.

    POSIX-only: `st_uid` and the write bits are meaningless on Windows, so
    this returns `None` (trusted) unconditionally there. Root is treated as
    trusted, matching `_supertool._config_trust_violation`.
    """
    if os.name != "posix":
        return None
    try:
        st = candidate.stat()
    except OSError as exc:
        return f"cannot stat: {exc}"
    caller_uid = os.getuid()
    if st.st_uid not in (caller_uid, 0) and caller_uid != 0:
        return (
            f"not owned by the current user (owner uid {st.st_uid}, "
            f"running as uid {caller_uid})"
        )
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"group/world-writable (mode {stat.S_IMODE(st.st_mode):o})"
    return None


class ProjectConfigResult(NamedTuple):
    """Three states, not two (docs/validators.md) -- the same discipline
    `presets/worktree/_common.py::ConfigResult` already applies to its own
    walk-up loader (#2427 follows that sibling's shape rather than
    inventing a new one).

    `data` is `None` when no TRUSTED `.supertool.json` was found at all
    (the repo-root boundary was reached, or the walk left the git repo,
    with nothing found or every candidate skipped for trust reasons) --
    the genuine "this project never configured Slack" case. `error` is set
    only when a FOUND, TRUSTED config could not be parsed as a JSON object
    -- a malformed write (merge marker, truncated save) -- and `data` is
    then `None` too, because a malformed file's intended content, narrow or
    wide, cannot be recovered. These two `data is None` cases are NOT the
    same fact and `resolve_channel` must not render them alike: only the
    first means "no project narrowing was ever declared"; the second means
    "a narrowing WAS declared here and is now unreadable", which must fail
    closed rather than silently fall back to the machine-owner's (possibly
    wider) base level (#2427).
    """
    data: Optional[dict]
    error: Optional[str]


def load_project_config_result() -> ProjectConfigResult:
    """The real walk-up loader -- see `load_project_config` below for the
    trust/boundary rules, unchanged here. Returns the three-state result;
    `load_project_config` is a thin, back-compat wrapper over this that
    only ever returns a dict (collapsing `error` into `{}`, since every
    pre-#2427 caller of that function already treats `{}` as fail-closed).
    A caller that must distinguish "not configured" from "malformed" --
    `resolve_channel`, via `presets/slack/auth.py` -- calls this function
    instead.

    Same two limits `_supertool._load_config` (#695), `worktree._common
    .load_config` (#2370) and `gitlab._maintenance.load_config` (#2365)
    already carry -- a config is exactly as trusted as the project that
    owns it, and this walk must not reach OUTSIDE that project, nor accept
    a config another local account could have rewritten:

    * the walk stops once it reaches a directory containing `.git` -- the
      repo root -- rather than continuing to `/`; a cwd not inside a git
      repo at all keeps walking to `/`, since there is no repo boundary;
    * each candidate is checked with `_config_trust_violation` before it
      is opened -- a file that is group/world-writable, or not owned by
      the caller (or root), is skipped with a warning on stderr, exactly
      like an absent one, so the walk can still find a further, trusted
      config higher up (until the repo-root boundary above stops it).
    """
    d = Path.cwd()
    #: A skipped-for-trust-reasons candidate along the way must not make an
    #: EXHAUSTED walk (no trusted config found by the boundary) look like a
    #: clean "never configured" absence -- that is the same silent-widen
    #: shape #2427 closed for malformed JSON, just triggered by a
    #: permission/ownership change instead of a parse error (found in
    #: self-review, #2427). The walk still keeps looking for a FURTHER
    #: trusted candidate higher up when one is skipped (#2416's design,
    #: unchanged) -- only the two "nothing trusted was ever found" returns
    #: below now check whether anything was skipped along the way.
    skipped: list[str] = []
    while True:
        candidate = d / ".supertool.json"
        if candidate.is_file():
            violation = _config_trust_violation(candidate)
            if violation is not None:
                msg = f"skipped {candidate} ({violation})"
                skipped.append(msg)
                sys.stderr.write(
                    f"WARNING: {msg} -- ignoring it for slack "
                    f"authorization.\n"
                )
            else:
                try:
                    data = json.loads(candidate.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as exc:
                    err = f"{candidate} is not valid JSON ({exc})"
                    sys.stderr.write(
                        f"WARNING: {err} -- ignoring it for slack "
                        f"authorization.\n"
                    )
                    return ProjectConfigResult(data=None, error=err)
                if not isinstance(data, dict):
                    err = (f"{candidate} does not contain a JSON object at "
                           f"its top level")
                    sys.stderr.write(
                        f"WARNING: {err} -- ignoring it for slack "
                        f"authorization.\n"
                    )
                    return ProjectConfigResult(data=None, error=err)
                return ProjectConfigResult(data=data, error=None)
        if (d / ".git").exists():
            if skipped:
                return ProjectConfigResult(data=None, error="; ".join(skipped))
            return ProjectConfigResult(data=None, error=None)
        parent = d.parent
        if parent == d:
            if skipped:
                return ProjectConfigResult(data=None, error="; ".join(skipped))
            return ProjectConfigResult(data=None, error=None)
        d = parent


def load_project_config() -> dict:
    """Back-compat wrapper over `load_project_config_result` -- returns
    `{}` for BOTH "not configured" and "malformed", exactly the pre-#2427
    behaviour every pre-existing caller of this function already treats as
    fail-closed (#2433's stderr diagnostic still fires either way).
    `resolve_channel`'s caller uses `load_project_config_result()` instead
    so it can pass the distinction through and avoid the silent-widen bug
    this function's own collapse used to cause (#2427).
    """
    return load_project_config_result().data or {}


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
    """(config dict, state) -- state is 'ok', 'absent', 'unreadable', or
    'invalid'.

    Every non-'ok' state is handled identically by every caller below (fail
    closed), and they are kept apart only so `detail` can say which one
    happened -- the same three-state discipline this repo asks of every
    other checker, applied here to the read itself: a missing file, a
    present-but-unreadable one (permission denied, a directory sitting at
    the path, a symlink loop) and a broken one must not all collapse into
    the same "nothing to report" if a human ever has to debug why a channel
    that should be open is not. `FileNotFoundError` is `absent`; every other
    `OSError` from the open/read is `unreadable` -- both fail closed
    identically, so this split only ever changes `detail`, never the
    decision.
    """
    path = config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        return None, "unreadable"
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
                    project_config: Optional[dict] = None,
                    project_config_error: Optional[str] = None) -> Decision:
    """The authorization decision for one channel (and, under `allowlist`,
    one user).

    `user_id` must be the poller's own computed identity for the message's
    author -- Slack's `U...` ID -- never a value read out of the message
    body. This function does not fetch or verify identity; that is the
    caller's job, same division as `author_is_viewer` in the poller.

    `project_config_error` is set when the caller found a TRACKED, TRUSTED
    `.supertool.json` that could not be parsed (`load_project_config_result
    ().error`) -- a different fact from `project_config` being empty or
    `None`, which means no such file was ever found at all. A malformed
    file's intended narrowing cannot be recovered, so this FAILS THE
    CHANNEL CLOSED (`off`) rather than falling back to `project_config`'s
    absence-shaped default of "no project override" -- which would
    silently widen to the machine-owner's base level and render
    byte-identical to a repo that never configured Slack in the first
    place (#2427).
    """
    data, state = _load_raw()
    path = config_path()
    if state == "absent":
        base_level = "off"
        base_detail = f"no config at {path} — absent means refuse (property 1)"
        entry: dict = {}
    elif state == "unreadable":
        base_level = "off"
        base_detail = (f"{path} exists but could not be read (permission "
                       f"denied, a directory sitting at that path, or "
                       f"another OS-level error) — different from an "
                       f"absent file, though both fail closed the same way")
        entry = {}
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

    if project_config_error is not None:
        effective_level = "off"
        fallback_note = (
            f" rather than falling back to {base_level!r}" if base_level != "off"
            else ""
        )
        detail = (base_detail +
                  f"; this project's .supertool.json could not be used "
                  f"({project_config_error}) — a narrowing may have been "
                  f"declared here and is now unrecoverable, so this fails "
                  f"closed to 'off'{fallback_note} (property 2, #2427)")
    else:
        proj_level = _project_level(project_config, channel_id)
        effective_level = base_level
        detail = base_detail
        if proj_level is not None:
            if proj_level not in LEVELS:
                detail += (f"; the project's .supertool.json names an unrecognised "
                           f"level {proj_level!r} for this channel — ignored")
            elif _RANK[proj_level] < _RANK[base_level]:
                effective_level = proj_level
                detail += f"; narrowed to {proj_level!r} by .supertool.json"
            elif _RANK[proj_level] == _RANK[base_level]:
                effective_level = proj_level
                detail += (f"; .supertool.json also names {proj_level!r} for "
                           f"this channel — same level, unchanged")
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
