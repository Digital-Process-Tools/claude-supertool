"""`_read_edit_hint`'s footer hardcoded `./supertool` (#2120).

Every read op's footer ended with a modify suggestion carrying a leading
`./`:

    up-arrow to modify: ./supertool 'edit:::OLD:::NEW:::README.md'  (or edit:@- ; ...)

`./supertool` is a gitignored symlink: present in a clone, absent in a
linked `git worktree` -- exactly where a dispatched agent normally stands,
since git does not carry a symlink into a worktree. The failure that
produces (`No such file or directory`, exit 127) says nothing about the
footer that caused it -- a reader concludes the tool is not installed, not
that the prefix was wrong.

Chosen fix: reuse the same three-state check `presets/_st_hint.py`'s
`st_hint` already applies to every OTHER printed remedy in this repo
(#905/#1012) -- `./supertool` only when a runnable wrapper (executable
bit, or a `#!` shebang on Windows) actually sits on disk beside this file;
`sys.executable supertool.py` when only the entry point is there (the
worktree case); and an explicit "no runnable supertool found" rather than
either literal, when neither is. Duplicated into `_supertool.py` as
`_modify_hint`/`_modify_hint_install_dir`/etc. rather than imported --
`presets/` ships no `__init__.py` and is subprocess-invoked, so core
duplicates small preset helpers instead of splicing `sys.path` into itself.
`_modify_hint_install_dir` is named and exposed exactly so a test can
monkeypatch it the way `presets/_st_hint.py`'s own `install_dir` already is
in `tests/test_st_hint_interpreter_1017.py`.
"""
from __future__ import annotations

from pathlib import Path

import supertool


def _worktree(monkeypatch, tmp_path: Path) -> Path:
    """An install with `supertool.py` and no `./supertool` wrapper -- the
    shape of a `git worktree` of this repo, where git never carries the
    gitignored symlink across."""
    (tmp_path / "supertool.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        supertool, "_modify_hint_install_dir", lambda: str(tmp_path))
    return tmp_path


def _clone(monkeypatch, tmp_path: Path) -> Path:
    """An install with a runnable `./supertool` wrapper beside `supertool.py`."""
    (tmp_path / "supertool.py").write_text("", encoding="utf-8")
    wrapper = tmp_path / "supertool"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setattr(
        supertool, "_modify_hint_install_dir", lambda: str(tmp_path))
    return tmp_path


def test_worktree_footer_does_not_print_a_bare_dot_slash(monkeypatch, tmp_path):
    """The exact reported defect: in a worktree (no wrapper on disk),
    the footer must not suggest `./supertool`, which fails there."""
    _worktree(monkeypatch, tmp_path)
    hint = supertool._read_edit_hint("README.md", "body")
    assert "./supertool " not in hint, hint


def test_worktree_footer_names_the_interpreter_and_entry_point(monkeypatch, tmp_path):
    """In a worktree, the footer must suggest the invocation that actually
    works there: the running interpreter plus `supertool.py`."""
    _worktree(monkeypatch, tmp_path)
    hint = supertool._read_edit_hint("README.md", "body")
    assert supertool.sys.executable in hint, hint
    assert "supertool.py 'edit:::OLD:::NEW:::README.md'" in hint, hint


def test_clone_footer_still_uses_the_dot_slash_wrapper(monkeypatch, tmp_path):
    """Where a runnable `./supertool` really is on disk (the main clone),
    the footer is unchanged from before this fix."""
    _clone(monkeypatch, tmp_path)
    hint = supertool._read_edit_hint("README.md", "body")
    assert "./supertool 'edit:::OLD:::NEW:::README.md'" in hint, hint


def test_no_runnable_supertool_found_says_so_rather_than_guessing(monkeypatch, tmp_path):
    """Neither a wrapper nor an entry point on disk: the footer must not
    print an invocation it cannot back up."""
    monkeypatch.setattr(
        supertool, "_modify_hint_install_dir", lambda: str(tmp_path))
    hint = supertool._read_edit_hint("README.md", "body")
    assert "./supertool " not in hint, hint
    assert "supertool.py 'edit" not in hint, hint
    assert "no runnable supertool found" in hint, hint
    assert "'edit:::OLD:::NEW:::README.md'" in hint, hint


def test_the_op_string_is_still_interpolated_verbatim(monkeypatch, tmp_path):
    """#1012's shape must not move underneath this fix -- the printed op
    is still the exact edit invocation for the read path, quoted."""
    _worktree(monkeypatch, tmp_path)
    hint = supertool._read_edit_hint("a b.py", "body")
    assert "'edit:::OLD:::NEW:::a b.py'" in hint, hint


def test_footer_still_carries_the_at_dash_alternative_and_the_harness_note(
        monkeypatch, tmp_path):
    """The rest of the footer -- the `edit:@-` alternative and the 'no
    harness Read needed' note -- must survive this fix untouched."""
    _worktree(monkeypatch, tmp_path)
    hint = supertool._read_edit_hint("README.md", "body")
    assert "(or edit:@- ; no harness Read needed)" in hint, hint
