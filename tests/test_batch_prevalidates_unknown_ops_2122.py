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

# ---------------------------------------------------------------------------
# Review findings on the first cut of this fix -- each of these failed against
# it and passes against the shipped one.
# ---------------------------------------------------------------------------

def test_the_flag_hint_never_names_an_unroutable_neighbour(
    tmp_path, monkeypatch, capsys
) -> None:
    """The first cut used `argv[i - 1]` blindly, so in this issue's own
    headline example the member before `--limit` was `900` -- itself
    unroutable -- and the hint read `900:PATH:...` one line under
    `unknown operation: 900`: a remedy contradicting the diagnosis directly
    above it. The hint must name the nearest earlier member that ROUTES.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "big.txt").write_text("x\n", encoding="utf-8")

    supertool.main(["read:big.txt", "--offset", "900", "--limit", "5"])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert "read:PATH:..." in combined
    assert "900:PATH:..." not in combined
    assert "5:PATH:..." not in combined


def test_a_refused_batch_keeps_the_help_a_single_op_typo_gets(
    tmp_path, monkeypatch, capsys
) -> None:
    """Refusing earlier must not buy the caller a THINNER diagnosis. A typo
    alone gets #614's `Did you mean` and the roster; the same typo inside a
    batch must still get both.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.txt").write_text("x\n", encoding="utf-8")

    supertool.main(["raed:x.txt", "wc:x.txt"])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert "Did you mean:" in combined
    assert "read" in combined
    assert "Valid operations:" in combined


def test_the_roster_is_printed_once_not_once_per_bad_member(
    tmp_path, monkeypatch, capsys
) -> None:
    """Four typos must not print the 40+-name roster four times -- spending,
    on a refusal, exactly the output this issue exists to save.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.txt").write_text("x\n", encoding="utf-8")

    supertool.main(["read:x.txt", "raed:x.txt", "wcc:x.txt", "grepp:x:x.txt"])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert combined.count("Valid operations:") == 1


def test_a_config_that_failed_to_parse_still_says_so_before_the_refusal(
    tmp_path, monkeypatch, capsys
) -> None:
    """The absence the tool produced, read as an absence in the world.

    A `.supertool.json` that fails to parse drops every custom op it
    declared. Refusing the batch BEFORE the loader warnings are printed
    would answer `unknown operation: mycustomop` about an op the caller
    really did define, with nothing on screen saying the config never
    loaded. The refusal must sit after those warnings, not before.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".supertool.json").write_text("{not json at all", encoding="utf-8")
    (tmp_path / "x.txt").write_text("x\n", encoding="utf-8")
    # The config cache is process-global; force a re-read in this cwd.
    monkeypatch.setattr(supertool, "_CONFIG", None, raising=False)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False, raising=False)

    supertool.main(["mycustomop:x", "read:x.txt"])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert "mycustomop" in combined            # the refusal still names it
    assert ".supertool.json" in combined       # and the config is disclosed


def test_a_non_dict_ops_config_does_not_wave_a_substring_through(
    monkeypatch
) -> None:
    """`op in config["ops"]` against a STRING is a Python substring test, so
    `foo` read as routable against `"xyzfooabc"` -- the guard silently
    permitting exactly what it exists to refuse. A malformed config must
    degrade this to "not a custom op", never to "yes, routable".
    """
    monkeypatch.setattr(supertool, "_CONFIG",
                        {"ops": "xyzfooabc", "aliases": "barbaz"}, raising=False)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True, raising=False)

    assert supertool._op_is_unroutable("foo") is True
    assert supertool._op_is_unroutable("bar") is True
    # A real builtin is unaffected by the malformed config.
    assert supertool._op_is_unroutable("read") is False
