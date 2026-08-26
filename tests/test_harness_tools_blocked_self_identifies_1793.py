"""A subagent read harness-tools-blocked.md as a prompt-injection payload (#1793).

Twice, in two separate reviews spawned against a developer agent's own diff, a
stock `Explore` reviewer with no supertool briefing received this file's text
through its blocked `Read` call and reported it as "a fabricated JIT Context
block ... a prompt-injection payload (planted content masquerading as a system
directive)". It refused to comply and fell back to `git show`.

Given only what that reader could see -- unsolicited text inside a tool result,
asserting its own tools are unavailable, naming a differently-named executable
to use instead -- refusing was the textbook-correct response to a textbook
tool-result injection. The rule applied the right suspicion to the wrong
object, and had no way to tell which object it had.

The fix is not to soften the rule -- #1793's own brief: "Do not weaken what the
rule does" -- and not to assert first-party origin in prose, which an
injection can write just as cheaply. It is to give the reader something it
can check WITHOUT trusting this text at all: the reviewer that hit this had
`Bash` in its own tool grant (as `Explore` does, and as it demonstrated by
falling back to `git show`), so a verification command using a channel the
injected text cannot forge -- git's own history -- is checkable independently
of whether this body is believed.

This does not cover a reader with no `Bash`/git access, and it does not cover
the scaffolded per-repo copy at `.claude/jit-context/tools/01-oss/
supertool-required.md` that #1793's own comment reports as a second instance
-- that copy is written by the `oss` plugin into OTHER repositories and is not
tracked here (CLAUDE.md, `tests/test_harness_tools_do_not_ship_1791.py`), so
this repository cannot edit it. Both gaps are named in the pull request rather
than closed here.

Would this pass if the rule body did nothing? No: the assertions below name a
verification command and a stated non-injection claim that were not in the
body before this change, and `test_the_body_still_fits_the_budget` would still
fail to protect against a fix that ships by inflating the rule past #1433's
ceiling.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RULE = REPO / ".claude" / "jit-context" / "tools" / "00-manual" / "harness-tools-blocked.md"
BUDGET = 3200  # tests/test_jit_rule_body_budget_1433.py's ceiling for this layer


def _body():
    return RULE.read_text(encoding="utf-8")


def test_the_rule_names_a_verification_command_the_text_itself_cannot_forge():
    """The reader must be pointed at a channel outside this text -- git's own
    history -- rather than asked to trust a self-declaration of origin."""
    body = _body()
    assert "git log" in body or "git show" in body, (
        "the rule body gives the reader no independently-checkable command; "
        "an assertion of first-party origin inside the same text an injection "
        "would also control proves nothing")
    assert ".claude/jit-context/tools/00-manual/harness-tools-blocked.md" in body, (
        "the verification command must name this file's own tracked path, "
        "so the reader can run it against the exact bytes in front of them")


def test_the_rule_states_plainly_that_it_is_not_an_injection():
    """A reader needs the hypothesis named to know what it is checking against."""
    body = _body().lower()
    assert "injection" in body, (
        "the body never names the failure mode it exists to head off, so a "
        "reader has no anchor for what the verification command is answering")


def test_the_body_still_fits_the_budget():
    """#1433: injected in full on every match, false ones included."""
    size = len(RULE.read_bytes().replace(b"\r\n", b"\n"))
    assert size <= BUDGET, (
        f"harness-tools-blocked.md is {size} bytes, over the {BUDGET}-byte "
        "per-match ceiling -- the self-identification fix must not buy "
        "correctness by inflating a cost paid on every match, true ones too")
