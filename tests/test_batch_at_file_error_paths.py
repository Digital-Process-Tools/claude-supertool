"""Tests for the `batch:@file` route's error paths (8602-8615 region)."""
from __future__ import annotations

import json
from pathlib import Path

import supertool


def _write(tmp_path: Path, payload) -> Path:
    f = tmp_path / "p.json"
    f.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return f


def test_batch_at_file_with_string_payload_emits_error(tmp_path: Path) -> None:
    f = _write(tmp_path, json.dumps("not a list or dict"))
    out = supertool.dispatch(f"batch:@{f}")
    assert "ERROR" in out


def test_batch_at_file_with_number_payload_emits_error(tmp_path: Path) -> None:
    f = _write(tmp_path, "42")
    out = supertool.dispatch(f"batch:@{f}")
    assert "ERROR" in out


def test_batch_at_file_with_dict_missing_ops_key(tmp_path: Path) -> None:
    f = _write(tmp_path, {"continue_on_error": False})
    out = supertool.dispatch(f"batch:@{f}")
    # No ops → nothing to do; just confirm no crash.
    assert isinstance(out, str)


def test_batch_at_file_with_explicit_continue_false(tmp_path: Path) -> None:
    payload = {
        "continue_on_error": False,
        "ops": [{"op": "read", "path": str(tmp_path / "nonexistent.txt")}],
    }
    f = _write(tmp_path, payload)
    out = supertool.dispatch(f"batch:@{f}")
    assert "nonexistent" in out or "ERROR" in out or "not found" in out
