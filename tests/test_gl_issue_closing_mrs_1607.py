"""#1607 item 2 -- `gl-issue` cannot answer "which MR will close this".

`Related MRs:` reads `/issues/:iid/related_merge_requests` -- every MR that
REFERENCES the issue, closing or not. `gh-issue`'s `Linked PRs:` heading
specifically means "will this close it" (settled in #780), and GitLab's own
`/issues/:iid/closed_by` is the endpoint for that narrower question. This is
a data change, not a wording change: switching `Related MRs:` to read
`closed_by` would silently narrow an existing fact, so a second section,
`Closing MRs:`, is added instead -- both endpoints are read, both facts are
reported, and neither claim is stronger than what its own endpoint answers.

Three states, not two, exactly as `Related MRs:` already has since #815/#780:
a real list, an empty one (`none`), and a fetch that could not be completed
(`unknown`, with a reason) -- the third state must never render as `none`.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "issue.py"
_spec = importlib.util.spec_from_file_location("gitlab_issue_closing_1607", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
issue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issue)


def _fake_run(stdout: str, returncode: int = 0) -> Any:
    return subprocess.CompletedProcess(
        args=["glab"], returncode=returncode, stdout=stdout, stderr="boom")


def _issue_payload() -> str:
    return json.dumps({
        "title": "Plan", "state": "opened", "labels": [], "milestone": None,
        "assignees": [], "author": {"username": "florian"}, "iid": 1607,
        "web_url": "", "description": "body", "project_id": 1,
    })


def _install(monkeypatch, *, closing_response) -> None:
    """`closing_response` is a list body, or ("error", stderr) for a failure."""
    def fake_glab(args, timeout=10):
        return _fake_run(_issue_payload())

    def fake_glab_api(endpoint, timeout=10):
        if "closed_by" in endpoint:
            if isinstance(closing_response, tuple):
                return _fake_run("", returncode=1)
            return _fake_run(json.dumps(closing_response))
        # related_merge_requests and anything else: empty, so it never
        # interferes with the closing-set assertions below.
        return _fake_run("[]")

    monkeypatch.setattr(issue, "_glab", fake_glab)
    monkeypatch.setattr(issue, "_glab_api", fake_glab_api)
    monkeypatch.setattr(issue, "_download_images", lambda urls, n: [])


def _run(monkeypatch, capsys) -> str:
    monkeypatch.setattr(sys, "argv", ["issue.py", "1607"])
    assert issue.main() == 0
    return capsys.readouterr().out


def _closing_section(out: str) -> str:
    assert "Closing MRs" in out, f"no closing-MR section at all:\n{out}"
    return out.split("Closing MRs")[1].split("## Description")[0]


# ---------------------------------------------------------------------------
# The defect: gl-issue answers only "mentions it", never "will close it"
# ---------------------------------------------------------------------------

def test_a_closing_mr_is_named(monkeypatch, capsys) -> None:
    _install(monkeypatch, closing_response=[
        {"iid": 42, "title": "Fix the thing", "state": "opened",
         "source_branch": "fix/1607"},
    ])
    out = _run(monkeypatch, capsys)
    assert "Closing MRs: 1" in out, out
    assert "!42" in out and "Fix the thing" in out, out


def test_no_closing_mr_says_none(monkeypatch, capsys) -> None:
    """Must-fire twin: an empty closing set must still print a real line."""
    _install(monkeypatch, closing_response=[])
    out = _run(monkeypatch, capsys)
    assert "Closing MRs: none" in out, out


# ---------------------------------------------------------------------------
# The third state: could not look is not "will not close it"
# ---------------------------------------------------------------------------

def test_a_failed_closing_lookup_says_unknown_not_none(monkeypatch, capsys) -> None:
    _install(monkeypatch, closing_response=("error", "500 boom"))
    out = _run(monkeypatch, capsys)
    section = _closing_section(out)
    assert "unknown" in section.lower(), section
    assert "none" not in section.lower(), section


# ---------------------------------------------------------------------------
# Carried, not swapped: the pre-existing "mentions it" fact is unaffected
# ---------------------------------------------------------------------------

def test_related_mrs_still_answers_its_own_broader_question(monkeypatch, capsys) -> None:
    """A referencing-but-not-closing MR must still appear under Related,
    proving the new section is additive rather than a silent endpoint swap."""
    def fake_glab(args, timeout=10):
        return _fake_run(_issue_payload())

    def fake_glab_api(endpoint, timeout=10):
        if "related_merge_requests" in endpoint:
            return _fake_run(json.dumps([
                {"iid": 99, "title": "mentions but does not close", "state": "opened",
                 "source_branch": "chore/mention"},
            ]))
        if "closed_by" in endpoint:
            return _fake_run("[]")
        return _fake_run("[]")

    monkeypatch.setattr(issue, "_glab", fake_glab)
    monkeypatch.setattr(issue, "_glab_api", fake_glab_api)
    monkeypatch.setattr(issue, "_download_images", lambda urls, n: [])
    out = _run(monkeypatch, capsys)
    assert "Related MRs: 1" in out and "!99" in out, out
    assert "Closing MRs: none" in out, out
