"""Regression tests for #148: MCP daemon UDS hardening.

Covers the new path layout (runtime dir, NOT /tmp), name validation, and
the _paths helper module.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import socket as _socket

import pytest

pytestmark = pytest.mark.skipif(
    not hasattr(_socket, "AF_UNIX"),
    reason="MCP daemon module imports require AF_UNIX — not available on Windows runners.",
)

# Make presets/mcp importable as a flat module dir.
sys.path.insert(0, str(Path(__file__).parent.parent / "presets" / "mcp"))
if hasattr(_socket, "AF_UNIX"):
    import _paths  # noqa: E402
    import daemon as mcp_daemon  # noqa: E402
else:
    _paths = None  # type: ignore[assignment]
    mcp_daemon = None  # type: ignore[assignment]


@pytest.fixture
def tmp_runtime(tmp_path, monkeypatch):
    """Force runtime_dir() to a writable tmp_path subdir."""
    rd = tmp_path / "rt"
    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(rd))
    yield rd


class TestRuntimeDir:
    def test_creates_owner_only_dir(self, tmp_runtime):
        path = _paths.runtime_dir()
        assert path == str(tmp_runtime)
        st = os.stat(path)
        assert stat.S_IMODE(st.st_mode) == 0o700, \
            f"runtime dir must be mode 0700, got {oct(stat.S_IMODE(st.st_mode))}"

    def test_existing_dir_owned_by_us_ok(self, tmp_runtime):
        # Pre-create with 0700
        tmp_runtime.mkdir(parents=True, exist_ok=True)
        os.chmod(tmp_runtime, 0o700)
        assert _paths.runtime_dir() == str(tmp_runtime)

    def test_override_takes_precedence(self, tmp_path, monkeypatch):
        # Set both XDG and our override — override wins.
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
        override = tmp_path / "explicit"
        monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(override))
        assert _paths.runtime_dir() == str(override)


class TestSocketPidPaths:
    def test_paths_under_runtime_dir(self, tmp_runtime):
        sock, pid = _paths.socket_pid_paths("/some/cwd", "my-server")
        assert sock.startswith(str(tmp_runtime) + os.sep + "supertool-mcp-")
        assert pid.startswith(str(tmp_runtime) + os.sep + "supertool-mcp-")
        assert sock.endswith(".sock")
        assert pid.endswith(".pid")

    def test_paths_not_under_bare_tmp(self, monkeypatch):
        """The whole point of #148 — socket no longer in `/tmp/supertool-mcp-*`.

        We unset all overrides + XDG so runtime_dir picks the platform default
        (`~/Library/Caches/...` on macOS, `~/.cache/...` on Linux fallback).
        Neither should land under `/tmp/`.
        """
        monkeypatch.delenv("SUPERTOOL_RUNTIME_DIR", raising=False)
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        sock, pid = _paths.socket_pid_paths("/cwd", "name")
        assert not sock.startswith("/tmp/supertool-mcp-"), \
            f"#148 regression: socket back in bare /tmp/: {sock}"
        assert not pid.startswith("/tmp/supertool-mcp-"), \
            f"#148 regression: pidfile back in bare /tmp/: {pid}"

    def test_same_inputs_same_paths(self, tmp_runtime):
        """Deterministic — supervisor + status + stop must all compute the same path."""
        a = _paths.socket_pid_paths("/cwd", "name")
        b = _paths.socket_pid_paths("/cwd", "name")
        assert a == b


class TestNameValidation:
    @pytest.mark.parametrize("name", [
        "normal-name",
        "abc_123",
        "X",
        "a" * 64,
    ])
    def test_valid_names_accepted(self, name):
        # No exception, no exit
        mcp_daemon._validate_name(name)

    @pytest.mark.parametrize("name", [
        "",                          # empty
        "a" * 65,                    # too long
        "../etc",                    # path traversal
        "name with spaces",
        "name\x00null",
        "name/with/slashes",
        "name.with.dots",
        "name;rm -rf",
        "name$VAR",
        "name`echo`",
    ])
    def test_invalid_names_rejected(self, name):
        with pytest.raises(SystemExit, match="invalid server name"):
            mcp_daemon._validate_name(name)


class TestListPidfiles:
    def test_empty_when_no_daemons(self, tmp_runtime):
        assert _paths.list_pidfiles() == []

    def test_lists_only_matching_files(self, tmp_runtime):
        tmp_runtime.mkdir(parents=True, exist_ok=True)
        (tmp_runtime / "supertool-mcp-abc123.pid").write_text("123")
        (tmp_runtime / "supertool-mcp-def456.pid").write_text("456")
        # Decoy files that should NOT match
        (tmp_runtime / "supertool-mcp-abc123.sock").write_text("")
        (tmp_runtime / "other.pid").write_text("999")
        result = _paths.list_pidfiles()
        names = sorted(os.path.basename(p) for p in result)
        assert names == ["supertool-mcp-abc123.pid", "supertool-mcp-def456.pid"]
