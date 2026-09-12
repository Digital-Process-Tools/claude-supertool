"""batch:@file accepts a single op's own fields as a one-op batch (#1026 item 3).

A `{"op": "X", ...}` document handed to batch:@file is unambiguous — it is
exactly one op's fields, not a batch wrapper with a typo — so it should run
as a one-op batch rather than being refused with "no 'ops' array". The
sibling refusal for a genuinely malformed wrapper (no 'op' key at all, e.g.
a flat old/new/path document, or an unknown top-level wrapper key) must
still fire.
"""
from __future__ import annotations

import json
from pathlib import Path

import supertool


def _write(tmp_path: Path, payload) -> Path:
    f = tmp_path / "p.json"
    f.write_text(json.dumps(payload))
    return f


def test_single_op_dict_with_op_key_runs_as_a_one_op_batch(tmp_path: Path) -> None:
    """Must fire: a bare {"op": ...} document is accepted, not refused."""
    f = _write(tmp_path, {"op": "git-status"})
    out = supertool.dispatch(f"batch:@{f}")
    assert "ERROR: this payload has no 'ops' array" not in out


def test_stdin_single_op_document_runs_via_batch(monkeypatch, capsys) -> None:
    """The exact repro from the issue: echo '{"op":"git-status"}' | ... batch:@-"""
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO('{"op":"git-status"}'))
    out = supertool.dispatch("batch:@-")
    assert "ERROR: this payload has no 'ops' array" not in out


def test_flat_document_with_no_op_key_still_refuses(tmp_path: Path) -> None:
    """Must not fire: a flat old/new/path document (no 'op' key) is still a
    malformed wrapper, not a single op — the original refusal must survive."""
    f = _write(tmp_path, {"old": "a", "new": "b", "path": str(tmp_path / "x.py")})
    out = supertool.dispatch(f"batch:@{f}")
    assert "ERROR" in out
    assert "ops" in out.lower()


def test_unknown_wrapper_key_still_refuses_even_near_op_shaped_payloads(tmp_path: Path) -> None:
    """Must not fire: an 'ops' wrapper with a misspelt continue_on_error is
    still refused — this path is unrelated to the single-op acceptance and
    must not be swallowed by it."""
    f = _write(tmp_path, {"continue_on_eror": False, "ops": [{"op": "git-status"}]})
    out = supertool.dispatch(f"batch:@{f}")
    assert "ERROR: unknown key" in out
