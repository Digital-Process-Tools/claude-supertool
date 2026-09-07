"""#938 self-review finding: the reapplied-detection loop must stay linear.

`_edit_already_applied` does an O(n) `content.find(new)` scan per call, which
is cheap for `op_edit` (exactly one occurrence of `old`, more than one is
refused as ambiguous). `op_replace` is replace-all and can have many
occurrences of `old` in one file, and a first draft of #938's fix called
`_edit_already_applied` once per occurrence -- an O(n*m) pass added on top of
what used to be a flat O(n) `str.replace`, paid even on a plain FIRST
application with nothing to disclose. A self-review (Explore reviewer)
measured 1.46s/4.35s/16.23s at 5000/10000/20000 occurrences on that draft --
consistent with quadratic growth.

`_count_already_applied` replaces the per-occurrence loop with one pass over
each string's own occurrence list plus a `bisect` lookup per occurrence
(O(n + m*log k)). This test is a budget, not a growth-curve proof -- a wall
clock is too noisy across CI runners for the latter -- but the budget is wide
enough that a reintroduced O(n*m) pass at this N would blow through it by a
large margin (the pre-fix code took ~16s at N=20000; this asserts the whole
call completes well under one second).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import supertool


def test_first_application_over_many_occurrences_stays_fast(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))
    f = tmp_path / "big.py"
    # `old` occurs 20000 times and `new` ("ba") already occurs just as often
    # for unrelated reasons (it is a substring of the file's own repeating
    # unit) -- the shape that defeated a naive `eff_new in content` short
    # circuit in the first draft of this fix.
    f.write_text("bax" * 20000, encoding="utf-8")
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([
        {"op": "replace", "path": str(f), "old": "a", "new": "ba"},
    ]), encoding="utf-8")

    start = time.time()
    out = supertool.dispatch(f"batch:@{payload}")
    elapsed = time.time() - start

    assert "[result] 1 op run, 1 write" in out
    assert elapsed < 3.0, f"took {elapsed:.2f}s -- reapplied detection regressed to O(n*m)"


def test_count_already_applied_matches_the_single_occurrence_helper() -> None:
    """`_count_already_applied` must agree with `_edit_already_applied` on
    every occurrence it counts -- the batching is an optimisation, not a
    redefinition of what counts as a re-application."""
    content = "def f():\n    return 1\n\ndef f():\n    return 1\n"
    old = "def f():"
    new = "@decorated\ndef f():"
    # Neither occurrence of `old` here sits inside an occurrence of `new` --
    # this file has two independent (never-decorated) definitions, not one
    # decorated one -- so the batched count must agree with zero.
    assert supertool._count_already_applied(content, old, new) == 0

    reapplied_once = "@decorated\ndef f():\n    return 1\n"
    assert supertool._count_already_applied(reapplied_once, old, new) == 1
    idx = reapplied_once.index(old)
    assert supertool._edit_already_applied(reapplied_once, old, new, idx) is True

    twice = "@decorated\ndef f():\n    return 1\n\n@decorated\ndef f():\n    return 1\n"
    assert supertool._count_already_applied(twice, old, new) == 2
