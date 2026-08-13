"""#1524 — a synonym that maps a NAME onto a different QUESTION.

`_OP_SYNONYMS` maps `gh-since-tag -> gh-prs` (#1405 deleted the op into that
one as the `merged-since=` filter). Its own header states the rule it is
bending — *a synonym carries a NAME, not an invocation* — and then cited the
mechanism that closes the gap:

> What closes that gap is `_shipped_preset_ops()`'s own description, which the
> unknown-op message already prints for a preset op.

It did not. `_near_miss_ops` pairs each candidate with a **provenance label**
only (`"builtin"`, `f"preset '{preset}'"`, `"project op"`) and
`_unknown_op_message` renders exactly that, so the caller who typed the release
gate was answered `Did you mean: gh-prs (preset 'github')` — and bare `gh-prs`
is every open PR on the repo, a different board from
`gh-prs:merged-since=TAG,state=merged`. The comment conceded the synonym was
incomplete without the filter and then named a compensating mechanism that
does not exist: #1524's `justifies` class, a false claim in a change's own
prose used as the argument for relaxing a rule.

The fix is in the code, not the sentence. The synonym route is the one route
where the tool holds a **documented fact** rather than a guess (the table's own
header says so), so it — and only it — now prints the target's registry
`syntax`, which is where the rest of the sentence already lives. An
edit-distance guess still prints a bare name: two candidates for one typo, each
dragging a 250-character syntax line, is the roster problem #1222 fixed.
"""
from __future__ import annotations

import supertool


def _suggestion_block(msg: str) -> str:
    """Everything between the ERROR line and the roster."""
    return msg.split("\n", 1)[1].split("Valid operations:", 1)[0]


def test_the_deleted_release_gate_is_answered_with_its_invocation() -> None:
    """The whole defect: a name, where the caller needed the filter.

    Asserts the substring `merged-since=TAG,state=merged` rather than the whole
    syntax line, because that is the part whose absence hands over the wrong
    board. Before the fix the suggestion block is
    `Did you mean: gh-prs (preset 'github')` and nothing else.
    """
    block = _suggestion_block(supertool._unknown_op_message("gh-since-tag"))
    assert "gh-prs" in block
    assert "merged-since=TAG,state=merged" in block, (
        "the synonym points at the board and not at the release gate: " + block)


def test_every_preset_targeted_synonym_carries_its_targets_syntax() -> None:
    """The general pin, so the next entry cannot repeat #1524.

    A builtin target (`write -> paste`, `vi -> vim`) has no registry entry and
    so no syntax to print; the table's header already argues those names are
    whole answers. A PRESET target is the case where the name alone is not, and
    that is what this holds.
    """
    shipped = supertool._shipped_preset_ops()
    preset_targeted = {typed: target
                       for typed, target in supertool._OP_SYNONYMS.items()
                       if target in shipped}
    assert preset_targeted, "no preset-targeted synonym left — this is vacuous"
    for typed, target in sorted(preset_targeted.items()):
        block = _suggestion_block(supertool._unknown_op_message(typed))
        syntax = supertool._registry_syntax(target)
        assert syntax, f"{target} has no registry syntax to teach"
        assert syntax in block, (
            f"'{typed}' names {target} without its invocation: {block}")


def test_a_builtin_targeted_synonym_gains_no_syntax_line() -> None:
    """`write -> paste` was already a whole answer; do not pad it."""
    block = _suggestion_block(supertool._unknown_op_message("write"))
    assert "paste (builtin)" in block
    assert block.count("\n") == 1, block


def test_a_distance_guess_stays_a_bare_name() -> None:
    """`gh-prr` is a typo, not a documented mapping.

    Two candidates arrive from the edit-distance rule. Printing a syntax line
    per candidate would put ~500 characters of filter grammar above a roster
    #1222 already fought to keep readable, for a name the tool is guessing at.
    """
    block = _suggestion_block(supertool._unknown_op_message("gh-prr"))
    # Provenance is deliberately not asserted: it reads "preset 'github'" from
    # a project root and "preset 'github', not loaded here" from anywhere else,
    # and both are correct answers to a different question than this one.
    assert "gh-pr" in block and "gh-prs" in block
    assert block.count("\n") == 1, block


def test_registry_syntax_declines_rather_than_guessing() -> None:
    """Three states: a syntax, no entry, and an entry with no syntax key.

    `docs/validators.md` "Declining instead of guessing". An op with no
    registry entry must return None so the caller prints nothing, rather than
    an empty string that renders as a syntax line teaching nothing.
    """
    assert supertool._registry_syntax("paste") is None
    assert supertool._registry_syntax("no-such-op-anywhere") is None
    assert "merged-since" in (supertool._registry_syntax("gh-prs") or "")
