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

__all__ = [
    "verdict", "describe", "assert_ok", "assert_declined", "assert_adapter_ok",
    "stalled_at_its_own_wall", "skip_if_stalled",
    "assert_adapter_ok_or_skip_if_stalled",
]

MAX_ERRORS_SHOWN = 3
MAX_FIELD_CHARS = 200
MAX_STREAM_CHARS = 600

# The code every adapter reserves for "no verdict was obtained" — a tool that
# was absent, a spawn that expired, output that would not parse. See
# validators/SCHEMA.md, "`adapter`: the reserved code for no verdict".
ADAPTER_CODE = "adapter"


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
    - the message names a timeout — `validators/SCHEMA.md` is explicit that an
      `adapter` error "stays a real error ... because the process ran and
      something is broken that someone has to fix". A missing binary and an
      unreadable argv are that. Only a wall is a statement about the machine.
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
            if not isinstance(msg, str) or "timeout" not in msg.lower():
                return None

        duration = payload.get("duration_ms")
        if not isinstance(duration, int) or isinstance(duration, bool):
            return None
        if duration < inner_s * 1000:
            return None

        return (
            f"adapter spent its whole {inner_s}s internal budget without "
            f"reaching a verdict, so there is nothing here to assert on — "
            f"{describe(payload)}"
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
