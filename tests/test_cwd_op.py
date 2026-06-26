"""cwd: op — set the working dir for a whole call.

Cross-repo sessions otherwise need a `cd <repo> && ./supertool …` prefix on
every call (shell cwd resets between Bash invocations). A leading `cd` trips the
use-supertool hook and risks cwd-poisoning (relative greps/diffs resolving
against the wrong repo).

`cwd:PATH` mirrors `cd PATH && …`: it MUST be the first op, is consumed in a
pre-pass before dispatch (chdir once, then stripped), so the remaining ops all
resolve against PATH and keep their parallel fast-path. Required-first keeps the
mental model unambiguous — every op after it runs in the new dir.
"""
from __future__ import annotations

import os

import pytest

import supertool


@pytest.fixture
def restore_cwd():
    """cwd: mutates process cwd; snapshot + restore so it doesn't bleed."""
    saved = os.getcwd()
    yield
    os.chdir(saved)


def test_cwd_op_chdirs_before_dispatch_and_is_stripped(tmp_path, monkeypatch, restore_cwd) -> None:
    seen_ops: list[str] = []
    seen_cwd: list[str] = []
    monkeypatch.setattr(
        supertool, "dispatch",
        lambda a: (seen_ops.append(a), seen_cwd.append(os.getcwd()), "")[-1],
    )
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)

    rc = supertool.main([f"cwd:{tmp_path}", "read:foo"])

    assert rc == 0
    assert seen_ops == ["read:foo"]                                          # cwd op stripped
    assert os.path.realpath(seen_cwd[0]) == os.path.realpath(str(tmp_path))  # chdir'd before op ran


def test_cwd_op_expands_tilde(monkeypatch, restore_cwd) -> None:
    seen_cwd: list[str] = []
    monkeypatch.setattr(
        supertool, "dispatch",
        lambda a: (seen_cwd.append(os.getcwd()), "")[-1],
    )
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)

    rc = supertool.main(["cwd:~", "read:foo"])

    assert rc == 0
    assert os.path.realpath(seen_cwd[0]) == os.path.realpath(os.path.expanduser("~"))


def test_cwd_op_missing_dir_errors_without_chdir(tmp_path, monkeypatch, restore_cwd) -> None:
    before = os.getcwd()
    called: list[str] = []
    monkeypatch.setattr(supertool, "dispatch", lambda a: called.append(a) or "")
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)

    rc = supertool.main([f"cwd:{tmp_path}/does-not-exist", "read:foo"])

    assert rc == 1
    assert called == []                  # bailed before dispatch
    assert os.getcwd() == before         # cwd untouched


def test_cwd_op_must_be_first(tmp_path, monkeypatch, restore_cwd) -> None:
    before = os.getcwd()
    called: list[str] = []
    monkeypatch.setattr(supertool, "dispatch", lambda a: called.append(a) or "")
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)

    rc = supertool.main(["read:foo", f"cwd:{tmp_path}"])

    assert rc == 1
    assert called == []                  # rejected before any dispatch
    assert os.getcwd() == before         # cwd untouched


def test_no_cwd_op_leaves_cwd_untouched(monkeypatch, restore_cwd) -> None:
    before = os.getcwd()
    monkeypatch.setattr(supertool, "dispatch", lambda a: "")
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)

    supertool.main(["read:foo"])

    assert os.getcwd() == before


def test_cwd_op_expands_env_var(monkeypatch, restore_cwd) -> None:
    seen_cwd: list[str] = []
    monkeypatch.setattr(
        supertool, "dispatch",
        lambda a: (seen_cwd.append(os.getcwd()), "")[-1],
    )
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)

    rc = supertool.main(["cwd:$HOME", "read:foo"])

    assert rc == 0
    assert os.path.realpath(seen_cwd[0]) == os.path.realpath(os.path.expanduser("~"))


def test_cwd_op_empty_path_errors(monkeypatch, restore_cwd) -> None:
    before = os.getcwd()
    called: list[str] = []
    monkeypatch.setattr(supertool, "dispatch", lambda a: called.append(a) or "")
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)

    rc = supertool.main(["cwd:", "read:foo"])

    assert rc == 1
    assert called == []                  # bailed before dispatch
    assert os.getcwd() == before


def test_cwd_op_rejects_multiple(tmp_path, monkeypatch, restore_cwd) -> None:
    before = os.getcwd()
    called: list[str] = []
    monkeypatch.setattr(supertool, "dispatch", lambda a: called.append(a) or "")
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)

    rc = supertool.main([f"cwd:{tmp_path}", "read:foo", f"cwd:{tmp_path}"])

    assert rc == 1
    assert called == []                  # rejected before any dispatch
    assert os.getcwd() == before
