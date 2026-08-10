"""One refused op in a batch, and the two questions it left unanswered (#1234).

Filed as "a rejected op suppressed the whole call's output". Reproduced on
master at 0.32.0 and that is *not* what happens -- every op ran and every op
rendered:

    $ supertool 'version' 'grep:LIMIT:_supertool.py:0' 'wc:_supertool.py'
    supertool 0.32.0
    --- grep:LIMIT:_supertool.py:0 ---
    ERROR: grep LIMIT 0 is not "unlimited" here ...
    --- wc:_supertool.py ---
    20502 115553 942894 _supertool.py
    EXIT=1

What is actually missing is the reconciliation. The exit code is one bit for
the whole call, so a caller -- a shell `&&`, a hook, an agent harness that
reframes any non-zero command as an error block -- reads "the call failed"
over output in which two of three answers are complete. Nothing in the render
says how many ops ran, so the caller cannot tell a batch that half-worked from
a batch that did not run.

The fix is a tally line, not a change of behaviour: the batch was never
all-or-nothing and must not start being one.

The second half of #1234 is the range spelling: `read:` takes `PATH:1-30`,
`between:` takes neither -- and `between:PATH:1:30` already teaches the
redirect while `between:PATH:1-30`, the spelling `read:` accepts, answered
`ERROR: file not found: 1-30`.
"""
from __future__ import annotations

import supertool
from _changelog_findable import assert_change_is_findable


# ---------------------------------------------------------------------------
# The tally
# ---------------------------------------------------------------------------

def test_a_refusal_does_not_suppress_its_siblings(tmp_path, monkeypatch, capsys) -> None:
    """The premise as filed. Pinned so a future fix cannot make it true."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.txt").write_text("LIMIT" + chr(10), encoding="utf-8")

    rc = supertool.main(["version", "grep:LIMIT:x.txt:0", "wc:x.txt"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "supertool " in out                 # op 1 answered
    assert "LIMIT 0 is not" in out             # op 2 refused, out loud
    assert "x.txt" in out.split("--- wc:x.txt ---")[-1]   # op 3 answered


def test_a_mixed_batch_states_how_many_ops_ran(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.txt").write_text("LIMIT" + chr(10), encoding="utf-8")

    rc = supertool.main(["version", "grep:LIMIT:x.txt:0", "wc:x.txt"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "[batch] 3 ops ran" in out, out
    assert "2 ok, 1 refused" in out, out


def test_an_all_refused_batch_claims_no_surviving_answer(
    tmp_path, monkeypatch, capsys
) -> None:
    """With nothing left standing, "the other answers are complete" is a lie."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.txt").write_text("LIMIT" + chr(10), encoding="utf-8")

    rc = supertool.main(["grep:LIMIT:x.txt:0", "grep:LIMIT:x.txt:-1"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "[batch] 2 ops ran — all 2 refused." in out, out
    assert "complete" not in out, out


def test_both_kinds_of_failure_at_once_withholds_the_completeness_claim(
    tmp_path, monkeypatch, capsys
) -> None:
    """A refusal and a skipped write in the same call.

    `refused` counts first-line ERROR/FAIL markers, so it does not see
    `replace`'s `(0 occurrences ...)` or an edit a validator reverted -- those
    reach `any_failure` through `_SKIP_COUNT` / `_ROLLBACK_COUNT` instead. An
    op can therefore sit outside `refused` and still not have landed, and the
    two-branch tally called it "ok" and asserted "the other 1 answers above are
    complete" over a write whose own receipt one line up says nothing changed
    on disk. Found in review of this change; it is the same shape as the
    defect the change exists to fix, one layer in.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.txt").write_text("LIMIT" + chr(10), encoding="utf-8")

    rc = supertool.main(["grep:LIMIT:x.txt:0", "replace:::nomatch:::y:::x.txt"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "[batch] 2 ops ran — 1 refused" in out, out
    assert "complete" not in out, out
    assert "1 ok" not in out, out
    assert "read the per-op receipts" in out, out


def test_a_clean_batch_says_nothing(tmp_path, monkeypatch, capsys) -> None:
    """The line discloses a mismatch. With nothing to reconcile it is noise."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.txt").write_text("LIMIT" + chr(10), encoding="utf-8")

    rc = supertool.main(["version", "wc:x.txt"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "[batch]" not in out


def test_a_single_op_call_says_nothing(tmp_path, monkeypatch, capsys) -> None:
    """One op and exit 1 is already unambiguous -- there is no sibling to lose."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.txt").write_text("LIMIT" + chr(10), encoding="utf-8")

    rc = supertool.main(["grep:LIMIT:x.txt:0"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "[batch]" not in out


def test_exit_one_from_a_non_op_cause_does_not_claim_a_refusal(
    tmp_path, monkeypatch, capsys
) -> None:
    """`any_failure` also fires on a skipped write, a rollback or an unrun
    validator.

    `replace` finding nothing is the documented case: its body is
    `(0 occurrences of 'x' found)`, which `_body_indicates_failure` does not
    read as a failure, while `_SKIP_COUNT` does. None of those is an op
    refusing, so the line must not say `0 refused` beside an exit 1 and leave
    the reader hunting for the op that did not fail.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.txt").write_text("LIMIT" + chr(10), encoding="utf-8")

    rc = supertool.main(["version", "replace:::nomatch:::y:::x.txt"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "[batch] 2 ops ran" in out, out
    assert "refused" not in out, out
    assert "not an op" in out, out


# ---------------------------------------------------------------------------
# The range spelling
# ---------------------------------------------------------------------------

def test_between_teaches_the_dash_range_too(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.py").write_text("a" + chr(10), encoding="utf-8")

    out = supertool.dispatch("between:x.py:1-30")

    assert "does not take line ranges" in out, out
    assert "read:x.py:1-30" in out, out
    assert "file not found" not in out, out


def test_between_dash_range_reversed_is_normalised(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.py").write_text("a" + chr(10), encoding="utf-8")

    out = supertool.dispatch("between:x.py:30-1")

    assert "read:x.py:1-30" in out, out


def test_a_dashed_filename_is_still_a_symbol_lookup(tmp_path, monkeypatch) -> None:
    """`1-30` must not be read as a range when it is a real path on disk.

    Same guard the `:START:END` spelling already carries -- a file called
    `1-30` is a range nobody asked for.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "1-30").write_text("a" + chr(10), encoding="utf-8")
    (tmp_path / "x.py").write_text("a" + chr(10), encoding="utf-8")

    out = supertool.dispatch("between:x.py:1-30")

    assert "does not take line ranges" not in out, out


def test_a_dash_range_outside_cwd_is_not_an_existence_oracle(tmp_path, monkeypatch) -> None:
    """#1142, for the new spelling.

    The hint stats parts[1], a slot `_PATH_ARG_POSITIONS["between"]` does not
    cover, so without the containment call the pair of answers below is an
    existence oracle for anything the process can stat. The colon spelling was
    closed in #1142; the dash spelling arrives at the same line and must
    refuse identically.

    conftest sets SUPERTOOL_ALLOW_OUTSIDE_CWD=1 so tmp_path fixtures work at
    all -- without putting the boundary back this test asserts nothing.
    """
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    present = tmp_path / "outside_a.txt"
    present.write_text("one" + chr(10), encoding="utf-8")
    box = tmp_path / "box"
    box.mkdir()
    monkeypatch.chdir(box)

    here = supertool.dispatch("between:../outside_a.txt:1-30")
    gone = supertool.dispatch("between:../outside_gone.txt:1-30")

    # Byte equality is the wrong bar -- the two messages differ by the path the
    # caller typed, which the caller already knows. What must not differ is
    # which *branch* answered.
    assert "does not take line ranges" not in here, here
    assert "escapes cwd" in here, here
    assert "escapes cwd" in gone, gone


def test_a_changelog_fragment_exists() -> None:
    assert_change_is_findable(1234)
