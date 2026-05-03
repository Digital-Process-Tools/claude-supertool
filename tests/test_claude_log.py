"""Tests for the claude-log preset (list / tail / summary).

Covers:
- POSIX and Windows cwd encoding (encode_cwd)
- project_dir() fallback to longest-common-prefix sibling
- list.py: ranking by mtime, line count, first user excerpt extraction
- tail.py: compact event formatting, bootstrap line, error marker
- summary.py: tool counts, error counts, final assistant text
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

PRESET_DIR = Path(__file__).resolve().parent.parent / "presets" / "claude-log"
sys.path.insert(0, str(PRESET_DIR))

import _common  # noqa: E402


# ---------- helpers ----------------------------------------------------------


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _user_text(text: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _assistant_text(text: str, *, model: str | None = None, usage: dict | None = None, ts: str | None = None) -> dict:
    msg: dict = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    if model:
        msg["model"] = model
    if usage:
        msg["usage"] = usage
    ev: dict = {"type": "assistant", "message": msg}
    if ts:
        ev["timestamp"] = ts
    return ev


def _assistant_tool(name: str, inp: dict) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": name, "input": inp}],
        },
    }


def _tool_result(content: str, is_error: bool = False) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "content": content,
                    "is_error": is_error,
                }
            ],
        },
    }


@dataclass
class FakeProject:
    cwd: Path
    home: Path
    proj_dir: Path

    def add_session(self, uuid: str, events: list[dict]) -> Path:
        path = self.proj_dir / f"{uuid}.jsonl"
        _write_jsonl(path, events)
        return path

    def run(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["USERPROFILE"] = str(self.home)  # Windows
        return subprocess.run(
            [sys.executable, str(PRESET_DIR / script), *args],
            capture_output=True,
            text=True,
            cwd=self.cwd,
            env=env,
            timeout=15,
        )


def make_project(tmp_path: Path) -> FakeProject:
    """Build an isolated home + cwd for a preset script invocation."""
    cwd = tmp_path / "work" / "proj"
    cwd.mkdir(parents=True)
    home = tmp_path / "fake-home"
    encoded = _common.encode_cwd(str(cwd))
    proj_dir = home / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True)
    return FakeProject(cwd=cwd, home=home, proj_dir=proj_dir)


# ---------- encode_cwd -------------------------------------------------------


class TestEncodeCwd:
    def test_posix_path(self) -> None:
        assert _common.encode_cwd("/Users/foo/proj") == "-Users-foo-proj"

    def test_posix_root(self) -> None:
        assert _common.encode_cwd("/") == "-"

    def test_windows_drive_path(self) -> None:
        # Backslashes and the drive colon both become hyphens.
        assert _common.encode_cwd(r"C:\Users\foo\proj") == "-C--Users-foo-proj"

    def test_windows_forward_slashes(self) -> None:
        assert _common.encode_cwd("C:/Users/foo") == "-C--Users-foo"

    def test_relative_path_gets_dash_prefix(self) -> None:
        assert _common.encode_cwd("relative/path") == "-relative-path"

    def test_already_dash_prefixed(self) -> None:
        # Idempotent on the leading dash.
        assert _common.encode_cwd("-foo-bar") == "-foo-bar"


# ---------- project_dir fallback --------------------------------------------


class TestProjectDir:
    def test_direct_match(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        encoded = _common.encode_cwd("/Users/foo/proj")
        target = tmp_path / ".claude" / "projects" / encoded
        target.mkdir(parents=True)
        assert _common.project_dir("/Users/foo/proj") == target

    def test_fallback_to_closest_sibling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        root = tmp_path / ".claude" / "projects"
        # A sibling that shares a long prefix but is not exact
        (root / "-Users-foo-proj-old").mkdir(parents=True)
        (root / "-totally-unrelated").mkdir(parents=True)
        result = _common.project_dir("/Users/foo/proj")
        assert result.name == "-Users-foo-proj-old"

    def test_returns_encoded_when_no_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # No ~/.claude/projects/ exists yet
        result = _common.project_dir("/Users/foo/proj")
        assert result.name == "-Users-foo-proj"
        assert not result.exists()


# ---------- list.py ----------------------------------------------------------


class TestList:
    def test_lists_recent_sessions(self, tmp_path: Path) -> None:
        p = make_project(tmp_path)
        a = p.add_session("aaaa", [_user_text("hello from A")])
        b = p.add_session("bbbb", [_user_text("hello from B")])
        os.utime(a, (1_700_000_000, 1_700_000_000))
        os.utime(b, (1_700_000_100, 1_700_000_100))

        r = p.run("list.py")
        assert r.returncode == 0, r.stderr + r.stdout
        # Most recent first
        assert r.stdout.index("bbbb") < r.stdout.index("aaaa")
        assert "hello from B" in r.stdout
        assert "hello from A" in r.stdout
        # Header includes Turns column
        assert "Turns" in r.stdout

    def test_turn_count_column(self, tmp_path: Path) -> None:
        p = make_project(tmp_path)
        # Session with 2 user + 1 assistant = 3 turns; bootstrap should not count
        p.add_session("turny", [
            {"type": "queue-operation", "content": "ignored"},
            _user_text("first"),
            _assistant_text("reply"),
            _user_text("follow up"),
        ])
        r = p.run("list.py")
        assert r.returncode == 0
        # Find the row for our session and verify turn count column is 3
        for line in r.stdout.splitlines():
            if line.startswith("turny"):
                # Format: UUID  WHEN  TURNS  LINES  EXCERPT
                fields = line.split()
                # fields[0]=uuid, [1]=date, [2]=time, [3]=turns, [4]=lines
                assert fields[3] == "3", f"expected 3 turns, got {fields[3]} in {line!r}"
                break
        else:
            raise AssertionError("session 'turny' not found in output")

    def test_no_sessions(self, tmp_path: Path) -> None:
        p = make_project(tmp_path)
        r = p.run("list.py")
        assert r.returncode == 0
        assert "No sessions found" in r.stdout

    def test_skips_system_reminders_in_excerpt(self, tmp_path: Path) -> None:
        p = make_project(tmp_path)
        p.add_session("abcd", [
            _user_text("<system-reminder>ignore me</system-reminder>"),
            _user_text("the real ask"),
        ])
        r = p.run("list.py")
        assert "the real ask" in r.stdout
        assert "system-reminder" not in r.stdout

    def test_limit_respected(self, tmp_path: Path) -> None:
        p = make_project(tmp_path)
        for i in range(5):
            p.add_session(f"s{i}", [_user_text(f"msg {i}")])
        r = p.run("list.py", "2")
        assert r.returncode == 0
        listed = sum(1 for line in r.stdout.splitlines() if line.startswith("s"))
        assert listed == 2


# ---------- tail.py ----------------------------------------------------------


class TestTail:
    def test_compact_output(self, tmp_path: Path) -> None:
        p = make_project(tmp_path)
        uuid = "feed-face"
        p.add_session(uuid, [
            _user_text("hi"),
            _assistant_tool("Bash", {"command": "ls"}),
            _tool_result("file1\nfile2", is_error=False),
            _assistant_text("done"),
        ])
        r = p.run("tail.py", uuid)
        assert r.returncode == 0, r.stderr + r.stdout
        out = r.stdout
        assert "[user] TEXT: hi" in out
        assert "[assistant] TOOL Bash:" in out
        assert "[result]" in out
        assert "[assistant] TEXT: done" in out

    def test_marks_errors(self, tmp_path: Path) -> None:
        p = make_project(tmp_path)
        uuid = "errs"
        p.add_session(uuid, [
            _assistant_tool("Bash", {"command": "false"}),
            _tool_result("boom", is_error=True),
        ])
        r = p.run("tail.py", uuid)
        assert "[result/ERR]" in r.stdout

    def test_session_not_found(self, tmp_path: Path) -> None:
        p = make_project(tmp_path)
        p.add_session("present", [_user_text("x")])
        r = p.run("tail.py", "nonexistent-uuid")
        assert r.returncode == 1
        assert "session not found" in r.stdout

    def test_n_argument(self, tmp_path: Path) -> None:
        p = make_project(tmp_path)
        uuid = "many"
        p.add_session(uuid, [_assistant_text(f"line {i}") for i in range(10)])
        r = p.run("tail.py", uuid, "3")
        lines = [ln for ln in r.stdout.splitlines() if ln.startswith("[assistant]")]
        assert len(lines) == 3
        assert "line 9" in r.stdout
        assert "line 0" not in r.stdout

    def test_bootstrap_line(self, tmp_path: Path) -> None:
        p = make_project(tmp_path)
        uuid = "boot"
        p.add_session(uuid, [
            {"type": "queue-operation", "content": "bootstrap prompt content"},
            _user_text("first ask"),
        ])
        r = p.run("tail.py", uuid)
        assert "[bootstrap]" in r.stdout
        assert "bootstrap prompt content" in r.stdout


# ---------- summary.py -------------------------------------------------------


class TestSummary:
    def test_counts_and_final_text(self, tmp_path: Path) -> None:
        p = make_project(tmp_path)
        uuid = "sum"
        p.add_session(uuid, [
            _user_text("kick off the work"),
            _assistant_tool("Bash", {"command": "ls"}),
            _tool_result("ok"),
            _assistant_tool("Bash", {"command": "pwd"}),
            _tool_result("/tmp"),
            _assistant_tool("Read", {"file_path": "/x"}),
            _tool_result("err", is_error=True),
            _assistant_text("done summary"),
        ])
        r = p.run("summary.py", uuid)
        assert r.returncode == 0, r.stderr + r.stdout
        out = r.stdout
        assert "Tool calls:      3" in out
        assert "Tool errors:     1" in out
        assert "2  Bash" in out
        assert "1  Read" in out
        assert "kick off the work" in out
        assert "done summary" in out

    def test_session_not_found(self, tmp_path: Path) -> None:
        p = make_project(tmp_path)
        p.add_session("present", [_user_text("x")])
        r = p.run("summary.py", "nope")
        assert r.returncode == 1

    def test_model_duration_and_tokens(self, tmp_path: Path) -> None:
        p = make_project(tmp_path)
        uuid = "tok"
        p.add_session(uuid, [
            _assistant_text(
                "msg",
                model="claude-opus-4-7",
                usage={
                    "input_tokens": 10,
                    "output_tokens": 200,
                    "cache_read_input_tokens": 1500,
                    "cache_creation_input_tokens": 500,
                },
                ts="2026-04-29T20:38:00.000Z",
            ),
            _assistant_text(
                "msg2",
                usage={
                    "input_tokens": 20,
                    "output_tokens": 100,
                    "cache_read_input_tokens": 1500,
                    "cache_creation_input_tokens": 0,
                },
                ts="2026-04-29T20:40:09.000Z",  # 2m 9s later
            ),
        ])
        r = p.run("summary.py", uuid)
        assert r.returncode == 0, r.stderr + r.stdout
        out = r.stdout
        assert "Model:           claude-opus-4-7" in out
        assert "Duration:        2m 9s" in out
        # 30 input + 300 output + 3000 cache_read + 500 cache_creation
        assert "input:          30" in out
        assert "output:         300" in out
        assert "cache read:     3.0k" in out
        assert "cache create:   500" in out
        # Cache hit = 3000 / (3000 + 500) = 85.7%
        assert "cache hit:      85.7%" in out

    def test_per_tool_error_counts(self, tmp_path: Path) -> None:
        p = make_project(tmp_path)
        uuid = "errsplit"
        p.add_session(uuid, [
            _assistant_tool("Bash", {"command": "ls"}),
            _tool_result("ok"),
            _assistant_tool("Edit", {"file_path": "/x"}),
            _tool_result("not read", is_error=True),
            _assistant_tool("Bash", {"command": "false"}),
            _tool_result("boom", is_error=True),
        ])
        r = p.run("summary.py", uuid)
        out = r.stdout
        # Bash: 2 calls, 1 error. Edit: 1 call, 1 error.
        assert "2  Bash (1 err)" in out
        assert "1  Edit (1 err)" in out


# ---------- session_path cross-project lookup ------------------------------


class TestSessionPathCrossProject:
    """session_path() should prefer the current project, but fall back to
    scanning all projects under ~/.claude/projects/ when the UUID isn't found
    locally. This covers worktree / multi-checkout setups where a session
    lives under a sibling project's directory."""

    def test_finds_session_in_current_project(self, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
        p = make_project(tmp_path)
        uuid = "abc-current"
        p.add_session(uuid, [_user_text("hi")])
        monkeypatch.setenv("HOME", str(p.home))
        monkeypatch.setenv("USERPROFILE", str(p.home))
        monkeypatch.chdir(p.cwd)
        path = _common.session_path(uuid)
        assert path == p.proj_dir / f"{uuid}.jsonl"
        assert path.is_file()

    def test_falls_back_to_sibling_project(self, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
        # Build two projects under one fake home; session lives in the OTHER one.
        home = tmp_path / "fake-home"
        cwd_a = tmp_path / "work" / "proj-a"
        cwd_b = tmp_path / "work" / "proj-b"
        cwd_a.mkdir(parents=True)
        cwd_b.mkdir(parents=True)
        proj_a = home / ".claude" / "projects" / _common.encode_cwd(str(cwd_a))
        proj_b = home / ".claude" / "projects" / _common.encode_cwd(str(cwd_b))
        proj_a.mkdir(parents=True)
        proj_b.mkdir(parents=True)
        uuid = "lives-in-b"
        _write_jsonl(proj_b / f"{uuid}.jsonl", [_user_text("hello")])
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.chdir(cwd_a)
        path = _common.session_path(uuid)
        # Should resolve to project B even though we're cwd'd in project A.
        assert path == proj_b / f"{uuid}.jsonl"
        assert path.is_file()

    def test_missing_uuid_returns_direct_path(self, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
        """When UUID is nowhere, return the direct (current-project) path so
        the caller's `if not sp.exists()` error message points at the expected
        location, not some random sibling."""
        p = make_project(tmp_path)
        monkeypatch.setenv("HOME", str(p.home))
        monkeypatch.setenv("USERPROFILE", str(p.home))
        monkeypatch.chdir(p.cwd)
        path = _common.session_path("does-not-exist")
        assert path == p.proj_dir / "does-not-exist.jsonl"
        assert not path.exists()

    def test_skips_non_directory_entries(self, tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
        """Stray files under ~/.claude/projects/ shouldn't break the scan."""
        p = make_project(tmp_path)
        # Create a stray file alongside project directories.
        (p.home / ".claude" / "projects" / "stray.txt").write_text("noise")
        # Plus a real session in a sibling project.
        sibling_cwd = tmp_path / "work" / "sibling"
        sibling_cwd.mkdir(parents=True)
        sibling_dir = (p.home / ".claude" / "projects"
                       / _common.encode_cwd(str(sibling_cwd)))
        sibling_dir.mkdir(parents=True)
        uuid = "sib-uuid"
        _write_jsonl(sibling_dir / f"{uuid}.jsonl", [_user_text("hi")])
        monkeypatch.setenv("HOME", str(p.home))
        monkeypatch.setenv("USERPROFILE", str(p.home))
        monkeypatch.chdir(p.cwd)
        path = _common.session_path(uuid)
        assert path == sibling_dir / f"{uuid}.jsonl"
