"""The merge gate reads `gh-pr`'s tally through a seam nothing pins (#1181).

`pr_merge.py` calls `pr._declared_for_commit` across a module boundary and
unpacks the result positionally. Every test that exercises the gate replaces
`_load_pr_module()` with a stub, and the stub was written to match the caller —
so the caller and its double agreed with each other while the real function's
return shape moved, and 9,434 green tests said nothing.

The failure that produces is not a wrong number. It is `ValueError: too many
values to unpack` raised before the gate prints a single line, in the one op
that merges, which is the worst place in this repo to learn that a signature
changed.

This file is the missing check, and it is deliberately about the *real* module:
no stub, no monkeypatch of the seam. A test that mocks the thing it is
verifying is the absence it exists to close.
"""
from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

_PRESETS = Path(__file__).parent.parent / "presets" / "github"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _PRESETS / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pr = _load("github_pr_seam", "pr.py")
pr_merge = _load("github_pr_merge_seam", "pr_merge.py")


def test_the_gate_unpacks_what_the_real_tally_returns(monkeypatch) -> None:
    """Run the real `_declared_for_commit` and the real unpack, together."""
    monkeypatch.setattr(pr, "_runs_on_commit",
                        lambda *_a: [("1", "tests", "901")])
    monkeypatch.setattr(pr._declared_legs, "legs_for_run",
                        lambda _o, _r, _rid: ["leg-a", "leg-b"])
    row = {
        "url": "https://github.com/o/n/pull/7",
        "headRefOid": "b" * 40,
        "statusCheckRollup": [{
            "name": "leg-a",
            "conclusion": "SUCCESS",
            "detailsUrl": "https://github.com/o/n/actions/runs/1/job/1",
        }],
    }
    # The exact shape of pr_merge.py's own call site, executed for real.
    declared, declared_names, _unc, _reason = pr._declared_for_commit(row)
    assert declared == 2
    assert declared_names == ["leg-a", "leg-b"]


def test_the_gate_call_site_matches_the_tally_arity() -> None:
    """Whatever the tuple grows to next, the two must move together.

    Counting the names `pr_merge.py` binds is not elegant, and it is the only
    check that fails for the right reason without a live `gh`: the seam is a
    positional unpack across two files loaded independently, so nothing else
    brings the two into contact.
    """
    src = inspect.getsource(pr_merge)
    call = [ln for ln in src.splitlines() if "_declared_for_commit(" in ln
            and "def " not in ln]
    assert call, "pr_merge.py no longer calls _declared_for_commit — retarget this"
    bound = call[0].split("=")[0].strip()
    arity = len([p for p in bound.split(",") if p.strip()])
    row = {"url": "", "headRefOid": "", "statusCheckRollup": []}
    assert len(pr._declared_for_commit(row)) == arity, (
        f"pr_merge.py unpacks {arity} values from _declared_for_commit, which "
        f"returns {len(pr._declared_for_commit(row))}"
    )


def test_the_gate_states_why_a_tally_could_not_be_verified() -> None:
    """The reason is worth most here — this is the line before a merge."""
    pr_row = {
        "number": 7,
        "headRefOid": "c" * 40,
        "statusCheckRollup": [{"name": "leg-a", "conclusion": "SUCCESS",
                               "status": "COMPLETED"}],
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "isDraft": False,
        "state": "OPEN",
    }
    lines = pr_merge._check_findings(
        pr_row, None, (), reason="7 distinct workflows on this commit exceed "
                                 "the reconciliation cap of 8")
    body = " ".join(lines)
    assert "cap of 8" in body, (
        f"a merge refusal that names no cause is one nobody can act on; "
        f"got {body!r}"
    )
