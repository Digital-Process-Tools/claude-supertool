"""Regressions caught by review on PR #395 — each of these shipped green.

Three independent reviewers, four defects. The common shape: the fix was
correct for the case it was written for and wrong one step to the side.
"""
import json
import os
from pathlib import Path

import pytest

import supertool


SPEC = {"cmd": "prettier --write {file}", "match": "*.md", "hooks_into": ["edit"]}


def _repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / ".git").mkdir()
    return root


# ---------------------------------------------------------------------------
# #392 follow-up: the footer must key on an ATTEMPTED mutation, not a landed
# write. _retract_write decrements on rollback and a failed edit never reaches
# _atomic_write at all — both are exactly when a wrong-branch hypothesis helps.
# ---------------------------------------------------------------------------

def test_batch_with_a_failing_sub_op_still_reports_the_branch(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(supertool, "_current_branch", lambda: "my-feature")
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([
        {"op": "edit", "path": str(f), "old": "nope = 1", "new": "a = 2"},
    ]))
    out = supertool.dispatch(f"batch:@{payload}")
    assert "old string not found" in out
    assert out.count("[branch: my-feature]") == 1


def test_nested_batch_with_a_failing_sub_op_still_reports_the_branch(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(supertool, "_current_branch", lambda: "my-feature")
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    inner = tmp_path / "inner.json"
    inner.write_text(json.dumps([
        {"op": "edit", "path": str(f), "old": "nope = 1", "new": "a = 2"},
    ]))
    outer = tmp_path / "outer.json"
    outer.write_text(json.dumps([{"op": "batch", "path": f"@{inner}"}]))
    out = supertool.dispatch(f"batch:@{outer}")
    assert out.count("[branch: my-feature]") == 1


def test_read_only_batch_still_has_no_footer(tmp_path: Path, monkeypatch) -> None:
    """The attempt counter must not make every batch report a branch."""
    monkeypatch.setattr(supertool, "_current_branch", lambda: "my-feature")
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([{"op": "read", "path": str(f)}]))
    assert "[branch:" not in supertool.dispatch(f"batch:@{payload}")


# ---------------------------------------------------------------------------
# #393 follow-ups
# ---------------------------------------------------------------------------

def test_gate_follows_symlinks_to_the_real_repo(tmp_path: Path) -> None:
    """abspath keeps the symlink's own location, so the walk climbed past the
    real repo to the filesystem root and found no config that was right there."""
    root = _repo(tmp_path, "real")
    (root / ".prettierrc").write_text("{}\n")
    sub = root / "sub"
    sub.mkdir()
    (sub / "x.md").write_text("x\n")
    link = tmp_path / "link"
    os.symlink(sub, link)
    supertool._REPO_ROOT_WALK_CACHE.clear()
    assert supertool._repo_opts_into_formatter("prettier", SPEC, str(link / "x.md"))


def test_format_staged_applies_the_gate(tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path, "staged")
    f = root / "x.md"
    f.write_text("x\n")
    monkeypatch.chdir(root)
    supertool._REPO_ROOT_WALK_CACHE.clear()
    monkeypatch.setattr(supertool, "_load_config", lambda: {"formatters": {"prettier": SPEC}})
    monkeypatch.setattr(supertool, "_formatter_run_one",
                        lambda *a, **k: pytest_fail_marker())
    out = supertool.op_format(str(f), gated=True)
    assert "skipped" in out


def test_named_format_is_never_gated(tmp_path: Path, monkeypatch) -> None:
    """`format:PATH` names the file — the caller already said what they want."""
    root = _repo(tmp_path, "named")
    f = root / "x.md"
    f.write_text("x\n")
    supertool._REPO_ROOT_WALK_CACHE.clear()
    monkeypatch.setattr(supertool, "_load_config", lambda: {"formatters": {"prettier": SPEC}})
    ran = []
    monkeypatch.setattr(supertool, "_formatter_run_one",
                        lambda n, s, p: ran.append(n) or {"ok": True, "name": n})
    supertool.op_format(str(f))
    assert ran == ["prettier"]


def pytest_fail_marker():  # pragma: no cover - only reached if the gate leaks
    raise AssertionError("formatter ran despite the gate")


# ---------------------------------------------------------------------------
# #394 follow-up: the hint must not misdiagnose an unrelated TOML error whose
# payload merely mentions ''' inside an ordinary string.
# ---------------------------------------------------------------------------

def test_hint_stays_silent_when_no_literal_block_opens() -> None:
    raw = 'new = "isn\'t it\'\'\' odd"\nbogus key here without value\n'
    assert supertool._toml_delimiter_hint(raw) == ""


def test_hint_still_fires_when_a_value_opens_with_the_delimiter() -> None:
    raw = "path = 'x.py'\nnew = '''a ''' b'''\n"
    assert "basic" in supertool._toml_delimiter_hint(raw)


def test_hint_routes_its_glyph_through_mark(monkeypatch) -> None:
    """Every other arrow in the file goes through mark(); this one was written
    as a literal, so plain mode leaked a multibyte glyph into hook/CI output."""
    raw = "path = 'x.py'\nnew = '''a ''' b'''\n"
    monkeypatch.setenv("SUPERTOOL_PLAIN", "1")
    out = supertool._toml_delimiter_hint(raw)
    assert "\u21b3" not in out
    assert "->" in out


# ---------------------------------------------------------------------------
# #393 follow-up: _FORMATTER_SKIPS is module-level and drained only on the
# normal return path. An exception that escapes skips the drain, and the next
# top-level call reports skips belonging to a call that already died.
# ---------------------------------------------------------------------------

def test_a_dying_call_does_not_leak_its_formatter_skips(
    tmp_path: Path, monkeypatch
) -> None:
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")

    def boom(*a, **k):
        supertool._FORMATTER_SKIPS.append("prettier")
        raise RuntimeError("op died past the skip bookkeeping")

    monkeypatch.setattr(supertool, "op_read", boom)
    supertool._FORMATTER_SKIPS.clear()
    with pytest.raises(RuntimeError):
        supertool.dispatch(f"read:{f}")
    assert supertool._FORMATTER_SKIPS == []
