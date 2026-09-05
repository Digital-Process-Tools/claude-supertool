"""#1337 -- session_path() finds a UUID in a neighbour's store and says nothing
about it.

#1317 made the sibling store-RESOLUTION codepath (resolve_project_dir /
project_dir, used when no UUID is given) walk upward only and decline
sideways. session_path() was deliberately left unchanged: given a bare UUID it
scans every store under ~/.claude/projects and returns the first match, which
is correct -- a session id is globally unique -- but nothing in the return
value says which store answered when that store is not the caller's own.

This is a disclosure gap, not a resolution bug: the fix must keep finding the
session in the neighbour's store (unlike #1317, which declines), and must
additionally say so.
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
    env["USERPROFILE"] = str(home)
    return subprocess.run(
        [sys.executable, str(PRESET_DIR / script), *args],
        capture_output=True, text=True, cwd=str(cwd), env=env,
        timeout=60, encoding="utf-8", errors="replace",
    )


class TestSessionPathDisclosesCrossStoreHit:
    def test_returns_a_note_naming_the_other_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "fake-home"
        wt = tmp_path / "Documents" / "st-wt"
        mine = wt / "1337"
        sibling = wt / "1069"
        mine.mkdir(parents=True)
        sibling.mkdir(parents=True)
        store = _store(home, sibling)
        uuid = "aaaaaaaa-0000-0000-0000-000000000000"
        expected = _session(store, uuid, "the neighbours work")

        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.chdir(mine)

        path, note = _common.session_path(uuid)
        assert path == expected
        assert note != ""
        assert str(store) in note

    def test_own_store_hit_carries_no_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "fake-home"
        mine = tmp_path / "work" / "proj"
        mine.mkdir(parents=True)
        store = _store(home, mine)
        uuid = "own-session"
        expected = _session(store, uuid, "my own work")

        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.chdir(mine)

        path, note = _common.session_path(uuid)
        assert path == expected
        assert note == ""

    def test_missing_uuid_carries_no_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "fake-home"
        mine = tmp_path / "work" / "proj"
        mine.mkdir(parents=True)
        _store(home, mine)

        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.chdir(mine)

        path, note = _common.session_path("does-not-exist")
        assert not path.exists()
        assert note == ""

    def test_tail_discloses_the_cross_store_hit(self, tmp_path: Path) -> None:
        home = tmp_path / "fake-home"
        wt = tmp_path / "Documents" / "st-wt"
        mine = wt / "1337"
        sibling = wt / "1069"
        mine.mkdir(parents=True)
        sibling.mkdir(parents=True)
        store = _store(home, sibling)
        uuid = "bbbbbbbb-0000-0000-0000-000000000000"
        _session(store, uuid, "hello from the neighbour")

        cp = _run("tail.py", home, mine, uuid)
        out = cp.stdout + cp.stderr
        assert cp.returncode == 0
        assert str(store) in out

    def test_summary_discloses_the_cross_store_hit(self, tmp_path: Path) -> None:
        home = tmp_path / "fake-home"
        wt = tmp_path / "Documents" / "st-wt"
        mine = wt / "1337"
        sibling = wt / "1069"
        mine.mkdir(parents=True)
        sibling.mkdir(parents=True)
        store = _store(home, sibling)
        uuid = "cccccccc-0000-0000-0000-000000000000"
        _session(store, uuid, "hello from the neighbour")

        cp = _run("summary.py", home, mine, uuid)
        out = cp.stdout + cp.stderr
        assert cp.returncode == 0
        assert str(store) in out

    def test_cost_discloses_the_cross_store_hit(self, tmp_path: Path) -> None:
        home = tmp_path / "fake-home"
        wt = tmp_path / "Documents" / "st-wt"
        mine = wt / "1337"
        sibling = wt / "1069"
        mine.mkdir(parents=True)
        sibling.mkdir(parents=True)
        store = _store(home, sibling)
        uuid = "dddddddd-0000-0000-0000-000000000000"
        _session(store, uuid, "hello from the neighbour")

        cp = _run("cost.py", home, mine, uuid)
        out = cp.stdout + cp.stderr
        assert cp.returncode == 0
        assert str(store) in out

    def test_own_store_hit_via_ops_carries_no_note(self, tmp_path: Path) -> None:
        """A direct hit must not gain a spurious cross-store disclosure."""
        home = tmp_path / "fake-home"
        mine = tmp_path / "work" / "proj"
        mine.mkdir(parents=True)
        store = _store(home, mine)
        uuid = "eeeeeeee-0000-0000-0000-000000000000"
        _session(store, uuid, "my own work")

        cp = _run("tail.py", home, mine, uuid)
        out = cp.stdout + cp.stderr
        assert cp.returncode == 0
        assert "cross" not in out.lower()
