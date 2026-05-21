"""Tests for op_validate_staged and op_format_staged."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import supertool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_formatters(fmt: dict) -> None:
    supertool._CONFIG = {"formatters": fmt}
    supertool._CONFIG_CHECKED = True


def _set_validators(cfg: dict) -> None:
    supertool._CONFIG = {"validators": cfg}
    supertool._CONFIG_CHECKED = True


def _git(args: list, cwd: Path) -> None:
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    """Init a minimal git repo with a configured user identity."""
    _git(["init"], tmp_path)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# op_validate_staged
# ---------------------------------------------------------------------------

def test_validate_staged_empty_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    _set_validators({})
    result = supertool.op_validate_staged()
    assert result == "no staged files\n"


def test_validate_staged_single_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)

    f = repo / "hello.json"
    f.write_text("{}\n")
    _git(["add", "hello.json"], repo)

    _set_validators({
        "jsoncheck": {
            "cmd": "printf '%s' '{\"tool\":\"jsoncheck\",\"ok\":true,\"count\":0,\"errors\":[],\"duration_ms\":1}'",
            "match": "*.json",
            "hooks_into": ["edit"],
        }
    })
    result = supertool.op_validate_staged()
    assert "hello.json" in result
    assert "jsoncheck" in result


def test_validate_staged_not_a_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Plain directory — no .git — git diff --cached will fail
    monkeypatch.chdir(tmp_path)
    _set_validators({})
    result = supertool.op_validate_staged()
    assert result.startswith("ERROR")


# ---------------------------------------------------------------------------
# op_format_staged
# ---------------------------------------------------------------------------

def test_format_staged_empty_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    _set_formatters({})
    result = supertool.op_format_staged()
    assert result == "no staged files\n"


def test_format_staged_single_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)

    f = repo / "style.json"
    f.write_text("{}\n")
    _git(["add", "style.json"], repo)

    sentinel = tmp_path / "fmt_ran"
    _set_formatters({
        "prettier": {
            "cmd": f"touch {sentinel}",
            "match": "*.json",
        }
    })
    result = supertool.op_format_staged()
    assert sentinel.exists(), "formatter did not run on staged file"
    assert "style.json" in result
    assert "prettier" in result


def test_format_staged_not_a_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _set_formatters({})
    result = supertool.op_format_staged()
    assert result.startswith("ERROR")


# ---------------------------------------------------------------------------
# verbose mode — staged ops
# ---------------------------------------------------------------------------

def test_validate_staged_verbose_passes_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)

    f = repo / "hello.php"
    f.write_text("<?php\n")
    _git(["add", "hello.php"], repo)

    errors = [{"line": i, "col": None, "severity": "error", "code": "x", "msg": f"e{i}"}
              for i in range(1, 8)]
    payload = {"tool": "fake", "file": "hello.php", "ok": False, "count": 7,
               "errors": errors, "duration_ms": 1}
    js = json.dumps(payload).replace("'", "'\\''")
    _set_validators({
        "fake": {
            "cmd": f"printf '%s' '{js}'",
            "match": "*.php",
            "hooks_into": ["edit"],
        }
    })
    result = supertool.op_validate_staged(verbose=True)
    assert "+2 more" not in result
    for i in range(1, 8):
        assert f"e{i}" in result


def test_format_staged_verbose_passes_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)

    f = repo / "style.json"
    f.write_text("{}\n")
    _git(["add", "style.json"], repo)

    _set_formatters({
        "prettier": {"cmd": "true", "match": "*.json"},
    })
    result = supertool.op_format_staged(verbose=True)
    assert "[verbose]" in result


# ---------------------------------------------------------------------------
# dispatch: staged verbose parsing
# ---------------------------------------------------------------------------

def test_dispatch_validate_staged_verbose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)

    f = repo / "hello.php"
    f.write_text("<?php\n")
    _git(["add", "hello.php"], repo)

    errors = [{"line": i, "col": None, "severity": "error", "code": "x", "msg": f"e{i}"}
              for i in range(1, 8)]
    payload = {"tool": "fake", "file": "hello.php", "ok": False, "count": 7,
               "errors": errors, "duration_ms": 1}
    js = json.dumps(payload).replace("'", "'\\''")
    _set_validators({
        "fake": {
            "cmd": f"printf '%s' '{js}'",
            "match": "*.php",
            "hooks_into": ["edit"],
        }
    })
    result = supertool.dispatch("validate_staged:verbose")
    assert "+2 more" not in result


def test_dispatch_format_staged_verbose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)

    f = repo / "style.json"
    f.write_text("{}\n")
    _git(["add", "style.json"], repo)

    _set_formatters({"prettier": {"cmd": "true", "match": "*.json"}})
    result = supertool.dispatch("format_staged:verbose")
    assert "[verbose]" in result
