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


def test_grep_accepts_file_kw_before_pattern(tmp_path: Path) -> None:
    f = _hay(tmp_path)
    # keyword ahead of the pattern -- order no longer matters
    out = supertool.dispatch(f"grep:file={f}:needle")
    assert "ERROR" not in out, out
    assert "needle here" in out, out


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
