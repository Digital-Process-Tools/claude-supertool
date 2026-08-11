r"""#1314 and #1328 — both are `grep` answering a question the caller did not ask.

* #1314 — the #1120 refusal only catches an *empty* alternation branch. A branch
  that is trivially saturating (`^`, `$`, `.*`) matches every line just as hard
  and sailed straight through: `grep:^\|def op_:_supertool.py:5` reported five
  results out of "1000+ matches" for a pattern that matches every line of every
  file. The rewrite notice printed above the list; nothing said the pattern had
  stopped being a search.
* #1328 — `grep` has one shape for "show me some" and "find every one", and its
  default limit silently answers the second with the first. `all` makes the
  intent explicit at the call site, which is where the reader's mistake happens.
"""

from __future__ import annotations

from pathlib import Path

import supertool


def _lines(tmp_path: Path, name: str, count: int) -> Path:
    f = tmp_path / name
    f.write_text("".join(f"line{i}\n" for i in range(1, count + 1)))
    return f


# ---------------------------------------------------------------------------
# #1314 — a branch that matches every line is refused, empty or not
# ---------------------------------------------------------------------------

def test_caret_branch_from_the_bre_rewrite_is_refused(tmp_path: Path) -> None:
    r"""The filed call. `^\|def op_` rewrites to `^|def op_`, whose `^` branch
    matches at position 0 of every line — 1000+ "matches" that mean nothing."""
    f = tmp_path / "code.py"
    f.write_text("alpha\ndef op_x():\ngamma\n")
    out = supertool.op_grep(r"^\|def op_", str(f), limit=5)
    assert out.startswith("ERROR:"), (
        "a `^` alternation branch matches every line exactly as an empty one "
        "does, and its report is indistinguishable from a real hit list: "
        + repr(out))
    assert "alpha" not in out, (
        "refusing means returning no results, not results plus a note: "
        + repr(out))


def test_the_refusal_names_the_saturating_branch(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("alpha\n")
    out = supertool.op_grep(r"^\|def op_", str(f), limit=5)
    assert "`^`" in out, (
        "which branch saturates is the whole diagnosis — without it the caller "
        "cannot tell which half of their pattern to fix: " + repr(out))


def test_dollar_branch_is_refused(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("alpha\nbeta\n")
    out = supertool.op_grep("alpha|$", str(f), limit=5)
    assert out.startswith("ERROR:"), out


def test_dot_star_branch_is_refused(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("alpha\nbeta\n")
    out = supertool.op_grep(".*|alpha", str(f), limit=5)
    assert out.startswith("ERROR:"), out


def test_star_quantified_branch_is_refused(tmp_path: Path) -> None:
    """`z*` matches the empty string, so it matches at position 0 of every
    line — the same saturation wearing a quantifier."""
    f = tmp_path / "code.py"
    f.write_text("alpha\nbeta\n")
    out = supertool.op_grep("z*|alpha", str(f), limit=5)
    assert out.startswith("ERROR:"), out


def test_around_refuses_the_caret_branch_too(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("alpha\ndef op_x():\n")
    out = supertool.op_around(r"^\|def op_", str(f), 2)
    assert out.startswith("ERROR:"), out


def test_grep_around_refuses_it_too(tmp_path: Path) -> None:
    """`grep_around` is `op_grep` with context, so it inherits the refusal."""
    f = tmp_path / "code.py"
    f.write_text("alpha\ndef op_x():\n")
    out = supertool.dispatch(f"grep_around:^|def op_:{f}:2:5")
    assert "ERROR:" in out, out


# --- and no wider: patterns that do NOT match every line still run ----------

def test_blank_line_branch_is_not_saturating(tmp_path: Path) -> None:
    """`^$` matches the empty string but not `abc`, so `^$|alpha` is a real
    search — "blank lines or alpha" — and refusing it would remove the op."""
    f = tmp_path / "code.py"
    f.write_text("alpha\nbeta\n")
    out = supertool.op_grep("^$|alpha", str(f), limit=5, no_auto_read=True)
    assert not out.startswith("ERROR:"), out
    assert "alpha" in out, out


def test_ordinary_anchored_alternation_still_runs(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("def a():\nclass B:\nzzz\n")
    out = supertool.op_grep("^def |^class ", str(f), limit=5, no_auto_read=True)
    assert not out.startswith("ERROR:"), out
    assert "zzz" not in out, out


def test_caret_inside_a_group_is_not_saturating(tmp_path: Path) -> None:
    """Only a TOP-LEVEL branch saturates — `(^|,)alpha` is the standard
    idiom for "at line start or after a comma" and must keep working."""
    f = tmp_path / "code.py"
    f.write_text("alpha\nzzz\n")
    out = supertool.op_grep("(^|,)alpha", str(f), limit=5, no_auto_read=True)
    assert not out.startswith("ERROR:"), out
    assert "zzz" not in out, out


def test_a_saturating_pattern_with_no_alternation_is_untouched(
        tmp_path: Path) -> None:
    """A lone `.*` is a deliberate whole-file match, not a rewrite accident,
    and the refusal is scoped to alternation."""
    f = tmp_path / "code.py"
    f.write_text("alpha\n")
    out = supertool.op_grep(".*", str(f), limit=5, no_auto_read=True)
    assert not out.startswith("ERROR:"), out


# ---------------------------------------------------------------------------
# #1328 — `all` says "find every one" at the call site
# ---------------------------------------------------------------------------

def test_all_returns_every_match(tmp_path: Path) -> None:
    f = _lines(tmp_path, "many.txt", 40)
    out = supertool.dispatch(f"grep:line:{f}:all:no-auto-read")
    assert "line40" in out, (
        "`all` exists so a call-site sweep is not answered with the top N: "
        + repr(out))
    assert "(40 results" in out, out


def test_all_never_carries_a_truncation_marker(tmp_path: Path) -> None:
    """Both halves matter: before #1328 `all` fell through to the PATH slot and
    the resulting `path not found` ERROR also lacked the word TRUNCATED, so the
    absence of the marker on its own is not evidence of anything."""
    f = _lines(tmp_path, "many.txt", 40)
    out = supertool.dispatch(f"grep:line:{f}:all:no-auto-read")
    assert "(40 results" in out and "line40" in out, out
    assert "TRUNCATED" not in out, out


def test_all_outside_the_limit_slot_is_refused(tmp_path: Path) -> None:
    """`grep:PAT:PATH:LIMIT:CONTEXT:all` peels three trailing tokens and only
    two are read. Dropping the third silently would run the default under a
    token the caller believes removed the cap."""
    f = _lines(tmp_path, "many.txt", 40)
    out = supertool.dispatch(f"grep:line:{f}:5:2:all")
    assert "ERROR:" in out and "LIMIT" in out, out


def test_grep_around_names_its_own_slot_order_for_all(tmp_path: Path) -> None:
    """`grep_around` is PATTERN:PATH:N:LIMIT — context first, the opposite of
    `grep`. A caller copying `grep:PAT:PATH:all` lands `all` in the N slot and
    used to get `invalid literal for int() with base 10: 'all'`."""
    f = _lines(tmp_path, "many.txt", 40)
    out = supertool.dispatch(f"grep_around:line:{f}:all")
    assert "ERROR:" in out, out
    assert "invalid literal" not in out, (
        "a raw int() traceback message is not a diagnosis: " + repr(out))
    assert f"grep_around:line:{f}:N:all" in out or "N:all" in out, out


def test_payload_limit_all_is_case_sensitive(tmp_path: Path) -> None:
    """`all` is spelled one way in both routes. The colon CLI matches the token
    exactly, as `count` and `no-auto-read` do, so the payload must not quietly
    accept a spelling the CLI reads as a path."""
    f = _lines(tmp_path, "many.txt", 40)
    payload = tmp_path / "p.toml"
    payload.write_text(
        'pattern = "line"\npath = "' + f.as_posix() + '"\n'
        'limit = "All"\n')
    out = supertool.dispatch(f"grep:@{payload}")
    assert "ERROR:" in out and "limit" in out, out


def test_all_is_carried_on_the_count_line(tmp_path: Path) -> None:
    """The count line is the part that survives a pipe, so the completeness
    claim has to live there rather than in the caller's memory."""
    f = _lines(tmp_path, "many.txt", 40)
    out = supertool.dispatch(f"grep:line:{f}:all:no-auto-read")
    assert "limit all" in out, out


def test_default_limit_still_truncates(tmp_path: Path) -> None:
    """The other intent is untouched: without `all`, the cap and its marker
    are the same as before."""
    f = _lines(tmp_path, "many.txt", 40)
    out = supertool.dispatch(f"grep:line:{f}:no-auto-read")
    assert "TRUNCATED, 40 matches total" in out, out


def test_all_composes_with_context(tmp_path: Path) -> None:
    f = _lines(tmp_path, "many.txt", 40)
    out = supertool.dispatch(f"grep:line3:{f}:all:1")
    assert "limit all, context 1" in out, out
    assert "line39" in out, out


def test_all_in_the_context_slot_is_refused(tmp_path: Path) -> None:
    """`all` is a LIMIT, and a context of "everything" is a read, not a grep.
    Silently reading it as a limit would run a call nobody typed."""
    f = _lines(tmp_path, "many.txt", 40)
    out = supertool.dispatch(f"grep:line:{f}:5:all")
    assert "ERROR:" in out and "LIMIT" in out, out


def test_grep_around_takes_all_too(tmp_path: Path) -> None:
    f = _lines(tmp_path, "many.txt", 40)
    out = supertool.dispatch(f"grep_around:line:{f}:1:all")
    assert "ERROR:" not in out, out
    assert "limit all" in out, out


def test_payload_limit_all(tmp_path: Path) -> None:
    f = _lines(tmp_path, "many.txt", 40)
    payload = tmp_path / "p.toml"
    payload.write_text(
        'pattern = "line"\npath = "' + f.as_posix() + '"\n'
        'limit = "all"\nno_auto_read = true\n')
    out = supertool.dispatch(f"grep:@{payload}")
    assert "limit all" in out, out
    assert "line40" in out, out


def test_all_is_not_a_path(tmp_path: Path) -> None:
    """Before #1328 a non-numeric LIMIT fell through to the path slot, so
    `grep:PAT:PATH:all` searched a directory called `all`."""
    f = _lines(tmp_path, "many.txt", 40)
    out = supertool.dispatch(f"grep:line:{f}:all:no-auto-read")
    assert "not found" not in out, out
