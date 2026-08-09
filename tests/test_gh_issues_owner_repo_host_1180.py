"""#1180 — the board's repo is derived from row urls with a suffix host test.

`parts[2].endswith("github.com")` matches `evilgithub.com`. The rows come from
`gh` today, so this is not attacker-reachable through the normal path; what it
decides is which repo every subsequent GraphQL call is made against, from row
content rather than from configuration.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

PRESET_PATH = Path(__file__).parent.parent / "presets" / "github" / "issues.py"
_spec = importlib.util.spec_from_file_location("github_issues", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
issues = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issues)


def _no_target(monkeypatch) -> None:
    monkeypatch.setattr(issues._repo_target, "owner_repo", lambda: None)


def test_owner_repo_reads_a_real_github_url(monkeypatch) -> None:
    _no_target(monkeypatch)
    rows = [{"url": "https://github.com/owner/name/issues/7"}]
    assert issues._owner_repo(rows) == ("owner", "name")


def test_owner_repo_declines_a_lookalike_host(monkeypatch) -> None:
    _no_target(monkeypatch)
    rows = [{"url": "https://evilgithub.com/attacker/payload/issues/7"}]
    assert issues._owner_repo(rows) is None


def test_owner_repo_declines_a_suffixed_lookalike_host(monkeypatch) -> None:
    _no_target(monkeypatch)
    rows = [{"url": "https://notgithub.com/attacker/payload/issues/7"}]
    assert issues._owner_repo(rows) is None


def test_owner_repo_skips_a_lookalike_and_takes_the_real_one(monkeypatch) -> None:
    _no_target(monkeypatch)
    rows = [
        {"url": "https://evilgithub.com/attacker/payload/issues/7"},
        {"url": "https://github.com/owner/name/issues/8"},
    ]
    assert issues._owner_repo(rows) == ("owner", "name")


def test_owner_repo_accepts_a_dot_boundary_subdomain(monkeypatch) -> None:
    _no_target(monkeypatch)
    rows = [{"url": "https://www.github.com/owner/name/issues/7"}]
    assert issues._owner_repo(rows) == ("owner", "name")


def test_owner_repo_target_still_wins(monkeypatch) -> None:
    monkeypatch.setattr(issues._repo_target, "owner_repo", lambda: ("t", "r"))
    rows = [{"url": "https://evilgithub.com/attacker/payload/issues/7"}]
    assert issues._owner_repo(rows) == ("t", "r")
