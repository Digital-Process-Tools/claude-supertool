"""The core's own spawn wall must not be asserted on as a verdict (#1501).

`master` went NOT GREEN at `423b236` on `pytest (windows-latest, 3.12)` with two
reds in `tests/test_validator_warm_unsafe_345.py`, both reading `assert 1 == 2`
under `--tb=no`. The commit touched `presets/watch/`, a notifier and one line of
a workflow — nothing on the validator path — and the two commits before it were
green on the same leg with the same file. The leg took 8m41s.

`assert 1 == 2` is `out["count"] == 2` reading `1`, and the adapter in that file
is a `{python} -c` that prints a fixed two-error payload: there is no route by
which it says `count: 1`. The `1` is the core's, from `_validator_run_one`'s
`TimeoutExpired` arm, which fabricates `{"ok": False, "count": 1, "errors":
[{"code": "orchestrator", "msg": "timeout after Ns"}], "timeout": True}` when a
cold CPython spawn does not come back inside the spec's 10s.

The arm is right and does not change. It is an absence, it is flagged as one,
and #969 pins that it never rolls an edit back. What is wrong is the assertion
site: `assert out["count"] == 2` cannot tell "the adapter found two problems"
from "the orchestrator gave up waiting", so a machine limit is published as a
product verdict — this repo's own defect class, inside the thing built to detect
it (#1143).

`tests/_adapter_verdict.py` has carried `skip_if_stalled` since #794 for the
neighbouring case: a stall at the **adapter's** own internal wall, which the
adapter reports itself as a normal `ok: false` verdict. That predicate cannot
see this one. The core's arm is a different payload produced by a different
party — the adapter never ran to completion at all — and it is identified by a
key no adapter can set. `skip_if_core_timed_out` is its sibling.

Two directions are pinned here, and the second is the one that matters:

* a `timeout: True` payload declines, so a loaded runner costs a skip;
* a genuine `count: 1` adapter finding passes through **untouched**. Declining
  on `count == 1` — the number that showed up in the log — would mute every
  real single-error verdict in the suite, which is the same defect pointed the
  other way.

And the sweep is enforced rather than asserted in a PR body. Every unit-test
call that hands the core a `cmd` it will really spawn now goes through
`run_one_or_skip`; the guard at the bottom of this file names any raw
`_validator_run_one` call left in `tests/` whose spec can spawn, so a site that
opts out has to do it visibly.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import supertool
from _adapter_verdict import (
    core_timed_out,
    run_one_or_skip,
    skip_if_core_timed_out,
)

TESTS = Path(__file__).parent


# ---------------------------------------------------------------------------
# The payload is really the core's, and really reachable
# ---------------------------------------------------------------------------

def test_the_core_fabricates_count_one_when_the_spawn_outlives_the_wall(
    tmp_path: Path,
) -> None:
    """The reproduction, at 1s instead of the 8m41s leg that found it.

    This is the claim the issue rests on, re-derived in-tree rather than
    inferred from a `--tb=no` line: an adapter that never answers yields a
    payload whose `count` is 1 and which carries no `skipped` key, so both
    assertions that failed on Windows are reachable from a healthy adapter and
    a slow machine.

    It also rules out the other candidate. `_validator_unusable_reply` — the arm
    for a spawn that printed nothing, or printed something without a verdict —
    cannot produce `assert 1 == 2`: it has no `count` key at all, and it carries
    `skipped`, so the assertion above it (`assert "skipped" not in out`) is the
    one that would have fired, with a different message.
    """
    f = tmp_path / "x.php"
    f.write_text("<?php\n", encoding="utf-8")
    spec = {
        "cmd": '{python} -c "import time; time.sleep(30)"',
        "timeout": 1,
        "cache": False,
    }
    # core-timeout: asserted on (#1501) — this test is the reproduction
    out = supertool._validator_run_one("phpunit", spec, str(f))

    # `.get`, not `[...]`: if the spawn failed to start instead of hanging the
    # core routes to `_validator_unusable_reply`, and a KeyError there would
    # report nothing about what actually arrived.
    assert out is not None
    assert out.get("timeout") is True, out
    assert out.get("count") == 1, out
    assert out.get("ok") is False, out
    assert "skipped" not in out, (
        "the core's timeout arm is not a skip — if this ever becomes one, this "
        f"whole guard is looking at the wrong key: {out!r}")

    # The Windows red, in the shape the assertion site had it. `--tb=no` printed
    # exactly this and nothing else, which is why the payload had to be derived.
    with pytest.raises(AssertionError):
        assert out["count"] == 2

    # And what the converted site does with the same payload instead.
    assert core_timed_out(out) is not None, out
    with pytest.raises(pytest.skip.Exception):
        skip_if_core_timed_out(out)


def test_an_unusable_reply_is_not_mistaken_for_the_wall(tmp_path: Path) -> None:
    """The other core arm passes through: it is a skip, and skips are asserted on.

    Several tests exist precisely to pin `_validator_unusable_reply` (#634,
    #975). A guard that declined on those would turn them into permanent skips.
    """
    f = tmp_path / "x.php"
    f.write_text("<?php\n", encoding="utf-8")
    # Through the wrapper, and the budget is generous: if this runner really is
    # slow enough to blow a 60s wall on `-c pass` there is no unusable reply here
    # to classify, and the test that would then fail is this one rather than the
    # thing it is about. A new test may not reintroduce the defect it pins.
    spec = {"cmd": '{python} -c "pass"', "timeout": 60, "cache": False}
    out = run_one_or_skip("phpunit", spec, str(f))

    assert out is not None and "skipped" in out, out
    assert core_timed_out(out) is None, (
        f"an unusable reply was classified as the core's wall: {out!r}")


# ---------------------------------------------------------------------------
# Direction 1 — the wall declines
# ---------------------------------------------------------------------------

def _core_timeout_payload(**extra: object) -> dict:
    """Exactly what `_supertool.py`'s `TimeoutExpired` arm returns."""
    payload = {
        "tool": "phpunit", "file": "x.php", "ok": False, "count": 1,
        "errors": [{"line": None, "col": None, "severity": "error",
                    "code": "orchestrator", "msg": "timeout after 10s"}],
        "duration_ms": 10000, "elapsed_s": 10.4, "timeout": True,
    }
    payload.update(extra)
    return payload


def test_the_wall_is_classified_with_a_reason_naming_it() -> None:
    reason = core_timed_out(_core_timeout_payload())
    assert reason is not None
    assert "timeout after 10s" in reason, reason


def test_skip_if_core_timed_out_declines_the_wall() -> None:
    with pytest.raises(pytest.skip.Exception) as excinfo:
        skip_if_core_timed_out(_core_timeout_payload())
    assert "timeout after 10s" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Direction 2 — everything else passes through untouched
# ---------------------------------------------------------------------------

def test_a_genuine_single_error_verdict_is_returned_unchanged() -> None:
    """`count == 1` is the number the Windows log showed, and it is not the test.

    A real adapter reporting one syntax error looks numerically identical to the
    fabricated payload. Only the core's flag separates them, so only the flag
    may be read.
    """
    real = {
        "tool": "phplint", "file": "x.php", "ok": False, "count": 1,
        "errors": [{"line": 12, "col": 3, "severity": "error",
                    "code": "syntax", "msg": "unexpected ';'"}],
        "duration_ms": 40,
    }
    assert core_timed_out(real) is None, real
    assert skip_if_core_timed_out(real) is real


def test_a_real_finding_that_mentions_a_timeout_is_still_a_finding() -> None:
    """The message is not a clause. An adapter is entitled to say "timeout".

    `stalled_at_its_own_wall` has to read the message because the adapter's own
    stall arrives as an ordinary verdict with nothing else to distinguish it.
    This one does not: the flag is unforgeable, so matching prose would only add
    a way to mute a real error about a `timeout` setting in someone's config.
    """
    real = {
        "tool": "eslint", "file": "a.js", "ok": False, "count": 1,
        "errors": [{"line": 3, "col": 1, "severity": "error", "code": "rule",
                    "msg": "setTimeout: timeout must be a number"}],
        "duration_ms": 90,
    }
    assert core_timed_out(real) is None, real


def test_a_clean_verdict_passes_through() -> None:
    ok = {"tool": "t", "file": "x", "ok": True, "count": 0, "errors": [],
          "duration_ms": 3}
    assert core_timed_out(ok) is None
    assert skip_if_core_timed_out(ok) is ok


def test_a_falsy_or_non_boolean_timeout_key_is_not_the_wall() -> None:
    """`is True`, not truthiness. A spec echo or a stray `"timeout": 10` is not
    a report that the wall fired, and 10 is truthy."""
    for value in (False, None, 0, 10, "true", "yes", [1]):
        payload = _core_timeout_payload(timeout=value)
        assert core_timed_out(payload) is None, value


def test_classification_never_raises_whatever_it_is_handed() -> None:
    """A predicate that throws while classifying a failure becomes the failure."""
    for junk in (None, [], "", 7, {"timeout": True, "errors": "not a list"},
                 {"timeout": True, "errors": [None, 3]}, {"timeout": True}):
        core_timed_out(junk)  # must not raise


def test_the_wall_still_declines_when_the_rest_of_the_payload_is_junk() -> None:
    """The flag is the whole identification, so a mangled payload still skips
    rather than being asserted on — `describe` renders whatever arrived."""
    assert core_timed_out({"timeout": True, "errors": "boom"}) is not None
    assert core_timed_out({"timeout": True}) is not None


# ---------------------------------------------------------------------------
# The sweep, enforced
# ---------------------------------------------------------------------------

GUARD_NAME = "skip_if_core_timed_out"

#: The visible opt-out. A test that *provokes* the wall and asserts on the
#: payload must call the core directly — declining there would delete the thing
#: being pinned, and it did: converting
#: `test_validator_adapter_reply_634.py::test_adapter_timeout_stays_loud` along
#: with the rest turned it into a silent skip. Two sites carry the marker and
#: both assert on the arm; anything else raw is an oversight.
ASSERTED_ON = "# core-timeout: asserted on"


def _is_run_one(func: ast.expr) -> bool:
    if isinstance(func, ast.Attribute):
        return func.attr == "_validator_run_one"
    return isinstance(func, ast.Name) and func.id == "_validator_run_one"


def _is_guard(func: ast.expr) -> bool:
    if isinstance(func, ast.Attribute):
        return func.attr == GUARD_NAME
    return isinstance(func, ast.Name) and func.id == GUARD_NAME


def _spec_cannot_spawn(node: ast.Call) -> bool:
    """A `{"builtin": ...}` spec runs in this process — #477, `_builtin_syntax_run`.

    `_validator_run_one` returns on it before any `cmd` substitution, so there
    is no `subprocess.run` and no `TimeoutExpired` to guard. Recognised from the
    literal in the call rather than from a hand-kept list of line numbers, which
    would go stale on the first insertion above it.
    """
    if len(node.args) < 2 or not isinstance(node.args[1], ast.Dict):
        return False
    keys = {k.value for k in node.args[1].keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return "builtin" in keys and "cmd" not in keys


def _unguarded_calls(path: Path) -> list[int]:
    """Lines calling `_validator_run_one` on a spawning spec without the guard.

    A site that went through `run_one_or_skip` has no raw call left to find, so
    it never reaches here; this only sees the ones written the other way.
    """
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)

    def marked(node: ast.Call) -> bool:
        first = max(node.lineno - 2, 0)          # the comment sits above the call
        last = node.end_lineno or node.lineno
        return any(ASSERTED_ON in line for line in lines[first:last])

    wrapped = {
        id(arg)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_guard(node.func)
        for arg in node.args
        if isinstance(arg, ast.Call)
    }
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _is_run_one(node.func)
        and id(node) not in wrapped
        and not _spec_cannot_spawn(node)
        and not marked(node)
    )


def test_every_spawning_run_one_call_in_the_suite_is_guarded() -> None:
    """The sweep is the deliverable; a PR that converted only the two reds today
    would leave the identical trap in six other files, to surface next month on
    a different leg under a different name.

    Unlike #725's guard this polices the whole directory rather than only files
    that already adopted the helper: the conversion is closed, so nothing is
    being failed for work nobody has done. A new exposed call is meant to fail
    here on the day it is written.
    """
    offenders: dict[str, list[int]] = {}
    for path in sorted(TESTS.glob("test_*.py")):
        lines = _unguarded_calls(path)
        if lines:
            offenders[path.name] = lines
    assert not offenders, (
        "these call _validator_run_one on a spec that can spawn, without "
        f"{GUARD_NAME}, so the core's own timeout can be asserted on as a "
        f"verdict about the file: {offenders}")


def test_the_guard_can_actually_see_an_offender(tmp_path: Path) -> None:
    """A guard that has never caught anything has not been shown to work."""
    subject = tmp_path / "test_sample.py"
    subject.write_text(
        "def test_x():\n"
        "    a = supertool._validator_run_one('t', spec, f)\n"
        "    b = skip_if_core_timed_out(supertool._validator_run_one('t', spec, f))\n"
        "    c = av.skip_if_core_timed_out(_validator_run_one('t', spec, f))\n"
        "    d = supertool._validator_run_one('t', {'builtin': 'python'}, f)\n"
        "    e = supertool._validator_run_one('t', {'builtin': 'p', 'cmd': 'x'}, f)\n"
        "    supertool._validator_run_one('t', spec, f)\n"
        "    other_thing('t', spec, f)\n"
        f"    {ASSERTED_ON} deliberate\n"
        "    g = supertool._validator_run_one('t', spec, f)\n"
        "    h = run_one_or_skip('t', spec, f)\n",
        encoding="utf-8",
    )
    assert _unguarded_calls(subject) == [2, 6, 7]
