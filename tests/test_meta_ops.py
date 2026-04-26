from __future__ import annotations

import json
from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# op_ops — self-documenting ops reference from .supertool.json
# ---------------------------------------------------------------------------

def test_ops_no_config(tmp_path: Path, monkeypatch) -> None:
    """No .supertool.json → shows built-in op names only (no descriptions)."""
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_ops()
    # Should list built-in op names as fallback
    assert "read" in out
    assert "grep" in out
    assert "glob" in out
    assert "No descriptions configured" in out


def test_ops_builtin_ops_section(tmp_path: Path, monkeypatch) -> None:
    """builtin-ops section renders syntax, description, and example."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "builtin-ops": {
            "read": {
                "syntax": "read:PATH[:OFFSET:LIMIT]",
                "description": "Read file (300 lines, 20KB cap)",
                "example": "read:src/foo.py:10:50"
            },
            "grep": {
                "syntax": "grep:PATTERN:PATH[:LIMIT]",
                "description": "Search files",
                "example": "grep:extends:src/:20"
            }
        }
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_ops()
    assert "read:PATH[:OFFSET:LIMIT]" in out
    assert "Read file (300 lines, 20KB cap)" in out
    assert "read:src/foo.py:10:50" in out
    assert "grep:PATTERN:PATH[:LIMIT]" in out
    assert "Search files" in out


def test_ops_custom_ops_with_description(tmp_path: Path, monkeypatch) -> None:
    """Custom ops from ops section show description and example."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "ops": {
            "phpstan": {
                "cmd": "php -l {file}",
                "timeout": 30,
                "description": "PHPStan level 9. Use after every Edit.",
                "example": "phpstan:src/Foo.php"
            }
        }
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_ops()
    assert "phpstan" in out
    assert "PHPStan level 9. Use after every Edit." in out
    assert "phpstan:src/Foo.php" in out


def test_ops_custom_ops_without_description(tmp_path: Path, monkeypatch) -> None:
    """Custom ops without description still appear with just the name."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "ops": {
            "phpmd": {
                "cmd": "phpmd {file} text codesize",
                "timeout": 60
            }
        }
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_ops()
    assert "phpmd" in out


def test_ops_status_0_hides_builtin(tmp_path: Path, monkeypatch) -> None:
    """builtin-ops with status: 0 are hidden from output."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "builtin-ops": {
            "read": {
                "syntax": "read:PATH",
                "description": "Read file",
                "status": 1
            },
            "map": {
                "syntax": "map:PATH",
                "description": "Symbol tree",
                "status": 0
            }
        }
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_ops()
    assert "read:PATH" in out
    assert "map" not in out


def test_ops_status_0_hides_custom(tmp_path: Path, monkeypatch) -> None:
    """Custom ops with status: 0 are hidden from output."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "ops": {
            "phpstan": {
                "cmd": "php -l {file}",
                "description": "PHPStan",
                "status": 1
            },
            "psr": {
                "cmd": "phpcs {file}",
                "description": "PSR12 check",
                "status": 0
            }
        }
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_ops()
    assert "phpstan" in out
    assert "psr" not in out


def test_ops_both_sections_combined(tmp_path: Path, monkeypatch) -> None:
    """Both builtin-ops and custom ops appear in output."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "builtin-ops": {
            "read": {
                "syntax": "read:PATH",
                "description": "Read file"
            }
        },
        "ops": {
            "lint": {
                "cmd": "php -l {file}",
                "description": "PHP syntax check",
                "example": "lint:src/Foo.php"
            }
        }
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_ops()
    assert "## Operations" in out
    assert "read:PATH" in out
    assert "lint" in out
    assert "PHP syntax check" in out


def test_ops_dispatch_integration(tmp_path: Path, monkeypatch) -> None:
    """dispatch('ops') routes to op_ops."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "builtin-ops": {
            "read": {"syntax": "read:PATH", "description": "Read file"}
        }
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.dispatch("ops")
    assert "--- ops ---" not in out
    assert "read:PATH" in out


def test_ops_default_status_is_shown(tmp_path: Path, monkeypatch) -> None:
    """Missing status field defaults to shown (status: 1)."""
    config = tmp_path / ".supertool.json"
    config.write_text(json.dumps({
        "builtin-ops": {
            "read": {
                "syntax": "read:PATH",
                "description": "Read file"
            }
        }
    }))
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = None
    supertool._CONFIG_CHECKED = False
    out = supertool.op_ops()
    assert "read:PATH" in out


def test_ops_aliases_section_rendered() -> None:
    """Aliases appear in a separate section in op_ops output."""
    supertool._CONFIG = {
        "aliases": {
            "verify": {
                "ops": ["phpstan:{file}", "phpmd:{file}"],
                "description": "Run all quality checks",
                "example": "verify:src/Module.php"
            }
        }
    }
    supertool._CONFIG_CHECKED = True
    out = supertool.op_ops()
    assert "## Aliases" in out
    assert "verify" in out
    assert "Run all quality checks" in out
    assert "verify:src/Module.php" in out


def test_ops_aliases_status_0_hidden() -> None:
    """Aliases with status: 0 are hidden from output."""
    supertool._CONFIG = {
        "aliases": {
            "visible": {"ops": ["read:{file}"], "description": "Shown"},
            "hidden": {"ops": ["read:{file}"], "description": "Hidden", "status": 0}
        }
    }
    supertool._CONFIG_CHECKED = True
    out = supertool.op_ops()
    assert "visible" in out
    assert "hidden" not in out


def test_alias_ops_not_a_list() -> None:
    """Alias with ops as a string instead of list gives error."""
    supertool._CONFIG = {
        "aliases": {"bad": {"ops": "not-a-list"}}
    }
    out = supertool._resolve_alias("bad", ["bad", "file.php"])
    assert "ERROR" in out
    assert "must be a list" in out
