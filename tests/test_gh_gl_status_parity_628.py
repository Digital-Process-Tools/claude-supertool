"""#628 — `gh-pr:N:status` and `gl-mr:N:status` must report the same facts.

`presets/github/` and `presets/gitlab/` hold twin scripts forked from a common
ancestor, and nothing pins the two `:status` renders together. So improving one
side reaches the other only if somebody remembers, and #620 is what that looks
like when nobody does: an identical `_load_payload` defect in both
`issue_create.py` files, fixed on one side by hand.

**This pins the fact-key set, not the code.** Sharing a renderer is the larger
change and it fights real API differences; asserting that the two outputs carry
the same *facts* is cheap and it fails the day someone improves one side only.

The bar, from the issue: **every fact one side reports, the other reports or
explicitly declines.** That is `docs/validators.md`'s three-state contract
applied to output shape — "not applicable on this platform" is an answer,
silence is not. So a divergence is legal only with an entry below carrying the
reason it is legal, and three further tests exist purely to stop that list
becoming a record of every divergence nobody could be bothered to fix:

  * `test_every_synonym_pair_is_still_emitted` — a spelling that no longer
    appears is a stale pairing, and a stale pairing hides a real gap.
  * `test_every_exemption_is_still_a_real_divergence` — an exemption for a
    fact the exempted side now emits, or that the owning side has dropped, is
    deleted rather than carried.
  * `test_every_exemption_states_a_reason` — an entry with no reason is the
    thing the issue says an allowlist decays into.

Both fixtures are rendered in two states, merged and open, because half of
these keys are conditional and a one-state fixture would compare the sets on
whichever branch happened to run.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gh_pr = _load("presets/github/pr.py", "github_pr_628")
gl_mr = _load("presets/gitlab/mr.py", "gitlab_mr_628")


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

#: Facts both sides report under different spellings. The pairing is the claim
#: that the fact is present on both — never that the two values are comparable.
#: canonical -> (github key, gitlab key, why the spellings differ)
_SYNONYMS: dict[str, tuple[str, str, str]] = {
    "mergeability": (
        "mergeable",
        "merge_status",
        "Each platform's own vocabulary, and the domains do not overlap: "
        "GitHub answers MERGEABLE/CONFLICTING/UNKNOWN, GitLab answers "
        "can_be_merged/cannot_be_merged/unchecked plus a detailed_merge_status "
        "block reason. One spelling over both would claim a comparability that "
        "is not there, and renaming either key breaks a poll-loop output that "
        "consumers grep. The fact is reported on both sides, which is the bar.",
    ),
    "ci": (
        "checks",
        "pipeline",
        "GitHub's unit is a flat list of check runs; GitLab's is one pipeline "
        "object whose jobs nest in stages. The issue is explicit that a checks "
        "tally shape must not be forced onto a pipeline model, and it has not "
        "been: both sides now sum their legs through the one classifier in "
        "presets/_checks.py (#1607 closed that gap), but GitHub sums its "
        "rollup inline on this line while GitLab sums a separate jobs fetch "
        "onto an indented `legs:` line under it. The spellings differ because "
        "the objects do; the arithmetic and the vocabulary do not.",
    ),
}

#: Facts one side reports and the other does not. Key is (side that is SILENT,
#: canonical fact). Every entry states why the silence is defensible today and
#: what it would cost to close.
_EXEMPTIONS: dict[tuple[str, str], str] = {
    ("gitlab", "review"): (
        "GitHub's reviewDecision arrives inside the single `gh pr view --json` "
        "call the op already makes, so it is free. GitLab approvals are a "
        "separate `/merge_requests/:iid/approvals` request, and `:status` is "
        "the poll-loop render — #815 is explicit that an extra per-call round "
        "trip must not be bought silently. `gl-mr` (full mode) does render an "
        "approvals line, so the fact is reachable; what is not decided is "
        "whether the slim render should pay for it. Filed rather than guessed."
    ),
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _completed(stdout: str) -> Any:
    return subprocess.CompletedProcess(
        args=["cli"], returncode=0, stdout=stdout, stderr="")


def _gh_payload(*, merged: bool) -> str:
    return json.dumps({
        "number": 628,
        "title": "parity",
        "state": "MERGED" if merged else "OPEN",
        "author": {"login": "max"},
        "headRefName": "fix/628",
        "baseRefName": "master",
        "labels": [],
        "milestone": None,
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED" if merged else None,
        "reviews": [],
        "mergeCommit": {"oid": "abc123def4567890"} if merged else None,
        "mergedAt": "2026-08-13T09:00:00Z" if merged else None,
        "additions": 1, "deletions": 0, "changedFiles": 1,
        "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
        "url": "https://github.com/o/r/pull/628",
        "body": "", "comments": [],
        "assignees": [], "createdAt": "2026-08-13T08:00:00Z",
        "updatedAt": "2026-08-13T09:00:00Z", "headRefOid": "abc123def4567890",
    })


def _gl_payload(*, merged: bool) -> str:
    return json.dumps({
        "iid": 628,
        "state": "merged" if merged else "opened",
        "merge_status": "can_be_merged",
        "has_conflicts": False,
        "source_branch": "fix/628",
        "target_branch": "master",
        "head_pipeline": {"status": "success", "id": 4242},
        "merged_at": "2026-08-13T09:00:00Z" if merged else None,
        "merge_commit_sha": "abc123def4567890" if merged else "",
        "web_url": "https://gitlab.example/o/r/-/merge_requests/628",
    })


def _render(monkeypatch, capsys, mod: Any, script: str, payload: str) -> str:
    monkeypatch.setattr(
        mod.subprocess, "run", lambda *a, **kw: _completed(payload))
    monkeypatch.setattr(sys, "argv", [script, "628", "status"])
    assert mod.main() == 0
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# Fact-key extraction
# ---------------------------------------------------------------------------

_KEY = re.compile(r"([A-Za-z_]+): ")


def _fact_keys(out: str) -> set[str]:
    """The `key:` labels a reader can grep for, from the render itself.

    Read off the output rather than the source: a key that is computed and
    never printed is not a fact the other side has to match, and reading the
    source would count it. Indented lines are detail under a fact (pending
    legs, named failed jobs, a staleness disclosure), never a fact of their
    own, so they are skipped — otherwise the two sides diverge on how much
    detail a fixture happens to trigger rather than on what they report.
    """
    keys: set[str] = set()
    for index, line in enumerate(out.splitlines()):
        if not line.strip() or line[:1].isspace():
            continue
        segments = line.split(" | ") if index == 0 else [line]
        for segment in segments:
            match = _KEY.match(segment)
            if match:
                keys.add(match.group(1))
    return keys


def _canonical(side: str, keys: set[str]) -> set[str]:
    lookup = {
        pair[0 if side == "github" else 1]: canon
        for canon, pair in _SYNONYMS.items()
    }
    return {lookup.get(key, key) for key in keys}


@pytest.fixture()
def rendered(monkeypatch, capsys) -> Any:
    def render(merged: bool) -> tuple[set[str], set[str]]:
        gh_out = _render(monkeypatch, capsys, gh_pr, "pr.py",
                         _gh_payload(merged=merged))
        gl_out = _render(monkeypatch, capsys, gl_mr, "mr.py",
                         _gl_payload(merged=merged))
        return _fact_keys(gh_out), _fact_keys(gl_out)
    return render


# ---------------------------------------------------------------------------
# The pin
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("merged", [True, False], ids=["merged", "open"])
def test_status_fact_keys_are_at_parity(rendered, merged: bool) -> None:
    """Every fact one side reports, the other reports or is exempted for."""
    gh_keys, gl_keys = rendered(merged)
    gh_facts = _canonical("github", gh_keys)
    gl_facts = _canonical("gitlab", gl_keys)

    gitlab_silent = {fact for side, fact in _EXEMPTIONS if side == "gitlab"}
    github_silent = {fact for side, fact in _EXEMPTIONS if side == "github"}

    assert gh_facts - gl_facts == gh_facts & gitlab_silent, (
        "gh-pr:status reports facts gl-mr:status does not, with no exemption "
        f"stating why: {sorted((gh_facts - gl_facts) - gitlab_silent)}. Either "
        "report it on the GitLab side or add an entry to _EXEMPTIONS with the "
        f"reason. gh={sorted(gh_keys)} gl={sorted(gl_keys)}"
    )
    assert gl_facts - gh_facts == gl_facts & github_silent, (
        "gl-mr:status reports facts gh-pr:status does not, with no exemption "
        f"stating why: {sorted((gl_facts - gh_facts) - github_silent)}. Either "
        "report it on the GitHub side or add an entry to _EXEMPTIONS with the "
        f"reason. gh={sorted(gh_keys)} gl={sorted(gl_keys)}"
    )


@pytest.mark.parametrize("merged", [True, False], ids=["merged", "open"])
def test_every_synonym_pair_is_still_emitted(rendered, merged: bool) -> None:
    """A pairing whose spelling has gone is hiding a gap, not describing one."""
    gh_keys, gl_keys = rendered(merged)
    for canon, (gh_key, gl_key, _reason) in _SYNONYMS.items():
        assert gh_key in gh_keys, (
            f"_SYNONYMS['{canon}'] pairs '{gh_key}' on the GitHub side, but "
            f"gh-pr:status no longer emits it: {sorted(gh_keys)}")
        assert gl_key in gl_keys, (
            f"_SYNONYMS['{canon}'] pairs '{gl_key}' on the GitLab side, but "
            f"gl-mr:status no longer emits it: {sorted(gl_keys)}")


@pytest.mark.parametrize("merged", [True, False], ids=["merged", "open"])
def test_every_exemption_is_still_a_real_divergence(rendered,
                                                    merged: bool) -> None:
    """An exemption that has been fixed, or gone moot, is deleted not carried."""
    gh_keys, gl_keys = rendered(merged)
    facts = {"github": _canonical("github", gh_keys),
             "gitlab": _canonical("gitlab", gl_keys)}
    for side, fact in _EXEMPTIONS:
        other = "gitlab" if side == "github" else "github"
        assert fact not in facts[side], (
            f"_EXEMPTIONS excuses {side} for not reporting '{fact}', but "
            f"{side} reports it now. Delete the entry.")
        assert fact in facts[other], (
            f"_EXEMPTIONS excuses {side} for not reporting '{fact}', but "
            f"{other} does not report it either, so there is nothing to "
            "excuse. Delete the entry.")


def test_every_exemption_states_a_reason() -> None:
    """The allowlist decays into a divergence graveyard without this."""
    for entry, reason in _EXEMPTIONS.items():
        assert len(reason.strip()) >= 80, (
            f"_EXEMPTIONS{entry} carries no real reason. An entry nobody can "
            "argue with is an entry nobody revisits.")
    for canon, (_gh, _gl, reason) in _SYNONYMS.items():
        assert len(reason.strip()) >= 80, (
            f"_SYNONYMS['{canon}'] carries no real reason.")

# ---------------------------------------------------------------------------
# The same fact, weaker on the render that is read most (#628)
# ---------------------------------------------------------------------------

def _gl_slim(monkeypatch, capsys, payload: dict) -> str:
    return _render(monkeypatch, capsys, gl_mr, "mr.py", json.dumps(payload))


def _gl_base(**overrides: Any) -> dict:
    base = json.loads(_gl_payload(merged=False))
    base.update(overrides)
    return base


def test_slim_merge_status_falls_back_the_way_full_mode_does(
        monkeypatch, capsys) -> None:
    """`merge_status: ?` was the slim render answering worse than `:full`.

    `:full` reads `d.get("merge_status") or d.get("detailed_merge_status") or
    "?"`. `:status` read `d.get("merge_status", "?")` — same file, same
    payload, strictly less. GitLab deprecated `merge_status` in favour of
    `detailed_merge_status`, so on an instance that has stopped populating the
    old key the poll-loop render prints `?` over a value sitting in the
    payload it already fetched. That is the house defect in miniature: a
    literal question mark where a state belongs, indistinguishable from "the
    API said unknown" and "nobody looked".
    """
    payload = _gl_base(detailed_merge_status="not_approved")
    payload.pop("merge_status")
    out = _gl_slim(monkeypatch, capsys, payload)
    assert "merge_status: not_approved" in out, out


def test_slim_merge_status_empty_string_is_unknown_not_blank(
        monkeypatch, capsys) -> None:
    """`.get(key, default)` never fires on a key that is present and empty.

    A blank after the colon is the worst of the three states: it reads as a
    rendering bug rather than as an unanswered question, so nobody chases it.
    """
    out = _gl_slim(monkeypatch, capsys, _gl_base(merge_status=""))
    assert "merge_status: ?" in out, out


def test_slim_merge_status_still_prefers_the_field_gitlab_populated(
        monkeypatch, capsys) -> None:
    """The fallback must not overtake a real answer — pins the order."""
    out = _gl_slim(monkeypatch, capsys, _gl_base(
        merge_status="can_be_merged", detailed_merge_status="mergeable"))
    assert "merge_status: can_be_merged" in out, out
