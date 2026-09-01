"""#2122 -- an unknown op in a batch is refused before any member runs.

Reproduced verbatim on 0.52.0:

    $ ./supertool 'read:docs/compatibility.md' --offset 900 --limit 5
    --- read:docs/compatibility.md ---
    (1025 lines, 75571 bytes)   <-- the entire file
    --- --offset ---
    ERROR: unknown operation: --offset
    ...

Every positional argument is an op, so a mistyped option becomes a batch
member. The refusal was already correct -- four ERRORs, exit 1 -- what cost
was the ORDER: the unbounded read ran to completion as batch member 1, so
the diagnosis landed under ~270 lines of file content.

The fix validates every member's op name against the registry BEFORE
running any of them, and refuses the whole batch if one does not route --
mirroring the raw-command guard's own rule that a refusal covers the whole
call, never the part that named it. Only a NAME that cannot route at all
(a typo, a flag) is pre-validated this way; an op that names something real
but fails at runtime (a missing file, a bad argument) is not knowable before
it runs and must still run in place, which is what the must-not-fire test
below pins.
"""
from __future__ import annotations

import supertool


def test_unknown_op_in_a_batch_refuses_before_the_expensive_member_runs(
    tmp_path, monkeypatch, capsys
) -> None:
    """Must-fire: the read never happens -- its content is nowhere in output."""
    monkeypatch.chdir(tmp_path)
    big = tmp_path / "big.txt"
    big.write_text(
        "SENTINEL_EXPENSIVE_CONTENT_LINE\n" * 500, encoding="utf-8"
    )

    rc = supertool.main(["read:big.txt", "--offset", "900", "--limit", "5"])
    captured = capsys.readouterr()
    out, err = captured.out, captured.err

    assert rc == 1
    assert "SENTINEL_EXPENSIVE_CONTENT_LINE" not in out
    assert "SENTINEL_EXPENSIVE_CONTENT_LINE" not in err
    # Names the member(s) it could not route, before anything else ran.
    combined = out + err
    assert "--offset" in combined


def test_a_valid_batch_still_runs_every_member_in_order(
    tmp_path, monkeypatch, capsys
) -> None:
    """Must-not-fire: nothing here is an unroutable NAME -- 'grep' is a real
    op that merely fails at runtime on this argument, which is not knowable
    before it runs and must not be pre-refused.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.txt").write_text("LIMIT" + chr(10), encoding="utf-8")

    rc = supertool.main(["version", "grep:LIMIT:x.txt:0", "wc:x.txt"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "supertool " in out                       # op 1 answered
    assert "LIMIT 0 is not" in out                    # op 2 refused, out loud
    assert "x.txt" in out.split("--- wc:x.txt ---")[-1]   # op 3 answered


def test_a_flag_shaped_member_is_diagnosed_as_a_flag(
    tmp_path, monkeypatch, capsys
) -> None:
    """The cheap add-on from the same issue: a token starting with '-' is
    never a valid op name, so it is worth saying so rather than only
    listing every valid op.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "small.txt").write_text("x\n", encoding="utf-8")

    rc = supertool.main(["read:small.txt", "--offset", "0", "--limit", "1"])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert rc == 1
    assert "flag" in combined.lower()
    assert "positionally" in combined.lower()
