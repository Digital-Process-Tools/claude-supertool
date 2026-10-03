"""#2738 -- grep, glob, tree and map skip gitignored FILES, not only directories.

Since #449 the walks pruned gitignored directories and left ignored files in.
Measured 2026-10-04 on a scratch repo: grep printed an ignored file's
contents, `glob:*` auto-read one, tree listed an ignored file and an ignored
directory, map extracted symbols from an ignored `.py`. A gitignored `.env` was
kept out of grep/glob only by the hardcoded secret entries in
`_DEFAULT_EXCLUDE_PATHS`; with those removed it came back with its token.

`.gitignore` is the guard rg and Claude Code's own Grep honour, so it is the one
this tool honours. Every "hidden" assertion below is paired with a tracked file
that must still show -- a walk that returned nothing would pass a hidden-only
assertion vacuously.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import supertool

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not installed"
)

GIT = ["git", "-c", "user.email=f@example.invalid", "-c", "user.name=f"]


def _git(args, cwd):
    subprocess.run(GIT + args, cwd=str(cwd), capture_output=True, text=True,
                   check=False, encoding="utf-8", errors="replace")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Ignored: one plain file, one code file, one directory, `.max/`, `.env`.
    None of the first three is on any built-in exclude list, so whatever hides
    them is the gitignore filter and nothing else."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "tracked.py").write_text("def kept():  # MARK\n    pass\n")
    (tmp_path / "plain-ignored.txt").write_text("MARK in an ignored file\n")
    (tmp_path / "ignored_code.py").write_text("def leaked():  # MARK\n    pass\n")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "p.txt").write_text("MARK in an ignored dir\n")
    (tmp_path / ".max").mkdir()
    (tmp_path / ".max" / "notes.md").write_text("MARK in the maintainer notes\n")
    (tmp_path / ".env").write_text("TOKEN=MARK_env\n")
    (tmp_path / ".gitignore").write_text(
        "plain-ignored.txt\nignored_code.py\nprivate/\n.max/\n.env\n")
    _git(["init", "-q"], tmp_path)
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-q", "-m", "init"], tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUPERTOOL_NO_RTK", "1")
    monkeypatch.setattr(supertool, "_RTK_CHECKED", False)
    return tmp_path


def _fwd(text: str) -> str:
    return text.replace(os.sep, "/")


def test_git_reports_the_ignored_files(repo: Path) -> None:
    """Guard on everything below: if git never answers, nothing here pins."""
    view = supertool._git_ignore_view(".")
    assert "plain-ignored.txt" in view.files
    assert "ignored_code.py" in view.files
    assert ".env" in view.files
    assert "private" in view.dirs
    assert view.unavailable == ""


# -- grep -------------------------------------------------------------------


def test_grep_hides_gitignored_files_and_says_how_many(repo: Path) -> None:
    out = _fwd(supertool.op_grep("MARK", ".", no_auto_read=True))
    assert "src/tracked.py" in out                     # positive control
    assert "plain-ignored.txt" not in out
    assert "ignored_code.py" not in out
    assert "MARK_env" not in out
    assert "gitignored files hidden" in out, out


def test_grep_count_mode_says_how_many_were_hidden(repo: Path) -> None:
    out = _fwd(supertool.op_grep("MARK", ".", count_only=True, no_auto_read=True))
    assert "src/tracked.py" in out
    assert "plain-ignored.txt" not in out
    assert "gitignored files hidden" in out, out


def test_grep_searches_a_gitignored_directory_named_as_the_root(repo: Path) -> None:
    """`grep:X:.max/` is where the maintainer keeps its notes. Naming it is
    deliberate, the way rg searches a path it is handed even when ignored."""
    out = _fwd(supertool.op_grep("MARK", ".max/", no_auto_read=True))
    assert "notes.md" in out
    assert "(1 results in 1 files" in out


def test_grep_searches_a_gitignored_file_named_directly(repo: Path) -> None:
    out = supertool.op_grep("MARK", "plain-ignored.txt", no_auto_read=True)
    assert "MARK in an ignored file" in out


def test_grep_opt_out_by_environment_shows_them_again(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERTOOL_NO_GITIGNORE", "1")
    supertool._GIT_IGNORED_CACHE.clear()
    out = _fwd(supertool.op_grep("MARK", ".", no_auto_read=True))
    assert "plain-ignored.txt" in out
    assert "src/tracked.py" in out
    assert "gitignored" not in out


def test_grep_opt_out_by_config_shows_them_again(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supertool, "_CONFIG", {"gitignore": False})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    supertool._GIT_IGNORED_CACHE.clear()
    out = _fwd(supertool.op_grep("MARK", ".", no_auto_read=True))
    assert "plain-ignored.txt" in out
    assert "ignored_code.py" in out


def test_grep_does_not_delegate_to_rtk_when_an_ignored_file_would_leak(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """rtk runs the system grep, which knows nothing about .gitignore."""
    calls = []

    def _stub(args, timeout=30):
        calls.append(list(args))
        return "./plain-ignored.txt:1:MARK in an ignored file\n"

    monkeypatch.setattr(supertool, "_CONFIG", {"rtk": True})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_CHECKED", True)
    monkeypatch.setattr(supertool, "_RTK_PATH", "/fake/bin/rtk")
    monkeypatch.setattr(supertool, "_rtk_run", _stub)
    supertool._GIT_IGNORED_CACHE.clear()
    out = _fwd(supertool.op_grep("MARK", ".", no_auto_read=True))
    assert not calls, "delegated to rtk although an ignored file sits in the walk"
    assert "plain-ignored.txt" not in out
    assert "src/tracked.py" in out


# -- glob -------------------------------------------------------------------


def test_glob_recursive_hides_gitignored_files(repo: Path) -> None:
    out = _fwd(supertool.op_glob("**/*", no_auto_read=True))
    assert "src/tracked.py" in out
    assert "plain-ignored.txt" not in out
    assert "ignored_code.py" not in out
    assert ".env" not in out.replace(".gitignore", "")
    assert "gitignored files hidden" in out, out


def test_glob_flat_does_not_auto_read_a_gitignored_file(repo: Path) -> None:
    """`glob:*.txt` matched only the ignored file and auto-read it."""
    (repo / "kept.txt").write_text("visible\n")
    out = _fwd(supertool.op_glob("*.txt"))
    assert "kept.txt" in out
    assert "MARK in an ignored file" not in out
    assert "gitignored files hidden" in out, out


def test_glob_concrete_path_is_named_explicitly_and_still_reads(repo: Path) -> None:
    out = supertool.op_glob("plain-ignored.txt")
    assert "MARK in an ignored file" in out


# -- tree -------------------------------------------------------------------


def test_tree_hides_gitignored_files_and_directories(repo: Path) -> None:
    out = supertool.op_tree(".", 3, supertool._get_exclude_paths("tree"))
    assert "tracked.py" in out
    assert "plain-ignored.txt" not in out
    assert "ignored_code.py" not in out
    assert "private/" not in out
    assert "gitignored files hidden" in out, out


def test_tree_of_a_gitignored_directory_named_as_the_root(repo: Path) -> None:
    out = supertool.op_tree("private", 3, supertool._get_exclude_paths("tree"))
    assert "p.txt" in out


# -- map --------------------------------------------------------------------


def test_map_hides_a_gitignored_code_file(repo: Path) -> None:
    out = _fwd(supertool.op_map("."))
    assert "src/tracked.py" in out
    assert "kept" in out
    assert "ignored_code.py" not in out
    assert "leaked" not in out
    assert "gitignored files hidden" in out, out


def test_map_of_a_gitignored_file_named_directly(repo: Path) -> None:
    out = supertool.op_map("ignored_code.py")
    assert "leaked" in out


# -- the filter could not run ------------------------------------------------


def test_outside_a_repo_the_filter_says_it_did_not_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a.py").write_text("MARK\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUPERTOOL_NO_RTK", "1")
    monkeypatch.setattr(supertool, "_RTK_CHECKED", False)
    out = supertool.op_grep("MARK", ".", no_auto_read=True)
    assert "a.py" in out
    assert "gitignore filter not applied" in out, out
    assert "not a git repository" in out, out


def test_without_git_the_filter_says_it_did_not_run(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(repo / "no-such-bin"))
    supertool._GIT_IGNORED_CACHE.clear()
    out = supertool.op_glob("**/*.py", no_auto_read=True)
    assert "tracked.py" in out
    assert "gitignore filter not applied" in out, out


def test_one_git_listing_per_walk_not_one_per_file(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    real = supertool._run_git_ignore_query

    def _spy(root, args):
        calls.append(args[0])
        return real(root, args)

    monkeypatch.setattr(supertool, "_run_git_ignore_query", _spy)
    supertool._GIT_IGNORED_CACHE.clear()
    supertool.op_grep("MARK", ".", no_auto_read=True)
    assert calls.count("ls-files") == 1, calls
    assert len(calls) <= 2, calls


# -- #2734: .gitignore is now the guard for secret files ----------------------

_REMOVED_SECRET_ENTRIES = (
    ".max/", ".ssh/", ".aws/", ".gnupg/", ".kube/", ".docker/",
    ".terraform/", ".chef/", ".npm/", "secrets/", "credentials/",
    ".env/", ".env.*",
)


def test_shipped_defaults_no_longer_name_credential_directories() -> None:
    """The Anthropic directory validator holds a credential directory named in
    shipped data as a credential read (#2734: `credential_at:
    _supertool_config.py`, `env: ".aws/,"`). `.gitignore` is the guard now."""
    named = [e for e in _REMOVED_SECRET_ENTRIES
             if e in supertool._DEFAULT_EXCLUDE_PATHS]
    assert not named, named
    # Positive control: the noise half of the list is untouched.
    assert ".git/" in supertool._DEFAULT_EXCLUDE_PATHS
    assert "node_modules/" in supertool._DEFAULT_EXCLUDE_PATHS


def test_a_gitignored_env_is_hidden_by_gitignore_not_by_the_list(repo: Path) -> None:
    """With default config, `.env` stays out of grep -- and the header credits
    the gitignore filter, so the guard is the one this file says it is."""
    out = _fwd(supertool.op_grep("MARK_env", ".", no_auto_read=True))
    assert "TOKEN=MARK_env" not in out
    assert "gitignored files hidden" in out, out
    # Not the list any more: nothing in this repo is on the credential half.
    assert "hidden by exclude-paths" not in out, out
    tracked = _fwd(supertool.op_grep("def kept", ".", no_auto_read=True))
    assert "src/tracked.py" in tracked                   # positive control
