"""SUPERTOOL_WATCH_SOCK is honoured by the consumer and ignored by every
producer (#581). `notifiers/claude-channel/channel.ts:35` reads the env var;
`presets/watch/transport.py:30` was a plain constant. Four shipped surfaces
(the #550 refusal message, three lines in notifiers/claude-channel/README.md,
one of them a security claim) tell an operator to set it on producers too —
advice that did nothing.

This pins three things:
1. `SOCK_PATH` honours the override (and falls back correctly without one).
2. `poller_env()` — what a poller actually runs under when it relaunches
   itself under its own argv (`poller_argv`) — carries the override through,
   and a real child process reading that environment sees it.
3. A watcher's state file records which socket path it is actually bound to,
   so a poller spawned before the operator changed the variable is
   observable rather than a silent partial migration (some watchers deliver,
   others don't, and the board reads as healthy either way).
"""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

WATCH_DIR = str(Path(__file__).parent.parent / "presets" / "watch")
FIXTURE = str(Path(__file__).parent / "fixtures" / "print_watch_sock_path.py")

if WATCH_DIR not in sys.path:
    sys.path.insert(0, WATCH_DIR)
import transport  # noqa: E402


def _reload_with_env(monkeypatch, value: str | None) -> str:
    """Reload transport.py under a given SUPERTOOL_WATCH_SOCK and return the
    SOCK_PATH it computes. A reload (not a fresh interpreter) is enough here:
    the module-level assignment is what's under test, and reload re-executes
    it exactly as a real first import would."""
    if value is None:
        monkeypatch.delenv("SUPERTOOL_WATCH_SOCK", raising=False)
    else:
        monkeypatch.setenv("SUPERTOOL_WATCH_SOCK", value)
    importlib.reload(transport)
    return transport.SOCK_PATH


def _restore_default_transport(monkeypatch) -> None:
    """Every test here mutates the shared, process-wide `transport` module by
    reloading it. Leaving it pointed at a test path would leak into whichever
    test runs next in this worker."""
    _reload_with_env(monkeypatch, None)


def test_sock_path_honours_the_override(monkeypatch) -> None:
    try:
        got = _reload_with_env(monkeypatch, "/tmp/supertool-watch-581-custom.sock")
        assert got == "/tmp/supertool-watch-581-custom.sock"
    finally:
        _restore_default_transport(monkeypatch)


def test_sock_path_falls_back_to_the_default_without_the_override(monkeypatch) -> None:
    try:
        got = _reload_with_env(monkeypatch, None)
        assert got == "/tmp/supertool-watch.sock"
    finally:
        _restore_default_transport(monkeypatch)


def test_sock_path_falls_back_on_an_empty_override(monkeypatch) -> None:
    """Matches STATE_DIR's `or` idiom: an empty string is not a path, it is
    the absence of one, and must not be handed to socket.connect()."""
    try:
        got = _reload_with_env(monkeypatch, "")
        assert got == "/tmp/supertool-watch.sock"
    finally:
        _restore_default_transport(monkeypatch)


def test_poller_env_carries_the_override(monkeypatch) -> None:
    """`poller_env()` is what a poller actually runs under when it
    relaunches itself. If the override didn't reach this dict, a poller
    spawned after the operator set the variable would still bind the
    default socket downstream."""
    try:
        monkeypatch.setenv("SUPERTOOL_WATCH_SOCK", "/tmp/supertool-watch-581-relaunched.sock")
        importlib.reload(transport)
        env = transport.poller_env()
        assert env.get("SUPERTOOL_WATCH_SOCK") == "/tmp/supertool-watch-581-relaunched.sock"
    finally:
        _restore_default_transport(monkeypatch)


def test_a_real_child_process_under_poller_env_sees_the_override(monkeypatch) -> None:
    """End-to-end version of the above: actually launch a process the way a
    poller would (its own env, nothing inherited implicitly) and read back
    what it resolves SOCK_PATH to."""
    try:
        monkeypatch.setenv("SUPERTOOL_WATCH_SOCK", "/tmp/supertool-watch-581-child.sock")
        importlib.reload(transport)
        env = transport.poller_env()
        proc = subprocess.run(
            [sys.executable, FIXTURE, WATCH_DIR],
            env=env,
            capture_output=True,
            timeout=10,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"child failed: {proc.stderr}"
        assert proc.stdout.strip() == "/tmp/supertool-watch-581-child.sock"
    finally:
        _restore_default_transport(monkeypatch)


def test_a_watcher_records_which_socket_path_it_is_actually_using(monkeypatch, tmp_path) -> None:
    """A partial migration — some watchers still on the old path, some on the
    new one — must be inspectable rather than inferred from "some events
    arrived". Each watcher's own state file is the place every other
    per-process fact already lives (`only`, `first_seen`), so this is where a
    reader checking for a stray watcher looks."""
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(transport, "SOCK_PATH", "/tmp/supertool-watch-581-mine.sock")
    monkeypatch.setattr(transport, "emit_socket", lambda *_a, **_kw: None)
    monkeypatch.setattr(transport, "desktop_notify", lambda *_a, **_kw: None)

    transport.emit_event("gitlab-mr", "581", "merged", {})

    state = json.loads(
        (tmp_path / "supertool-watch-gitlab-mr__581.state.json").read_text(encoding="utf-8")
    )
    assert state.get("sock_path") == "/tmp/supertool-watch-581-mine.sock", (
        f"state file does not record the socket path this watcher is bound to: {state!r}"
    )
