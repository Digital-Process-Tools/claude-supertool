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


def test_compact_strips_bang_form_html_comment_close(tmp_path: Path) -> None:
    """`--!>` closes an HTML comment exactly as `-->` does.

    Verified against Python's spec-compliant `html.parser`: both
    `<!-- x --!>` and `<!-- x -->` yield one comment token and no stray
    data. `_COMPACT_SKIP` recognised only the `-->` spelling, so a
    comment-only line closing a comment the `--!>` way survived
    compaction while its `-->` twin did not.

    The post-condition is the rendered `read` output, not the regex.
    """
    f = tmp_path / "page.html"
    f.write_text("<div>\n<!--\n  a note\n--!>\n</div>\n")
    supertool._CONFIG = {"compact": True}
    out = supertool.op_read(str(f))
    assert "<div>" in out
    assert "a note" in out
    assert "--!>" not in out


def test_compact_still_strips_plain_html_comment_close(tmp_path: Path) -> None:
    """Guard against overshoot: the `-->` spelling keeps working.

    Green before the fix as well as after — it exists to catch a change
    that widens the branch by breaking the case that already worked.
    """
    f = tmp_path / "page.html"
    f.write_text("<div>\n<!--\n  a note\n-->\n</div>\n")
    supertool._CONFIG = {"compact": True}
    out = supertool.op_read(str(f))
    assert "<div>" in out
    assert "-->" not in out


def test_compact_keeps_lines_that_merely_start_with_dashes(tmp_path: Path) -> None:
    """`--!?>` must not become "any line opening with two dashes".

    A YAML document marker carries content the reader asked for; only
    the comment-*close* spellings are droppable.
    """
    f = tmp_path / "doc.yml"
    f.write_text("---\nkey: value\n--- second doc\n")
    supertool._CONFIG = {"compact": True}
    out = supertool.op_read(str(f))
    assert "key: value" in out
    assert "second doc" in out
