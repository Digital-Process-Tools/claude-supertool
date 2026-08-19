"""#1812 -- a preset-derived op is invisible to every test, and says nothing.

`tests/conftest.py`'s autouse `_disable_rtk_and_config` sets
`supertool._CONFIG = {}` for every test in the suite. That reset is deliberate
and load-bearing -- `_load_config()` walks up from the cwd, so without it a
test's behaviour would depend on which checkout it ran from -- but its cost is
that every op whose route comes from a preset manifest rather than the builtin
table does not exist in any test, in CI included. The condition is never false,
so a test that arms a `pytest.skip` on it reports a skip forever and asserts
nothing, which is how #1776 shipped a `git-commit` guard assertion that never
ran once.

This file pins the opt-in that replaces those hand-rolled arms, and pins it in
both directions: that the route really appears when a test asks for it, and
that a request which cannot be honoured is a **failure** rather than a skip.
Every "must not happen" case here has a "must happen" partner in the same
fixture, because an assertion about an absence passes when nothing at all
works.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import _supertool
import supertool

from conftest import (
    PRESET_OP_ROUTE_LOST,
    PRESET_OP_ROUTE_NONE,
    PRESET_OP_ROUTE_OK,
    preset_op_route_state,
)


REPO_ROOT = Path(__file__).parent.parent

#: Read off the shipped manifest rather than typed, so a `syntax` rewording
#: that stops yielding clean identifiers -- the documented way to silently
#: delete an op's payload route -- reddens this file instead of skipping it.
GIT_COMMIT_ENTRY = json.loads(
    (REPO_ROOT / "presets" / "git.json").read_text(encoding="utf-8")
)["ops"]["git-commit"]


# --- the trap itself, so the rest of the file is not asserting into a void ---

def test_a_preset_op_has_no_route_until_a_test_asks_for_one():
    """The positive control for every "the fixture installed it" claim below.

    Without this, `test_with_preset_op_installs_a_working_payload_route` would
    pass just as happily if `with_preset_op` did nothing and `git-commit` had
    been reachable all along.
    """
    assert supertool._CONFIG == {}, (
        "the autouse config reset did not hold -- some earlier test in this "
        "worker leaked config, and every claim in this file is now untrustworthy"
    )
    assert supertool._at_file_fields("git-commit") == []
    assert "git-commit" not in (supertool._load_config().get("ops") or {})


# --- the opt-in works ------------------------------------------------------

def test_with_preset_op_installs_a_working_payload_route(with_preset_op):
    entries = with_preset_op("git-commit", payload_route=True)

    # The route is really there -- asserted, not assumed, or this test degrades
    # into the silent pass it exists to replace.
    assert supertool._at_file_fields("git-commit") == ["message", "paths"]
    # And it is dispatchable, which is a different question from having a
    # payload route: one reads `_CONFIG["ops"]`, the other the @file registry.
    assert (supertool._load_config().get("ops") or {})["git-commit"]["cmd"]
    assert entries["git-commit"]["syntax"] == GIT_COMMIT_ENTRY["syntax"]


def test_the_installed_entry_is_the_shipped_one_resolved(with_preset_op):
    """Joined to the artifact, not to a copy of it typed into a test.

    Not byte-identical to `presets/git.json`, and that is the point: the entry
    comes through `_merge_presets`, which resolves `{path}` to the preset's
    directory. The raw manifest carries `{python} {path}git/commit.py {args}`
    and a test handed that unresolved would install a `cmd` the dispatcher
    cannot spawn -- declared, and not dispatchable. So: every key equal to the
    manifest's, and `cmd` equal to the manifest's with `{path}` resolved.
    """
    entries = with_preset_op("git-commit")
    installed = entries["git-commit"]
    for key, value in GIT_COMMIT_ENTRY.items():
        if key == "cmd":
            continue
        assert installed[key] == value, key
    assert installed["cmd"] != GIT_COMMIT_ENTRY["cmd"]
    # Forward slashes on every platform, not `os.sep`: `_resolve_preset_cmd`
    # normalises them deliberately, because the cmd template flows through
    # `shlex.split(posix=True)`, which would eat a Windows backslash as an
    # escape. Building the expectation with `os.sep` would pass on POSIX and
    # fail on Windows for a reason that is nothing to do with this fixture.
    expected_prefix = str(REPO_ROOT / "presets").replace(os.sep, "/").rstrip("/") + "/"
    assert installed["cmd"] == GIT_COMMIT_ENTRY["cmd"].replace(
        "{path}", expected_prefix)
    assert "{path}" not in installed["cmd"]


def test_the_route_reaches_the_guard_that_needed_it(with_preset_op):
    """The #1776 assertion, which skipped on every run before this existed.

    The guard is called directly rather than through `dispatch`: for
    `git-commit` the dispatch form runs a real commit, so a regressed guard
    would commit the working tree the suite runs in.
    """
    with_preset_op("git-commit", payload_route=True)
    out = _supertool._stdin_ref_in_value_field(
        "git-commit", ["git-commit", "a message", "@-"])
    assert "ERROR" in out, out
    assert "the paths field is" in out, out
    # The must-fire partner's silent half: the guard stays quiet where the
    # payload reference is the op's own, intercepted upstream.
    assert _supertool._stdin_ref_in_value_field("git-commit", ["git-commit"]) == ""


def test_two_ops_at_once_and_neither_displaces_the_other(with_preset_op):
    entries = with_preset_op("git-commit", "git-push")
    assert set(entries) == {"git-commit", "git-push"}
    ops = supertool._load_config().get("ops") or {}
    assert "git-commit" in ops and "git-push" in ops


def test_the_fixture_installs_only_what_was_asked_for(with_preset_op):
    """A test asking for one op must not silently acquire the whole registry --
    that would make every other op's presence an accident of this fixture."""
    with_preset_op("git-commit")
    assert set((supertool._load_config().get("ops") or {})) == {"git-commit"}


# --- and it is loud when it cannot ------------------------------------------

def refusal(fn, *args, **kwargs) -> AssertionError:
    """Return the `AssertionError` *fn* refused with, and fail on anything else.

    `pytest.raises(AssertionError)` is the wrong tool for every assertion in
    this section, and wrong in the direction this whole issue is about.
    `pytest.skip` raises `Skipped`, which inherits from `BaseException` and not
    from `AssertionError` -- so `pytest.raises` would let it through, the test
    would be reported as SKIPPED, and the mutation that turns this fixture's
    refusals back into skips would produce a green suite with one more skip in
    it. Which is the defect, inside its own test.
    """
    # The parameter is `fn` and not `call`: `tests/test_encoding_seam.py`
    # scans every test file for `call(...)` by name, cannot tell this one from
    # `subprocess.call`, and declines rather than passing it -- which is the
    # right call for that scanner and a red here for a name.
    try:
        fn(*args, **kwargs)
    except AssertionError as exc:
        return exc
    except BaseException as exc:  # noqa: BLE001 -- Skipped lives out here
        pytest.fail(
            f"refused with {type(exc).__name__}, not AssertionError: {exc}. "
            f"A skip is not a refusal (#1812)."
        )
    pytest.fail("did not refuse at all")


def test_an_undeclared_op_fails_and_does_not_skip(with_preset_op):
    """The judgment call #1812 hands the implementer, pinned as a behaviour.

    A skip here would reproduce the defect one layer up: the suite would report
    a skip that reads as an environment quirk for a request that can never be
    honoured in any environment.
    """
    exc = refusal(with_preset_op, "no-preset-declares-this-op-1812")
    assert "no-preset-declares-this-op-1812" in str(exc)


def test_the_refusal_helper_fails_on_a_skip():
    """The must-fire partner for `refusal` itself. Without this, `refusal`
    could be letting `Skipped` straight through and every test above would go
    on passing while asserting nothing.

    `pytest.fail.Exception` rather than `refusal` recursively: `pytest.fail`
    raises `Failed`, which is not an `AssertionError` either, so `refusal`
    cannot be its own harness.
    """
    with pytest.raises(pytest.fail.Exception) as skipped:
        refusal(pytest.skip, "pretending to be an environment")
    assert "Skipped" in str(skipped.value), skipped.value
    assert "A skip is not a refusal" in str(skipped.value), skipped.value

    with pytest.raises(pytest.fail.Exception) as silent:
        refusal(lambda: None)
    assert "did not refuse at all" in str(silent.value), silent.value

    # And it returns, rather than failing, on the case it is for.
    def _refuses():
        raise AssertionError("the real refusal")
    assert str(refusal(_refuses)) == "the real refusal"


def test_a_declared_route_that_derives_no_fields_is_lost_not_absent():
    """The three states, on the classifier the fixture refuses with.

    `:::` in the syntax head means a payload route was intended. Fields that
    do not derive from it mean the route was deleted by a rewording -- which
    must read as a loss, never as "this op simply has no payload route".
    """
    lost, _ = preset_op_route_state(
        "made-up-op-1812", {"syntax": "made-up-op-1812:::not an identifier"})
    assert lost == PRESET_OP_ROUTE_LOST

    none, _ = preset_op_route_state(
        "made-up-op-1812", {"syntax": "made-up-op-1812[:REF]"})
    assert none == PRESET_OP_ROUTE_NONE

    absent, _ = preset_op_route_state("made-up-op-1812", {})
    assert absent == PRESET_OP_ROUTE_NONE

    ok, _ = preset_op_route_state("git-commit", GIT_COMMIT_ENTRY)
    assert ok == PRESET_OP_ROUTE_OK


def test_the_classifier_leaves_no_state_behind():
    """It installs a config to ask the product's own registry builder, so a
    leak here would hand the next test a route nobody asked for."""
    before = (supertool._CONFIG, supertool._AT_FILE_REGISTRY_BUILT,
              list(supertool._AT_FILE_DROPPED_ROUTES))
    preset_op_route_state("git-commit", GIT_COMMIT_ENTRY)
    assert supertool._CONFIG == before[0]
    assert supertool._AT_FILE_REGISTRY_BUILT == before[1]
    assert list(supertool._AT_FILE_DROPPED_ROUTES) == before[2]
    assert supertool._at_file_fields("git-commit") == []


def test_payload_route_required_of_an_op_that_has_none_fails(with_preset_op,
                                                             monkeypatch):
    """`payload_route=True` is the loud form. Proven by mutation rather than by
    reading: an op with a routeless syntax must be refused when it is demanded,
    and accepted when it is not."""
    routeless = {"cmd": ["true"], "syntax": "made-up-op-1812[:REF]"}
    monkeypatch.setattr("conftest._shipped_ops",
                        lambda: {"made-up-op-1812": routeless})

    # Accepted when the route is not demanded ...
    assert with_preset_op("made-up-op-1812") == {"made-up-op-1812": routeless}
    # ... and refused when it is.
    exc = refusal(with_preset_op, "made-up-op-1812", payload_route=True)
    assert PRESET_OP_ROUTE_NONE in str(exc)


def test_a_lost_route_is_refused_even_when_it_was_not_demanded(with_preset_op,
                                                               monkeypatch):
    """`payload_route=False` means "I did not ask for one", never "install a
    broken one quietly". A `:::` syntax that derives no fields is a defect in
    the manifest and must not reach a test as a working install."""
    broken = {"cmd": ["true"], "syntax": "made-up-op-1812:::not an identifier"}
    monkeypatch.setattr("conftest._shipped_ops",
                        lambda: {"made-up-op-1812": broken})
    exc = refusal(with_preset_op, "made-up-op-1812")
    assert PRESET_OP_ROUTE_LOST in str(exc)


def test_asking_for_nothing_is_a_mistake_not_a_no_op(with_preset_op):
    assert "no op name" in str(refusal(with_preset_op))
