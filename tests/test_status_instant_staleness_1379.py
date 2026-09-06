"""`Working tree: clean` is a claim about an instant, printed with no instant
attached (#1379).

Correct when printed and read as a standing fact by an agent with no memory:
in a worktree somebody else is committing into, `clean` means *nothing is
happening right now, in the working tree, as of this instant* -- and nothing
in the render said which instant that was, or how long ago HEAD itself last
moved. The fix appends HEAD's own commit age, reusing the vocabulary
`git-worktrees` already renders elsewhere (`newest write ... ago`).

Both a positive control (the note DOES appear on an ordinary clean read) and
a negative one are needed here, per the brief for this issue: a broken stamp
must not silently pass as "nothing to show".
"""
from __future__ import annotations

import importlib.util
import io
import re
import subprocess
import time
from contextlib import redirect_stdout
from pathlib import Path

PRESET_PATH = Path(__file__).parent.parent / "presets" / "git" / "status.py"
_spec = importlib.util.spec_from_file_location("git_status_1379", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
status = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(status)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True,
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "f").write_text("x\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-m", "initial")
    return repo


def _stub_no_mr(monkeypatch) -> None:
    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        if args and args[0] in ("glab", "gh"):
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(status.subprocess, "run", fake_run)


def _run_main(repo: Path, monkeypatch, *args: str) -> str:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(status.sys, "argv", ["status.py", *args])
    buf = io.StringIO()
    with redirect_stdout(buf):
        status.main()
    return buf.getvalue()


def test_a_freshly_committed_clean_tree_discloses_head_is_seconds_old(
    tmp_path: Path, monkeypatch,
) -> None:
    """POSITIVE CONTROL: the note appears on an ordinary clean read."""
    repo = _init_repo(tmp_path)
    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch)
    assert "Working tree: clean" in out, out
    m = re.search(r"Working tree: clean, HEAD (\d+)s old", out)
    assert m, (
        "a freshly-committed repo's clean tree did not disclose HEAD's age "
        "at all:" + chr(10) + out)
    assert int(m.group(1)) < 30, out


def test_an_older_commit_is_disclosed_in_a_larger_unit(
    tmp_path: Path, monkeypatch,
) -> None:
    """NEGATIVE half: the note must track the REAL commit age, not just fire."""
    repo = _init_repo(tmp_path)
    _stub_no_mr(monkeypatch)
    real_git = status._git

    def fake_git(args, timeout=None):
        if args[:1] == ["log"] and "--format=%ct" in args:
            old_ts = int(time.time()) - 10_000  # ~2h47m ago
            return subprocess.CompletedProcess(
                args=["git"] + args, returncode=0, stdout=f"{old_ts}\n", stderr="")
        return real_git(args, timeout=timeout)

    monkeypatch.setattr(status, "_git", fake_git)
    out = _run_main(repo, monkeypatch)
    assert "Working tree: clean, HEAD 2h old" in out, out
    assert re.search(r"HEAD \d+s old", out) is None, (
        "an old commit was reported in seconds -- the unit is wrong, not "
        "just the number:" + chr(10) + out)


def test_a_dirty_tree_is_unaffected(tmp_path: Path, monkeypatch) -> None:
    """The boundary: the note is only for `clean`, never for a dirty tree."""
    repo = _init_repo(tmp_path)
    (repo / "f").write_text("changed\n")
    _stub_no_mr(monkeypatch)
    out = _run_main(repo, monkeypatch)
    assert "Working tree (1 changes)" in out, out
    assert "HEAD" not in out.split("Working tree")[1].split(chr(10))[0], out


def test_head_age_note_declines_rather_than_guesses_on_an_unborn_head(
    tmp_path: Path, monkeypatch,
) -> None:
    """The third state, checked directly: `_git(["log", ...])` failing (an
    unborn HEAD, git not answering) must render as nothing appended, never
    as a fabricated age that looks like a real answer.
    """
    def _fail(args, timeout=None):
        return subprocess.CompletedProcess(
            args=["git"] + args, returncode=128, stdout="", stderr="fatal")

    monkeypatch.setattr(status, "_git", _fail)
    assert status._head_age_note() == ""
