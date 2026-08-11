"""#1317 — project_dir() must never answer with a SIBLING directory's store.

The old fallback picked the store whose encoded name shared the longest common
prefix with the cwd's. From `~/Documents/st-wt/1317`, with no store of its own,
that returned `-Users-floriandavid-Documents-st-wt-1024` — a different worktree,
rendered by every claude-log op as if it were the caller's own sessions.

Upward is legitimate (a subdirectory of a project belongs to that project's
store). Sideways is not. When neither the cwd nor an ancestor has a store, the
answer is a named decline, not a neighbour.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PRESET_DIR = Path(__file__).resolve().parent.parent / "presets" / "claude-log"
sys.path.insert(0, str(PRESET_DIR))

from _preset_loader import load_preset_module  # noqa: E402

_common = load_preset_module("claude-log", "_common", prefix="claude_log_")


def _store(home: Path, cwd: Path) -> Path:
    """Create the transcript store Claude Code would keep for `cwd`."""
    d = home / ".claude" / "projects" / _common.encode_cwd(str(cwd))
    d.mkdir(parents=True)
    return d


def _session(store: Path, uuid: str, text: str) -> Path:
    p = store / f"{uuid}.jsonl"
    ev = {"type": "user", "message": {"role": "user",
                                      "content": [{"type": "text", "text": text}]}}
    p.write_text(json.dumps(ev) + "\n", encoding="utf-8")
    return p


def _run(script: str, home: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)  # Windows
    return subprocess.run(
        [sys.executable, str(PRESET_DIR / script), *args],
        capture_output=True, text=True, cwd=str(cwd), env=env,
        timeout=60, encoding="utf-8", errors="replace",
    )


class TestNeverSideways:
    """The reported defect, at the resolver."""

    def test_sibling_store_is_not_this_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        wt = tmp_path / "Documents" / "st-wt"
        mine = wt / "1317"
        sibling = wt / "1024"
        mine.mkdir(parents=True)
        sibling.mkdir(parents=True)
        _store(tmp_path, sibling)

        res = _common.resolve_project_dir(str(mine))
        assert res.kind == "missing"
        assert res.path == tmp_path / ".claude" / "projects" / _common.encode_cwd(str(mine))
        assert not res.path.exists()
        # And the plain accessor agrees — callers gate on .exists().
        assert not _common.project_dir(str(mine)).exists()

    def test_shared_prefix_alone_never_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`.../proj-old` shares every char of `.../proj` and is still not it."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        work = tmp_path / "work"
        mine = work / "proj"
        mine.mkdir(parents=True)
        old = work / "proj-old"
        old.mkdir(parents=True)
        _store(tmp_path, old)

        assert _common.resolve_project_dir(str(mine)).kind == "missing"

    def test_decline_reports_how_many_stores_it_looked_past(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        mine = tmp_path / "work" / "proj"
        mine.mkdir(parents=True)
        for name in ("a", "b", "c"):
            other = tmp_path / "work" / name
            other.mkdir()
            _store(tmp_path, other)

        res = _common.resolve_project_dir(str(mine))
        assert res.kind == "missing"
        assert res.store_count == 3


class TestUpwardStillWorks:
    """The fallback's legitimate half must survive the fix."""

    def test_direct_match(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        mine = tmp_path / "work" / "proj"
        mine.mkdir(parents=True)
        store = _store(tmp_path, mine)

        res = _common.resolve_project_dir(str(mine))
        assert res.kind == "direct"
        assert res.path == store

    def test_subdirectory_resolves_to_its_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        proj = tmp_path / "work" / "proj"
        deep = proj / "presets" / "claude-log"
        deep.mkdir(parents=True)
        store = _store(tmp_path, proj)

        res = _common.resolve_project_dir(str(deep))
        assert res.kind == "ancestor"
        assert res.path == store

    def test_nearest_ancestor_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        outer = tmp_path / "work"
        inner = outer / "proj"
        deep = inner / "src"
        deep.mkdir(parents=True)
        _store(tmp_path, outer)
        near = _store(tmp_path, inner)

        assert _common.resolve_project_dir(str(deep)).path == near

    def test_windows_shaped_path_walks_up_on_any_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ancestors come from the path string, not from pathlib.

        On POSIX, pathlib reads a backslash-separated Windows path as ONE
        component with no useful parents, so a resolver built on `.parents`
        would answer `missing` here on macOS and `ancestor` on Windows — a
        platform-shaped lie rather than a test.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        root = tmp_path / ".claude" / "projects"
        root.mkdir(parents=True)
        (root / _common.encode_cwd(r"C:\Users\foo\proj")).mkdir()

        res = _common.resolve_project_dir(r"C:\Users\foo\proj\src\deep")
        assert res.kind == "ancestor"
        assert res.path.name == _common.encode_cwd(r"C:\Users\foo\proj")

    def test_no_projects_root_at_all_is_missing_not_a_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        mine = tmp_path / "work" / "proj"
        mine.mkdir(parents=True)

        res = _common.resolve_project_dir(str(mine))
        assert res.kind == "missing"
        assert res.store_count == 0


class TestOpsSurfaceTheDecline:
    """Blast radius: the decline must reach the render — not an empty board, and
    not somebody else's board."""

    def _sideways_setup(self, tmp_path: Path):
        home = tmp_path / "fake-home"
        wt = tmp_path / "Documents" / "st-wt"
        mine = wt / "1317"
        sibling = wt / "1024"
        mine.mkdir(parents=True)
        sibling.mkdir(parents=True)
        store = _store(home, sibling)
        _session(store, "aaaaaaaa-0000-0000-0000-000000000000", "the neighbours work")
        return home, mine, store

    def test_list_declines_instead_of_rendering_a_neighbour(self, tmp_path: Path) -> None:
        home, mine, store = self._sideways_setup(tmp_path)
        cp = _run("list.py", home, mine)
        out = cp.stdout + cp.stderr
        assert "no sessions recorded for this directory" in out
        assert "aaaaaaaa-0000-0000-0000-000000000000" not in out
        assert "the neighbours work" not in out
        assert str(store) not in out
        assert str(mine) in out
        assert cp.returncode != 0

    def test_cost_declines_instead_of_rendering_a_neighbour(self, tmp_path: Path) -> None:
        home, mine, store = self._sideways_setup(tmp_path)
        cp = _run("cost.py", home, mine)
        out = cp.stdout + cp.stderr
        assert "no sessions recorded for this directory" in out
        assert "the neighbours work" not in out
        assert str(store) not in out
        assert cp.returncode != 0

    def test_list_names_an_ancestor_substitution(self, tmp_path: Path) -> None:
        """Upward resolution is allowed but is still not the directory asked
        about, so the output has to say which store it read."""
        home = tmp_path / "fake-home"
        proj = tmp_path / "work" / "proj"
        deep = proj / "presets"
        deep.mkdir(parents=True)
        store = _store(home, proj)
        _session(store, "bbbbbbbb-0000-0000-0000-000000000000", "hello from the project")

        cp = _run("list.py", home, deep)
        out = cp.stdout + cp.stderr
        assert cp.returncode == 0
        assert "bbbbbbbb-0000-0000-0000-000000000000" in out
        assert str(proj) in out
        assert "ancestor" in out.lower()

    def test_list_direct_match_carries_no_substitution_note(self, tmp_path: Path) -> None:
        home = tmp_path / "fake-home"
        mine = tmp_path / "work" / "proj"
        mine.mkdir(parents=True)
        store = _store(home, mine)
        _session(store, "cccccccc-0000-0000-0000-000000000000", "my own work")

        cp = _run("list.py", home, mine)
        out = cp.stdout + cp.stderr
        assert cp.returncode == 0
        assert "cccccccc-0000-0000-0000-000000000000" in out
        assert "ancestor" not in out.lower()
        assert "no sessions recorded" not in out
