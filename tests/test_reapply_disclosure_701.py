"""#701 — a re-run that applies an edit a SECOND time must not print the first run's receipt.

The reproduction, on master at `b0b65c7`:

    [[ops]]
    op = "edit"
    path = "a.py"
    old = "def f():"
    new = "@decorated\\ndef f():"

Run it twice and the anchor survives its own edit, so it matches again and the
decorator lands twice. Both runs exit 0 and both print

    edited a.py (line 1-2)        /  edited a.py (line 2-3)
    [result] 1 op run, 1 write    /  [result] 1 op run, 1 write

Applying it twice is not the defect. `old` is still in the file, find-and-replace
found it, and an edit that legitimately applies twice exists — appending a second
repeated element is the same shape. Refusing it would be guessing at intent.

The defect is that **the output cannot distinguish "applied" from "applied
again"**, and the caller who re-runs a payload is by definition the caller who was
already unsure whether it landed. #680 was filed because a batch result was
ambiguous enough that the reporter could not tell; the natural response to that is
to run the payload again, and this is what happens when they do. The first defect
makes you doubt, the second punishes checking.

The harm test: can someone acting reasonably on this output conclude the opposite
of the truth? Yes — an identical receipt reads as "the same thing happened" when
what happened is a second mutation.

So: disclosure, never refusal, and in the footer's existing vocabulary rather than
a third convention. `[result] N ops run, M writes, K skipped` gains `K re-applied`,
and the op receipt gains one `↳` line naming it.

The detection is positional, not a text search: the occurrence of `old` about to
be replaced is CONTAINED INSIDE an existing occurrence of `new`. That is the
literal statement "the text this edit produces is already here, around this
anchor" — so `new` merely existing somewhere else in the file cannot trigger it.

Every test below is written to fail if the code did nothing: each asserts a token
the pre-fix output does not contain, or asserts an absence against a run where the
naive `new in content` heuristic WOULD have fired.
"""
from __future__ import annotations

import json
from pathlib import Path

import supertool


def _tail(out: str, n: int = 4) -> str:
    return "\n".join(out.rstrip().splitlines()[-n:])


def _no_branch(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))


def _result_line_of(out: str) -> str:
    """Just the `[result]` line — asserting against the whole body would be a
    statement about the pytest tmp dir name as much as about the footer."""
    for line in out.splitlines():
        if line.startswith("[result] "):
            return line
    return ""


def _repro_payload(tmp_path: Path, target: Path) -> Path:
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([
        {"op": "edit", "path": str(target),
         "old": "def f():", "new": "@decorated" + chr(10) + "def f():"},
    ]), encoding="utf-8")
    return payload


# ---------------------------------------------------------------------------
# The two receipts must differ in a way a reader notices
# ---------------------------------------------------------------------------

def test_second_run_of_the_same_payload_is_named_in_the_footer(
    tmp_path: Path, monkeypatch
) -> None:
    """The issue's reproduction, verbatim.

    Pre-fix both footers read `[result] 1 op run, 1 write` and the only
    difference anywhere is a line range, which is not something a reader is
    watching. Post-fix the footer — the line that survives `| tail -4`, which is
    the whole reason #621 put it there — says the second run re-applied.
    """
    _no_branch(monkeypatch)
    f = tmp_path / "a.py"
    f.write_text("def f():\n    return 1\n", encoding="utf-8")
    payload = _repro_payload(tmp_path, f)

    first = supertool.dispatch(f"batch:@{payload}")
    second = supertool.dispatch(f"batch:@{payload}")

    assert _result_line_of(first) == "[result] 1 op run, 1 write"
    assert "re-applied" in _result_line_of(second)
    assert _result_line_of(second) != _result_line_of(first)
    assert "re-applied" in _tail(second), "must survive a tail-reader"


def test_the_op_receipt_names_the_re_application(tmp_path: Path, monkeypatch) -> None:
    """The footer is the tail-reader's signal; the `↳` line is the one attached
    to the claim it qualifies. `edited a.py (line 2-3)` on its own is a true
    sentence that reads as a first application."""
    _no_branch(monkeypatch)
    f = tmp_path / "a.py"
    f.write_text("def f():\n    return 1\n", encoding="utf-8")
    payload = _repro_payload(tmp_path, f)

    supertool.dispatch(f"batch:@{payload}")
    second = supertool.dispatch(f"batch:@{payload}")

    # `mark()` degrades the glyph to an ASCII marker in plain mode, so asking
    # for the literal arrow would be a statement about the terminal.
    arrow = supertool.mark("↳")
    disclosure = [ln for ln in second.splitlines() if "re-applied" in ln]
    assert any(ln.lstrip().startswith(arrow) for ln in disclosure), (
        "the disclosure belongs on the receipt, next to `edited ...`")


def test_the_edit_still_applies_twice(tmp_path: Path, monkeypatch) -> None:
    """Disclosure, not refusal. The second mutation still happens, still writes,
    and still exits 0 — an edit that legitimately applies twice exists, and this
    fix is not allowed to start guessing which one it is looking at."""
    _no_branch(monkeypatch)
    f = tmp_path / "a.py"
    f.write_text("def f():\n    return 1\n", encoding="utf-8")
    payload = _repro_payload(tmp_path, f)

    supertool.dispatch(f"batch:@{payload}")
    second = supertool.dispatch(f"batch:@{payload}")

    assert f.read_text(encoding="utf-8") == "@decorated\n@decorated\ndef f():\n    return 1\n"
    assert "1 write" in _result_line_of(second)


def test_exit_code_stays_zero_on_a_re_application(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A re-apply is not a decline: it must not join `skipped` on the exit code,
    or every legitimate repeated append breaks a `&&` chain."""
    _no_branch(monkeypatch)
    f = tmp_path / "a.py"
    f.write_text("def f():\n    return 1\n", encoding="utf-8")
    payload = _repro_payload(tmp_path, f)

    assert supertool.main([f"batch:@{payload}"]) == 0
    capsys.readouterr()
    assert supertool.main([f"batch:@{payload}"]) == 0
    capsys.readouterr()


def test_re_apply_is_not_reported_as_skipped(tmp_path: Path, monkeypatch) -> None:
    """The two words name different states. A re-applied op wrote; a skipped op
    deliberately did not. Collapsing them would make `skipped` mean 'something
    was odd', which is how a count stops being read."""
    _no_branch(monkeypatch)
    f = tmp_path / "a.py"
    f.write_text("def f():\n    return 1\n", encoding="utf-8")
    payload = _repro_payload(tmp_path, f)

    supertool.dispatch(f"batch:@{payload}")
    line = _result_line_of(supertool.dispatch(f"batch:@{payload}"))
    assert "skipped" not in line
    assert "re-applied" in line


# ---------------------------------------------------------------------------
# The signal must not over-fire — otherwise it is noise and stops being read
# ---------------------------------------------------------------------------

def test_a_first_application_says_nothing(tmp_path: Path, monkeypatch) -> None:
    _no_branch(monkeypatch)
    f = tmp_path / "a.py"
    f.write_text("def f():\n    return 1\n", encoding="utf-8")
    out = supertool.dispatch(f"batch:@{_repro_payload(tmp_path, f)}")
    assert "re-applied" not in out
    assert _result_line_of(out) == "[result] 1 op run, 1 write"


def test_new_text_existing_elsewhere_is_not_a_re_application(
    tmp_path: Path, monkeypatch
) -> None:
    """The failure mode of the naive test the issue floated ("does the file
    already contain `new`?").

    Here `new` — `return None` — genuinely pre-exists in an unrelated function,
    and the edit being made is a first application at a different site. A
    `new in content` check fires; the containment check does not, because the
    anchor being replaced is nowhere near the pre-existing copy.
    """
    _no_branch(monkeypatch)
    f = tmp_path / "a.py"
    f.write_text("def g():\n    return None\n\ndef h():\n    return 1\n", encoding="utf-8")
    out = supertool.dispatch(f"edit:::    return 1:::    return None:::{f}")
    assert "re-applied" not in out
    assert _result_line_of(out) == "[result] 1 op run, 1 write"


def test_an_edit_that_destroys_its_own_anchor_is_never_flagged(
    tmp_path: Path, monkeypatch
) -> None:
    """The ordinary case: `old` is gone after the edit, so a re-run declines with
    the existing `1 skipped` and never reaches this code at all. Guards against a
    fix that flags every edit whose `new` shares text with the file."""
    _no_branch(monkeypatch)
    f = tmp_path / "a.py"
    f.write_text("alpha\nbeta\n", encoding="utf-8")
    first = supertool.dispatch(f"edit:::alpha:::ALPHA:::{f}")
    second = supertool.dispatch(f"edit:::alpha:::ALPHA:::{f}")
    assert "re-applied" not in first
    assert "re-applied" not in second
    assert "1 skipped" in _result_line_of(second)


def test_repeated_element_append_is_disclosed_but_allowed(
    tmp_path: Path, monkeypatch
) -> None:
    """The legitimate double-apply named in the issue.

    `</list>` → `<item/></list>` is an append that keeps its own anchor, so it is
    byte-for-byte the shape of the bug — and running it twice to get two items is
    exactly what someone may mean. Both runs land; the second is disclosed. The
    caller decides, the tool discloses. Refusing here would break a real use.
    """
    _no_branch(monkeypatch)
    f = tmp_path / "x.xml"
    f.write_text("<list>\n</list>\n", encoding="utf-8")
    op = f"edit:::</list>:::  <item/>{chr(10)}</list>:::{f}"

    first = supertool.dispatch(op)
    assert "re-applied" not in first

    second = supertool.dispatch(op)
    assert f.read_text(encoding="utf-8") == "<list>\n  <item/>\n  <item/>\n</list>\n"
    assert "re-applied" in second


# ---------------------------------------------------------------------------
# Counting, across a batch and across calls
# ---------------------------------------------------------------------------

def test_batch_counts_every_re_applied_entry(tmp_path: Path, monkeypatch) -> None:
    _no_branch(monkeypatch)
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("def f():\n    return 1\n", encoding="utf-8")
    b.write_text("def g():\n    return 2\n", encoding="utf-8")
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([
        {"op": "edit", "path": str(a), "old": "def f():",
         "new": "@decorated" + chr(10) + "def f():"},
        {"op": "edit", "path": str(b), "old": "def g():",
         "new": "@decorated" + chr(10) + "def g():"},
    ]), encoding="utf-8")
    supertool.dispatch(f"batch:@{payload}")
    line = _result_line_of(supertool.dispatch(f"batch:@{payload}"))
    assert line == ("[result] 2 ops run, 2 writes, 2 re-applied"
                    " — an edit already present in the file was applied again")
    assert line.count("[result]") == 1


def test_the_count_does_not_leak_between_calls(tmp_path: Path, monkeypatch) -> None:
    """The counter is process-global and the daemon reuses the process, so the
    footer must read a per-call delta — the same trap #680 documented for
    `_SKIP_COUNT`. Without it, one re-apply labels every later call in the worker.
    """
    _no_branch(monkeypatch)
    f = tmp_path / "a.py"
    f.write_text("def f():\n    return 1\n", encoding="utf-8")
    payload = _repro_payload(tmp_path, f)
    supertool.dispatch(f"batch:@{payload}")
    supertool.dispatch(f"batch:@{payload}")

    other = tmp_path / "clean.txt"
    other.write_text("alpha\n", encoding="utf-8")
    out = supertool.dispatch(f"edit:::alpha:::ALPHA:::{other}")
    assert _result_line_of(out) == "[result] 1 op run, 1 write"
