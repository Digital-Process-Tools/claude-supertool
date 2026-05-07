"""Unit tests for presets/git/diverge.py."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any


PRESET = Path(__file__).parent.parent / "presets" / "git" / "diverge.py"
_spec = importlib.util.spec_from_file_location("git_diverge", PRESET)
assert _spec is not None and _spec.loader is not None
diverge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(diverge)


def _fake_run(stdout: str = "", stderr: str = "", returncode: int = 0) -> Any:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_resolve_base_uses_explicit() -> None:
    assert diverge._resolve_base("v18.5.x") == "v18.5.x"


def test_resolve_base_falls_back_to_master(monkeypatch) -> None:
    monkeypatch.setattr(
        diverge, "_git",
        lambda args, timeout=10: _fake_run(returncode=0)
        if args == ["rev-parse", "--verify", "--quiet", "master"]
        else _fake_run(returncode=1),
    )
    assert diverge._resolve_base("") == "master"


def test_resolve_base_falls_back_to_main(monkeypatch) -> None:
    def fake(args, timeout=10):
        if args == ["rev-parse", "--verify", "--quiet", "main"]:
            return _fake_run(returncode=0)
        return _fake_run(returncode=1)
    monkeypatch.setattr(diverge, "_git", fake)
    assert diverge._resolve_base("") == "main"
