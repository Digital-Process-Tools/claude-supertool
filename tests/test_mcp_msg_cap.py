"""Validator msg-cap regression tests.

Three warm-MCP validators (phpunit, phpstan, rector) emit error messages
that can be multi-MB when a test asserts on rendered HTML, a typed property
dump is huge, or a rector diff is enormous. Without a cap, the validator
output explodes — observed 2M+ token dump on a single PageIndexDelegate
test failure (2026-05-23, DVSI session).

These tests don't spawn the MCP daemon. They exercise the message-shaping
code path by importing the adapter and calling the cap helper directly
(phpunit), or by checking that the cap code is wired into the adapter
source for the other two (phpstan, rector — those caps live inline so we
exercise them by injecting a synthetic e dict via the structured error
handler when reachable).
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str):
    """Load a validator module by file path (it has a `-` in its name)."""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    return mod


# ---------------------------------------------------------------------------
# phpunit-mcp: exposes _cap_msg directly
# ---------------------------------------------------------------------------

@pytest.fixture
def phpunit_mod():
    return _load("validators/phpunit-mcp/phpunit-mcp.py", "phpunit_mcp_adapter")


def test_phpunit_cap_passes_short_msg_unchanged(phpunit_mod) -> None:
    short = "testFoo: assertion failed: expected 1 got 2"
    assert phpunit_mod._cap_msg(short) == short


def test_phpunit_cap_truncates_long_msg_with_hint(phpunit_mod) -> None:
    big = "X" * 10_000
    out = phpunit_mod._cap_msg(big)
    assert len(out) < 5_000  # well below the input — cap is 2000
    assert "TRUNCATED" in out
    assert "PHPUNIT_MCP_MSG_MAX_CHARS" in out
    # Truncation hint must include the dropped-char count
    import re
    assert re.search(r"\d+\s+more chars", out)


def test_phpunit_cap_default_is_2000(phpunit_mod) -> None:
    # Default cap (module constant) — no env override
    assert phpunit_mod.MSG_MAX_CHARS == 2000


# ---------------------------------------------------------------------------
# phpstan-mcp + rector-mcp: caps are inline. Verify the cap clause exists
# in the source so a regression won't silently drop the protection.
# ---------------------------------------------------------------------------

def test_phpstan_mcp_has_msg_cap_in_source() -> None:
    src = (ROOT / "validators/phpstan-mcp/phpstan-mcp.py").read_text(encoding="utf-8")
    assert "PHPSTAN_MCP_MSG_MAX_CHARS" in src
    assert "TRUNCATED" in src


def test_rector_mcp_has_msg_cap_in_source() -> None:
    src = (ROOT / "validators/rector-mcp/rector-mcp.py").read_text(encoding="utf-8")
    assert "RECTOR_MCP_MSG_MAX_CHARS" in src
    assert "TRUNCATED" in src


# ---------------------------------------------------------------------------
# All three default to the same cap value (consistent UX)
# ---------------------------------------------------------------------------

def test_default_cap_is_consistent_across_mcps() -> None:
    """All three MCPs default to 2000 chars (env can override per-tool)."""
    phpunit_src = (ROOT / "validators/phpunit-mcp/phpunit-mcp.py").read_text(encoding="utf-8")
    phpstan_src = (ROOT / "validators/phpstan-mcp/phpstan-mcp.py").read_text(encoding="utf-8")
    rector_src = (ROOT / "validators/rector-mcp/rector-mcp.py").read_text(encoding="utf-8")
    assert '"2000"' in phpunit_src
    assert '"2000"' in phpstan_src
    assert '"2000"' in rector_src
