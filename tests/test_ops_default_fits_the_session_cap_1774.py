"""#1774 — the one call that teaches the tool cost more than the session it taught.

Measured on 0.46.0, in this checkout:

    ops           74,838 bytes   (164 lines)
    ops-compact   14,708 bytes
    _HOOK_OUTPUT_CAP_BYTES        7,168

Both renders are over the SessionStart cap, so the startup injection had already
fallen back to a bare alphabetical roster carrying no signatures at all — and an
interactive `ops` spent ~19k tokens to answer "which op lists PRs".

The cost is not spread evenly. Across the 128 documented ops the median
description is 151 characters and the top ten rows are 37,739 — **49% of the
corpus in 8% of the ops** — because a `description` is rendered whole by both
`ops` and `help:OP` and has been carrying the record of how each op got here.
#1775 put a ratchet under that; this is the other half.

**The default listing is signatures.** Every op, every name, the exact shape of
the call — 88 rows in this tree, ~3.1KB of signature — and the descriptions move
behind `ops:full`, where a reader who wants the whole reference asks for it. The
withheld size is stated rather than implied, because a shorter listing that says
nothing is the house defect: an absence produced by the tool, read as an absence
in the world.

`help:OP` is unaffected and remains the per-op reference. `ops:roster` (#1231)
remains names-plus-safety-class, which is a different question — it answers
"what exists", where the default now answers "how is it called".
"""
from __future__ import annotations

from pathlib import Path

import supertool


REPO_ROOT = Path(__file__).parent.parent


def test_the_default_listing_fits_the_session_cap(shipped_config) -> None:
    """The acceptance number, asserted against the constant and not a literal
    so the two cannot drift."""
    body = supertool.op_ops()
    size = len(body.encode("utf-8"))
    assert size <= supertool._HOOK_OUTPUT_CAP_BYTES, size


def test_the_default_listing_is_still_every_op(shipped_config) -> None:
    """Smaller by dropping prose, never by dropping rows. A listing that fit
    the cap by hiding ops would be the #1231 defect wearing the #1774 fix."""
    body = supertool.op_ops()
    full = supertool.op_ops(full=True)
    for name in ("read", "grep", "paste", "gh-prs", "git-push", "batch"):
        assert name in body, name
    assert body.count("\n- `") == full.count("\n- `")


def test_the_default_listing_carries_no_descriptions(shipped_config) -> None:
    """The 4,512-character entry is the one this issue was filed over."""
    desc = shipped_config["ops"]["gh-prs"]["description"]
    body = supertool.op_ops()
    assert desc[:60] not in body
    assert "gh-prs" in body


def test_the_full_listing_still_carries_them(shipped_config) -> None:
    """Nothing is deleted — it moves behind a token."""
    desc = shipped_config["ops"]["gh-prs"]["description"]
    full = supertool.op_ops(full=True)
    assert desc[:60] in full


def test_the_default_says_what_it_withheld_and_how_to_get_it(
        shipped_config) -> None:
    """Three states, not two. A listing that quietly stopped carrying prose is
    indistinguishable from ops that stopped having any."""
    body = supertool.op_ops()
    assert "ops:full" in body
    assert "help:" in body
    withheld = len(supertool.op_ops(full=True).encode("utf-8"))
    assert str(withheld) in body


def test_ops_full_is_dispatchable(shipped_config) -> None:
    """The token has to reach the render, not only exist in the footer."""
    out = supertool.dispatch("ops:full")
    desc = shipped_config["ops"]["gh-prs"]["description"]
    assert desc[:60] in out


def test_an_unknown_token_is_still_refused(shipped_config) -> None:
    """#1231's rule, restated over the token this issue adds: a mode the op
    does not have is refused, and the refusal names the modes it does."""
    out = supertool.dispatch("ops:fullish")
    assert "ERROR" in out
    assert "ops:full" in out


def test_compact_is_untouched_by_the_default_change(shipped_config) -> None:
    """`ops-compact` is the SessionStart render and has its own contract
    (#1231's `hint` keys). This issue is about the interactive default."""
    compact = supertool.op_ops(compact=True)
    hinted = [n for n, i in shipped_config["ops"].items()
              if isinstance(i, dict) and i.get("hint")]
    if hinted:
        desc = shipped_config["ops"][hinted[0]]["description"]
        assert desc[:40] in compact
