"""gh-issue renders a `classify:` verdict beside the fence banner (#2049).

Loads the real presets/github/issue.py the way test_github_issue.py already
does, and stubs the classify model spawn the same way
test_classify_model_2046.py does -- nothing here shells out to a real
`claude -p`. The bar: `could-not-classify` (and this module's own `off` /
`scanner-clean` states) must never render as `classify: safe`.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PRESET_PATH = Path(__file__).parent.parent / "presets" / "github" / "issue.py"
_spec = importlib.util.spec_from_file_location("github_issue_classify", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
issue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issue)


class _Proc:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _fake_gh(stdout: str) -> Any:
    return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=stdout, stderr="")


def _install(monkeypatch, tmp_path, *, body: str, comments: list[dict],
             spawn_stdout: str = "SAFE") -> None:
    issue_payload = json.dumps({
        "number": 42, "title": "Plan", "state": "OPEN", "labels": [],
        "milestone": None, "assignees": [], "author": {"login": "florian"},
        "url": "", "body": body, "comments": comments,
    })
    pr_payload = json.dumps([])

    def fake_gh(args, timeout=10):
        if args and args[0] == "pr":
            return _fake_gh(pr_payload)
        return _fake_gh(issue_payload)

    monkeypatch.setattr(issue, "_gh", fake_gh)
    monkeypatch.setattr(issue, "_download_images", lambda urls, n: [])

    def spawn(prompt, system_prompt, timeout):
        return _Proc(stdout=spawn_stdout)
    monkeypatch.setattr(issue._classify_render.model, "_default_spawn", spawn)

    # #2097 wired a real, file-backed verdict cache into every `Budget()`
    # this op builds. Several tests here classify the literal string "an
    # ordinary bug report" under different `spawn_stdout`s, and the real
    # on-disk default (shared by uid, not by test) would let an earlier
    # test's cached "safe" answer a later test that expects
    # `could-not-classify` -- reproduced once as a real failure before this
    # fixture was given its own `tmp_path`-rooted cache per test, the same
    # isolation `tests/test_classify_cache_2054.py` already gives
    # `cache.py`'s own tests directly.
    fresh_cache = issue._classify_render.classify_cache.Cache(
        directory=str(tmp_path / "classify-cache"))
    monkeypatch.setattr(issue._classify_render.classify_cache, "default_cache",
                         lambda: fresh_cache)


def _comment(login: str, body: str) -> dict:
    return {"author": {"login": login}, "body": body, "createdAt": "2026-05-07T20:00:00Z"}


def test_body_gets_a_classify_line(monkeypatch, capsys, tmp_path) -> None:
    _install(monkeypatch, tmp_path, body="an ordinary bug report", comments=[])
    monkeypatch.setattr(sys, "argv", ["issue.py", "42"])
    monkeypatch.setattr(issue, "_CLASSIFY_LEVEL", issue._classify_render.LEVEL_FULL)
    rc = issue.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "classify: safe" in out


def test_each_comment_gets_its_own_classify_line(monkeypatch, capsys, tmp_path) -> None:
    _install(monkeypatch, tmp_path, body="body text",
             comments=[_comment("a", "first comment"), _comment("b", "second comment")])
    monkeypatch.setattr(sys, "argv", ["issue.py", "42"])
    monkeypatch.setattr(issue, "_CLASSIFY_LEVEL", issue._classify_render.LEVEL_FULL)
    rc = issue.main()
    out = capsys.readouterr().out
    assert rc == 0
    # One for the body, one for each of the two comments.
    assert out.count("classify: safe") == 3


def test_could_not_classify_never_renders_as_safe(monkeypatch, capsys, tmp_path) -> None:
    _install(monkeypatch, tmp_path, body="an ordinary bug report", comments=[],
             spawn_stdout="I refuse to answer in the fixed vocabulary.")
    monkeypatch.setattr(sys, "argv", ["issue.py", "42"])
    monkeypatch.setattr(issue, "_CLASSIFY_LEVEL", issue._classify_render.LEVEL_FULL)
    rc = issue.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "classify: could-not-classify" in out
    assert "classify: safe" not in out


def test_off_level_never_renders_safe_and_never_spawns(monkeypatch, capsys, tmp_path) -> None:
    calls = []
    _install(monkeypatch, tmp_path, body="an ordinary bug report", comments=[])

    def spy(prompt, system_prompt, timeout):
        calls.append(1)
        return _Proc(stdout="SAFE")
    monkeypatch.setattr(issue._classify_render.model, "_default_spawn", spy)
    monkeypatch.setattr(sys, "argv", ["issue.py", "42"])
    monkeypatch.setattr(issue, "_CLASSIFY_LEVEL", issue._classify_render.LEVEL_OFF)
    rc = issue.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert calls == []
    assert "classify: off" in out
    assert "classify: safe" not in out
