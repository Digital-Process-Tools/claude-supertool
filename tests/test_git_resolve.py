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
    monkeypatch.setattr(resolve, "_validate_paths", lambda ps: {p: None for p in ps})
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
    monkeypatch.setattr(resolve, "_validate_paths", lambda ps: {p: None for p in ps})
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


# ----- post-resolve marker scan (_scan_markers) -----


def test_scan_markers_detects_leftover(tmp_path) -> None:
    """A file still carrying <<<<<<< / >>>>>>> is flagged with 1-indexed lines."""
    f = tmp_path / "a.php"
    f.write_text("clean\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> x\n", encoding="utf-8")
    lines = resolve._scan_markers(str(f))
    assert lines == [2, 6]


def test_scan_markers_ignores_bare_equals(tmp_path) -> None:
    """A row of === decoration (no <<< / >>>) is NOT a false positive."""
    f = tmp_path / "b.md"
    f.write_text("Title\n=======\nbody\n", encoding="utf-8")
    assert resolve._scan_markers(str(f)) == []


def test_scan_markers_binary_is_safe(tmp_path) -> None:
    """Non-UTF-8 (binary) files scan clean — no crash."""
    f = tmp_path / "c.bin"
    f.write_bytes(b"\xff\xfe<<<<<<<\x00")
    assert resolve._scan_markers(str(f)) == []


def test_marker_leftover_blocks_staging(monkeypatch, capsys, tmp_path) -> None:
    """HARD GATE: a resolve that leaves a conflict marker is NOT staged."""
    import subprocess
    f = tmp_path / "x.php"
    # checkout 'succeeds' but leaves a marker behind (simulated corruption)
    f.write_text("ok\n<<<<<<< HEAD\nbad\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_git(args, timeout=10):
        calls.append(args)
        if args[:2] == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=".git\n", stderr="")
        if args[:3] == ["diff", "--name-only", "--diff-filter=U"]:
            n = len([c for c in calls if c[:3] == ["diff", "--name-only", "--diff-filter=U"]])
            stdout = f"{f}\n" if n == 1 else f"{f}\n"  # still conflicted (never staged)
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(resolve, "_git", fake_git)
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "ours", str(f)])
    rc = resolve.main()
    out = capsys.readouterr().out

    assert rc == 1
    assert "conflict markers remain" in out
    # never staged
    assert not any(c[:2] == ["add", "--"] for c in calls)


def test_validate_digest_in_receipt(monkeypatch, capsys, tmp_path) -> None:
    """A clean resolve shows 'markers: clean' + the validate digest per file."""
    import subprocess
    f = tmp_path / "x.php"
    f.write_text("clean php\n", encoding="utf-8")
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
    monkeypatch.setattr(resolve, "_validate_paths", lambda ps: {p: "validate: ok" for p in ps})
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "ours", str(f)])
    rc = resolve.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "markers: clean | validate: ok" in out


def test_validate_digest_parses_err_rows(monkeypatch) -> None:
    """_validate_paths digests validator 'N err' rows into a warn line."""
    import subprocess
    sample = "validate: x.php\nxmllint     : ok          (5ms)\nphplint     : 2 err       (9ms)\n"
    monkeypatch.setattr(
        resolve.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(args=a, returncode=0, stdout=sample, stderr=""),
    )
    # path must exist for the guard; use this test file itself, mapped to x.php
    monkeypatch.setattr(resolve.os.path, "isfile", lambda p: True)
    digests = resolve._validate_paths(["x.php"])
    digest = digests["x.php"]
    assert digest is not None
    assert "⚠" in digest and "phplint 2 err" in digest


def test_validate_scopes_to_syntax_validators(monkeypatch) -> None:
    """The validate call prefers the declarative @syntax filter."""
    import subprocess
    seen: dict[str, list] = {}

    def fake_run(cmd, *a, **k):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="validate: x\nphplint     : ok\n", stderr="")

    monkeypatch.setattr(resolve.subprocess, "run", fake_run)
    monkeypatch.setattr(resolve.os.path, "isfile", lambda p: True)
    resolve._validate_paths([__file__])
    arg = seen["cmd"][-1]  # validate:PATH:FILTER
    assert arg.startswith("validate:")
    # declarative scope: the @syntax sentinel keeps the scope in config, not code
    assert arg.endswith(":@syntax")
    # noisy semantic/diagnostic validators must NOT be named in the request
    for noisy in ("lsp-diag", "pyright", "psr", "tsc-check", "prettier-check", "git-status"):
        assert noisy not in arg


def test_validate_falls_back_to_name_list_when_syntax_unmatched(monkeypatch) -> None:
    """@syntax matches nothing (old config) → retry with the hardcoded list."""
    import subprocess
    cmds: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        cmds.append(cmd)
        if cmd[-1].endswith(":@syntax"):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="no validators matched filter\n", stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="validate: x\nphplint     : ok\n", stderr="")

    monkeypatch.setattr(resolve.subprocess, "run", fake_run)
    monkeypatch.setattr(resolve.os.path, "isfile", lambda p: True)
    digests = resolve._validate_paths(["x.php"])
    assert len(cmds) == 2
    assert cmds[1][-1].endswith(":" + ",".join(resolve._SYNTAX_VALIDATORS))
    assert digests["x.php"] == "validate: ok"


def test_validate_no_matching_validator_returns_none(monkeypatch) -> None:
    """No syntax validator for this file type → None (no false 'ok' line)."""
    import subprocess
    monkeypatch.setattr(
        resolve.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(args=a, returncode=0, stdout="no validators configured\n", stderr=""),
    )
    monkeypatch.setattr(resolve.os.path, "isfile", lambda p: True)
    assert resolve._validate_paths([__file__])[__file__] is None


def test_validate_all_ok_returns_ok(monkeypatch) -> None:
    """All parser rows ok → 'validate: ok'."""
    import subprocess
    monkeypatch.setattr(
        resolve.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(args=a, returncode=0, stdout="validate: x\nxmllint     : ok          (5ms)\n", stderr=""),
    )
    monkeypatch.setattr(resolve.os.path, "isfile", lambda p: True)
    assert resolve._validate_paths([__file__])[__file__] == "validate: ok"


def test_validate_paths_single_supertool_call_for_many_files(monkeypatch, tmp_path) -> None:
    """The digest shells supertool ONCE for all resolved files (issue #306)."""
    import subprocess
    files = []
    for name in ("a.php", "b.php", "c.php"):
        p = tmp_path / name
        p.write_text("x\n", encoding="utf-8")
        files.append(str(p))
    cmds: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        cmds.append(cmd)
        blocks = "".join(f"validate: {f}\nphplint     : ok\n" for f in files)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=blocks, stderr="")

    monkeypatch.setattr(resolve.subprocess, "run", fake_run)
    digests = resolve._validate_paths(files)
    # exactly one subprocess for the whole batch
    assert len(cmds) == 1
    # all files comma-joined into the single validate arg, folded back per file
    arg = cmds[0][-1]
    assert all(f in arg for f in files)
    assert set(digests) == set(files)
    assert all(d == "validate: ok" for d in digests.values())
