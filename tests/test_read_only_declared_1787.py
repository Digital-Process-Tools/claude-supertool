"""#1787 -- a caller who is read-only by design has no way to say so and no
way to be held to it. `ops:roster` already classes every op unmarked
(read-only), `*` (writes in this tree) or `!` (reaches outside it, or
outlives the call) -- this pins the ONE new consumer of that classification:
`SUPERTOOL_READ_ONLY=1` makes `dispatch` refuse any op that is not classed
`read-only`, naming the op and its class rather than a bare "denied", instead
of running it.

Env var, not a `.supertool.json` key: the flag is a property of the CALLER's
intent for one invocation, not of the repo. The same worktree is dispatched
into by both a read-only review agent and a normal writing session, often in
the same tick -- a project-level toggle would gate everyone or no one, and
whichever agent set it last would silently win for every other caller in that
process tree.

Mirrors `tests/test_mixed_tree_write_ops_1942.py`'s fixture shape: a genuine
read-only op is the positive control (must still run) beside the write/acts
ops that must be refused -- a "must not fire" case with no "must fire" case
beside it passes when the whole gate is dead, not just when it is correct.
"""

from __future__ import annotations

import supertool


def _stand_in_acts_op(monkeypatch, tmp_path):
    """A project config declaring one `!`-class (acts) custom op, standing
    in for the issue's own incident (`radar`, classed `!`) without shelling
    out to it -- the gate must fire before `_resolve_custom_op` ever runs
    the command, so the cmd template here is never actually executed."""
    monkeypatch.chdir(tmp_path)
    supertool._CONFIG = {
        "ops": {"myacts": {"cmd": "{python} -c \"pass\"", "safety": "acts"}}
    }
    supertool._CONFIG_CHECKED = True
    supertool._CONFIG_PATH = str(tmp_path / ".supertool.json")


def test_write_class_builtin_runs_without_the_flag(tmp_path, monkeypatch):
    """Positive control: no flag set, a `writes`-class op executes normally."""
    monkeypatch.delenv("SUPERTOOL_READ_ONLY", raising=False)
    monkeypatch.chdir(tmp_path)
    out = supertool.dispatch("paste:::new_file.txt:::hello world")
    assert "hello world" in (tmp_path / "new_file.txt").read_text(encoding="utf-8")
    assert "SKIPPED" not in out


def test_write_class_builtin_declines_under_read_only(tmp_path, monkeypatch):
    """The case this issue is about: `paste` is class `writes` and must not
    touch disk when the caller declared itself read-only."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUPERTOOL_READ_ONLY", "1")
    before = supertool._SKIP_COUNT[0]
    out = supertool.dispatch("paste:::new_file.txt:::hello world")

    assert not (tmp_path / "new_file.txt").exists(), (
        "a read-only-declared caller must never see this file created")
    assert "SKIPPED" in out
    assert "paste" in out
    assert "writes" in out
    assert "SUPERTOOL_READ_ONLY" in out
    assert supertool._SKIP_COUNT[0] == before + 1


def test_acts_class_op_declines_under_read_only(tmp_path, monkeypatch):
    """`!`-class (acts) custom ops are refused the same way as `*`-class
    builtins -- the issue's own incident (`radar`, classed `!`) is this
    branch, not `writes`."""
    _stand_in_acts_op(monkeypatch, tmp_path)
    monkeypatch.setenv("SUPERTOOL_READ_ONLY", "1")
    out = supertool.dispatch("myacts")
    assert "SKIPPED" in out
    assert "myacts" in out
    assert "acts" in out


def test_acts_class_op_runs_without_the_flag(tmp_path, monkeypatch):
    """Positive control for the acts case above: unset, the same op is not
    caught by anything this change added (it may still fail for unrelated
    reasons -- e.g. no real python in {cmd} substitution -- but must not say
    SKIPPED for a read-only reason it was never given)."""
    _stand_in_acts_op(monkeypatch, tmp_path)
    monkeypatch.delenv("SUPERTOOL_READ_ONLY", raising=False)
    out = supertool.dispatch("myacts")
    assert "SUPERTOOL_READ_ONLY" not in out


def test_read_only_class_op_still_runs_under_read_only(tmp_path, monkeypatch):
    """The positive control for the flag itself: a genuinely read-only op
    (`version`) must NOT be caught by its own gate -- a flag that blocks
    everything, including reads, is not what was asked for."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUPERTOOL_READ_ONLY", "1")
    out = supertool.dispatch("version")
    assert "SKIPPED" not in out


def test_refusal_names_class_and_points_at_roster(tmp_path, monkeypatch):
    """Sub-question 2 in the issue: the refusal must name the class and the
    op, and point somewhere the full list lives, not just say "denied"."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUPERTOOL_READ_ONLY", "1")
    out = supertool.dispatch("paste:::x.txt:::y")
    assert "ops:roster" in out


def test_flag_recognises_common_truthy_spellings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("SUPERTOOL_READ_ONLY", value)
        out = supertool.dispatch("paste:::x.txt:::y")
        assert "SKIPPED" in out, f"value {value!r} should have declared read-only"
        assert not (tmp_path / "x.txt").exists()


def test_falsy_or_absent_flag_does_not_gate(tmp_path, monkeypatch):
    """Negative control on the flag's own parsing: empty string, '0', and
    unset must all behave as 'not declared', not crash or refuse."""
    monkeypatch.chdir(tmp_path)
    for value in ("0", "", "false", "no"):
        monkeypatch.setenv("SUPERTOOL_READ_ONLY", value)
        out = supertool.dispatch("gc")
        assert "SUPERTOOL_READ_ONLY=1 is set" not in out
    monkeypatch.delenv("SUPERTOOL_READ_ONLY", raising=False)
    out = supertool.dispatch("gc")
    assert "SUPERTOOL_READ_ONLY=1 is set" not in out
