"""The `append:` op (#383).

Appending used to cost two calls: `wc:PATH` to learn the line count, then
`replace_lines` with `start = N+1, end = N` — the inverted-range insert form.
The first call existed only to compute an argument for the second, and
`887:886` reads like a typo to whoever reviews the command later.
"""

from __future__ import annotations

from pathlib import Path


import supertool


def _write(path: Path, text: str) -> None:
    path.write_bytes(text.encode())


# ---------------------------------------------------------------------------
# happy paths
# ---------------------------------------------------------------------------

def test_append_adds_block_at_end(tmp_path: Path) -> None:
    f = tmp_path / "notes.md"
    _write(f, "line1\nline2\n")
    out = supertool.op_append(str(f), "## New section\n")
    assert "ERROR" not in out
    assert f.read_text() == "line1\nline2\n## New section\n"
    assert "appended to" in out
    assert "1 lines at 3-3" in out


def test_append_multi_line_block(tmp_path: Path) -> None:
    f = tmp_path / "notes.md"
    _write(f, "a\n")
    supertool.op_append(str(f), "b\nc\nd\n")
    assert f.read_text() == "a\nb\nc\nd\n"


def test_append_supplies_missing_trailing_newline_on_content(tmp_path: Path) -> None:
    f = tmp_path / "notes.md"
    _write(f, "a\n")
    supertool.op_append(str(f), "b")
    assert f.read_text() == "a\nb\n"


def test_append_fixes_missing_trailing_newline_on_target(tmp_path: Path) -> None:
    """Without this the appended block would land on the end of the last line."""
    f = tmp_path / "notes.md"
    _write(f, "a\nb")
    out = supertool.op_append(str(f), "c\n")
    assert f.read_text() == "a\nb\nc\n"
    assert "added the missing trailing newline first" in out


def test_append_creates_missing_file(tmp_path: Path) -> None:
    f = tmp_path / "new.md"
    out = supertool.op_append(str(f), "hello\n")
    assert "created" in out
    assert f.read_text() == "hello\n"


def test_append_creates_parent_dirs(tmp_path: Path) -> None:
    f = tmp_path / "deep" / "nested" / "new.md"
    out = supertool.op_append(str(f), "hello\n")
    assert "ERROR" not in out
    assert f.read_text() == "hello\n"


def test_append_preserves_existing_bytes(tmp_path: Path) -> None:
    """surrogateescape round-trip: bytes outside the append window are untouched."""
    f = tmp_path / "mixed.txt"
    f.write_bytes(b"valid\n\xff\xfe raw bytes\n")
    supertool.op_append(str(f), "tail\n")
    assert f.read_bytes() == b"valid\n\xff\xfe raw bytes\ntail\n"


def test_append_preserves_crlf_in_existing_content(tmp_path: Path) -> None:
    """Reading in text mode would translate CRLF→LF and write the whole file
    back normalised — an append must not touch a byte it wasn't given."""
    f = tmp_path / "win.txt"
    f.write_bytes(b"a\r\nb\r\n")
    supertool.op_append(str(f), "c\n")
    assert f.read_bytes() == b"a\r\nb\r\nc\n"


def test_append_matches_crlf_when_supplying_the_missing_newline(tmp_path: Path) -> None:
    f = tmp_path / "win.txt"
    f.write_bytes(b"a\r\nb")
    supertool.op_append(str(f), "c\n")
    assert f.read_bytes() == b"a\r\nb\r\nc\n"


def test_append_through_symlink_writes_to_the_target(tmp_path: Path) -> None:
    """_atomic_write must write through to the real file, not replace the link."""
    real = tmp_path / "real.txt"
    real.write_bytes(b"original\n")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    out = supertool.op_append(str(link), "added\n")

    assert "ERROR" not in out
    assert link.is_symlink(), "symlink was clobbered"
    assert real.read_text() == "original\nadded\n"


# ---------------------------------------------------------------------------
# receipt
# ---------------------------------------------------------------------------

def test_receipt_shows_preceding_context(tmp_path: Path) -> None:
    f = tmp_path / "notes.md"
    _write(f, "one\ntwo\nthree\nfour\n")
    out = supertool.op_append(str(f), "five\n")
    assert "three" in out
    assert "four" in out
    assert "five" in out
    assert "one" not in out  # only 2 lines of context


def test_receipt_caps_a_long_block(tmp_path: Path) -> None:
    """append is the op you reach for with a long entry — echoing it back in
    full is pure token cost on content the caller already had."""
    f = tmp_path / "notes.md"
    _write(f, "head\n")
    block = "".join(f"body{i}\n" for i in range(50))
    out = supertool.op_append(str(f), block)
    assert f"(+{50 - supertool._APPEND_RECEIPT_LINES} more appended lines)" in out
    assert "body0" in out
    assert "body49" not in out


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

def test_append_empty_path_errors() -> None:
    assert "ERROR: empty path" in supertool.op_append("", "x\n")


def test_append_empty_content_errors(tmp_path: Path) -> None:
    f = tmp_path / "notes.md"
    _write(f, "a\n")
    out = supertool.op_append(str(f), "")
    assert "ERROR: empty content" in out
    assert f.read_text() == "a\n"


def test_append_to_directory_errors(tmp_path: Path) -> None:
    out = supertool.op_append(str(tmp_path), "x\n")
    assert "ERROR" in out
    assert tmp_path.is_dir()


def test_append_path_traversal_blocked(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    out = supertool.op_append("../escaped.txt", "x\n")
    assert "ERROR" in out
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_append_traversal_creates_no_directories(tmp_path: Path, monkeypatch) -> None:
    """Containment must be checked BEFORE makedirs, or the dirs land outside
    cwd even though the write itself is rejected."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    out = supertool.op_append("../evil/nested/foo.txt", "x\n")
    assert "ERROR" in out
    assert not (tmp_path.parent / "evil").exists()


# ---------------------------------------------------------------------------
# dispatch wiring
# ---------------------------------------------------------------------------

def test_dispatch_colon_cli(tmp_path: Path) -> None:
    f = tmp_path / "notes.md"
    _write(f, "a\n")
    out = supertool.dispatch(f"append:::{f}:::b")
    assert "ERROR" not in out
    assert f.read_text() == "a\nb\n"


def test_dispatch_content_may_contain_colons(tmp_path: Path) -> None:
    f = tmp_path / "notes.md"
    _write(f, "a\n")
    supertool.dispatch(f"append:::{f}:::key: value: more")
    assert f.read_text() == "a\nkey: value: more\n"


def test_dispatch_payload_route(tmp_path: Path) -> None:
    f = tmp_path / "notes.md"
    _write(f, "a\n")
    payload = tmp_path / "p.toml"
    payload.write_text(
        f'path = "{f.as_posix()}"\n'
        "content = '''\ndef foo():\n    return {\"a\": 1}\n'''\n"
    )
    out = supertool.dispatch(f"append:@{payload}")
    assert "ERROR" not in out
    assert f.read_text() == 'a\ndef foo():\n    return {"a": 1}\n'


def test_append_is_a_builtin_op() -> None:
    """Registered, so a custom op can't shadow it and @file/validators wire up."""
    assert "append" in supertool._BUILTIN_OPS
    assert "append" in supertool._OP_TARGETS
    assert "append" in supertool._AT_FILE_BUILTIN_DEFAULTS


def test_append_is_not_parallel_safe() -> None:
    """It mutates — a batch must not run it concurrently with anything."""
    assert "append" not in supertool._PARALLEL_SAFE_OPS
