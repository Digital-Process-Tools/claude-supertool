"""#818 — a branch tracking a dash-named remote ref must not reach `git fetch`
as an option.

Proven on git 2.46.2: `git fetch origin '--upload-pack=<cmd>'` *executes*
<cmd> (RCE), while `git ls-remote` with the same value does not. A
remote-tracking ref an attacker controls (e.g. `origin/--upload-pack=<cmd>`)
flows through `@{upstream}` into the (remote, ref) pair that both merge.py's
`_fresh_merge_ref` and push.py's `_recover_by_rebase` hand to `git fetch` as a
bare argv element — no `--`, no leading-dash refusal.

Two kinds of test here, and the split is deliberate:

* **Refusal tests** run on every platform. They assert the guard declines and
  says what it declined. That is the regression guard, and it depends on
  nothing but string handling.
* **Non-execution tests** additionally assert the payload never ran. Asserting
  that a file does not exist passes for free on a platform where the payload
  could never have run at all, so each one depends on the `rce_is_live`
  positive control below, which proves the sink is live *here* before the
  absence of the sentinel is allowed to mean anything.

The payload is named without a filesystem path on purpose. git's ref-name
grammar forbids `:` and `\\`, so a ref carrying a Windows temp path
(`C:\\Users\\…`) cannot be created at all — that made an earlier version of
this file POSIX-only by construction, and no local run could show it.
`--upload-pack=stpwnexec` is letters and `-`/`=` only: a legal ref name
everywhere, with the payload found on PATH instead. The payload name also
avoids the digits `818` on purpose — `"#818" in out` is how the refusal tests
tell the guard's message apart from git's own error, which quotes the ref.
"""
from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


def _load(name: str):
    preset = Path(__file__).parent.parent / "presets" / "git" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"git_{name}", preset)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


merge = _load("merge")
push = _load("push")

_gc_spec = importlib.util.spec_from_file_location(
    "git_common_818",
    Path(__file__).parent.parent / "presets" / "git" / "_git_common.py",
)
assert _gc_spec is not None and _gc_spec.loader is not None
gc = importlib.util.module_from_spec(_gc_spec)
_gc_spec.loader.exec_module(gc)


#: Payload command name. No path separators, no ':' — a legal ref name on every
#: platform, which is the whole point (see module docstring).
EVIL = "stpwnexec"
HOSTILE_REF = f"--upload-pack={EVIL}"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args), cwd=repo, capture_output=True, text=True, check=check
    )


def _arm_payload(tmp_path: Path, monkeypatch) -> Path:
    """Put an executable named EVIL on PATH. Returns the sentinel it touches.

    `as_posix()` on the sentinel: the script is run by a POSIX shell (git for
    Windows ships its own), where a backslash path would read as escapes.
    """
    sentinel = tmp_path / "PWNED_818"
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / EVIL
    script.write_text(f"#!/bin/sh\ntouch '{sentinel.as_posix()}'\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return sentinel


def _origin_repo(tmp_path: Path) -> Path:
    """Bare remote + a clone with one commit pushed to master."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "master", str(remote))
    a = tmp_path / "a"
    _git(tmp_path, "clone", str(remote), str(a))
    _git(a, "config", "user.email", "a@test.com")
    _git(a, "config", "user.name", "A")
    (a / "f.txt").write_text("hi\n")
    _git(a, "add", "f.txt")
    _git(a, "commit", "-m", "init")
    _git(a, "push", "origin", "master")
    return a


# --------------------------------------------------------------------------- #
# Positive control — is the sink live on THIS platform?
# --------------------------------------------------------------------------- #
def _probe_rce(tmp_path: Path, monkeypatch) -> bool:
    """Run the unguarded `git fetch <remote> <hostile-ref>` and report exec."""
    sentinel = _arm_payload(tmp_path, monkeypatch)
    a = _origin_repo(tmp_path)
    subprocess.run(
        ["git", "fetch", "origin", HOSTILE_REF],
        cwd=a, capture_output=True, text=True,
    )
    return sentinel.exists()


def test_fetch_option_executes_unguarded_818(tmp_path, monkeypatch):
    """The sink, stated as a fact about this platform rather than assumed.

    This is what keeps the non-execution assertions honest. If it ever turns
    from pass to skip, the platform stopped reproducing the RCE and the
    sentinel checks below became vacuous there — which is a thing to know now,
    not to discover later.
    """
    live = _probe_rce(tmp_path, monkeypatch)
    if not live:
        pytest.skip(
            f"`git fetch origin {HOSTILE_REF}` did not execute the payload on "
            f"{sys.platform} — git may not honour --upload-pack for this "
            "transport here. The refusal tests still run; the non-execution "
            "assertions are vacuous on this platform."
        )
    assert live


@pytest.fixture
def rce_is_live(tmp_path, monkeypatch):
    """Skip a non-execution assertion whose premise this platform cannot meet."""
    probe = tmp_path / "probe"
    probe.mkdir()
    if not _probe_rce(probe, monkeypatch):
        pytest.skip(
            f"RCE not reproducible on {sys.platform} — 'payload did not run' "
            "would pass for the wrong reason. See "
            "test_fetch_option_executes_unguarded_818."
        )


def test_hostile_ref_name_is_portable_818(tmp_path):
    """The fixture's ref name must be creatable on every platform.

    An earlier version embedded `tmp_path` in the ref, which is `/tmp/…` on
    POSIX (legal — `/` is just hierarchy) but `C:\\Users\\…` on Windows, where
    both `:` and `\\` are forbidden by git's ref-name grammar. `update-ref`
    refused it and two Windows CI legs went red on a test that passed
    everywhere else. This pins the property rather than the incident.
    """
    assert ":" not in HOSTILE_REF and "\\" not in HOSTILE_REF
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    ok = _git(repo, "check-ref-format", f"refs/heads/{HOSTILE_REF}", check=False)
    assert ok.returncode == 0, f"git rejects the ref name: {ok.stderr}"


# --------------------------------------------------------------------------- #
# reject_fetch_option — the shared predicate both sinks call
# --------------------------------------------------------------------------- #
def test_reject_fetch_option_double_dash_ref():
    msg = gc.reject_fetch_option("origin", HOSTILE_REF)
    assert msg and "--upload-pack" in msg and "ref" in msg


def test_reject_fetch_option_single_dash_ref():
    # A single-dash value is still an option in ref position ('-X' -> option).
    msg = gc.reject_fetch_option("origin", "-X")
    assert msg and "'-X'" in msg


def test_reject_fetch_option_flags_dash_remote():
    # The remote half lands as a bare argv element too.
    msg = gc.reject_fetch_option(HOSTILE_REF, "master")
    assert msg and "remote" in msg


def test_reject_fetch_option_allows_ordinary_pair():
    assert gc.reject_fetch_option("origin", "master") == ""
    assert gc.reject_fetch_option("origin", "feature/foo") == ""


# --------------------------------------------------------------------------- #
# merge.py — _fresh_merge_ref feeds (remote, rbranch) from @{upstream} to fetch
# --------------------------------------------------------------------------- #
def _poison_tracking_branch(tmp_path: Path) -> Path:
    """Clone whose local branch `victim` tracks origin/--upload-pack=evil818.

    The hostile remote ref is created with `update-ref` — git's porcelain
    refuses to *create* an option-shaped branch name, but fetches and tracks
    one happily once it exists, which is the whole gap.
    """
    a = _origin_repo(tmp_path)
    remote = tmp_path / "remote.git"
    _git(remote, "update-ref", f"refs/heads/{HOSTILE_REF}", "HEAD")
    _git(a, "fetch", "origin")
    _git(a, "checkout", "-b", "victim", "--track", f"origin/{HOSTILE_REF}")
    return a


def test_merge_refuses_option_like_upstream_818(tmp_path, monkeypatch, capsys):
    """Runs on every platform: the guard must decline, and say what it declined."""
    a = _poison_tracking_branch(tmp_path)
    up = _git(a, "rev-parse", "--abbrev-ref", "victim@{upstream}").stdout.strip()
    assert up == f"origin/{HOSTILE_REF}", up

    monkeypatch.chdir(a)
    monkeypatch.setattr(sys, "argv", ["merge.py", "victim"])
    rc = merge.main()
    out = capsys.readouterr().out

    # Assert the REFUSAL, not merely that the ref got mentioned. Unguarded,
    # merge.py also prints this ref name (in `fetch ... failed ... may be
    # stale`) and also returns 0 — so a laxer assertion passes on a vulnerable
    # build, which is precisely what must not happen on a platform where the
    # non-execution check is vacuous.
    assert "looks like a git option" in out, out
    assert "#818" in out, out
    assert "not fetching" in out, out
    assert "may be stale" not in out, "fell through to the unguarded fetch path"
    # The refusal must name what it refused — a silent drop would fetch the
    # wrong thing, trading the loud bug for the quiet one.
    assert HOSTILE_REF in out, out
    # Merging the local ref is safe (no fetch, no exec) and must still succeed.
    assert rc == 0, out


def test_merge_does_not_execute_payload_818(tmp_path, monkeypatch, rce_is_live):
    """The RCE itself, on a platform proven to reproduce it."""
    sentinel = _arm_payload(tmp_path, monkeypatch)
    a = _poison_tracking_branch(tmp_path)
    monkeypatch.chdir(a)
    monkeypatch.setattr(sys, "argv", ["merge.py", "victim"])
    merge.main()
    assert not sentinel.exists(), "RCE: the --upload-pack payload executed on fetch"


def test_merge_normal_upstream_still_fetches_818(tmp_path, monkeypatch, capsys):
    """Guard is dash-scoped: an ordinary upstream is untouched (no false refusal)."""
    a = _origin_repo(tmp_path)
    _git(a, "checkout", "-b", "feat")
    (a / "g.txt").write_text("g\n")
    _git(a, "add", "g.txt")
    _git(a, "commit", "-m", "feat")

    monkeypatch.chdir(a)
    monkeypatch.setattr(sys, "argv", ["merge.py", "master"])
    rc = merge.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "--upload-pack" not in out


# --------------------------------------------------------------------------- #
# push.py — _recover_by_rebase feeds (remote_name, remote_ref) to fetch
# --------------------------------------------------------------------------- #
def test_recover_by_rebase_refuses_option_like_ref_818(tmp_path, monkeypatch, capsys):
    """Runs on every platform: the recovery must abort and name the ref."""
    a = _origin_repo(tmp_path)
    monkeypatch.chdir(a)
    rc = push._recover_by_rebase(
        "master", "", f"origin/{HOSTILE_REF}", "origin", HOSTILE_REF, set()
    )
    out = capsys.readouterr().out
    assert rc != 0, out
    # Same discipline as the merge case: unguarded, push.py prints this ref in
    # `fetch of <ref> failed, cannot rebase` and also returns non-zero.
    assert "looks like a git option" in out, out
    assert "#818" in out, out
    assert "cannot rebase" not in out, "fell through to the unguarded fetch path"
    assert HOSTILE_REF in out, out


def test_recover_by_rebase_does_not_execute_payload_818(
    tmp_path, monkeypatch, rce_is_live
):
    """The RCE itself, on a platform proven to reproduce it."""
    sentinel = _arm_payload(tmp_path, monkeypatch)
    a = _origin_repo(tmp_path)
    monkeypatch.chdir(a)
    push._recover_by_rebase(
        "master", "", f"origin/{HOSTILE_REF}", "origin", HOSTILE_REF, set()
    )
    assert not sentinel.exists(), "RCE: fetch executed the --upload-pack payload"
