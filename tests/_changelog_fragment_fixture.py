"""Fixture fragments that satisfy the self-reference rule (#1251).

Six suites build a `changelog.d/` fixture the same way: a `_repo` helper that
writes `{"936.fixed.md": body}` where `body` is whatever CommonMark shape the
test is about. None of those bodies name the issue in their own filename,
because until #1251 nothing required it.

#1251 does require it — the number is in the filename and the release deletes
the filename, so an entry that never says `#N` ships unfindable, which 8 of 20
entries in v0.32.0 and 6 of 28 in v0.33.0 did. Rather than editing thirty-odd
bodies that are about fences, tables and block quotes and have nothing to say
about issue numbers, the fixture writer adds what an author would have written.

**It asks the assembler, it does not re-implement the rule.** A second opinion
about what counts as a self-reference is how the fixture and the rule drift,
and a drifted fixture is a suite that goes green against a rule nobody ships.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / ".github" / "scripts" / "assemble_changelog.py"

_spec = importlib.util.spec_from_file_location("_st_fragment_fixture_asm", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_asm = importlib.util.module_from_spec(_spec)
sys.modules["_st_fragment_fixture_asm"] = _asm
_spec.loader.exec_module(_asm)


def with_self_reference(name: str, body: str) -> str:
    """`body`, plus `(#N)` as a continuation paragraph if it names no `#N`.

    A trailing paragraph at the bullet's own indent, so nothing above it moves:
    line numbers in a shape finding stay where the test expects them, and
    `_entry_count` — which counts lines beginning `- ` — is unchanged.

    A name that does not parse is left alone: it has no issue number to be
    missing, and the suites that pass one are testing exactly that refusal.
    """
    if _asm.self_reference_finding(name, body) is None:
        return body
    number = _asm.parse_fragment_name(name).issue
    return body + "\n  (#{0})\n".format(number)
