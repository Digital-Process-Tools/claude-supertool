"""`gh-issue` must say when it could not ask about linked PRs (#780 item 1).

The lookup had two silent failure paths: a non-zero `gh pr list` printed
nothing at all, and `except (TimeoutExpired, JSONDecodeError): pass` swallowed
the rest. Either way the reader sees no `Linked PRs` line and concludes **there
are none** — when the truth may be *I could not ask*.

That reading has a cost attached. "No linked PR" is the signal that an issue is
unclaimed, so the action it invites is delegating work. Work has already been
re-delegated onto an already-merged fix once on this tracker because a list did
not say.

Same class as #414, #445/#454, #459, #477/#482, #487, #486: an absence produced
by the tool, read as an absence in the world. `docs/validators.md` §"Declining
instead of guessing" — three states, not two.

Scope: item 1 only. Items 2 and 3 (the lookup used a full-text `--search`
that both over-matched prose and, via its open-only default, under-matched
merged closers) are covered separately in
test_github_issue_linked_prs_search_wrong_780_items_2_3.py — the fix there is
`closedByPullRequestsReferences(includeClosedPrs: true)`, not the timeline.
That fix changed the shape of the stubbed `gh` response the tests below use
(GraphQL envelope, not a `pr list --json` array); the three-state contract
itself is unchanged.
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

PRESET = Path(__file__).parent.parent / "presets" / "github" / "issue.py"
_spec = importlib.util.spec_from_file_location("github_issue_780", PRESET)
assert _spec is not None and _spec.loader is not None
issue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issue)


# A resolvable owner/repo so these tests exercise the `gh` failure paths
# themselves, not the owner/repo resolution added for #780 items 2/3 (that
# has its own coverage in test_github_issue_linked_prs_search_wrong_780_items_2_3.py).
WEB_URL = "https://github.com/Digital-Process-Tools/claude-supertool/issues/42"


def _linked_section(monkeypatch, gh_behaviour) -> str:
    """Render just the linked-PR section, with `gh api graphql` stubbed."""
    monkeypatch.setattr(issue, "_gh", gh_behaviour)
    buf = io.StringIO()
    with redirect_stdout(buf):
        issue._print_linked_prs(42, WEB_URL)
    return buf.getvalue()


def test_non_zero_exit_says_unknown_rather_than_nothing(monkeypatch) -> None:
    """The path that printed nothing at all."""
    out = _linked_section(
        monkeypatch,
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )

    assert "unknown" in out.lower(), (
        "a failed lookup printed nothing, so the reader sees no Linked PRs "
        f"line and concludes there are none. Got:\n{out!r}"
    )
    assert "none" not in out.lower(), (
        f"'none' is a claim the op cannot support here:\n{out!r}"
    )


@pytest.mark.parametrize(
    "boom",
    [
        subprocess.TimeoutExpired(cmd="gh", timeout=1),
        json.JSONDecodeError("bad", "", 0),
    ],
    ids=["timeout", "malformed-json"],
)
def test_swallowed_exceptions_say_unknown(monkeypatch, boom) -> None:
    """The `except ...: pass` path."""
    def _raise(*a, **k):
        raise boom

    out = _linked_section(monkeypatch, _raise)

    assert "unknown" in out.lower(), (
        f"{type(boom).__name__} was swallowed silently. Got:\n{out!r}"
    )
    assert "none" not in out.lower()


def test_a_genuine_empty_result_still_says_none(monkeypatch) -> None:
    """The control: 'none' must remain available for a real, answered zero.

    This passes against the broken code too — deliberately. It is here so that
    a fix cannot satisfy the tests above by simply never saying 'none' again,
    which would trade one wrong answer for another.

    Payload shape is the GraphQL envelope (#780 items 2/3 moved the lookup off
    `gh pr list`), not the old `pr list --json` array — the three-state
    contract this file tests is unchanged, only the data source is.
    """
    empty = json.dumps({"data": {"repository": {"issue": {
        "closedByPullRequestsReferences": {"nodes": []}
    }}}})
    out = _linked_section(
        monkeypatch,
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=empty, stderr=""),
    )

    assert "none" in out.lower()
    assert "unknown" not in out.lower()


# The rollup subtree the linked-PR query asks for since #815. A node without
# it is a *partial* response, and the op is required to say the tally is
# UNKNOWN there — so a fixture omitting it no longer models a happy path.
_GREEN_ROLLUP = {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {
    "totalCount": 1,
    "nodes": [{"__typename": "CheckRun", "name": "pytest",
               "status": "COMPLETED", "conclusion": "SUCCESS"}],
}}}}]}


def test_a_populated_result_still_lists_the_prs(monkeypatch) -> None:
    """The other control: the happy path is unchanged."""
    payload = json.dumps({"data": {"repository": {"issue": {
        "closedByPullRequestsReferences": {"nodes": [
            {"number": 99, "title": "a fix", "state": "OPEN", "headRefName": "b",
             "commits": _GREEN_ROLLUP},
        ]}
    }}}})
    out = _linked_section(
        monkeypatch,
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=payload, stderr=""),
    )

    assert "#99" in out
    assert "unknown" not in out.lower()
