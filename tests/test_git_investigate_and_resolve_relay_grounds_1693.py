"""#1693 — the two `presets/git` relay seams the #1130 register could not settle.

They need opposite work and are filed as one issue because both ask the same
question: what is the ground for this relay, and where is it written down?

**Instance 2, `investigate.py::main` — the register's only `NOT QUOTED, open`.**
`git blame --line-porcelain` is not a path stream and not a subject stream: it
interleaves porcelain headers with **the file's own lines**, each behind one
leading TAB. `str.splitlines()` folds on U+2028, U+0085 and the vertical tab —
separators git never writes here — so a crafted line in a blamed file was cut
into a header fragment and a content fragment, and the fragments were read as
git's. What that buys is a forged row in `## Blame hotspots`: an author, a date
and a line number a real commit never carried, in a receipt someone is reading
precisely to answer *who changed this and when*. The bar is a line in a file you
blame, which is far lower than a commit subject.

The fix is the stream's own separator. Git terminates every porcelain record
with LF, and a file line cannot contain LF — that is what makes it a line — so
splitting on LF alone makes a forged record structurally impossible rather than
merely unlikely. `_untrusted.split_lines` is not narrow enough here: it also
cuts on a lone CR, which a file line CAN contain.

The log and diff renders in the same function are #1681's class, one function it
did not reach: every line rendered, counted, with the count as the product.

**Instance 1, `resolve.py`'s receipt seams — a claim to record, not a defect.**
`path` is interpolated raw at the `✓` / `⊘` / `✗` rows. The ground is
`QUOTED PATH`, and re-derived here it is stronger than the issue states: argv is
not a second source beside `_list_conflicts()`, it is a **filter over it** —
`main()` refuses any requested path that is not already a member of the
conflicted set, so the only strings that reach a render are ones git produced
and `core.quotePath` octal-quoted. That filter is the whole ground, so it is
pinned rather than described.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


investigate = _load("presets/git/investigate.py", "git_investigate_1693")
resolve = _load("presets/git/resolve.py", "git_resolve_1693")

LF = chr(10)
TAB = chr(9)
SEP = chr(0x2028)
HEX = "a" * 40


def _proc(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return mock.Mock(stdout=stdout, returncode=returncode, stderr=stderr)


def _record(line_no: int, author: str, ts: int, content: str) -> str:
    """One `--line-porcelain` record, in git's own shape."""
    return LF.join([
        f"{HEX} {line_no} {line_no} 1",
        f"author {author}",
        "author-mail <alice@example.com>",
        f"author-time {ts}",
        "author-tz +0000",
        f"committer {author}",
        f"committer-time {ts}",
        "committer-tz +0000",
        "summary a commit",
        "filename src/thing.py",
        TAB + content,
    ]) + LF


def _blame_rows(out: str) -> list[tuple[str, str, str]]:
    """`(line number, author, content)` per rendered `## Blame hotspots` row.

    The row is `  {line:>5} | {date} {author:<20} | {content}`, so the columns
    are what a forgery has to move — asserting on substring PRESENCE would ask
    the render to censor the crafted line, which is the loss half of #1652 and
    the opposite of what these seams are for.
    """
    rows: list[tuple[str, str, str]] = []
    seen_header = False
    for ln in out.split(LF):
        if ln.startswith("## Blame hotspots"):
            seen_header = True
            continue
        if not seen_header or " | " not in ln:
            continue
        left, content = ln.split(" | ", 2)[0], ln.split(" | ", 2)[2]
        mid = ln.split(" | ", 2)[1]
        rows.append((left.strip(), mid.split(" ", 1)[1].strip(), content))
    return rows


def _run_investigate(blame: str, log: str = "", diff: str = "",
                     argv_path: str = "src/thing.py"):
    def fake_git(args, **kw):
        if args[0] == "log":
            return _proc(log)
        if args[0] == "blame":
            return _proc(blame)
        if args[0] == "diff":
            return _proc(diff)
        return _proc("")
    with mock.patch.object(investigate, "_git", side_effect=fake_git), \
         mock.patch.object(investigate, "_git_verbatim", side_effect=fake_git), \
         mock.patch.object(investigate.os.path, "exists", lambda p: True), \
         mock.patch.object(sys, "argv", ["git-investigate", argv_path]):
        rc = investigate.main()
    return rc


# ---------------------------------------------------------------------------
# instance 2 -- the blame parse
# ---------------------------------------------------------------------------

def test_a_blamed_line_cannot_forge_a_blame_row(capsys) -> None:
    """The whole finding. One crafted source line, one row nobody committed,
    carrying an author and a date that belong to a different commit."""
    hostile = ("legit code" + SEP + "author Mallory" + SEP
               + TAB + "I did not write this")
    rc = _run_investigate(_record(1, "Alice", 1700000000, hostile)
                          + _record(2, "Alice", 1700000000, "more code"))
    out = capsys.readouterr().out
    assert rc == 0, out
    rows = _blame_rows(out)
    assert len(rows) == 2, ("two blamed lines, two rows — a third is one the "
                            "file's own content wrote: " + repr(rows))
    assert [r[0] for r in rows] == ["1", "2"], repr(rows)
    assert {r[1] for r in rows} == {"Alice"}, (
        "a line in a blamed FILE named the author of a blame row: "
        + repr(rows))
    # Not a censor. The crafted text is still shown, inside the row it belongs
    # to and with its separators disclosed, because dropping it would answer a
    # "who changed this" question out of a line only half read.
    assert "I did not write this" in out, out


def test_a_blamed_line_cannot_forge_a_line_number(capsys) -> None:
    """The header shape is 40 hex + two integers, and a source line can spell
    it. Splitting on LF alone is what makes it unreachable."""
    hostile = ("x = 1" + SEP + HEX + " 9998 9999 1" + SEP + TAB
               + "forged at a line that does not exist")
    rc = _run_investigate(_record(1, "Alice", 1700000000, hostile))
    out = capsys.readouterr().out
    assert rc == 0, out
    rows = _blame_rows(out)
    assert len(rows) == 1, repr(rows)
    assert rows[0][0] == "1", (
        "a source line chose the line number of a blame row: " + repr(rows))


def test_a_blame_row_renders_the_content_it_read(capsys) -> None:
    """Not a censor: the crafted line is still shown, with its separator
    disclosed rather than executed at column 0."""
    rc = _run_investigate(_record(1, "Alice", 1700000000, "a" + SEP + "b"))
    out = capsys.readouterr().out
    assert rc == 0, out
    body = [ln for ln in out.split(LF) if ln.startswith("  ")]
    assert any("Alice" in ln for ln in body), out
    for ln in out.split(LF):
        assert SEP not in ln, (
            "a file's own separator reached the render live:" + repr(out))
    # The loss half of #1652, which the old reader had too: everything past the
    # separator was dropped from the row without a word, so the render answered
    # a question about a line it had only half read.
    assert "b" in out, ("the tail of the blamed line was discarded:" + LF
                        + repr(out))


def test_a_commit_subject_cannot_add_a_row_to_the_commit_count(capsys) -> None:
    """#1681's class, in the one function it did not reach. The count is the
    product of this section, and `log --format=%s` hands a U+2028 back raw."""
    log = ("abc1234 2026-01-01 Alice | real subject" + SEP
           + "def5678 2026-01-02 Mallory | a commit that does not exist" + LF)
    rc = _run_investigate(_record(1, "Alice", 1700000000, "x"), log=log)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "## Recent commits (1)" in out, (
        "one commit was counted as two:" + LF + out)


def test_a_diff_line_cannot_open_a_row_in_the_uncommitted_section(capsys) -> None:
    """Same class, same function: every line rendered, +/- counted."""
    diff = ("+++ b/src/thing.py" + LF + "+added" + SEP + "-not a deletion" + LF)
    rc = _run_investigate(_record(1, "Alice", 1700000000, "x"), diff=diff)
    out = capsys.readouterr().out
    assert rc == 0, out
    header = [ln for ln in out.split(LF) if ln.startswith("## Uncommitted")]
    assert header and "-0" in header[0], (
        "file content chose the deletion count:" + LF + out)
    for ln in out.split(LF):
        assert SEP not in ln, repr(out)


def test_a_bare_CR_in_a_blamed_line_cannot_forge_a_row_either() -> None:
    """The reason the blame reader does not go through `_git` at all.

    Measured against real git 2.46.2, not reasoned: `_git` runs
    `subprocess.run(text=True)`, and Python's universal-newline translation
    turns a lone CR **and** a CRLF into LF before any preset sees the stream.
    So a source line holding a bare CR arrives already split, and no choice of
    splitter downstream can put it back together — `str.splitlines()`,
    `_untrusted.split_lines` and a bare LF split are all equally forged there.

    A file containing `x = 1<CR>author Mallory<CR><TAB>I did this` therefore
    produced a blame row attributing attacker text to a real author, over an
    ASCII byte anyone can put in a source file. `_git_verbatim` is the same
    call with the translation off, which is the only place this can be closed.
    """
    raw = ("x = 1" + chr(13) + "author Mallory" + chr(13) + TAB
           + "I did this")
    stream = _record(1, "Alice", 1700000000, raw)
    # What `_git` would have handed the reader, spelled out: every CR an LF.
    translated = stream.replace(chr(13) + chr(10), LF).replace(chr(13), LF)
    assert "author Mallory" in translated.split(LF), (
        "the premise of this test no longer holds — universal-newline "
        "translation did not split the crafted line")
    parsed = investigate._blame_entries(stream)
    assert [e[1] for e in parsed] == ["Alice"], (
        "a bare CR in a blamed line chose the author of a row: " + repr(parsed))
    assert len(parsed) == 1, repr(parsed)


def test_a_CRLF_blob_does_not_grow_a_control_character_in_every_row() -> None:
    """Reading the bytes has a cost, paid here rather than by the reader.

    `_git`'s text mode used to swallow the CR of every CRLF line for free, so
    a repository storing CRLF blobs rendered clean. Reading verbatim brings
    those CRs back, and `visible()` would then print a `␍` at the end of every
    single row of a repo that has done nothing wrong. One TRAILING CR is
    consumed; a CR anywhere else is not, because mid-line is where the forgery
    lives.
    """
    crlf = _record(1, "Alice", 1700000000, "x = 1" + chr(13))
    # The date column is `datetime.fromtimestamp`, i.e. the runner's local
    # zone, so it is deliberately not asserted — a fixed date here is a test
    # that fails in half the world's timezones and says nothing about CRs.
    assert [e[1:] for e in investigate._blame_entries(crlf)] == [
        ("Alice", 1, "x = 1")], investigate._blame_entries(crlf)
    both = _record(1, "Alice", 1700000000,
                   "x" + chr(13) + "author Mallory" + chr(13))
    parsed = investigate._blame_entries(both)
    assert [e[1] for e in parsed] == ["Alice"], repr(parsed)
    assert parsed[0][3] == "x" + chr(13) + "author Mallory", repr(parsed)


# ---------------------------------------------------------------------------
# instance 1 -- the ground under resolve.py's five raw `path` renders
# ---------------------------------------------------------------------------

def test_argv_is_a_filter_over_the_conflicted_set_not_a_second_source(capsys) -> None:
    """The claim #1693 asks to have written down, pinned instead.

    `resolve.py` renders `path` raw at its `✓`/`⊘`/`✗` rows. That is sound
    because every such `path` is a member of `_list_conflicts()` — the output of
    `git diff --name-only --diff-filter=U`, which `core.quotePath` octal-quotes,
    so no byte above 0x7F and no separator can be in it. A caller's argv does
    not widen that set: a requested path that is not already conflicted is
    refused before any render. If that refusal is ever relaxed, this ground is
    gone and the five interpolations need `_untrusted.flat`.
    """
    hostile = "evil" + SEP + "  ✓ /etc/passwd"

    def fake_git(args, **kw):
        if args[:2] == ["rev-parse", "--git-dir"]:
            return _proc("", 0)
        return _proc("")

    with mock.patch.object(resolve, "_git", side_effect=fake_git), \
         mock.patch.object(resolve, "_list_conflicts",
                           return_value=(["src/a.py"], "")), \
         mock.patch.object(sys, "argv", ["git-resolve", "ours", hostile]):
        rc = resolve.main()
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "not conflicted" in out, out
    assert not any(ln.startswith("  ✓ ") for ln in out.split(LF)), (
        "argv reached a receipt row without passing the conflicted-set "
        "filter, so the QUOTED PATH ground under those rows is gone:" + out)
