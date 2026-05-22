"""Tests for the `notifiers` config block — fire-and-forget side-effect hooks.

Notifiers are the validator's read-friendly sibling: same `hooks_into`/`match` shape,
but spawn-and-forget (no rollback, no JSON receipt, no blocking the op).

A notifier writes its event to a side channel (Unix socket, log file, HTTP webhook).
Consumers like the Cursor witness extension listen on that channel.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import supertool


def _set_config(d: dict) -> None:
    supertool._CONFIG = d
    supertool._CONFIG_CHECKED = True


def test_applicable_notifiers_filters_by_op_and_match(tmp_path: Path) -> None:
    """Notifiers fire only when (op in hooks_into) AND (path matches glob)."""
    _set_config({
        "notifiers": {
            "watch-php": {
                "cmd": "true",
                "match": "*.php",
                "hooks_into": ["edit", "read"],
            }
        }
    })
    assert "watch-php" in supertool._applicable_notifiers("edit", "x.php")
    assert "watch-php" in supertool._applicable_notifiers("read", "x.php")
    # wrong op
    assert "watch-php" not in supertool._applicable_notifiers("paste", "x.php")
    # wrong extension
    assert "watch-php" not in supertool._applicable_notifiers("edit", "x.py")


def test_notifier_fires_on_edit(tmp_path: Path) -> None:
    """Mutating an edit-hooked notifier writes its side effect."""
    marker = tmp_path / "fired.txt"
    _set_config({
        "notifiers": {
            "marker": {
                "cmd": f"sh -c 'echo {{op}} {{file}} > {marker}'",
                "match": "*",
                "hooks_into": ["edit"],
            }
        }
    })
    supertool._run_notifiers("edit", str(tmp_path / "target.php"))
    # fire-and-forget — give the worker a moment
    deadline = time.time() + 2
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists()
    content = marker.read_text().strip()
    assert content.startswith("edit ")
    assert "target.php" in content


def test_notifier_fires_on_read(tmp_path: Path) -> None:
    """Notifiers can hook into read ops (validators can't)."""
    marker = tmp_path / "read-fired.txt"
    _set_config({
        "notifiers": {
            "read-watch": {
                "cmd": f"sh -c 'echo {{op}} {{file}} > {marker}'",
                "match": "*",
                "hooks_into": ["read"],
            }
        }
    })
    supertool._run_notifiers("read", str(tmp_path / "x.php"))
    deadline = time.time() + 2
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists()
    assert "read" in marker.read_text()


def test_notifier_does_not_block_caller(tmp_path: Path) -> None:
    """A slow notifier must not delay the calling op."""
    _set_config({
        "notifiers": {
            "slow": {
                "cmd": "sh -c 'sleep 5'",
                "match": "*",
                "hooks_into": ["edit"],
            }
        }
    })
    start = time.time()
    supertool._run_notifiers("edit", "x.php")
    elapsed = time.time() - start
    # Should return well under the 5s sleep. 2s threshold tolerates cold
    # python startup on slow CI runners while still catching real blocks.
    assert elapsed < 2.0, f"_run_notifiers blocked for {elapsed:.2f}s"


def test_notifier_failure_does_not_raise(tmp_path: Path) -> None:
    """A broken notifier cmd must not propagate exceptions to the caller."""
    _set_config({
        "notifiers": {
            "broken": {
                "cmd": "/nonexistent/binary",
                "match": "*",
                "hooks_into": ["edit"],
            }
        }
    })
    # Must not raise
    supertool._run_notifiers("edit", "x.php")


def test_notifiers_block_empty_when_unconfigured() -> None:
    """No notifiers block → empty dict, no spawn attempts."""
    _set_config({})
    assert supertool._applicable_notifiers("edit", "x.php") == {}
