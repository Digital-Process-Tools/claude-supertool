"""1675 (instance 1) — `docs/contributing.md`'s reserved-key list and
`_OP_CONFIG_RESERVED_KEYS`, the set that actually decides what leaks to a
custom op's subprocess as `SUPERTOOL_<KEY>`, had drifted: `form` (and its
sibling `hint`) are real config keys — `#1245`'s `builtin-ops` forms declare
`form`, `ops:roster` and `help:` both read `hint` (see `_supertool.py` around
`.get("hint")`) — and neither was in the reserved list on either surface.

Inert today only because `builtin-ops` entries never spawn a subprocess. The
moment somebody copies a `form` or `hint` key onto an **`ops`** entry (a
preset op, a project op), the launcher forwards it as
`SUPERTOOL_FORM` / `SUPERTOOL_HINT` — config metadata reaching a script's
environment as if it mattered to the script. This file pins two things: the
doc and the code do not disagree, and the two builtin-ops-only keys are
covered.
"""
from __future__ import annotations

import re
from pathlib import Path

import supertool

REPO_ROOT = Path(__file__).parent.parent


def _documented_reserved_keys() -> set:
    text = (REPO_ROOT / "docs/contributing.md").read_text(encoding="utf-8")
    m = re.search(r"Any key in an op config that isn't a reserved key \(([^)]+)\)",
                  text)
    assert m, "reserved-key sentence not found in docs/contributing.md"
    return {tok.strip(" `") for tok in m.group(1).split(",")}


def test_docs_and_code_name_the_same_reserved_keys() -> None:
    documented = _documented_reserved_keys()
    coded = set(supertool._OP_CONFIG_RESERVED_KEYS)
    assert documented == coded, (
        f"docs/contributing.md's reserved-key list and "
        f"_OP_CONFIG_RESERVED_KEYS disagree — "
        f"docs only: {documented - coded}, code only: {coded - documented}")


def test_form_and_hint_are_reserved() -> None:
    """The two keys #1245's `builtin-ops` forms actually use, both still
    missing until this fix — an `ops`-section entry declaring either would
    otherwise leak it to the subprocess environment."""
    assert "form" in supertool._OP_CONFIG_RESERVED_KEYS
    assert "hint" in supertool._OP_CONFIG_RESERVED_KEYS
