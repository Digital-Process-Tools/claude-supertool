"""A created file is unlinked when a rollback validator fails (#1088).

The payload route's headline guarantee -- "validators run post-edit and roll
back on a syntax failure" -- is the stated reason this repo tells every agent
to route writes through supertool instead of the harness Edit tool. It held on
`edit`, and on `paste` over an existing file. It did not hold on create: the
receipt printed the red mark and the file stayed.

Measured cause: the rollback loop was gated on `pre_content is not None`, and a
file that did not exist has no prior bytes, so the loop never ran. The gate was
doing double duty -- "do we have something to restore" and "did this op create
the path" -- and those are different questions with different answers.

The correct null state for a created file is unlink, which is a delete, so the
code must be able to tell a path the call brought into being from a path that
existed and was overwritten. It can: `_pre_existed` is sampled before the op.
Where it cannot -- an existing file whose bytes could not be read -- it says so
instead of guessing, because unlinking there would destroy work.
"""
from pathlib import Path

import supertool

NL = chr(10)
Q3 = chr(39) * 3
BROKEN = "def f(:" + NL + "    pass" + NL


def _payload(tmp_path: Path, body: str) -> str:
    p = tmp_path / "p.toml"
    p.write_text(body, encoding="utf-8")
    return "@" + str(p)


def _toml_path(target: Path) -> str:
    return chr(34) + str(target).replace(chr(92), chr(92) * 2) + chr(34)


def _paste(tmp_path: Path, target: Path, content: str) -> str:
    body = (
        "path = " + _toml_path(target) + NL
        + "content = " + Q3 + content + Q3 + NL
    )
    return supertool.dispatch("paste:" + _payload(tmp_path, body))


def test_a_created_file_that_fails_py_syntax_is_removed(tmp_path: Path) -> None:
    """The issue, asserted against the filesystem rather than the receipt."""
    target = tmp_path / "new_broken.py"
    out = _paste(tmp_path, target, BROKEN)
    assert not target.exists(), "the failed create left the file behind:" + NL + out


def test_the_receipt_retracts_the_created_line(tmp_path: Path) -> None:
    """`created <path>` above a red validator row is the half that misleads. The
    retraction has to quote it back, so a filtered read that caught the claim
    catches the undo -- and it must say created, not edited, because a reader
    deciding what to do next needs to know the path does not exist."""
    target = tmp_path / "new_broken.py"
    out = _paste(tmp_path, target, BROKEN)
    assert "rolled back" in out, out
    assert "NOT created" in out, out
    assert "0 writes" in out, out


def test_a_created_file_that_passes_is_left_alone(tmp_path: Path) -> None:
    """The boundary. A rollback that fired on a clean create would be worse than
    the bug it replaced."""
    target = tmp_path / "new_ok.py"
    out = _paste(tmp_path, target, "x = 1" + NL)
    assert target.exists(), out
    assert target.read_text(encoding="utf-8").startswith("x = 1")
    assert "rolled back" not in out, out


def test_paste_over_an_existing_file_still_restores_rather_than_unlinks(
    tmp_path: Path,
) -> None:
    """The regression this fix could most easily cause. An overwrite has prior
    bytes; unlinking there would turn a rollback into a delete."""
    target = tmp_path / "existing.py"
    target.write_text("y = 2" + NL, encoding="utf-8")
    out = _paste(tmp_path, target, BROKEN)
    assert target.exists(), "the rollback deleted a pre-existing file:" + NL + out
    assert target.read_text(encoding="utf-8") == "y = 2" + NL


def test_a_failing_formatter_also_removes_a_created_file(
    tmp_path: Path, monkeypatch
) -> None:
    """The formatter loop had the identical gap, and it runs FIRST.

    Two things at once, because they are one code path. A formatter with
    `rollback_on_fail` that fails on a file this op created must remove it, not
    leave it behind on the "no prior bytes" reasoning. And having removed it,
    the validator loop that follows must not try to undo the same write again:
    restoring bytes twice was idempotent, but a second `os.unlink` of a path the
    first one removed raises `FileNotFoundError`, lands in the `OSError` arm,
    and prints `[ROLLBACK FAILED]` under a rollback that in fact succeeded.

    So the assertion is exactly one retraction and no failure line -- the
    invariant being "at most one undo per op", however the first one was
    reached.
    """
    monkeypatch.setattr(
        supertool, "_applicable_formatters",
        lambda op, path: {"fake-fmt": {"rollback_on_fail": True}})
    monkeypatch.setattr(
        supertool, "_formatters_run_batch",
        lambda applicable, path: [{"name": "fake-fmt", "ok": False}])
    target = tmp_path / "fmt_created.py"
    out = _paste(tmp_path, target, BROKEN)
    assert not target.exists(), "the formatter rollback left the file:" + NL + out
    # The bracketed marker, not the bare words: `[result]` also prints
    # `1 rolled back`, and counting that would make this assertion about the
    # footer rather than about how many undos were attempted.
    assert out.count("[rolled back]") == 1, "more than one undo:" + NL + out
    assert "ROLLBACK FAILED" not in out, out
    # And the validator that follows says it could not look, rather than
    # reporting a clean file or a second failure.
    assert "skipped" in out, out


def test_the_three_rollback_states_are_told_apart_by_provenance() -> None:
    """The decision this issue turns on, asserted where it is made.

    Three states, not two. `restore` needs prior bytes. `unlink` needs proof the
    call created the path -- anything less makes a rollback into a delete of work
    that was there before. Everything else is `refuse`, which has to be a state
    the caller is told about rather than a branch that falls through to silence.
    """
    assert supertool._rollback_action(True, b"prior") == "restore"
    assert supertool._rollback_action(False, None) == "unlink"
    assert supertool._rollback_action(True, None) == "refuse"


def test_an_unreadable_baseline_never_unlinks() -> None:
    """The one arm that could destroy work. A file that existed and whose bytes
    could not be captured must not be removed on a validator failure -- that
    trades a misreport for a deletion, which this repo rules out by name."""
    assert supertool._rollback_action(True, None) != "unlink"
