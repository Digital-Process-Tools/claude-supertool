"""`_mcp_stop_server` must distinguish a refusal from a success (#547).

The new-file invalidation path (#239) SIGTERMs a warm daemon so the next op
cold-starts one that has indexed the new file. It ran `stop.py` with both
streams on `DEVNULL` inside a bare `except: pass`, so *stopped*, *refused*,
*crashed* and *binary missing* all produced the same observable: nothing.

Suppression stays — invalidation is an optimization and must never block the
op. What changes is that the outcome is no longer discarded along with the
blocking. Two halves, both asserted here:

1. `stop.py` reports honestly. It used to return 0 even when the daemon was
   still alive after SIGKILL, so exit status alone could not have detected
   #239 recurring — the one thing this path exists to prevent.
2. `_mcp_stop_server` returns that outcome and logs a single debug-gated line
   when the stop did not succeed. Nothing reaches the op's normal output, on
   any path, ever — a background optimization must not become user-facing
   noise on every `edit:`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import supertool

sys.path.insert(0, str(Path(__file__).parent.parent / "presets" / "mcp"))

# stop.py reaches _paths.runtime_dir(), which refuses outright where
# os.geteuid does not exist (#544) — those tests are POSIX-only. The
# _mcp_stop_server half below never touches a runtime dir and runs everywhere.
posix_only = pytest.mark.skipif(
    not hasattr(os, "geteuid"),
    reason="stop.py's runtime dir is ownership-checked; os.geteuid is required.",
)


def _fake_stop_script(tmp_path: Path, code: int, stderr: str = "") -> str:
    """A stand-in for stop.py with a chosen exit code and stderr."""
    script = tmp_path / "fake_stop.py"
    script.write_text(
        "import sys\n"
        f"sys.stderr.write({stderr!r})\n"
        "sys.stdout.write('chatter that must never surface\\n')\n"
        f"sys.exit({code})\n",
        encoding="utf-8",
    )
    return str(script)


class TestStopScriptExitCodes:
    """stop.py's exit status must carry the outcome, not just 'it ran'."""

    @pytest.fixture
    def stop_mod(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(tmp_path / "rt"))
        import stop  # noqa: PLC0415

        return stop

    @posix_only
    def test_no_daemon_is_its_own_code(self, stop_mod) -> None:
        """Nothing to stop is benign and must not read as a failure."""
        assert stop_mod.main(["stop.py", "never-started"]) == stop_mod.EXIT_NO_DAEMON

    @posix_only
    def test_daemon_that_would_not_die_is_a_failure(
        self, stop_mod, monkeypatch, capsys
    ) -> None:
        """The #239 case: pidfile present, process still alive after SIGKILL.

        This returned 0 — indistinguishable from a clean stop.
        """
        _sock, pid_path = stop_mod.socket_pid_paths(os.path.abspath(os.getcwd()), "zombie")
        Path(pid_path).write_text("4242\n", encoding="utf-8")
        monkeypatch.setattr(stop_mod, "stop_pid", lambda pid: False)

        rc = stop_mod.main(["stop.py", "zombie"])

        assert rc == stop_mod.EXIT_STOP_FAILED
        assert "4242" in capsys.readouterr().err

    @posix_only
    def test_unreadable_pidfile_is_a_failure_not_a_success(
        self, stop_mod, monkeypatch
    ) -> None:
        """A pidfile we cannot parse leaves the daemon's fate unknown."""
        _sock, pid_path = stop_mod.socket_pid_paths(os.path.abspath(os.getcwd()), "garbled")
        Path(pid_path).write_text("not-a-pid\n", encoding="utf-8")

        assert stop_mod.main(["stop.py", "garbled"]) == stop_mod.EXIT_STOP_FAILED

    @posix_only
    def test_refusal_is_not_no_daemon(self, stop_mod, monkeypatch, capsys) -> None:
        """runtime_dir's SystemExit(str) must not land on the benign code.

        `sys.exit("...")` exits 1, which was exactly EXIT_NO_DAEMON until #574
        moved it — a refusal would otherwise be read as "there was nothing to
        stop". `1` is now unassigned and reads as a crash, so the assertion
        below is no longer the only thing between a refusal and an `ok`; it
        still pins that a stated refusal keeps its own name.
        """
        def _refuse(_cwd, _name):
            raise SystemExit("daemon: runtime dir owned by uid 0, not us (501).")

        monkeypatch.setattr(stop_mod, "socket_pid_paths", _refuse)

        rc = stop_mod.main(["stop.py", "php-lsp"])

        assert rc == stop_mod.EXIT_REFUSED
        assert rc != stop_mod.EXIT_NO_DAEMON
        assert "owned by uid 0" in capsys.readouterr().err

    @posix_only
    def test_numeric_systemexit_is_not_relabelled(self, stop_mod, monkeypatch) -> None:
        """Only a stated reason is a refusal; a bare numeric exit propagates."""
        def _boom(_cwd, _name):
            raise SystemExit(7)

        monkeypatch.setattr(stop_mod, "socket_pid_paths", _boom)

        with pytest.raises(SystemExit) as exc:
            stop_mod.main(["stop.py", "php-lsp"])
        assert exc.value.code == 7


class TestMcpStopServerOutcome:
    """The caller can now tell the four outcomes apart — and stays quiet."""

    def test_success_is_reported_and_silent(self, tmp_path, monkeypatch, capsys) -> None:
        monkeypatch.setattr(supertool, "_MCP_STOP_SCRIPT", _fake_stop_script(tmp_path, 0))
        monkeypatch.setenv("SUPERTOOL_DEBUG", "1")

        outcome = supertool._mcp_stop_server("php-lsp")

        assert outcome.ok is True
        assert outcome.code == "stopped"
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_no_daemon_is_ok_and_silent(self, tmp_path, monkeypatch, capsys) -> None:
        """The common case on every new file: no warm daemon was running.

        `5`, not `1`, since #574 — see `tests/test_mcp_stop_crash_574.py`.
        """
        monkeypatch.setattr(supertool, "_MCP_STOP_SCRIPT", _fake_stop_script(tmp_path, 5))
        monkeypatch.setenv("SUPERTOOL_DEBUG", "1")

        outcome = supertool._mcp_stop_server("php-lsp")

        assert outcome.ok is True
        assert outcome.code == "no-daemon"
        assert capsys.readouterr().err == ""

    def test_failure_is_distinguishable_from_success(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            supertool,
            "_MCP_STOP_SCRIPT",
            _fake_stop_script(tmp_path, 3, "  failed to stop pid=4242  "),
        )

        outcome = supertool._mcp_stop_server("php-lsp")

        assert outcome.ok is False
        assert outcome.code == "failed"
        assert "pid=4242" in outcome.detail

    def test_refusal_is_distinguishable_from_success(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            supertool,
            "_MCP_STOP_SCRIPT",
            _fake_stop_script(tmp_path, 4, "daemon: cannot verify ownership"),
        )

        outcome = supertool._mcp_stop_server("php-lsp")

        assert outcome.ok is False
        assert outcome.code == "refused"
        assert "cannot verify ownership" in outcome.detail

    def test_crash_is_distinguishable_from_success(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            supertool,
            "_MCP_STOP_SCRIPT",
            _fake_stop_script(tmp_path, 9, "Traceback (most recent call last):"),
        )

        outcome = supertool._mcp_stop_server("php-lsp")

        assert outcome.ok is False
        assert outcome.code == "crashed"

    def test_missing_interpreter_is_reported(self, tmp_path, monkeypatch) -> None:
        """The binary-missing case: spawn raises, and that is not a success."""
        monkeypatch.setattr(supertool, "_MCP_STOP_SCRIPT", _fake_stop_script(tmp_path, 0))
        monkeypatch.setattr(supertool.sys, "executable", str(tmp_path / "no-such-python"))

        outcome = supertool._mcp_stop_server("php-lsp")

        assert outcome.ok is False
        assert outcome.code == "unavailable"

    def test_detail_is_bounded(self, tmp_path, monkeypatch) -> None:
        """A daemon dumping megabytes of stderr must not be carried whole."""
        monkeypatch.setattr(
            supertool, "_MCP_STOP_SCRIPT", _fake_stop_script(tmp_path, 3, "x" * 50_000)
        )

        outcome = supertool._mcp_stop_server("php-lsp")

        assert outcome.ok is False
        assert 0 < len(outcome.detail) <= supertool._MCP_STOP_DETAIL_CAP


class TestFailureReporting:
    """Where the signal goes: stderr, debug-gated, never the op's output."""

    def test_failure_logs_nothing_without_debug(self, tmp_path, monkeypatch, capsys) -> None:
        monkeypatch.delenv("SUPERTOOL_DEBUG", raising=False)
        monkeypatch.setattr(
            supertool, "_MCP_STOP_SCRIPT", _fake_stop_script(tmp_path, 3, "failed to stop")
        )

        supertool._mcp_stop_server("php-lsp")

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_failure_logs_to_stderr_under_debug(self, tmp_path, monkeypatch, capsys) -> None:
        monkeypatch.setenv("SUPERTOOL_DEBUG", "1")
        monkeypatch.setattr(
            supertool,
            "_MCP_STOP_SCRIPT",
            _fake_stop_script(tmp_path, 3, "failed to stop pid=4242"),
        )

        supertool._mcp_stop_server("php-lsp")

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "php-lsp" in captured.err
        assert "failed" in captured.err
        assert "pid=4242" in captured.err


class TestRestartMcpHonesty:
    """`restartMcp` already prints a claim — it must stop being a false one."""

    def _op(self, monkeypatch, outcome):
        monkeypatch.setattr(supertool, "_mcp_stop_server", lambda name: outcome)
        monkeypatch.setattr(supertool, "_mcp_specs", {"phpstan-warm": {}})
        supertool._CONFIG = {"ops": {"clean": {"cmd": "echo ok", "restartMcp": True}}}
        return supertool._resolve_custom_op("clean", ["clean", "x"])

    def test_failed_stop_is_not_reported_as_restarted(self, monkeypatch) -> None:
        result = self._op(
            monkeypatch, supertool._StopOutcome(False, "failed", "failed to stop pid=1")
        )
        assert result is not None
        assert "restarted 1 daemon(s)" not in result
        assert "phpstan-warm" in result

    def test_successful_stop_still_reads_as_restarted(self, monkeypatch) -> None:
        result = self._op(monkeypatch, supertool._StopOutcome(True, "stopped", ""))
        assert result is not None
        assert "restarted 1 daemon(s)" in result
