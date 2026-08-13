"""A preserved global that is built at first use is not preserved state (#1322).

`conftest.RESET_EXEMPT_GLOBALS` is a list of claims: each name on it is asserted
to hold a value whose lifetime is the *process*, not the test. Two entries broke
that claim. `_REPO_TARGET_MODES` and `_SHIPPED_PRESET_OPS` are `None` sentinels
populated at first call from `_INSTALL_DIR` -- and `_INSTALL_DIR` is patchable
and is patched (`tests/test_presets.py:38,57`).

So a cache computed while the install dir pointed at a `tmp_path` is empty, and
being exempt from the reset it stays empty for the rest of that xdist worker.
`_repo_target_ops()` then returns nothing, every `repo:` call refuses every
`gh-*` op, and the three `test_repo_target_673.py` tests fail with `assert 1 ==
0` -- for a reason produced by an unrelated test that ran earlier on the same
worker.

Reproduced on `275069e`, serially, by adding one test that patches
`_INSTALL_DIR` and calls `_repo_target_modes()` and letting the repo-target file
run after it: `6 failed, 26 passed`. Removing that one test: `32 passed`. The
reported flake itself does **not** reproduce on this base -- a full run under
`-n 8` with a teardown probe on the cache saw 12246 pass and **zero** poisoned
teardowns, so no test in the suite currently populates it under a patch. The
mechanism is armed and unloaded; the next test to patch `_INSTALL_DIR` near a
dispatch loads it.

"Same lifetime as X" is a claim about **when a value is built**, and both of
these are built at first use rather than at import. That is what the guard below
now enforces, so the classification cannot go stale again silently.
"""
from __future__ import annotations

import ast
from pathlib import Path

import conftest
import pytest

import supertool

CORE = Path(supertool.__file__)

#: `_CONFIG` is also a `None`-sentinel, and is also exempt -- legitimately. The
#: autouse fixture in conftest saves and restores it **by name** on every test
#: (`old_config = supertool._CONFIG` ... `supertool._CONFIG = {}`), so it already
#: has a per-test lifetime by a different route. An entry here is a claim that
#: something else handles the name; it is not a way to quiet this test.
HANDLED_ELSEWHERE = {"_CONFIG"}


def _declared_none_sentinels() -> set[str]:
    """Module-level names in the core assigned a literal `None` at import.

    Read from the **source**, not from the live module. A first draft of this
    used `getattr(supertool, name) is None` and passed while both offenders were
    still exempt: by the time it ran, an earlier test had built the caches, so
    the check asked "is it unbuilt right now" when the question is "is `None`
    its declared initial state". A guard that answers a different question than
    the one it is named for is the defect this file is about, one level up.
    """
    tree = ast.parse(CORE.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        else:
            continue
        if not (isinstance(node.value, ast.Constant) and node.value.value is None):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _none_sentinel_exempt_globals() -> list[str]:
    """Exempt names declared `None` at import -- i.e. built at first use."""
    declared = _declared_none_sentinels()
    return sorted(
        name
        for name in conftest.RESET_EXEMPT_GLOBALS
        if name not in HANDLED_ELSEWHERE and name in declared
    )


def test_the_sentinel_scan_finds_the_names_it_is_built_on() -> None:
    """The scan itself must not be able to return an empty set silently.

    An AST walk that stopped matching -- a new assignment form, a move into a
    class body -- would report zero offenders forever, which reads exactly like
    a clean build. Pin the three names known to have this shape.
    """
    declared = _declared_none_sentinels()
    for name in ("_REPO_TARGET_MODES", "_SHIPPED_PRESET_OPS", "_CONFIG"):
        assert name in declared, (
            f"{name} is declared `= None` in {CORE.name} but the scan missed "
            "it -- the guard below is measuring nothing"
        )


def test_a_cache_poisoned_under_a_patched_install_dir_does_not_outlive_the_test(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The bar: this must fail if the reset leaves the empty cache in place.

    It drives `_reset_module_state` directly rather than relying on test order,
    because ordering is exactly what makes the real defect a flake -- an
    order-dependent assertion would reproduce the bug's own unreliability.
    """
    monkeypatch.setattr(supertool, "_INSTALL_DIR", str(tmp_path))
    assert supertool._repo_target_modes() == {}, (
        "precondition: an install dir with no presets/ must build an empty map"
    )
    monkeypatch.undo()

    conftest._reset_module_state()

    assert supertool._repo_target_ops(), (
        "the empty map survived the per-test reset: every `repo:` call in this "
        "worker will now refuse every op it is given (#1322)"
    )


def test_the_shipped_preset_index_is_reset_the_same_way(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Same shape, same input, same list -- found by sweeping for the class.

    `_SHIPPED_PRESET_OPS` backs the unknown-op message and `ops`, so a poisoned
    copy reports that no shipped preset declares the op the caller just typed.
    That is this repo's defect class in its purest form: an absence produced by
    a stale cache, rendered as an absence in the install.
    """
    monkeypatch.setattr(supertool, "_INSTALL_DIR", str(tmp_path))
    assert supertool._shipped_preset_ops() == {}, "precondition"
    monkeypatch.undo()

    conftest._reset_module_state()

    assert supertool._shipped_preset_ops(), (
        "the empty index survived the per-test reset (#1322)"
    )


def test_no_exempt_global_is_a_lazily_built_cache() -> None:
    """The enforcement, not the repair.

    Deleting two names fixes today. This fails the build the next time somebody
    adds a `None`-sentinel lazy cache to the exempt list on the reasoning that it
    "has the same lifetime as" a neighbour that is genuinely written at import.
    """
    offenders = _none_sentinel_exempt_globals()
    assert offenders == [], (
        "these names are exempted from the per-test reset as process-lifetime "
        "state, but they are `None` at import and built at first use -- from "
        "inputs a test can patch. Put them in RESET_GLOBALS, or add them to "
        f"HANDLED_ELSEWHERE with the mechanism that handles them: {offenders}"
    )


def test_the_reset_can_actually_restore_a_none_sentinel() -> None:
    """`_reset_module_state` used to mutate in place -- `current.clear()` for a
    dict, `current[:] = pristine` otherwise. Neither reaches a name whose
    pristine value is `None`: the fallback would raise `TypeError: 'NoneType'
    object does not support item assignment`. So the two names above could not
    simply be moved; the reset had to learn to rebind.
    """
    for name in ("_REPO_TARGET_MODES", "_SHIPPED_PRESET_OPS"):
        assert name in conftest.RESET_GLOBALS, f"{name} is not reset per test"
        assert conftest._PRISTINE_GLOBALS[name] is None, (
            f"{name} was captured as {conftest._PRISTINE_GLOBALS[name]!r} rather "
            "than the unbuilt sentinel -- something populated it before conftest "
            "took the snapshot, and every reset now restores that snapshot"
        )
        setattr(supertool, name, {"poisoned": "x"})
    conftest._reset_module_state()
    assert supertool._REPO_TARGET_MODES is None
    assert supertool._SHIPPED_PRESET_OPS is None
