"""A filename cannot forge a `[rolled back]` or `[result]` line (#1019).

Two renders interpolated a path raw into a line the *tool* owns:

* `--- {arg} ---`, the op header `_dispatch_impl` prints above every answer, and
* `edited {path} (line N)`, `op_edit`'s success receipt.

A path is not always typed by the operator — `validate:` walks a tree, a batch
payload carries one, and a repository that accepts contributed fixtures names
files the operator never chose. `str.splitlines()` breaks on ten separators
(#886) and U+2028 is one of them, so a file named

    a<U+2028>[rolled back] forged.txt

put two forged marker lines at column 0 *above* any genuine one. A reader — or
the column-0-anchored grep the receipt is written to be read with — sees a
rollback that did not happen, attached to a file that was in fact edited.

`_flat_cell` — the one implementation of "this value occupies exactly one
line", #895 — already existed. These two call sites had never adopted it,
which is this tracker's most common shape.

The bar, the same one `tests/test_forged_branch_line_965.py` holds:

* the forged text may not be its own rendered line, asserted against
  `splitlines()` because that is what a consumer counts with — not "flat was
  called", which a site can do and still print the raw value; and
* the name must still be readable in full. Disclosed, never stripped.
"""
from __future__ import annotations

from pathlib import Path

import _supertool

#: One of the ten separators `str.splitlines()` breaks on, and not one
#: `_untrusted.split_lines` cuts on — so it survives inside a "line" and is the
#: vector #1470 and #886 were both about.
SEP = chr(0x2028)

FORGED_ROLLBACK = "[rolled back] forged.txt regressed; file restored"
FORGED_RESULT = "[result] 1 op run, 0 writes"

#: The two markers a consumer anchors at column 0. Asserting on the *prefix*
#: rather than on whole-line equality is deliberate: the render appends its own
#: tail (` ---`, ` (line 1)`) to whatever the name ended with, so an
#: equality check passes on a forgery the anchored grep still reports.
MARKERS = ("[rolled back]", "[result]")


def assert_no_forged_marker(out: str, *, skip_first: bool = False) -> None:
    rendered = out.splitlines()
    body = rendered[1:] if skip_first else rendered
    for line in body:
        for marker in MARKERS:
            assert not line.startswith(marker), (
                f"a path forged a column-0 {marker} line:" + chr(10)
                + chr(10).join(f"  {i:>3} | {ln}"
                               for i, ln in enumerate(rendered, 1))
            )


def assert_nothing_censored(out: str, *fragments: str) -> None:
    for fragment in fragments:
        assert fragment in out, f"{fragment!r} was removed from the render"


def test_op_header_cannot_be_forged_by_a_path(tmp_path: Path) -> None:
    """`--- {arg} ---` is the tool talking; an argument may not add a line."""
    hostile = tmp_path / f"a{SEP}{FORGED_RESULT}"
    out = _supertool.dispatch(f"read:{hostile}")
    assert_no_forged_marker(out)
    assert_nothing_censored(out, "1 op run, 0 writes")


def test_edit_receipt_cannot_be_forged_by_a_path(tmp_path: Path) -> None:
    """`edited {path} (line N)` claims a write; a name may not claim an undo."""
    hostile = tmp_path / f"a{SEP}{FORGED_ROLLBACK}"
    hostile.write_text("alpha" + chr(10), encoding="utf-8")

    out = _supertool.op_edit("alpha", "beta", str(hostile))

    assert not out.startswith("ERROR"), out
    assert_no_forged_marker(out)
    assert_nothing_censored(out, "forged.txt regressed")
    assert hostile.read_text(encoding="utf-8") == "beta" + chr(10)


def test_the_edit_receipt_still_names_the_line_it_wrote(tmp_path: Path) -> None:
    """The flattening may not cost the receipt its own meaning."""
    plain = tmp_path / "plain.txt"
    plain.write_text("alpha" + chr(10), encoding="utf-8")
    out = _supertool.op_edit("alpha", "beta", str(plain))
    assert out.splitlines()[0] == f"edited {plain} (line 1)"
