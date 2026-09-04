"""`edit` takes one edit per payload; that has to be discoverable, not just
enforced (#1867).

`edit:@-` refuses an `[[edits]]`/`edits = [...]` payload correctly -- the
refusal names `edits` as unknown and lists the accepted keys. What it does
not do is say the one-edit-per-payload contract as a positive statement, or
point at `batch:@file` as the route for more than one edit to the same
file. An author reaching for a batch shape does not learn it is unsupported
until the write is attempted, by which point the payload is already
composed (#1867's own cost: seven mechanical edits -> seven heredocs, plus
one refused round-trip).

Two surfaces carry the fix: the refusal itself (the moment the caller is
already paying for the mistake) and `help:edit` (the moment before they
compose the payload at all).
"""
from __future__ import annotations

from pathlib import Path

import supertool


def test_edits_array_refusal_names_the_one_edit_contract(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("line1\nline2\n")
    # Exercise the same function the payload route itself calls to render the
    # refusal text (`_at_file_to_parts`), rather than wiring a real stdin --
    # that keeps the assertion about the text, not about how stdin is piped.
    payload = {"path": str(f), "edits": [{"old": "line1", "new": "X"}]}
    try:
        supertool._at_file_to_parts("edit", payload)
    except ValueError as exc:
        msg = str(exc)
    else:
        raise AssertionError(
            "an edits array must still be refused, not silently accepted")
    assert "unknown field(s)" in msg and "edits" in msg
    assert "one edit per payload" in msg.lower(), (
        "the refusal names the unknown field but never states the contract "
        "as a positive sentence, so a caller only learns the shape by "
        "already having gotten it wrong: " + msg)
    assert "batch" in msg.lower(), (
        "the remedy for more than one edit -- batch:@file -- must be named "
        "in the same refusal that says edit takes only one: " + msg)


def test_help_edit_states_the_one_edit_contract() -> None:
    out = supertool.dispatch("help:edit")
    assert "one edit per payload" in out.lower(), (
        "the accepted shape must be discoverable from `ops`/`help:edit` "
        "before a batch payload is even composed, per #1867: " + repr(out))
    assert "batch" in out.lower(), (
        "help:edit must point at batch:@file as the multi-edit route: "
        + repr(out))
