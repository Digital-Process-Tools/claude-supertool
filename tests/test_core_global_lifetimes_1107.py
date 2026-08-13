"""The core lifetime guard checked *mutability*; the property is *carried state* (#1107).

`test_state_reset_and_lint_timeout.py` enumerates module-level names bound to a
`dict`, `list` or `set`. That is a proxy for "this global carries state between
calls", and the two sets are not the same one:

  - a `str`/`bool`/`None`-sentinel memo carries state across calls and is
    invisible to a container check. `_VALIDATOR_MEANING_VERSION` is the case
    #1107 was filed from;
  - a constant lookup table is a container and carries nothing, which is why the
    container check needs `RESET_EXEMPT_GLOBALS` to be 30 names long.

**The property this file checks instead: the module rebinds the name while a
call is running.** Python spells that `global NAME` inside a function -- there
is no other way to rebind a module-level name from a call -- so it is decidable
from the source, needs no type judgement, and cannot be reached by a constant, a
compiled regex, a type alias or an imported symbol. 27 names in the core answer
yes; the container check saw 8 of them.

That is deliberately *not* "every module-level name must be classified", which
#1107 rules out for the reason #686 gives: a guard that is mostly noise gets
exempted without reading, and stops being a guard.

**Neither check subsumes the other, so both stay.** `_WRITE_WARNINGS.append(...)`
mutates in place and needs no `global`; `_VALIDATOR_MEANING_VERSION = h(...)`
rebinds and is no container. The sibling was renamed to say which half it holds.

A name found here must be in one of three tables, and the third is new:

  1. `conftest.RESET_GLOBALS` -- restored between tests by `_reset_module_state`.
  2. `conftest.RESET_EXEMPT_GLOBALS` -- argued to have process lifetime.
  3. `conftest.FIXTURE_RESTORED_GLOBALS` -- saved and restored by name in the
     autouse fixture, which is how the RTK/tree-sitter/ctags probes were already
     held. That table existed as ten hand-written lines and no declaration, so
     nothing could tell a name it had stopped restoring from a name it never
     covered. The claim is **verified** here against the fixture's own source,
     #686-style: a declaration nobody checks is a comment.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import conftest
import supertool

CORE = Path(supertool.__file__)
CONFTEST = Path(conftest.__file__)

#: The autouse fixture whose save/restore lines FIXTURE_RESTORED_GLOBALS claims.
_FIXTURE = "_disable_rtk_and_config"


def _module_level_bindings(tree: ast.Module) -> dict[str, int]:
    """Names assigned at module scope -> line number. Any value, any type."""
    bound: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bound.setdefault(target.id, node.lineno)
    return bound


def runtime_rebound_globals(source: str) -> dict[str, int]:
    """Module-level names some function declares `global` -> where it is bound.

    A `global` inside a function is the only way to rebind a module-level name
    from a call, so this is the exact set of names whose value can differ
    between two calls in one process. Names declared `global` but never bound at
    module scope are ignored: they are created by the first call, so there is no
    import-time value to restore and no cross-test carry a snapshot could hold.
    """
    tree = ast.parse(source)
    bound = _module_level_bindings(tree)
    rebound: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Global):
                for name in sub.names:
                    if name in bound:
                        rebound[name] = bound[name]
    return rebound


def fixture_restored_names(source: str, fixture: str) -> set[str]:
    """`supertool.X = ...` assignments AFTER the fixture's `yield`.

    The restore is the load-bearing half of the claim, so it is the half that is
    checked. An override before the yield with no assignment after it is a test
    fixture that changes the process and does not change it back -- which is
    what this table exists to make impossible to write silently.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != fixture:
            continue
        after_yield = False
        restored: set[str] = set()
        for stmt in node.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Yield):
                after_yield = True
                continue
            if not after_yield:
                continue
            for sub in ast.walk(stmt):
                if not isinstance(sub, ast.Assign):
                    continue
                for target in sub.targets:
                    if (isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "supertool"):
                        restored.add(target.attr)
        return restored
    return set()


# ---------------------------------------------------------------------------
# The scans must not be able to answer "nothing" quietly.
# ---------------------------------------------------------------------------

def test_the_rebind_scan_finds_the_names_it_is_built_on() -> None:
    """A scan that stopped matching reports zero offenders, which reads clean."""
    live = runtime_rebound_globals(CORE.read_text(encoding="utf-8"))
    for name in ("_VALIDATOR_MEANING_VERSION", "_RTK_PATH", "_FORMAT_QUEUE"):
        assert name in live, (
            f"{name} is rebound under a `global` in {CORE.name} but the scan "
            "missed it -- the guard below is measuring nothing"
        )


def test_the_fixture_scan_finds_the_names_it_is_built_on() -> None:
    restored = fixture_restored_names(CONFTEST.read_text(encoding="utf-8"), _FIXTURE)
    assert {"_RTK_CHECKED", "_TS_PACKAGE"} <= restored, (
        f"the scan of conftest::{_FIXTURE} found {sorted(restored)} -- if the "
        "fixture was renamed or its restore block moved, every claim in "
        "FIXTURE_RESTORED_GLOBALS is being verified against an empty set"
    )


# ---------------------------------------------------------------------------
# The guard.
# ---------------------------------------------------------------------------

def test_every_runtime_rebound_global_has_a_declared_lifetime() -> None:
    live = runtime_rebound_globals(CORE.read_text(encoding="utf-8"))
    accounted = (set(conftest.RESET_GLOBALS)
                 | set(conftest.RESET_EXEMPT_GLOBALS)
                 | set(conftest.FIXTURE_RESTORED_GLOBALS))
    missing = sorted(set(live) - accounted)
    assert missing == [], (
        "module-level name(s) in the core that a call rebinds, with no declared "
        "lifetime (#1107): "
        + ", ".join(f"{name} (L{live[name]})" for name in missing)
        + ". Each one carries a value from one call into the next, which in a "
          "test run means from one test into the next. Pick one: "
          "conftest.RESET_GLOBALS if it is scratch or a cache built from "
          "anything a test can patch; conftest.FIXTURE_RESTORED_GLOBALS if the "
          "autouse fixture saves and restores it by name; or "
          "conftest.RESET_EXEMPT_GLOBALS with the argument for why a value "
          "outliving a test is correct."
    )


def test_the_fixture_restored_claims_are_true() -> None:
    """#686's rule, applied here: the declaration is checked, not trusted."""
    restored = fixture_restored_names(CONFTEST.read_text(encoding="utf-8"), _FIXTURE)
    unkept = sorted(set(conftest.FIXTURE_RESTORED_GLOBALS) - restored)
    assert unkept == [], (
        f"declared in conftest.FIXTURE_RESTORED_GLOBALS, but {_FIXTURE} does "
        f"not assign them after its `yield`: {unkept}. Restore them there, or "
        "move them to RESET_GLOBALS -- the declaration currently says something "
        "untrue about their lifetime."
    )


def test_no_declared_lifetime_names_something_that_is_no_longer_state() -> None:
    """The other direction. A registry that outlives its subject rots quietly."""
    live = runtime_rebound_globals(CORE.read_text(encoding="utf-8"))
    stale = sorted(name for name in conftest.FIXTURE_RESTORED_GLOBALS
                   if name not in live)
    assert stale == [], (
        "conftest.FIXTURE_RESTORED_GLOBALS names globals nothing rebinds at run "
        f"time any more, so the fixture is restoring constants: {stale}"
    )


# ---------------------------------------------------------------------------
# Watching it catch the case it was filed for.
# ---------------------------------------------------------------------------

def test_a_scalar_memo_does_not_outlive_the_test(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The bar: this fails if the widened guard and the reset do nothing.

    `_validator_meaning_version()` hashes `validators/SCHEMA.md` under
    `_INSTALL_DIR`, and `_INSTALL_DIR` is patched by tests
    (`tests/test_presets.py:38,57`). A memo computed under a patched install dir
    is the hash of `schema-unreadable`, and before #1107 nothing in the reset
    could reach a `str` -- so every later validator cache entry in that xdist
    worker was written and read under another test's key space.
    """
    monkeypatch.setattr(supertool, "_INSTALL_DIR", str(tmp_path))
    # Written through the module global, not through monkeypatch: the poison has
    # to be the real one. `monkeypatch.setattr` would undo it at teardown and
    # this test would then pass with the reset doing nothing at all.
    try:
        supertool._VALIDATOR_MEANING_VERSION = None
        poisoned = supertool._validator_meaning_version()
        monkeypatch.undo()
        assert supertool._VALIDATOR_MEANING_VERSION == poisoned, (
            "precondition: the memo is supposed to be held across calls"
        )

        conftest._reset_module_state()

        assert supertool._VALIDATOR_MEANING_VERSION is None, (
            "the memo survived the per-test reset -- the next test in this "
            "worker keys its validator cache on an install dir that no longer "
            "exists"
        )
        assert supertool._validator_meaning_version() != poisoned, (
            "recomputed under the real install dir and got the same value as "
            "under an empty tmp_path: this test is not testing what it says"
        )
    finally:
        supertool._VALIDATOR_MEANING_VERSION = None


def test_the_reset_can_restore_a_non_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_reset_module_state` restored containers in place and `None` by rebind.

    A `str`, a `bool` or a tuple hit neither arm: `current[:] = pristine` raises
    `TypeError` on a bool and rebinds nothing useful on a str. Scalars could not
    simply be added to RESET_GLOBALS; the reset had to learn to rebind anything
    that is not a live container.
    """
    for name, poison in (("_VALIDATOR_MEANING_VERSION", "deadbeef"),
                         ("_DEFER_FORMATTERS", True),
                         ("_GREP_EXTENSIONS_EFFECTIVE", ("*.py",))):
        assert name in conftest.RESET_GLOBALS, f"{name} is not reset per test"
        monkeypatch.setattr(supertool, name, poison)

    conftest._reset_module_state()

    assert supertool._VALIDATOR_MEANING_VERSION is None
    assert supertool._DEFER_FORMATTERS is False
    assert supertool._GREP_EXTENSIONS_EFFECTIVE is None
