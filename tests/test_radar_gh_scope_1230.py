"""radar's GitHub tier answered a narrower question than `gh-prs` (#1230).

#1207 removed the implicit `author=@me` from `gh-prs` (shipped in #1212) by
passing `any_author=True` at the op's own call sites and leaving the parameter
default `False`. `presets/watch/tiers/gh_prs.py:live_open_prs` calls
`prs._build_list_cmd(filters, per_page)` with two positional arguments, so the
tier kept the narrow board — two renders of one question disagreeing about
which population answered:

    $ supertool 'radar'
    radar: no rows changed | scope author=@me (default) on ... | 0 open
    $ supertool 'gh-prs'
    0 PR(s) | no author filter (default) — every author's open PRs on this repo

The measured harm is #1071's, unchanged: on 2026-08-09 two dependabot PRs and
one external contributor's PR, all green, were invisible on a board that
rendered as healthy.

The fix is at the coupling, not at the call site. `_build_list_cmd` no longer
injects `--author @me` at all, so there is no second default left for a caller
to inherit after the op moves — which is the mechanism that produced this bug.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


prs = _load("github_prs_1230", "presets/github/prs.py")
gh_tier = _load("radar_gh_prs_1230", "presets/watch/tiers/gh_prs.py")


class _Result:
    def __init__(self, stdout: str) -> None:
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


def _capture(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]
             ) -> list[list[str]]:
    """Run the tier's live fetch against a stub `gh`, returning the argv seen."""
    seen: list[list[str]] = []

    def fake_run(cmd, **_kw):
        seen.append(list(cmd))
        return _Result(json.dumps(rows))

    monkeypatch.setattr(gh_tier.subprocess, "run", fake_run)
    return seen


# ---------------------------------------------------------------------------
# 1. the coupling: no narrow default left anywhere for a caller to inherit
# ---------------------------------------------------------------------------

def test_bare_build_list_cmd_does_not_narrow() -> None:
    """The op's helper is the shared surface, so the default lives there.

    Fixing only the tier's call site would leave `_build_list_cmd`'s own
    default narrow — a default no caller in the tree wants, waiting for the
    next one to inherit it exactly as this tier did.
    """
    cmd = prs._build_list_cmd({}, 50)
    assert "--author" not in cmd, (
        f"a bare gh-prs argv must not carry an author filter; got {cmd!r}")


def test_an_explicit_author_is_still_honoured() -> None:
    """The narrowing must stay reachable, or this test would pass on a
    `_build_list_cmd` that had stopped filtering altogether."""
    cmd = prs._build_list_cmd({"author": "someone"}, 50)
    assert "--author" in cmd and "someone" in cmd, cmd


# ---------------------------------------------------------------------------
# 2. the tier's own board
# ---------------------------------------------------------------------------

def test_tier_board_is_every_author_by_default(
        monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, [])
    gh_tier.live_open_prs(gh_tier.resolve_filter(""))
    assert seen, "the tier never issued a gh pr list call"
    assert "--author" not in seen[0], (
        f"radar's GitHub tier still narrows to one author; argv={seen[0]!r}")


def test_tier_still_narrows_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """`radar:author=@me` is an explicit narrowing and must keep working —
    without this the test above is satisfied by a tier that lost the filter."""
    seen = _capture(monkeypatch, [])
    gh_tier.live_open_prs(gh_tier.resolve_filter("author=@me"))
    assert seen and "--author" in seen[0] and "@me" in seen[0], seen


# ---------------------------------------------------------------------------
# 3. the disclosure — a scope line that names the population that answered
# ---------------------------------------------------------------------------

def test_scope_label_no_longer_claims_a_narrow_default() -> None:
    label = gh_tier.scope_label({}, "owner/repo")
    assert "author=@me" not in label, label
    assert "owner/repo" in label, label
    assert "every author" in label, (
        f"the default scope line must name the population; got {label!r}")


def test_scope_label_still_spells_an_explicit_filter() -> None:
    label = gh_tier.scope_label({"author": "@me"}, "owner/repo")
    assert "author=@me" in label, label


def test_radar_state_filter_line_matches_the_board(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`radar:--state` is how the default was found; it must not restate the
    old one. Read-only — it spawns nothing and calls nothing."""
    monkeypatch.setattr(gh_tier._repo_target, "target", lambda: "")
    lines = gh_tier.radar_state({"_arg": ""})
    filter_line = next(line for line in lines if line.strip().startswith("filter"))
    assert "author=@me" not in filter_line, filter_line
    assert "every author" in filter_line, filter_line
