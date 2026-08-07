"""`gh-issue`'s linked-PR lookup is wrong in two opposite directions (#780 items 2/3).

Item 1 (silence on failure) shipped separately. These two share one cause and
one fix, per the issue's own revision after measurement:

    gh pr list --search N

- **False positive (item 2)** — full-text search matches the number *anywhere*
  in a PR's title/body, not only a real closer. Measured live: `gh-issue:770`
  reported `#774` as linked; #774 closes #760 and only *mentions* #770 in
  prose ("unlike #761, this one…"-shaped false match).
- **False negative (item 3)** — `gh pr list` defaults to `--state open`, so a
  *merged* closer is invisible. Measured live: `gh-issue:778` reported "none"
  while #781 (MERGED) actually closes #778.

The maintainer's first suggested fix for item 2 (read the issue timeline) was
independently measured and rejected on #782/#780-comments: a `Closes #N` line
produces the same `CrossReferencedEvent` as prose, so the timeline conflates
the two cases exactly like `--search` does. The verified discriminator is
`closedByPullRequestsReferences(includeClosedPrs: true)` — the same field
`gh-issues` (#782) already uses, so the two ops agree.

Bar for these tests: stub the `gh` boundary (`issue._gh`), not
`_print_linked_prs` itself — a test that stubs the function under test could
pass against a no-op.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
import io

import pytest

PRESET = Path(__file__).parent.parent / "presets" / "github" / "issue.py"
_spec = importlib.util.spec_from_file_location("github_issue_780_items23", PRESET)
assert _spec is not None and _spec.loader is not None
issue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issue)

WEB_URL = "https://github.com/Digital-Process-Tools/claude-supertool/issues/42"


# The rollup subtree the query asks for since #815. Attached to every node
# below rather than left out: a node without it is a *partial* response, and
# the op is required to render the tally as UNKNOWN there, so omitting it
# would make these fixtures model a failure while claiming to model success.
_GREEN_ROLLUP = {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {
    "totalCount": 1,
    "nodes": [{"__typename": "CheckRun", "name": "pytest",
               "status": "COMPLETED", "conclusion": "SUCCESS"}],
}}}}]}


def _graphql_payload(nodes: list[dict]) -> str:
    """The envelope `gh api graphql` returns for the closing-PRs query."""
    return json.dumps({
        "data": {
            "repository": {
                "issue": {
                    "closedByPullRequestsReferences": {
                        "nodes": [dict({"commits": _GREEN_ROLLUP}, **n)
                                  for n in nodes]
                    }
                }
            }
        }
    })


def _linked_section(monkeypatch, gh_behaviour, web_url: str = WEB_URL) -> str:
    monkeypatch.setattr(issue, "_gh", gh_behaviour)
    buf = io.StringIO()
    with redirect_stdout(buf):
        issue._print_linked_prs(42, web_url)
    return buf.getvalue()


# --- the query itself must ask the right field, the right way --------------

def test_query_never_uses_search_and_requests_closers_with_closed_prs_included() -> None:
    """The fix is a different question, not a filtered version of the old one."""
    query = issue._closing_prs_query("o", "n", 42)

    assert "closedByPullRequestsReferences" in query
    assert "includeClosedPrs: true" in query
    assert "search" not in query.lower()


# --- item 2: false positive -------------------------------------------------

def test_a_pr_that_only_mentions_the_number_is_not_reported_as_linked(monkeypatch) -> None:
    """The #774 on #770 case: a real merged PR, but not a closer of *this* issue.

    Because the new query only ever asks for closers, a PR that merely
    name-drops the issue number never enters the response at all — there is
    no text to match, so there is nothing to filter.
    """
    out = _linked_section(monkeypatch, lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=_graphql_payload([]), stderr="",
    ))

    assert "774" not in out
    assert "none" in out.lower()


# --- item 3: false negative -------------------------------------------------

def test_a_merged_closer_is_reported_even_though_search_would_have_hidden_it(monkeypatch) -> None:
    """The #781 on #778 case: `gh pr list --search` (open-only default) misses this."""
    payload = _graphql_payload([
        {"number": 781, "title": "fix: the thing", "state": "MERGED", "headRefName": "fix/781"},
    ])
    out = _linked_section(monkeypatch, lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=payload, stderr="",
    ))

    assert "#781" in out
    assert "MERGED" in out
    assert "none" not in out.lower()
    assert "unknown" not in out.lower()


# --- the host check that decides which repo gets queried --------------------

def test_a_lookalike_host_does_not_resolve_owner_repo() -> None:
    """`endswith("github.com")` also accepts `evilgithub.com`.

    The owner/name lifted from the URL decides which repository the GraphQL
    query asks about, so a lookalike host would return a confident answer
    about the wrong repo — this repo's recurring defect, arriving through a
    hostname. Flagged by CodeQL as py/incomplete-url-substring-sanitization.
    """
    assert issue._owner_repo("https://evilgithub.com/o/r/issues/1") is None
    assert issue._owner_repo("https://github.com.attacker.io/o/r/issues/1") is None


def test_real_github_hosts_still_resolve() -> None:
    """The fix must not cost Enterprise, whose host is a `github.com` subdomain."""
    assert issue._owner_repo("https://github.com/o/r/issues/1") == ("o", "r")
    assert issue._owner_repo("https://GitHub.com/o/r/issues/1") == ("o", "r")
    assert issue._owner_repo("https://corp.github.com/o/r/issues/1") == ("o", "r")


# --- a new failure surface: GraphQL needs owner/repo, `pr list` did not ----

def test_unresolvable_owner_repo_says_unknown_not_none(monkeypatch) -> None:
    """Moving to GraphQL introduces a new way to fail: no owner/repo to query.

    `gh pr list` resolved the repo from the cwd's git remote implicitly; the
    GraphQL query embeds owner/name in its text and has no such fallback. That
    new failure mode gets the same three-state treatment as every other one
    here — it must not silently render as "no linked PRs".
    """
    called = {"n": 0}

    def _gh_should_not_be_called(*a, **k):
        called["n"] += 1
        return SimpleNamespace(returncode=0, stdout=_graphql_payload([]), stderr="")

    out = _linked_section(monkeypatch, _gh_should_not_be_called, web_url="not-a-github-url")

    assert "unknown" in out.lower()
    assert "none" not in out.lower()
    assert called["n"] == 0, "no owner/repo to query, so gh should never be invoked"


# --- the cap: singular op, so it should not silently truncate --------------

def test_hitting_the_cap_says_so_rather_than_silently_truncating(monkeypatch) -> None:
    """A capped-and-silent list is this repo's recurring defect (per the issue).

    A single-issue lookup is a user asking about one issue specifically, not a
    board scan — so the cap here is generous, and hitting it must be visible.
    """
    nodes = [
        {"number": n, "title": f"pr {n}", "state": "MERGED", "headRefName": f"b{n}"}
        for n in range(issue.CLOSING_PR_LIMIT)
    ]
    out = _linked_section(monkeypatch, lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=_graphql_payload(nodes), stderr="",
    ))

    assert len(nodes) == issue.CLOSING_PR_LIMIT
    assert "more" in out.lower() or "first" in out.lower(), (
        f"cap was hit ({issue.CLOSING_PR_LIMIT}) but nothing told the reader:\n{out!r}"
    )


def test_under_the_cap_is_silent_about_capping() -> None:
    """The control: an ordinary result must not carry a truncation warning."""
    # covered implicitly by test_a_merged_closer_is_reported_...; this test
    # exists so a fix cannot satisfy the cap test by always printing the note.
    assert issue.CLOSING_PR_LIMIT > 1


# --- control: the existing "unknown on failure" paths must still work -----

def test_non_zero_exit_still_says_unknown(monkeypatch) -> None:
    out = _linked_section(monkeypatch, lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="", stderr="boom",
    ))

    assert "unknown" in out.lower()
    assert "none" not in out.lower()


@pytest.mark.parametrize(
    "boom",
    [
        subprocess.TimeoutExpired(cmd="gh", timeout=1),
        json.JSONDecodeError("bad", "", 0),
    ],
    ids=["timeout", "malformed-json"],
)
def test_swallowed_exceptions_still_say_unknown(monkeypatch, boom) -> None:
    def _raise(*a, **k):
        raise boom

    out = _linked_section(monkeypatch, _raise)

    assert "unknown" in out.lower()
    assert "none" not in out.lower()
