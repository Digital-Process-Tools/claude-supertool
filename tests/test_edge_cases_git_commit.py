"""Edge-case tests for presets/git/commit.py.

Covers: newline in MSG, special chars, detached HEAD, no-paths (pre-staged),
nonexistent path, path outside repo, binary file, trailing whitespace in MSG,
and empty message.

All tests that run real git operations use a throwaway repo in tmp_path.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load the module under test
# ---------------------------------------------------------------------------
PRESET = Path(__file__).parent.parent / "presets" / "git" / "commit.py"
_spec = importlib.util.spec_from_file_location("git_commit_ec", PRESET)
assert _spec is not None and _spec.loader is not None
commit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(commit)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Helper: bootstrap a minimal git repo
# ---------------------------------------------------------------------------

def _init_repo(path: Path) -> None:
    """Init a git repo with one commit so HEAD exists."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "config", "user.email", "test@test.invalid"], check=True, cwd=path)
    subprocess.run(["git", "config", "user.name", "Test User"], check=True, cwd=path)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], check=True, cwd=path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], check=True, cwd=path)


def _stage_file(path: Path, name: str = "work.txt", content: str = "work\n") -> Path:
    """Write a file and stage it; returns the file path."""
    f = path / name
    if isinstance(content, str):
        f.write_text(content)
    else:
        f.write_bytes(content)  # type: ignore[arg-type]
    subprocess.run(["git", "add", "--", name], check=True, cwd=path)
    return f


# ---------------------------------------------------------------------------
# 1. Newline in commit message
# ---------------------------------------------------------------------------

def test_newline_in_commit_message_produces_multiline_commit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """\\n in MSG must reach `git commit -m` verbatim → multi-line commit."""
    _init_repo(tmp_path)
    _stage_file(tmp_path)
    monkeypatch.chdir(tmp_path)

    msg = "line1\nline2"
    monkeypatch.setattr(commit.sys, "argv", ["commit.py", msg])
    rc = commit.main()
    out = capsys.readouterr().out

    assert rc == 0, f"commit failed:\n{out}"

    # Verify the subject and body landed in git log
    log = subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        capture_output=True, text=True, cwd=tmp_path, check=True,
    ).stdout.strip()
    assert "line1" in log
    assert "line2" in log


# ---------------------------------------------------------------------------
# 2. Special chars in message: backticks, colons, quotes
# ---------------------------------------------------------------------------

def test_special_chars_in_message_preserved(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Backticks, quotes, colons in MSG must be preserved verbatim."""
    _init_repo(tmp_path)
    _stage_file(tmp_path)
    monkeypatch.chdir(tmp_path)

    msg = "feat: `some code` works & 'quotes' and \"doubles\""
    monkeypatch.setattr(commit.sys, "argv", ["commit.py", msg])
    rc = commit.main()
    out = capsys.readouterr().out

    assert rc == 0, f"commit failed:\n{out}"

    log = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        capture_output=True, text=True, cwd=tmp_path, check=True,
    ).stdout.strip()
    assert "`some code`" in log
    assert "quotes" in log


# ---------------------------------------------------------------------------
# 3. Detached HEAD — should succeed
# ---------------------------------------------------------------------------

def test_commit_on_detached_head_succeeds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """git allows commits on detached HEAD; the op must not block it."""
    _init_repo(tmp_path)

    # Detach HEAD
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, cwd=tmp_path, check=True,
    ).stdout.strip()
    subprocess.run(["git", "checkout", "--detach", sha], check=True, cwd=tmp_path,
                   capture_output=True)

    _stage_file(tmp_path)
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(commit.sys, "argv", ["commit.py", "detached commit"])
    rc = commit.main()
    out = capsys.readouterr().out

    # The op must either succeed (rc == 0) or fail with a clear, non-crash error.
    # Git itself allows committing on detached HEAD, so we assert rc == 0.
    assert rc == 0, f"commit on detached HEAD failed unexpectedly:\n{out}"
    assert "HEAD after" in out


# ---------------------------------------------------------------------------
# 4. No paths provided — relies on already-staged content
# ---------------------------------------------------------------------------

def test_no_paths_uses_already_staged_content(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """When no PATHS are given, op commits whatever is already staged."""
    _init_repo(tmp_path)
    _stage_file(tmp_path, "prestaged.txt", "already staged\n")
    monkeypatch.chdir(tmp_path)

    # No paths in argv — only MSG
    monkeypatch.setattr(commit.sys, "argv", ["commit.py", "pre-staged commit"])
    rc = commit.main()
    out = capsys.readouterr().out

    assert rc == 0, f"commit with pre-staged files failed:\n{out}"
    assert "prestaged.txt" in out


def test_no_paths_and_nothing_staged_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """No PATHS + nothing staged must produce a clear error, not a crash."""
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(commit.sys, "argv", ["commit.py", "oops nothing staged"])
    rc = commit.main()
    out = capsys.readouterr().out

    assert rc != 0
    assert "nothing staged" in out.lower() or "staged" in out.lower()


# ---------------------------------------------------------------------------
# 5. Path that doesn't exist → clean error
# ---------------------------------------------------------------------------

def test_nonexistent_path_gives_clean_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Passing a path that doesn't exist should fail with a readable error."""
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(commit.sys, "argv",
                        ["commit.py", "msg", "nonexistent_file.txt"])
    rc = commit.main()
    out = capsys.readouterr().out

    assert rc != 0
    # Should explain the problem — not crash with a traceback
    # `git add -- nonexistent.txt` exits 128 with a pathspec error
    assert "error" in out.lower() or "fatal" in out.lower() or "pathspec" in out.lower()


# ---------------------------------------------------------------------------
# 6. Path outside repo → should reject cleanly
# ---------------------------------------------------------------------------

def test_path_outside_repo_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`git add -- /etc/passwd` is rejected by git (outside work-tree)."""
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(commit.sys, "argv",
                        ["commit.py", "malicious msg", "/etc/passwd"])
    rc = commit.main()
    out = capsys.readouterr().out

    assert rc != 0
    # git add should have failed; op should surface it, not silently succeed
    assert "error" in out.lower() or "fatal" in out.lower() or "outside" in out.lower()


# ---------------------------------------------------------------------------
# 7. Binary file — git allows it; op must not choke
# ---------------------------------------------------------------------------

def test_binary_file_committed_without_choking(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Binary blobs are valid git objects; the op must handle them gracefully."""
    _init_repo(tmp_path)

    binary_content = bytes(range(256)) * 4  # 1 KB of all byte values including NUL
    binary_path = tmp_path / "blob.bin"
    binary_path.write_bytes(binary_content)
    subprocess.run(["git", "add", "--", "blob.bin"], check=True, cwd=tmp_path)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commit.sys, "argv", ["commit.py", "add binary blob"])
    rc = commit.main()
    out = capsys.readouterr().out

    assert rc == 0, f"binary file commit failed:\n{out}"
    assert "HEAD after" in out


# ---------------------------------------------------------------------------
# 8. Message with trailing whitespace — documented behaviour
# ---------------------------------------------------------------------------

def test_trailing_whitespace_in_message_behaviour(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """MSG with trailing spaces: git trims trailing whitespace from the subject.
    The op must not error; the commit must land regardless.
    """
    _init_repo(tmp_path)
    _stage_file(tmp_path)
    monkeypatch.chdir(tmp_path)

    msg = "trailing spaces here   "
    monkeypatch.setattr(commit.sys, "argv", ["commit.py", msg])
    rc = commit.main()
    out = capsys.readouterr().out

    # The commit should succeed — git trims trailing whitespace silently
    assert rc == 0, f"commit with trailing whitespace failed:\n{out}"

    log_subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        capture_output=True, text=True, cwd=tmp_path, check=True,
    ).stdout.strip()
    # Git trims trailing whitespace from subject; core message text must survive
    assert "trailing spaces here" in log_subject


# ---------------------------------------------------------------------------
# 9. Empty message → must error
# ---------------------------------------------------------------------------

def test_empty_message_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """An empty (or whitespace-only) MSG must be rejected before touching git."""
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(commit.sys, "argv", ["commit.py", "   "])
    rc = commit.main()
    out = capsys.readouterr().out

    assert rc == 1
    assert "empty" in out.lower()


def test_completely_empty_string_message_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Zero-length MSG string must also be rejected."""
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(commit.sys, "argv", ["commit.py", ""])
    rc = commit.main()
    out = capsys.readouterr().out

    assert rc == 1
    assert "empty" in out.lower()
