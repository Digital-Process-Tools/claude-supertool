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


def test_batch_at_file_with_flat_single_op_document_names_single_op_route(tmp_path: Path) -> None:
    """469's mirror case: a flat single-op document (old/new/path, no 'ops'
    wrapper) handed to batch:@file must say so, not silently no-op."""
    f = _write(tmp_path, {"old": "a", "new": "b", "path": str(tmp_path / "x.py")})
    out = supertool.dispatch(f"batch:@{f}")
    assert "ERROR" in out
    assert "ops" in out.lower()


def test_batch_at_file_with_explicit_continue_false(tmp_path: Path) -> None:
    payload = {
        "continue_on_error": False,
        "ops": [{"op": "read", "path": str(tmp_path / "nonexistent.txt")}],
    }
    f = _write(tmp_path, payload)
    out = supertool.dispatch(f"batch:@{f}")
    assert "nonexistent" in out or "ERROR" in out or "not found" in out
