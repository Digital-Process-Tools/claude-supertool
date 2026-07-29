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

Windows: these tests were the first thing in the suite to reach this code path
on a Windows runner, and they found a separate live bug there (#544) — the
adapters publish a fabricated `adapter` finding for a reason that has nothing
to do with the binary. Two tests are scoped away from it below, narrowly; see
the marker for why the scoping is about a false premise and not convenience.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_REFUSAL = _ROOT / "validators" / "common" / "refusal.py"

# Two of the tests below drive `main()` all the way to `ensure_daemon`, and they
# presuppose that the binary lookup is the first thing on that path that can
# fail. On Windows it is not, for a reason that is a separate live bug (#544):
# `_spawn.ensure_daemon` calls `socket_pid_paths` (line 334) before `preflight`
# (line 358), and `socket_pid_paths` -> `runtime_dir` calls `os.geteuid()`,
# which does not exist there. So the adapter raises before it ever looks for the
# binary, and publishes a fabricated `adapter` finding — the very defect this
# file is about, arriving one layer earlier.
#
# Skipped because the premise is untrue there, not because it is inconvenient:
# on such a platform there is no "missing binary" outcome to assert. The two
# tests that DO hold on Windows are deliberately left running — the must-stay-
# loud guard (which stubs `ensure_daemon` and so never reaches `runtime_dir`)
# and the `resolve_bin` raise-site test (which calls it directly). Those are the
# assertions that matter most, and #544 must not silence them either.
#
# When #544 lands, delete this marker and both usages.
reaches_the_binary_lookup_first = pytest.mark.skipif(
    not hasattr(os, "geteuid"),
    reason="#544: runtime_dir() raises on os.geteuid before the binary lookup "
           "is reached on this platform, so 'missing binary' is not the "
           "outcome under test here",
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

@reaches_the_binary_lookup_first
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


@reaches_the_binary_lookup_first
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
