"""#1671 — the raw-command guard is one route wide, and said so nowhere.

`hooks/hooks.json` registers the PreToolUse guard with `"matcher":
"Bash|PowerShell"`. Claude Code's own `Write`/`Edit` are not Bash, so they
never reach the guard: the same one-key change to the same file is denied
through a heredoc and unremarkable through `Edit`, with no op, no post-edit
validator and no rollback-on-syntax-failure.

That is this repository's own defect class arriving in the thing that enforces
boundaries — an absence produced by the tool read as an absence in the world.
The refusal reads as "this file is protected" when what is true is "this route
is protected", and the SessionStart roster reads as a complete account of how
files get touched.

Three disclosures are pinned here, plus one claim the code does not support:

* the roster legend, which every session pays for, states the scope;
* the refusal trailer, which already names the op and the off switch, names
  the route too;
* the plugin manifest may not advertise an enforcement mode that blocks
  competing tools unless a shipped hook matcher actually covers one;
* the README's own hard-block recipe must name the write tools it claims to
  block.

Not pinned, because it cannot be built from a Bash-only hook: making the
mismatch visible at the moment `Edit` writes. Nothing in the plugin observes
that call.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import supertool


_ROOT = Path(__file__).resolve().parent.parent

#: The SessionStart budget `hooks/session-start.sh` is written against.
_SESSION_CAP = 7168


@pytest.fixture
def shipped_config(monkeypatch: pytest.MonkeyPatch):
    """The repo's own config — conftest hands tests `_CONFIG = {}`."""
    cfg = json.loads((_ROOT / ".supertool.json").read_text(encoding="utf-8"))
    supertool._merge_presets(cfg, str(_ROOT))
    monkeypatch.setattr(supertool, "_CONFIG", cfg)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG_PATH",
                        str(_ROOT / ".supertool.json"))
    return cfg


# --- the SessionStart roster states the scope ------------------------------

def test_roster_legend_says_the_guard_is_bash_only(shipped_config) -> None:
    """The listing is the only account of file routes a session is given.

    Asserted on the rendered roster rather than on `_ROSTER_LEGEND`, because
    what a session pays for is the bytes `ops:roster` emits.
    """
    out = supertool.op_ops_roster()
    prose = " ".join(
        line for line in out.splitlines() if not line.startswith("  "))
    assert "Bash" in prose, (
        "the roster never names the tool the guard inspects, so a reader "
        "cannot tell which route it governs")
    assert re.search(r"\bEdit\b|\bWrite\b", prose), (
        "the roster never names the harness write tools, so it reads as a "
        "complete account of how files get touched")


def test_session_start_payload_still_fits_its_cap(shipped_config) -> None:
    """A bound on the disclosure above, not a pin on it.

    `hooks/session-start.sh` emits `introduction`, `output-format` and
    `ops:roster` into a ~7KB budget. #1231 exists because that budget was
    already blown once and the truncation hid whole op families.
    """
    payload = (supertool.op_introduction()
               + supertool.op_output_format()
               + supertool.op_ops_roster())
    assert len(payload.encode("utf-8")) < _SESSION_CAP, (
        "the SessionStart injection no longer fits; the roster's own "
        "legend warns that the descriptive listing stops fitting first")


# --- the refusal names the route, not the file -----------------------------

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


def test_refusal_discloses_that_it_governs_one_route() -> None:
    """"This file is protected" is the wrong reading and the guard invited it.

    The trailer already names the op, where the op lives and the off switch.
    What it never said is that the denial is about *how* the write was
    attempted.

    **It says that without naming a tool since #1706.** This test asserted
    `Edit|Write` in the refusal until then, which made the scope disclosure
    and a spelled-out route past the gate the same assertion — and the second
    one is a remedy that loses the validator chain and the rollback. The
    scope claim is what #1671 was about and is what is pinned here; the tool
    names are pinned on the roster (above), in `docs/configuration.md` and in
    the README recipe (below), which is where a reader is deciding rather
    than being denied. `tests/test_guard_refusal_names_no_bypass_1706.py`
    holds the negative.
    """
    text = _refusal()
    assert "Bash" in text, (
        "a refusal that never names the route it governs lets the reader "
        "conclude the path itself is protected")
    assert "route, not the path" in text, text


def test_refusal_adds_no_line_shape_the_tool_did_not_write() -> None:
    """#1391's property: every line of a refusal has a shape the tool wrote.

    The disclosure is appended to the existing trailer sentence rather than
    added as a new line, so this list stays the whole vocabulary.
    """
    prefixes = ("`", "  ", "Only invocations", "The description", "and ",
                "An op named above", "")
    for line in _refusal().splitlines():
        assert line.startswith(prefixes), line


# --- a claim the shipped hooks do not support ------------------------------

def _plugin_description() -> str:
    manifest = json.loads(
        (_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    return str(manifest.get("description", ""))


def _pretooluse_matchers() -> list:
    hooks = json.loads(
        (_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    return [entry.get("matcher", "")
            for entry in hooks.get("hooks", {}).get("PreToolUse", [])]


def test_manifest_claims_no_tool_block_no_shipped_hook_delivers() -> None:
    """Derived from `hooks.json`, not from the sentence.

    A description asserting the plugin blocks competing tools is a claim
    about a hook matcher. If no shipped matcher names a write tool, the
    marketplace entry advertises a mode that does not exist — and an operator
    who read it believes a door is closed that nobody closed.
    """
    description = _plugin_description()
    claims_tool_block = bool(
        re.search(r"blocks? (competing|other) tools?", description))
    if not claims_tool_block:
        return
    delivered = any(re.search(r"\bEdit\b|\bWrite\b", matcher)
                    for matcher in _pretooluse_matchers())
    assert delivered, (
        "plugin.json advertises an enforcement mode that blocks competing "
        "tools; no shipped PreToolUse matcher covers Edit or Write, so "
        "nothing in the plugin implements it: " + description)


# --- the README recipe covers the tools it claims to ------------------------

def test_canonical_guard_doc_discloses_the_bash_only_scope() -> None:
    """The section a reader consults first was the last to say it.

    `docs/configuration.md` is where `raw_command_guard` is specified — what
    turns it off, what it never blocked, how a repository wires it. It
    reasoned carefully about *which commands* are covered and never once
    about *which tools*, which is the axis #1671 is on.
    """
    doc = (_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    start = doc.index("## `raw_command_guard`")
    end = doc.index("\n## ", start + 4)
    section = doc[start:end]
    assert re.search(r"\bEdit\b", section), (
        "the canonical guard section never names the harness write tools, "
        "so the one surface a reader reaches first still implies the gate "
        "covers every route to disk")
    assert re.search(r"\bWrite\b", section)


def test_readme_hard_block_recipe_names_the_write_tools() -> None:
    """The recipe is what an operator reaches for after reading the manifest.

    It denied `Grep`, `Glob`, `LS` and a list of read-ish Bash commands and
    left `Edit`/`Write` out — so following it exactly leaves open the one
    route #1671 was filed about.
    """
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("Hard-block native tools")
    section = readme[start:readme.index("###", start + 10)]
    for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        assert re.search(r'"%s"|,%s\b' % (tool, tool), section), (
            "the hard-block recipe omits " + tool)
