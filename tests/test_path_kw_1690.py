"""#1690 -- the read family disagrees about which slot the path argument
goes in: `read:PATH:...` takes it first, `grep:PATTERN:PATH` and
`around:PATTERN:PATH[:N]` take it second, and only a multi-op call surfaces
the disagreement (each op's own signature is internally consistent and
documented).

Direction 1 from the issue body -- "a path=/file= keyword accepted on all
four, so a caller who does not remember the order does not have to" -- is
implemented here for `grep`, `around` and `grep_around`, the three ops that
share the PATTERN:PATH order. It is purely additive: a call with no such
token must parse exactly as it did before (the "must not fire" half of every
case below), and `path=`/`file=` let the caller skip the ordering question
entirely regardless of where the pattern falls (the "must fire" half).

`between` and `read` are deliberately not touched here -- `read`'s path
already leads (no ordering ambiguity to close) and `between` has two modes
(symbol / re:START:END) whose own argument-position defect is #1711's, not
this issue's; folding it in here would widen this issue's diff into that
one's.
"""

from __future__ import annotations

from pathlib import Path

import supertool


def _hay(tmp_path: Path) -> Path:
    f = tmp_path / "code.py"
    f.write_text("alpha\nneedle here\nbeta\n", encoding="utf-8")
    return f


# --- grep -------------------------------------------------------------

def test_grep_accepts_path_kw_after_pattern(tmp_path: Path) -> None:
    f = _hay(tmp_path)
    out = supertool.dispatch(f"grep:needle:path={f}")
    assert "ERROR" not in out, out
    assert "needle here" in out, out


def test_grep_accepts_file_kw_alias(tmp_path: Path) -> None:
    """`file=` is accepted as an alias for `path=`. Not tested ahead of the
    pattern: `parts[1]` -- always the mandatory PATTERN slot for this op
    family -- is deliberately excluded from the keyword scan (self-review,
    #1711/#1690), so a legitimate pattern of literally `file=...` is never
    silently reinterpreted."""
    f = _hay(tmp_path)
    out = supertool.dispatch(f"grep:needle:file={f}")
    assert "ERROR" not in out, out
    assert "needle here" in out, out


def test_grep_pattern_literally_starting_with_path_equals_is_unaffected(
        tmp_path: Path) -> None:
    """Must-not-fire half of the parts[1]-exclusion above: a real pattern
    of `path=` (plausible in any codebase assigning that variable name)
    must not be swallowed as the keyword."""
    f = tmp_path / "config.py"
    f.write_text("path=/tmp/somewhere\n", encoding="utf-8")
    out = supertool.dispatch(f"grep:path=:{f}")
    assert "ERROR" not in out, out
    assert "path=/tmp/somewhere" in out, out


def _body(out: str) -> str:
    """Drop the echoed `--- op:args ---` header line, which necessarily
    differs between a path= call and its positional equivalent -- the args
    it echoes ARE the difference. Everything after it is what this test
    cares about."""
    return out.split("\n", 1)[1] if "\n" in out else out


def test_grep_without_path_kw_parses_exactly_as_before(tmp_path: Path) -> None:
    """Must-not-fire half: an ordinary two-token call is untouched."""
    f = _hay(tmp_path)
    with_kw = supertool.dispatch(f"grep:needle:path={f}")
    without_kw = supertool.dispatch(f"grep:needle:{f}")
    assert _body(with_kw) == _body(without_kw), (with_kw, without_kw)


def test_grep_two_path_kw_tokens_falls_back_to_positional(tmp_path: Path) -> None:
    """Two path= tokens is an ambiguous call, not a pick-one call -- it must
    be left for the ordinary positional parser to answer (and fail) rather
    than silently resolved."""
    f = _hay(tmp_path)
    out = supertool.dispatch(f"grep:needle:path=a:path={f}")
    # positional parsing treats 'path=a' and 'path={f}' as trailing tokens;
    # neither resolves to a real limit/context int, so this must not
    # silently answer as if only one path= had been given.
    assert "needle here" not in out or "ERROR" in out, out


# --- around -------------------------------------------------------------

def test_around_accepts_path_kw(tmp_path: Path) -> None:
    f = _hay(tmp_path)
    out = supertool.dispatch(f"around:needle:path={f}")
    assert "ERROR" not in out, out
    assert "needle here" in out, out


def test_around_without_path_kw_parses_exactly_as_before(tmp_path: Path) -> None:
    f = _hay(tmp_path)
    with_kw = supertool.dispatch(f"around:needle:path={f}:2")
    without_kw = supertool.dispatch(f"around:needle:{f}:2")
    assert _body(with_kw) == _body(without_kw), (with_kw, without_kw)


# --- grep_around -------------------------------------------------------------

def test_grep_around_accepts_path_kw(tmp_path: Path) -> None:
    f = _hay(tmp_path)
    out = supertool.dispatch(f"grep_around:needle:path={f}")
    assert "ERROR" not in out, out
    assert "needle here" in out, out


def test_grep_around_without_path_kw_parses_exactly_as_before(tmp_path: Path) -> None:
    f = _hay(tmp_path)
    with_kw = supertool.dispatch(f"grep_around:needle:path={f}:2")
    without_kw = supertool.dispatch(f"grep_around:needle:{f}:2")
    assert _body(with_kw) == _body(without_kw), (with_kw, without_kw)


# --- containment (self-review findings, #1690/#1711) ------------------

def test_grep_around_path_kw_cannot_escape_cwd() -> None:
    """The generic dispatch-level containment gate runs BEFORE this op's
    own `_extract_path_kw` call, against the raw `path=...` token (which
    never itself escapes cwd -- it is a string starting with 'path=', not
    an absolute path). Without a second gate on the extracted value,
    `grep_around:PAT:path=/etc/passwd` read straight through where
    `grep_around:PAT:/etc/passwd` was refused. Both must refuse alike."""
    import os
    prev = os.environ.pop("SUPERTOOL_ALLOW_OUTSIDE_CWD", None)
    try:
        positional = supertool.dispatch("grep_around:root:/etc/passwd")
        kw = supertool.dispatch("grep_around:root:path=/etc/passwd")
    finally:
        if prev is not None:
            os.environ["SUPERTOOL_ALLOW_OUTSIDE_CWD"] = prev
    assert "escapes cwd" in positional or "ERROR" in positional, positional
    assert "escapes cwd" in kw or "ERROR" in kw, kw
    assert "root:*:0:0" not in kw, kw


def test_swap_suggest_never_confirms_existence_outside_cwd(tmp_path: Path) -> None:
    """`_swap_suggest` stats the OTHER slot to decide whether to fire. That
    stat must never answer for a path outside the containment boundary --
    doing so would make the diagnostic itself an oracle for "does this
    absolute host path exist", independent of whether it fires."""
    import os
    prev = os.environ.pop("SUPERTOOL_ALLOW_OUTSIDE_CWD", None)
    try:
        out = supertool.dispatch("grep:/etc/passwd:definitely/does/not/exist.py")
    finally:
        if prev is not None:
            os.environ["SUPERTOOL_ALLOW_OUTSIDE_CWD"] = prev
    assert "Did you mean" not in out, out


# --- Windows drive letter (CI: pytest windows-latest, job #98096203755) ----

def test_split_arg_reassembles_drive_letter_after_kw_prefix() -> None:
    r"""`_split_arg` already reassembles a bare drive letter ('C' + '\x') into
    one token -- that's how `read:C:\Users\file.py` has always worked. The
    same reassembly must also fire when the drive letter sits after a
    `path=`/`file=` prefix instead of at the start of the piece; the naive
    split treats 'path=C' as a whole token that fails `_DRIVE_LETTER`'s
    single-letter check, so the next piece ('\\Users\\...') is left as its
    own token instead of being absorbed."""
    import _supertool as st
    parts = st._split_arg(r"grep:needle:path=C:\Users\dev\code.py")
    assert parts == ["grep", "needle", r"path=C:\Users\dev\code.py"], parts


def test_split_arg_still_reassembles_bare_drive_letter() -> None:
    """Must-not-fire half: the pre-existing bare-letter reassembly (no
    `key=` prefix) is unaffected by the new stripping step."""
    import _supertool as st
    parts = st._split_arg(r"read:C:\Users\file.py")
    assert parts == ["read", r"C:\Users\file.py"], parts
