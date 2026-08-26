r"""#1774's ratchet is right; its stated reason was a release out of date (#1813).

The module docstring of `test_description_is_not_a_changelog_1774.py` used to
justify the byte budget by claiming `description` is "rendered whole by `ops`,
so its length is a per-session tax." Since #1775/#1778 that is false: `ops`
(bare, no argument) is signatures only, and the descriptive render moved to
`ops:full`. `help:OP` was never affected -- `op_help()` has always appended
`description` verbatim, with no `compact`/`full` distinction of its own.

The ratchet in #1774 stays; only the reason a maintainer reads when the test
goes red was wrong. This file pins the corrected claim against the actual
render, rather than trusting a hand-check of the prose:

* `ops` must stay far smaller than `ops:full` -- the gap IS the claim.
* A real op's description must be absent from `ops` and present in both
  `ops:full` and `help:OP`.
* The docstring text itself must no longer say `ops` renders `description`
  whole, and must name the two renders that actually do.

Would this pass if the code did nothing? No -- at the parent commit the
docstring literally reads "rendered whole by `ops`, so its length is a
per-session tax", which is the exact string
`test_docstring_no_longer_claims_bare_ops_renders_description` refuses.
"""
from __future__ import annotations

import re
from pathlib import Path

import supertool

REPO_ROOT = Path(__file__).parent.parent
RATCHET_FILE = REPO_ROOT / "tests" / "test_description_is_not_a_changelog_1774.py"

# An op known to carry a description comfortably over MAX_DESCRIPTION, so its
# text is unmistakable in a render that includes it and absent from one that
# does not -- picked from the #1774 ledger itself rather than assumed.
_KNOWN_LONG_OP = "channel"


def _description_for(op_name: str) -> str:
    """The raw `description` string .supertool.json declares for `op_name`."""
    import json

    for path in sorted((REPO_ROOT / "presets").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = (data.get("ops") or {}).get(op_name)
        if isinstance(entry, dict) and entry.get("description"):
            return entry["description"]
    root = json.loads((REPO_ROOT / ".supertool.json").read_text(encoding="utf-8"))
    entry = (root.get("builtin-ops") or {}).get(op_name)
    if isinstance(entry, dict) and entry.get("description"):
        return entry["description"]
    raise AssertionError(f"{op_name!r} carries no description in this tree")


def test_bare_ops_is_dramatically_smaller_than_the_descriptive_render(
        shipped_config) -> None:
    """The empirical fact the corrected docstring must be consistent with."""
    bare = len(supertool.op_ops().encode("utf-8"))
    full = len(supertool.op_ops(full=True).encode("utf-8"))
    assert full > bare * 4, (
        f"ops={bare} bytes, ops:full={full} bytes -- if these are close, "
        f"`ops` is no longer signatures-only and the #1813 fix is itself stale"
    )


def test_a_real_description_is_absent_from_bare_ops_and_present_elsewhere(
        shipped_config) -> None:
    desc = _description_for(_KNOWN_LONG_OP)
    bare = supertool.op_ops()
    full = supertool.op_ops(full=True)
    helped = supertool.op_help(_KNOWN_LONG_OP)
    assert desc not in bare, (
        f"{_KNOWN_LONG_OP!r}'s description appears in bare `ops` -- "
        f"`description` is being rendered whole by `ops` again"
    )
    assert desc in full, f"{_KNOWN_LONG_OP!r}'s description missing from `ops:full`"
    assert desc in helped, f"{_KNOWN_LONG_OP!r}'s description missing from `help:OP`"


def test_docstring_no_longer_claims_bare_ops_renders_description() -> None:
    """The exact stale sentence, refused by string rather than by feel."""
    text = RATCHET_FILE.read_text(encoding="utf-8")
    module_doc = text.split(chr(34) * 3, 2)[1]
    assert not re.search(r"rendered whole by `ops`", module_doc), (
        "the ratchet's docstring still claims bare `ops` renders `description` "
        "whole -- false since #1775/#1778 (#1813)"
    )
    assert "ops:full" in module_doc, (
        "the docstring should name `ops:full` as the render that actually "
        "carries `description` whole"
    )
    assert "help:OP" in module_doc or "help:" in module_doc, (
        "the docstring should name `help:OP` as the other render carrying "
        "`description` whole -- op_help() has no long/short distinction of "
        "its own, which is the second half of #1813"
    )
