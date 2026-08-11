"""#907 — the residue the host fix left: two spellings, and one `None`.

`parts[2].endswith("github.com")` was replaced in #1212, but by *two*
implementations. `issues.py` strips userinfo and port off the authority before
comparing; `issue.py` compares `url.split("/")[2]` whole, so a perfectly real
`https://user@github.com/o/r` or `https://github.com:443/o/r` resolves in one
op and not in the other. Both are hand-rolled index arithmetic over a URL, which
is point 2 of #907 and was only ever answered for one of them.

The second half is #907's point 3: `_owner_repo` in `issues.py` returned `None`
for an empty listing, for rows with no url, for rows on another host, and for a
github.com url too short to carry an owner and a name. Four situations, one
answer, and the caller renders it into a decline the reader is meant to act on.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PRESETS = Path(__file__).parent.parent / "presets"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


issues = _load("github_issues_907", _PRESETS / "github" / "issues.py")
issue = _load("github_issue_907", _PRESETS / "github" / "issue.py")


@pytest.fixture(autouse=True)
def _no_repo_target(monkeypatch):
    """No `repo:` target, so every assertion is about the URL and nothing else."""
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    monkeypatch.setattr(issues._repo_target, "owner_repo", lambda: None)
    monkeypatch.setattr(issue._repo_target, "owner_repo", lambda: None)


def _pair(rows):
    """The pair half of `issues._owner_repo`, which now also returns a reason."""
    return issues._owner_repo(rows)[0]


def _reason(rows):
    return issues._owner_repo(rows)[1]


# --- one host test, not two ------------------------------------------------

REAL = [
    "https://github.com/o/r/issues/1",
    "https://GitHub.com/o/r/issues/1",
    "https://www.github.com/o/r/issues/1",
    "https://github.com./o/r/issues/1",
    "https://user@github.com/o/r/issues/1",
    "https://github.com:443/o/r/issues/1",
    "https://github.com/o/r/issues/1?tab=x",
    "https://github.com/o/r",
]

LOOKALIKE = [
    "https://evilgithub.com/o/r/issues/1",
    "https://notgithub.com/o/r/issues/1",
    "https://github.com.attacker.io/o/r/issues/1",
    "https://github.com@evil.example/o/r/issues/1",
    "https://github.com:443@evil.example/o/r/issues/1",
    "https://evil.example/o/r/issues/1#github.com",
    "https://evil.example/o/r?x=https://github.com/a/b",
]


@pytest.mark.parametrize("url", REAL)
def test_both_ops_resolve_every_real_github_url(url: str) -> None:
    """A URL GitHub itself can emit must resolve in both ops, identically."""
    assert issue._owner_repo(url) == ("o", "r"), url
    assert _pair([{"url": url}]) == ("o", "r"), url


@pytest.mark.parametrize("url", LOOKALIKE)
def test_both_ops_refuse_every_lookalike(url: str) -> None:
    assert issue._owner_repo(url) is None, url
    assert _pair([{"url": url}]) is None, url


def test_a_fragment_cannot_smuggle_a_path_segment() -> None:
    """`#` ends the path. Index arithmetic over `split("/")` did not know that.

    `https://github.com/o#x/r/issues/1` has ONE path segment, `o`. Splitting on
    `/` yields `['https:', '', 'github.com', 'o#x', 'r', ...]` and reads owner
    `o#x`, name `r` — a repo nobody named, on a real host, so nothing downstream
    has any reason to doubt it.
    """
    assert issue._owner_repo("https://github.com/o#x/r/issues/1") is None
    assert _pair([{"url": "https://github.com/o#x/r/issues/1"}]) is None


def test_an_unparseable_url_is_declined_not_raised() -> None:
    """A malformed IPv6 authority makes `urlsplit` raise; the op must not."""
    assert issue._owner_repo("https://[::1/o/r/issues/1") is None
    assert _pair([{"url": "https://[::1/o/r/issues/1"}]) is None


# --- `None` said four things; it now says which one ------------------------

def test_no_rows_is_not_the_same_answer_as_no_usable_row() -> None:
    empty = _reason([])
    unusable = _reason([{"url": "https://evilgithub.com/o/r/issues/1"}])
    assert empty is not None
    assert unusable is not None
    assert empty != unusable
    assert "no rows" in empty


def test_rows_without_a_url_are_named_as_such() -> None:
    reason = _reason([{"number": 1}, {"number": 2}])
    assert reason is not None
    assert "2" in reason and "url" in reason


def test_rows_on_another_host_are_counted_not_echoed() -> None:
    """The count, never the host: a row url is tracker content, not our text."""
    reason = _reason([
        {"url": "https://evilgithub.com/o/r/issues/1"},
        {"url": "https://notgithub.com/o/r/issues/2"},
    ])
    assert reason == "no row url is on github.com (checked 2)"
    assert "evilgithub" not in reason and "notgithub" not in reason


def test_a_github_url_with_no_owner_name_is_its_own_reason() -> None:
    """On the right host and still unusable — the one case that is our bug."""
    reason = _reason([{"url": "https://github.com/o"}])
    assert reason is not None
    assert "owner" in reason


def test_a_resolved_pair_carries_no_reason() -> None:
    pair, reason = issues._owner_repo([{"url": "https://github.com/o/r/issues/1"}])
    assert pair == ("o", "r")
    assert reason is None


def test_the_decline_the_caller_prints_names_the_cause(capsys, monkeypatch) -> None:
    """The reason is not decoration: it reaches the sentence the reader acts on."""
    reason = _reason([{"url": "https://evilgithub.com/o/r/issues/1"}])
    text = issues._decline("external", "author association",
                           f"repo could not be identified from the listing: {reason}")
    assert reason is not None and reason in text
