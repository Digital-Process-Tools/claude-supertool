"""A session declaring two channel-capable servers is a static, checkable
fact, not just a reactive one (#2051).

#2133/#2136 already catch the case where a *rival consumer process* loses
the socket bind and leaves a `.refused.json` marker behind: `subscription()`
demotes `subscribed` to `CANNOT DETERMINE` while that marker is unread. But
the marker only exists between the moment a rival is refused and the moment
the winning consumer next (re)binds and clears it -- and #2051's own
2026-09-01 comment measured a session where the collision had already
happened, both of the harness's MCP connections were `CONNECTION_CLOSED`,
and `channel:health` still read `subscribed`, because no marker was left
beside *this* socket by the time anyone looked.

The fact this file pins does not need a marker at all. This repository's own
`.mcp.json` declares `claude-channel` unconditionally (#1541) and, absent an
explicit `env` block redirecting it, that server inherits the session's
environment -- the same environment a `--dangerously-load-development-channels
server:NAME` tag's consumer inherits. Two channel-capable servers that both
resolve one socket by construction is knowable from the two config files
alone, before either process ever binds or refuses anything, and it survives
a marker being cleared because it was never reactive in the first place.
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
PATH = "/tmp/st2051-oss-supertool.sock"
OTHER_PATH = "/tmp/st2051-other.sock"


def _process_table(monkeypatch, session_argv: str):
    table = {
        os.getpid(): (SESSION_PID, CONSUMER_ARGV, ""),
        SESSION_PID: (1, session_argv, ""),
    }
    monkeypatch.setattr(channel, "_ps_fields",
                        lambda pid: table.get(pid, (None, "", f"no process {pid}")))


def _configured(monkeypatch, answer, why: str = ""):
    monkeypatch.setattr(channel, "_configured",
                        lambda _name, _budget=None: (answer, why))


def _mcp_json(tmp_path: Path, env: dict | None) -> Path:
    server: dict = {"command": "bun", "args": ["channel.ts"]}
    if env is not None:
        server["env"] = env
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"claude-channel": server}}), encoding="utf-8")
    return tmp_path


TAGGED_OSS = ("claude /oss:tick "
              "--dangerously-load-development-channels server:oss-channel")
TAGGED_CLAUDE_CHANNEL = ("claude /oss:tick "
                         "--dangerously-load-development-channels server:claude-channel")


# --- must fire: the static double declaration -------------------------------

def test_a_standing_claude_channel_that_inherits_the_same_socket_is_cannot_determine(
        tmp_path, monkeypatch):
    """`.mcp.json` declares `claude-channel` with no `env` block, so it
    inherits this session's environment -- the same one the consumer inherits.
    A session that *also* carries `server:oss-channel` is guaranteed to spawn
    two channel-capable servers over the identical socket."""
    _process_table(monkeypatch, TAGGED_OSS)
    _configured(monkeypatch, True)
    _mcp_json(tmp_path, env=None)
    resolved = naming.resolve({})
    monkeypatch.setattr(channel, "RESOLVED", resolved)
    sub = channel.subscription(os.getpid(), path=resolved.sock, roots=[tmp_path],
                               resolved=resolved)
    assert sub.state == channel.SUB_UNKNOWN, (sub.state, sub.lines)
    joined = " ".join(sub.lines)
    assert "claude-channel" in joined, joined
    assert "oss-channel" in joined, joined


def test_the_2051_scenario_end_to_end(tmp_path, monkeypatch):
    """The exact shape the issue's 2026-09-01 comment measured: a session
    carrying `server:oss-channel`, no live refusal marker for this socket
    (already cleared, or never written), and a standing `claude-channel`
    declaration that resolves to the same socket. Must not read `subscribed`."""
    _process_table(monkeypatch, TAGGED_OSS)
    _configured(monkeypatch, True)
    _mcp_json(tmp_path, env={naming.NAME_ENV: "oss-supertool"})
    resolved = naming.resolve({naming.NAME_ENV: "oss-supertool"})
    monkeypatch.setattr(channel, "RESOLVED", resolved)
    sub = channel.subscription(os.getpid(), path=resolved.sock, roots=[tmp_path],
                               resolved=resolved)
    assert sub.state == channel.SUB_UNKNOWN, (sub.state, "".join(sub.lines), sub.lines)


# --- must not fire ------------------------------------------------------------

def test_subscribing_through_claude_channel_itself_is_not_a_double_declaration(
        tmp_path, monkeypatch):
    """The tag *is* the standing server -- there is only one declaration in
    play, not two."""
    _process_table(monkeypatch, TAGGED_CLAUDE_CHANNEL)
    _configured(monkeypatch, True)
    _mcp_json(tmp_path, env=None)
    resolved = naming.resolve({})
    monkeypatch.setattr(channel, "RESOLVED", resolved)
    sub = channel.subscription(os.getpid(), path=PATH, roots=[tmp_path], resolved=resolved)
    assert sub.state == channel.SUB_SUBSCRIBED, (sub.state, "".join(sub.lines))


def test_a_claude_channel_pointed_elsewhere_is_not_a_collision(tmp_path, monkeypatch):
    """#2044's shape: an intentional second, differently-named channel.
    Explicit `env` redirects `claude-channel` to a socket that is not the one
    being asked about, so nothing here collides."""
    _process_table(monkeypatch, TAGGED_OSS)
    _configured(monkeypatch, True)
    _mcp_json(tmp_path, env={naming.NAME_ENV: "some-other-project"})
    resolved = naming.resolve({naming.NAME_ENV: "oss-supertool"})
    monkeypatch.setattr(channel, "RESOLVED", resolved)
    sub = channel.subscription(os.getpid(), path=resolved.sock, roots=[tmp_path],
                               resolved=resolved)
    assert sub.state == channel.SUB_SUBSCRIBED, (sub.state, "".join(sub.lines))


def test_no_mcp_json_at_all_is_not_a_collision(tmp_path, monkeypatch):
    _process_table(monkeypatch, TAGGED_OSS)
    _configured(monkeypatch, True)
    resolved = naming.resolve({})
    monkeypatch.setattr(channel, "RESOLVED", resolved)
    sub = channel.subscription(os.getpid(), path=PATH, roots=[tmp_path], resolved=resolved)
    assert sub.state == channel.SUB_SUBSCRIBED, (sub.state, "".join(sub.lines))


def test_an_unreadable_mcp_json_is_cannot_determine_not_no_collision(
        tmp_path, monkeypatch):
    """Reviewer finding on #2051's own fix: `_declared_env` collapses 'no
    `.mcp.json` here' and 'a `.mcp.json` exists but could not be read/parsed'
    into the same `(None, why)` shape. `_dual_declaration_objection` must not
    read both as 'nothing declared, no collision' -- that is the exact
    absence-read-as-presence defect this file's neighbours (`consumer_lines`,
    and `subscription`'s own `read_refusal` branch six lines below) already
    guard against. An unreadable `.mcp.json` must land in CANNOT DETERMINE,
    not SUBSCRIBED."""
    _process_table(monkeypatch, TAGGED_OSS)
    _configured(monkeypatch, True)
    mcp_path = tmp_path / ".mcp.json"
    mcp_path.write_text("{not valid json", encoding="utf-8")
    resolved = naming.resolve({})
    monkeypatch.setattr(channel, "RESOLVED", resolved)
    # `PATH`, not `resolved.sock`: the default socket path is a real,
    # process-wide path that can carry a genuine #2133 refusal marker left
    # over from something else on this machine, which would make this test
    # pass off the *old* collision-marker mechanism rather than the one
    # under test.
    sub = channel.subscription(os.getpid(), path=PATH, roots=[tmp_path],
                               resolved=resolved)
    assert sub.state == channel.SUB_UNKNOWN, (sub.state, sub.lines)


def test_no_path_given_preserves_old_behaviour(tmp_path, monkeypatch):
    """Callers with no socket to check (the pre-#2133 call shape) get exactly
    the old behaviour -- this check, like the refusal-marker one, is gated on
    `path` being given."""
    _process_table(monkeypatch, TAGGED_OSS)
    _configured(monkeypatch, True)
    _mcp_json(tmp_path, env=None)
    resolved = naming.resolve({})
    monkeypatch.setattr(channel, "RESOLVED", resolved)
    sub = channel.subscription(os.getpid(), roots=[tmp_path], resolved=resolved)
    assert sub.state == channel.SUB_SUBSCRIBED, (sub.state, "".join(sub.lines))


def test_change_is_findable():
    assert_change_is_findable(2051)
