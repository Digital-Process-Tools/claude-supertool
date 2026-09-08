"""#1398 - the `git status` budget behind the `git?` token is now configurable.

`_path_meta_suffix` and `_path_meta_bulk_fill` used to hardcode `timeout=2` on
every `git status` spawn, with no config key. Parallel mode spawns several of
these against that same fixed budget, so raising worker count raised the rate
of honest `git?` declines (the token meaning "the working-tree lookup
declined, state unknown") as a side effect of a constant nobody could tune.

This file pins three things:
  - `builtin-ops.read.git_timeout_seconds` reaches both git spawn sites
    (the per-repo bulk `git status` and the per-path fallback `git status`).
  - An explicit `0` is refused loudly and the default is used instead,
    following #1332's lesson (a threshold's `0` must not be silently
    swallowed).
  - With no config key at all, both spawns still use the historical default
    of 2 seconds, so an unconfigured install behaves exactly as before.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List

import pytest
import supertool


class _TimeoutCapture:
    """Records every `timeout=` kwarg passed to a `git status` spawn."""

    def __init__(self) -> None:
        self.timeouts: List[Any] = []

    def __call__(self, cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git" and "status" in cmd:
            self.timeouts.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> _TimeoutCapture:
    c = _TimeoutCapture()
    monkeypatch.setattr(supertool.subprocess, "run", c)
    return c


def test_default_timeout_is_two_seconds_with_no_config(
    tmp_path: Path, capture: _TimeoutCapture
) -> None:
    """Unconfigured install: both call sites use the historical 2s default."""
    assert supertool._path_meta_bulk_fill(str(tmp_path)) is not None
    assert capture.timeouts == [2]


def test_git_timeout_seconds_config_key_reaches_bulk_fill(
    tmp_path: Path, capture: _TimeoutCapture
) -> None:
    supertool._CONFIG = {"builtin-ops": {"read": {"git_timeout_seconds": 7}}}
    assert supertool._path_meta_bulk_fill(str(tmp_path)) is not None
    assert capture.timeouts == [7]


def test_git_timeout_seconds_config_key_reaches_per_path_fallback(
    tmp_path: Path, capture: _TimeoutCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-path `git status` fallback (used when the bulk snapshot cannot
    serve this path) reads the same config key."""
    supertool._CONFIG = {"builtin-ops": {"read": {"git_timeout_seconds": 9}}}
    f = tmp_path / "f.txt"
    f.write_text("x")
    # Force the per-path branch: give it a fake repo root and make the bulk
    # snapshot already "declined" so the per-path spawn is what runs.
    monkeypatch.setattr(supertool, "_path_meta_repo_root", lambda path: str(tmp_path))
    monkeypatch.setattr(supertool, "_PATH_META_BULK", {str(tmp_path): "declined"})
    supertool._path_meta_suffix(str(f), b"x")
    assert capture.timeouts == [9]


def test_git_timeout_seconds_explicit_zero_is_refused_loudly(
    tmp_path: Path, capture: _TimeoutCapture, capsys: pytest.CaptureFixture
) -> None:
    """#1332's lesson: a configured `0` must not be silently swallowed into
    the default with no trace -- it is refused, announced, and the default
    wins."""
    supertool._CONFIG = {"builtin-ops": {"read": {"git_timeout_seconds": 0}}}
    assert supertool._path_meta_bulk_fill(str(tmp_path)) is not None
    assert capture.timeouts == [2]
    out = capsys.readouterr().out
    assert "git_timeout_seconds=0" in out
    assert "not a positive whole number" in out
