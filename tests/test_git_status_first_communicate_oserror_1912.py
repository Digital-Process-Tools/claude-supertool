"""The outer `run()`'s FIRST `communicate()` also drops an `OSError` (#1912).

PR #1883 fixed the exact `communicate()`-fails-but-the-child-is-alive shape
inside `_settled`/`_stop`, which `_stop` uses on a `TimeoutExpired`. But
`run()`'s own `communicate(timeout=remaining)` call -- the one every one of
the four `git` calls this adapter makes goes through -- only ever caught
`subprocess.TimeoutExpired`. An `OSError` there (reproduced against a real
child in the issue: closing a live child's stdout fd under a running
`sleep 5` raises `OSError(9, "Bad file descriptor")` with `p.poll() is
None`) propagated straight past `main()`'s `except _NoAnswer`, so `_stop`
was never called and a `git status` holding `.git/index.lock` was left
running.

The control is the pair, in the same fixture: a genuinely dead child and an
unreadable-but-alive one. A fix that routes every `OSError` straight to
`_stop` (skipping the `_settled` check `_settled`/`_stop` themselves make)
passes a test that only exercises the alive case -- `test_...already_dead`
below is what catches that.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ADAPTER = REPO / "validators" / "git-status" / "git-status.py"


def _load() -> object:
    spec = importlib.util.spec_from_file_location("git_status_under_test_1912", ADAPTER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _OSErrorProcess:
    """`communicate()` always raises `OSError` -- the broken-pipe shape.

    `already_dead` decides what `wait()` (the fallback `_settled` reaches
    for) reports: a child that already exited answers `wait()` immediately;
    a child still running keeps raising `TimeoutExpired` from `wait()`
    until `terminate()`/`kill()` is called, exactly like `_StalledProcess`
    in `tests/test_git_status_validator_timeout_1882.py`.
    """

    def __init__(self, already_dead: bool) -> None:
        self.args = ["git"]
        self.returncode = 0 if already_dead else None
        self._already_dead = already_dead
        self.signals: list = []

    def communicate(self, timeout=None):  # noqa: ANN001
        raise OSError(9, "Bad file descriptor")

    def wait(self, timeout=None):  # noqa: ANN001
        if self._already_dead or "kill" in self.signals:
            self.returncode = 0
            return 0
        raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout or 0)

    def terminate(self) -> None:
        self.signals.append("terminate")

    def kill(self) -> None:
        self.signals.append("kill")

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def _drive(monkeypatch: pytest.MonkeyPatch, target: Path,
          already_dead: bool) -> "tuple[dict, list]":
    mod = _load()
    children: list = []

    def _popen(*_args, **_kwargs):
        proc = _OSErrorProcess(already_dead)
        children.append(proc)
        return proc

    monkeypatch.setattr(mod.subprocess, "Popen", _popen)
    monkeypatch.setattr(mod.shutil, "which", lambda _n: "/usr/bin/git")
    monkeypatch.setattr(mod.sys, "argv", [str(ADAPTER), str(target)])

    emitted: list = []
    monkeypatch.setattr("builtins.print",
                        lambda *a, **k: emitted.append(" ".join(map(str, a))))
    mod.main()
    payloads = [json.loads(line) for line in emitted if line.strip().startswith("{")]
    assert payloads, "the adapter emitted no JSON when communicate() raised OSError"
    return payloads[-1], children


def _git(repo: Path, *args: str) -> None:
    import os
    subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.com"},
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t.com")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "base.txt").write_text("line1\n", encoding="utf-8")
    _git(tmp_path, "add", "base.txt")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


# ---------------------------------------------------------------------------
# Positive control: a real clean file still reports clean, unmocked. Without
# this, "declines rather than reporting clean" would pass on a harness that
# never actually drove the adapter.
# ---------------------------------------------------------------------------

def test_a_real_clean_file_still_reports_clean(repo: Path) -> None:
    """MUST FIRE."""
    proc = subprocess.run(
        [sys.executable, str(ADAPTER), str(repo / "base.txt")],
        capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload["ok"] is True, payload
    assert payload["metrics"]["state"] == "clean", payload


# ---------------------------------------------------------------------------
# The pair: an alive-but-unreadable child, and a genuinely dead one.
# ---------------------------------------------------------------------------

def test_an_unreadable_but_alive_child_is_stopped_not_left_running(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST FIRE. The bug: `.git/index.lock` left stranded under a live child.

    `wait()` keeps timing out (child alive) until `terminate()` is called,
    so this asserts `_stop`'s escalation actually reached the process --
    not just that the adapter declined.
    """
    payload, children = _drive(monkeypatch, repo / "base.txt", already_dead=False)

    assert payload["ok"] is False, payload
    assert payload["errors"][0]["code"] == "adapter", payload
    assert children[0].signals and children[0].signals[0] == "terminate", (
        "the alive child was never asked to stop", children[0].signals)


def test_an_already_dead_child_is_not_terminated_again(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST NOT FIRE. The control half of the pair.

    A fix that routes every `OSError` straight to `_stop` -- skipping the
    `_settled` check that `_stop` itself is built around -- passes the test
    above but also sends `terminate()` to a process that is already gone.
    `_settled`'s own `wait()` fallback should see the child has already
    exited and never reach `_stop` at all.
    """
    payload, children = _drive(monkeypatch, repo / "base.txt", already_dead=True)

    assert payload["ok"] is False, payload
    assert payload["errors"][0]["code"] == "adapter", payload
    assert children[0].signals == [], (
        "an already-dead child was sent a stop signal it never needed",
        children[0].signals)


def test_the_decline_names_the_real_cause_not_a_timeout(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken-pipe decline must not say `timed out` -- that sends the
    reader to raise a budget that was never the problem (mirrors
    `test_a_spawn_failure_is_not_reported_as_a_timeout` in #1882's suite).
    """
    payload, _children = _drive(monkeypatch, repo / "base.txt", already_dead=True)

    msg = payload["errors"][0]["msg"]
    assert "timed out" not in msg.lower(), msg
    assert "OSError" in msg, msg
