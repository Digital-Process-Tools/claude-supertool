"""#2432 -- the mixed-tree decline's own suggested remedy (`cwd:` as the first
op) is safe advice for a read-only op and dangerous advice for a write: `cwd:`
really does `os.chdir(target)` for the rest of the call, so a caller who
follows the remedy for a *write* op (git-push, git-commit, gh-pr-merge,
git-conflicts, ...) ends up running that write against `core`'s own
checked-out branch, not the worktree branch they meant -- silently, because
the write still succeeds, just against the wrong repository/branch.

Mirrors the helper shapes in `test_mixed_tree_guard_678.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool


def _probe_cmd(sentinel: str, token: str) -> str:
    return (
        '{python} -c "'
        f"open('{sentinel}', 'w').close(); print('{token}')"
        '"'
    )


def _project_root(tmp_path: Path, name: str, ops: dict) -> Path:
    root = tmp_path / name
    root.mkdir()
    cfg = {"ops": ops}
    (root / ".supertool.json").write_text(json.dumps(cfg), encoding="utf-8")
    return root


def _stand_in(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.chdir(root)
    cfg = json.loads((root / ".supertool.json").read_text(encoding="utf-8"))
    supertool._CONFIG = cfg
    supertool._CONFIG_CHECKED = True
    supertool._CONFIG_PATH = str(root / ".supertool.json")


def _foreign_core(root: Path) -> Path:
    peer = root / "supertool.py"
    peer.write_text("# a different build of supertool\n", encoding="utf-8")
    return peer


def test_decline_for_a_write_op_does_not_suggest_cwd(tmp_path, monkeypatch):
    """The dangerous case: a write-class custom op declines without pointing
    the caller at `cwd:`, since `cwd:{core}` would silently retarget the
    write at the wrong checkout."""
    ops = {"probe-write": {"cmd": _probe_cmd("ran.txt", "PROBE-OK"), "safety": "writes"}}
    root = _project_root(tmp_path, "other_checkout", ops)
    _foreign_core(root)
    _stand_in(monkeypatch, root)

    out = supertool.dispatch("probe-write")

    assert "make the first op 'cwd:" not in out, (
        f"a write-op decline must not suggest 'cwd:' as the way to fix the "
        f"call -- following it silently redirects the write at the wrong "
        f"checkout (#2432):\n{out}"
    )
    assert "Do NOT use 'cwd:" in out, (
        f"a write-op decline should warn a reader off 'cwd:' by name, not "
        f"just omit it:\n{out}"
    )
    assert str(root) in out, "the decline never names the tree the write belongs to"
    assert "supertool.py" in out, "the decline never names the safe remedy"


def test_decline_for_a_read_only_op_still_suggests_cwd(tmp_path, monkeypatch):
    """The positive control / normal-use case this must not break: a
    read-only custom op is safe to answer via `cwd:{core}`, and the decline
    still offers that remedy."""
    ops = {"probe-read": {"cmd": _probe_cmd("ran.txt", "PROBE-OK"), "safety": "read-only"}}
    root = _project_root(tmp_path, "other_checkout", ops)
    _foreign_core(root)
    _stand_in(monkeypatch, root)

    out = supertool.dispatch("probe-read")

    assert "cwd:" in out, (
        f"a read-only op's decline lost its 'cwd:' remedy -- that route is "
        f"still safe for a read:\n{out}"
    )
