"""#1892: fixture-created git repos inherit the ambient core.fsmonitor and
leak a detached fsmonitor--daemon process.

Three independent measurements on one machine on 2026-08-21 (issue body and
its comment) found 965-979 orphaned `fsmonitor--daemon` processes at
`ppid 1`, and one full suite run hung at 98% for over an hour with them
present. `core.fsmonitor = true` is an ordinary user preference (the same
"ambient config bleeds into a fixture repo" shape as
test_git_status_display_config_gates_1295.py) and every fixture in this
suite creates its temp repos with a bare `subprocess.run(["git", ...])`,
inheriting whatever the pytest process's own environment holds. None of
them pin `core.fsmonitor` off, so all of them inherit it.

`GIT_CONFIG_GLOBAL` redirects git's "global" config scope to a throwaway
file for the ambient config used below -- the maintainer's real
~/.gitconfig is never opened, let alone written, matching the instruction
not to touch it.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest


def _daemon_watching(repo: Path):
    """True/False, or None if this git build cannot answer the question.

    None is the third state this repo's own defect class asks for: a git
    predating `fsmonitor--daemon status`, or one built without it, must not
    be read as "not watching" -- that is the same absence-as-answer shape
    the rest of this suite guards against everywhere else.
    """
    proc = subprocess.run(
        ["git", "fsmonitor--daemon", "status"], cwd=str(repo),
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = proc.stdout + proc.stderr
    if proc.returncode == 0 and "is watching" in out:
        return True
    if proc.returncode != 0 and "is not watching" in out:
        return False
    return None


def _stop_daemon(repo: Path) -> None:
    """Stop only the daemon this test itself spawned, by its own repo path
    -- never a machine-wide kill. Shared state belonging to other sessions
    on this machine is left alone, per the issue's own instruction."""
    subprocess.run(["git", "fsmonitor--daemon", "stop"], cwd=str(repo),
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace")


def _wait_for_daemon(repo: Path, want: bool, attempts: int = 20):
    """Poll briefly: the daemon starts asynchronously off `git add`."""
    seen = _daemon_watching(repo)
    for _ in range(attempts):
        if seen is None or seen == want:
            return seen
        time.sleep(0.25)
        seen = _daemon_watching(repo)
    return seen


@pytest.fixture
def poisoned_global(tmp_path, monkeypatch):
    """Simulate a machine with `core.fsmonitor = true` set ambiently."""
    cfg = tmp_path / "fake_global.gitconfig"
    cfg.write_text("[core]\n\tfsmonitor = true\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    return tmp_path


def _init_repo(path: Path, *, force_fsmonitor: bool = False) -> None:
    path.mkdir()
    prefix = ["git", "-c", "core.fsmonitor=true"] if force_fsmonitor else ["git"]

    def run(*args):
        subprocess.run([*prefix, *args], cwd=str(path), check=True,
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace")

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (path / "f.txt").write_text("one\n", encoding="utf-8")
    run("add", "f.txt")


def test_forced_fsmonitor_spawns_a_daemon_the_detector_can_see(poisoned_global):
    """Positive control. If forcing `-c core.fsmonitor=true` on the `git add`
    itself never spawns a daemon, the negative assertion below proves
    nothing -- a silence with no watcher listening in the first place."""
    repo = poisoned_global / "control"
    try:
        _init_repo(repo, force_fsmonitor=True)
        watching = _wait_for_daemon(repo, want=True)
        if watching is None:
            pytest.skip("this git build cannot answer fsmonitor--daemon status")
        assert watching is True, (
            "the positive control never spawned a daemon -- the detector "
            "itself cannot be trusted on this machine")
    finally:
        _stop_daemon(repo)


def test_fixture_repo_does_not_inherit_the_ambient_fsmonitor(poisoned_global):
    """The suite's own idiom -- bare `subprocess.run(["git", ...])`, no
    `-c`, inheriting whatever the pytest process environment holds -- must
    not end up watched, even though the ambient (global-scope) config says
    to watch it."""
    repo = poisoned_global / "fixture"
    try:
        _init_repo(repo, force_fsmonitor=False)
        watching = _wait_for_daemon(repo, want=False)
        if watching is None:
            pytest.skip("this git build cannot answer fsmonitor--daemon status")
        assert watching is False, (
            "a repo created the way every fixture in this suite creates one "
            "ended up watched by a detached fsmonitor--daemon, because it "
            "inherited the ambient core.fsmonitor -- this is the leak "
            "#1892 measured at up to 979 orphaned daemons on one machine")
    finally:
        _stop_daemon(repo)
