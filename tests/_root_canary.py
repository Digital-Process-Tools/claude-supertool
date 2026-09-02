"""#1635 -- a canary for the destructive event nobody could reproduce.

A full `pytest` run in a fresh `git clone` was once observed to leave the
tree holding only `tests/` -- `.git` and everything else gone, 603 collection
errors, once, unreproduced, with a reviewer agent running the suite in a
sibling worktree at the same time. `test_directory_removal_ownership_1635.py`
answered the question that is answerable either way -- every directory
removal this tree's own code performs resolves to a directory that same code
created, so nothing *found* explains it -- and said so rather than the event
being established as a code defect.

That leaves the case a register of known call sites cannot cover: a
mechanism outside this tree's own Python -- a fixture racing another
process's cleanup, a plugin, the runner itself. A register can only be
wrong about code it can see; this file does not try to see the mechanism,
it watches the one outcome that matters and says so if it happens again.
Session-scoped by construction: this is checking whether the *run* leaves
the tree standing, not whether any one test does.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

#: What "the tree is still here" means. `.git` is the load-bearing one --
#: gone, and a clone or a worktree cannot be restored from within itself.
#: `pyproject.toml` and `supertool.py` are two more: #1635's own observed end
#: state left `tests/` behind and *only* `tests/`, so a marker that also asks
#: about a sibling of `tests/` is needed for the check to fire on that exact
#: shape rather than only on `.git` going first.
MARKERS = (".git", "pyproject.toml", "supertool.py")


def _kind(path: Path) -> str:
    """A marker's kind rather than a presence bit -- a `.git` that changed
    from a worktree's file to nothing, or from a clone's directory to a file,
    is also worth reporting, and a bare bool would collapse both into the
    same "still there" as an untouched marker."""
    if path.is_dir():
        return "dir"
    if path.exists():
        return "file"
    return "absent"


def snapshot(root: Path) -> Dict[str, str]:
    """What every marker looks like right now, under `root`."""
    return {name: _kind(root / name) for name in MARKERS}


def verdict(before: Dict[str, str], after: Dict[str, str]) -> Optional[str]:
    """`None` if every marker that existed before still exists after.

    Otherwise the sentence to fail the run on, naming exactly what vanished
    -- a marker present in `before` and absent in `after`. A marker never
    present to begin with (a synthetic tree in a test of this module, or a
    checkout missing one by some other cause entirely) is not this check's
    business: it cannot tell "this run destroyed it" from "it was never
    there", and reporting the former for the latter is the false alarm that
    would get this canary disabled.
    """
    vanished = sorted(
        name for name, kind in before.items()
        if kind != "absent" and after.get(name, "absent") == "absent"
    )
    if not vanished:
        return None
    return (
        "the working tree lost {0} during this run -- #1635's own observed "
        "failure mode (once, unreproduced) has a second data point now. Do "
        "not re-run to check; the tree in this state is the evidence.".format(
            ", ".join(vanished)))
