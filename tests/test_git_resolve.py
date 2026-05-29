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
    assert "must be 'ours', 'theirs', or 'both'" in out


def test_usage_mentions_both(monkeypatch, capsys) -> None:
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "both"])
    rc = resolve.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "both" in out


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


# ----- both / union resolution (_union_file) -----

CONFLICT = (
    "before\n"
    "<<<<<<< HEAD\n"
    " * @typesAudit 2026-05-28\n"
    "=======\n"
    " * @see \\Utag\\CommandFindTagTest\n"
    ">>>>>>> origin/master\n"
    "after\n"
)


def test_union_keeps_both_sides(tmp_path) -> None:
    f = tmp_path / "a.php"
    f.write_text(CONFLICT, encoding="utf-8")
    ok, err = resolve._union_file(str(f))
    assert ok and err == ""
    assert f.read_text(encoding="utf-8") == (
        "before\n"
        " * @typesAudit 2026-05-28\n"
        " * @see \\Utag\\CommandFindTagTest\n"
        "after\n"
    )


def test_union_drops_diff3_base(tmp_path) -> None:
    f = tmp_path / "b.php"
    f.write_text(
        "<<<<<<< HEAD\nours\n||||||| base\nbase line\n=======\ntheirs\n>>>>>>> branch\n",
        encoding="utf-8",
    )
    ok, _ = resolve._union_file(str(f))
    assert ok
    assert f.read_text(encoding="utf-8") == "ours\ntheirs\n"


def test_union_multiple_hunks(tmp_path) -> None:
    f = tmp_path / "c.php"
    f.write_text(CONFLICT + CONFLICT, encoding="utf-8")
    ok, _ = resolve._union_file(str(f))
    assert ok
    txt = f.read_text(encoding="utf-8")
    assert txt.count("@typesAudit") == 2
    assert txt.count("@see") == 2
    assert "<<<<<<<" not in txt and ">>>>>>>" not in txt and "=======" not in txt


def test_union_no_markers_fails(tmp_path) -> None:
    f = tmp_path / "d.php"
    f.write_text("clean file\nno markers\n", encoding="utf-8")
    ok, err = resolve._union_file(str(f))
    assert not ok
    assert "no conflict markers" in err
    # File left untouched
    assert f.read_text(encoding="utf-8") == "clean file\nno markers\n"


def test_union_unterminated_fails(tmp_path) -> None:
    f = tmp_path / "e.php"
    original = "<<<<<<< HEAD\nours\n=======\ntheirs\n"  # missing >>>>>>>
    f.write_text(original, encoding="utf-8")
    ok, err = resolve._union_file(str(f))
    assert not ok
    assert "unterminated" in err
    assert f.read_text(encoding="utf-8") == original


def test_both_end_to_end(monkeypatch, capsys, tmp_path) -> None:
    """side=both unions the file and stages it via git add."""
    import subprocess
    f = tmp_path / "x.php"
    f.write_text(CONFLICT, encoding="utf-8")
    calls: list[list[str]] = []

    def fake_git(args, timeout=10):
        calls.append(args)
        if args[:2] == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=".git\n", stderr="")
        if args[:3] == ["diff", "--name-only", "--diff-filter=U"]:
            n = len([c for c in calls if c[:3] == ["diff", "--name-only", "--diff-filter=U"]])
            stdout = f"{f}\n" if n == 1 else ""
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(resolve, "_git", fake_git)
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "both", str(f)])
    rc = resolve.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "git-resolve: both (1 file(s))" in out
    assert f"✓ {f}" in out
    # No checkout (union writes directly); file was staged
    assert not any(c[:1] == ["checkout"] for c in calls)
    assert any(c[:2] == ["add", "--"] for c in calls)
    # Both annotations survived
    txt = f.read_text(encoding="utf-8")
    assert "@typesAudit" in txt and "@see" in txt
