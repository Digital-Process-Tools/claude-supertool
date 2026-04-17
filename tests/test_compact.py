from __future__ import annotations

from pathlib import Path

import supertool


def test_compact_strips_blank_and_comment_lines(tmp_path: Path) -> None:
    f = tmp_path / "code.php"
    f.write_text("<?php\n\n// comment\nuse Foo;\n\n/* block */\nclass X {}\n")
    supertool._CONFIG = {"compact": True}
    out = supertool.op_read(str(f))
    assert "use Foo" in out
    assert "class X" in out
    assert "// comment" not in out
    assert "/* block" not in out


def test_compact_preserves_line_numbers(tmp_path: Path) -> None:
    f = tmp_path / "code.php"
    f.write_text("<?php\n\n\nuse Foo;\n")
    supertool._CONFIG = {"compact": True}
    out = supertool.op_read(str(f))
    assert "4→use Foo" in out


def test_compact_off_by_default(tmp_path: Path) -> None:
    f = tmp_path / "code.php"
    f.write_text("<?php\n\n// comment\nuse Foo;\n")
    out = supertool.op_read(str(f))
    assert "// comment" in out


def test_compact_disabled_when_grep_filter(tmp_path: Path) -> None:
    f = tmp_path / "code.php"
    f.write_text("<?php\n\n// comment\nuse Foo;\n")
    supertool._CONFIG = {"compact": True}
    out = supertool.op_read(str(f), grep_filter="comment")
    assert "// comment" in out
