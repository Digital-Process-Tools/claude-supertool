"""A push timeout must say whether the clock was the network or the hook (#1242).

`_report_push_timeout` is the receipt this issue explicitly did not want
weakened: it refuses to infer success, asks the remote, names both shas, and
tells the reader not to force-push. All of that stays.

What it never said is *where the time went*. The success arm already hedges —
"slow pre-push hook or transfer" — but the failing arm, the one a reader acts
on, offers only "the push may still be in flight". For a push whose local
pre-push hook was still running the suite when the budget expired, nothing was
ever in flight, and "in flight" is the reading that sends someone to check the
network.

`.githooks/pre-push` runs the full suite (~296s measured) when the destination
is `master`/`main`, against a 300s budget. So the two causes are four seconds
apart and the receipt could not tell them apart.

Three states, per docs/validators.md §"Declining instead of guessing" — the
point is that `unknown` is a real answer and must never render as `none`:

* `runs` — an executable pre-push hook is what `git push` would run here;
* `none` — `--no-verify` was passed, or there is no executable hook;
* `unknown` — git did not answer where the hook lives.

The verdict itself is untouched by all three. This changes where the reader
looks, not what the receipt concludes.
"""
from __future__ import annotations

import importlib.util
import io
import stat
import subprocess
import types
from contextlib import redirect_stdout
from pathlib import Path

import pytest

PRESET = Path(__file__).parent.parent / "presets" / "git" / "push.py"
_spec = importlib.util.spec_from_file_location("git_push_1242", PRESET)
assert _spec is not None and _spec.loader is not None
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)


def _repo(tmp_path: Path) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True,
                   capture_output=True)
    return work


def _install_hook(work: Path, hooks_dir: str = ".githooks") -> Path:
    subprocess.run(["git", "config", "core.hooksPath", hooks_dir], cwd=work,
                   check=True, capture_output=True)
    d = work / hooks_dir
    d.mkdir(parents=True, exist_ok=True)
    hook = d / "pre-push"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    return hook


# ---------------------------------------------------------------------------
# the state helper
# ---------------------------------------------------------------------------

def test_an_executable_hook_reads_as_runs(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    work = _repo(tmp_path)
    _install_hook(work)
    monkeypatch.chdir(work)
    state, detail = push._prepush_hook_state(set())
    assert state == "runs", detail
    assert "pre-push" in detail


def test_no_hook_reads_as_none(tmp_path: Path,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    work = _repo(tmp_path)
    monkeypatch.chdir(work)
    state, _detail = push._prepush_hook_state(set())
    assert state == "none"


def test_no_verify_reads_as_none_without_asking_git(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag settles it — a hook on disk that git was told to skip did not
    run, and reporting `runs` there would point the reader at the wrong cause."""
    work = _repo(tmp_path)
    _install_hook(work)
    monkeypatch.chdir(work)
    state, detail = push._prepush_hook_state({"no-verify"})
    assert state == "none"
    assert "no-verify" in detail


def test_git_not_answering_is_unknown_not_none(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect class. A lookup that could not run must not render as `no
    hook ran`, which is the reading that blames the network."""
    monkeypatch.setattr(push, "_checked_git",
                        lambda *a, **k: (None, "`git rev-parse` exited 128"))
    state, detail = push._prepush_hook_state(set())
    assert state == "unknown", detail
    assert "rev-parse" in detail


# ---------------------------------------------------------------------------
# the receipt
# ---------------------------------------------------------------------------

def _timeout_receipt(monkeypatch: pytest.MonkeyPatch,
                     hook_state: tuple[str, str]) -> tuple[str, int]:
    """The failing arm: local HEAD and remote disagree, so the push is
    UNVERIFIED. That is the arm a reader acts on."""
    monkeypatch.setattr(push, "_local_head", lambda: ("a" * 40, ""))
    monkeypatch.setattr(push, "_live_remote_sha", lambda *a, **k: ("b" * 40, ""))
    monkeypatch.setattr(push, "_prepush_hook_state", lambda flags: hook_state)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = push._report_push_timeout("fix/1", "c" * 40, "origin",
                                       "refs/heads/fix/1", set())
    return buf.getvalue(), rc


def test_timeout_receipt_names_a_hook_that_would_have_run(
        monkeypatch: pytest.MonkeyPatch) -> None:
    out, _rc = _timeout_receipt(monkeypatch, ("runs", ".githooks/pre-push"))
    assert ".githooks/pre-push" in out
    assert "pre-push hook" in out


def test_timeout_receipt_says_so_when_no_hook_ran(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The useful half of the disclosure: ruling the hook out is what confirms
    the network diagnosis the rest of the receipt implies."""
    out, _rc = _timeout_receipt(
        monkeypatch, ("none", "no executable pre-push hook"))
    assert "no executable pre-push hook" in out.lower()


def test_timeout_receipt_declines_rather_than_claiming_no_hook(
        monkeypatch: pytest.MonkeyPatch) -> None:
    out, _rc = _timeout_receipt(
        monkeypatch, ("unknown", "`git rev-parse` exited 128"))
    assert "UNKNOWN" in out
    assert "rev-parse" in out


# ---------------------------------------------------------------------------
# what must not weaken — the receipt #1242 asked to keep intact
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", [
    ("runs", ".githooks/pre-push"),
    ("none", "no executable pre-push hook"),
    ("unknown", "`git rev-parse` exited 128"),
])
def test_the_verdict_and_its_advice_survive_every_hook_state(
        monkeypatch: pytest.MonkeyPatch, state: tuple[str, str]) -> None:
    out, rc = _timeout_receipt(monkeypatch, state)
    assert rc == 1
    assert "PUSH TIMED OUT" in out
    assert "aaaaaaa" in out and "bbbbbbb" in out, "both shas must still be named"
    assert "do NOT force-push" in out
    assert "NOT PUSHED - UNVERIFIED" in out


def test_a_landed_push_is_still_reported_as_pushed(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The other arm: the remote matches HEAD, so the timeout is not a failure
    and no hook disclosure may turn it into one."""
    monkeypatch.setattr(push, "_local_head", lambda: ("a" * 40, ""))
    monkeypatch.setattr(push, "_live_remote_sha", lambda *a, **k: ("a" * 40, ""))
    monkeypatch.setattr(push, "_mr_lookup",
                        lambda *a, **k: types.SimpleNamespace(mr=None,
                                                              answered=True))
    monkeypatch.setattr(push, "_open_mr_line", lambda *a, **k: "")
    monkeypatch.setattr(push, "_post_push_advisories", lambda *a, **k: None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = push._report_push_timeout("fix/1", "c" * 40, "origin",
                                       "refs/heads/fix/1", set())
    out = buf.getvalue()
    assert rc == 0
    assert "PUSHED" in out
    assert "PUSH TIMED OUT" not in out
