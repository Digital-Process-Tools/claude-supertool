"""#2029 — the hook-output cap is Claude Code's own constant, not a bracket midpoint.

`_HOOK_OUTPUT_CAP_BYTES` was 7168: a midpoint picked from "6.6KB landed, 11KB+
got persisted". The comment said so. The real value is in the Claude Code
bundle:

    var CKr=50000, mor=500000, AKr=4, A0u=400000, R0u=200000, i3=50, k0u=1e4;

    async function jKe(e, t, r, n = k0u) {
        if (e.length <= n) return e;
        let o = await x2e(e, `hook-${t}-${r}`);
        if (I2e(o)) return M("tengu_hook_output_persisted", ...

`k0u = 1e4`. Both original observations fit it, and the midpoint drawn between
them was 40% low — which matters because this constant is the premise of what a
fresh session is shown (`hooks/session-start.sh`, #2028) and of the truncation
warning `op_ops(compact=True)` prepends.

The test pins the number *and* the reasoning, because a bare `== 10000` invites
the next reader to nudge it the way the last one did.
"""

from __future__ import annotations

import inspect

import supertool


def test_the_cap_is_the_harness_constant() -> None:
    assert supertool._HOOK_OUTPUT_CAP_BYTES == 10000


def test_the_boundary_is_inclusive() -> None:
    """`if (e.length <= n) return e` — exactly the cap passes.

    Asserted against the helper rather than the constant, so a future check
    written as `>=` is caught here rather than costing one listing.
    """
    cap = supertool._HOOK_OUTPUT_CAP_BYTES
    assert not supertool._over_hook_cap("x" * cap)
    assert supertool._over_hook_cap("x" * (cap + 1))


def _cap_comment() -> str:
    """The comment block immediately above the constant."""
    src = inspect.getsource(supertool)
    idx = src.index("_HOOK_OUTPUT_CAP_BYTES = ")
    return src[max(0, idx - 2000):idx]


def test_the_comment_names_its_source() -> None:
    """A number with no source is the one that gets rounded again.

    The old comment's honesty is what made this fixable — it said "appears to
    be" and showed its bracket. The replacement has to keep that standard: name
    the constant in the bundle, not just assert a value.
    """
    comment = _cap_comment()
    for token in ("k0u", "jKe", "2000"):
        assert token in comment, f"the cap comment does not mention {token!r}"


def test_the_comment_records_the_unit_mismatch() -> None:
    """`e.length` is UTF-16 code units; supertool counts bytes.

    Every em dash and arrow in a listing is one character and three bytes, so a
    byte count over-reports against this limit. That is the safe direction —
    it can only warn early — but a reader who does not know it will one day
    "fix" the discrepancy in the unsafe direction.
    """
    comment = _cap_comment().lower()
    assert "byte" in comment and "character" in comment


def test_the_empirical_bracket_survives_as_corroboration() -> None:
    """6.6KB landed and 11KB+ persisted. Both bracket 10,000, which is why the
    sourced value is believable rather than merely asserted. Deleting the
    observations would leave the claim resting on one reading of a binary."""
    comment = _cap_comment()
    assert "6.6" in comment, "the known-good observation was dropped"
    assert "11" in comment, "the known-persisted observation was dropped"
