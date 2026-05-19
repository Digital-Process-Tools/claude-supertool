"""Tests for the batch op — runs multiple ops from a JSON file."""
from __future__ import annotations

import json
import io
from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(tmp_path: Path, name: str, payload) -> Path:
    f = tmp_path / name
    f.write_text(json.dumps(payload))
    return f


# ---------------------------------------------------------------------------
# Basic dispatch
# ---------------------------------------------------------------------------

class TestBatchBasic:
    def test_bare_array_runs_all_ops(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.py"
        f1.write_text("hello\n")
        f2 = tmp_path / "b.py"
        f2.write_text("world\n")
        ops = [
            {"op": "read", "path": str(f1)},
            {"op": "read", "path": str(f2)},
        ]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "hello" in out
        assert "world" in out

    def test_wrapper_object_runs_ops(self, tmp_path: Path) -> None:
        f = tmp_path / "x.py"
        f.write_text("content\n")
        payload = {
            "continue_on_error": True,
            "ops": [{"op": "read", "path": str(f)}],
        }
        spec = _write_json(tmp_path, "ops.json", payload)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "content" in out

    def test_missing_at_prefix_returns_error(self, tmp_path: Path) -> None:
        out = supertool.dispatch("batch:ops.json")
        assert "ERROR" in out
        assert "@file" in out

    def test_nonexistent_file_returns_error(self, tmp_path: Path) -> None:
        out = supertool.dispatch(f"batch:@{tmp_path}/nope.json")
        assert "ERROR" in out

    def test_invalid_json_returns_error(self, tmp_path: Path) -> None:
        spec = tmp_path / "bad.json"
        spec.write_text("{{{not json")
        out = supertool.dispatch(f"batch:@{spec}")
        assert "ERROR" in out

    def test_payload_not_array_or_object_returns_error(self, tmp_path: Path) -> None:
        spec = _write_json(tmp_path, "ops.json", "just a string")
        out = supertool.dispatch(f"batch:@{spec}")
        assert "ERROR" in out

    def test_op_missing_op_field_returns_error(self, tmp_path: Path) -> None:
        spec = _write_json(tmp_path, "ops.json", [{"path": "x.py"}])
        out = supertool.dispatch(f"batch:@{spec}")
        assert "ERROR" in out
        assert "op" in out

    def test_non_object_item_returns_error(self, tmp_path: Path) -> None:
        spec = _write_json(tmp_path, "ops.json", ["not_an_object"])
        out = supertool.dispatch(f"batch:@{spec}")
        assert "ERROR" in out


# ---------------------------------------------------------------------------
# Mutating ops in batch — validators must fire
# ---------------------------------------------------------------------------

class TestBatchMutatingOps:
    def test_edit_in_batch(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("foo\nbar\n")
        ops = [{"op": "edit", "old": "foo", "new": "FOO", "path": str(target)}]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "edited" in out
        assert "FOO" in target.read_text()

    def test_replace_lines_in_batch(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("line1\nline2\nline3\n")
        ops = [{"op": "replace_lines", "path": str(target), "start": 2, "end": 2, "content": "LINE2"}]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "replaced" in out
        assert target.read_text() == "line1\nLINE2\nline3\n"

    def test_paste_in_batch(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("old\n")
        ops = [{"op": "paste", "path": str(target), "content": "new"}]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "rewrote" in out
        assert "new" in target.read_text()

    def test_validators_called_for_mutating_op_in_batch(self, tmp_path: Path, monkeypatch) -> None:
        called = []
        original = supertool._run_with_validators

        def spy(op, parts, do_op):
            called.append(op)
            return original(op, parts, do_op)

        monkeypatch.setattr(supertool, "_run_with_validators", spy)
        target = tmp_path / "x.py"
        target.write_text("alpha\n")
        ops = [{"op": "edit", "old": "alpha", "new": "beta", "path": str(target)}]
        spec = _write_json(tmp_path, "ops.json", ops)
        supertool.dispatch(f"batch:@{spec}")
        assert "edit" in called


# ---------------------------------------------------------------------------
# Mixed batch (read + mutating)
# ---------------------------------------------------------------------------

class TestBatchMixed:
    def test_read_and_edit_in_same_batch(self, tmp_path: Path) -> None:
        f = tmp_path / "x.py"
        f.write_text("before\n")
        ops = [
            {"op": "read", "path": str(f)},
            {"op": "edit", "old": "before", "new": "after", "path": str(f)},
            {"op": "read", "path": str(f)},
        ]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "before" in out   # first read
        assert "after" in out    # edit receipt or second read
        assert f.read_text() == "after\n"


# ---------------------------------------------------------------------------
# continue_on_error behaviour
# ---------------------------------------------------------------------------

class TestBatchContinueOnError:
    def test_continue_on_error_true_runs_all(self, tmp_path: Path) -> None:
        f = tmp_path / "x.py"
        f.write_text("real\n")
        payload = {
            "continue_on_error": True,
            "ops": [
                {"op": "edit", "old": "NO_MATCH", "new": "x", "path": str(f)},
                {"op": "read", "path": str(f)},
            ],
        }
        spec = _write_json(tmp_path, "ops.json", payload)
        out = supertool.dispatch(f"batch:@{spec}")
        # Error from first op present
        assert "ERROR" in out or "not found" in out.lower()
        # Second op (read) also ran
        assert "real" in out

    def test_continue_on_error_false_stops_after_first_error(self, tmp_path: Path) -> None:
        f1 = tmp_path / "x.py"
        f1.write_text("real\n")
        f2 = tmp_path / "y.py"
        f2.write_text("should_not_appear\n")
        payload = {
            "continue_on_error": False,
            "ops": [
                {"op": "edit", "old": "NO_MATCH", "new": "x", "path": str(f1)},
                {"op": "read", "path": str(f2)},
            ],
        }
        spec = _write_json(tmp_path, "ops.json", payload)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "should_not_appear" not in out


# ---------------------------------------------------------------------------
# stdin route (@-)
# ---------------------------------------------------------------------------

class TestBatchStdin:
    def test_batch_from_stdin(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "x.py"
        f.write_text("stdin_test\n")
        ops = [{"op": "read", "path": str(f)}]
        monkeypatch.setattr(supertool.sys, "stdin", io.StringIO(json.dumps(ops)))
        out = supertool.dispatch("batch:@-")
        assert "stdin_test" in out


# ---------------------------------------------------------------------------
# Output format — per-op headers still present
# ---------------------------------------------------------------------------

class TestBatchOutputFormat:
    def test_each_op_has_header(self, tmp_path: Path) -> None:
        f = tmp_path / "x.py"
        f.write_text("hi\n")
        ops = [{"op": "read", "path": str(f)}, {"op": "wc", "path": str(f)}]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "--- read:" in out
        assert "--- wc:" in out
