"""Unit tests for presets/git/commit.py — error parsing + empty-msg guard."""
from __future__ import annotations

import importlib.util
from pathlib import Path


PRESET = Path(__file__).parent.parent / "presets" / "git" / "commit.py"
_spec = importlib.util.spec_from_file_location("git_commit", PRESET)
assert _spec is not None and _spec.loader is not None
commit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(commit)


def test_first_error_line_picks_error_keyword() -> None:
    text = "Running pre-commit\nok 12 files\nfatal: hook rejected\nbye"
    assert commit._first_error_line(text) == "fatal: hook rejected"


def test_first_error_line_picks_emoji_marker() -> None:
    text = "step 1\nstep 2\n❌ Push blocked. Fix violations\n"
    assert "❌" in commit._first_error_line(text)


def test_first_error_line_falls_back_to_last_nonempty() -> None:
    assert commit._first_error_line("a\nb\n\n") == "b"


def test_first_error_line_empty_input() -> None:
    assert commit._first_error_line("") == ""
    assert commit._first_error_line("\n\n") == ""


def test_empty_message_rejected(monkeypatch, capsys) -> None:
    monkeypatch.setattr(commit.sys, "argv", ["commit.py", "   "])
    rc = commit.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "empty" in out


def test_no_args_prints_usage(monkeypatch, capsys) -> None:
    monkeypatch.setattr(commit.sys, "argv", ["commit.py"])
    rc = commit.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "usage" in out


def test_no_edit_outside_merge_rejected(monkeypatch, capsys, tmp_path) -> None:
    """--no-edit needs MERGE_HEAD or CHERRY_PICK_HEAD; reject otherwise."""
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commit.sys, "argv", ["commit.py", "--no-edit"])
    rc = commit.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "--no-edit" in out
    assert "MERGE_HEAD" in out or "merge" in out.lower()


def test_next_hint_recommends_git_push_op(monkeypatch, capsys, tmp_path) -> None:
    """With an upstream set and no existing MR, the Next hint points at the
    git-push op — not raw `git push` (issue #310)."""
    import subprocess
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    monkeypatch.chdir(work)
    subprocess.run(["git", "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "config", "user.name", "t"], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], check=True)
    (work / "a.txt").write_text("hi\n")
    subprocess.run(["git", "add", "a.txt"], check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"], check=True)

    # Stage a new change to commit via the preset.
    (work / "a.txt").write_text("hi\nmore\n")
    subprocess.run(["git", "add", "a.txt"], check=True)

    monkeypatch.setattr(commit, "_existing_mr_for_branch", lambda branch: None)
    monkeypatch.setattr(commit.sys, "argv", ["commit.py", "second"])

    rc = commit.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Next: ./supertool 'git-push'" in out
    assert "Next: git push (" not in out


def test_no_edit_during_merge_uses_prepared_message(monkeypatch, capsys, tmp_path) -> None:
    """With MERGE_HEAD present, --no-edit calls `git commit --no-edit`."""
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "config", "user.name", "t"], check=True)
    (tmp_path / "a.txt").write_text("hi\n")
    subprocess.run(["git", "add", "a.txt"], check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], check=True)
    # Fake a merge state: MERGE_HEAD + MERGE_MSG + a staged change
    gd = subprocess.run(["git", "rev-parse", "--git-dir"],
                        capture_output=True, text=True, check=True).stdout.strip()
    Path(gd, "MERGE_HEAD").write_text("0" * 40 + "\n")
    Path(gd, "MERGE_MSG").write_text("Merge fake\n")
    (tmp_path / "a.txt").write_text("hi\nmerged\n")
    subprocess.run(["git", "add", "a.txt"], check=True)

    calls: list[list[str]] = []
    real_run = commit._git

    def spy(args, timeout=30):
        calls.append(list(args))
        # Short-circuit the actual commit so we don't depend on a clean merge:
        # we only want to verify --no-edit reaches git.
        if list(args)[:2] == ["commit", "--no-edit"]:
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()
        return real_run(args, timeout=timeout)
    monkeypatch.setattr(commit, "_git", spy)
    monkeypatch.setattr(commit.sys, "argv", ["commit.py", "--no-edit"])

    rc = commit.main()
    capsys.readouterr()  # drain
    assert ["commit", "--no-edit"] in calls
    # No -m was passed for any commit invocation
    assert all("-m" not in c for c in calls if c[:1] == ["commit"])


# --- Co-Authored-By trailer (issue #286) ---------------------------------

def test_coauthor_appended_when_absent(monkeypatch) -> None:
    monkeypatch.delenv("SUPERTOOL_COAUTHOR", raising=False)
    out = commit._with_coauthor("feat: thing")
    assert out == "feat: thing\n\nCo-Authored-By: Max <noreply>"


def test_coauthor_blank_line_separates_body(monkeypatch) -> None:
    monkeypatch.delenv("SUPERTOOL_COAUTHOR", raising=False)
    out = commit._with_coauthor("subject\n\nbody line")
    assert out == "subject\n\nbody line\n\nCo-Authored-By: Max <noreply>"


def test_coauthor_skipped_when_already_present(monkeypatch) -> None:
    monkeypatch.delenv("SUPERTOOL_COAUTHOR", raising=False)
    msg = "fix: x\n\nCo-Authored-By: Someone <s@e>"
    assert commit._with_coauthor(msg) == msg


def test_coauthor_skip_is_case_insensitive(monkeypatch) -> None:
    monkeypatch.delenv("SUPERTOOL_COAUTHOR", raising=False)
    msg = "fix: x\n\nco-authored-by: Someone <s@e>"
    assert commit._with_coauthor(msg) == msg


def test_coauthor_configurable_via_env(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_COAUTHOR", "Jane Doe <jane@x>")
    out = commit._with_coauthor("chore: y")
    assert out == "chore: y\n\nCo-Authored-By: Jane Doe <jane@x>"


def test_coauthor_disabled_via_env(monkeypatch) -> None:
    for val in ("", "none", "off", "false"):
        monkeypatch.setenv("SUPERTOOL_COAUTHOR", val)
        assert commit._with_coauthor("chore: y") == "chore: y"


def test_coauthor_in_real_commit(monkeypatch, tmp_path) -> None:
    """End-to-end: the trailer lands in the actual commit message."""
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "config", "user.name", "t"], check=True)
    (tmp_path / "a.txt").write_text("hi\n")
    monkeypatch.setenv("SUPERTOOL_COAUTHOR", "Max <noreply>")
    monkeypatch.setattr(commit.sys, "argv", ["commit.py", "feat: a", "a.txt"])
    rc = commit.main()
    assert rc == 0
    body = subprocess.run(["git", "log", "-1", "--pretty=%B"],
                          capture_output=True, text=True, check=True).stdout
    assert "Co-Authored-By: Max <noreply>" in body
    assert body.count("Co-Authored-By:") == 1
