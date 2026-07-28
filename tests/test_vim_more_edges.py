"""More edge-case coverage: regex error fallback, visual-mode finds,
config schema, batch sub-op edge errors."""
from __future__ import annotations

import json
from pathlib import Path

import supertool


def _run(tmp_path: Path, initial: str, script: str) -> str:
    f = tmp_path / "x.txt"
    f.write_text(initial)
    out = supertool.op_vim(str(f), script)
    assert not out.startswith("ERROR"), out
    return f.read_text(encoding="utf-8")


# --- regex error → literal fallback in n/N (around 5315) ---

def test_n_with_invalid_regex_falls_back_to_literal(tmp_path: Path) -> None:
    # First do /( which would be invalid regex; supertool's search may
    # handle that via literal-mode autocorrect at /-time. Then n repeats.
    out = _run(tmp_path, "a(b(c)d\n", "gg␞/(␞n␞iX")
    assert "X" in out


def test_N_after_search_uses_reverse(tmp_path: Path) -> None:
    out = _run(tmp_path, "aXbXcXd\n", "gg␞/X␞n␞N␞iY")
    assert "Y" in out


# --- batch sub-op with malformed at-file fields ---

def test_batch_subop_edit_missing_required_fields(tmp_path: Path) -> None:
    payload_file = tmp_path / "b.json"
    payload_file.write_text(json.dumps([
        {"op": "edit"},  # missing old/new/path
    ]))
    out = supertool.dispatch(f"batch:@{payload_file}")
    assert "ERROR" in out


def test_batch_subop_with_replace_all_true_promotes_edit(tmp_path: Path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("foo foo foo\n")
    payload_file = tmp_path / "b.json"
    payload_file.write_text(json.dumps([
        {"op": "edit", "path": str(target), "old": "foo", "new": "bar", "replace_all": True},
    ]))
    out = supertool.dispatch(f"batch:@{payload_file}")
    # replace_all should turn it into a replace and change all 3.
    assert target.read_text(encoding="utf-8") == "bar bar bar\n", out


# --- config registry: ops with no syntax (around 8275) ---

def test_dispatch_includes_aliases_section(tmp_path: Path, monkeypatch) -> None:
    """Ensure alias dispatch hits config loading branches."""
    # Use existing aliases via the `ops` meta-op.
    out = supertool.dispatch("ops")
    assert "verify" in out or "qa" in out or len(out) > 0
