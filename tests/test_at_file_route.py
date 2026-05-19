"""Tests for the @file input route on mutating ops (edit, replace, replace_lines, paste, vim)."""
from __future__ import annotations

import json
from pathlib import Path

import supertool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(tmp_path: Path, name: str, payload: dict) -> Path:
    f = tmp_path / name
    f.write_text(json.dumps(payload))
    return f


# ---------------------------------------------------------------------------
# edit:@file
# ---------------------------------------------------------------------------

class TestAtFileEdit:
    def test_basic_edit(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("a = 1\nb = 2\n")
        spec = _write_json(tmp_path, "e.json", {"old": "a = 1", "new": "a = 99", "path": str(target)})
        out = supertool.dispatch(f"edit:@{spec}")
        assert "edited" in out
        assert "a = 99" in target.read_text()

    def test_missing_field_returns_error(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("a = 1\n")
        spec = _write_json(tmp_path, "e.json", {"old": "a = 1", "path": str(target)})  # missing 'new'
        out = supertool.dispatch(f"edit:@{spec}")
        assert "ERROR" in out
        assert "new" in out

    def test_nonexistent_at_file_returns_error(self, tmp_path: Path) -> None:
        out = supertool.dispatch(f"edit:@{tmp_path}/does_not_exist.json")
        assert "ERROR" in out
        assert "not found" in out

    def test_invalid_json_returns_error(self, tmp_path: Path) -> None:
        spec = tmp_path / "bad.json"
        spec.write_text("not valid json {{{")
        out = supertool.dispatch(f"edit:@{spec}")
        assert "ERROR" in out
        assert "JSON parse error" in out

    def test_payload_not_object_returns_error(self, tmp_path: Path) -> None:
        spec = tmp_path / "arr.json"
        spec.write_text('["a", "b"]')
        out = supertool.dispatch(f"edit:@{spec}")
        assert "ERROR" in out

    def test_validators_still_run(self, tmp_path: Path, monkeypatch) -> None:
        """_run_with_validators must be called even via @file route."""
        called = []

        original = supertool._run_with_validators

        def spy(op, parts, do_op):
            called.append(op)
            return original(op, parts, do_op)

        monkeypatch.setattr(supertool, "_run_with_validators", spy)
        target = tmp_path / "x.py"
        target.write_text("foo\n")
        spec = _write_json(tmp_path, "e.json", {"old": "foo", "new": "bar", "path": str(target)})
        supertool.dispatch(f"edit:@{spec}")
        assert "edit" in called

    def test_header_contains_at_ref(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("foo\n")
        spec = _write_json(tmp_path, "e.json", {"old": "foo", "new": "bar", "path": str(target)})
        out = supertool.dispatch(f"edit:@{spec}")
        assert f"edit:@{spec}" in out


# ---------------------------------------------------------------------------
# replace:@file
# ---------------------------------------------------------------------------

class TestAtFileReplace:
    def test_basic_replace(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("foo bar foo\n")
        spec = _write_json(tmp_path, "r.json", {"old": "foo", "new": "baz", "path": str(target)})
        out = supertool.dispatch(f"replace:@{spec}")
        assert "ERROR" not in out
        assert "foo" not in target.read_text()
        assert "baz" in target.read_text()


# ---------------------------------------------------------------------------
# replace_lines:@file
# ---------------------------------------------------------------------------

class TestAtFileReplaceLines:
    def test_basic_replace_lines(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("line1\nline2\nline3\n")
        spec = _write_json(tmp_path, "rl.json", {
            "path": str(target), "start": 2, "end": 2, "content": "REPLACED"
        })
        out = supertool.dispatch(f"replace_lines:@{spec}")
        assert "replaced" in out
        assert target.read_text() == "line1\nREPLACED\nline3\n"

    def test_missing_start_field_returns_error(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("line1\n")
        spec = _write_json(tmp_path, "rl.json", {"path": str(target), "end": 1, "content": "X"})
        out = supertool.dispatch(f"replace_lines:@{spec}")
        assert "ERROR" in out
        assert "start" in out


# ---------------------------------------------------------------------------
# paste:@file
# ---------------------------------------------------------------------------

class TestAtFilePaste:
    def test_basic_paste(self, tmp_path: Path) -> None:
        target = tmp_path / "x.py"
        target.write_text("old content\n")
        spec = _write_json(tmp_path, "p.json", {"path": str(target), "content": "new content"})
        out = supertool.dispatch(f"paste:@{spec}")
        assert "rewrote" in out
        assert "new content" in target.read_text()

    def test_paste_creates_new_file(self, tmp_path: Path) -> None:
        target = tmp_path / "new_file.py"
        spec = _write_json(tmp_path, "p.json", {"path": str(target), "content": "hello"})
        out = supertool.dispatch(f"paste:@{spec}")
        assert "created" in out
        assert target.exists()


# ---------------------------------------------------------------------------
# @- stdin route
# ---------------------------------------------------------------------------

class TestAtFileStdin:
    def test_edit_from_stdin(self, tmp_path: Path, monkeypatch) -> None:
        target = tmp_path / "x.py"
        target.write_text("alpha\n")
        payload = json.dumps({"old": "alpha", "new": "beta", "path": str(target)})
        import io
        monkeypatch.setattr(supertool.sys, "stdin", io.StringIO(payload))
        out = supertool.dispatch("edit:@-")
        assert "edited" in out
        assert "beta" in target.read_text()


# ---------------------------------------------------------------------------
# Non-mutating op — @file route NOT applied (plain parse fallback)
# ---------------------------------------------------------------------------

class TestAtFileNotAppliedToReadOps:
    def test_read_at_file_not_intercepted(self, tmp_path: Path) -> None:
        """read:@something should NOT go through the @file route — it's not a mutating op."""
        # Create a file literally named @something (edge case — just verify no crash)
        # More practically: read:@path is just a weird path, handled by op_read as "file not found"
        out = supertool.dispatch(f"read:@{tmp_path}/nonexistent.json")
        # Should be a normal "file not found" error from op_read, not @file machinery
        assert "ERROR" in out or "not found" in out.lower()
