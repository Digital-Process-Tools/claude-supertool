"""A warm-daemon adapter whose own binary is absent must skip, not report (#531).

The four warm-process adapters resolve their daemon binary before spawning it.
When that binary is not on disk — the normal case for any `cwd:` pointed at a
git worktree, where `composer install` never ran — `resolve_bin` raised, the
blanket `except Exception` in `main` caught it, and the adapter published a
finding:

    phpstan-mcp : 1 err  (pre-existing — not from this edit)
         adapter  RuntimeError: mcp-phpstan-warm not found at: /tmp/.../wt-foo

That is a checker reporting on a file it never opened. It is the same category
mistake `validators/common/refusal.py` was written for (#406) and the same shape
as #263: the abstraction existed, the call sites had not adopted it.

The hazard in the other direction is worse than the noise, so these tests pin
both edges:

- binary absent           -> `skipped`, with the missing path in the reason
- daemon present but sick -> still `ok: False`, still an `adapter` error

A single `except` that swallowed everything would fail the second half; an
implementation that changed nothing would fail the first.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_REFUSAL = _ROOT / "validators" / "common" / "refusal.py"

# The binary-lookup tests below presuppose that the lookup is the first thing
# that can fail. On a build without AF_UNIX it is not: there is no transport to
# reach a daemon over at all, and the platform decline fires first and rightly
# outranks it. That case is asserted on every platform by
# `test_a_build_without_the_daemon_transport_declines_before_looking`, which
# removes AF_UNIX rather than waiting for a runner that lacks it.
needs_uds = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="binary lookup is unreachable without AF_UNIX; the platform decline "
           "is asserted separately and on all platforms",
)

# (tool name, adapter path, bin env var, working-dir env var)
ADAPTERS = [
    ("phpstan-mcp", "validators/phpstan-mcp/phpstan-mcp.py",
     "MCP_PHPSTAN_BIN", "MCP_PHPSTAN_WORKING_DIR", "mcp-phpstan-warm"),
    ("rector-mcp", "validators/rector-mcp/rector-mcp.py",
     "MCP_RECTOR_BIN", "MCP_RECTOR_WORKING_DIR", "mcp-rector-warm"),
    ("phpmd-mcp", "validators/phpmd-mcp/phpmd-mcp.py",
     "MCP_PHPMD_BIN", "MCP_PHPMD_WORKING_DIR", "mcp-phpmd-warm"),
    ("phpunit-mcp", "validators/phpunit-mcp/phpunit-mcp.py",
     "MCP_PHPUNIT_BIN", "MCP_PHPUNIT_WORKING_DIR", "mcp-phpunit-warm"),
]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _refusal_mod():
    return _load(_REFUSAL, "refusal_531")


def _load_adapter(monkeypatch, tmp_path, rel: str, bin_env: str, cwd_env: str,
                  bin_value: str):
    """Import the adapter with its daemon binary pointed somewhere absent.

    Both env vars are read at import time, so they must be set first. The
    working dir is a fresh tmp dir so the socket path is unique and the spawn
    path — the only path that resolves the binary — is genuinely taken.
    """
    monkeypatch.setenv(bin_env, bin_value)
    monkeypatch.setenv(cwd_env, str(tmp_path))
    return _load(_ROOT / rel, f"adapter_531_{Path(rel).stem}_{bin_value.replace('/', '_')}")


def _run(mod, target: Path) -> dict:
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main(["adapter", str(target)])
    assert rc == 0
    return json.loads(buf.getvalue().strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# the third state
# ---------------------------------------------------------------------------

@needs_uds
@pytest.mark.parametrize("tool,rel,bin_env,cwd_env,bin_name", ADAPTERS)
def test_missing_daemon_binary_at_a_path_skips(
    monkeypatch, tmp_path, tool, rel, bin_env, cwd_env, bin_name
) -> None:
    """A configured relative path that is not on disk is an absence, not a finding."""
    target = tmp_path / "Foo.php"
    target.write_text("<?php\n")
    mod = _load_adapter(monkeypatch, tmp_path, rel, bin_env, cwd_env,
                        f"libs/bin/{bin_name}")

    result = _run(mod, target)

    assert "skipped" in result, f"{tool} reported instead of declining: {result}"
    assert bin_name in result["skipped"]
    assert "libs/bin" in result["skipped"], "the reason must name the path looked at"
    assert result["tool"] == tool
    # #515: the verdict keys are omitted on a skip, never padded.
    for key in ("ok", "count", "errors"):
        assert key not in result, f"{tool} padded {key} onto a skip"


@needs_uds
@pytest.mark.parametrize("tool,rel,bin_env,cwd_env,bin_name", ADAPTERS)
def test_missing_daemon_binary_on_path_skips(
    monkeypatch, tmp_path, tool, rel, bin_env, cwd_env, bin_name
) -> None:
    """A bare name absent from $PATH is the same absence, and keeps the install hint."""
    target = tmp_path / "Foo.php"
    target.write_text("<?php\n")
    monkeypatch.setenv("PATH", str(tmp_path))
    mod = _load_adapter(monkeypatch, tmp_path, rel, bin_env, cwd_env,
                        f"{bin_name}-absent-from-path")

    result = _run(mod, target)

    assert "skipped" in result, f"{tool} reported instead of declining: {result}"
    assert "composer" in result["skipped"], "the reason must stay discoverable"
    assert "\n" not in result["skipped"], "a skip reason renders on one row"
    for key in ("ok", "count", "errors"):
        assert key not in result


# ---------------------------------------------------------------------------
# the edge that must NOT go quiet
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool,rel,bin_env,cwd_env,bin_name", ADAPTERS)
def test_a_daemon_that_exists_and_fails_is_still_an_error(
    monkeypatch, tmp_path, tool, rel, bin_env, cwd_env, bin_name
) -> None:
    """Guessing towards silence is how a broken validator starts looking clean."""
    target = tmp_path / "Foo.php"
    target.write_text("<?php\n")
    mod = _load_adapter(monkeypatch, tmp_path, rel, bin_env, cwd_env,
                        f"libs/bin/{bin_name}")

    def boom(_cwd):
        raise RuntimeError("daemon exited during handshake")

    monkeypatch.setattr(mod, "ensure_daemon", boom)
    result = _run(mod, target)

    assert "skipped" not in result, f"{tool} silenced a real daemon failure"
    assert result["ok"] is False
    assert result["count"] == 1
    assert result["errors"][0]["code"] == "adapter"
    assert "daemon exited during handshake" in result["errors"][0]["msg"]


def test_the_marker_exception_is_a_runtimeerror() -> None:
    """Existing `except RuntimeError` call sites must keep catching it."""
    refusal = _refusal_mod()
    assert issubclass(refusal.DaemonUnavailable, RuntimeError)


@needs_uds
@pytest.mark.parametrize("tool,rel,bin_env,cwd_env,bin_name", ADAPTERS)
def test_resolve_bin_raises_the_marker_not_a_bare_runtimeerror(
    monkeypatch, tmp_path, tool, rel, bin_env, cwd_env, bin_name
) -> None:
    """The distinction lives at the raise site, not in a message-substring guess."""
    refusal = _refusal_mod()
    mod = _load_adapter(monkeypatch, tmp_path, rel, bin_env, cwd_env,
                        f"libs/bin/{bin_name}")
    with pytest.raises(Exception) as caught:
        mod.resolve_bin(str(tmp_path))
    assert type(caught.value).__name__ == refusal.DaemonUnavailable.__name__

# ---------------------------------------------------------------------------
# no transport on this platform — the same absence, one layer earlier
#
# Found by running the tests above on the Windows CI legs, where they failed
# for a reason that was not about the binary at all:
#
#   AttributeError: module 'os' has no attribute 'geteuid'
#     trace: if st.st_uid != os.geteuid():   (presets/mcp/_paths.py:66)
#
# The warm daemons speak over a Unix domain socket, and GH-hosted Windows
# Python builds do not expose `socket.AF_UNIX` — `supertool.py` has said so in
# a comment since the MCP client was written, and
# `tests/test_security_mcp_daemon_148.py` skips its whole module for it. So the
# daemon was never reachable on Windows; it reported an `adapter` error there
# instead, which is the exact defect this issue is about, one layer earlier and
# on a platform the issue never mentions.
#
# Fixing only the `geteuid` crash would have moved the failure three lines
# later into `socket.socket(socket.AF_UNIX, ...)`, so the decline is made
# where the knowledge is: no transport, nothing measured, `skipped`.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool,rel,bin_env,cwd_env,bin_name", ADAPTERS)
def test_a_build_without_the_daemon_transport_declines_before_looking(
    monkeypatch, tmp_path, tool, rel, bin_env, cwd_env, bin_name
) -> None:
    """No AF_UNIX means no daemon to reach, whatever is or is not installed.

    Asserted by removing the attribute rather than by waiting for a Windows
    runner, so the contract is checked on every leg. The binary is missing here
    too — the platform reason must win, because it is the one a reader can act
    on and the binary is irrelevant without a transport.
    """
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    target = tmp_path / "Foo.php"
    target.write_text("<?php\\n")
    mod = _load_adapter(monkeypatch, tmp_path, rel, bin_env, cwd_env,
                        f"libs/bin/{bin_name}")

    result = _run(mod, target)

    assert "skipped" in result, f"{tool} reported instead of declining: {result}"
    assert "AF_UNIX" in result["skipped"], (
        "the reason must name the missing transport, not the missing binary — "
        f"got {result['skipped']!r}")
    for key in ("ok", "count", "errors"):
        assert key not in result


def test_the_transport_check_is_the_marker_type() -> None:
    """One marker, one handler — the platform case reuses the mechanism."""
    refusal = _refusal_mod()
    assert refusal.daemon_transport_reason() is None or not hasattr(socket, "AF_UNIX")
    with pytest.raises(refusal.DaemonUnavailable):
        refusal.require_daemon_transport(has_uds=False)
    assert refusal.require_daemon_transport(has_uds=True) is None


# ---------------------------------------------------------------------------
# the ownership check must refuse, never wave through
# ---------------------------------------------------------------------------

def test_runtime_dir_refuses_when_ownership_cannot_be_verified(
    monkeypatch, tmp_path
) -> None:
    """A security check that cannot run must stop, not silently pass.

    `runtime_dir()` refuses to trust a directory owned by another uid. Where
    `os.geteuid` does not exist that comparison is not merely unavailable, it
    is unanswerable — `st_uid` is a constant 0 on Windows and carries no
    information. Defaulting it to "ours" would trade a loud failure for a quiet
    one on the one check whose whole job is to be suspicious.
    """
    sys.path.insert(0, str(_ROOT / "presets" / "mcp"))
    paths = importlib.import_module("_paths")
    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.delattr(os, "geteuid", raising=False)

    with pytest.raises(SystemExit) as exited:
        paths.runtime_dir()
    assert "ownership" in str(exited.value).lower()
    assert "SUPERTOOL_RUNTIME_DIR" in str(exited.value)


def test_runtime_dir_is_unchanged_where_ownership_can_be_verified(
    monkeypatch, tmp_path
) -> None:
    sys.path.insert(0, str(_ROOT / "presets" / "mcp"))
    paths = importlib.import_module("_paths")
    target = tmp_path / "rt"
    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(target))
    assert paths.runtime_dir() == str(target)
