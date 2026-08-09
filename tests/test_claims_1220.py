"""#1220 — a document's references go stale silently.

`.claude/skills/opensource-manager/SKILL.md` told the maintainer, under a
bullet headed "Two genuine gaps", that no op rendered a commit's run list and
nothing tallied label distribution. Both had shipped. The same file records the
same failure with `repo:` months earlier. A wrong claim in your own notes does
not merely risk being wrong — it *produces* the behaviour it describes, because
the reader obeys it.

`claims:PATH` checks a doc's **references**, never its reasoning. The
distinction is the whole design and it was bought with a measurement: a probe
that flagged citations by issue-state plus an absence-marker word list scored
15 flagged, 2 real. Every false positive was past-tense narration of a fixed
bug, which is lexically identical to a present-tense claim that a hole exists.
Three narrower lexical anchors were measured against the same corpus during
implementation and scored 14%, 11% and 20%. None beat 13%. So there is no
lexical lens here at all, and these tests pin that absence: a sentence is
never a finding.

What is checkable is a reference:

* an op name and its named flags, against the live registry;
* a path, a line number, a section heading, against the tree;
* an issue cited under a heading that *declares* the citation is an open
  defect, against the tracker.

The third is not a heuristic — it reads the doc's own structural annotation,
which is why it can carry a verdict where prose cannot. Measured on
`.claude/jit-context/`, which is injected automatically at tool-call time and
therefore lands with more authority than a doc someone chose to open: 4 cited
"open defects", 4 closed.

Three states throughout, and the third never collapses into either neighbour.
A doc whose references could not be checked must not render as a clean doc.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


check = _load("presets/claims/check.py", "claims_check_1220")

BACKSLASH = chr(92)

REGISTRY = {
    "gh-issues": "gh-issues[:author=@me,label=bug,milestone=v1.0,state=open,per=50,external,nomilestone,iids]",
    "gh-labels": "gh-labels[:tally=PREFIX]",
    "gh-branch": "gh-branch[:BRANCH|:COMMIT_SHA]",
    "gh-pr": "gh-pr:NUMBER_OR_BRANCH[:status|:full|:diff[:PATH]]",
    "read": "read:PATH[:OFFSET:LIMIT|:START-END|:full]",
    "around": "around:PATTERN:PATH[:N]",
    "radar": "radar[:all]",
}


def _open(_n: int):
    return ("OPEN", "")


def _closed(_n: int):
    return ("CLOSED", "")


def _unreachable(_n: int):
    return (None, "gh not authenticated")


THIS_REPO = "Digital-Process-Tools/claude-supertool"


def _scan(text: str, root: Path, issue_state=_open, this_repo=THIS_REPO):
    return check.scan(text, root=root, registry=REGISTRY,
                      issue_state=issue_state, this_repo=this_repo)


def _by_state(findings, state):
    return [f for f in findings if f.state == state]


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "presets" / "github").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "tests").mkdir()
    (root / "presets" / "github" / "pr.py").write_text("x\n" * 300, encoding="utf-8")
    (root / "presets" / "github" / "issues.py").write_text("y\n" * 40, encoding="utf-8")
    (root / "tests" / "conftest.py").write_text("z\n", encoding="utf-8")
    (root / "docs" / "validators.md").write_text(
        "# Validators\n\n## Declining instead of guessing\n\ntext\n", encoding="utf-8")
    return root


# --------------------------------------------------------------------------
# Lens 3 — issues cited under a heading that declares them open defects.
# This is the narrow third rule. It reads an annotation, not a sentence.
# --------------------------------------------------------------------------

def test_closed_issue_under_open_defects_heading_is_contradicted(tmp_path):
    """The live case. `.claude/jit-context/paths/00-manual/presets-github.md`
    listed #1207 under "Open defects" after PR #1212 merged it, and the block
    is injected into every session that touches `presets/github/`."""
    doc = (
        "# Check-state buckets\n\n"
        "text\n\n"
        "# Open defects - check before trusting output\n\n"
        "- **#1207**: gh-prs defaults to author=@me but a maintainer wants every author.\n"
    )
    out = _scan(doc, _tree(tmp_path), issue_state=_closed)
    bad = _by_state(out, check.CONTRADICTED)
    assert [f.token for f in bad] == ["#1207"], out
    assert bad[0].line == 7
    assert "CLOSED" in bad[0].note


def test_open_issue_under_open_defects_heading_holds(tmp_path):
    doc = "# Open defects\n\n- **#1181**: gh-pr prints TALLY UNVERIFIED.\n"
    out = _scan(doc, _tree(tmp_path), issue_state=_open)
    assert _by_state(out, check.CONTRADICTED) == []
    assert [f.token for f in _by_state(out, check.HOLDS) if f.lens == "issue"] == ["#1181"]


def test_singular_open_defect_heading_carries_its_own_citation(tmp_path):
    """docs/validators.md's JIT twin writes it as `# Open defect #1202 - ...`,
    so the number sits in the heading rather than in a bullet under it."""
    doc = "# Open defect #1202 - required() gate is inert on most adapters\n\ntext\n"
    out = _scan(doc, _tree(tmp_path), issue_state=_closed)
    assert [f.token for f in _by_state(out, check.CONTRADICTED)] == ["#1202"]


def test_citation_outside_an_open_defects_block_is_never_a_finding(tmp_path):
    """The 13% measurement, pinned. Every one of these carries an absence
    marker and cites a closed issue; not one is a finding, because a sentence
    is not a reference."""
    doc = (
        "# Notes\n\n"
        "- #429 was filed as two copies and was seven call sites.\n"
        "- surfaced #498, a crash on master no test covered\n"
        "- a checker that cannot answer must say so (#406)\n"
        "- **#1083**, no op renders a commit's run list, and **#1084**, nothing\n"
        "  tallies label distribution across open issues.\n"
    )
    out = _scan(doc, _tree(tmp_path), issue_state=_closed)
    assert [f for f in out if f.lens == "issue"] == []


def test_tracker_unreachable_is_couldnt_check_not_holds(tmp_path):
    """The third state, load-bearing. An issue whose state could not be read
    must not render as a live open defect *or* as a stale one."""
    doc = "# Open defects\n\n- #1207: text\n"
    out = _scan(doc, _tree(tmp_path), issue_state=_unreachable)
    unchecked = _by_state(out, check.UNCHECKED)
    assert [f.token for f in unchecked] == ["#1207"]
    assert "gh not authenticated" in unchecked[0].note
    assert _by_state(out, check.HOLDS) == []
    assert _by_state(out, check.CONTRADICTED) == []


def test_cross_repo_citation_is_couldnt_check(tmp_path):
    """Six of the thirteen the probe could not classify pointed at
    claude-remember's tracker. Resolving them against this repo's numbering
    would answer confidently about a different project."""
    doc = (
        "# Open defects\n\n"
        "- Digital-Process-Tools/claude-remember#88: the backup hook is silent.\n"
        "- #77 in claude-remember: same shape.\n"
    )
    out = _scan(doc, _tree(tmp_path), issue_state=_closed)
    assert _by_state(out, check.CONTRADICTED) == []
    notes = " ".join(f.note for f in _by_state(out, check.UNCHECKED))
    assert notes.count("repository") >= 1


def test_a_slash_in_the_prose_is_not_another_repository(tmp_path):
    """Found by running the op on the real file it was built for. A general
    `owner/name` scan read `dependabot/outside-contributor` and
    `presets/github` as other projects and demoted two real contradictions to
    "couldn't check". A third state that eats findings is the same defect as a
    third state that never fires."""
    doc = (
        "# Open defects\n\n"
        "- **#1207**: use gh-prs:anyauthor or dependabot/outside-contributor "
        "PRs are invisible.\n"
        "- **#1180**: presets/github/issues.py:251 matches evilgithub.com.\n"
    )
    out = _scan(doc, _tree(tmp_path), issue_state=_closed)
    assert sorted(f.token for f in _by_state(out, check.CONTRADICTED)
                  if f.lens == "issue") == ["#1180", "#1207"]


def test_sibling_detection_is_off_when_the_repo_is_unknown(tmp_path):
    """The family prefix is derived from this repo's own name. With no repo
    resolved there is no answer to "another project than which?", so the rule
    declines rather than inventing one."""
    doc = "# Open defects\n\n- #77 in claude-remember: same shape.\n"
    out = _scan(doc, _tree(tmp_path), issue_state=_closed, this_repo=None)
    assert [f.token for f in _by_state(out, check.CONTRADICTED)] == ["#77"]


def test_open_defects_block_ends_at_the_next_heading(tmp_path):
    doc = (
        "# Open defects\n\n- #10: real\n\n"
        "# gh-issues flags\n\n- #20: narration, not a defect list\n"
    )
    out = _scan(doc, _tree(tmp_path), issue_state=_closed)
    assert [f.token for f in _by_state(out, check.CONTRADICTED)] == ["#10"]


def test_a_deeper_heading_does_not_end_the_block(tmp_path):
    doc = "# Open defects\n\n## still defects\n\n- #10: real\n\n# Other\n\n- #20: no\n"
    out = _scan(doc, _tree(tmp_path), issue_state=_closed)
    assert [f.token for f in _by_state(out, check.CONTRADICTED)] == ["#10"]


# --------------------------------------------------------------------------
# Lens 2 — paths, line numbers, headings.
# --------------------------------------------------------------------------

def test_line_number_past_end_of_file_is_contradicted(tmp_path):
    doc = "See `presets/github/issues.py:251` for the host check.\n"
    out = _scan(doc, _tree(tmp_path))
    bad = _by_state(out, check.CONTRADICTED)
    assert [f.token for f in bad] == ["presets/github/issues.py:251"]
    assert "40 lines" in bad[0].note


def test_line_number_within_the_file_holds(tmp_path):
    doc = "See `presets/github/pr.py:219`.\n"
    out = _scan(doc, _tree(tmp_path))
    assert _by_state(out, check.CONTRADICTED) == []
    assert [f.token for f in _by_state(out, check.HOLDS)] == ["presets/github/pr.py:219"]


def test_missing_file_under_an_existing_directory_is_contradicted(tmp_path):
    """`presets/_pr_board.py` is cited by SKILL.md and does not exist. The
    parent does, so this is a claim about this repo and it is wrong."""
    doc = "The board render lives in `presets/_pr_board.py`.\n"
    out = _scan(doc, _tree(tmp_path))
    assert [f.token for f in _by_state(out, check.CONTRADICTED)] == ["presets/_pr_board.py"]


def test_missing_file_under_a_missing_directory_is_couldnt_check(tmp_path):
    """`hooks.d/after_save/50-git-backup.sh` is claude-remember's. Calling it
    a broken reference would be this repo answering about another one."""
    doc = "See `hooks.d/after_save/50-git-backup.sh`.\n"
    out = _scan(doc, _tree(tmp_path))
    assert _by_state(out, check.CONTRADICTED) == []
    assert [f.token for f in _by_state(out, check.UNCHECKED)] == [
        "hooks.d/after_save/50-git-backup.sh"]


def test_bare_basename_resolves_when_unique(tmp_path):
    doc = "The fixtures live in `conftest.py`.\n"
    out = _scan(doc, _tree(tmp_path))
    assert [f.token for f in _by_state(out, check.HOLDS)] == ["conftest.py"]


def test_bare_basename_with_no_match_is_couldnt_check_not_contradicted(tmp_path):
    """A basename can belong to any project the doc talks about."""
    doc = "See `radar.py`.\n"
    out = _scan(doc, _tree(tmp_path))
    assert _by_state(out, check.CONTRADICTED) == []
    assert [f.token for f in _by_state(out, check.UNCHECKED)] == ["radar.py"]


def test_a_basename_at_the_repository_root_wins_over_its_namesakes(tmp_path):
    """Measured: `README.md` in CLAUDE.md matched 16 files and went unchecked.
    The third state is for questions this tool cannot answer, not for ones it
    declined to think about."""
    root = _tree(tmp_path)
    (root / "README.md").write_text("# Top\n", encoding="utf-8")
    (root / "docs" / "README.md").write_text("# Nested\n", encoding="utf-8")
    out = _scan("See `README.md`.\n", root)
    assert [f.state for f in out] == [check.HOLDS]


def test_ambiguous_basename_is_couldnt_check(tmp_path):
    root = _tree(tmp_path)
    (root / "docs" / "conftest.py").write_text("q\n", encoding="utf-8")
    out = _scan("See `conftest.py`.\n", root)
    unchecked = _by_state(out, check.UNCHECKED)
    assert [f.token for f in unchecked] == ["conftest.py"]
    assert "2 files" in unchecked[0].note


def test_named_section_that_no_longer_exists_is_contradicted(tmp_path):
    doc = "The write-up is `docs/validators.md` § “Declining by guessing”.\n"
    out = _scan(doc, _tree(tmp_path))
    bad = [f for f in _by_state(out, check.CONTRADICTED) if f.lens == "heading"]
    assert len(bad) == 1
    assert "Declining by guessing" in bad[0].token


def test_named_section_that_exists_holds(tmp_path):
    doc = "The write-up is `docs/validators.md` § “Declining instead of guessing”.\n"
    out = _scan(doc, _tree(tmp_path))
    assert [f for f in out if f.lens == "heading" and f.state != check.HOLDS] == []


def test_quoted_text_beside_a_line_number_is_checked_against_that_line(tmp_path):
    """Found by running the op on the real
    `.claude/jit-context/paths/00-manual/validators.md`, which cites
    `docs/validators.md:650` with a quotation. The file is 1031 lines, so the
    line number alone read as holding while the quoted text was nowhere near
    line 650. A number proves length; a number with a quotation beside it
    proves the reference."""
    root = _tree(tmp_path)
    (root / "docs" / "validators.md").write_text(
        "one\ntwo\nDeclining instead of guessing\nfour\n", encoding="utf-8")
    doc = "See `docs/validators.md:2`, “Declining instead of guessing”.\n"
    out = _scan(doc, root)
    bad = [f for f in _by_state(out, check.CONTRADICTED) if f.lens == "quote"]
    assert len(bad) == 1
    assert "not :2" in bad[0].note and "validators.md:3" in bad[0].note


def test_quoted_text_on_the_cited_line_holds(tmp_path):
    root = _tree(tmp_path)
    (root / "docs" / "validators.md").write_text(
        "one\nDeclining instead of guessing\n", encoding="utf-8")
    doc = "See `docs/validators.md:2`, “Declining instead of guessing”.\n"
    out = _scan(doc, root)
    assert [f for f in out if f.lens == "quote" and f.state != check.HOLDS] == []


def test_quoted_text_absent_from_the_file_is_contradicted(tmp_path):
    doc = "See `docs/validators.md:1`, “No verdict never rolls back an edit”.\n"
    out = _scan(doc, _tree(tmp_path))
    bad = [f for f in _by_state(out, check.CONTRADICTED) if f.lens == "quote"]
    assert len(bad) == 1
    assert "no line" in bad[0].note


def test_a_line_number_suppresses_the_heading_lens(tmp_path):
    """With a line number the quotation is a quotation of that line, not a
    section name. Reporting it as a missing heading would be a true finding
    reached by a false mechanism, which is not worth having."""
    doc = "See `docs/validators.md:3`, “Declining instead of guessing”.\n"
    out = _scan(doc, _tree(tmp_path))
    assert [f for f in out if f.lens == "heading"] == []


def test_a_placeholder_path_is_a_naming_convention_not_a_broken_reference(tmp_path):
    """`changelog.d/NNN.section.md` in docs-index.md describes how fragments
    are named. Calling it a missing file is the tool inventing a defect."""
    doc = "Fragments are named `changelog.d/NNN.section.md`.\n"
    root = _tree(tmp_path)
    (root / "changelog.d").mkdir()
    out = _scan(doc, root)
    assert _by_state(out, check.CONTRADICTED) == []
    unchecked = _by_state(out, check.UNCHECKED)
    assert [f.token for f in unchecked] == ["changelog.d/NNN.section.md"]
    assert "placeholder" in unchecked[0].note


def test_a_shouty_real_filename_is_not_mistaken_for_a_placeholder(tmp_path):
    root = _tree(tmp_path)
    (root / "docs" / "SCHEMA.md").write_text("# Schema\n", encoding="utf-8")
    out = _scan("See `docs/SCHEMA.md`.\n", root)
    assert [f.state for f in out] == [check.HOLDS]


def test_field_notation_is_not_an_op_reference(tmp_path):
    """ok:true and code:"adapter" are JSON keys being discussed. The op lens
    read four of them as unresolved op names in the real
    `.claude/jit-context/paths/00-manual/validators.md`."""
    quoted = "`code:" + chr(34) + "adapter" + chr(34) + "`"
    doc = "`ok:true`, `ok:false`, " + quoted + ", `count:0`\n"
    out = _scan(doc, _tree(tmp_path))
    assert [f for f in out if f.lens == "op"] == []


def test_path_findings_render_with_forward_slashes_on_every_platform(tmp_path):
    """CI is not macOS. A finding keyed on os.sep would read as a different
    file on Windows, and a suffix match on separators is what #1005 cost."""
    doc = "See `presets/github/issues.py:251`.\n"
    out = _scan(doc, _tree(tmp_path))
    text = check.render("d.md", out)
    assert "presets/github/issues.py:251" in text
    assert BACKSLASH not in text


# --------------------------------------------------------------------------
# Lens 1 — op names and their named flags.
# --------------------------------------------------------------------------

def test_unknown_named_flag_is_contradicted(tmp_path):
    doc = "Use `gh-issues:cohort=3` for the burn-down.\n"
    out = _scan(doc, _tree(tmp_path))
    bad = [f for f in _by_state(out, check.CONTRADICTED) if f.lens == "op"]
    assert len(bad) == 1
    assert "cohort" in bad[0].note


def test_a_second_registry_entry_for_the_same_op_is_merged(tmp_path):
    """`.supertool.json` documents `read:PATH:::grep=PATTERN` under the key
    `read-grep`. Keying the registry by JSON key rather than by the head of
    the syntax string hid that form and reported the documented `read:grep=`
    as a flag that does not exist — in README.md and in a JIT context file
    that is injected at tool-call time."""
    root = _tree(tmp_path)
    (root / ".supertool.json").write_text(
        '{"builtin-ops": {'
        '"read": {"syntax": "read:PATH[:OFFSET:LIMIT]"},'
        '"read-grep": {"syntax": "read:PATH:::grep=PATTERN"}}}',
        encoding="utf-8")
    registry = check._load_registry(root)
    assert "grep=" in registry["read"]
    assert "read-grep" not in registry
    out = check.scan("Use `read:supertool.py:::grep=def op_`.\n", root=root,
                     registry=registry, issue_state=_open)
    assert [f for f in out if f.state == check.CONTRADICTED] == []


def test_a_path_named_as_what_not_to_do_is_not_a_reference(tmp_path):
    """`presets/mytools/status.py`, not `scripts/status.py` — contributing.md
    L304. The second path is the shape being warned against and was never
    meant to exist."""
    root = _tree(tmp_path)
    (root / "scripts").mkdir()
    out = _scan("Co-locate: `presets/github/pr.py`, not `scripts/status.py`.\n", root)
    assert _by_state(out, check.CONTRADICTED) == []


def test_known_named_flag_holds(tmp_path):
    doc = "Use `gh-issues:label=cohort-3,per=100` and `gh-labels:tally=cohort`.\n"
    out = _scan(doc, _tree(tmp_path))
    assert [f for f in out if f.lens == "op" and f.state == check.CONTRADICTED] == []


def test_positional_value_is_never_read_as_a_flag(tmp_path):
    """Measured: a segment-membership check flagged `gh-pr:master:status`,
    `gh-branch:master` and `around:localhost:/etc/hosts:1`, all values sitting
    in placeholder slots, all wrong. Only key=value segments are checked."""
    doc = ("`gh-pr:master:status`, `gh-branch:master`, "
           "`around:localhost:/etc/hosts:1`, `read:supertool.py:1:50`\n")
    out = _scan(doc, _tree(tmp_path))
    assert [f for f in out if f.lens == "op" and f.state == check.CONTRADICTED] == []


def test_unresolved_op_head_is_couldnt_check_never_contradicted(tmp_path):
    """Measured on this repo's own docs: 19 op-shaped tokens resolved to no op
    and not one was a stale op name — they were skill ids, label filters and
    other tools' namespaces. Naming them a finding is the 13% defect again."""
    doc = "`code-review:code-review`, `priority:high`, `caveman:cavecrew-reviewer`\n"
    out = _scan(doc, _tree(tmp_path))
    assert [f for f in out if f.state == check.CONTRADICTED] == []
    assert len([f for f in out if f.lens == "op" and f.state == check.UNCHECKED]) == 3


def test_prose_colon_is_not_an_op_reference(tmp_path):
    """A space after the colon means YAML or English, not a supertool call."""
    doc = "`status: in_progress`, `stop: true`, `cohort-1: 30 open`\n"
    out = _scan(doc, _tree(tmp_path))
    assert [f for f in out if f.lens == "op"] == []


def test_bare_op_name_with_no_arguments_is_not_examined(tmp_path):
    doc = "The `radar` op and the `gh-labels` op.\n"
    out = _scan(doc, _tree(tmp_path))
    assert [f for f in out if f.lens == "op"] == []


# --------------------------------------------------------------------------
# Shared: fences, and the render contract.
# --------------------------------------------------------------------------

def test_fenced_code_is_not_scanned(tmp_path):
    """A fence holds examples, sample output and other projects' code."""
    doc = (
        "```\n"
        "gh-issues:cohort=3\n"
        "presets/_pr_board.py\n"
        "```\n"
    )
    out = _scan(doc, _tree(tmp_path))
    assert out == []


def test_unclosed_fence_swallows_the_rest_rather_than_guessing(tmp_path):
    doc = "`presets/_pr_board.py`\n```\n`presets/_pr_board.py`\n"
    out = _scan(doc, _tree(tmp_path))
    assert len(out) == 1


def test_render_states_all_three_counts(tmp_path):
    doc = (
        "# Open defects\n\n- #1207: text\n\n"
        "# Other\n\nSee `presets/github/pr.py:219` and `radar.py`.\n"
    )
    out = _scan(doc, _tree(tmp_path), issue_state=_closed)
    text = check.render("d.md", out)
    assert "holds 1" in text
    assert "contradicted 1" in text
    assert "couldn't check 1" in text


def test_a_doc_with_unchecked_references_does_not_render_as_clean(tmp_path):
    """The house defect, applied to this op's own output: zero findings plus
    one thing it could not look at must not read as a clean bill of health."""
    doc = "See `radar.py`.\n"
    out = _scan(doc, _tree(tmp_path))
    text = check.render("d.md", out)
    assert "contradicted 0" in text
    assert "couldn't check 1" in text
    assert "NOT A CLEAN DOC" in text


def test_a_doc_with_nothing_unchecked_and_nothing_wrong_says_so(tmp_path):
    doc = "See `presets/github/pr.py:219`.\n"
    text = check.render("d.md", _scan(doc, _tree(tmp_path)))
    assert "NOT A CLEAN DOC" not in text
    assert "contradicted 0" in text
    assert "couldn't check 0" in text


def test_render_says_out_loud_what_it_does_not_check(tmp_path):
    """A reference checker that reads as a document checker is the same
    absence-for-presence swap one layer up."""
    text = check.render("d.md", _scan("text\n", _tree(tmp_path)))
    low = text.lower()
    assert "reference" in low
    assert "prose" in low or "reasoning" in low


def test_render_is_ordered_by_line(tmp_path):
    doc = (
        "See `presets/github/issues.py:251`.\n"
        "See `presets/_pr_board.py`.\n"
        "See `radar.py`.\n"
    )
    out = _scan(doc, _tree(tmp_path))
    text = check.render("d.md", out)
    assert text.index("issues.py:251") < text.index("_pr_board.py")


def test_scan_never_answers_for_a_path_outside_the_repo_root(tmp_path):
    """A doc can cite an absolute path or climb out with `..`. Neither is a
    reference this repo can answer for."""
    doc = "See `../../etc/passwd` and `/etc/hosts`.\n"
    out = _scan(doc, _tree(tmp_path))
    assert [f for f in out if f.state == check.CONTRADICTED] == []
