"""Unit tests for presets/git/resolve.py — argument validation."""
from __future__ import annotations

import importlib.util
from pathlib import Path


PRESET = Path(__file__).parent.parent / "presets" / "git" / "resolve.py"
_spec = importlib.util.spec_from_file_location("git_resolve", PRESET)
assert _spec is not None and _spec.loader is not None
resolve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve)


def test_missing_args_prints_usage(monkeypatch, capsys) -> None:
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "ours"])
    rc = resolve.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "usage" in out


def test_invalid_side_rejected(monkeypatch, capsys) -> None:
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "mine", "x.py"])
    rc = resolve.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "must be 'ours' or 'theirs'" in out


def test_side_case_insensitive(monkeypatch, capsys) -> None:
    """SIDE accepts uppercase."""
    import subprocess
    fake = subprocess.CompletedProcess(args=["git"], returncode=1, stdout="", stderr="not a git repo")
    monkeypatch.setattr(resolve, "_git", lambda args, timeout=10: fake)
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "OURS", "x.py"])
    rc = resolve.main()
    # Reaches the "not in a git repo" check — proves SIDE was accepted
    out = capsys.readouterr().out
    assert "not inside a git repository" in out
    assert rc == 1


def test_comma_separated_paths(monkeypatch, capsys) -> None:
    """PATH accepts comma-separated list — resolves each in one call."""
    import subprocess
    calls: list[list[str]] = []

    def fake_git(args, timeout=10):
        calls.append(args)
        if args[:2] == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=".git\n", stderr="")
        if args[:3] == ["diff", "--name-only", "--diff-filter=U"]:
            # First call lists conflicts; subsequent (after resolves) returns empty
            stdout = "a.php\nb.php\nc.php\n" if len([c for c in calls if c[:3] == ["diff", "--name-only", "--diff-filter=U"]]) == 1 else ""
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")
        # checkout / add — succeed silently
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(resolve, "_git", fake_git)
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "theirs", "a.php,b.php"])
    rc = resolve.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "git-resolve: theirs (2 file(s))" in out
    assert "✓ a.php" in out
    assert "✓ b.php" in out
    # Both files received `checkout --theirs --` and `add --`
    checkouts = [c for c in calls if c[:2] == ["checkout", "--theirs"]]
    adds = [c for c in calls if c[:2] == ["add", "--"]]
    assert len(checkouts) == 2
    assert len(adds) == 2


def test_comma_separated_unknown_path_rejected(monkeypatch, capsys) -> None:
    """Unknown path in CSV list is rejected before any checkout runs."""
    import subprocess
    calls: list[list[str]] = []

    def fake_git(args, timeout=10):
        calls.append(args)
        if args[:2] == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=".git\n", stderr="")
        if args[:3] == ["diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="a.php\nb.php\n", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(resolve, "_git", fake_git)
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "ours", "a.php,zzz.php"])
    rc = resolve.main()
    out = capsys.readouterr().out

    assert rc == 1
    assert "not conflicted" in out
    assert "'zzz.php'" in out
    # No checkout/add happened — atomic validation
    assert not any(c[:2] == ["checkout", "--ours"] for c in calls)
