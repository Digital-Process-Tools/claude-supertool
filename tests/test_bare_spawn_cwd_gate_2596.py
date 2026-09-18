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
3. A register, in the same spirit as `test_gate_matches_spawn_2579.py`, that
   NO file under `presets/`, `hooks/`, `notifiers/`, or `_supertool.py` calls
   `shutil.which()`/`which()` bare with a literal tool name any more -- with
   a positive control proving the walker can actually see the defect, so a
   green register is not a register that matches nothing.

   This used to be a hand-listed dict of exactly six files and ten tool
   names (`SITES`), which meant a seventh site -- `hooks/guard-selftest.py`
   (`shutil.which("bash")`, tracked separately as #2610), which sits in
   `hooks/`, a directory none of this repo's three spawn registers ever
   walked -- was structurally invisible to it (#2611). Walking the actual
   directories instead of naming files by hand found three more real,
   previously invisible instances in the same sweep, all in `_supertool.py`:
   `_has_rtk()` and `_has_ctags()` each cached a raw `which()` result and
   spawned it, and `_doctor_symlink()` did the same to compare its own
   reported version -- the exact #2596/#2575 shape, fixed here the same
   way `_which_excluding_cwd()` already exists in this same file to fix
   it. (An earlier draft of this fix excused `_doctor_symlink()`'s
   instance with a `DIAGNOSTIC_ONLY` exclusion and the claim it "never
   executes" the resolved path -- wrong, per self-review: `[which,
   "version"]` is spawned two lines later. Fixed rather than excused, and
   the exclusion mechanism removed along with it.) ALLOWLIST below is for
   the one remaining, separately-tracked instance only.
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

#: The directories #2579/#2540/#2605's own registers already cover
#: (`validators/`, `formatters/`) are deliberately NOT walked again here --
#: this register exists for everywhere ELSE a bare cwd-searching which()
#: can hide (#2611).
WALK_DIRS = ("presets", "hooks", "notifiers")

#: `presets/_spawnable.py` is this directory's own duplicated copy of the
#: chokepoint algorithm (see module docstring) -- implementation, not an
#: instance of the defect it implements the fix for.
CHOKEPOINT_FILES = {"_spawnable.py"}

#: `_supertool.py` sits at the repo root, outside every WALK_DIRS entry,
#: but is one of #2596's own six original call sites and cannot be reached
#: by any `rglob` rooted at WALK_DIRS -- named explicitly rather than
#: silently dropped when the walk replaced the hand-listed SITES dict.
EXTRA_FILES = ("_supertool.py",)

#: Known, real, unfixed instances of this exact class, tracked separately
#: rather than fixed in this change -- (relative path, tool name): reason.
#: An allowlist entry here means "this is real and tracked elsewhere", not
#: "this is a false positive" -- `hooks/guard-selftest.py`'s own
#: `bash_candidates()` genuinely resolves "bash" via a raw `shutil.which()`
#: and then spawns whatever it returns. Removing an entry is the signal
#: one has actually been fixed.
ALLOWLIST = {
    ("hooks/guard-selftest.py", "bash"): "#2610",
}

def _walked_sources() -> "list[pathlib.Path]":
    out = []
    for d in WALK_DIRS:
        out.extend(sorted((ROOT / d).rglob("*.py")))
    out = [p for p in out if p.name not in CHOKEPOINT_FILES]
    out.extend(ROOT / f for f in EXTRA_FILES)
    return out


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


def _offenders() -> "list[str]":
    hits = []
    for path in _walked_sources():
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in sorted(_bare_which_calls(tree)):
            if (rel, name) in ALLOWLIST:
                continue
            hits.append(f"{rel}: {name!r}")
    return hits


def test_no_bare_which_call_survives_outside_the_adapter_dirs() -> None:
    offenders = _offenders()
    assert not offenders, (
        "these files call shutil.which()/which() bare for a literal tool "
        "name, rather than the cwd-excluding chokepoint (#2596/#2611) -- "
        "either fix the site or, if it is a real, already-tracked instance "
        "out of scope for this change, add it to ALLOWLIST with the issue "
        "number:\n  " + "\n  ".join(offenders)
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


def test_the_allowlisted_instance_is_still_real() -> None:
    """The one remaining allowlist entry must still name a genuine, unfixed
    instance -- an allowlist that outlives the bug it excuses is the same
    silent absence this register exists to prevent (same convention as
    `tests/test_bare_argv0_construction_2605.py`)."""
    for rel, name in sorted(ALLOWLIST):
        path = ROOT / rel
        assert path.is_file(), f"{rel} no longer exists -- drop it from ALLOWLIST"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert name in _bare_which_calls(tree), (
            f"{rel} is allowlisted as a known unfixed bare which({name!r}) "
            "call, but the register no longer finds one -- it has "
            "apparently been fixed; remove it from ALLOWLIST"
        )


def test_the_register_covers_a_population_it_can_name() -> None:
    """A register over zero files is green and means nothing (same
    convention as #2579's/#2540's/#2605's own registers)."""
    sources = _walked_sources()
    assert len(sources) >= 100, (
        f"only {len(sources)} sources found under {WALK_DIRS} plus "
        f"{EXTRA_FILES} -- the walk root is wrong, and an empty walk reads "
        "exactly like a clean one"
    )
    assert any(_bare_which_calls(ast.parse(p.read_text(encoding="utf-8")))
               for p in sources), (
        "no file calls shutil.which()/which() at all, so this register is "
        "asserting nothing about anything"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
