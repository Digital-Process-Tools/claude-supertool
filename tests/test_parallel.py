"""Tests for parallel execution mode (SUPERTOOL_PARALLEL=1)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import supertool


def _subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Subprocess env that works on POSIX + Windows.

    Inherit the parent env (Windows Python needs SYSTEMROOT, APPDATA, etc.
    to start at all; pinning a minimal POSIX-only env breaks the runner).
    Force PYTHONIOENCODING=utf-8 so supertool's `→` arrow doesn't crash
    the default cp1252 codec on Windows. Strip SUPERTOOL_PARALLEL so
    callers control it explicitly via `extra`.
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("SUPERTOOL_PARALLEL", None)
    # SUPERTOOL_NO_RTK=1 is set in conftest.pytest_configure (covers all
    # subprocess-spawning tests) — env.copy() picks it up here. Without it,
    # supertool delegates `read` to rtk and rtk's output format (`1 │ hi`)
    # breaks the byte-identical assertions below.
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def test_parallel_safe_includes_read_only_ops() -> None:
    for op in ("read", "grep", "glob", "ls", "head", "tail", "wc", "stat",
               "map", "tree", "around", "around_line", "between", "diff",
               "blame", "version"):
        assert supertool._is_parallel_safe(f"{op}:foo")


def test_parallel_safe_excludes_mutating_ops() -> None:
    for op in ("edit", "replace", "replace_dry", "replace_lines", "paste", "append", "vim"):
        assert not supertool._is_parallel_safe(f"{op}:a:b:c")
        assert not supertool._is_parallel_safe(f"{op}:::a:::b:::c")


def test_parallel_safe_excludes_unknown_ops() -> None:
    """Custom ops (mysql_write, mr, phpstan, etc.) — unknown to safety set."""
    assert not supertool._is_parallel_safe("mysql_write:UPDATE x SET y=1")
    assert not supertool._is_parallel_safe("mr:.max/mr.md|1h|labels")
    assert not supertool._is_parallel_safe("phpstan:src/")


def test_parallel_safe_handles_triple_colon() -> None:
    assert supertool._is_parallel_safe("read:::path")
    assert not supertool._is_parallel_safe("edit:::a:::b:::c")


def test_parallel_safe_handles_malformed() -> None:
    assert not supertool._is_parallel_safe("")
    assert not supertool._is_parallel_safe("::just-colons")


# ---------------------------------------------------------------------------
# End-to-end via subprocess — verify ordering and correctness
# ---------------------------------------------------------------------------

def _supertool_path() -> Path:
    return Path(__file__).parent.parent / "supertool.py"


def _run(argv: list[str], parallel: bool, tmp_path: Path) -> str:
    extra = {"SUPERTOOL_PARALLEL": "4"} if parallel else None
    result = subprocess.run(
        [sys.executable, str(_supertool_path()), *argv],
        capture_output=True, text=True, encoding="utf-8",
        env=_subprocess_env(extra), cwd=str(tmp_path), errors="replace",
    )
    return result.stdout


def test_parallel_preserves_input_order(tmp_path: Path) -> None:
    """Output must match input order, not completion order."""
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text(f"content{i}\n")
    argv = [f"read:f{i}.txt" for i in range(5)]
    seq = _run(argv, parallel=False, tmp_path=tmp_path)
    par = _run(argv, parallel=True, tmp_path=tmp_path)
    assert seq == par


def test_parallel_falls_back_to_sequential_for_mixed_batch(
    tmp_path: Path,
) -> None:
    """Any non-safe op present → whole batch runs sequentially.

    We can't observe sequential vs parallel directly, but the output should
    still be byte-identical between modes when correct.
    """
    f = tmp_path / "x.txt"
    f.write_text("foo\nbar\n")
    # `replace_dry` is not in the safe set
    argv = ["read:x.txt", "replace_dry:::foo:::FOO:::."]
    seq = _run(argv, parallel=False, tmp_path=tmp_path)
    par = _run(argv, parallel=True, tmp_path=tmp_path)
    assert seq == par


def test_parallel_single_op_unchanged(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hi\n")
    seq = _run(["read:x.txt"], parallel=False, tmp_path=tmp_path)
    par = _run(["read:x.txt"], parallel=True, tmp_path=tmp_path)
    assert seq == par


def test_parallel_disabled_by_default(tmp_path: Path) -> None:
    """Without env var = sequential (no SUPERTOOL_PARALLEL set)."""
    f = tmp_path / "x.txt"
    f.write_text("hi\n")
    result = subprocess.run(
        [sys.executable, str(_supertool_path()), "read:x.txt"],
        capture_output=True, text=True, encoding="utf-8",
        env=_subprocess_env(),  # no SUPERTOOL_PARALLEL
        cwd=str(tmp_path), errors="replace",
    )
    assert "1→hi" in result.stdout


def test_parallel_workers_int_from_json(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", {"parallel": 4})
    monkeypatch.delenv("SUPERTOOL_PARALLEL", raising=False)
    assert supertool._parallel_workers() == 4


def test_parallel_workers_zero_disables(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", {"parallel": 0})
    monkeypatch.delenv("SUPERTOOL_PARALLEL", raising=False)
    assert supertool._parallel_workers() == 0


def test_parallel_workers_bool_compat(monkeypatch) -> None:
    """Back-compat: `true` → 4, `false` → 0."""
    monkeypatch.setattr(supertool, "_CONFIG", {"parallel": True})
    monkeypatch.delenv("SUPERTOOL_PARALLEL", raising=False)
    assert supertool._parallel_workers() == 4
    monkeypatch.setattr(supertool, "_CONFIG", {"parallel": False})
    assert supertool._parallel_workers() == 0


def test_parallel_workers_env_overrides_json(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", {"parallel": 8})
    monkeypatch.setenv("SUPERTOOL_PARALLEL", "0")
    assert supertool._parallel_workers() == 0
    monkeypatch.setenv("SUPERTOOL_PARALLEL", "3")
    assert supertool._parallel_workers() == 3


def test_parallel_workers_default_zero(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", {})
    monkeypatch.delenv("SUPERTOOL_PARALLEL", raising=False)
    assert supertool._parallel_workers() == 0


def test_parallel_workers_invalid_str_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", {})
    monkeypatch.setenv("SUPERTOOL_PARALLEL", "garbage")
    assert supertool._parallel_workers() == 0


def test_parallel_error_isolation(tmp_path: Path) -> None:
    """One failing op shouldn't corrupt other ops' output."""
    (tmp_path / "good.txt").write_text("ok\n")
    argv = ["read:good.txt", "read:nope.txt", "read:good.txt"]
    par = _run(argv, parallel=True, tmp_path=tmp_path)
    # All three headers present, in order
    headers = [line for line in par.splitlines() if line.startswith("--- ")]
    assert headers == [
        "--- read:good.txt ---",
        "--- read:nope.txt ---",
        "--- read:good.txt ---",
    ]
    # Middle one is the error
    assert "ERROR: file not found" in par
