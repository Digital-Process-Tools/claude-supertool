"""#945 follow-up — the window note must describe the window that was emitted.

The note added by #945 derives everything from `offset + printed` and attributes
any shortfall against the requested end to EOF. Two of its three inputs are
wrong in the general case:

* a **byte-cap** break is also a shortfall, and gets reported as "the end of the
  file" — an absence produced by the tool, stated as an absence in the world;
* under **compact mode** `printed` counts *emitted* lines while the loop skips
  blanks and comments without counting them, so `offset + printed` is neither
  the last line read nor the last line shown.

Every assertion below compares the note against the *body of the same render*,
so it cannot pass on a version that merely formats the note plausibly.
"""

from __future__ import annotations

import re
from pathlib import Path

import supertool

_LINE_RE = re.compile(r"^\s*(\d+)→", re.M)
_WINDOW_RE = re.compile(r"^window: .*$", re.M)
_RETURNING_RE = re.compile(r"returning lines (\d+)-(\d+) of (\d+)")
# The trailing `)` was an assumption about formatting, not part of what any
# assertion here is about: every use reads `group(1)` and compares the COUNT.
# Since #1820 the footer may carry a reason after the count — which bound ended
# the window — so the paren no longer closes immediately. Widened to a
# lookahead rather than relaxed: it still anchors on the footer's own opening
# paren and on the count, so it cannot start matching some other parenthesised
# number in the render. No assertion below is weakened; each still compares the
# same count against the same body.
_MORE_RE = re.compile(r"\((\d+) more lines?(?=[)\s])")


def _emitted(out: str) -> list[int]:
    return [int(m) for m in _LINE_RE.findall(out)]


def _window(out: str) -> str:
    m = _WINDOW_RE.search(out)
    assert m, f"no window note in:\n{out[:2000]}"
    return m.group(0)


def _wide(tmp_path: Path, lines: int = 200) -> Path:
    """Lines fat enough that MAX_READ_BYTES (20000) bites well before EOF."""
    f = tmp_path / "big.txt"
    f.write_bytes(b"".join(b"L%04d " % i + b"x" * 400 + b"\n"
                           for i in range(1, lines + 1)))
    return f


def _mixed(tmp_path: Path, lines: int = 200) -> Path:
    """Alternating comment / code — compact mode drops exactly half."""
    f = tmp_path / "mix.py"
    body = []
    for i in range(lines):
        n = i // 2 + 1
        body.append(b"# comment %d\n" % n if i % 2 == 0
                    else b"code_%03d = %d\n" % (n, n))
    f.write_bytes(b"".join(body))
    return f


def _compact(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_is_compact", lambda: True)


# --- F3: a byte cap is not the end of the file -----------------------------


def test_byte_capped_read_does_not_call_the_cap_the_end_of_the_file(
    tmp_path: Path,
) -> None:
    """200 fat lines, 150 requested, ~49 fit under the 20KB cap. The old note
    said "stopping at line 54, the end of the file" at the top of a render
    whose own footer said 146 lines remained."""
    f = _wide(tmp_path)
    out = supertool.dispatch(f"read:{f}:5:150")
    note = _window(out)
    assert "end of the file" not in note, note
    assert "truncated at" in out, "fixture did not reach the byte cap"
    assert "cap" in note, note


def test_byte_capped_note_agrees_with_the_last_line_it_emitted(
    tmp_path: Path,
) -> None:
    f = _wide(tmp_path)
    out = supertool.dispatch(f"read:{f}:5:150")
    lines = _emitted(out)
    m = _RETURNING_RE.search(_window(out))
    assert m, _window(out)
    assert int(m.group(1)) == lines[0]
    assert int(m.group(2)) == lines[-1], (
        f"note claims it stopped at {m.group(2)}, body ends at {lines[-1]}")


def test_byte_capped_note_and_footer_do_not_contradict(tmp_path: Path) -> None:
    """Two counts in one render, both describing where reading stopped."""
    f = _wide(tmp_path)
    out = supertool.dispatch(f"read:{f}:5:150")
    note_last = int(_RETURNING_RE.search(_window(out)).group(2))
    foot = re.search(r"showed lines \d+-(\d+) of", out)
    assert foot, out[-400:]
    assert note_last == int(foot.group(1))


# --- F4: compact mode -------------------------------------------------------


def test_compact_note_names_the_window_actually_returned(
    tmp_path: Path, monkeypatch
) -> None:
    """The reported case: the note said 11-35 while the body ran to line 60."""
    _compact(monkeypatch)
    f = _mixed(tmp_path)
    out = supertool.dispatch(f"read:{f}:10:50")
    lines = _emitted(out)
    assert lines, out
    m = _RETURNING_RE.search(_window(out))
    assert m, _window(out)
    assert int(m.group(2)) == lines[-1], (
        f"note claims it stopped at {m.group(2)}, body ends at {lines[-1]}")


def test_compact_note_does_not_claim_eof_mid_file(
    tmp_path: Path, monkeypatch
) -> None:
    _compact(monkeypatch)
    f = _mixed(tmp_path)
    out = supertool.dispatch(f"read:{f}:10:50")
    note = _window(out)
    assert "end of the file" not in note, note
    assert _emitted(out)[-1] < 200, "fixture must stop short of EOF"


def test_compact_note_states_how_many_lines_it_suppressed(
    tmp_path: Path, monkeypatch
) -> None:
    """25 of the 50 lines in the span were dropped. A note that says nothing
    about that reads as a contiguous window."""
    _compact(monkeypatch)
    f = _mixed(tmp_path)
    out = supertool.dispatch(f"read:{f}:10:50")
    emitted = _emitted(out)
    note = _window(out)
    assert str(len(emitted)) in note, note
    assert "compact" in note, note


def test_compact_more_lines_footer_counts_the_lines_never_read(
    tmp_path: Path, monkeypatch
) -> None:
    """`(N more lines)` used `line_count - offset - printed`, so suppressed
    lines were counted as unread. The body visibly ends at 60; 140 remain."""
    _compact(monkeypatch)
    f = _mixed(tmp_path)
    out = supertool.dispatch(f"read:{f}:10:50")
    last = _emitted(out)[-1]
    m = _MORE_RE.search(out)
    assert m, out[-400:]
    assert int(m.group(1)) == 200 - last, (
        f"footer says {m.group(1)} more, but the body ends at {last} of 200")


# --- three states, not two --------------------------------------------------


def test_limit_reached_mid_file_is_named_as_the_limit(tmp_path: Path) -> None:
    f = tmp_path / "many.txt"
    f.write_bytes(b"".join(b"L%d\n" % i for i in range(1, 101)))
    note = _window(supertool.dispatch(f"read:{f}:10:20"))
    assert "the limit was reached" in note, note
    assert "end of the file" not in note, note


def test_file_end_reached_is_still_named_as_the_file_end(
    tmp_path: Path,
) -> None:
    f = tmp_path / "many.txt"
    f.write_bytes(b"".join(b"L%d\n" % i for i in range(1, 101)))
    note = _window(supertool.dispatch(f"read:{f}:90:50"))
    assert "end of the file" in note, note
    assert _emitted(supertool.dispatch(f"read:{f}:90:50"))[-1] == 100


def test_limit_and_eof_coinciding_is_settled_by_the_file_end(
    tmp_path: Path,
) -> None:
    """offset 90 + limit 10 on a 100-line file ends on both at once.

    #945 read that as a tie and declined. #1342 overturned it: the note prints
    `of 100` in its own text, so `last_scanned >= line_count` decides which
    state the caller is in — "you have everything from line 91" rather than
    "you may have been cut off". The limit is still named; it is no longer
    offered as a rival explanation for a question the op can answer.
    """
    f = tmp_path / "many.txt"
    f.write_bytes(b"".join(b"L%d\n" % i for i in range(1, 101)))
    note = _window(supertool.dispatch(f"read:{f}:90:10"))
    assert "cannot be told apart" not in note, note
    assert "the end of the file" in note, note
    assert "the limit was reached" in note, note


def test_uncapped_full_read_from_an_offset_reports_the_file_end(
    tmp_path: Path, monkeypatch
) -> None:
    """`:full` outside Claude Code drops both caps — the only honest reason
    left is EOF, and a synthesised limit must not read as an ambiguity."""
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    f = tmp_path / "many.txt"
    f.write_bytes(b"".join(b"L%d\n" % i for i in range(1, 101)))
    note = _window(supertool.dispatch(f"read:{f}:90:full"))
    assert "end of the file" in note, note
    assert "coincide" not in note, note
