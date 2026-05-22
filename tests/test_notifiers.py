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


def test_notifier_debug_log_off_by_default(tmp_path: Path, monkeypatch) -> None:
    """No SUPERTOOL_NOTIFIER_DEBUG, no notifier_debug in config → no log file written."""
    log = tmp_path / "notifier-debug.log"
    monkeypatch.setenv("SUPERTOOL_NOTIFIER_DEBUG_LOG", str(log))
    monkeypatch.delenv("SUPERTOOL_NOTIFIER_DEBUG", raising=False)
    _set_config({
        "notifiers": {
            "n": {"cmd": "true", "match": "*", "hooks_into": ["edit"]}
        }
    })
    supertool._run_notifiers("edit", "x.php")
    time.sleep(0.2)
    assert not log.exists(), "debug log written despite being off"


def test_notifier_debug_log_via_env(tmp_path: Path, monkeypatch) -> None:
    """SUPERTOOL_NOTIFIER_DEBUG=1 → logger writes notifier dispatch info."""
    log = tmp_path / "notifier-debug.log"
    monkeypatch.setenv("SUPERTOOL_NOTIFIER_DEBUG", "1")
    monkeypatch.setenv("SUPERTOOL_NOTIFIER_DEBUG_LOG", str(log))
    _set_config({
        "notifiers": {
            "n": {"cmd": "true", "match": "*.php", "hooks_into": ["edit"]}
        }
    })
    supertool._run_notifiers("edit", "x.php")
    time.sleep(0.2)
    assert log.exists(), "debug log not written"
    content = log.read_text()
    assert "edit" in content
    assert "x.php" in content


def test_notifier_debug_log_via_config(tmp_path: Path, monkeypatch) -> None:
    """Config `notifier_debug: true` enables logging (no env needed)."""
    log = tmp_path / "notifier-debug.log"
    monkeypatch.delenv("SUPERTOOL_NOTIFIER_DEBUG", raising=False)
    monkeypatch.setenv("SUPERTOOL_NOTIFIER_DEBUG_LOG", str(log))
    _set_config({
        "notifier_debug": True,
        "notifiers": {
            "n": {"cmd": "true", "match": "*", "hooks_into": ["edit"]}
        }
    })
    supertool._run_notifiers("edit", "y.php")
    time.sleep(0.2)
    assert log.exists()
    assert "y.php" in log.read_text()


def test_empty_placeholders_preserve_positional_argv(tmp_path: Path) -> None:
    """Empty {line}/{line_end}/{before_file} must NOT collapse argv slots.

    Regression: when supertool replaced empty placeholders with the empty string,
    shlex.split collapsed adjacent spaces, shifting subsequent positional args.
    Consumers like notify.py read argv[5] and got the wrong value (or IndexError).

    Each placeholder must round-trip as an explicit empty arg.
    """
    # The notifier echoes its full argv list (one arg per line) into a marker
    # file. We then count lines + check positional values.
    marker = tmp_path / "argv.json"
    _set_config({
        "notifiers": {
            "echo-argv": {
                # 5 positional slots after the script — same shape as notify.py
                "cmd": f'python3 -c "import sys, json; open(\'{marker}\', \'w\').write(json.dumps(sys.argv[1:]))" '
                       f'{{op}} {{file}} {{line}} {{line_end}} {{before_file}}',
                "match": "*",
                "hooks_into": ["edit"],
            }
        }
    })
    # Fire with no line/line_end and no pre_content — three empties in the middle
    supertool._run_notifiers("edit", "x.php")

    deadline = time.time() + 2
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), "notifier did not fire"
    import json as _json
    args = _json.loads(marker.read_text())
    # Expected 5 positional args: edit, x.php, "", "", ""
    assert args == ["edit", "x.php", "", "", ""], \
        f"positional argv corrupted: {args!r}"
