"""#1794 item 2 -- `edit`'s nearest-match hint named WHICH KIND of difference
a near miss was (#1855: "differs in N of M lines" / "differs only in
whitespace") but stopped one detail short for the case that actually cost a
round-trip: a single line differing by something invisible at a glance, like
a doubled backslash escaping a quote inside a payload literal. Naming the
first byte where that one line diverges turns "which of these 12 lines" into
"which byte of this one line" -- the whole gap #1794 reports.

Mirrors `_block`/`_file` from test_receipt_arity_and_identity_1855_1860.py
rather than importing them (that file's helpers are undecorated module
functions with no `__all__`, and this file pins a distinct, narrower claim).
"""

from __future__ import annotations

import supertool

NL = chr(10)


def _block(n: int = 12) -> list:
    return ["    value_%d = compute_with_a_fairly_long_name(%d, %d)" % (i, i, i)
            for i in range(n)]


def _file(body: list) -> list:
    return (["header %d" % i for i in range(113)] + body
            + ["tail %d" % i for i in range(20)])


def test_a_single_line_byte_difference_names_the_offset() -> None:
    """The reporter's own shape: one line differs by two characters a human
    skims past. The hint must say WHERE, not just WHICH line."""
    body = _block()
    anchor = list(body)
    # Same length as the real line, differs starting at a specific column --
    # the doubled-backslash-in-a-literal shape, stood in with plain text so
    # the fixture needs no payload machinery.
    anchor[6] = "    value_6 = compute_with_a_fairlyXlong_name(6, 6)"
    hint = supertool._edit_nearest_hint(NL.join(anchor), _file(body), "f.py")
    assert "1 of 12" in hint, f"lost the count this builds on: {hint}"
    assert "offset" in hint.lower(), f"no byte offset named: {hint}"
    # anchor[6] and body[6] first diverge where "fairly_long" (body) meets
    # "fairlyXlong" (anchor) -- both share the prefix "    value_6 = "
    # "compute_with_a_fairly", so the divergent index is len of that shared
    # prefix.
    shared_prefix_len = len("    value_6 = compute_with_a_fairly")
    assert f"offset {shared_prefix_len}" in hint, (
        f"named the wrong offset (expected {shared_prefix_len}): {hint}")
    assert "line 7" in hint, f"named the wrong line (1-based, expected 7): {hint}"


def test_a_multi_line_difference_still_gets_no_offset() -> None:
    """Positive control's opposite: more than one line differs, so no single
    positional pairing can be trusted -- the hint must NOT claim a byte
    offset it cannot back up (docs/validators.md, three states not two)."""
    body = _block()
    anchor = list(body)
    anchor[3] = anchor[3].replace("value_3", "valueX3")
    anchor[8] = anchor[8].replace("value_8", "valueX8")
    hint = supertool._edit_nearest_hint(NL.join(anchor), _file(body), "f.py")
    assert "2 of 12" in hint, f"expected two differing lines: {hint}"
    assert "offset" not in hint.lower(), (
        f"named a byte offset for a two-line difference it cannot pin "
        f"to one pairing: {hint}")


def test_an_inserted_line_still_gets_no_offset() -> None:
    """A height change shifts every positional pairing after it -- naming an
    offset from the wrong pairing would be confidently wrong, which this repo
    treats as worse than naming nothing (docs/validators.md, "Declining
    instead of guessing")."""
    body = _block()
    anchor = body[:6] + ["    # an inserted comment line"] + body[6:11]
    hint = supertool._edit_nearest_hint(NL.join(anchor), _file(body), "f.py")
    assert hint, "no hint at all"
    assert "offset" not in hint.lower(), (
        f"named a byte offset across a height mismatch: {hint}")

def test_the_offset_is_a_real_utf8_byte_offset_not_a_character_count():
    """Self-review finding: a CHARACTER index reads as correct on every
    ASCII fixture in this file and is silently wrong the moment the
    COMMON PREFIX before the divergence point holds a multi-byte character
    -- exactly the kind of invisible-at-a-glance difference #1794 is about.
    The line and its near-miss here share `caf` + accented-e (2 bytes) +
    `_value = compute` as an identical prefix, THEN diverge -- so a
    character count and a byte count disagree by exactly one, and only the
    byte count is correct for a caller re-anchoring with a byte-oriented
    tool.
    """
    accented_e = chr(233)  # 'e' with acute accent, 2 bytes in UTF-8
    body = list(_block())
    body[6] = "    caf" + accented_e + "_value = compute(6, 6)"
    anchor = list(body)
    anchor[6] = "    caf" + accented_e + "_value = computeX(6, 6)"
    hint = supertool._edit_nearest_hint(NL.join(anchor), _file(body), "f.py")
    assert "1 of 12" in hint, f"expected exactly one differing line: {hint}"
    assert "offset 25" in hint, (
        f"expected the true UTF-8 byte offset (25 -- one more than the "
        f"24-character prefix, because the accented e costs 2 bytes): "
        f"{hint}")
    assert "offset 24" not in hint, (
        f"reported a CHARACTER offset where a byte offset was promised: "
        f"{hint}")
