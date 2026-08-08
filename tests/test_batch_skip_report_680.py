"""#680 — a batch entry that changed nothing must say so in the footer.

The reported incident: a 4-op batch removed two `use` imports and failed to
rewrite the block that referenced them, because that entry's `old` had drifted.
The per-op `ERROR:` was printed, but it sat above two `[validators]` blocks, and
the reader was piping to `tail`. The last line they saw was

    [result] 6 ops run, 4 writes

which is a *count mismatch* — you only read it as a failure if you already
arrived suspicious. The branch went to CI with imports removed and their users
left behind.

So the invariant here is narrower than #621's "an op which changed nothing must
not end with output that looks like an op which did". It is:

    an op which DECLINED must be named as declined in the footer — with the
    word, not with arithmetic the reader has to perform.

`skipped` is the third state docs/validators.md already defines for validators
("Declining instead of guessing"): not `ok`, not a finding — an op that ran and
deliberately left the disk alone. It is bumped where the decline is *decided*,
never inferred from `attempts - writes`, because that subtraction is wrong for
multi-file `replace` (writes > attempts) and for `replace_dry` (a preview is
not a decline).

The tests assert the rendered tail and the process exit code, never a warning
substring: "the word WARN appeared" would pass against a tool that printed the
word and applied nothing.
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
    """Just the `[result]` line. Tests must not assert against the whole body:
    pytest tmp dirs are named after the test, so `"skipped" not in out` is a
    statement about the directory name as much as about the footer."""
    for line in out.splitlines():
        if line.startswith("[result] "):
            return line
    return ""


# ---------------------------------------------------------------------------
# The footer names the decline
# ---------------------------------------------------------------------------

def test_batch_footer_names_the_skipped_entry(tmp_path: Path, monkeypatch) -> None:
    """The reported incident, minimised: one drifted `old` among three edits.

    `3 ops run, 2 writes` is the pre-fix rendering — true, and unreadable at a
    glance. The fix is the word.
    """
    _no_branch(monkeypatch)
    f = tmp_path / "x.txt"
    f.write_text("alpha\nbeta\ngamma\n")
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([
        {"op": "edit", "path": str(f), "old": "alpha", "new": "ALPHA"},
        {"op": "edit", "path": str(f), "old": "DRIFTED_AWAY", "new": "x"},
        {"op": "edit", "path": str(f), "old": "gamma", "new": "GAMMA"},
    ]))
    out = supertool.dispatch(f"batch:@{payload}")
    assert "[result] 3 ops run, 2 writes, 1 skipped" in _tail(out)
    # Two since #1027, not one: the batch leads with the same count, because the
    # footer sits below a validators block long enough that `| tail` ends on
    # `git-status : ok`. The invariant this line has always guarded is untouched
    # -- one count per SUB-OP is what must never happen -- and it is asserted
    # positionally as well, since a bare total of two would be satisfied by a
    # two-op batch that repeated per sub-op.
    assert out.count("[result] ") == 2, "one leading count and one footer"
    between = out.split("--- edit:", 1)[-1].rsplit("[result] ", 1)[0]
    assert "[result] " not in between, "no count inside the per-op results"


def test_batch_where_every_entry_declines_counts_them_all(tmp_path: Path, monkeypatch) -> None:
    """`0 writes — nothing changed on disk` already told the truth here. The
    skipped count is what says *why* it changed nothing: the ops declined,
    rather than never having run."""
    _no_branch(monkeypatch)
    f = tmp_path / "x.txt"
    f.write_text("alpha\n")
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([
        {"op": "edit", "path": str(f), "old": "NOPE1", "new": "x"},
        {"op": "edit", "path": str(f), "old": "NOPE2", "new": "y"},
    ]))
    out = supertool.dispatch(f"batch:@{payload}")
    assert f.read_text(encoding="utf-8") == "alpha\n"
    assert "[result] 2 ops run, 0 writes, 2 skipped — nothing changed on disk" in _tail(out)


def test_ambiguous_edit_is_a_decline_not_a_write(tmp_path: Path, monkeypatch) -> None:
    """`old` found twice is refused for safety — same third state: the op ran
    and chose to leave the file alone."""
    _no_branch(monkeypatch)
    f = tmp_path / "x.txt"
    f.write_text("dup\ndup\n")
    out = supertool.dispatch(f"edit:::dup:::UNIQ:::{f}")
    assert f.read_text(encoding="utf-8") == "dup\ndup\n"
    assert "[result] 1 op run, 0 writes, 1 skipped — nothing changed on disk" in _tail(out)


def test_replace_zero_match_is_a_decline(tmp_path: Path, monkeypatch) -> None:
    """`(0 occurrences of 'x' found)` is the one no-op receipt in the tool that
    does not say `ERROR`, so it never reached the exit code and never reached
    the footer. It is exactly the shape #680 is about."""
    _no_branch(monkeypatch)
    f = tmp_path / "x.txt"
    f.write_text("alpha\n")
    out = supertool.dispatch(f"replace:::NOPE_NOT_THERE:::x:::{f}")
    assert "[result] 1 op run, 0 writes, 1 skipped — nothing changed on disk" in _tail(out)


def test_vim_atomic_decline_is_counted(tmp_path: Path, monkeypatch) -> None:
    """vim ops are all-or-nothing: a pattern that does not match applies none
    of the actions. That is a decline, and the footer has to carry it."""
    _no_branch(monkeypatch)
    f = tmp_path / "x.txt"
    f.write_text("alpha\n")
    out = supertool.dispatch(f"vim:::{f}:::/NOPE_NOT_THERE")
    assert f.read_text(encoding="utf-8") == "alpha\n"
    assert "1 skipped" in _tail(out)


# ---------------------------------------------------------------------------
# The exit code — so a `&&` chain stops
# ---------------------------------------------------------------------------

def test_exit_code_nonzero_when_a_batch_entry_declined(tmp_path: Path, monkeypatch, capsys) -> None:
    """An agent chaining `batch: && git commit` must not commit a half-applied
    set. `replace` is the case that mattered: its zero-match receipt says no
    `ERROR`, so `_body_indicates_failure` never saw it and the call exited 0."""
    _no_branch(monkeypatch)
    f = tmp_path / "x.txt"
    f.write_text("alpha\nbeta\n")
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([
        {"op": "replace", "path": str(f), "old": "alpha", "new": "ALPHA"},
        {"op": "replace", "path": str(f), "old": "NOPE_NOT_THERE", "new": "x"},
    ]))
    rc = supertool.main([f"batch:@{payload}"])
    capsys.readouterr()
    assert rc == 1


def test_exit_code_nonzero_for_a_lone_replace_that_matched_nothing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _no_branch(monkeypatch)
    f = tmp_path / "x.txt"
    f.write_text("alpha\n")
    rc = supertool.main([f"replace:::NOPE_NOT_THERE:::x:::{f}"])
    capsys.readouterr()
    assert rc == 1


def test_exit_code_stays_zero_when_everything_applied(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The guard against the fix over-firing: a fully applied batch must still
    be a green `&&` link, or the exit code stops being information."""
    _no_branch(monkeypatch)
    f = tmp_path / "x.txt"
    f.write_text("alpha\nbeta\n")
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([
        {"op": "edit", "path": str(f), "old": "alpha", "new": "ALPHA"},
        {"op": "edit", "path": str(f), "old": "beta", "new": "BETA"},
    ]))
    rc = supertool.main([f"batch:@{payload}"])
    capsys.readouterr()
    assert rc == 0
    assert f.read_text(encoding="utf-8") == "ALPHA\nBETA\n"


def test_skip_count_does_not_leak_between_calls(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The counter is process-global and the daemon reuses the process, so the
    exit code must read a per-call delta. Without that, one declined replace
    poisons every later call in the same worker."""
    _no_branch(monkeypatch)
    f = tmp_path / "x.txt"
    f.write_text("alpha\n")
    assert supertool.main([f"replace:::NOPE:::x:::{f}"]) == 1
    capsys.readouterr()
    g = tmp_path / "y.txt"
    g.write_text("beta\n")
    assert supertool.main([f"edit:::beta:::BETA:::{g}"]) == 0
    capsys.readouterr()


# ---------------------------------------------------------------------------
# Guards — the field must not become noise, and the hint must survive
# ---------------------------------------------------------------------------

def test_clean_run_omits_the_field(tmp_path: Path, monkeypatch) -> None:
    """#680 asked for `0 skipped` on every line. Declined: a zero on the green
    path is a number you learn to stop reading, which is how the original
    `4 writes` failed. The word appears only when it means something."""
    _no_branch(monkeypatch)
    f = tmp_path / "x.txt"
    f.write_text("alpha\n")
    out = supertool.dispatch(f"edit:::alpha:::ALPHA:::{f}")
    assert "[result] 1 op run, 1 write\n" in out
    assert "skipped" not in _result_line_of(out)


def test_preview_is_not_a_decline(tmp_path: Path, monkeypatch) -> None:
    """`replace_dry` writes nothing by design. Inferring skips from
    `attempts - writes` would call every preview a decline; the counter is
    bumped where the decline is decided, so a preview is silent."""
    _no_branch(monkeypatch)
    f = tmp_path / "x.txt"
    f.write_text("alpha\n")
    out = supertool.dispatch(f"replace_dry:::alpha:::GAMMA:::{f}")
    assert "[result] " not in out


def test_batch_decline_keeps_the_nearest_match_hint(tmp_path: Path, monkeypatch) -> None:
    """The hint is what makes a declined entry a 5-second fix instead of a
    read round-trip. It already works in a batch; pin it so the footer work
    cannot quietly cost it."""
    _no_branch(monkeypatch)
    f = tmp_path / "x.txt"
    f.write_text("alpha_one_two_three\n")
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([
        {"op": "edit", "path": str(f), "old": "alpha_one_twe_three", "new": "x"},
    ]))
    out = supertool.dispatch(f"batch:@{payload}")
    assert "nearest match at line 1" in out
    assert "1 skipped" in _tail(out)


# ---------------------------------------------------------------------------
# The rendering itself
# ---------------------------------------------------------------------------

def test_result_line_renders_and_singularises_skipped() -> None:
    assert supertool._result_line(3, 2, 1) == "[result] 3 ops run, 2 writes, 1 skipped\n"
    assert supertool._result_line(3, 1, 2) == "[result] 3 ops run, 1 write, 2 skipped\n"
    assert (supertool._result_line(2, 0, 2)
            == "[result] 2 ops run, 0 writes, 2 skipped — nothing changed on disk\n")


def test_result_line_omits_skipped_when_zero() -> None:
    """Back-compatible with every existing positional reader and with #621's
    own assertions."""
    assert supertool._result_line(2, 2) == "[result] 2 ops run, 2 writes\n"
    assert supertool._result_line(2, 2, 0) == "[result] 2 ops run, 2 writes\n"
