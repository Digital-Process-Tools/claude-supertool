"""Regression tests for issue #252.

A `batch:@file` edit/replace/replace_lines/paste sub-op whose old/new content
contains a literal `:::` must apply correctly. The `@file` JSON route carries
explicit fields (old/new/path) and must never re-tokenize on `:::` — exactly as
a standalone `edit:@-` call already does. Batch previously re-serialized the
sub-op into a `edit:::OLD:::NEW:::PATH` string and re-split it, corrupting any
content that itself contained `:::`.
"""
from __future__ import annotations

import json
from pathlib import Path

import supertool


def _write_json(tmp_path: Path, name: str, payload) -> Path:
    f = tmp_path / name
    f.write_text(json.dumps(payload))
    return f


class TestBatchColonContent252:
    def test_edit_content_with_triple_colon(self, tmp_path: Path) -> None:
        target = tmp_path / "conf.json"
        target.write_text('"example": "read:OLD.php:::grep=class"\n')
        ops = [
            {
                "op": "edit",
                "path": str(target),
                "old": '"example": "read:OLD.php:::grep=class"',
                "new": '"example": "read:src/Foo.php:::grep=class"',
            }
        ]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        # The edit must succeed and the full new content (including the ::: run)
        # must land verbatim.
        assert "ERROR" not in out, out
        assert target.read_text() == '"example": "read:src/Foo.php:::grep=class"\n'

    def test_replace_content_with_triple_colon(self, tmp_path: Path) -> None:
        target = tmp_path / "code.php"
        target.write_text("$x = a:::b;\n$y = a:::b;\n")
        ops = [
            {
                "op": "replace",
                "path": str(target),
                "old": "a:::b",
                "new": "c:::d",
            }
        ]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "ERROR" not in out, out
        assert target.read_text() == "$x = c:::d;\n$y = c:::d;\n"

    def test_replace_lines_content_with_triple_colon(self, tmp_path: Path) -> None:
        target = tmp_path / "code.php"
        target.write_text("line1\nline2\nline3\n")
        ops = [
            {
                "op": "replace_lines",
                "path": str(target),
                "start": 2,
                "end": 2,
                "content": "left:::right",
            }
        ]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "ERROR" not in out, out
        assert target.read_text() == "line1\nleft:::right\nline3\n"

    def test_paste_content_with_triple_colon(self, tmp_path: Path) -> None:
        target = tmp_path / "code.php"
        target.write_text("old\n")
        ops = [
            {
                "op": "paste",
                "path": str(target),
                "content": "a:::b:::c",
            }
        ]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "ERROR" not in out, out
        assert "a:::b:::c" in target.read_text()

    def test_multiline_content_with_triple_colon(self, tmp_path: Path) -> None:
        target = tmp_path / "code.php"
        target.write_text("marker\n")
        ops = [
            {
                "op": "edit",
                "path": str(target),
                "old": "marker",
                "new": "$r = $ok ? a:::b : c:::d;\n$s = 1;",
            }
        ]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "ERROR" not in out, out
        assert target.read_text() == "$r = $ok ? a:::b : c:::d;\n$s = 1;\n"

    def test_single_colon_still_works(self, tmp_path: Path) -> None:
        target = tmp_path / "code.php"
        target.write_text("host = a:b\n")
        ops = [
            {
                "op": "edit",
                "path": str(target),
                "old": "host = a:b",
                "new": "host = c:d",
            }
        ]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "ERROR" not in out, out
        assert target.read_text() == "host = c:d\n"

    def test_replace_all_edit_with_triple_colon(self, tmp_path: Path) -> None:
        # replace_all:true promotes edit→replace; content with ::: must still
        # survive the pre_parsed routing and replace every occurrence.
        target = tmp_path / "code.php"
        target.write_text("a:::b\na:::b\n")
        ops = [
            {
                "op": "edit",
                "path": str(target),
                "old": "a:::b",
                "new": "x:::y",
                "replace_all": True,
            }
        ]
        spec = _write_json(tmp_path, "ops.json", ops)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "ERROR" not in out, out
        assert target.read_text() == "x:::y\nx:::y\n"

    def test_mixed_batch_read_and_colon_edit(self, tmp_path: Path) -> None:
        f = tmp_path / "code.php"
        f.write_text("a:::b\n")
        payload = {
            "continue_on_error": False,
            "ops": [
                {"op": "read", "path": str(f)},
                {"op": "edit", "path": str(f), "old": "a:::b", "new": "x:::y"},
                {"op": "read", "path": str(f)},
            ],
        }
        spec = _write_json(tmp_path, "ops.json", payload)
        out = supertool.dispatch(f"batch:@{spec}")
        assert "ERROR" not in out, out
        assert f.read_text() == "x:::y\n"
