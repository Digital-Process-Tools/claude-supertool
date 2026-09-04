"""#2195 -- `_guard_drop_io_numbers` strips the leading digit off a numbered
file-descriptor redirect (`2>&1` becomes `>&1`) before `_guard_segments_with_origins`
builds `origin_texts` -- but the span-fidelity check that produces
`origin_faithful` still marked that entry `True`, claiming a faithful render
for a slice that had already lost a character.

Chosen fix (see the design-decision comments beside `undropped`/
`undropped_spans` in `_guard_segments_with_origins`): the digit-drop is still
needed downstream so `2` in `2>&1` never lands in an argv as a stray word
(#1684) -- that is a fact about MATCHING, not about what the caller typed.
So `origin_texts`/`GuardMatch.command` now render the UNDROPPED slice
(digit intact), which makes `origin_faithful`/`command_faithful` genuinely
true rather than falsely so. Option B (teaching the fidelity check to mark
the span `False` instead) was rejected: it would still hand the caller back
a mutated re-quote of their own command, worse for a security-relevant
render than simply not mutating it in the first place, and this repo's own
brief for this fix said to prefer whichever option is easiest to verify
byte-for-byte -- rendering the true original text is directly comparable to
what the caller typed; a `False` flag is not.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

import supertool

_COMMIT_OP = {
    "ops": {
        "git-commit": {
            "safety": "write",
            "cmd": "true",
            "syntax": "git-commit:::MESSAGE",
            "description": "Commit staged changes with MESSAGE.",
            "replaces": [
                {"argv": "git commit", "use": "git-commit:::MESSAGE"},
            ],
        },
    }
}

_PR_OP = {
    "ops": {
        "gh-pr": {
            "safety": "read-only",
            "cmd": "true",
            "syntax": "gh-pr:NUMBER",
            "description": "Review a pull request.",
            "replaces": [
                {"argv": "gh pr view", "use": "gh-pr:NUMBER"},
            ],
        },
    }
}


@pytest.fixture
def guard_config(monkeypatch: pytest.MonkeyPatch):
    def _load(tmp_path: Path, config: Dict[str, Any]) -> Dict[str, Any]:
        (tmp_path / ".supertool.json").write_text(
            json.dumps(config), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(supertool, "_CONFIG", None)
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
        monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
        return config

    return _load


def test_direct_reproduction_numbered_fd_survives_the_render(tmp_path, guard_config):
    """The exact repro from the issue: `_guard_segments_with_origins` on
    `git status 2>&1` must render `2>&1`, not `>&1`, and must not claim
    fidelity for a render that lost the digit."""
    heads, _unread, _origins, origin_texts, origin_faithful = (
        supertool._guard_segments_with_origins("git status 2>&1"))
    assert heads == [["git", "status"]], heads
    assert origin_texts == ["git status 2>&1"], origin_texts
    assert origin_faithful == [True], origin_faithful


def test_matched_command_field_keeps_the_fd_number(tmp_path, guard_config):
    """`GuardMatch.command` (#2076) is fed from the same origin machinery --
    a numbered fd redirect on the MATCHED segment must render intact too."""
    guard_config(tmp_path, _COMMIT_OP)
    cmd = 'git commit -m "hi" 2>&1'
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    match = verdict.matches[0]
    assert match.command == 'git commit -m "hi" 2>&1', match
    assert match.command_faithful is True, match


def test_discarded_field_keeps_the_fd_number(tmp_path, guard_config):
    """The sibling `discarded` field (#2010/#2017/#2023) reads from the same
    `origin_texts`/`origin_faithful` pair -- confirm the fix reaches it too,
    rather than assuming it because both consumers share the machinery."""
    guard_config(tmp_path, _PR_OP)
    cmd = "git status 2>&1 && gh pr view 12"
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    assert verdict.discarded == ("git status 2>&1",), verdict.discarded
    assert verdict.discarded_unfaithful == (), verdict.discarded_unfaithful


def test_unrelated_transform_is_unaffected_positive_control(tmp_path, guard_config):
    """Paired 'must fire' case (per the brief): a plain command carrying no
    numbered fd redirect must still render faithfully, exactly as before --
    proving a broken harness that always renders the undropped text (with no
    real fidelity check) cannot pass both this test and the one above by
    accident, since this one has nothing for the fix to have changed."""
    heads, _unread, _origins, origin_texts, origin_faithful = (
        supertool._guard_segments_with_origins('git commit -m "a; rm -rf /tmp/x"'))
    assert heads == [["git", "commit", "-m", "a; rm -rf /tmp/x"]], heads
    assert origin_texts == ['git commit -m "a; rm -rf /tmp/x"'], origin_texts
    assert origin_faithful == [True], origin_faithful


def test_multiple_numbered_fd_redirects_all_survive(tmp_path, guard_config):
    """More than one numbered fd redirect in the same segment -- both digits
    must survive, not just the first."""
    heads, _unread, _origins, origin_texts, origin_faithful = (
        supertool._guard_segments_with_origins("git status 1>&2 2>&1"))
    assert heads == [["git", "status"]], heads
    assert origin_texts == ["git status 1>&2 2>&1"], origin_texts
    assert origin_faithful == [True], origin_faithful


def test_numbered_fd_redirect_on_an_earlier_discarded_segment_and_a_later_one(
        tmp_path, guard_config):
    """Two segments, each carrying its own numbered fd redirect -- the
    earlier (discarded) one and the later (matched) one both keep their
    digit, so the fix is not accidentally scoped to only the first span."""
    guard_config(tmp_path, _PR_OP)
    cmd = "echo hi 1>&2 && gh pr view 12 2>&1"
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    assert verdict.discarded == ("echo hi 1>&2",), verdict.discarded
    assert verdict.discarded_unfaithful == (), verdict.discarded_unfaithful
    match = verdict.matches[0]
    assert match.command == "gh pr view 12 2>&1", match
    assert match.command_faithful is True, match


def test_no_space_before_a_numbered_fd_redirect_is_now_fixed(
        tmp_path, guard_config):
    """Was a KNOWN pre-existing gap, pinned as `clean` (a bypass) in the
    previous version of this test rather than silently regressing further
    while unfixed. Fixed by #2267: see
    `tests/test_guard_fused_separator_redirect_2267.py` for the full
    reproduction and the chosen fix (a space inserted by
    `_guard_drop_io_numbers` between a separator it is about to fuse with a
    redirect operator). Kept here, updated rather than deleted, because this
    is the exact scenario #2195's own fidelity fix could not repair on its
    own -- `spans_aligned` now finds the two span lists the same length
    again for this class, so the fidelity machinery this file exercises
    renders normally instead of hitting the word-rejoin fallback.
    """
    guard_config(tmp_path, _PR_OP)
    cmd = "true|2>&1 gh pr view 1"
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    assert verdict.discarded == ("true",), verdict.discarded
    match = verdict.matches[0]
    assert match.command_faithful is True, match
    heads, _unread, _origins, _origin_texts, _origin_faithful = (
        supertool._guard_segments_with_origins(cmd))
    assert heads == [["true"], ["gh", "pr", "view", "1"]], heads
