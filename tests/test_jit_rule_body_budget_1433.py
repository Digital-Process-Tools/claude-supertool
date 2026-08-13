"""A `tools/` jit rule body is a per-match cost, so it has a ceiling (#1433).

A `paths/` rule is injected one time per session -- not because it says `once`,
which no reader consumes, but because `pre-path-hook.sh:347` dedups every paths
rule before the pattern is tried (#1442). A `tools/` rule has no such dedup: its
whole body is injected on **every** match, including every false one.

On 2026-08-11 the no-cut rule produced four wrong
blocks in one evening across three callers, and one agent reported the injected
text as the largest single input cost of its run (#1433) — 3,884 bytes, where
the next largest tools rule was 2,861.

So the ceiling enforces a rule the notes already state and nothing checked:
`.claude/jit-context/paths/00-manual/jit-context.md` says "Keep it short ...
length is a cost paid forever." That sentence had no checker, and the file it
was written about grew to 61 lines across three incident write-ups. (It said
"injected in full on every match" until #1442, which is true of a `tools/` body
and not of the `paths/` body that sentence sits in.)

Would this pass if the code did nothing? No: supertool-no-cut.md is 3,884 bytes
at the parent commit, over the 3,200-byte ceiling, and the content assertions
below fail on a body shrunk by deleting the replacement-op table instead of the
history.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".claude" / "jit-context" / "tools" / "00-manual"
INDEX = TOOLS / "00-index.tsv"
TAB = "\t"

# Headroom of ~340 bytes over the largest compliant rule at the time of writing
# (op-defaults-that-narrow.md, 2,861). ~800 tokens, re-paid on every match.
BUDGET = 3200


def _indexed_rule_files():
    """Every rule file named by a live row of the tools index."""
    rows = INDEX.read_text(encoding="utf-8").splitlines()
    out = []
    for raw in rows:
        fields = raw.split(TAB)
        if len(fields) >= 3 and fields[2].endswith(".md"):
            out.append(TOOLS / fields[2])
    return out


PATHS = REPO / ".claude" / "jit-context" / "paths" / "00-manual"


# A paths body is paid once per session, not once per match, so it gets its own
# ceiling. Same construction as BUDGET: ~150 bytes over the largest compliant
# rule at the time of writing (docs-index.md, 5,847 -- the section map for a
# 124KB file). A ratchet on growth, which is the only honest number available:
# nothing has measured what a per-session body costs an agent.
PATHS_BUDGET = 6000


def _indexed_paths_rules():
    """Every `paths/` rule named by a live index row.

    Selected by the index, NOT by frontmatter. This function used to keep only
    the rules whose `mode:` lacked `once` and call them "per-match" -- but a
    paths row is `pattern<TAB>file` and `build_path_tsv` never reads `mode:`,
    so the filter was keyed on a field with no reader (#1442). It happened to
    budget jit-context.md at 3,023 while exempting docs-index.md at 5,847,
    which is an arbitrary line, not a lenient one.
    """
    out = []
    for raw in (PATHS / "00-index.tsv").read_text(encoding="utf-8").splitlines():
        fields = raw.split(TAB)
        if len(fields) < 2 or not fields[1].endswith(".md"):
            continue
        path = PATHS / fields[1]
        if path.is_file() and path not in out:
            out.append(path)
    return out


def test_index_names_rules_at_all():
    """Guard the guard: an empty list would make every assertion below vacuous."""
    files = _indexed_rule_files()
    assert len(files) >= 4, f"tools index named {len(files)} rule files"
    for path in files:
        assert path.is_file(), f"index names {path.name}, which is not on disk"


def test_paths_rules_are_named_at_all():
    """Vacuity guard: eleven were indexed when this was written."""
    names = {p.name for p in _indexed_paths_rules()}
    assert "jit-context.md" in names and "docs-index.md" in names, (
        f"paths index named {sorted(names)} - if a rule was retired that is a "
        "real change, not a broken test")


def test_no_tools_rule_body_exceeds_the_per_match_budget():
    over = {
        p.name: p.stat().st_size
        for p in _indexed_rule_files()
        if p.stat().st_size > BUDGET
    }
    assert not over, (
        f"tools rule bodies over the {BUDGET}-byte per-match budget: {over}. "
        "These are injected in full on every match, false ones included. "
        "Cut the incident history; keep the table of replacement ops.")


def test_no_paths_rule_body_exceeds_the_per_session_budget():
    over = {
        p.name: p.stat().st_size
        for p in _indexed_paths_rules()
        if p.stat().st_size > PATHS_BUDGET
    }
    assert not over, (
        f"paths rule bodies over the {PATHS_BUDGET}-byte per-session budget: "
        f"{over}. Paid once per session per agent, so the ceiling is looser "
        "than the tools one -- but it is a ratchet, and growing past it wants "
        "a measurement, not a bigger number.")


def test_the_no_cut_rule_keeps_the_part_that_helps():
    """Shrinking must not remove the replacement ops, which are the whole yield."""
    body = (TOOLS / "supertool-no-cut.md").read_text(encoding="utf-8")
    for token in ("gh-pr:1208:status", "grep:PAT:PATH:10:2",
                  "read:PATH:::grep=", "gh-job:N:fail"):
        assert token in body, f"no-cut rule no longer offers {token}"


def test_the_no_cut_rule_still_says_it_is_not_precise():
    """#1430's disclosure of the two out-of-anchor shapes stays (#1433 option 2)."""
    # Collapsed: these phrases are prose and a reflow must not silently
    # unpin them, which the first draft of this test did on a line wrap.
    body = " ".join((TOOLS / "supertool-no-cut.md").read_text(
        encoding="utf-8").split())
    assert "env-var prefix" in body
    assert "command substitution" in body
    assert "heredoc" in body
