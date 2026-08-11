"""An argument grammar must name which reading it took, at the point of use (#1417).

Three instances folded into #1417 and covered here:

* **#1414** — `read:PATH:A:B` (OFFSET:LIMIT) and `read:PATH:A-B` (START:END) are one
  character apart and return different windows; the receipt never said which
  grammar it had taken.
* **#1138** — `read:PATH:1:40` returns lines 2-41. The skip-count lecture was gated
  on `LIMIT <= OFFSET` and deferred the rest to `_read_range_note` (#382), which is
  itself gated on overshoot-or-`LIMIT < 2*OFFSET`. `1:40` satisfies neither, so the
  archetype in the issue fell in the hole between two gates and got no hint at all.
* **#1407** — a nested `batch` sub-op wants `path = "@inner.toml"`; `file = "..."`
  was accepted as an unnamed single positional and the `@`-less refusal answered in
  command-line grammar, naming no field.

The parse is deliberately NOT changed. Measured over 5,598 real `read:PATH:N:M`
invocations, both parses #1138 proposed (refuse `:N:M`, or re-read it as
`START-END` when N < M) would refuse or silently re-aim thousands of calls that
meant OFFSET:LIMIT.
"""

from __future__ import annotations

import json
from pathlib import Path

import supertool


def _numbered(tmp_path: Path, name: str, count: int) -> Path:
    body = "".join(f"line{i}\n" for i in range(1, count + 1))
    f = tmp_path / name
    f.write_bytes(body.encode())
    return f


def _toml_str(value: str) -> str:
    """A TOML basic string for VALUE. `json.dumps` escapes a Windows separator
    correctly, where an f-string would emit a raw backslash that TOML then reads
    as an escape."""
    return json.dumps(value)


# ---------------------------------------------------------------------------
# #1138 — the hole between the two gates
# ---------------------------------------------------------------------------

def test_offset_one_limit_forty_is_not_silent(tmp_path: Path) -> None:
    """`read:f:1:40` on a 200-line file: no overshoot, LIMIT >= 2*OFFSET, so
    neither existing gate fired and the caller who meant lines 1-40 was told
    nothing at all."""
    f = _numbered(tmp_path, "many.txt", 200)
    out = supertool.dispatch(f"read:{f}:1:40")
    assert "OFFSET is a skip count" in out, out


def test_offset_form_names_the_range_spelling_of_the_window_returned(
        tmp_path: Path) -> None:
    """The fact, not the guess: lines 2-41 came back, and `read:f:2-41` spells
    exactly that window. Seeing the `2` against the `1` that was typed is what
    makes the off-by-one visible."""
    f = _numbered(tmp_path, "many.txt", 200)
    out = supertool.dispatch(f"read:{f}:1:40")
    assert f"this window is read:{f}:2-41" in out, out


def test_offset_form_also_names_the_call_for_the_lines_probably_meant(
        tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 200)
    out = supertool.dispatch(f"read:{f}:1:40")
    assert f"for lines 1-40 use read:{f}:1-40" in out, out


def test_both_ranges_are_named_and_they_differ(tmp_path: Path) -> None:
    """The window returned and the window probably meant are one line apart. A
    receipt naming only one of them is what let the mistake survive."""
    f = _numbered(tmp_path, "many.txt", 200)
    out = supertool.dispatch(f"read:{f}:1:40")
    assert f"read:{f}:2-41" in out, out
    assert f"read:{f}:1-40" in out, out


def test_limit_below_offset_keeps_its_existing_correction(tmp_path: Path) -> None:
    """#945's band still says what it always said — this widens coverage, it
    does not move the old hint."""
    f = _numbered(tmp_path, "many.txt", 200)
    out = supertool.dispatch(f"read:{f}:120:5")
    assert f"for lines 120-124 use read:{f}:120-124" in out, out
    assert f"this window is read:{f}:121-125" in out, out


# ---------------------------------------------------------------------------
# #1414 — the receipt names which of the two grammars ran
# ---------------------------------------------------------------------------

def test_offset_form_is_named_as_such(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 200)
    out = supertool.dispatch(f"read:{f}:1:40")
    assert "OFFSET:LIMIT form" in out, out
    assert "START-END form" not in out, out


def test_range_form_is_named_as_such(tmp_path: Path) -> None:
    f = _numbered(tmp_path, "many.txt", 200)
    out = supertool.dispatch(f"read:{f}:120-124")
    assert "START-END form" in out, out
    assert "OFFSET:LIMIT form" not in out, out


def test_range_form_is_still_not_lectured(tmp_path: Path) -> None:
    """A correct call being told it was wrong is #983; naming the form must not
    reintroduce it."""
    f = _numbered(tmp_path, "many.txt", 200)
    out = supertool.dispatch(f"read:{f}:120-124")
    assert "OFFSET is a skip count" not in out, out


# ---------------------------------------------------------------------------
# #1407 — a nested batch's field is named, in payload grammar
# ---------------------------------------------------------------------------

def _inner_batch(tmp_path: Path) -> Path:
    target = tmp_path / "target.txt"
    target.write_text("alpha\n", encoding="utf-8")
    inner = tmp_path / "inner.toml"
    inner.write_text(
        "[[ops]]\nop = \"read\"\npath = " + _toml_str(str(target)) + "\n",
        encoding="utf-8",
    )
    return inner


def _outer_batch(tmp_path: Path, field: str, value: str) -> Path:
    outer = tmp_path / "outer.toml"
    outer.write_text(
        "[[ops]]\nop = \"batch\"\n" + field + " = " + _toml_str(value) + "\n",
        encoding="utf-8",
    )
    return outer


def test_nested_batch_rejects_the_wrong_field_by_name(tmp_path: Path) -> None:
    """`file = "inner.toml"` was accepted as an unnamed single positional and
    dispatched as `batch:inner.toml`, whose refusal named no field at all."""
    inner = _inner_batch(tmp_path)
    outer = _outer_batch(tmp_path, "file", str(inner))
    out = supertool.dispatch(f"batch:@{outer}")
    assert "file" in out, out
    assert "accepted: path" in out, out


def test_bare_path_refusal_names_the_payload_field_and_the_at_sign(
        tmp_path: Path) -> None:
    """`path = "inner.toml"` without the `@` is the other half of #1407: the
    refusal spoke only command-line grammar."""
    inner = _inner_batch(tmp_path)
    outer = _outer_batch(tmp_path, "path", str(inner))
    out = supertool.dispatch(f"batch:@{outer}")
    assert 'path = "@' in out, out
    assert "batch:@" in out, out


def test_nested_batch_with_the_documented_spelling_still_runs(
        tmp_path: Path) -> None:
    inner = _inner_batch(tmp_path)
    outer = _outer_batch(tmp_path, "path", "@" + str(inner))
    out = supertool.dispatch(f"batch:@{outer}")
    assert "alpha" in out, out
    assert "ERROR" not in out, out

# ---------------------------------------------------------------------------
# Round 2 — the review of 58135ef
# ---------------------------------------------------------------------------

def test_the_named_window_is_the_one_that_came_back_not_the_one_requested(
        tmp_path: Path) -> None:
    """`this window is read:PATH:A-B` is offered as a fact. When EOF clamps the
    window short, the requested end is not that fact — naming it there is the
    house defect inside the line written to cure it."""
    f = _numbered(tmp_path, "short.txt", 10)
    out = supertool.dispatch(f"read:{f}:8:5")
    assert "returning lines 9-10 of 10" in out, out
    assert f"this window is read:{f}:9-10" in out, out
    assert f"this window is read:{f}:9-13" not in out, out


def test_only_one_range_is_ever_proposed_as_the_reading_meant(
        tmp_path: Path) -> None:
    """#382's note and the window hint both answer 'what did you mean', and on
    a call that fires both they answered it differently: `read:f:20:25` drew
    `for lines 20-44` from one and `For lines 20-25` from the other. The window
    hint yields the guess to #382 and keeps only the fact."""
    f = _numbered(tmp_path, "many.txt", 400)
    out = supertool.dispatch(f"read:{f}:20:25")
    assert f"read:{f}:20-25" in out, out
    assert f"read:{f}:20-44" not in out, out
    assert f"this window is read:{f}:21-45" in out, out


def test_the_fact_is_stated_even_when_382_speaks(tmp_path: Path) -> None:
    """Yielding the guess must not silence the window's own range spelling —
    that is the half #1138 never got."""
    f = _numbered(tmp_path, "many.txt", 400)
    out = supertool.dispatch(f"read:{f}:20:25")
    assert "OFFSET is a skip count" in out, out


def test_the_guess_is_still_made_when_382_is_silent(tmp_path: Path) -> None:
    """`1:40` on a long file is the shape that trips neither of #382's tells,
    so the window hint is the only place the second reading can be named."""
    f = _numbered(tmp_path, "many.txt", 400)
    out = supertool.dispatch(f"read:{f}:1:40")
    assert f"for lines 1-40 use read:{f}:1-40" in out, out
    assert "not START:END" not in out, out

# ---------------------------------------------------------------------------
# Adjacent: the notifier/cursor range for `read` reads OFFSET as a start line
# ---------------------------------------------------------------------------

def test_notifier_range_matches_the_lines_the_read_actually_returned() -> None:
    """`read:f:19:1` renders line 20, and the editor was told to highlight line
    19 — the same OFFSET-is-a-start-line mistake #1417 is about, in the code
    that decides where the cursor lands. `docs/notifiers.md` documented the
    off-by-one faithfully, which is why it survived."""
    assert supertool._read_target_read(["read", "f.py", "19", "1"]) == (
        "f.py", 20, 20)
    assert supertool._read_target_read(["read", "f.py", "100", "60"]) == (
        "f.py", 101, 160)


def test_notifier_range_understands_the_range_form() -> None:
    """`read:PATH:START-END` is the form the docs tell callers to prefer, and it
    is the one form whose range needs no arithmetic at all — it was the only one
    that got no highlight."""
    assert supertool._read_target_read(["read", "f.py", "120-124"]) == (
        "f.py", 120, 124)


def test_notifier_range_declines_rather_than_inverting() -> None:
    """A zero LIMIT would compute an END before its START. No range is an
    honest answer; a backwards one is not."""
    assert supertool._read_target_read(["read", "f.py", "10", "0"]) == (
        "f.py", None, None)

def test_one_skipped_line_is_singular(tmp_path: Path) -> None:
    """`offset 1` is the archetype in #1138 and therefore the sentence most
    often read; it rendered as "1 lines were skipped"."""
    f = _numbered(tmp_path, "many.txt", 200)
    out = supertool.dispatch(f"read:{f}:1:40")
    assert "1 line was skipped" in out, out
    assert "1 lines were skipped" not in out, out
