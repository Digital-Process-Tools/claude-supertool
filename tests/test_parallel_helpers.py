"""Direct tests for parallel-branch code paths in validator/formatter batches."""
from __future__ import annotations

from pathlib import Path

import supertool


def test_validators_run_batch_parallel_branch(tmp_path: Path, monkeypatch) -> None:
    """Force workers>=2 and >1 applicable validators to hit the parallel branch."""
    f = tmp_path / "x.py"
    f.write_text("print('hi')\n")
    # Two trivially-successful stubs
    spec1 = {"cmd": "true", "exts": [".py"]}
    spec2 = {"cmd": "true", "exts": [".py"]}
    applicable = {"v1": spec1, "v2": spec2}

    monkeypatch.setattr(supertool, "_parallel_workers", lambda: 2)
    monkeypatch.setattr(
        supertool, "_validator_run_one",
        lambda name, spec, path: {"name": name, "tool": name, "ok": True, "count": 0},
    )
    out = supertool._validators_run_batch(applicable, str(f))
    assert "v1" in out and "v2" in out


def test_formatters_run_batch_parallel_branch(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "x.py"
    f.write_text("print('hi')\n")
    applicable = {
        "f1": {"cmd": "true"},
        "f2": {"cmd": "true"},
    }

    monkeypatch.setattr(supertool, "_parallel_workers", lambda: 2)
    monkeypatch.setattr(
        supertool, "_formatter_run_one",
        lambda name, spec, path: {"name": name, "ok": True},
    )
    out = supertool._formatters_run_batch(applicable, str(f))
    assert isinstance(out, list)
    assert len(out) == 2


def test_validators_run_batch_sequential_single_validator(tmp_path: Path, monkeypatch) -> None:
    """Single validator → sequential branch even with workers>=2."""
    f = tmp_path / "x.py"
    f.write_text("x = 1\n")
    monkeypatch.setattr(supertool, "_parallel_workers", lambda: 4)
    monkeypatch.setattr(
        supertool, "_validator_run_one",
        lambda name, spec, path: {"name": name, "tool": name, "ok": True, "count": 0},
    )
    out = supertool._validators_run_batch({"only": {"cmd": "true"}}, str(f))
    assert "only" in out


def test_formatters_run_batch_sequential_single(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "x.py"
    f.write_text("x = 1\n")
    monkeypatch.setattr(supertool, "_parallel_workers", lambda: 1)
    monkeypatch.setattr(
        supertool, "_formatter_run_one",
        lambda name, spec, path: {"name": name, "ok": True},
    )
    out = supertool._formatters_run_batch({"only": {"cmd": "true"}}, str(f))
    assert isinstance(out, list) and len(out) == 1
