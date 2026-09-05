"""`_parse_since` accepted non-finite and future-epoch input and folded it
into a silently-wrong age number instead of reporting a declaration error
(#2332).

Found by the gate-3 release audit for v0.57.0: `float()` accepts `'inf'`,
`'nan'`, `'1e400'` (which overflows to `inf`) without raising, so the
existing `seconds_ago < 0` guard never fires for them, and `@<epoch>` had no
bound against `now` at all. Concretely: `since=@<ms-epoch-mistake>` (the
ordinary typo of giving milliseconds where the documented contract wants
seconds) produced a huge future epoch, and the resulting negative age
rendered in the receipt as "0s ago" -- a caller's bad declaration read back
as maximally fresh, not reported as bad input.

The issue explicitly leaves the two-way call open: (a) reject and report as
a declaration error, or (b) rely on the existing fail-safe routing (a
negative/nonsensical age can satisfy the "recent" branch but can never
satisfy the "quiet long enough" branch, so it can never manufacture
STATE_IDLE). This fix takes (a): `_parse_since` now rejects non-finite input
and any `@<epoch>` in the future, both as a reported error rather than a
silently wrong number -- fail-safe is a property of what happens with a bad
age that gets through, not a reason to keep manufacturing one when the input
was never a valid declaration to begin with.

Every "must reject" case here is paired with a "must still work" case for an
ordinary, valid `since=` value in the same fixture -- a test that only
asserts the absence of the old behavior would also pass if `_parse_since`
rejected everything.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parent.parent
PRESET = ROOT / "presets" / "git" / "worktrees.py"
_spec = importlib.util.spec_from_file_location("git_worktrees", PRESET)
assert _spec is not None and _spec.loader is not None
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)


# ── non-finite input, both forms -- MUST FIRE ─────────────────────────────

def test_parse_since_rejects_inf_duration() -> None:
    got, why = wt._parse_since("inf", now=1000.0)
    assert got is None, (got, why)
    assert why and "inf" in why.lower() or "finite" in (why or "").lower(), why


def test_parse_since_rejects_nan_duration() -> None:
    got, why = wt._parse_since("nan", now=1000.0)
    assert got is None, (got, why)
    assert why, why


def test_parse_since_rejects_at_inf_epoch() -> None:
    got, why = wt._parse_since("@inf", now=1000.0)
    assert got is None, (got, why)
    assert why, why


def test_parse_since_rejects_at_nan_epoch() -> None:
    got, why = wt._parse_since("@nan", now=1000.0)
    assert got is None, (got, why)
    assert why, why


def test_parse_since_rejects_overflowing_literal() -> None:
    """`1e400` overflows float() to `inf` without raising ValueError -- the
    exact gap the old `seconds_ago < 0` guard could never catch."""
    got, why = wt._parse_since("1e400", now=1000.0)
    assert got is None, (got, why)
    assert why, why


# ── future @<epoch> -- MUST FIRE ──────────────────────────────────────────

def test_parse_since_rejects_future_epoch() -> None:
    """The reported repro: an epoch given in milliseconds instead of
    seconds lands far in the future and must be reported, not silently
    folded into a negative age that renders as '0s ago'."""
    now = 1_700_000_000.0
    ms_mistake = now * 1000  # the ordinary typo
    got, why = wt._parse_since(f"@{ms_mistake}", now=now)
    assert got is None, (got, why)
    assert why and "future" in why.lower(), why


def test_parse_since_rejects_epoch_one_second_in_the_future() -> None:
    """MUST FIRE, boundary: strictly after `now` is still a future claim,
    not just wildly-off ones."""
    now = 1000.0
    got, why = wt._parse_since("@1001", now=now)
    assert got is None, (got, why)
    assert why and "future" in why.lower(), why


# ── ordinary, valid input -- MUST NOT FIRE (siblings of every case above) ─

def test_parse_since_accepts_absolute_epoch_in_the_past() -> None:
    got, why = wt._parse_since("@1000", now=99999.0)
    assert why is None, why
    assert got == 1000.0


def test_parse_since_accepts_epoch_equal_to_now() -> None:
    """MUST NOT FIRE, boundary: exactly `now` is not future."""
    got, why = wt._parse_since("@1000", now=1000.0)
    assert why is None, why
    assert got == 1000.0


def test_parse_since_accepts_ordinary_seconds_ago() -> None:
    got, why = wt._parse_since("90", now=1000.0)
    assert why is None, why
    assert got == 910.0


def test_parse_since_still_rejects_garbage() -> None:
    got, why = wt._parse_since("banana", now=1000.0)
    assert got is None
    assert why and "banana" in why


def test_parse_since_still_rejects_negative_duration() -> None:
    got, why = wt._parse_since("-5", now=1000.0)
    assert got is None
    assert why and "negative" in why
