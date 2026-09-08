"""A `mode: block` refusal must leave the agent able to retry (#2421).

Three agents wedged for hours in one night after `supertool-no-cut.md`
refused a piped call: a developer four hours, a releaser two, another
developer three. Each was `running` per `ListAgents` the whole time and
never sent another tool call. `agents/developer.md`/`dispatch.md`'s own
diagnosis bar for telling that apart from a dead context is the oss
plugin's problem, out of this repository's blast radius (#2421's own
scope note) - but the text a wedged agent actually reads, the rule body
itself, is this repository's own file, and it is what this fixes.

The prose used to explain the *policy* (narrow the op, do not pipe) at
length without ever telling a blocked caller, in so many words, that the
refusal is recoverable and what to do about it right now. This pins that
an explicit, imperative retry instruction is present, and that it sits
near the top of what a caller reads rather than after several paragraphs
of history and regex mechanics only the rule's own maintainers need.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RULE = REPO / ".claude" / "jit-context" / "tools" / "00-manual" / "supertool-no-cut.md"


def _body_after_frontmatter(text: str) -> str:
    lines = text.splitlines()
    assert lines and lines[0].strip() == "---", "no frontmatter fence"
    for offset in range(1, len(lines)):
        if lines[offset].strip() == "---":
            return chr(10).join(lines[offset + 1:]).strip()
    raise AssertionError("frontmatter never closed")


def test_the_body_tells_a_blocked_caller_the_call_is_recoverable():
    """Would this pass if the code did nothing?

    No: at the parent commit the body opens with "Do not cut a supertool
    op's output" and a policy explanation, with no sentence anywhere
    telling the caller the refusal is not terminal or naming the concrete
    next action (resend without the pipe).
    """
    body = _body_after_frontmatter(RULE.read_text(encoding="utf-8"))
    lowered = body.lower()
    assert "resend" in lowered or "retry" in lowered, (
        "the rule body never tells a blocked caller it may try again: "
        + body[:400])
    assert "not a dead end" in lowered or "not stuck" in lowered, (
        "the rule body never says the refusal is recoverable in words a "
        "wedged caller would act on: " + body[:400])


def test_the_retry_instruction_is_the_first_thing_the_body_says():
    """Position matters as much as presence.

    A caller that stopped taking further tool calls read *something* in
    this text and did not treat it as actionable - burying the recoverable
    instruction after paragraphs of history and regex detail is exactly
    the shape #2421 reports. The instruction must be in the first
    paragraph, not merely present somewhere in 3KB of prose.
    """
    body = _body_after_frontmatter(RULE.read_text(encoding="utf-8"))
    first_paragraph = body.split(chr(10) + chr(10), 1)[0].lower()
    assert "resend" in first_paragraph or "retry" in first_paragraph, (
        "the recoverable instruction is not in the first paragraph: "
        + first_paragraph)


def test_the_addition_still_fits_the_per_match_budget():
    """The ceiling `test_jit_rule_body_budget_1433.py` already enforces,
    restated here so a reader of this file sees the constraint the fix had
    to work inside rather than assuming budget was free.
    """
    raw = RULE.read_bytes().replace(b"\r\n", b"\n")
    assert len(raw) <= 3200, (
        "the retry instruction pushed this rule over its per-match "
        "budget: " + str(len(raw)) + " bytes")
