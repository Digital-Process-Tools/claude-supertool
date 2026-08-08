"""#1060 - `read` and `replace_lines` disagreed about what a line is, so
editing line N could edit a different line.

render_file split with bytes.splitlines (LF, CR, CRLF). op_replace_lines split
with str.splitlines, which additionally breaks on VT, FF, FS, GS, RS, NEL,
U+2028 and U+2029. A file holding any of those above the target therefore had
two numberings: the caller read line N, asked to replace line N, and the write
landed somewhere else. Nothing reported a problem.

The contract chosen here is the conservative one - LF, CR and CRLF only, the
set every line-oriented CLI and every editor's gutter agrees on - and it is
owned by one function that both call sites use, so the next author cannot
reintroduce the split by editing one side.

Because that is a contract change and not a private detail, a read of a file
holding one of the extra characters says so.
"""

from __future__ import annotations

from pathlib import Path

import _supertool


LS = chr(0x2028)
PS = chr(0x2029)
# Every separator str.splitlines() breaks on and bytes.splitlines() does not.
EXTRA_SEPARATORS = [chr(0x0b), chr(0x0c), chr(0x1c), chr(0x1d), chr(0x1e),
                    chr(0x85), LS, PS]
ARROW = chr(0x2192)


def test_replace_lines_edits_the_line_the_read_showed(tmp_path: Path) -> None:
    """The round trip the issue describes: read, note the line, replace it."""
    f = tmp_path / "s.txt"
    f.write_bytes(f"one{LS}two\nTARGET\nthree\n".encode("utf-8"))

    shown = _supertool.render_file(str(f))
    target_line = None
    for raw in shown.splitlines():
        if "TARGET" in raw and ARROW in raw:
            target_line = int(raw.split(ARROW, 1)[0].strip())
            break
    assert target_line is not None, shown

    out = _supertool.op_replace_lines(str(f), target_line, target_line, "REPLACED")
    assert not out.startswith("ERROR"), out
    assert f.read_bytes() == f"one{LS}two\nREPLACED\nthree\n".encode("utf-8")


def test_read_and_replace_lines_agree_on_the_file_length(tmp_path: Path) -> None:
    """One numbering, so one length. `replace_lines` refuses past the end and
    names the length it used; that number must be the one `read` printed."""
    for sep in EXTRA_SEPARATORS:
        f = tmp_path / "len.txt"
        f.write_bytes(f"a{sep}b\nc\nd\n".encode("utf-8"))

        shown = _supertool.render_file(str(f))
        read_len = int(shown.split(" lines,", 1)[0].lstrip("("))

        out = _supertool.op_replace_lines(str(f), read_len + 2, read_len + 2, "x")
        assert out.startswith("ERROR"), (sep.encode("unicode_escape"), out)
        assert f"file length ({read_len})" in out, (
            sep.encode("unicode_escape"), read_len, out)


def test_line_split_helper_is_the_conservative_definition() -> None:
    """The one owner. LF, CR and CRLF split; nothing else does."""
    assert _supertool._split_lines_keepends("a\nb\r\nc\rd") == [
        "a\n", "b\r\n", "c\r", "d"]
    assert _supertool._split_lines_keepends(b"a\nb\r\nc\rd") == [
        b"a\n", b"b\r\n", b"c\r", b"d"]
    for sep in EXTRA_SEPARATORS:
        assert _supertool._split_lines_keepends(f"a{sep}b") == [f"a{sep}b"]
    assert _supertool._split_lines_keepends("") == []
    assert _supertool._split_lines_keepends(b"") == []


def _regex_split(data):
    """The definition, spelled once, as the reference both branches answer to."""
    rx = (_supertool._LINE_BREAK_RE_BYTES if isinstance(data, bytes)
          else _supertool._LINE_BREAK_RE_STR)
    out, pos = [], 0
    for m in rx.finditer(data):
        out.append(data[pos:m.end()])
        pos = m.end()
    if pos < len(data):
        out.append(data[pos:])
    return out


def test_the_bytes_branch_and_the_str_branch_are_the_same_contract() -> None:
    """`_split_lines_keepends` delegates the bytes side to `bytes.splitlines`
    for speed - `read` runs it on every call. One contract with two
    implementations is only safe while something proves them equal, so this is
    that something, and it is why the bytes delegation is allowed at all."""
    corpus = [
        "", "a", "a\n", "a\nb", "a\r\nb\rc\n", "\n\n\r\r\n",
        "trailing\r", "\r\n" * 5, "mixed\r\nlines\nhere\rend",
    ]
    corpus += [f"x{sep}y\nz\n" for sep in EXTRA_SEPARATORS]
    corpus += [f"{sep}" for sep in EXTRA_SEPARATORS]
    for text in corpus:
        assert _supertool._split_lines_keepends(text) == _regex_split(text), text
        raw = text.encode("utf-8")
        assert _supertool._split_lines_keepends(raw) == _regex_split(raw), raw


def test_read_discloses_a_character_other_tools_would_split_on(tmp_path: Path) -> None:
    """A caller who counted lines somewhere else - an editor, `wc -l`, a
    language runtime - may hold a different number for this file. Saying so
    beats silently picking one."""
    f = tmp_path / "amb.txt"
    f.write_bytes(f"one{LS}two\nthree\n".encode("utf-8"))
    shown = _supertool.render_file(str(f))
    assert "U+2028" in shown, shown


def test_read_says_nothing_about_an_ordinary_file(tmp_path: Path) -> None:
    f = tmp_path / "plain.txt"
    f.write_bytes(b"one\ntwo\nthree\n")
    shown = _supertool.render_file(str(f))
    assert "U+" not in shown, shown
