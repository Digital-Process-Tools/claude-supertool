"""#2028 — a session starts with signatures, not just names.

`ops:roster` gives names and a safety class. That prevents "I did not know this
op existed" (#614). It does not prevent the failure one level up: *I did not
know this op was the answer*. An op's own error teaches its signature, which is
true and is why the roster says so — but an error only fires after the decision
to call has already been made. A name a reader cannot interpret is a capability
never reached for, and nothing fails when that happens.

`ops` has been signatures-only since #1774 and renders in 3,740 bytes against a
10,000-byte cap (#2029). The roster spends 20% of the allowance and withholds
every signature; `ops` spends 37% and withholds none.

`ops:session` is the choice, made where the cap constant lives rather than in
shell: signatures when they fit, the roster when they do not, and a stated
reason either way — never a silently shorter listing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import supertool

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks" / "session-start.sh"


def test_the_hook_asks_for_the_session_listing() -> None:
    text = HOOK.read_text(encoding="utf-8")
    # Keyed on the op arguments, not on `"$BIN"` alone: the wrapper-symlink
    # setup above the onboarding call also names `$BIN`, and a predicate that
    # matched those would assert about lines that print no listing at all.
    invocations = [ln for ln in text.splitlines()
                   if '"$BIN"' in ln and "'introduction'" in ln]
    assert invocations, "no supertool onboarding call found in the hook"
    # The comment above it discusses the roster at length and should — what
    # must not happen is the *call* naming it, because then two places decide
    # which listing a session gets and one of them goes stale. That is the
    # failure this whole change is downstream of.
    assert all("'ops:session'" in ln for ln in invocations), invocations
    assert not any("'ops:roster'" in ln for ln in invocations), invocations


def test_the_session_listing_is_the_signature_listing(
        shipped_config: dict) -> None:
    out = supertool.dispatch("ops:session")
    assert "read:PATH" in out, "no signatures — this is the roster"
    assert "between:SYMBOL:PATH" in out


def test_it_fits_the_cap_with_room(shipped_config: dict) -> None:
    """Measured, not asserted. The whole point of #2029 was that a number
    standing in for the harness had drifted 40% without anyone noticing."""
    out = supertool.dispatch("ops:session")
    assert not supertool._over_hook_cap(out), len(out.encode("utf-8"))


def test_it_carries_the_safety_class(shipped_config: dict) -> None:
    """The one thing the roster had that the signature listing did not.

    It is what stops an agent probing an op that opens issues or merges a pull
    request to learn its arguments — the roster's own rule: an op you may probe
    needs only a name, one you may not needs a class.
    """
    out = supertool.dispatch("ops:session")
    assert "paste:::PATH:::CONTENT` *" in out or "paste" in out
    marked = [ln for ln in out.splitlines() if ln.startswith("- `")]
    assert any(ln.rstrip().endswith("*") for ln in marked), "no writes marker"
    assert any(ln.rstrip().endswith("!") for ln in marked), "no acts marker"
    assert any(not ln.rstrip().endswith(("*", "!")) for ln in marked), (
        "everything is marked — unmarked read-only is a positive claim and "
        "must stay distinguishable")


def test_the_legend_explains_all_three_classes(shipped_config: dict) -> None:
    out = supertool.dispatch("ops:session")
    for token in ("read-only", "`*`", "`!`"):
        assert token in out, token


def test_an_over_cap_listing_falls_back_and_says_so(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Three states, not two. A listing too large to inject must not be
    silently swapped for a shorter one — that is this repo's own defect class,
    arriving through the hook that introduces the tool."""
    big = {
        f"op_{i}": {"syntax": f"op_{i}:PATH:LIMIT[:CONTEXT][:MODE]:MORE:STILL"}
        for i in range(supertool._HOOK_OUTPUT_CAP_BYTES // 20)
    }
    monkeypatch.setattr(supertool, "_CONFIG", {"builtin-ops": big})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    assert supertool._over_hook_cap(supertool.op_ops()), (
        "the fixture does not exceed the cap, so the fallback is untested")
    out = supertool.dispatch("ops:session")
    assert "read:PATH" not in out, "the signatures were printed anyway"
    # The roster's names come from `_valid_op_names()`, so this fixture's
    # config-only entries are not in it by design — assert on a real one.
    assert "grep" in out, "the fallback is not the roster"
    assert str(supertool._HOOK_OUTPUT_CAP_BYTES) in out, (
        "the fallback does not say what it measured against")
    assert "withheld" in out.lower(), (
        "a shorter listing arrived with no account of itself")


def test_the_fallback_names_the_listing_it_withheld(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reader given the roster must be able to ask for what they did not get.
    An absence with no name attached is one nobody can act on."""
    big = {
        f"op_{i}": {"syntax": f"op_{i}:PATH:LIMIT[:CONTEXT][:MODE]:MORE:STILL"}
        for i in range(supertool._HOOK_OUTPUT_CAP_BYTES // 20)
    }
    monkeypatch.setattr(supertool, "_CONFIG", {"builtin-ops": big})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    out = supertool.dispatch("ops:session")
    assert "`ops`" in out


def test_an_unknown_ops_argument_is_still_refused(shipped_config: dict) -> None:
    """Adding an accepted token must not widen the arm that refuses the rest
    (#1231) — the first pass at that refusal fixed `ops` and left `ops-compact`
    swallowing the same token one elif over."""
    out = supertool.dispatch("ops:sessions")
    assert out.startswith("ERROR")


def test_the_session_token_is_ops_only(shipped_config: dict) -> None:
    out = supertool.dispatch("ops-compact:session")
    assert out.startswith("ERROR")
