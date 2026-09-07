"""#2374 -- `worktree` was classed `*` ("writes files in this tree") even
though `worktree:teardown[:PATH]` (and `worktree:setup[:PATH]`, which shares
the same op) can `dest.unlink()` / `shutil.rmtree(dest)` a PATH that resolves
to a SIBLING worktree entirely outside cwd -- `presets/worktree.json`
exempts PATH from the generic cwd/repo containment gate
(`"paths": {"args": []}`) precisely because pointing outside cwd is the
documented use case. `_op_safety_class`'s own docstring says an undeclared
op falls back to `"acts"` (class `!`, "reaches outside this tree, or
outlives the call"), calling that the loudest and safest default -- `*`
understated this op's actual reach, so an agent reading `ops:roster` was
told "confined to this tree" about an op that is not.

Fixed by reclassifying `worktree` from `"writes"` to `"acts"` in
`presets/worktree.json`, matching `git-push` and every other op in this repo
whose write/delete effect can land outside the tree the call was made from.

Pins two things: `_op_safety_class("worktree")` answers `"acts"`, and the
rendered roster (`ops:roster`, which walks `_roster_classes()`) shows the
`!` marker beside the worktree op's row rather than `*` -- the docstring's
own claim ("undeclared/misdeclared renders as the loudest class") is worth
nothing if the row an agent actually reads still shows the quieter one.
"""

from __future__ import annotations

import supertool


def test_worktree_op_is_classed_acts_not_writes(shipped_config):
    assert supertool._op_safety_class("worktree") == "acts", (
        "worktree:teardown[:PATH]/worktree:setup[:PATH] can write/delete "
        "at a PATH outside cwd (a sibling worktree) -- PATH is explicitly "
        "exempted from the cwd containment gate for exactly this reason "
        "(presets/worktree.json's \"paths\": {\"args\": []}) -- so the "
        "safety class must be the loudest one ('acts', marker '!'), not "
        "'writes' (marker '*', 'writes files in this tree')."
    )


def test_roster_renders_bang_marker_beside_worktree(shipped_config):
    classes = supertool._roster_classes()
    assert classes.get("worktree") == "acts"
    marker = supertool._SAFETY_MARKERS.get(classes.get("worktree"), "!")
    assert marker == "!", (
        "ops:roster must render '!' beside the worktree op's row so an "
        "agent reading the roster is told this op reaches outside the "
        "tree, matching its actual PATH-outside-cwd write/delete reach."
    )
