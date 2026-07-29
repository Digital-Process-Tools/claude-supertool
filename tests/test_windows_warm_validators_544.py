"""Warm validators must not fabricate an `adapter` finding on Windows (#544).

On a Python build without `socket.AF_UNIX` — every GH-hosted Windows build —
there is no transport to reach a warm daemon over, so no warm validator has
ever worked there. It did not say so. It walked into `_spawn.ensure_daemon`,
which computes `socket_pid_paths` (line 334) *before* running `preflight`
(line 358), and `socket_pid_paths` -> `runtime_dir` calls `os.geteuid()`, which
Windows also lacks. The resulting `AttributeError` was caught by the adapter's
blanket `except Exception` and published as a finding about the file:

    phpstan-mcp : 1 err  (pre-existing — not from this edit)
         adapter  AttributeError: module 'os' has no attribute 'geteuid'

A checker reporting on a file it never opened — #406's defect, on a platform
the original issue never mentions. Worse than noise: it adds a `+1` to the
before/after delta that no edit caused, and can revert a good edit through
`rollback_on_fail`.

**The platform is asserted by removing the attribute, on every platform.** A
`sys.platform` guard would leave the contract untested everywhere except the
one runner that happens to lack it — which is how this shipped unnoticed in the
first place. `del socket.AF_UNIX` and `del os.geteuid` reproduce the Windows
tracebacks byte for byte on macOS and Linux.

The seam
--------
The decline lives **inside each adapter's `ensure_daemon`**, not at the top of
`main`. That placement is the entire lesson of the reverted first attempt
(`b704c81`): three suites stub the daemon layer with
`monkeypatch.setattr(mod, "ensure_daemon", ...)`, and a platform check ahead of
that assignment short-circuits before the stub can take effect. Eight green
tests went green-for-the-wrong-reason, including
`test_a_daemon_that_exists_and_fails_is_still_an_error` — the guard whose whole
job is to assert that a real daemon failure stays loud. The fix reproduced the
bug it was written to prevent.

Because the check sits in the body of the function the stubs replace, a stub
still wins. The last two tests here pin that directly: they strip `AF_UNIX`
*and* stub the daemon, and assert the stub was reached.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import socket
import sys
from pathlib import Path

import pytest

import supertool

_ROOT = Path(__file__).parent.parent
_REFUSAL = _ROOT / "validators" / "common" / "refusal.py"

# (tool name, adapter path, bin env var, working-dir env var, bin name)
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
    return _load(_REFUSAL, "refusal_544")


def _load_adapter(monkeypatch, tmp_path, rel, bin_env, cwd_env, bin_value):
    """Import the adapter with its binary and working dir pointed at `tmp_path`.

    Both env vars are read at import time, so they must be set before the load.
    """
    monkeypatch.setenv(bin_env, bin_value)
    monkeypatch.setenv(cwd_env, str(tmp_path))
    return _load(_ROOT / rel, f"adapter_544_{Path(rel).stem}_{abs(hash(bin_value))}")


def _installed_bin(tmp_path: Path, bin_name: str) -> str:
    """An absolute path to a binary that exists — so the lookup cannot be the reason."""
    p = tmp_path / "bin" / bin_name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(0o755)
    return str(p)


def _run(mod, target: Path) -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main(["adapter", str(target)])
    assert rc == 0
    return json.loads(buf.getvalue().strip().splitlines()[-1])


def _no_windows_daemon_facilities(monkeypatch) -> None:
    """Exactly what a GH-hosted Windows Python build is missing, and nothing else."""
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    monkeypatch.delattr(os, "geteuid", raising=False)


# ---------------------------------------------------------------------------
# the defect: an absence reported as a finding about the file
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool,rel,bin_env,cwd_env,bin_name", ADAPTERS)
def test_no_transport_skips_instead_of_fabricating_an_adapter_finding(
    monkeypatch, tmp_path, tool, rel, bin_env, cwd_env, bin_name
) -> None:
    """The reproduction. Binary installed, platform cannot reach a daemon."""
    target = tmp_path / "Foo.php"
    target.write_text("<?php\n")
    mod = _load_adapter(monkeypatch, tmp_path, rel, bin_env, cwd_env,
                        _installed_bin(tmp_path, bin_name))
    _no_windows_daemon_facilities(monkeypatch)

    result = _run(mod, target)

    assert "skipped" in result, f"{tool} reported a verdict it never measured: {result}"
    assert not any(e.get("code") == "adapter" for e in result.get("errors") or [])
    assert "geteuid" not in json.dumps(result), (
        "the tool's own missing attribute must never appear as a fact about the file")
    assert result["tool"] == tool
    # #515: a skip omits the verdict keys rather than padding them.
    for key in ("ok", "count", "errors"):
        assert key not in result, f"{tool} padded {key} onto a skip"


@pytest.mark.parametrize("tool,rel,bin_env,cwd_env,bin_name", ADAPTERS)
def test_the_skip_reason_names_the_missing_transport(
    monkeypatch, tmp_path, tool, rel, bin_env, cwd_env, bin_name
) -> None:
    """`skipped` with no actionable reason is a shrug. Name what is absent."""
    target = tmp_path / "Foo.php"
    target.write_text("<?php\n")
    mod = _load_adapter(monkeypatch, tmp_path, rel, bin_env, cwd_env,
                        _installed_bin(tmp_path, bin_name))
    _no_windows_daemon_facilities(monkeypatch)

    reason = _run(mod, target)["skipped"]

    assert "AF_UNIX" in reason, reason
    assert "\n" not in reason, "a skip reason renders on one row"


@pytest.mark.parametrize("tool,rel,bin_env,cwd_env,bin_name", ADAPTERS)
def test_a_missing_binary_outranks_the_missing_transport(
    monkeypatch, tmp_path, tool, rel, bin_env, cwd_env, bin_name
) -> None:
    """Both are skips; only one of them is the reader's next action.

    The reverted attempt declined on the platform first, which made "missing
    binary" unreachable on Windows and forced two skipifs onto
    `test_validator_daemon_unavailable_531.py`. Those are removed by this
    change, so the binary reason has to survive here — on a build with no
    transport, `install it` is still the more useful sentence than `this Python
    cannot reach a daemon`, and the platform reason is what a reader gets once
    the thing is actually installed.
    """
    target = tmp_path / "Foo.php"
    target.write_text("<?php\n")
    mod = _load_adapter(monkeypatch, tmp_path, rel, bin_env, cwd_env,
                        f"libs/bin/{bin_name}")
    _no_windows_daemon_facilities(monkeypatch)

    reason = _run(mod, target)["skipped"]

    assert bin_name in reason, reason
    assert "libs/bin" in reason, "the reason must name the path looked at"


# ---------------------------------------------------------------------------
# the seam — a stub must still win, on a platform with no transport
#
# This is the pair the first attempt broke. Both assert that the platform
# decline does NOT fire ahead of the point where the suites inject their fake
# daemon; without them, a check moved back to the top of `main()` looks green.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool,rel,bin_env,cwd_env,bin_name", ADAPTERS)
def test_a_real_daemon_failure_stays_loud_even_without_a_transport(
    monkeypatch, tmp_path, tool, rel, bin_env, cwd_env, bin_name
) -> None:
    """The guard the first attempt silenced with the fix meant to protect it.

    A stubbed `ensure_daemon` never needed a real socket, which is exactly why
    `test_a_daemon_that_exists_and_fails_is_still_an_error` passed on Windows
    before. A platform decline placed ahead of the stub turned that assertion
    into a skip and the suite stopped testing anything.
    """
    target = tmp_path / "Foo.php"
    target.write_text("<?php\n")
    mod = _load_adapter(monkeypatch, tmp_path, rel, bin_env, cwd_env,
                        _installed_bin(tmp_path, bin_name))
    _no_windows_daemon_facilities(monkeypatch)

    def boom(_cwd):
        raise RuntimeError("daemon exited during handshake")

    monkeypatch.setattr(mod, "ensure_daemon", boom)
    result = _run(mod, target)

    assert "skipped" not in result, f"{tool} silenced a real daemon failure"
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "adapter"
    assert "daemon exited during handshake" in result["errors"][0]["msg"]


@pytest.mark.parametrize("tool,rel,bin_env,cwd_env,bin_name", ADAPTERS)
def test_a_stubbed_daemon_is_still_contacted_without_a_transport(
    monkeypatch, tmp_path, tool, rel, bin_env, cwd_env, bin_name
) -> None:
    """`test_validators_phpstan_paths_412` records contact to prove a no-op cannot pass.

    Its recorder is injected the same way. If the platform check fires first,
    the recorder stays empty and every assertion in that file about *reaching*
    the daemon becomes vacuous while still reporting green.
    """
    target = tmp_path / "Foo.php"
    target.write_text("<?php\n")
    mod = _load_adapter(monkeypatch, tmp_path, rel, bin_env, cwd_env,
                        _installed_bin(tmp_path, bin_name))
    _no_windows_daemon_facilities(monkeypatch)

    contacted: list = []
    monkeypatch.setattr(mod, "ensure_daemon",
                        lambda cwd: contacted.append(cwd) or "/sock")
    monkeypatch.setattr(
        mod, "ndjson_call",
        lambda s, f: {"jsonrpc": "2.0", "id": 2,
                      "result": {"structuredContent": {"errors": [], "exit_code": 0}}})

    _run(mod, target)

    assert contacted, (
        f"{tool} short-circuited before the injection seam — the suites that "
        "stub the daemon layer would pass without exercising anything")


# ---------------------------------------------------------------------------
# the reason helper
# ---------------------------------------------------------------------------

def test_the_transport_reason_is_injectable_and_none_when_reachable() -> None:
    """Injectable so the contract is asserted on every platform, not only Windows."""
    refusal = _refusal_mod()
    assert refusal.daemon_transport_reason(has_uds=True) is None
    reason = refusal.daemon_transport_reason(has_uds=False)
    assert reason and "AF_UNIX" in reason and "\n" not in reason


def test_the_transport_decline_reuses_the_daemon_unavailable_marker() -> None:
    """One marker, one handler — no second code path in `main`."""
    refusal = _refusal_mod()
    assert issubclass(refusal.DaemonUnavailable, RuntimeError)


# ---------------------------------------------------------------------------
# the ownership check: unanswerable is not the same as safe to default
# ---------------------------------------------------------------------------

def test_runtime_dir_refuses_where_ownership_cannot_be_verified(
    monkeypatch, tmp_path
) -> None:
    """A security check that cannot run must stop, not quietly pass.

    `runtime_dir()` refuses a runtime directory a co-tenant owns (#148). Where
    `os.geteuid` does not exist that comparison is not merely unavailable, it
    is unanswerable: `st_uid` is a constant `0` on Windows and carries no
    ownership information at all. Defaulting it to "ours" would trade a loud
    failure for a quiet one on the single check whose whole job is to be
    suspicious.
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
    """Characterization: the platforms that can answer the question still do."""
    sys.path.insert(0, str(_ROOT / "presets" / "mcp"))
    paths = importlib.import_module("_paths")
    target = tmp_path / "rt"
    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", str(target))
    assert paths.runtime_dir() == str(target)


# ---------------------------------------------------------------------------
# supertool's own MCP client reaches the same dead end one line too late
# ---------------------------------------------------------------------------

def test_mcp_client_reports_the_missing_transport_instead_of_crashing(
    monkeypatch
) -> None:
    """`MCPClient.__init__` computed a runtime path before checking the transport.

    The AF_UNIX knowledge was already written in `spawn()`, but `__init__`
    resolves the socket path first — through the same `runtime_dir()` — so on
    Windows it raised `AttributeError` before reaching the sentence that
    explains the platform. `_mcp_ensure_server` catches `MCPServerError` and
    falls back to the non-MCP heuristic path; it does not catch
    `AttributeError`, and it would not catch the `SystemExit` that
    `runtime_dir()` now raises there either.
    """
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    monkeypatch.delattr(os, "geteuid", raising=False)

    with pytest.raises(supertool.MCPServerError) as caught:
        supertool.MCPClient(name="cclsp", timeout=5)

    assert "AF_UNIX" in str(caught.value)


def test_mcp_client_with_an_explicit_socket_path_is_untouched(monkeypatch) -> None:
    """Characterization: an externally managed socket does not resolve a runtime dir."""
    client = supertool.MCPClient(name="cclsp", timeout=5, socket_path="/tmp/x.sock")
    assert client._sock_path == "/tmp/x.sock"
