"""New-file auto-invalidation of warm MCP daemons (#239).

When a mutating op creates a brand-new file, warm LSP daemons that opt into
`stopOnNewFile` are SIGTERM'd before the post-op validator run, so the next
connect cold-starts a daemon that has indexed the new file.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import supertool


def _fake_cmd(payload: dict) -> str:
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    return (
        f'{{python}} -c "import sys, base64; '
        f"sys.stdout.write(base64.b64decode('{encoded}').decode())"
        f'"'
    )


@pytest.fixture
def record_stops(monkeypatch):
    """Capture _mcp_stop_server calls instead of spawning stop.py."""
    calls: list[str] = []
    monkeypatch.setattr(supertool, "_mcp_stop_server", lambda name: calls.append(name))
    return calls


def _set_ok_validator() -> None:
    payload_ok = {"tool": "fake", "ok": True, "count": 0, "errors": [], "duration_ms": 1}
    supertool._CONFIG = {
        "validators": {
            "fake": {"cmd": _fake_cmd(payload_ok), "hooks_into": ["edit"], "match": "*.php"},
        }
    }
    supertool._CONFIG_CHECKED = True


def test_stops_server_on_new_file(tmp_path: Path, record_stops, monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_mcp_specs",
                        {"php-lsp": {"match": "*.php", "stopOnNewFile": True}})
    _set_ok_validator()
    f = tmp_path / "New.class.php"  # does NOT exist pre-op

    def do_op() -> str:
        f.write_text("<?php\nclass New {}\n")
        return "created\n"

    out = supertool._run_with_validators("edit", ["edit", "", "", str(f)], do_op)
    assert "[validators]" in out
    assert record_stops == ["php-lsp"]


def test_stops_server_on_new_file_with_deferred_slow_validator(
        tmp_path: Path, record_stops, monkeypatch) -> None:
    """The warm phpstan/rector validators are tier=slow → deferred in batch mode,
    so `applicable` (inline) is empty. The stop must still fire, else the deferred
    validators run against a stale daemon (#239 in a multi-op call)."""
    monkeypatch.setattr(supertool, "_mcp_specs",
                        {"php-lsp": {"match": "*.php", "stopOnNewFile": True}})
    payload_ok = {"tool": "slow", "ok": True, "count": 0, "errors": [], "duration_ms": 1}
    supertool._CONFIG = {"validators": {
        "slow": {"cmd": _fake_cmd(payload_ok), "hooks_into": ["edit"],
                 "match": "*.php", "tier": "slow"},
    }}
    supertool._CONFIG_CHECKED = True
    monkeypatch.setattr(supertool, "_DEFER_FORMATTERS", True)
    monkeypatch.setattr(supertool, "_VALIDATOR_DEFER_QUEUE", [])
    monkeypatch.setattr(supertool, "_VALIDATOR_DEFER_SEEN", set())
    f = tmp_path / "New.class.php"

    def do_op() -> str:
        f.write_text("<?php\nclass NewThing {}\n")
        return "created\n"

    supertool._run_with_validators("edit", ["edit", "", "", str(f)], do_op)
    assert record_stops == ["php-lsp"]
    assert any(name == "slow" for name, _spec, _p in supertool._VALIDATOR_DEFER_QUEUE)


def test_no_stop_when_file_pre_existed(tmp_path: Path, record_stops, monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_mcp_specs",
                        {"php-lsp": {"match": "*.php", "stopOnNewFile": True}})
    _set_ok_validator()
    f = tmp_path / "Existing.class.php"
    f.write_text("<?php\n")  # exists before the op

    out = supertool._run_with_validators("edit", ["edit", "", "", str(f)],
                                         lambda: "edited\n")
    assert "[validators]" in out
    assert record_stops == []


def test_no_stop_when_flag_absent(tmp_path: Path, record_stops, monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_mcp_specs",
                        {"php-lsp": {"match": "*.php"}})  # no stopOnNewFile
    _set_ok_validator()
    f = tmp_path / "New.class.php"

    def do_op() -> str:
        f.write_text("<?php\n")
        return "created\n"

    supertool._run_with_validators("edit", ["edit", "", "", str(f)], do_op)
    assert record_stops == []


def test_no_stop_when_extension_does_not_match(tmp_path: Path, record_stops, monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_mcp_specs",
                        {"php-lsp": {"match": "*.php", "stopOnNewFile": True}})
    # validator matches *.py so the op still runs validators, but the MCP server
    # glob is *.php → no server should be stopped for a new .py file.
    payload_ok = {"tool": "fake", "ok": True, "count": 0, "errors": [], "duration_ms": 1}
    supertool._CONFIG = {
        "validators": {
            "fake": {"cmd": _fake_cmd(payload_ok), "hooks_into": ["edit"], "match": "*.py"},
        }
    }
    supertool._CONFIG_CHECKED = True
    f = tmp_path / "thing.py"

    def do_op() -> str:
        f.write_text("x = 1\n")
        return "created\n"

    supertool._run_with_validators("edit", ["edit", "", "", str(f)], do_op)
    assert record_stops == []


def test_servers_to_stop_lookup() -> None:
    specs = {
        "php-lsp": {"match": "*.php", "stopOnNewFile": True},
        "py-lsp": {"match": "*.py", "stopOnNewFile": True},
        "no-flag": {"match": "*.php"},
    }
    import unittest.mock as mock
    with mock.patch.object(supertool, "_mcp_specs", specs):
        assert supertool._mcp_servers_to_stop_on_new_file("a/b/Foo.php") == ["php-lsp"]
        assert supertool._mcp_servers_to_stop_on_new_file("x.py") == ["py-lsp"]
        assert supertool._mcp_servers_to_stop_on_new_file("x.txt") == []
        assert supertool._mcp_servers_to_stop_on_new_file("") == []
