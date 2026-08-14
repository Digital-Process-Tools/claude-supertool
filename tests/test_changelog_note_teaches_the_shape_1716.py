"""The pre-write note for changelog.d/ must teach both rules, and its example must obey them (#1716).

#1716 was filed as "the fragment shape is learnable only by tripping its
validator twice". Measured on this tree, that premise is false: a fragment that
violates both body rules at once gets **both** findings from one run —
`changelog-fragment.py` deliberately does not return after
`self_reference_finding`, and both messages already carry their own remedy.

What is true is one step earlier. The thing that fires *before* the write is the
jit-context note `.claude/jit-context/paths/00-manual/changelog-d.md`, injected
on any call naming that directory, and until this issue it:

1. never mentioned the self-reference rule (#1251) at all — one of the exact two
   rules the author is said to learn by tripping; and
2. carried a worked example that, copied verbatim, **fails** that rule, because
   it names no issue.

A note whose example is refused by the validator the note is about is worse than
no example: it is the absence-read-as-guidance shape, and an author following it
lands in precisely the two-attempt loop #1716 describes.

So the guard is not a string check on prose. Every markdown example in that note
is run through the project's own fragment rules — the same two functions the
write-time validator calls — and has to pass. Would this suite pass if the code
did nothing? No: on master the example names no issue and
`self_reference_finding` returns a finding for it.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / ".github" / "scripts" / "assemble_changelog.py"
NOTE = REPO / ".claude" / "jit-context" / "paths" / "00-manual" / "changelog-d.md"

_spec = importlib.util.spec_from_file_location("assemble_changelog_1716", SCRIPT)
assert _spec is not None and _spec.loader is not None
asm = importlib.util.module_from_spec(_spec)
sys.modules["assemble_changelog_1716"] = asm
_spec.loader.exec_module(asm)

#: How an entry may name its own issue, as `self_reference_finding` accepts it.
_CITES = re.compile(r"(?:\(#|/(?:issues|pull)/)([0-9]+)")

FENCE = "```"


def _md_examples(text):
    """Every top-level md fence in the note: (opening line number, body).

    A block ends at the first fence sitting at column 0. An indented closer
    belongs to a fence *inside* the example — which the note's own advice is
    largely about — so treating it as the end would truncate the example
    exactly where it is most load-bearing.
    """
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        if lines[i].rstrip() == FENCE + "md":
            body = []
            j = i + 1
            while j < len(lines) and lines[j].rstrip() != FENCE:
                body.append(lines[j])
                j += 1
            out.append((i + 1, "\n".join(body) + "\n"))
            i = j + 1
            continue
        i += 1
    return out


EXAMPLES = _md_examples(NOTE.read_text(encoding="utf-8"))


def test_the_note_carries_at_least_one_worked_example():
    """No example is the failure mode one step further back."""
    assert EXAMPLES, "{0} has no md fence — nothing for an author to copy".format(NOTE)


@pytest.mark.parametrize("at,body", EXAMPLES, ids=[str(at) for at, _b in EXAMPLES])
def test_every_example_names_an_issue(at, body):
    assert _CITES.search(body), (
        "the example at {0}:{1} names no issue, so copying it produces a "
        "fragment the write-time validator refuses (#1251)".format(NOTE.name, at))


@pytest.mark.parametrize("at,body", EXAMPLES, ids=[str(at) for at, _b in EXAMPLES])
def test_every_example_passes_the_rules_the_note_is_about(at, body):
    pytest.importorskip(
        "markdown_it",
        reason="scan_fragment_body raises CannotValidate without the parser")
    match = _CITES.search(body)
    assert match, "covered by test_every_example_names_an_issue"
    name = "{0}.fixed.md".format(match.group(1))

    assert asm.self_reference_finding(name, body) is None
    assert asm.scan_fragment_body(name, body) == []


def test_the_note_states_the_self_reference_rule():
    """The rule an author cannot infer from the shape of a bullet.

    A leading `- ` at column 0 is visible in any example. "the entry must name
    its own issue" is not visible in anything, which is why its absence from
    the note was the load-bearing half of #1716.
    """
    text = NOTE.read_text(encoding="utf-8")
    assert "#1251" in text, "the note never cites the self-reference rule"
