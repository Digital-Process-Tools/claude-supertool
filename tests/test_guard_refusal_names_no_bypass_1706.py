"""#1706 — every refusal closed by naming a working way past itself.

The trailer #1671 added ended:

    It hooks Bash only: a harness Edit/Write reaches this same path with no
    op, no validator and no rollback, so this refusal is about the route, not
    the path (#1671).

The first half of that clause is a route past the gate, written into the one
sentence a blocked agent is guaranteed to read, and an agent that takes it
loses the validator chain and the write rollback the refusal exists to route
it into. Inert in this repository — `harness-tools-blocked` denies those tools
here — and live for every plugin user, which is who the plugin ships to.

**Not a revert.** What #1671 was right about is kept: the denial is about the
route and not the path, and a route the guard cannot see gets no op, no
validator and no rollback. What is removed is the tool name, i.e. the part
that turns a disclosure into an instruction. The disclosure that *does* need
the tool names — an operator deciding what to put in a deny list — is
documentation, and stays in `docs/configuration.md` and the SessionStart
roster, both asserted here so the split is a fact rather than a claim.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent

#: Harness write tools. Matched on a word boundary rather than bare, or the
#: trailer's own prose could never mention writing at all.
_WRITE_TOOLS = re.compile(r"\b(Edit|Write|MultiEdit|NotebookEdit)\b")


def _refusal(command: str = "git commit -m x") -> str:
    verdict = supertool.GuardVerdict(
        state="blocked",
        matches=(supertool.GuardMatch(
            op="git-commit",
            use="git-commit:::MESSAGE:::PATHS",
            description="Commit MESSAGE (stages PATHS).",
            argv="git commit",
            command=command,
        ),),
        notes=(),
    )
    return supertool.guard_refusal(verdict)


# --- the refusal names no route past itself --------------------------------

def test_the_refusal_names_no_harness_write_tool(shipped_config):
    text = _refusal()
    # Must-fire control first: an empty or stubbed refusal would satisfy the
    # negative below on silence alone.
    assert "git-commit" in text, text
    assert "raw_command_guard: false" in text, text
    assert not _WRITE_TOOLS.search(text), (
        "the refusal still names a harness write tool, which is a working "
        "route past the gate offered in the sentence that denies: " + text)


def test_the_refusal_still_says_the_gate_is_one_route_wide(shipped_config):
    """#1671's actual finding, kept.

    "This file is protected" stays the wrong reading, and the trailer still
    says so — it just no longer says which tool to reach for instead.
    """
    text = _refusal()
    assert "Bash" in text, text
    assert "route" in text and "not the path" in text, text


def test_the_refusal_still_says_what_an_unguarded_route_loses(shipped_config):
    """The deterrent half is the reason the sentence is worth keeping."""
    text = _refusal()
    assert "no op, no validator and no rollback" in text, text


def test_every_line_of_the_refusal_still_has_a_shape_the_tool_wrote():
    """#1391's property, re-asserted because the trailer moved."""
    prefixes = ("`", "  ", "Only invocations", "The description", "and ",
                "An op named above", "")
    for line in _refusal().splitlines():
        assert line.startswith(prefixes), line


# --- the tool names moved to the surfaces that want them, not away ----------

def test_the_session_roster_still_names_the_harness_write_tools(
        shipped_config):
    """A once-per-session legend is a disclosure; a denial is an instruction.

    That is the whole split, so it is pinned rather than left implied.
    """
    prose = " ".join(line for line in supertool.op_ops_roster().splitlines()
                     if not line.startswith("  "))
    assert _WRITE_TOOLS.search(prose), prose


def test_the_configuration_doc_still_names_the_harness_write_tools():
    doc = (_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    start = doc.index("## `raw_command_guard`")
    section = doc[start:doc.index(chr(10) + "## ", start + 4)]
    assert _WRITE_TOOLS.search(section), (
        "the tool names left the refusal and did not land in the section a "
        "reader consults to write a deny list")
