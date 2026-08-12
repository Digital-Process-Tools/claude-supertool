"""The consumer honours `SUPERTOOL_WATCH_NAME` too, or the name is half a fix (#1477).

`channel.ts` is spawned by the harness from `.mcp.json`, not by supertool, so
the config-to-env route that carries a name to every poller cannot reach it. If
the consumer could only be pointed at a channel by a full socket path, the name
would configure three of four surfaces — which is precisely the half-configured
state `presets/watch/README.md` says is worse than configuring nothing, arriving
through a new door.

So both ends read the same variable and derive the same path from it, and these
tests are the pin on *the same*: they assert the path `presets/watch/naming.py`
computes, not a path spelled out again here.

Precedence matches the Python side and is asserted rather than assumed: an
explicit `SUPERTOOL_WATCH_SOCK` wins, because it is the value a running poller
already captured.
"""
from __future__ import annotations

import os
import shutil
import socket as _socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from _toolchain_gate import js_promised, require_or_skip

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import naming  # noqa: E402

CHANNEL_TS = REPO / "notifiers" / "claude-channel" / "channel.ts"
NODE_MODULES = REPO / "notifiers" / "claude-channel" / "node_modules"

pytestmark = [
    require_or_skip(
        hasattr(_socket, "AF_UNIX"),
        "claude-channel binds an AF_UNIX socket — not available on this platform",
        promised=js_promised(),
    ),
    require_or_skip(
        shutil.which("bun") is not None,
        "claude-channel runs under bun; no bun on PATH",
        promised=js_promised(),
    ),
    require_or_skip(
        NODE_MODULES.exists(),
        "channel deps not installed — run notifiers/claude-channel/install.sh",
        promised=js_promised(),
    ),
]


def _unique_name() -> str:
    """Inside `NAME_RE`, and unique per run: the derived path is a real one in
    `/tmp`, so two runs on one machine must not collide."""
    return f"t1477{os.getpid()}{time.time_ns() % 100000}"


def _run_until_bound(env_extra: dict[str, str], expect: str,
                     timeout: float = 20.0) -> tuple[bool, str]:
    """Start the consumer, wait for `expect` to appear, return (bound, stderr).

    The socket file is the observable: `channel.ts` creates it on `listen`, and
    which path it created is the entire question here. stderr comes back too
    because what the server *says* about a name it refused is the other half —
    a diagnostic naming the wrong cause is the defect, not the fallback.
    """
    proc = subprocess.Popen(
        ["bun", str(CHANNEL_TS)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(CHANNEL_TS.parent),
        env={**os.environ, **env_extra},
        text=True, bufsize=1, encoding="utf-8", errors="replace",
    )
    bound = False
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(expect):
                bound = True
                break
            if proc.poll() is not None:
                break
            time.sleep(0.05)
    finally:
        proc.kill()
        proc.wait(timeout=10)
        try:
            os.unlink(expect)
        except OSError:
            pass
    return bound, (proc.stderr.read() if proc.stderr else "")


def test_the_consumer_binds_the_socket_the_name_derives():
    name = _unique_name()
    expect = naming.sock_for(name)
    bound, _ = _run_until_bound({"SUPERTOOL_WATCH_NAME": name,
                                 "SUPERTOOL_WATCH_SOCK": ""}, expect)
    assert bound, (
        f"channel.ts did not bind {expect}; a name that configures the producers "
        f"and not the consumer is the half-configured state this closes")


def test_an_explicit_socket_still_wins_on_the_consumer_side_too():
    """Two ends with two precedence rules would be a new way to disagree."""
    name = _unique_name()
    explicit = naming.sock_for(f"{name}x")
    bound, _ = _run_until_bound({"SUPERTOOL_WATCH_NAME": name,
                                 "SUPERTOOL_WATCH_SOCK": explicit}, explicit)
    assert bound
    assert not os.path.exists(naming.sock_for(name)), (
        "the name's path was bound as well as the override's")


def test_a_name_the_python_side_refuses_is_refused_here_too():
    """A name that resolves to a private channel on one end and the default on
    the other is exactly the split both ends exist to avoid."""
    assert naming.resolve({"SUPERTOOL_WATCH_NAME": "../evil"}).refusal
    if os.path.exists(naming.DEFAULT_SOCK):
        pytest.skip("the default socket is already held by a live consumer")
    bound, err = _run_until_bound({"SUPERTOOL_WATCH_NAME": "../evil",
                                   "SUPERTOOL_WATCH_SOCK": ""}, naming.DEFAULT_SOCK)
    assert bound
    assert "not usable" in err, err


def test_an_unusable_name_is_reported_even_when_a_socket_overrides_it():
    """The two ends have to disagree about *nothing*, diagnostics included. With
    an explicit socket in play the old `resolveSockPath` returned before its own
    regex ever ran, so it announced that the socket had overridden a name — a
    name it had not looked at and would have refused. `naming.resolve` computes
    the refusal before precedence, and says both things."""
    name = _unique_name()
    explicit = naming.sock_for(f"{name}y")
    resolved = naming.resolve({"SUPERTOOL_WATCH_NAME": "../evil",
                               "SUPERTOOL_WATCH_SOCK": explicit})
    assert resolved.refusal, "the Python side refuses it regardless of the override"
    assert not [n for n in resolved.notes if "overrides" in n], (
        "there was no usable name for the socket to override")
    bound, err = _run_until_bound({"SUPERTOOL_WATCH_NAME": "../evil",
                                   "SUPERTOOL_WATCH_SOCK": explicit}, explicit)
    assert bound
    assert "not usable" in err, err
    assert "overrides" not in err, err
