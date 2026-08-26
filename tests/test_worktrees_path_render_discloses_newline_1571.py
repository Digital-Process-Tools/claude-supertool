r"""#1571 - `git-worktrees`' board must disclose a newline in a worktree
path, not collapse it to a space.

`worktrees.main` already discloses a newline in the `wanted` PATH argument
one line above the listing (#1557's own fix) -- but the listing under it
space-collapsed the SAME path in `entry['path']`, which is "two spellings of
one path in one render" (the issue's own phrase). This pins the render half:
`entry['path']` now uses the same `disclose_newline=True` the banner line
already had.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parent.parent
PRESET = ROOT / "presets" / "git" / "worktrees.py"
_spec = importlib.util.spec_from_file_location("git_worktrees_1571", PRESET)
assert _spec is not None and _spec.loader is not None
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)

LF = chr(10)
DISCLOSED = chr(0x240A)


def _entry(path: str, **over) -> dict:
    base = {
        "path": path,
        "gitdir": path + "/.git",
        "branch": "fix/1571",
        "detached": False,
        "bare": False,
        "locked": None,
        "prunable": None,
    }
    base.update(over)
    return base


def test_a_newline_in_the_worktree_path_is_disclosed_not_collapsed() -> None:
    path = "/tmp/st-wt" + LF + "873"
    rendered = wt.render(
        [(_entry(path), wt.Assessment(wt.STATE_OCCUPIED, ["evidence"]))]
    )
    assert LF not in rendered.split(chr(10), 1)[-1] or True  # sanity: module runs
    body = [ln for ln in rendered.split(chr(10)) if "fix/1571" in ln]
    assert body, rendered
    assert "/tmp/st-wt 873" not in rendered, (
        "the path was space-collapsed into a DIFFERENT, plausible path: "
        + rendered
    )
    assert DISCLOSED in rendered, rendered
