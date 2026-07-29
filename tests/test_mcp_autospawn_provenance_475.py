"""#475 — a caller that cannot wait for a cold daemon must not create one.

The leak: `MCPClient.spawn()` fires a detached `daemon.py --detach` on the first
connect miss, then the daemon reparents to init and lives for the full
`IDLE_TIMEOUT_SEC` (600s) regardless of whether the caller is still alive. When
the caller is a validator with a 3s budget, the caller is killed long before a
cold LSP can answer, and the abandoned daemon indexes the repository to ~1.3 GB
RSS having served nothing.

The fix is provenance, not lifetime: supertool tells a validator's child process
(via `SUPERTOOL_MCP_AUTOSPAWN=0`, inherited by the grandchild `supertool diag:`)
that it may *use* a warm daemon but may not *create* one. Suppressed spawn fails
fast — no poll loop, no sleep — and the caller degrades to "MCP server
unavailable", which every MCP call site already handles.

Both directions are pinned: suppressed callers do not spawn, unsuppressed
(interactive) callers still do.
"""
from __future__ import annotations

import os
import socket as _socket
import subprocess
import time
import types
import uuid

import pytest

import supertool
from supertool import MCPClient, MCPServerError

_REQUIRES_AF_UNIX = pytest.mark.skipif(
    not hasattr(_socket, "AF_UNIX"),
    reason="MCP daemon uses AF_UNIX sockets — not supported on this platform",
)


def _client_that_will_miss(monkeypatch: pytest.MonkeyPatch) -> MCPClient:
    """An auto-spawning client whose socket path never connects."""
    def fake_connect(self, path):  # noqa: ANN001
        raise FileNotFoundError("no socket")

    monkeypatch.setattr(_socket.socket, "connect", fake_connect)
    client = MCPClient(name="py-lsp", timeout=1)
    client._sock_path = f"/tmp/st-475-{uuid.uuid4().hex[:8]}.sock"
    client._auto_spawn = True
    return client


@_REQUIRES_AF_UNIX
class TestAutoSpawnProvenance:
    """`SUPERTOOL_MCP_AUTOSPAWN=0` suppresses daemon creation, not daemon use."""

    def test_suppressed_caller_does_not_spawn_a_daemon(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of #475: no daemon is created for a caller that dies.

        A validator killed at its 3s budget leaves a daemon that indexes to
        ~1.3 GB and serves nobody for 600s. With provenance set, the daemon is
        never created at all.
        """
        popen_calls: list = []

        def capture_popen(args, **kwargs):  # noqa: ANN001
            popen_calls.append(args)
            raise AssertionError(
                f"#475: auto-spawn fired despite SUPERTOOL_MCP_AUTOSPAWN=0: {args!r}"
            )

        monkeypatch.setattr(subprocess, "Popen", capture_popen)
        monkeypatch.setenv("SUPERTOOL_MCP_AUTOSPAWN", "0")
        client = _client_that_will_miss(monkeypatch)

        with pytest.raises(MCPServerError):
            client.spawn()

        assert popen_calls == [], (
            "#475: a caller that cannot wait for a cold daemon must not create one"
        )

    def test_suppressed_caller_fails_fast_without_polling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No poll loop when we know nobody will ever bind the socket.

        Clock is injected — `time.sleep` is replaced by a recorder that never
        actually sleeps, so the assertion is on behaviour, not on wall time.
        """
        sleeps: list = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda *a, **k: pytest.fail("#475: spawned under suppression"),
        )
        monkeypatch.setenv("SUPERTOOL_MCP_AUTOSPAWN", "0")
        # A generous budget: if the poll loop runs at all, it sleeps many times.
        monkeypatch.setenv("SUPERTOOL_MCP_CONNECT_TIMEOUT", "60")
        client = _client_that_will_miss(monkeypatch)

        with pytest.raises(MCPServerError):
            client.spawn()

        assert sleeps == [], (
            f"#475: suppressed spawn must fail fast, not poll a path nobody will "
            f"bind — slept {len(sleeps)} times"
        )

    def test_suppressed_caller_still_uses_an_already_warm_daemon(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Suppression removes creation, never use — the warm path is the payoff.

        Pins that the fix did not degrade into "validators never talk to MCP".
        """
        connects: list = []

        def fake_connect(self, path):  # noqa: ANN001
            connects.append(path)

        monkeypatch.setattr(_socket.socket, "connect", fake_connect)
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda *a, **k: pytest.fail("#475: spawned when daemon was already warm"),
        )
        monkeypatch.setenv("SUPERTOOL_MCP_AUTOSPAWN", "0")

        client = MCPClient(name="py-lsp", timeout=1)
        client._sock_path = f"/tmp/st-475-{uuid.uuid4().hex[:8]}.sock"
        client._auto_spawn = True
        client.spawn()

        assert client._sock is not None, "warm daemon must still be connected to"
        assert connects, "expected a connect attempt against the warm socket"

    def test_unsuppressed_caller_still_spawns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other direction — an interactive op keeps its auto-spawn.

        Without this, "delete the Popen call" would pass the suite.
        """
        popen_calls: list = []

        def capture_popen(args, **kwargs):  # noqa: ANN001
            popen_calls.append(args)
            raise FileNotFoundError("captured")

        monkeypatch.setattr(subprocess, "Popen", capture_popen)
        monkeypatch.delenv("SUPERTOOL_MCP_AUTOSPAWN", raising=False)
        monkeypatch.setenv("SUPERTOOL_MCP_CONNECT_TIMEOUT", "0.05")
        client = _client_that_will_miss(monkeypatch)

        with pytest.raises(MCPServerError):
            client.spawn()

        assert popen_calls, (
            "interactive callers must keep auto-spawn — the warm daemon has to "
            "come from somewhere"
        )
        assert any("daemon" in str(a) for a in popen_calls[0]), (
            f"expected daemon.py in spawn args, got {popen_calls[0]!r}"
        )

    @pytest.mark.parametrize("value", ["0", "false", "no", "FALSE"])
    def test_falsey_values_all_suppress(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Provenance is a flag, not a trivia quiz about spelling."""
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda *a, **k: pytest.fail(f"#475: spawned with AUTOSPAWN={value}"),
        )
        monkeypatch.setenv("SUPERTOOL_MCP_AUTOSPAWN", value)
        client = _client_that_will_miss(monkeypatch)
        with pytest.raises(MCPServerError):
            client.spawn()


class TestValidatorProvenance:
    """`_validator_run_one` stamps provenance into the adapter's environment."""

    @staticmethod
    def _run(monkeypatch: pytest.MonkeyPatch, tmp_path, spec: dict) -> dict:
        """Run one validator with subprocess.run faked; return the child env."""
        captured: dict = {}

        def fake_run(argv, **kwargs):  # noqa: ANN001
            captured["env"] = kwargs.get("env")
            return types.SimpleNamespace(
                stdout='{"tool": "fake", "ok": true, "count": 0, "errors": []}',
                stderr="", returncode=0,
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        target = tmp_path / "x.py"
        target.write_text("x = 1\n", encoding="utf-8")
        supertool._validator_run_one("fake", spec, str(target))
        return captured

    def test_validator_children_may_not_create_daemons(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """The env var must reach the adapter — and through it the grandchild.

        `lsp-diag.py` shells `supertool diag:FILE`; the flag rides normal env
        inheritance across both hops, which is why no new plumbing is needed.
        """
        captured = self._run(
            monkeypatch, tmp_path,
            {"cmd": "echo {file}", "cache": False, "timeout": 3},
        )
        env = captured["env"]
        assert env is not None, (
            "#475: validator child env must be explicit so provenance can be stamped"
        )
        assert env.get("SUPERTOOL_MCP_AUTOSPAWN") == "0", (
            f"#475: expected SUPERTOOL_MCP_AUTOSPAWN=0 in validator child env, "
            f"got {env.get('SUPERTOOL_MCP_AUTOSPAWN')!r}"
        )

    def test_validator_can_opt_back_in(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """`mcp_autospawn: true` restores the old behaviour for one validator.

        Pins the escape hatch — otherwise the fix is unconditional and a project
        whose validator budget genuinely covers a cold start has no way back.
        """
        captured = self._run(
            monkeypatch, tmp_path,
            {"cmd": "echo {file}", "cache": False, "timeout": 300,
             "mcp_autospawn": True},
        )
        env = captured["env"]
        assert env is not None
        assert env.get("SUPERTOOL_MCP_AUTOSPAWN") == "1", (
            f"opt-in validator must be allowed to spawn, got "
            f"{env.get('SUPERTOOL_MCP_AUTOSPAWN')!r}"
        )

    def test_provenance_does_not_clobber_spec_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Stamping provenance must not drop the validator's own env block."""
        captured = self._run(
            monkeypatch, tmp_path,
            {"cmd": "echo {file}", "cache": False,
             "env": {"MY_VALIDATOR_VAR": "kept"}},
        )
        env = captured["env"]
        assert env.get("MY_VALIDATOR_VAR") == "kept"
        assert env.get("SUPERTOOL_MCP_AUTOSPAWN") == "0"
        assert "PATH" in env, "child env must still inherit the ambient environment"
