"""Tests for op_resolve and workspace ## Imports section."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import supertool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "resolve"


# ---------------------------------------------------------------------------
# op_resolve — PHP FQN
# ---------------------------------------------------------------------------

def test_resolve_php_fqn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Foo\\Bar resolves to the fixture .class.php file."""
    monkeypatch.chdir(FIXTURES)
    result = supertool.op_resolve("Foo\\Bar")
    assert "→" in result
    assert "not found" not in result
    assert "external" not in result
    assert "Bar.class.php" in result


# ---------------------------------------------------------------------------
# op_resolve — Python dotted
# ---------------------------------------------------------------------------

def test_resolve_python_dotted(monkeypatch: pytest.MonkeyPatch) -> None:
    """mypkg.utils resolves to the fixture .py file."""
    monkeypatch.chdir(FIXTURES)
    result = supertool.op_resolve("mypkg.utils")
    assert "→" in result
    assert "not found" not in result
    assert "external" not in result
    assert "utils.py" in result


# ---------------------------------------------------------------------------
# op_resolve — relative path
# ---------------------------------------------------------------------------

def test_resolve_relative_ts(monkeypatch: pytest.MonkeyPatch) -> None:
    """./util from the fixtures dir resolves to util.ts."""
    monkeypatch.chdir(FIXTURES)
    result = supertool.op_resolve("./util")
    assert "→" in result
    assert "not found" not in result
    assert "external" not in result
    assert "util.ts" in result


# ---------------------------------------------------------------------------
# op_resolve — external package
# ---------------------------------------------------------------------------

def test_resolve_external(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A package-spec with / that isn't relative (e.g. lodash/utils) returns external."""
    monkeypatch.chdir(tmp_path)
    result = supertool.op_resolve("lodash/utils")
    assert "external" in result


def test_resolve_bare_word_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare word with no matching file returns 'not found'."""
    monkeypatch.chdir(tmp_path)
    result = supertool.op_resolve("lodash")
    assert "not found" in result


# ---------------------------------------------------------------------------
# op_resolve — not found
# ---------------------------------------------------------------------------

def test_resolve_not_found_php(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PHP FQN that has no matching file returns 'not found'."""
    monkeypatch.chdir(FIXTURES)
    result = supertool.op_resolve("Does\\NotExist")
    assert "not found" in result


def test_resolve_not_found_python(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Python dotted import that has no matching file returns 'not found'."""
    monkeypatch.chdir(FIXTURES)
    result = supertool.op_resolve("does.not.exist.anywhere")
    assert "not found" in result


# ---------------------------------------------------------------------------
# op_resolve — dispatch round-trip
# ---------------------------------------------------------------------------

def test_resolve_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """dispatch('resolve:Foo\\\\Bar') returns the expected header + result."""
    monkeypatch.chdir(FIXTURES)
    out = supertool.dispatch("resolve:Foo\\Bar")
    assert "--- resolve:" in out
    assert "Bar.class.php" in out


# ---------------------------------------------------------------------------
# workspace — Imports section present for PHP with use statements
# ---------------------------------------------------------------------------

def test_workspace_imports_php(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """workspace on a PHP file with use statements emits an ## Imports section."""
    # Create a target PHP file with use statements
    php_file = tmp_path / "MyCommand.class.php"
    php_file.write_text(
        "<?php\n"
        "use Foo\\Bar;\n"
        "use Baz\\Qux as Q;\n"
        "class MyCommand {}\n"
    )
    # Create a matching file for Foo\Bar so resolve finds it
    foo_dir = tmp_path / "Foo"
    foo_dir.mkdir()
    (foo_dir / "Bar.class.php").write_text("<?php\nclass Bar {}\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG", {})

    out = supertool.op_workspace(str(php_file))

    assert "## Imports" in out
    # Both use statements should appear
    assert "Foo\\Bar" in out or "Foo" in out
    assert "Baz\\Qux" in out or "Qux" in out


# ---------------------------------------------------------------------------
# workspace — Imports section absent when no imports
# ---------------------------------------------------------------------------

def test_workspace_no_imports_section_when_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """workspace on a file with no import statements omits ## Imports."""
    f = tmp_path / "plain.py"
    f.write_text("x = 1\ny = 2\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG", {})

    out = supertool.op_workspace(str(f))
    assert "## Imports" not in out


# ---------------------------------------------------------------------------
# workspace — Imports section present for Python with import statements
# ---------------------------------------------------------------------------

def test_workspace_imports_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """workspace on a Python file with imports emits ## Imports."""
    f = tmp_path / "mymodule.py"
    f.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "x = 1\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG", {})

    out = supertool.op_workspace(str(f))
    assert "## Imports" in out
    assert "os" in out
    assert "pathlib" in out


# ---------------------------------------------------------------------------
# Imports section ordering: after Symbols, before Validators
# ---------------------------------------------------------------------------

def test_workspace_imports_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """## Imports appears after ## Symbols and before ## Validators."""
    f = tmp_path / "sample.php"
    f.write_text("<?php\nuse Foo\\Bar;\nclass Sample {}\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG", {
        "validators": {
            "phplint": {
                "cmd": "true {file}",
                "match": "*.php",
                "hooks_into": [],
                "timeout": 5,
            }
        }
    })

    out = supertool.op_workspace(str(f))
    sym_pos = out.find("## Symbols")
    imp_pos = out.find("## Imports")
    val_pos = out.find("## Validators")

    assert sym_pos != -1
    assert imp_pos != -1
    assert sym_pos < imp_pos
    if val_pos != -1:
        assert imp_pos < val_pos
