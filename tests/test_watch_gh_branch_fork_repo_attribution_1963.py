"""#1963: `transport.repo_slug()` falls back to `origin`'s remote while the
`gh-branch` poller resolves the repository through `gh` itself
(`branch._repo_identity()`), which honours `remote.<name>.gh-resolved`. In a
fork checkout the two disagree -- the poller polls upstream, the dispatcher's
generic `repo_slug()` reads the fork off `origin` -- and the event was
stamped with the fork's name while every `gh` call it describes ran against
upstream.

`tests/test_watch_transport_repo_attribution_1952.py` monkeypatches `git
config` in every one of its cases and never constructs a checkout where gh's
own resolution differs from it, so it has no negative control on the axis
this bug lives on. This file builds that axis directly: it mocks the two
resolvers to different values and checks which one wins, at both of the two
seams involved --

  * `poller.py`'s `_snapshot`/`poll()`, which already asks `gh` which
    repository `_head_commit`/`_run_list` just queried and used to discard
    the answer;
  * `dispatcher.py`'s `_run_poll_loop`, which reads `transport.repo_slug()`
    once per process and must not let that generic, git-config-based answer
    override a source that already knows better.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

POLLER = WATCH_DIR / "sources" / "gh-branch" / "poller.py"
_p_spec = importlib.util.spec_from_file_location("gh_branch_poller_1963", POLLER)
assert _p_spec is not None and _p_spec.loader is not None
poller = importlib.util.module_from_spec(_p_spec)
_p_spec.loader.exec_module(poller)
branch = poller.branch

_d_spec = importlib.util.spec_from_file_location("watch_dispatcher_1963", WATCH_DIR / "dispatcher.py")
assert _d_spec is not None and _d_spec.loader is not None
dispatcher = importlib.util.module_from_spec(_d_spec)
_d_spec.loader.exec_module(dispatcher)


def _proc(returncode=0, stdout="", stderr=""):
    r = mock.Mock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def _fake_gh_for(repo: str):
    """gh answers as though its own base-repo resolution landed on `repo` --
    the fork scenario: `remote.upstream.gh-resolved = base` means `gh repo
    view` names the parent while `origin`'s own remote URL still names the
    fork."""
    def _call(argv, timeout=20):
        if argv[:2] == ["gh", "api"] and "commits/" in argv[2]:
            return _proc(0, json.dumps({
                "sha": "c391c1333b6793f4fc2e5a2cc830024fd834ffe1",
                "commit": {"committer": {"date": "2026-08-25T12:00:00Z"}},
            }))
        if argv[:3] == ["gh", "run", "list"]:
            return _proc(0, "[]")
        if argv[:3] == ["gh", "repo", "view"]:
            return _proc(0, json.dumps({"nameWithOwner": repo,
                                        "defaultBranchRef": {"name": "main"}}))
        raise AssertionError(f"unexpected gh call: {argv}")
    return _call


def _ctx(ref="main"):
    return {"source": "gh-branch", "id": ref, "only": []}


# ---------------------------------------------------------------------------
# poller.py: the event it builds must carry gh's own answer
# ---------------------------------------------------------------------------

def test_snapshot_reports_the_gh_resolved_repo_not_the_cwds_origin() -> None:
    """The negative control the existing #1952 suite never built: gh's own
    resolution (`Digital-Process-Tools/claude-supertool`, the upstream a fork
    checkout's `gh repo set-default` points at) must be what `_snapshot`
    reports, regardless of what a bare `git config --get remote.origin.url`
    would answer for the same checkout (the fork, `mefork/claude-supertool`)."""
    with mock.patch.object(branch, "_gh",
                           side_effect=_fake_gh_for("Digital-Process-Tools/claude-supertool")):
        _state, _sentence, _sha, repo, error = poller._snapshot("main")
    assert error == "", error
    assert repo == "Digital-Process-Tools/claude-supertool", repo


def test_poll_event_carries_the_gh_resolved_repo() -> None:
    with mock.patch.object(branch, "_gh",
                           side_effect=_fake_gh_for("upstream/owner")):
        events, _new_state = poller.poll({}, _ctx())
    assert len(events) == 1, events
    assert events[0].get("repo") == "upstream/owner", events[0]


# ---------------------------------------------------------------------------
# dispatcher.py: a source-supplied repo must win over the process-level,
# git-config-derived transport.repo_slug()
# ---------------------------------------------------------------------------

class _ForkAwarePoller:
    """A stand-in for gh-branch: reaches a terminal state on the first tick
    and reports a repo the process-level resolver disagrees with."""
    INTERVAL = 1

    @staticmethod
    def poll(state, ctx):
        return ([{"event": "went_green",
                  "payload": {"sentence": "GREEN"},
                  "repo": "Digital-Process-Tools/claude-supertool"}],
                {"done": True})

    @staticmethod
    def is_terminal(state):
        return state.get("done") is True


class _OrdinaryPoller:
    """The positive control: a source that never names its own repo must
    keep getting the process-level attribution exactly as before."""
    INTERVAL = 1

    @staticmethod
    def poll(state, ctx):
        return ([{"event": "merged", "payload": {}}], {"done": True})

    @staticmethod
    def is_terminal(state):
        return state.get("done") is True


def _reap(pid: int, budget: float = 30.0):
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return status
        time.sleep(0.01)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    return None


def _capture_emissions(monkeypatch, tmp_path):
    log = tmp_path / "emitted.jsonl"

    def _capture(record):
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return dispatcher.transport.Emit(dispatcher.transport.EMIT_ACCEPTED, "captured")

    monkeypatch.setattr(dispatcher.transport, "emit_socket", _capture)
    return log


def _read_emissions(log):
    if not log.exists():
        return []
    return [json.loads(line) for line in
            log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run_loop_in_child(monkeypatch, tmp_path, poller_mod, process_repo: str) -> None:
    monkeypatch.setattr(dispatcher.transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(dispatcher, "_load_source", lambda name: poller_mod)
    # The mocked stand-in for `git config --get remote.origin.url` -- a
    # different repository from the one the stub poller reports, which is
    # the whole of the disagreement #1963 is about.
    monkeypatch.setattr(dispatcher.transport, "repo_slug", lambda: process_repo)
    pid = os.fork()
    if pid == 0:
        try:
            dispatcher._run_poll_loop("gh-branch", "main", [])
        finally:
            os._exit(0)
    status = _reap(pid)
    assert status is not None, "forked child did not exit"
    assert os.WIFEXITED(status)


@pytest.mark.skipif(sys.platform == "win32", reason="requires os.fork")
def test_a_sources_own_repo_overrides_the_process_level_slug(monkeypatch, tmp_path) -> None:
    log = _capture_emissions(monkeypatch, tmp_path)
    _run_loop_in_child(monkeypatch, tmp_path, _ForkAwarePoller,
                       process_repo="mefork/claude-supertool")
    records = _read_emissions(log)
    assert records, "the stub emits on its first tick -- an empty log means the loop never ran"
    assert records[0]["repo"] == "Digital-Process-Tools/claude-supertool", records[0]


@pytest.mark.skipif(sys.platform == "win32", reason="requires os.fork")
def test_a_source_with_no_opinion_still_gets_the_process_level_slug(monkeypatch, tmp_path) -> None:
    """The must-fire positive control: fixing #1963 must not silently drop
    repo attribution for every other watch source."""
    log = _capture_emissions(monkeypatch, tmp_path)
    _run_loop_in_child(monkeypatch, tmp_path, _OrdinaryPoller,
                       process_repo="OWNER/REPO")
    records = _read_emissions(log)
    assert records, "the stub emits on its first tick -- an empty log means the loop never ran"
    assert records[0]["repo"] == "OWNER/REPO", records[0]
