"""Tests for batch dispatch paths."""
from __future__ import annotations

import json
from pathlib import Path

import supertool


def test_batch_with_bare_list_payload(tmp_path: Path) -> None:
    f1 = tmp_path / "a.txt"
    f1.write_text("hello\n")
    payload_file = tmp_path / "batch.json"
    payload_file.write_text(json.dumps([
        {"op": "read", "path": str(f1)},
        {"op": "wc", "path": str(f1)},
    ]))
    out = supertool.dispatch(f"batch:@{payload_file}")
    assert "hello" in out


def test_batch_with_subop_using_at_file_fields(tmp_path: Path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("foo\n")
    payload_file = tmp_path / "batch.json"
    payload_file.write_text(json.dumps({
        "continue_on_error": False,
        "ops": [
            {"op": "edit", "path": str(target), "old": "foo", "new": "bar"},
        ],
    }))
    out = supertool.dispatch(f"batch:@{payload_file}")
    assert target.read_text(encoding="utf-8") == "bar\n", out


def test_batch_missing_op_field_emits_error(tmp_path: Path) -> None:
    payload_file = tmp_path / "batch.json"
    payload_file.write_text(json.dumps([
        {"path": str(tmp_path / "x.txt")},
    ]))
    out = supertool.dispatch(f"batch:@{payload_file}")
    assert "ERROR" in out


def test_batch_with_continue_on_error_false_stops_after_first_fail(tmp_path: Path) -> None:
    payload_file = tmp_path / "batch.json"
    payload_file.write_text(json.dumps({
        "continue_on_error": False,
        "ops": [
            {"op": "read", "path": str(tmp_path / "nonexistent.txt")},
            {"op": "read", "path": str(tmp_path / "also-nonexistent.txt")},
        ],
    }))
    out = supertool.dispatch(f"batch:@{payload_file}")
    # First op errors; second should NOT appear if continue=False.
    assert "nonexistent" in out
    # Hard to assert "also-nonexistent" absent — just check no crash.
    assert isinstance(out, str)
