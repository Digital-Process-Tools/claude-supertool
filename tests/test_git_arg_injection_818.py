"""#818 — a branch tracking a dash-named remote ref must not reach `git fetch`
as an option.

Proven on git 2.46.2: `git fetch origin '--upload-pack=<cmd>; git-upload-pack'`
*executes* <cmd> (RCE), while `git ls-remote` with the same value does not.
A remote-tracking ref an attacker controls (e.g. `origin/--upload-pack=/x.sh`)
flows through `@{upstream}` into the (remote, ref) pair that both merge.py's
`_fresh_merge_ref` and push.py's `_recover_by_rebase` hand to `git fetch` as a
bare argv element — no `--`, no leading-dash refusal. These tests assert the
refusal, by name, and prove the payload never runs.
"""
from __future__ import annotations

import importlib.util
import stat
import subprocess
import sys
from pathlib import Path


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


# --------------------------------------------------------------------------- #
# reject_fetch_option — the shared predicate both sinks call
# --------------------------------------------------------------------------- #
def test_reject_fetch_option_double_dash_ref():
    msg = gc.reject_fetch_option("origin", "--upload-pack=/x.sh")
    assert msg and "--upload-pack" in msg and "ref" in msg


def test_reject_fetch_option_single_dash_ref():
    # A single-dash value is still an option in ref position ('-X' -> option).
    msg = gc.reject_fetch_option("origin", "-X")
    assert msg and "'-X'" in msg


def test_reject_fetch_option_flags_dash_remote():
    # The remote half lands as a bare argv element too.
    msg = gc.reject_fetch_option("--upload-pack=/x.sh", "master")
    assert msg and "remote" in msg


def test_reject_fetch_option_allows_ordinary_pair():
    assert gc.reject_fetch_option("origin", "master") == ""
    assert gc.reject_fetch_option("origin", "feature/foo") == ""


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args), cwd=repo, capture_output=True, text=True, check=check
    )


def _payload(tmp_path: Path) -> tuple[Path, Path]:
    """An executable whose only job is to prove it ran. Returns (script, sentinel)."""
    sentinel = tmp_path / "PWNED_818"
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script, sentinel


# --------------------------------------------------------------------------- #
# merge.py — _fresh_merge_ref feeds (remote, rbranch) from @{upstream} to fetch
# --------------------------------------------------------------------------- #
def _poison_tracking_branch(tmp_path: Path, script: Path) -> Path:
    """Clone A whose local branch `victim` tracks origin/--upload-pack=<script>.

    Builds the exact upstream poisoning from the issue: a remote branch named
    like a git option, fetched and tracked locally.
    """
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
    # Create the hostile remote ref directly (a name git's porcelain would
    # refuse to *create*, but happily fetches and tracks once it exists).
    hostile = f"refs/heads/--upload-pack={script}"
    _git(remote, "update-ref", hostile, "HEAD")
    _git(a, "fetch", "origin")
    _git(a, "checkout", "-b", "victim", "--track", f"origin/--upload-pack={script}")
    return a


def test_merge_refuses_option_like_upstream_818(tmp_path, monkeypatch, capsys):
    script, sentinel = _payload(tmp_path)
    a = _poison_tracking_branch(tmp_path, script)
    # Sanity: the upstream really is poisoned and would split to a dash ref.
    up = _git(a, "rev-parse", "--abbrev-ref", "victim@{upstream}").stdout.strip()
    assert up == f"origin/--upload-pack={script}", up

    monkeypatch.chdir(a)
    monkeypatch.setattr(sys, "argv", ["merge.py", "victim"])
    rc = merge.main()
    out = capsys.readouterr().out

    assert not sentinel.exists(), "RCE: the --upload-pack payload executed on fetch"
    # The refusal must name what it refused (issue: no silent wrong-fetch).
    assert "--upload-pack" in out, out
    assert "818" in out or "option" in out.lower(), out
    # Merging the local ref is safe (no exec) and must still succeed.
    assert rc == 0, out


def test_merge_normal_upstream_still_fetches_818(tmp_path, monkeypatch, capsys):
    """Guard is dash-scoped: an ordinary upstream is untouched (no false refusal)."""
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
def _repo_with_origin(tmp_path: Path) -> Path:
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


def test_recover_by_rebase_refuses_option_like_ref_818(tmp_path, monkeypatch, capsys):
    script, sentinel = _payload(tmp_path)
    a = _repo_with_origin(tmp_path)
    monkeypatch.chdir(a)

    hostile_ref = f"--upload-pack={script}"
    rc = push._recover_by_rebase(
        "master", "", f"origin/{hostile_ref}", "origin", hostile_ref, set()
    )
    out = capsys.readouterr().out

    assert not sentinel.exists(), "RCE: fetch executed the --upload-pack payload"
    assert rc != 0, out
    assert "--upload-pack" in out, out
