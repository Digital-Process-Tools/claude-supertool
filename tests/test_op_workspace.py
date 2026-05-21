"""Tests for op_workspace — one-shot IDE-style file view."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import supertool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_php(tmp_path: Path, name: str = "Foo.class.php") -> Path:
    f = tmp_path / name
    f.write_text(
        "<?php\nclass Foo {\n    public function bar(): void {}\n}\n"
    )
    return f


def _make_py(tmp_path: Path, name: str = "mymodule.py") -> Path:
    f = tmp_path / name
    f.write_text(
        "class MyModule:\n    def run(self) -> None:\n        pass\n"
    )
    return f


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------

def test_workspace_missing_file() -> None:
    out = supertool.op_workspace("/nonexistent/path/to/NoSuchFile.php")
    assert "not found" in out


def test_workspace_dispatch_missing_file() -> None:
    out = supertool.dispatch("workspace:/nonexistent/file.py")
    assert "not found" in out


# ---------------------------------------------------------------------------
# Basic invocation — sections present and in correct order
# ---------------------------------------------------------------------------

def test_workspace_sections_order(tmp_path: Path) -> None:
    f = _make_php(tmp_path, "Widget.class.php")
    out = supertool.op_workspace(str(f))

    # All expected section headers present
    assert "## File:" in out
    assert "## Symbols" in out
    assert "## References" in out

    # Order: File → Symbols → References
    file_pos = out.index("## File:")
    sym_pos = out.index("## Symbols")
    ref_pos = out.index("## References")
    assert file_pos < sym_pos < ref_pos


def test_workspace_file_section_contains_content(tmp_path: Path) -> None:
    f = _make_php(tmp_path, "Widget.class.php")
    out = supertool.op_workspace(str(f))
    assert "class Foo" in out
    assert "public function bar" in out


def test_workspace_symbols_section_present(tmp_path: Path) -> None:
    f = _make_py(tmp_path)
    out = supertool.op_workspace(str(f))
    assert "## Symbols" in out
    # regex extraction should find the class
    assert "MyModule" in out


# ---------------------------------------------------------------------------
# Validators section — present only when validators configured
# ---------------------------------------------------------------------------

def test_workspace_no_validators_section_when_none_configured(tmp_path: Path, monkeypatch) -> None:
    """When no validators in config, ## Validators section is omitted."""
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG", {})
    f = _make_py(tmp_path)
    out = supertool.op_workspace(str(f))
    assert "## Validators" not in out


def test_workspace_validators_section_when_configured(tmp_path: Path, monkeypatch) -> None:
    """When validators are configured, ## Validators section appears."""
    f = tmp_path / "sample.json"
    f.write_text('{"key": "value"}\n')
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG", {
        "validators": {
            "jsonlint": {
                "cmd": "python3 -m json.tool {file}",
                "match": "*.json",
                "hooks_into": [],
                "timeout": 10,
            }
        }
    })
    out = supertool.op_workspace(str(f))
    assert "## Validators" in out


# ---------------------------------------------------------------------------
# Siblings section — present only when dirname != cwd
# ---------------------------------------------------------------------------

def test_workspace_siblings_present_when_in_subdir(tmp_path: Path, monkeypatch) -> None:
    subdir = tmp_path / "src"
    subdir.mkdir()
    f = subdir / "Thing.py"
    f.write_text("x = 1\n")
    (subdir / "Other.py").write_text("y = 2\n")

    monkeypatch.chdir(tmp_path)
    out = supertool.op_workspace(str(f))
    assert "## Siblings" in out
    assert "Thing.py" in out or "Other.py" in out


def test_workspace_siblings_skipped_when_in_cwd(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "top.py"
    f.write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)
    out = supertool.op_workspace(str(f))
    assert "## Siblings" not in out


# ---------------------------------------------------------------------------
# Git section — omitted outside a git repo
# ---------------------------------------------------------------------------

def test_workspace_no_git_section_outside_repo(tmp_path: Path, monkeypatch) -> None:
    """Outside a git repo the Git section must be absent."""
    f = _make_py(tmp_path)
    monkeypatch.chdir(tmp_path)

    # Patch subprocess.run to simulate "not inside a git repo"
    real_run = subprocess.run

    def _fake_run(cmd, **kwargs):
        if cmd[0] == "git" and "rev-parse" in cmd:
            import subprocess as sp
            result = sp.CompletedProcess(cmd, returncode=128, stdout="", stderr="")
            return result
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    out = supertool.op_workspace(str(f))
    assert "## Git" not in out


# ---------------------------------------------------------------------------
# References section — exclude paths respected
# ---------------------------------------------------------------------------

def test_workspace_references_excludes_vendor(tmp_path: Path, monkeypatch) -> None:
    """References scan must not return hits from vendor/ directories."""
    # Create the target file
    src = tmp_path / "src"
    src.mkdir()
    target = src / "Processor.py"
    target.write_text("class Processor:\n    pass\n")

    # Create a vendor file that references the symbol
    vendor = tmp_path / "vendor" / "lib"
    vendor.mkdir(parents=True)
    (vendor / "something.py").write_text("from Processor import Processor\n")

    # Create a legitimate reference
    (src / "caller.py").write_text("from Processor import Processor\n")

    monkeypatch.chdir(tmp_path)
    # Inject vendor/ into exclude paths
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG", {
        "ops": {
            "grep": {"exclude-paths": ["vendor/"]}
        }
    })

    out = supertool.op_workspace(str(target))
    ref_section_start = out.find("## References")
    assert ref_section_start != -1
    ref_section = out[ref_section_start:]

    assert "vendor" not in ref_section
    assert "caller.py" in ref_section


# ---------------------------------------------------------------------------
# Tests section — PHP test file detection
# ---------------------------------------------------------------------------

def test_workspace_tests_section_php(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "Widget.class.php"
    f.write_text("<?php\nclass Widget {}\n")
    test_f = tmp_path / "WidgetTest.php"
    test_f.write_text("<?php\nclass WidgetTest extends TestCase {}\n")

    out = supertool.op_workspace(str(f))
    assert "## Tests" in out
    assert "WidgetTest.php" in out


def test_workspace_tests_section_python(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "processor.py"
    f.write_text("class Processor:\n    pass\n")
    test_f = tmp_path / "test_processor.py"
    test_f.write_text("def test_run(): pass\n")

    out = supertool.op_workspace(str(f))
    assert "## Tests" in out
    assert "test_processor.py" in out


def test_workspace_no_tests_section_when_no_match(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "obscure_thing_xyz.py"
    f.write_text("x = 1\n")
    out = supertool.op_workspace(str(f))
    assert "## Tests" not in out


# ---------------------------------------------------------------------------
# Common symbol — noisy note injected, limit tightened
# ---------------------------------------------------------------------------

def test_workspace_common_symbol_note(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "index.py"
    f.write_text("x = 1\n")
    out = supertool.op_workspace(str(f))
    assert "common symbol" in out


# ---------------------------------------------------------------------------
# Dispatch round-trip
# ---------------------------------------------------------------------------

def test_workspace_dispatch_returns_header(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "sample.py"
    f.write_text("def hello(): pass\n")
    out = supertool.dispatch(f"workspace:{f}")
    assert f"--- workspace:{f} ---" in out
    assert "## File:" in out
