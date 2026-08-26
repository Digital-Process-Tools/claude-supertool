"""`transport.repo_slug()` and `emit_event`'s `repo` propagation (#1952).

A watch event carried the PR number and never the repository, so a desktop
notification or a channel tag read as `github-pr 527: merged` — ambiguous
across every repository. The repository is recoverable from the poller's own
`git remote`, not from the forge object being polled, so it is resolved once
per poller process and carried on the envelope the same way `first_tick` is —
never inside `payload`, which is a forge object's own words.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

_spec = importlib.util.spec_from_file_location("watch_transport_1952", WATCH_DIR / "transport.py")
assert _spec is not None and _spec.loader is not None
transport = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(transport)


def _run(returncode: int, stdout: str):
    result = mock.Mock()
    result.returncode = returncode
    result.stdout = stdout
    return result


@pytest.mark.parametrize("url, expected", [
    ("https://github.com/OWNER/REPO.git", "OWNER/REPO"),
    ("https://github.com/OWNER/REPO", "OWNER/REPO"),
    ("git@github.com:OWNER/REPO.git", "OWNER/REPO"),
    ("git@gitlab.example.com:group/subgroup/project.git", "group/subgroup/project"),
    ("https://gitlab.example.com/group/subgroup/project.git", "group/subgroup/project"),
    ("ssh://git@github.com/OWNER/REPO.git", "OWNER/REPO"),
])
def test_repo_slug_parses_the_ordinary_remote_shapes(monkeypatch, url, expected) -> None:
    monkeypatch.setattr(transport.subprocess, "run", lambda *a, **kw: _run(0, url + "\n"))
    assert transport.repo_slug() == expected


def test_repo_slug_is_empty_not_a_guess_when_git_fails(monkeypatch) -> None:
    """The must-fire pair for the must-not-fire cases above: a repository this
    call could not resolve stays empty, and callers already treat that as
    'unknown' — never as an invented or default name."""
    monkeypatch.setattr(transport.subprocess, "run", lambda *a, **kw: _run(1, ""))
    assert transport.repo_slug() == ""


def test_repo_slug_is_empty_when_git_is_unavailable(monkeypatch) -> None:
    def _raise(*a, **kw):
        raise FileNotFoundError("git")
    monkeypatch.setattr(transport.subprocess, "run", _raise)
    assert transport.repo_slug() == ""


def test_repo_slug_is_empty_on_timeout(monkeypatch) -> None:
    def _raise(*a, **kw):
        raise transport.subprocess.TimeoutExpired(cmd="git", timeout=5)
    monkeypatch.setattr(transport.subprocess, "run", _raise)
    assert transport.repo_slug() == ""


def test_repo_slug_honours_a_supertool_repo_target(monkeypatch) -> None:
    """A watcher started under a `repo:` target (`SUPERTOOL_REPO`) queries
    that repository, not the cwd's -- `_head_commit`/`_run_list` already
    route through `presets/_repo_target.py` for it. `repo_slug()` reading the
    cwd's `git remote` regardless would attribute the event to the wrong
    repository: not merely absent, actively wrong, which is the one failure
    #1952 was filed to eliminate. Reading `_gh` at all must not even be
    attempted once a target is set — asserted here by leaving `git config`
    unmocked and confirming it is never reached."""
    monkeypatch.setenv("SUPERTOOL_REPO", "OTHER/REPO")

    def _fail_if_called(*a, **kw):
        raise AssertionError("git must not be shelled out to under a repo: target")
    monkeypatch.setattr(transport.subprocess, "run", _fail_if_called)
    assert transport.repo_slug() == "OTHER/REPO"


def test_repo_slug_falls_back_to_the_cwd_remote_with_no_target(monkeypatch) -> None:
    """The positive control for the case above: with no target set, the cwd's
    own remote is still read exactly as before."""
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    monkeypatch.setattr(transport.subprocess, "run",
                        lambda *a, **kw: _run(0, "https://github.com/OWNER/REPO.git\n"))
    assert transport.repo_slug() == "OWNER/REPO"


# ---------------------------------------------------------------------------
# emit_event carries repo the same way it carries first_tick
# ---------------------------------------------------------------------------

def test_emit_event_with_a_repo_carries_it_on_the_record(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(transport, "SOCK_PATH", str(tmp_path / "nonexistent.sock"))
    transport.emit_event("github-pr", "527", "merged", {"title": "x"},
                         repo="OWNER/REPO")
    state = transport.read_state("github-pr", "527")
    assert state["last_event"]["repo"] == "OWNER/REPO"


def test_emit_event_without_a_repo_omits_it_rather_than_writing_blank(
        monkeypatch, tmp_path) -> None:
    """The must-fire pair: an unresolved repository must not appear on the
    record as an empty string, which `channel.ts` would coerce to a real,
    if useless, attribute rather than leaving it absent."""
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(transport, "SOCK_PATH", str(tmp_path / "nonexistent.sock"))
    transport.emit_event("github-pr", "527", "merged", {"title": "x"})
    state = transport.read_state("github-pr", "527")
    assert "repo" not in state["last_event"]
