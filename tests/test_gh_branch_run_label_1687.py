"""A workflow name cannot forge supertool's own `(run ID)` annotation (#1687).

`runs_on_sha` labels a row `NAME (run ID)` when one workflow name has two runs
on the commit (#1640, which stays). `NAME` is `workflowName` — remote text,
chosen by whoever controls the repo's `.github/workflows` — so a workflow
literally named `Analyze (run 12345)` used to render **byte for byte** as
supertool's annotation of a workflow named `Analyze`, in the first column of
the table a merge gate and a release gate are both read off.

`_untrusted.flat` does not help: it strips control characters, and every
character here is printable. The boundary that was missing is the one
`_untrusted.scrub` already draws for the fence markers — the tool's own
structural shape is neutralised where it appears in content, so the reader can
tell which of the two wrote it.

Class `forges` rather than `misreports`: the render is correct for its input.
What is wrong is that a third party chooses text arriving as supertool's own
annotation.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


branch = _load("presets/github/branch.py", "github_branch_1687")

SHA = "a" * 40
FORGED = "Analyze (run 12345)"


def _run(name: str, rid: int, attempt: int = 1) -> dict:
    return {"workflowName": name, "databaseId": rid, "attempt": attempt,
            "headSha": SHA, "status": "completed", "conclusion": "success"}


def test_a_forged_name_does_not_render_as_the_tool_s_annotation():
    """One run, a name shaped like the annotation. The two must not be equal.

    The honest render of a workflow with two runs named `Analyze` is
    `Analyze (run 12345)`. Nothing may produce those same bytes out of a single
    run whose *name* contains them.
    """
    honest = branch.runs_on_sha(
        [_run("Analyze", 12345), _run("Analyze", 67890)], SHA)
    forged = branch.runs_on_sha([_run(FORGED, 999)], SHA)
    assert FORGED in honest, honest
    assert FORGED not in forged, forged
    assert not set(honest) & set(forged), (honest, forged)


def test_the_neutralised_label_still_carries_the_name():
    """Declined, not censored — the reader still gets what the workflow is called."""
    labels = list(branch.runs_on_sha([_run(FORGED, 999)], SHA))
    assert len(labels) == 1, labels
    assert labels[0].startswith("Analyze "), labels
    assert "12345" not in labels[0], labels


def test_a_forged_name_with_two_runs_keeps_both_rows():
    """#1640 is what the annotation is for, and it survives the boundary.

    Two runs of one workflow on one commit must both be listed and both must
    pass. The forged tag in the name is neutralised; supertool's own is still
    appended, and the two rows are still distinguishable.
    """
    selected = branch.runs_on_sha([_run(FORGED, 111), _run(FORGED, 222)], SHA)
    assert len(selected) == 2, selected
    assert any("(run 111)" in k for k in selected), selected
    assert any("(run 222)" in k for k in selected), selected
    for label in selected:
        assert "12345" not in label, selected


def test_the_workflow_name_itself_is_untouched():
    """Only the *label* is neutralised. Every name-keyed consumer reads the run.

    `workflow_names` feeds the declared-set check and the previous-head
    comparison; rewriting the name there would invent a missing workflow out of
    a spelling, which is the trap `missing_workflows` was written against.
    """
    selected = branch.runs_on_sha([_run(FORGED, 999)], SHA)
    assert branch.workflow_names(selected) == {FORGED}
    assert branch.missing_workflows([FORGED], selected) == []


def test_an_ordinary_name_is_not_touched_at_all():
    """A refusal on every commit is one nobody reads."""
    selected = branch.runs_on_sha(
        [_run("tests", 1), _run("Code Quality: Push on master", 2)], SHA)
    assert set(selected) == {"tests", "Code Quality: Push on master"}, selected


def test_the_note_stops_teaching_the_ambiguous_form():
    """`run_id_note()` told the reader to trust `(run <id>)` in the name column.

    It has to name the boundary now, or it teaches the reader to accept exactly
    the bytes the fix exists to separate.
    """
    note = branch.run_id_note()
    assert "neutralis" in note, note
    assert "(run <id>)" in note, note
