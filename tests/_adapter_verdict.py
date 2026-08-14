"""One place that turns an adapter's verdict into a failure message (#725, #717).

Every validator under `validators/` answers with the same JSON object: `tool`,
`ok`, `count`, `errors`, `duration_ms` (see `validators/SCHEMA.md`). A test
spawning one therefore has a structured verdict in hand at the moment it fails
— and the suite's habit was to throw all of it away:

    assert out["ok"] is True

which fires as `assert False is True`. An adapter has roughly a dozen routes to
`ok=False`: the tool was absent, the tool was present but not executable, the
adapter's own internal budget expired, the file really did not parse, the
adapter was handed no file at all. That output separates none of them.

Twice now that has cost a whole occurrence. #658/#717 (`test_valid_ruby`) and
#725 (the phplint spawn test) are both a single Windows red that never
reproduced, both diagnosed as "cause unknown", and in both cases the adapter
had *already said why* — into a payload the assertion discarded. These
failures are rare enough that each occurrence is the entire diagnostic budget.

**The rule: a test asserting a verdict renders the verdict when it fails.**
Not the reader's job to re-run it with prints; the reader may not have the
platform it happened on.

**And the rule that makes that safe:** this file formats a structure it does
not own. Adapters are separate programs, some third-party-shaped, and a
payload can arrive in a shape no one anticipated — a crash before any JSON, an
`errors` that is a string, a list where an object belongs. A formatter that
renders blank on those reproduces the defect it exists to fix, one layer in.
So every branch here ends in text that names what it could not read, and none
of them can raise. `describe()` returning "" is the bug.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

import _core_timeout_census

__all__ = [
    "verdict", "describe", "assert_ok", "assert_declined", "assert_adapter_ok",
    "stalled_at_its_own_wall", "skip_if_stalled",
    "assert_adapter_ok_or_skip_if_stalled",
    "ADAPTER_WALL_TOKEN", "ADAPTER_WALL_POPULATION", "adapter_wall_line",
    "CORE_TIMEOUT_KEY", "core_timed_out", "skip_if_core_timed_out",
    "run_one_or_skip",
]

#: Grep handle, and the difference between the two shapes #1604 put on the
#: table. Normalising a wall away erases it; declining without counting hides it
#: one layer up, because `N skipped` on a Windows leg cannot then be resolved to
#: `N tests did not get an adapter verdict`. #794 shipped the decline with no
#: token for four instances (#1296, #1360, #1461, #1501) before #1604 was the
#: fifth. Every other decline register here carries one.
ADAPTER_WALL_TOKEN = "adapter-wall(#794,#1604)"

#: Printed under the count, never without it (#1274): a bare N reads as a total.
ADAPTER_WALL_POPULATION = (
    "  ^ counts skips carrying that token only, not every test that spawns an "
    "adapter: the majority of adapter-spawning test files are NOT gated yet and "
    "still publish a wall as a verdict, so a zero here is a statement about the "
    "gated sites and not about the suite. The gated ones are the callers of "
    "`skip_if_stalled` / `assert_adapter_ok_or_skip_if_stalled` in "
    "tests/_adapter_verdict.py; an adapter that declined for any reason other "
    "than its own wall is deliberately not counted and stays red.")


def adapter_wall_line(n: int, total: int) -> str:
    """``N of M skipped``, never a bare ``N`` (#1274)."""
    return (
        "{0}: {1} of {2} skipped tests did NOT get an adapter verdict -- the "
        "adapter spent its whole internal budget without reaching one (expect "
        "0; a non-zero count is a runner too loaded to produce a verdict, not "
        "a finding about any file)".format(ADAPTER_WALL_TOKEN, n, total))

MAX_ERRORS_SHOWN = 3
MAX_FIELD_CHARS = 200
MAX_STREAM_CHARS = 600

# The code every adapter reserves for "no verdict was obtained" — a tool that
# was absent, a spawn that expired, output that would not parse. See
# validators/SCHEMA.md, "`adapter`: the reserved code for no verdict".
ADAPTER_CODE = "adapter"

#: How an adapter spells "I ran out of wall". Both spellings are already in the
#: tree and neither is wrong: `timeout` (xmllint, node-check, stylelint,
#: cargo-check, eslint, gitleaks, shellcheck, …) and `timed out` (go-vet,
#: pyright, cargo-check's `cargo metadata` helper). Matching only the first made
#: this predicate blind to the exact payload that produced #1461 — every other
#: clause matched, so the guard read a wall as a verdict about the package and
#: the assertion written for a lint result fired on one.
WALL_PHRASES = ("timeout", "timed out")


def _clip(text: object, limit: int = MAX_FIELD_CHARS) -> str:
    s = text if isinstance(text, str) else repr(text)
    s = " ".join(s.split())
    if len(s) <= limit:
        return s
    return s[:limit] + f"… (+{len(s) - limit} chars)"


def _describe_error(entry: object) -> str:
    """One error, however malformed. Never returns ""."""
    if not isinstance(entry, dict):
        return f"<non-object error entry: {_clip(entry)}>"

    code = entry.get("code")
    line = entry.get("line")
    col = entry.get("col")
    msg = entry.get("msg")

    where = ""
    if line is not None:
        where = f"line {line}"
        if col is not None:
            where += f":{col}"

    head = f"[{code}]" if code is not None else "[no code]"
    parts = [head]
    if where:
        parts.append(where)
    parts.append(_clip(msg) if msg is not None else f"<no msg; fields: {sorted(entry)}>")
    return " ".join(parts)


def describe(payload: Any) -> str:
    """Render a verdict for a human reading a CI log. Never blank, never raises."""
    try:
        if not isinstance(payload, dict):
            return (
                "<not a verdict payload: expected a JSON object per "
                f"validators/SCHEMA.md, got {type(payload).__name__}: {_clip(payload)}>"
            )

        tool = payload.get("tool", "<no tool key>")
        count = payload.get("count", "?")
        head = f"{tool} reported ok={payload.get('ok')!r} count={count}"
        # duration_ms separates "declined instantly" (tool missing, no file arg)
        # from "declined at its own wall" (the adapter's internal budget fired,
        # which reads as a verdict and not as a TimeoutExpired). That distinction
        # is the open question in #725 and it is free to print.
        if "duration_ms" in payload:
            head += f" after {payload['duration_ms']}ms"

        if "errors" not in payload:
            return (
                f"{head}, and its payload carries no \"errors\" key to explain it "
                f"— keys present: {sorted(payload)}"
            )

        errors = payload["errors"]
        if not isinstance(errors, list):
            return (
                f"{head}; \"errors\" is a {type(errors).__name__}, not a list: "
                f"{_clip(errors)}"
            )
        if not errors:
            return f"{head}, with an empty \"errors\" list — the adapter declined without saying why"

        shown = [_describe_error(e) for e in errors[:MAX_ERRORS_SHOWN]]
        rendered = "; ".join(shown)
        if len(errors) > MAX_ERRORS_SHOWN:
            rendered += f"; (+{len(errors) - MAX_ERRORS_SHOWN} more of {len(errors)} not shown)"
        return f"{head}: {rendered}"
    except Exception as exc:  # pragma: no cover - the formatter must not become the failure
        return f"<could not render verdict payload ({type(exc).__name__}: {exc}): {_clip(payload)}>"


def verdict(result: subprocess.CompletedProcess, *, adapter: object = None) -> dict:
    """Parse an adapter spawn's stdout, or fail saying what arrived instead.

    Replaces a bare `json.loads(r.stdout.strip())`, whose failure mode is a
    `JSONDecodeError` naming neither the adapter, the exit code, nor the
    stderr that almost always holds the traceback.
    """
    who = f"{adapter} " if adapter is not None else ""
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    stderr = result.stderr if isinstance(result.stderr, str) else ""
    tail = f"exit={result.returncode}; stderr={_clip(stderr, MAX_STREAM_CHARS)!r}"

    if not stdout.strip():
        raise AssertionError(f"{who}adapter produced no output on stdout ({tail})")

    try:
        payload = json.loads(stdout.strip())
    except (ValueError, TypeError) as exc:
        raise AssertionError(
            f"{who}adapter stdout is not JSON ({exc}); {tail}; "
            f"stdout={_clip(stdout, MAX_STREAM_CHARS)!r}"
        ) from None

    if not isinstance(payload, dict):
        raise AssertionError(
            f"{who}adapter emitted valid JSON that is not a verdict object: "
            f"got {type(payload).__name__}: {_clip(payload)}; {tail}"
        )
    return payload


def assert_ok(payload: Any, *, context: str = "") -> Any:
    """Assert the adapter passed the file, and say what it said when it did not."""
    if not isinstance(payload, dict):
        raise AssertionError(describe(payload))

    if "ok" not in payload:
        raise AssertionError(
            f'verdict payload has no "ok" key; keys present: {sorted(payload)}'
        )

    if payload["ok"] is not True:
        about = f" on {context}" if context else ""
        raise AssertionError(f"expected a clean verdict{about} — {describe(payload)}")
    return payload


def assert_declined(payload: Any, *, context: str = "") -> Any:
    """The other direction: the adapter was supposed to reject this and did not.

    `assert out["ok"] is False` fires as `assert True is False`, which is the
    same nothing. It costs less than its twin — a test asserting a *known bad*
    file is rejected can usually be re-run locally — but the payload is right
    there, and "it passed with count=0" versus "it passed because the tool was
    missing and the adapter degraded gracefully" are very different reds.
    """
    if not isinstance(payload, dict):
        raise AssertionError(describe(payload))

    if "ok" not in payload:
        raise AssertionError(
            f'verdict payload has no "ok" key; keys present: {sorted(payload)}'
        )

    if payload["ok"] is not False:
        about = f" for {context}" if context else ""
        raise AssertionError(
            f"expected the adapter to decline{about}, and it did not — {describe(payload)}"
        )
    return payload


def assert_adapter_ok(
    result: subprocess.CompletedProcess, *, adapter: object = None, context: str = ""
) -> dict:
    """`verdict` then `assert_ok`, for the common one-spawn-one-assertion case."""
    return assert_ok(verdict(result, adapter=adapter), context=context)


def stalled_at_its_own_wall(payload: Any, *, inner_s: int) -> str | None:
    """Did the adapter spend its whole internal budget without a verdict? (#794)

    Returns the reason to decline, or ``None`` when the payload is a verdict
    about the file and must be asserted on normally. Never raises: a predicate
    that throws while classifying a failure becomes the failure.

    **All four clauses are load-bearing**, and each one is a route back to the
    bug this exists to remove if it is dropped:

    - ``ok is False`` — a pass is a pass.
    - every error carries ``code: "adapter"`` — a `parse` finding beside a
      stall is still a broken file, and one unmeasurable error does not buy
      amnesty for a measured one.
    - the message names a timeout, in any of ``WALL_PHRASES`` —
      `validators/SCHEMA.md` is explicit that an `adapter` error "stays a real
      error ... because the process ran and something is broken that someone
      has to fix". A missing binary and an unreadable argv are that. Only a
      wall is a statement about the machine. The phrase list is a list because
      an adapter that says "timed out" is saying the same thing as one that
      says "timeout", and a guard that can only hear one of them is a guard
      with a hole in exactly the shape of the payload it never saw (#1461).
    - ``duration_ms`` reaches ``inner_s`` — an adapter that reports `timeout`
      in 12ms did not time out, and its error routing is broken. That is a
      defect in the thing the suite tests, and a message-only predicate would
      swallow it.

    ``inner_s`` is passed in rather than read here, so this file keeps knowing
    nothing about `_adapter_budget` and no second copy of any adapter's budget
    is written down (#702).
    """
    try:
        if not isinstance(payload, dict) or payload.get("ok") is not False:
            return None

        errors = payload.get("errors")
        if not isinstance(errors, list) or not errors:
            return None

        for entry in errors:
            if not isinstance(entry, dict) or entry.get("code") != ADAPTER_CODE:
                return None
            msg = entry.get("msg")
            if not isinstance(msg, str):
                return None
            lowered = msg.lower()
            if not any(phrase in lowered for phrase in WALL_PHRASES):
                return None

        duration = payload.get("duration_ms")
        if not isinstance(duration, int) or isinstance(duration, bool):
            return None
        if duration < inner_s * 1000:
            return None

        return (
            f"{ADAPTER_WALL_TOKEN}: adapter spent its whole {inner_s}s internal "
            f"budget without reaching a verdict, so there is nothing here to "
            f"assert on — {describe(payload)}"
        )
    except Exception:  # pragma: no cover - classification must never be the failure
        return None


def skip_if_stalled(payload: Any, *, inner_s: int) -> Any:
    """Decline if `payload` is a stall; otherwise hand it back untouched (#1296).

    `stalled_at_its_own_wall` classifies, this acts, and they are separate so
    that a caller which wants to assert *on* a stall still can. Returns the
    payload so it can wrap a parse in one expression.

    Split out of `assert_adapter_ok_or_skip_if_stalled` because the wrapper
    below only covers tests asserting a file is **clean**, and the failure that
    prompted this (#1296) was a test asserting a file is **broken**: a stall
    there is `ok: false` with `count: 1`, so `assert_declined` passes and the
    next line -- a pinned source line the adapter never reached -- is what
    fails. Same non-verdict, opposite assertion, and the wrapper could not be
    reused because there is nothing here to assert ok about.
    """
    reason = stalled_at_its_own_wall(payload, inner_s=inner_s)
    if reason is not None:
        pytest.skip(reason)
    return payload


#: The key `_validator_run_one`'s `TimeoutExpired` arm stamps on the payload it
#: fabricates when the adapter's spawn outlives `spec["timeout"]`. It is written
#: in exactly one place in `_supertool.py` and `_validator_strip_core_keys` drops
#: it from anything an adapter or a cache entry says (#1036, #1044), so its
#: presence identifies that arm and nothing else can claim it.
CORE_TIMEOUT_KEY = "timeout"


def core_timed_out(payload: Any) -> str | None:
    """Did the *core* give up waiting for the adapter? (#1501)

    The sibling of `stalled_at_its_own_wall`, one layer out. That one classifies
    an **adapter** reporting its own internal budget as an ordinary `ok: false`
    verdict; this one classifies the payload the **core** invents when the
    adapter never answered at all. Different party, different payload, different
    route — and the two must stay separate, because a test asserting on one is
    not asserting on the other.

    Returns the reason to decline, or ``None`` when the payload is a verdict
    about the file and must be asserted on normally. Never raises.

    **One clause, and the shortness is the design.** `stalled_at_its_own_wall`
    needs four because an adapter's stall is indistinguishable from its verdicts
    except by reading them. This one needs none of that: `timeout is True` is
    unforgeable, so every extra clause could only produce a false negative — and
    every clause a message or a `count` could contribute would produce a false
    *positive*, which is worse. In particular:

    - **not `count == 1`.** That is the number that showed up in the Windows log
      (#1501: `assert 1 == 2`) and it is also what a real adapter reports for a
      single syntax error. Declining on it would mute every genuine one-error
      verdict in the suite — the same defect this exists to remove, pointed the
      other way.
    - **not the message.** `WALL_PHRASES` exists because an adapter's stall has
      nothing else to go on. Here it would only add a way to swallow a real
      finding whose text happens to mention a timeout.
    - **`is True`, not truthiness.** A payload carrying `"timeout": 10` is
      echoing a setting, not reporting that the wall fired.

    The core's arm itself is correct and stays: it is an absence, it says so, and
    #969 pins that it never rolls an edit back. Only the assertion site was
    wrong.
    """
    try:
        if not isinstance(payload, dict):
            return None
        if payload.get(CORE_TIMEOUT_KEY) is not True:
            return None
        return (
            f"{_core_timeout_census.TOKEN}: "
            "the core's own spawn wall fired before the adapter answered, so "
            "there is no verdict about this file to assert on — "
            f"{describe(payload)}"
        )
    except Exception:  # pragma: no cover - classification must never be the failure
        return None


def skip_if_core_timed_out(payload: Any) -> Any:
    """Decline if the core timed the adapter out; else hand `payload` back (#1501).

    Wraps a `_validator_run_one` return in one expression so a call site keeps
    its shape:

        out = skip_if_core_timed_out(supertool._validator_run_one(n, spec, f))

    Split from `core_timed_out` for the same reason `skip_if_stalled` is split
    from `stalled_at_its_own_wall`: a test that wants to assert *on* the arm
    still can, and one does.
    """
    reason = core_timed_out(payload)
    if reason is not None:
        pytest.skip(reason)
    return payload


def run_one_or_skip(name: str, spec: dict, target: str, **kwargs: Any) -> Any:
    """`supertool._validator_run_one`, with the core's own wall as a skip (#1501).

    The adopted form at every unit-test call site that hands the core a `cmd` it
    will actually spawn. One wrapper rather than 42 hand-written ones: the risk
    is uniform, so a site that opts out by writing the raw call is the thing
    worth noticing, and the guard in
    `tests/test_core_timeout_is_not_a_verdict_1501.py` notices it.

    `supertool` is imported here rather than at module scope because several
    test files insert the repo root on `sys.path` before importing it, and this
    module is imported from some of them.

    **Every call is counted, declined or not (#1523).** The right to decline had
    no floor under it: a runner slow enough to blow the core's wall on every
    spawn muted all 42 sites at once and the session still exited 0, having
    asserted nothing about the validator core. `_core_timeout_census` turns the
    two numbers into a terminal-summary line and reds the session when the
    decline count reaches the call count. Counting happens here rather than in
    `skip_if_core_timed_out` because tests call that one directly on hand-built
    payloads to assert *on* the arm, and those are not spawns.
    """
    import supertool

    payload = supertool._validator_run_one(name, spec, target, **kwargs)
    reason = core_timed_out(payload)
    _core_timeout_census.record(reason is not None)
    if reason is not None:
        pytest.skip(reason)
    return payload


def assert_adapter_ok_or_skip_if_stalled(
    result: subprocess.CompletedProcess,
    *,
    adapter: object = None,
    inner_s: int,
    context: str = "",
) -> dict:
    """`assert_adapter_ok`, except that a stalled adapter declines (#794).

    For the one shape of test that spawns a real adapter to check a real tool
    answers about a real file. Such a test is asserting a *lint verdict*, and a
    stalled adapter has not produced one — it has reported, correctly and on
    contract, that it never got to look. Failing on that publishes "this PHP
    file is broken" about a file that is fine, on whichever pull request
    happened to draw the loaded runner.

    This is the three-state contract of `docs/validators.md` §"Declining
    instead of guessing" applied one layer out, to the test rather than the
    adapter. It moves nothing else: the adapter still reports the stall as
    `ok: false` per SCHEMA.md, and a blown *outer* budget still raises
    `TimeoutExpired` and still fails, because that one means the adapter
    ignored its own timeout and is a genuine hang (see `_adapter_budget`).

    The skip reason carries the rendered verdict, duration included — a 30s
    spawn is worth knowing about even when it is not worth a red.
    """
    payload = skip_if_stalled(verdict(result, adapter=adapter), inner_s=inner_s)
    return assert_ok(payload, context=context)
