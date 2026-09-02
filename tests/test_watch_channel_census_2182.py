"""A collision is a fact about the harness's configured servers, not about
files found on disk (#2182).

Both of `subscription()`'s collision gates used to answer "is a second
channel-capable server going to bind this socket?" from the filesystem, and
both were wrong in the ordinary supported configuration -- supertool installed
as a plugin, a session tagged with a differently-named channel server.

`_dual_declaration_objection` walks `_mcp_roots()`, whose first entry is
derived from `__file__`. That resolves to whichever copy of supertool is
executing -- a development clone, or the marketplace cache directory -- and
**both ship a `.mcp.json` declaring `claude-channel`**. So the objection fired
in every session, unconditionally, however the user configured their own repo.
The premise (this repository declares `claude-channel`, #1541) is true; what
does not follow is that the harness loaded it.

`read_refusal` is worse, because `channel:health` writes the evidence it then
reads: `subscription()` calls `_configured`, which runs `claude mcp get`,
which spawns a fresh instance of the configured channel server, which loses
the bind to the live consumer and leaves a `.refused.json` behind. Measured
2026-09-02: the marker was deleted, `channel:health` was run alone, and the
marker came back inside that run's own 1.26s. The next run reads it, so the
state is self-perpetuating.

The authority on whether a second server exists is the harness, and this file
already has the call: `_configured(CONSUMER_SERVER)`, three-state. `False`
means the on-disk declaration is not loaded and any marker came from a
transient probe -- neither gate should fire. `True` keeps today's behaviour.
`None` keeps declining, which is the existing safe direction.

Every "must not fire" case below is paired with a "must fire" control on the
identical fixture, because an assertion that a gate stays silent also passes
when the gate has been deleted.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import channel  # noqa: E402
import naming  # noqa: E402
from _changelog_findable import assert_change_is_findable  # noqa: E402

CONSUMER_ARGV = "bun /Users/x/notifiers/claude-channel/channel.ts"
SESSION_PID = 4242
TAGGED_OSS = ("claude /oss:tick "
              "--dangerously-load-development-channels server:oss-channel")


def _process_table(monkeypatch):
    table = {
        os.getpid(): (SESSION_PID, CONSUMER_ARGV, ""),
        SESSION_PID: (1, TAGGED_OSS, ""),
    }
    monkeypatch.setattr(channel, "_ps_fields",
                        lambda pid: table.get(pid, (None, "", f"no process {pid}")))


def _census(monkeypatch, standing):
    """`claude mcp get` answers per name: the tag is always configured, the
    standing `claude-channel` answers `standing` (True / False / None)."""
    def answer(name, _budget=None):
        if name == channel.CONSUMER_SERVER:
            return (standing, "" if standing is not None
                    else "`claude mcp get` exited 3 without saying the name is unknown")
        return True, ""
    monkeypatch.setattr(channel, "_configured", answer)


def _mcp_json_declaring_claude_channel(tmp_path: Path) -> Path:
    """The shape every installed copy of this plugin ships: `claude-channel`,
    no `env` block, so it inherits the session's socket."""
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {
            "claude-channel": {"command": "bun", "args": ["channel.ts"]}}}),
        encoding="utf-8")
    return tmp_path


def _refusal_marker(sock: str) -> None:
    Path(sock + channel.REFUSAL_SUFFIX).write_text(
        json.dumps({"pid": 29584, "ts": "2026-09-02T14:07:34Z",
                    "reason": "another claude-channel server is listening there",
                    "sock_path": sock}),
        encoding="utf-8")


def _resolved(tmp_path, monkeypatch):
    """Pin the resolved socket inside `tmp_path`, so the `claude-channel`
    declaration -- which carries no `env` and therefore inherits whatever this
    session resolves -- lands on the very socket being asked about. Without
    that the gate never fires and the "must not fire" assertions below would
    pass against a fixture that exercises nothing."""
    resolved = naming.resolve({naming.SOCK_ENV: str(tmp_path / "sock")})
    monkeypatch.setattr(channel, "RESOLVED", resolved)
    return resolved


def _ask(tmp_path, monkeypatch):
    resolved = _resolved(tmp_path, monkeypatch)
    return channel.subscription(os.getpid(), path=resolved.sock, roots=[tmp_path],
                                resolved=resolved), resolved.sock


# --- gate A: a declaration the harness never loaded ---------------------------

def test_a_declared_but_unconfigured_standing_server_is_not_a_collision(
        tmp_path, monkeypatch):
    """The measured case. `.mcp.json` in the plugin's own directory declares
    `claude-channel`; `claude mcp get -- claude-channel` says no such server.
    Nothing will bind twice, so the verdict must be the positive one."""
    _process_table(monkeypatch)
    _census(monkeypatch, standing=False)
    _mcp_json_declaring_claude_channel(tmp_path)
    sub, _ = _ask(tmp_path, monkeypatch)
    assert sub.state == channel.SUB_SUBSCRIBED, (sub.state, " ".join(sub.lines))


def test_control_a_configured_standing_server_still_collides(tmp_path, monkeypatch):
    """Positive control on the identical fixture: only the census answer
    differs, and the gate must still fire."""
    _process_table(monkeypatch)
    _census(monkeypatch, standing=True)
    _mcp_json_declaring_claude_channel(tmp_path)
    sub, _ = _ask(tmp_path, monkeypatch)
    assert sub.state == channel.SUB_UNKNOWN, (sub.state, " ".join(sub.lines))


def test_control_an_unanswerable_census_still_declines(tmp_path, monkeypatch):
    """`None` is not `False`. A lookup that failed must not be read as proof
    that no standing server exists -- that is the absence-as-presence defect
    this whole file guards, one call site over."""
    _process_table(monkeypatch)
    _census(monkeypatch, standing=None)
    _mcp_json_declaring_claude_channel(tmp_path)
    sub, _ = _ask(tmp_path, monkeypatch)
    assert sub.state == channel.SUB_UNKNOWN, (sub.state, " ".join(sub.lines))


# --- gate B: a marker left by a transient probe --------------------------------

def test_a_refusal_marker_with_no_configured_rival_is_not_a_collision(
        tmp_path, monkeypatch):
    """`claude mcp get` spawns the configured server, it loses the bind and
    marks. With no second server configured, that loser was a one-shot probe
    and its marker is not evidence of a session-held rival."""
    _process_table(monkeypatch)
    _census(monkeypatch, standing=False)
    resolved = _resolved(tmp_path, monkeypatch)
    _refusal_marker(resolved.sock)
    sub = channel.subscription(os.getpid(), path=resolved.sock, roots=[tmp_path],
                               resolved=resolved)
    assert sub.state == channel.SUB_SUBSCRIBED, (sub.state, " ".join(sub.lines))


def test_control_a_refusal_marker_with_a_configured_rival_still_collides(
        tmp_path, monkeypatch):
    """Positive control: same marker, same fixture, rival configured. #2133's
    finding is untouched."""
    _process_table(monkeypatch)
    _census(monkeypatch, standing=True)
    resolved = _resolved(tmp_path, monkeypatch)
    _refusal_marker(resolved.sock)
    sub = channel.subscription(os.getpid(), path=resolved.sock, roots=[tmp_path],
                               resolved=resolved)
    assert sub.state == channel.SUB_UNKNOWN, (sub.state, " ".join(sub.lines))


def test_control_a_refusal_marker_with_an_unanswerable_census_still_declines(
        tmp_path, monkeypatch):
    _process_table(monkeypatch)
    _census(monkeypatch, standing=None)
    resolved = _resolved(tmp_path, monkeypatch)
    _refusal_marker(resolved.sock)
    sub = channel.subscription(os.getpid(), path=resolved.sock, roots=[tmp_path],
                               resolved=resolved)
    assert sub.state == channel.SUB_UNKNOWN, (sub.state, " ".join(sub.lines))


def test_the_change_is_findable_from_the_changelog():
    assert_change_is_findable("2182")
