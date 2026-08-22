"""#1922 -- `gh-prs`'/`gl-mrs`' unwatched footer read a stale local default
for the poller state directory instead of the resolved one `radar` and
`watches` already use.

`presets/github/prs.py` and `presets/gitlab/mrs.py` each carried their own
hardcoded `STATE_DIR = "/tmp"`, used as the default argument to
`_watched_numbers`/`_watched_iids`. `presets/watch/transport.py` resolves the
*actual* poller state directory (`transport.STATE_DIR`), which differs from
bare `/tmp` under a named channel (`SUPERTOOL_WATCH_NAME`) or an explicit
`SUPERTOOL_WATCH_STATE_DIR` -- exactly the case in the reporting session,
where `SUPERTOOL_WATCH_NAME=oss-supertool` moved every pid file under
`/tmp/supertool-watch-oss-supertool/`. `radar` and `watches` both already
read `transport.STATE_DIR` (directly, or through the same
`_watched_numbers`/`_watched_iids` helper called with it explicitly), so only
the footer's own `main()` disagreed -- reporting a PR/MR as unwatched while a
live, emitting poller held its slot.

The control pair the issue asks for: an armed poller must read as covered,
and a bare one must read as uncovered, in the same directory the real
poller machinery uses -- not the board's own stale default. Both directions
are pinned so the fix cannot swap over-reporting for under-reporting, the
absence-read-as-clean direction the issue calls out as unobserved but
unruled-out.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prs = _load("presets/github/prs.py", "gh_prs_footer_state_dir_1922")
mrs = _load("presets/gitlab/mrs.py", "gl_mrs_footer_state_dir_1922")


def _pr(number: int) -> dict:
    return {"number": number, "_checks": "failed", "_approved": True}


def _mr(iid: int) -> dict:
    return {"iid": iid, "_pipeline": "failed", "_approved": True}


# ---------------------------------------------------------------------------
# gh-prs
# ---------------------------------------------------------------------------

def test_gh_prs_call_site_reads_transport_state_dir_live(tmp_path, monkeypatch) -> None:
    """`main()`'s own `watched = _watched_numbers(...)` call, isolated: it
    must read `transport.STATE_DIR` at call time, not a value frozen at
    import (#1922) -- a monkeypatched channel dir must be honoured exactly
    the way `radar` and `watches` honour it."""
    monkeypatch.setattr(prs.transport, "STATE_DIR", str(tmp_path))
    pid_file = tmp_path / "supertool-watch-github-pr__1917.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    assert prs._watched_numbers(prs.transport.STATE_DIR) == {"1917"}


def test_gh_prs_footer_agrees_when_a_named_channel_holds_the_poller(
        tmp_path, monkeypatch) -> None:
    """Positive control: a poller armed under a *non-default* state dir (the
    named-channel shape from the report) must not be reported unwatched."""
    monkeypatch.setattr(prs.transport, "STATE_DIR", str(tmp_path))
    pid_file = tmp_path / "supertool-watch-github-pr__1917.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    watched = prs._watched_numbers(prs.transport.STATE_DIR)
    footer = prs._footer([_pr(1917)], watched)
    assert "unwatched" not in footer, footer


def test_gh_prs_footer_still_flags_a_genuinely_bare_pr(tmp_path, monkeypatch) -> None:
    """Negative control, same directory, no poller armed: this must still
    say unwatched -- the fix must not swap the false alarm for the
    absence-read-as-clean direction (silently reporting a bare PR covered)."""
    monkeypatch.setattr(prs.transport, "STATE_DIR", str(tmp_path))

    watched = prs._watched_numbers(prs.transport.STATE_DIR)
    footer = prs._footer([_pr(1917)], watched)
    assert "1 unwatched → watch:github-pr:1917" in footer, footer


def test_gh_prs_footer_disagreed_before_the_fix(tmp_path, monkeypatch) -> None:
    """Reproduces the reported disagreement directly: the *old* call shape
    (`_watched_numbers()` with the stale hardcoded default) misses a poller
    armed under a resolved, non-default state dir, while the live read
    (what `main()` now does) finds it. Pinning both proves the fix is the
    live read, not an accidental pass."""
    monkeypatch.setattr(prs.transport, "STATE_DIR", str(tmp_path))
    pid_file = tmp_path / "supertool-watch-github-pr__1917.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    stale_default_watched = prs._watched_numbers()  # the pre-fix call shape
    live_watched = prs._watched_numbers(prs.transport.STATE_DIR)  # the fix

    assert "1917" not in stale_default_watched, (
        "the stale default should miss the armed poller -- if this fails, "
        "the fixture no longer reproduces the reported mismatch")
    assert "1917" in live_watched


# ---------------------------------------------------------------------------
# gl-mrs -- the identical shape, same root cause (#1922)
# ---------------------------------------------------------------------------

def test_gl_mrs_call_site_reads_transport_state_dir_live(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mrs.transport, "STATE_DIR", str(tmp_path))
    pid_file = tmp_path / "supertool-watch-gitlab-mr__34129.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    assert mrs._watched_iids(mrs.transport.STATE_DIR) == {"34129"}


def test_gl_mrs_footer_agrees_when_a_named_channel_holds_the_poller(
        tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mrs.transport, "STATE_DIR", str(tmp_path))
    pid_file = tmp_path / "supertool-watch-gitlab-mr__34129.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    watched = mrs._watched_iids(mrs.transport.STATE_DIR)
    footer = mrs._footer([_mr(34129)], watched, show_pipe=True)
    assert "unwatched" not in footer, footer


def test_gl_mrs_footer_still_flags_a_genuinely_bare_mr(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mrs.transport, "STATE_DIR", str(tmp_path))

    watched = mrs._watched_iids(mrs.transport.STATE_DIR)
    footer = mrs._footer([_mr(34129)], watched, show_pipe=True)
    assert "1 unwatched → watch:gitlab-mr:34129" in footer, footer


# ---------------------------------------------------------------------------
# end-to-end through main()/main_with_args() -- the actual call site the fix
# touched, not just the helper it calls. A test that only calls
# `_watched_numbers(transport.STATE_DIR)` directly would still pass if the
# call site in `main()` had never been changed back to the bare, argument-
# less `_watched_numbers()` -- these two drive the real entry point instead.
# ---------------------------------------------------------------------------

import contextlib
import io
import json
import subprocess


def _github_pr_row(number: int) -> dict:
    return {
        "number": number,
        "title": f"pr {number}",
        "state": "OPEN",
        "author": {"login": "someone"},
        "headRefName": f"feat/{number}",
        "headRefOid": "0" * 40,
        "baseRefName": "master",
        "labels": [],
        "assignees": [],
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED",
             "conclusion": "FAILURE"},
        ],
        "additions": 1,
        "deletions": 0,
        "changedFiles": 1,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
        "url": f"https://github.com/o/n/pull/{number}",
    }


def _drive_gh_prs(monkeypatch, arg_str: str, rows: list[dict]) -> str:
    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, json.dumps(rows), "")

    monkeypatch.setattr(prs.subprocess, "run", fake_run)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        prs.main_with_args(arg_str)
    return out.getvalue()


def test_gh_prs_main_reads_a_poller_armed_under_a_named_channel(
        tmp_path, monkeypatch) -> None:
    """The reported shape, end to end: `main_with_args` -- not the helper in
    isolation -- must see a poller armed under a resolved, non-default state
    dir and not call it unwatched."""
    monkeypatch.setattr(prs.transport, "STATE_DIR", str(tmp_path))
    (tmp_path / "supertool-watch-github-pr__1917.pid").write_text(
        str(os.getpid()), encoding="utf-8")
    out = _drive_gh_prs(monkeypatch, "nopipe", [_github_pr_row(1917)])
    assert "unwatched" not in out, out


def test_gh_prs_main_still_flags_a_bare_pr(tmp_path, monkeypatch) -> None:
    """Same call, same directory, no poller armed -- must still flag it. The
    negative half of the control pair, run through `main_with_args` too."""
    monkeypatch.setattr(prs.transport, "STATE_DIR", str(tmp_path))
    out = _drive_gh_prs(monkeypatch, "nopipe", [_github_pr_row(1917)])
    assert "1 unwatched → watch:github-pr:1917" in out, out


def _drive_gl_mrs(monkeypatch, arg_str: str, rows: list[dict]) -> str:
    def fake_run(cmd: list[str], timeout: int = 25) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, json.dumps(rows), "")

    def fake_enrich(mrs_list, cap=mrs.ENRICH_CAP, workers=mrs.ENRICH_WORKERS,
                    with_approvals=True) -> None:
        for m in mrs_list:
            m["_pipeline"] = "failed"
            m["_pipeline_id"] = 1
            m["_approved"] = True
            m["_enriched"] = True

    monkeypatch.setattr(mrs, "_run", fake_run)
    monkeypatch.setattr(mrs, "_enrich", fake_enrich)
    monkeypatch.setattr(mrs.sys, "argv", ["mrs.py", arg_str])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        mrs.main()
    return out.getvalue()


def test_gl_mrs_main_reads_a_poller_armed_under_a_named_channel(
        tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mrs.transport, "STATE_DIR", str(tmp_path))
    (tmp_path / "supertool-watch-gitlab-mr__34129.pid").write_text(
        str(os.getpid()), encoding="utf-8")
    out = _drive_gl_mrs(monkeypatch, "", [_mr(34129)])
    assert "unwatched" not in out, out


def test_gl_mrs_main_still_flags_a_bare_mr(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mrs.transport, "STATE_DIR", str(tmp_path))
    out = _drive_gl_mrs(monkeypatch, "", [_mr(34129)])
    assert "1 unwatched → watch:gitlab-mr:34129" in out, out
