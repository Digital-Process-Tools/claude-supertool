"""Dual `@-` stdin clash — clear error instead of an opaque parse failure.

sys.stdin is a single stream. Two `@-` (stdin) ops in one call both call
sys.stdin.read(); the first drains it, the second reads empty and dies with a
`@file ... parse error` that names neither the cause nor the fix. main() now
detects the clash in a pre-pass (sibling to the cwd: pre-pass) and errors with a
pointer to the escape hatches. Issue #341.
"""
from __future__ import annotations

import pytest

import supertool


@pytest.fixture
def stub_dispatch(monkeypatch):
    """Record dispatched ops; never touch real stdin or the filesystem."""
    seen: list[str] = []
    monkeypatch.setattr(supertool, "dispatch", lambda a: (seen.append(a), "")[-1])
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)
    return seen


def test_two_stdin_ops_error_before_dispatch(stub_dispatch, capsys) -> None:
    rc = supertool.main(["edit:@-", "edit:@-"])
    assert rc == 1
    assert stub_dispatch == []  # rejected before any op ran
    err = capsys.readouterr().err
    assert "only one '@-'" in err
    assert "got 2" in err
    assert "batch:@-" in err  # points at the fold-into-one escape hatch


def test_stdin_clash_lists_the_offending_ops(stub_dispatch, capsys) -> None:
    rc = supertool.main(["paste:@-", "vim:@-", "read:foo"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "paste:@-" in err and "vim:@-" in err
    assert "read:foo" not in err  # only stdin ops named


def test_single_stdin_op_passes(stub_dispatch) -> None:
    rc = supertool.main(["edit:@-"])
    assert rc == 0
    assert stub_dispatch == ["edit:@-"]  # guard did not fire


def test_one_stdin_plus_file_payloads_passes(stub_dispatch) -> None:
    rc = supertool.main(["edit:@-", "edit:@.max/e1.toml", "read:foo"])
    assert rc == 0
    assert stub_dispatch == ["edit:@-", "edit:@.max/e1.toml", "read:foo"]


def test_no_stdin_ops_passes(stub_dispatch) -> None:
    rc = supertool.main(["read:foo", "grep:bar:src/"])
    assert rc == 0
    assert stub_dispatch == ["read:foo", "grep:bar:src/"]


def test_bare_dash_arg_is_not_treated_as_stdin(stub_dispatch) -> None:
    """A literal '@-' with no op prefix isn't an `op:@-` stdin op — no false clash."""
    rc = supertool.main(["edit:@-", "@-"])
    assert rc == 0  # only one real stdin op
    assert stub_dispatch == ["edit:@-", "@-"]


def test_triple_colon_stdin_form_is_caught(stub_dispatch, capsys) -> None:
    """`op:::@-` reads stdin too (parts split on ':::') — must clash like `op:@-`."""
    rc = supertool.main(["edit:::@-", "edit:::@-"])
    assert rc == 1
    assert stub_dispatch == []
    assert "only one '@-'" in capsys.readouterr().err


def test_mixed_colon_forms_clash(stub_dispatch, capsys) -> None:
    """A single-colon and a triple-colon stdin op together are still two readers."""
    rc = supertool.main(["edit:@-", "paste:::@-"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "edit:@-" in err and "paste:::@-" in err
