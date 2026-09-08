r"""#1712 — `grep` cuts a matched line at the char cap with no way to ask for
the whole line in the same call. The truncation was always honest (it names
the exact `read:PATH:LINE-LINE` remedy), but that remedy costs a second
round-trip. `full` is a new trailing colon flag, order-independent with the
existing `count` / `no-auto-read` flags, that suppresses the per-line cap for
one call without touching the default for every call that doesn't ask for it.
"""
from __future__ import annotations

from pathlib import Path

import supertool


def _long_line_file(tmp_path: Path, needle: str = "NEEDLE") -> Path:
    # 200 repeats -> ~1030 chars: over the 500-char per-line cap (#363) but
    # well under the 16000-byte whole-response cap (#241) that grep_around's
    # context mode applies separately -- the two caps are unrelated, and a
    # fixture big enough to trip #241 too would make `full`'s per-line-cap
    # promise untestable in context mode (it composes with #363, not #241).
    f = tmp_path / "Big.php"
    f.write_text("<?php\n/** @extends " + ("Foo, " * 200) + f"{needle} */\n")
    return f


def _short_line_file(tmp_path: Path, needle: str = "needle") -> Path:
    f = tmp_path / "Small.php"
    f.write_text(f"<?php\n${needle} = 'here';\n")
    return f


# ---------------------------------------------------------------------------
# Default (no flag) behaviour is unchanged — the "must fire" control.
# ---------------------------------------------------------------------------

def test_default_still_truncates_the_long_line(tmp_path: Path) -> None:
    f = _long_line_file(tmp_path)
    out = supertool.op_grep("NEEDLE", str(f), limit=5, no_auto_read=True)
    assert "…" in out, out[:300]
    assert "chars cut" not in out  # sanity: this repo's note text, checked below
    assert "cut at" in out, out[:300]


def test_default_via_dispatch_still_truncates(tmp_path: Path) -> None:
    f = _long_line_file(tmp_path)
    out = supertool.dispatch(f"grep:NEEDLE:{f}:5:0:no-auto-read")
    assert "…" in out, out[:300]
    assert "cut at" in out, out[:300]


# ---------------------------------------------------------------------------
# `full` suppresses the cut, for one call.
# ---------------------------------------------------------------------------

def test_full_flag_returns_the_whole_line_direct(tmp_path: Path) -> None:
    f = _long_line_file(tmp_path)
    out = supertool.op_grep("NEEDLE", str(f), limit=5, no_auto_read=True,
                            full=True)
    assert "…" not in out, out[:300]
    assert "NEEDLE */" in out


def test_full_flag_returns_the_whole_line_via_dispatch(tmp_path: Path) -> None:
    f = _long_line_file(tmp_path)
    out = supertool.dispatch(f"grep:NEEDLE:{f}:5:0:no-auto-read:full")
    assert "…" not in out, out[:300]
    assert "NEEDLE */" in out


def test_full_flag_is_order_independent_with_no_auto_read(tmp_path: Path) -> None:
    f = _long_line_file(tmp_path)
    out = supertool.dispatch(f"grep:NEEDLE:{f}:5:0:full:no-auto-read")
    assert "…" not in out, out[:300]
    assert "NEEDLE */" in out


def test_full_flag_composes_with_context_mode(tmp_path: Path) -> None:
    f = _long_line_file(tmp_path)
    out = supertool.dispatch(f"grep:NEEDLE:{f}:5:2:no-auto-read:full")
    assert "…" not in out, out[:300]
    assert "NEEDLE */" in out


def test_default_context_mode_still_truncates(tmp_path: Path) -> None:
    f = _long_line_file(tmp_path)
    out = supertool.dispatch(f"grep:NEEDLE:{f}:5:2:no-auto-read")
    assert "…" in out, out[:300]


# ---------------------------------------------------------------------------
# The disclosure note changes shape rather than reusing "cut" wording.
# ---------------------------------------------------------------------------

def test_full_flag_note_does_not_claim_a_cut(tmp_path: Path) -> None:
    f = _long_line_file(tmp_path)
    out = supertool.op_grep("NEEDLE", str(f), limit=5, no_auto_read=True,
                            full=True)
    assert "cut at" not in out, (
        "full mode returned the line uncut, so a note using 'cut' wording "
        "would claim something that did not happen: " + repr(out[:400]))


def test_full_flag_says_something_was_returned_uncut(tmp_path: Path) -> None:
    f = _long_line_file(tmp_path)
    out = supertool.op_grep("NEEDLE", str(f), limit=5, no_auto_read=True,
                            full=True)
    assert "note:" in out and "full" in out.split("\n")[0:3].__str__(), (
        "when full actually suppressed a truncation, that is worth saying "
        "explicitly rather than rendering identically to a call with "
        "nothing long enough to matter: " + repr(out[:400]))


def test_full_flag_with_nothing_long_emits_no_stray_note(tmp_path: Path) -> None:
    """`full` requested but every line already fits under the cap: nothing was
    suppressed, so the note must not claim otherwise (would-be-clean output
    reads as clean, not as a fabricated disclosure)."""
    f = _short_line_file(tmp_path)
    out = supertool.op_grep("needle", str(f), limit=5, no_auto_read=True,
                            full=True)
    assert "cut" not in out, (
        "nothing was long enough to be cut, so no cut-flavoured note should "
        "appear at all: " + repr(out[:400]))


# ---------------------------------------------------------------------------
# Payload route (@file/@-) accepts `full` too — same field `read` already has.
# ---------------------------------------------------------------------------

def test_full_flag_via_payload(tmp_path: Path) -> None:
    f = _long_line_file(tmp_path)
    payload = tmp_path / "p.toml"
    payload.write_text(
        'pattern = "NEEDLE"\npath = "' + f.as_posix() + '"\n'
        'no_auto_read = true\nfull = true\n')
    out = supertool.dispatch(f"grep:@{payload}")
    assert "…" not in out, out[:300]
    assert "NEEDLE */" in out


def test_payload_without_full_still_truncates(tmp_path: Path) -> None:
    f = _long_line_file(tmp_path)
    payload = tmp_path / "p.toml"
    payload.write_text(
        'pattern = "NEEDLE"\npath = "' + f.as_posix() + '"\n'
        'no_auto_read = true\n')
    out = supertool.dispatch(f"grep:@{payload}")
    assert "…" in out, out[:300]
