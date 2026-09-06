"""The rollback and the writer still resolve the write target independently (#1147).

#1136 made `_run_with_validators`'s rollback arms and `_atomic_write` AGREE on
which object a write touched -- both call `_write_target`. It did not remove
the DOUBLE RESOLUTION: `_run_with_validators` samples `_target = _write_target(
path)` once, before the op runs, and reuses that sample for every rollback arm.
`_atomic_write` did not reuse it -- it re-derived `real_path = _write_target(
path)` on its own, at write time, from `path` alone.

So the two calls could still disagree, through a narrower window than #1136
closed: if `path` were retargeted between the sample and the write (an external
process relinking it, or a plain file turning into a symlink after the sample
was taken), the writer would land on one object and the rollback -- keyed on
the earlier sample -- would act on another. No reproduction exists without an
external process racing a single-user CLI, which is why this is hardening
rather than `destroys`, and why the test below has to simulate the race rather
than trigger it for real: it makes `_write_target` answer differently the
SECOND time it is asked, the same shape a real relink would produce between
the sample and the write.

The structural close is to pass the sampled target into `_atomic_write` rather
than let it re-derive one -- one resolution, one object, no window.
"""
from __future__ import annotations

from pathlib import Path

from _symlink import require_symlink

import supertool

NL = chr(10)


def _symlink(link: Path, target_name: str) -> None:
    require_symlink()
    import os
    os.symlink(target_name, str(link))


def test_the_write_lands_on_the_sample_not_on_a_later_re_resolution(
    tmp_path: Path, monkeypatch
) -> None:
    """RED before the fix: `_atomic_write` asks `_write_target` a second time
    and gets a different answer than `_run_with_validators` already sampled --
    the write then lands on the SECOND answer, not the sampled one.
    """
    link = tmp_path / "link.py"
    real_target = tmp_path / "real.py"
    other_target = tmp_path / "other.py"
    _symlink(link, "real.py")

    orig = supertool._write_target
    calls = []

    def fake(path):
        calls.append(path)
        if len(calls) == 1:
            # The one call `_run_with_validators` makes, before the op runs --
            # a genuine resolution, exactly as `_write_target` would answer.
            return orig(path)
        # Every later call is the writer asking again on its own -- answered
        # as if the link had been retargeted in between.
        return str(other_target)

    monkeypatch.setattr(supertool, "_write_target", fake)
    out = supertool.dispatch("paste:" + str(link) + ":x = 1" + NL)

    assert len(calls) >= 2, (
        "the fake never saw a second call -- this test no longer exercises "
        "the double resolution it was written to pin:" + NL + out)
    assert real_target.read_text(encoding="utf-8").startswith("x = 1"), (
        "the write landed on whatever _atomic_write re-resolved for itself, "
        "not on the target _run_with_validators already sampled:" + NL + out)
    assert not other_target.exists(), (
        "the write reached a second, independently-resolved object:" + NL + out)


def test_a_plain_write_with_no_race_is_unaffected(tmp_path: Path) -> None:
    """The boundary: pinning a target must not change an ordinary write."""
    target = tmp_path / "plain.py"
    out = supertool.dispatch("paste:" + str(target) + ":x = 1" + NL)
    assert target.read_text(encoding="utf-8").startswith("x = 1"), out
    assert "ERROR" not in out, out


def test_op_replace_is_unaffected_by_the_pin(tmp_path: Path) -> None:
    """`op_replace` walks many files under one `path`; none of them is the
    single path `_run_with_validators` pinned, so each must still resolve its
    own target rather than being redirected onto the pinned one.
    """
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x = 1" + NL, encoding="utf-8")
    b.write_text("x = 1" + NL, encoding="utf-8")
    out = supertool.dispatch("replace:x = 1:x = 2:" + str(tmp_path))
    assert a.read_text(encoding="utf-8").startswith("x = 2"), out
    assert b.read_text(encoding="utf-8").startswith("x = 2"), out
