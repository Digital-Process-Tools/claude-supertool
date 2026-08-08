"""#1049, second pass — three defects the Windows matrix found in the first fix.

Every fixture here writes **bytes**. The first fix went green on macOS because
`Path.write_text` translates `\\n` to `os.linesep`, so on Windows every fixture
file is CRLF and on POSIX none of them is: the fixtures, not the assertions,
were carrying the platform assumption. Written as bytes, each of these
reproduces on any platform.

1. `op_replace` read the file **twice with different translation settings**.
   The pass that counts matches kept universal-newline translation; the pass
   that writes got `newline=""`. On a CRLF file the first found an LF `old` and
   the second did not, so the write was a no-op and the receipt reported the
   first pass's number. `(2 replacements in 2 files)` over two unchanged files.

2. `op_replace_lines` re-terminated the caller's own content. A caller who
   writes `"a\\r\\nb\\nc\\r\\n"` chose those endings explicitly; rewriting them is
   the silent normalisation this issue is about, pointed the other way.

3. The line-ending receipt fired on every successful edit of a CRLF file, where
   nothing had been decided. On Windows that is every file, so a note meaning
   "I made a choice you did not" became noise on every call — and collided with
   `test_successful_edit_has_no_diagnostic`, which reads `↳` as "a diagnostic
   was emitted".
"""

from __future__ import annotations

from pathlib import Path

import _supertool


# --- 1. op_replace: the counting pass and the writing pass must agree -------


def _crlf_ini(tmp_path: Path) -> Path:
    d = tmp_path / "ini"
    d.mkdir()
    (d / "a.ini").write_bytes(b"[fr_all]\r\n[es_all]\r\n[es_all]\r\n")
    (d / "c.ini").write_bytes(b"[fr_all]\r\n[es_all]\r\n")
    return d


def test_replace_multiline_on_a_crlf_file_actually_replaces(
    tmp_path: Path,
) -> None:
    """The reported Windows red, as bytes. The `old` is LF because that is what
    a payload carries; the file is CRLF because Windows wrote it."""
    d = _crlf_ini(tmp_path)
    out = _supertool.op_replace("[es_all]\n[es_all]", "[es_all]", str(d))
    assert "ERROR" not in out, out
    assert (d / "a.ini").read_bytes() == b"[fr_all]\r\n[es_all]\r\n"
    assert (d / "c.ini").read_bytes() == b"[fr_all]\r\n[es_all]\r\n"


def test_replace_never_reports_a_replacement_it_did_not_write(
    tmp_path: Path,
) -> None:
    """The receipt's number has to come from the pass that touched the file.
    Taken from the counting pass it survived the write doing nothing at all —
    a green receipt over an unchanged file, which is worse than the red."""
    d = _crlf_ini(tmp_path)
    before = (d / "a.ini").read_bytes()
    out = _supertool.op_replace("[es_all]\n[es_all]", "[es_all]", str(d))
    after = (d / "a.ini").read_bytes()
    if "1 replacements" in out or "1 replacement" in out:
        assert before != after, (
            "the receipt claimed a replacement and the bytes are unchanged:\n"
            + out)


def test_replace_multiline_replacement_keeps_the_files_endings(
    tmp_path: Path,
) -> None:
    """An LF replacement written into a CRLF file must arrive as CRLF, or the
    op trades a whole-file rewrite for a scattered mixed-ending one."""
    d = _crlf_ini(tmp_path)
    out = _supertool.op_replace("[fr_all]\n[es_all]",
                                "[fr_all]\n[de_all]\n[es_all]",
                                str(d / "c.ini"))
    assert "ERROR" not in out, out
    assert (d / "c.ini").read_bytes() == (
        b"[fr_all]\r\n[de_all]\r\n[es_all]\r\n")


def test_replace_on_an_lf_file_is_unaffected(tmp_path: Path) -> None:
    d = tmp_path / "lf"
    d.mkdir()
    (d / "a.ini").write_bytes(b"[fr_all]\n[es_all]\n[es_all]\n")
    out = _supertool.op_replace("[es_all]\n[es_all]", "[es_all]", str(d))
    assert "ERROR" not in out, out
    assert (d / "a.ini").read_bytes() == b"[fr_all]\n[es_all]\n"


# --- 2. replace_lines passes the caller's content through verbatim ---------


def test_replace_lines_does_not_re_terminate_the_callers_content(
    tmp_path: Path,
) -> None:
    """`test_mixed_line_endings_preserved` states this contract and predates
    #1049: the endings inside the caller's block are the caller's choice. The
    file's convention is consulted only for a newline the op has to *invent* —
    the trailing one, when the block does not end in a line ending at all."""
    f = tmp_path / "f.txt"
    f.write_bytes(b"one\r\ntwo\r\nthree\r\nfour\r\nfive\r\n")
    out = _supertool.op_replace_lines(
        str(f), 2, 3, "line_a\r\nline_b\nline_c\r\n")
    assert not out.startswith("ERROR"), out
    raw = f.read_bytes()
    assert b"line_a\r\n" in raw
    assert b"line_b\n" in raw and b"line_b\r\n" not in raw
    assert raw == b"one\r\nline_a\r\nline_b\nline_c\r\nfour\r\nfive\r\n"


def test_replace_lines_still_supplies_the_trailing_newline_it_invents(
    tmp_path: Path,
) -> None:
    """The one ending the caller did not write is the one the op adds."""
    f = tmp_path / "f.txt"
    f.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    out = _supertool.op_replace_lines(str(f), 2, 2, "BETA")
    assert not out.startswith("ERROR"), out
    assert f.read_bytes() == b"alpha\r\nBETA\r\ngamma\r\n"


# --- 3. the note fires only when the op made a choice ----------------------


def test_a_clean_edit_of_a_crlf_file_says_nothing_about_line_endings(
    tmp_path: Path,
) -> None:
    """Nothing was decided: the `old` matched literally and every byte outside
    it is unchanged. On Windows every file is CRLF, so a note here fires on
    every edit ever made and trains the reader to skip the marker that means
    'I made a choice you did not'."""
    f = tmp_path / "x.py"
    f.write_bytes(b"a = 1\r\n")
    out = _supertool.op_edit("a = 1", "a = 2", str(f))
    assert not out.startswith("ERROR"), out
    assert f.read_bytes() == b"a = 2\r\n"
    assert "↳" not in out, out
    assert "line endings" not in out


def test_a_re_terminated_edit_still_says_so(tmp_path: Path) -> None:
    """The case where a choice *was* made keeps its note — otherwise the
    silence above is bought by removing the disclosure entirely."""
    f = tmp_path / "x.py"
    f.write_bytes(b"a = 1\r\nb = 2\r\n")
    out = _supertool.op_edit("a = 1\nb = 2", "a = 9\nb = 8", str(f))
    assert not out.startswith("ERROR"), out
    assert f.read_bytes() == b"a = 9\r\nb = 8\r\n"
    assert "CRLF" in out, out
