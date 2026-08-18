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

Both ceilings count the body with line endings NORMALISED (#1799), so the
number is a property of the content and not of the checkout that holds it.

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


def _body_bytes(path):
    """The size of a rule body as CONTENT, not as bytes on disk (#1799).

    CRLF collapses to LF before the count, so the number is the same on every
    checkout. The reasoning, and what this number does and does not claim, is
    the block above the #1799 tests below. ``None`` means the file could not be
    read -- a third state, never folded into "fits".
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return len(raw.replace(b"\r\n", b"\n"))


def _over_budget(files, budget):
    """(over, unmeasurable) -- three states, not two (#1799).

    A file the index names and nobody can read is NOT the same answer as a file
    that fits, and the paths side used to return the second for the first by
    filtering on ``is_file()`` before measuring. It is returned separately so a
    caller cannot mistake "no findings" for "nothing was looked at".
    """
    over = {}
    unmeasurable = []
    for path in files:
        size = _body_bytes(path)
        if size is None:
            unmeasurable.append(path.name)
        elif size > budget:
            over[path.name] = size
    return over, unmeasurable


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
# ceiling. Same construction as BUDGET: ~170 bytes over the largest compliant
# rule at the time of writing (docs-index.md, 5,828 -- the section map for a
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

    It also used to filter on ``is_file()``, so a rule the index named and
    nobody could read never reached the measurement and the check answered
    "fits" for "never looked". Every named path is returned now; ``_over_budget``
    reports the unreadable ones as their own state (#1799).
    """
    out = []
    for raw in (PATHS / "00-index.tsv").read_text(encoding="utf-8").splitlines():
        fields = raw.split(TAB)
        if len(fields) < 2 or not fields[1].endswith(".md"):
            continue
        path = PATHS / fields[1]
        if path not in out:
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
    over, unmeasurable = _over_budget(_indexed_rule_files(), BUDGET)
    assert not unmeasurable, (
        f"tools rule bodies the index names and this test could not read: "
        f"{unmeasurable}. Unmeasured, not compliant.")
    assert not over, (
        f"tools rule bodies over the {BUDGET}-byte per-match budget: {over}. "
        "These are injected in full on every match, false ones included. "
        "Cut the incident history; keep the table of replacement ops.")


def test_no_paths_rule_body_exceeds_the_per_session_budget():
    over, unmeasurable = _over_budget(_indexed_paths_rules(), PATHS_BUDGET)
    assert not unmeasurable, (
        f"paths rule bodies the index names and this test could not read: "
        f"{unmeasurable}. Unmeasured, not compliant.")
    assert not over, (
        f"paths rule bodies over the {PATHS_BUDGET}-byte per-session budget: "
        f"{over}. Paid once per session per agent, so the ceiling is looser "
        "than the tools one -- but it is a ratchet, and growing past it wants "
        "a measurement, not a bigger number.")


# ── #1799: the count is a property of the content, not of the checkout ──────
#
# Git checks these files out with the platform's line endings, so a Windows
# runner reads one extra byte per line for byte-identical content. PR #1768's
# presets-git.md is 5,918 bytes / 94 lines with LF; all four `windows-latest`
# legs of run #31912614030 failed with {'presets-git.md': 6012} -- 5918 + 94
# exactly -- while the ubuntu and macOS legs of the same run passed.
#
# WHAT THE NUMBER CLAIMS, written down because there are two readings and only
# one of them is a property of the repository: it is the size of the body its
# AUTHOR wrote, line endings normalised. It is NOT the number of bytes this
# particular checkout happens to hold. The ratchet exists to bound what an
# author adds; a CR git inserted at checkout is not authored, is invisible on
# the author's machine, and cannot be removed by editing the file -- so counting
# it names a size that exists on no machine the author has, which is the shape
# that reads as a flake and gets re-run.
#
# The cost of that choice, said rather than hidden: on a CRLF checkout the hook
# does inject those CRs. `claude-jit-context/scripts/common.sh` returns
# `e["body"]` as read (`jit_inject_text()`), and its CR handling is escaping for
# JSON, not stripping. So on such a checkout the real injected cost is up to one
# byte per line above what this test reports. That is a property of the
# checkout, not of the rule, and the place to fix it is `.gitattributes` at
# checkout time -- deliberately not done in this change, which owns one test.
#
# A LONE CR is still counted. Nothing inserts one, so it is authored content.

_FIXTURE_LINE = b"x" * 61  # 62 bytes with LF, 63 with CRLF
_LF = b"\n"
_CRLF = b"\r\n"


def _fixture(directory, name, lines, eol):
    path = directory / name
    path.write_bytes((_FIXTURE_LINE + eol) * lines)
    return path


# 96 * 62 = 5952 under the 6000 budget with LF; 96 * 63 = 6048 over it with
# CRLF. The straddle is the whole defect, and it is asserted below rather than
# trusted, so a change to PATHS_BUDGET cannot leave these tests quietly vacuous.
_STRADDLE_LINES = 96
_OVER_LINES = 120  # 7440 with LF -- over the budget on every platform


def test_the_crlf_fixture_actually_straddles_the_budget():
    """Vacuity guard: without the straddle, every assertion below is free."""
    lf = _STRADDLE_LINES * (len(_FIXTURE_LINE) + 1)
    crlf = _STRADDLE_LINES * (len(_FIXTURE_LINE) + 2)
    assert lf < PATHS_BUDGET < crlf, (
        f"the fixture no longer straddles the budget: LF {lf}, CRLF {crlf}, "
        f"budget {PATHS_BUDGET}")
    assert _OVER_LINES * (len(_FIXTURE_LINE) + 1) > PATHS_BUDGET


def test_body_size_is_the_same_under_lf_and_crlf(tmp_path):
    lf = _fixture(tmp_path, "lf.md", _STRADDLE_LINES, _LF)
    crlf = _fixture(tmp_path, "crlf.md", _STRADDLE_LINES, _CRLF)
    assert crlf.stat().st_size == lf.stat().st_size + _STRADDLE_LINES
    assert _body_bytes(crlf) == _body_bytes(lf) == lf.stat().st_size


def test_a_compliant_body_is_compliant_on_a_crlf_checkout_too(tmp_path):
    """The #1799 failure: identical content, red on Windows and green here."""
    files = [_fixture(tmp_path, "lf.md", _STRADDLE_LINES, _LF),
             _fixture(tmp_path, "crlf.md", _STRADDLE_LINES, _CRLF)]
    over, unmeasurable = _over_budget(files, PATHS_BUDGET)
    assert not unmeasurable
    assert over == {}, f"a CRLF checkout pushed a compliant body over: {over}"


def test_an_oversized_body_still_fails_under_both_line_endings(tmp_path):
    """Positive control. Without this, normalising to zero would also pass."""
    files = [_fixture(tmp_path, "lf.md", _OVER_LINES, _LF),
             _fixture(tmp_path, "crlf.md", _OVER_LINES, _CRLF)]
    over, unmeasurable = _over_budget(files, PATHS_BUDGET)
    assert not unmeasurable
    content = _OVER_LINES * (len(_FIXTURE_LINE) + 1)
    assert over == {"lf.md": content, "crlf.md": content}, (
        f"an over-budget body must fail on both, at the content size: {over}")


def test_an_indexed_body_nobody_can_read_is_not_reported_as_compliant(tmp_path):
    """Three states. The paths side used to drop these before measuring."""
    missing = tmp_path / "gone.md"
    over, unmeasurable = _over_budget(
        [missing, _fixture(tmp_path, "lf.md", _STRADDLE_LINES, _LF)],
        PATHS_BUDGET)
    assert unmeasurable == ["gone.md"]
    assert over == {}


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
