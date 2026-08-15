"""Unit tests for presets/git/resolve.py — argument validation."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


PRESET = Path(__file__).parent.parent / "presets" / "git" / "resolve.py"
_spec = importlib.util.spec_from_file_location("git_resolve", PRESET)
assert _spec is not None and _spec.loader is not None
resolve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve)

#: The shared helper `resolve` delegates to. `_list_conflicts` lives in
#: `_git_common` since #704 and therefore calls `_git_common._git`, not
#: `resolve._git` — so rebinding only the preset's name leaves the conflict
#: listing running real git against the test's temp tree, and every receipt
#: comes back "No conflicted files."
_common = sys.modules["_git_common"]


def _patch_git(monkeypatch, fn) -> None:
    """Intercept git for the preset *and* for the shared helper it calls."""
    monkeypatch.setattr(resolve, "_git", fn)
    monkeypatch.setattr(_common, "_git", fn)


def _z(*paths: str) -> str:
    """git's `-z` shape for a conflicted-path list: NUL AFTER every record.

    `_list_conflicts` reads `diff --name-only --diff-filter=U -z` since #1708,
    so a fixture that joins on LF hands the whole list back as ONE path and
    every receipt below reads `not conflicted`. Spelled as a helper rather than
    inline so the next fixture copies the terminator and not a separator - git
    terminates, it does not join, and the trailing empty record is what
    `_list_conflicts` drops.
    """
    return "".join(str(p) + chr(0) for p in paths)


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
    _patch_git(monkeypatch, lambda args, timeout=10: fake)
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
            stdout = _z("a.php", "b.php", "c.php") if len([c for c in calls if c[:3] == ["diff", "--name-only", "--diff-filter=U"]]) == 1 else ""
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")
        # checkout / add — succeed silently
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    _patch_git(monkeypatch, fake_git)
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
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=_z("a.php", "b.php"), stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    _patch_git(monkeypatch, fake_git)
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
    """side=both unions the file and stages it via git add.

    `force` because the fixture is `.php`: since #744 a union on source text is
    refused per file unless forced. The refusal itself is covered in
    test_git_resolve_source_guard_744.py; this test stays on the union path.
    """
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
            stdout = _z(f) if n == 1 else ""
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    _patch_git(monkeypatch, fake_git)
    monkeypatch.setattr(resolve, "_validate_paths", lambda ps: {p: None for p in ps})
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "both", str(f), "force"])
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
            stdout = _z(f)  # still conflicted (never staged), whichever call
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    _patch_git(monkeypatch, fake_git)
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
            stdout = _z(f) if n == 1 else ""
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    _patch_git(monkeypatch, fake_git)
    monkeypatch.setattr(resolve, "_validate_paths", lambda ps: {p: "validate: ok" for p in ps})
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "ours", str(f)])
    rc = resolve.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "markers: clean | validate: ok" in out


def test_resolve_renders_a_check_that_could_not_run(monkeypatch, tmp_path, capsys) -> None:
    """A digest that never ran is printed, not dropped by the caller's `if digest`.

    `markers: clean` with no digest line is indistinguishable from a check that
    ran and passed, which is the silence #880 is about. The render has to carry
    the doubt.
    """
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
            stdout = _z(f) if n == 1 else ""
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    def timing_out(cmd, *a, **k):
        raise subprocess.TimeoutExpired(cmd, 90)

    _patch_git(monkeypatch, fake_git)
    monkeypatch.setattr(resolve.subprocess, "run", timing_out)
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "ours", str(f)])
    rc = resolve.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "not checked (timed out)" in out


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
    import json
    import subprocess
    seen: dict[str, object] = {}

    def fake_run(cmd, *a, **k):
        seen["cmd"] = cmd
        seen["payload"] = json.loads(k["input"])
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="validate: x\nphplint     : ok\n", stderr="")

    monkeypatch.setattr(resolve.subprocess, "run", fake_run)
    monkeypatch.setattr(resolve.os.path, "isfile", lambda p: True)
    resolve._validate_paths([__file__])
    # The op string carries no fields at all: everything the receiver re-parses
    # travels in the payload, which is the whole point of the route (#878).
    assert seen["cmd"][-1] == "validate:@-"
    payload = seen["payload"]
    # declarative scope: the @syntax sentinel keeps the scope in config, not code
    assert payload["tools"] == resolve._SYNTAX_FILTER
    assert payload["paths"] == [__file__]
    # noisy semantic/diagnostic validators must NOT be named in the request
    for noisy in ("lsp-diag", "pyright", "psr", "tsc-check", "prettier-check", "git-status"):
        assert noisy not in json.dumps(payload)


def test_validate_falls_back_to_name_list_when_syntax_unmatched(monkeypatch) -> None:
    """@syntax matches nothing (old config) → retry with the hardcoded list."""
    import json
    import subprocess
    payloads: list[dict] = []

    def fake_run(cmd, *a, **k):
        payloads.append(json.loads(k["input"]))
        if payloads[-1]["tools"] == resolve._SYNTAX_FILTER:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="no validators matched filter\n", stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="validate: x\nphplint     : ok\n", stderr="")

    monkeypatch.setattr(resolve.subprocess, "run", fake_run)
    monkeypatch.setattr(resolve.os.path, "isfile", lambda p: True)
    digests = resolve._validate_paths(["x.php"])
    assert len(payloads) == 2
    assert payloads[1]["tools"] == list(resolve._SYNTAX_VALIDATORS)
    assert digests["x.php"] == "validate: ok"


def test_validate_no_matching_validator_returns_none(monkeypatch) -> None:
    """No syntax validator for this file type → None (no false 'ok' line).

    A block with no rows is that answer. `no validators configured` was the
    fixture here and is a different fact — see the case below (#883).
    """
    import subprocess
    monkeypatch.setattr(
        resolve.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(args=a, returncode=0, stdout="validate: x\n", stderr=""),
    )
    monkeypatch.setattr(resolve.os.path, "isfile", lambda p: True)
    assert resolve._validate_paths([__file__])[__file__] is None


def test_validate_no_validators_configured_is_not_checked(monkeypatch) -> None:
    """Nothing ran, so nothing may render as a pass (#883)."""
    import subprocess
    monkeypatch.setattr(
        resolve.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(args=a, returncode=0, stdout="no validators configured\n", stderr=""),
    )
    monkeypatch.setattr(resolve.os.path, "isfile", lambda p: True)
    assert "not checked" in resolve._validate_paths([__file__])[__file__]


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
    import json
    import subprocess
    files = []
    for name in ("a.php", "b.php", "c.php"):
        p = tmp_path / name
        p.write_text("x\n", encoding="utf-8")
        files.append(str(p))
    payloads: list[dict] = []

    def fake_run(cmd, *a, **k):
        payloads.append(json.loads(k["input"]))
        blocks = "".join(f"validate: {f}\nphplint     : ok\n" for f in files)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=blocks, stderr="")

    monkeypatch.setattr(resolve.subprocess, "run", fake_run)
    digests = resolve._validate_paths(files)
    # exactly one subprocess for the whole batch
    assert len(payloads) == 1
    # every file in the one payload's list, folded back per file. tmp_path is
    # absolute, so on Windows each of these carries a drive-letter colon — the
    # case that broke when the sender filtered its own separators out (#878).
    assert payloads[0]["paths"] == files
    assert set(digests) == set(files)
    assert all(d == "validate: ok" for d in digests.values())


# ----- per-block resolution (issue #305) -----

# Three conflict blocks — numbered 1, 2, 3 as git-conflicts lists them.
THREE_BLOCKS = (
    "head\n"
    "<<<<<<< HEAD\n"
    "ours-1\n"
    "=======\n"
    "theirs-1\n"
    ">>>>>>> branch\n"
    "middle\n"
    "<<<<<<< HEAD\n"
    "ours-2\n"
    "=======\n"
    "theirs-2\n"
    ">>>>>>> branch\n"
    "more\n"
    "<<<<<<< HEAD\n"
    "ours-3\n"
    "=======\n"
    "theirs-3\n"
    ">>>>>>> branch\n"
    "tail\n"
)


def test_count_blocks(tmp_path) -> None:
    f = tmp_path / "a.py"
    f.write_text(THREE_BLOCKS, encoding="utf-8")
    assert resolve._count_blocks(str(f)) == 3


def test_resolve_blocks_selected_only(tmp_path) -> None:
    """Blocks 1 and 3 take ours; block 2 keeps its markers verbatim."""
    f = tmp_path / "a.py"
    f.write_text(THREE_BLOCKS, encoding="utf-8")
    ok, err, resolved, total = resolve._resolve_blocks(str(f), "ours", {1, 3})
    assert ok and err == ""
    assert (resolved, total) == (2, 3)
    txt = f.read_text(encoding="utf-8")
    # block 1 resolved to ours
    assert "ours-1" in txt and "theirs-1" not in txt
    # block 2 untouched — markers intact
    assert "<<<<<<< HEAD\nours-2\n=======\ntheirs-2\n>>>>>>> branch" in txt
    # block 3 resolved to ours
    assert "ours-3" in txt and "theirs-3" not in txt


def test_resolve_blocks_theirs_side(tmp_path) -> None:
    f = tmp_path / "a.py"
    f.write_text(THREE_BLOCKS, encoding="utf-8")
    ok, _, resolved, total = resolve._resolve_blocks(str(f), "theirs", {2})
    assert ok and (resolved, total) == (1, 3)
    txt = f.read_text(encoding="utf-8")
    assert "theirs-2" in txt and "ours-2" not in txt
    # blocks 1 and 3 still conflicted
    assert txt.count("<<<<<<<") == 2


def test_resolve_blocks_both_union(tmp_path) -> None:
    f = tmp_path / "a.py"
    f.write_text(THREE_BLOCKS, encoding="utf-8")
    ok, _, resolved, total = resolve._resolve_blocks(str(f), "both", {1})
    assert ok and (resolved, total) == (1, 3)
    txt = f.read_text(encoding="utf-8")
    # block 1 union keeps both sides, drops markers
    assert "ours-1\ntheirs-1\n" in txt
    assert txt.count("<<<<<<<") == 2  # 2 and 3 remain


def test_resolve_blocks_out_of_range(tmp_path) -> None:
    f = tmp_path / "a.py"
    f.write_text(THREE_BLOCKS, encoding="utf-8")
    ok, err, _, total = resolve._resolve_blocks(str(f), "ours", {4})
    assert not ok
    assert "out of range" in err and total == 3
    # file untouched
    assert f.read_text(encoding="utf-8") == THREE_BLOCKS


def test_resolve_blocks_all_blocks_clean(tmp_path) -> None:
    """Selecting every block leaves no markers."""
    f = tmp_path / "a.py"
    f.write_text(THREE_BLOCKS, encoding="utf-8")
    ok, _, resolved, total = resolve._resolve_blocks(str(f), "ours", {1, 2, 3})
    assert ok and (resolved, total) == (3, 3)
    assert "<<<<<<<" not in f.read_text(encoding="utf-8")


def _fake_git_single(f, calls):
    """Build a fake_git for one conflicted file `f`, recording into `calls`."""
    import subprocess

    def fake_git(args, timeout=10):
        calls.append(args)
        if args[:2] == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=".git\n", stderr="")
        if args[:3] == ["diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=_z(f), stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    return fake_git


def test_partial_resolve_reports_n_of_m_not_staged(monkeypatch, capsys, tmp_path) -> None:
    """Partial resolve: 'N of M blocks resolved', markers kept, NOT staged."""
    f = tmp_path / "x.py"
    f.write_text(THREE_BLOCKS, encoding="utf-8")
    calls: list[list[str]] = []
    _patch_git(monkeypatch, _fake_git_single(f, calls))
    monkeypatch.setattr(resolve, "_validate_paths", lambda ps: {p: None for p in ps})
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "ours", str(f), "1,3"])
    rc = resolve.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "2 of 3 block(s) resolved, file still conflicted" in out
    assert "Not staged" in out
    # remaining markers preserved
    assert "<<<<<<<" in f.read_text(encoding="utf-8")
    # NEVER staged
    assert not any(c[:2] == ["add", "--"] for c in calls)


def test_full_selector_stages_clean(monkeypatch, capsys, tmp_path) -> None:
    """Selecting every block clears markers → file is staged."""
    f = tmp_path / "x.py"
    f.write_text(THREE_BLOCKS, encoding="utf-8")
    calls: list[list[str]] = []
    _patch_git(monkeypatch, _fake_git_single(f, calls))
    monkeypatch.setattr(resolve, "_validate_paths", lambda ps: {p: "validate: ok" for p in ps})
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "theirs", str(f), "1,2,3"])
    rc = resolve.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "3 of 3 block(s) resolved" in out
    assert "staged" in out
    assert "markers: clean | validate: ok" in out
    assert "<<<<<<<" not in f.read_text(encoding="utf-8")
    assert any(c[:2] == ["add", "--"] for c in calls)


def test_blocks_invalid_token_rejected(monkeypatch, capsys) -> None:
    import subprocess
    fake = subprocess.CompletedProcess(args=["git"], returncode=0, stdout=".git\n", stderr="")
    _patch_git(monkeypatch, lambda args, timeout=10: fake)
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "ours", "x.py", "1,abc"])
    rc = resolve.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKS must be 1-indexed" in out


def test_blocks_zero_rejected(monkeypatch, capsys) -> None:
    import subprocess
    fake = subprocess.CompletedProcess(args=["git"], returncode=0, stdout=".git\n", stderr="")
    _patch_git(monkeypatch, lambda args, timeout=10: fake)
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "ours", "x.py", "0"])
    rc = resolve.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKS must be 1-indexed" in out


def test_blocks_require_single_file(monkeypatch, capsys) -> None:
    """A block selector with a CSV multi-file target is rejected."""
    import subprocess

    def fake_git(args, timeout=10):
        if args[:2] == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=".git\n", stderr="")
        if args[:3] == ["diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=_z("a.py", "b.py"), stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    _patch_git(monkeypatch, fake_git)
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "ours", "a.py,b.py", "1"])
    rc = resolve.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "requires exactly one PATH" in out


def test_blocks_require_single_file_not_all(monkeypatch, capsys) -> None:
    """A block selector with target 'all' is rejected."""
    import subprocess

    def fake_git(args, timeout=10):
        if args[:2] == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=".git\n", stderr="")
        if args[:3] == ["diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=_z("a.py", "b.py"), stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    _patch_git(monkeypatch, fake_git)
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "ours", "all", "1"])
    rc = resolve.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "requires exactly one PATH" in out


def test_no_blocks_is_whole_file_backcompat(monkeypatch, capsys, tmp_path) -> None:
    """No block selector → unchanged whole-file behavior (checkout + stage)."""
    import subprocess
    f = tmp_path / "x.py"
    f.write_text("clean\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_git(args, timeout=10):
        calls.append(args)
        if args[:2] == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=".git\n", stderr="")
        if args[:3] == ["diff", "--name-only", "--diff-filter=U"]:
            n = len([c for c in calls if c[:3] == ["diff", "--name-only", "--diff-filter=U"]])
            stdout = _z(f) if n == 1 else ""
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    _patch_git(monkeypatch, fake_git)
    monkeypatch.setattr(resolve, "_validate_paths", lambda ps: {p: None for p in ps})
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "ours", str(f)])
    rc = resolve.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "git-resolve: ours (1 file(s))" in out
    # whole-file path uses checkout --ours, then add
    assert any(c[:2] == ["checkout", "--ours"] for c in calls)
    assert any(c[:2] == ["add", "--"] for c in calls)


def test_resolve_blocks_unterminated(tmp_path) -> None:
    f = tmp_path / "a.py"
    f.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n", encoding="utf-8")  # no >>>>>>>
    ok, err, _, _ = resolve._resolve_blocks(str(f), "ours", {1})
    assert not ok
    assert "unterminated" in err
