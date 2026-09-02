"""`init` — derive and write a starter .supertool.json for the current repo (#858).

Without a `.supertool.json`, every preset op (`gh-pr`, `gh-issue`, `git-trail`,
`gl-mr`, ...) simply does not exist (#614) -- there is no auto-generated
fallback, only silence. This op closes the onboarding gap: it derives what it
safely can from the repo itself (the origin remote, which languages are
tracked, which validator binaries actually resolve) and refuses rather than
guesses everywhere it cannot.

Judgment calls, each pinned by a test below:
  - never overwrites an existing .supertool.json (security-relevant defaults
    live in it -- a silent rewrite is worse than doing nothing)
  - a failed detection (no remote, unrecognised host, no repo) declines and
    writes nothing rather than emit a plausible-but-wrong github_repo
  - the op previews by default; only `init:write` commits the file
  - a repo that already has a .supertool.json is entirely out of scope --
    that is the harder "merge into a file a human may have edited" problem
    the issue explicitly punts to a follow-up, covered here only by the
    refuse-to-overwrite test above.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import supertool


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                    capture_output=True, encoding="utf-8", errors="replace")


def _repo(tmp_path: Path, *, remote: str | None = "https://github.com/acme/widgets.git") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("hi" + chr(10), encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    if remote:
        _git(repo, "remote", "add", "origin", remote)
    return repo


def test_init_is_registered_as_a_writing_op() -> None:
    assert "init" in supertool._valid_op_names()
    assert supertool._OP_SAFETY_BUILTIN.get("init") == "writes"
    assert "init" not in supertool._PARALLEL_SAFE_OPS


def test_init_declines_outside_a_git_repo(tmp_path, monkeypatch) -> None:
    bare_dir = tmp_path / "not-a-repo"
    bare_dir.mkdir()
    monkeypatch.chdir(bare_dir)
    out = supertool.op_init("")
    assert "ERROR" in out
    assert not (bare_dir / ".supertool.json").exists()


def test_init_refuses_to_overwrite_an_existing_config(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    existing = repo / ".supertool.json"
    existing.write_text(json.dumps({"allow_vim_shell": True}), encoding="utf-8")
    monkeypatch.chdir(repo)

    out = supertool.op_init("write")

    assert "ERROR" in out
    assert "already exists" in out
    assert "allow_vim_shell" in out  # shows what's already there
    assert existing.read_text(encoding="utf-8") == json.dumps({"allow_vim_shell": True})


def test_init_declines_with_no_origin_remote(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path, remote=None)
    monkeypatch.chdir(repo)
    out = supertool.op_init("")
    assert "ERROR" in out
    assert "remote" in out.lower()
    assert not (repo / ".supertool.json").exists()


def test_init_declines_on_an_unrecognised_host(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path, remote="https://bitbucket.org/acme/widgets.git")
    monkeypatch.chdir(repo)
    out = supertool.op_init("")
    assert "ERROR" in out
    assert "bitbucket.org" in out
    assert not (repo / ".supertool.json").exists()


def test_init_never_matches_a_spoofed_github_host(tmp_path, monkeypatch) -> None:
    """`evilgithub.com` must not be read as github.com — the exact class
    #1212 fixed for the preset's own host check, one caller over."""
    repo = _repo(tmp_path, remote="https://evilgithub.com/acme/widgets.git")
    monkeypatch.chdir(repo)
    out = supertool.op_init("")
    assert "ERROR" in out
    assert not (repo / ".supertool.json").exists()


def test_init_preview_does_not_write(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    out = supertool.op_init("")
    assert not (repo / ".supertool.json").exists()
    assert "acme/widgets" in out
    assert "PREVIEW" in out or "write" in out.lower()


def test_init_write_creates_a_valid_config(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    out = supertool.op_init("write")
    cfg_path = repo / ".supertool.json"
    assert cfg_path.is_file(), out
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["defaults"]["github_repo"] == "acme/widgets"
    assert "github" in data["presets"]
    assert "git" in data["presets"]


def test_init_defaults_are_conservative_not_copied_from_this_repo(tmp_path, monkeypatch) -> None:
    """This repo's own .supertool.json sets allow_outside_cwd: true because
    it is supertool's own checkout -- a stranger's repo must not inherit
    that."""
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    supertool.op_init("write")
    data = json.loads((repo / ".supertool.json").read_text(encoding="utf-8"))
    assert data["rtk"] is False
    assert data["allow_outside_cwd"] is False
    assert data["allow_vim_shell"] is False


def test_init_detects_gitlab_remote(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path, remote="git@gitlab.com:acme/widgets.git")
    monkeypatch.chdir(repo)
    out = supertool.op_init("write")
    data = json.loads((repo / ".supertool.json").read_text(encoding="utf-8"))
    assert data["defaults"]["gitlab_project"] == "acme/widgets"
    assert "gitlab" in data["presets"]
    assert "github" not in data["presets"], out


def test_init_enables_xml_preset_only_when_xml_is_tracked(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    out = supertool.op_init("")
    assert "xml" not in json.loads(_preview_json(out))["presets"]

    (repo / "config.xml").write_text("<a/>" + chr(10), encoding="utf-8")
    _git(repo, "add", "config.xml")
    _git(repo, "commit", "-q", "-m", "xml")
    out2 = supertool.op_init("")
    assert "xml" in json.loads(_preview_json(out2))["presets"]


def _preview_json(out: str) -> str:
    """The preview body is one JSON object, possibly followed by a
    'Declined (...)' note about a validator init chose not to declare --
    isolate the object with a brace-depth scan rather than assuming
    everything after the opening brace is JSON."""
    start = out.index("{")
    depth = 0
    for i, ch in enumerate(out[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return out[start:i + 1]
    raise AssertionError("unbalanced braces in op_init output: " + out)


def test_init_declares_jsonlint_always(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    out = supertool.op_init("")
    assert "jsonlint" in json.loads(_preview_json(out))["validators"]


def test_init_declares_a_python_validator_only_when_the_tool_resolves(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    (repo / "x.py").write_text("x = 1" + chr(10), encoding="utf-8")
    _git(repo, "add", "x.py")
    _git(repo, "commit", "-q", "-m", "py")
    monkeypatch.chdir(repo)

    monkeypatch.setattr(supertool.shutil, "which",
                        lambda name: None if name == "ruff" else "/bin/true")
    out_absent = supertool.op_init("")
    assert "ruff" not in json.loads(_preview_json(out_absent))["validators"]

    monkeypatch.setattr(supertool.shutil, "which",
                        lambda name: "/usr/bin/ruff" if name == "ruff" else None)
    out_present = supertool.op_init("")
    assert "ruff" in json.loads(_preview_json(out_present))["validators"]


def test_init_never_declares_a_validator_for_an_absent_binary_even_when_tracked(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    (repo / "x.sh").write_text("echo hi" + chr(10), encoding="utf-8")
    _git(repo, "add", "x.sh")
    _git(repo, "commit", "-q", "-m", "sh")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(supertool.shutil, "which", lambda name: None)
    out = supertool.op_init("")
    assert "shellcheck" not in json.loads(_preview_json(out))["validators"]


def test_init_declines_when_run_outside_the_repo_root(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    sub = repo / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    out = supertool.op_init("")
    assert "ERROR" in out
    assert not (repo / ".supertool.json").exists()


def test_init_dispatches_through_the_op_string(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    out = supertool.dispatch("init")
    assert "unknown operation" not in out
    assert "acme/widgets" in out
