"""The cwd-exclusion gate extended past validators/formatters (#2596).

#2579/#2581 closed the gate/spawn mismatch for `validators/`/`formatters/`
(register: `tests/test_gate_matches_spawn_2579.py`, `ADAPTER_DIRS =
("validators", "formatters")`). The same gate-then-bare-spawn shape was still
open in `_supertool.py` (`php`, `xmllint`) and in `presets/` (`glab`/`gh` in
two independent copies, `lsof` in two independent copies, `osascript` and
`ps`) -- none of those callers can import `validators/common/spawnable.py`
(a preset runs with `presets/` on `sys.path`, not the repo root; the core
cannot reach into `validators/common` either -- see
`_supertool._VALIDATOR_RESOLVE_ERROR_PREFIX`'s own comment), so the fix is
this repo's established duplicate-and-pin shape (`_LINE_BREAK_RE`,
`RESOLVE_ERROR_PREFIX`): a third stated copy of `which_excluding_cwd` in
`presets/_spawnable.py` and one in `_supertool.py`, each pinned equal to the
producer (`validators/common/spawnable.py`) by the tests below.

Three things this file checks:

1. The mechanism itself, for each of the two new duplicated copies -- the
   same positive control (a repo-planted shim wins under raw `shutil.which()`
   and is refused here) and negative control (a genuine PATH entry still
   resolves) `tests/test_which_excludes_cwd_2575.py` already runs against the
   producer.
2. That all three copies agree, across a small matrix of PATH shapes, so a
   spelling drift between them goes red here rather than silently reopening
   the class in whichever copy drifted.
3. A static register, in the same spirit as `test_gate_matches_spawn_2579.py`,
   that the six #2596 call sites no longer call `shutil.which()` bare for the
   tool names named in the issue -- with a positive control proving the
   walker can actually see the defect, so a green register is not a register
   that matches nothing.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "validators" / "common"))
from spawnable import which_excluding_cwd as producer_which  # noqa: E402

sys.path.insert(0, str(ROOT / "presets"))
from _spawnable import which_excluding_cwd as preset_which  # noqa: E402

sys.path.insert(0, str(ROOT))
import _supertool as supertool  # noqa: E402

core_which = supertool._which_excluding_cwd

# A name ending in a PATHEXT extension, not a bare name -- same reasoning as
# `tests/test_which_excludes_cwd_2575.py::TOOL`: CPython's real shutil.which()
# on Windows never tries the bare name once it decides the name carries no
# recognised extension, so a bare-named shim is invisible to the *real*
# shutil.which() the fixture's own self-check below uses to prove it planted
# a reachable shim, even though `which_excluding_cwd()` under test would find
# it fine either way.
TOOL = "st-probe-2596.cmd"

IMPLS = pytest.mark.parametrize("which_fn", [producer_which, preset_which, core_which],
                                ids=["validators/common", "presets", "_supertool"])


def _shim(d: "pathlib.Path", name: str = TOOL) -> "pathlib.Path":
    d.mkdir(parents=True, exist_ok=True)
    shim = d / name
    shim.write_text("", encoding="utf-8")
    shim.chmod(0o755)
    return shim


# ---------------------------------------------------------------------------
# 1) Mechanism, per duplicated copy
# ---------------------------------------------------------------------------

@IMPLS
def test_a_real_path_entry_still_resolves(which_fn, tmp_path, monkeypatch) -> None:
    """Negative control: the guard must not blind the ordinary case."""
    real = _shim(tmp_path / "realbin")
    monkeypatch.setenv("PATH", str(tmp_path / "realbin"))
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    assert which_fn(TOOL) == str(real)


@IMPLS
def test_a_match_that_only_exists_in_cwd_is_refused(which_fn, tmp_path, monkeypatch) -> None:
    """Positive control: the exploit shape -- only a cwd-planted shim
    matches, nothing legitimate is on PATH at all -- must be refused."""
    planted = _shim(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + "/nonexistent-bin")
    import shutil
    found = shutil.which(TOOL)
    assert found is not None and os.path.abspath(found) == str(planted), (
        "fixture does not reproduce cwd-first resolution -- shutil.which() "
        "did not find the planted shim, so the assertion below is not "
        "testing the #2596 shape at all"
    )
    assert which_fn(TOOL) is None


@IMPLS
def test_a_cwd_match_never_shadows_a_real_one_further_down_path(
        which_fn, tmp_path, monkeypatch) -> None:
    _shim(tmp_path)  # the attacker's plant, at cwd
    real = _shim(tmp_path / "realbin")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + str(tmp_path / "realbin"))
    assert which_fn(TOOL) == str(real)


# ---------------------------------------------------------------------------
# 2) The three copies agree
# ---------------------------------------------------------------------------

def test_the_three_duplicated_copies_agree(tmp_path, monkeypatch) -> None:
    """`validators/common/spawnable.py`, `presets/_spawnable.py` and
    `_supertool.py` each carry their own copy of this function (see module
    docstring for why none of the three can import another). A spelling
    drift between them is exactly the failure mode duplication risks, so
    this asks all three the same question and requires the same answer.
    """
    planted = _shim(tmp_path)
    real = _shim(tmp_path / "realbin")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + str(tmp_path / "realbin"))

    answers = {fn.__module__: fn(TOOL) for fn in (producer_which, preset_which, core_which)}
    assert len(set(answers.values())) == 1, answers
    assert list(answers.values())[0] == str(real)

    # And on a machine with no relevant PATH entry at all, all three refuse
    # identically rather than one falling back to the cwd-only match.
    monkeypatch.setenv("PATH", str(tmp_path))
    answers_absent = {fn.__module__: fn(TOOL) for fn in (producer_which, preset_which, core_which)}
    assert set(answers_absent.values()) == {None}, answers_absent
    del planted  # referenced only to keep the shim alive for the PATH walk


# ---------------------------------------------------------------------------
# 3) Static register over the six #2596 call sites
# ---------------------------------------------------------------------------

#: (file, tool names that must never be gated by a bare `shutil.which()` call
#: in this file any more).
SITES = {
    ROOT / "_supertool.py": {"php", "xmllint"},
    ROOT / "presets" / "git" / "_git_common.py": {"glab", "gh"},
    ROOT / "presets" / "git" / "conflicts.py": {"glab", "gh"},
    ROOT / "presets" / "_git_run.py": {"lsof"},
    ROOT / "presets" / "git" / "worktrees.py": {"lsof"},
    ROOT / "presets" / "watch" / "transport.py": {"osascript", "ps"},
}


def _bare_which_calls(tree: ast.AST) -> set:
    """Every literal string argument passed to a bare `shutil.which(...)` or
    `which(...)` call in `tree`."""
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if called != "which":
            continue
        if not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            names.add(arg.value)
    return names


@pytest.mark.parametrize("path,tools", list(SITES.items()),
                         ids=[p.relative_to(ROOT).as_posix() for p in SITES])
def test_no_named_tool_is_still_gated_by_a_bare_which_call(path, tools) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = _bare_which_calls(tree) & tools
    assert not offenders, (
        f"{path.relative_to(ROOT).as_posix()} still gates {offenders} with a "
        "bare shutil.which()/which() call rather than the cwd-excluding "
        "chokepoint (#2596)"
    )


def test_the_walker_can_actually_see_the_defect() -> None:
    """Positive control, same shape as #2579's own register: without this,
    the assertion above also passes when the walker is broken or blind."""
    bad = (
        "import shutil, subprocess\n"
        "if not shutil.which('lsof'):\n"
        "    return\n"
        "subprocess.run(['lsof'])\n"
    )
    tree = ast.parse(bad)
    assert _bare_which_calls(tree) == {"lsof"}, "the bare-which walker is blind"


def test_the_register_covers_every_named_site_and_tool() -> None:
    """A register naming zero files, or a file naming zero tools, is green
    and means nothing."""
    assert len(SITES) == 6, SITES
    assert sum(len(v) for v in SITES.values()) == 10, SITES
    for path in SITES:
        assert path.is_file(), f"{path} does not exist -- the register is stale"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
