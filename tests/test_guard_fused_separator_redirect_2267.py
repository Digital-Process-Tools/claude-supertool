"""#2267 -- `_guard_drop_io_numbers` strips the leading digit off a numbered
file-descriptor redirect (`2>&1` becomes `>&1`). When that redirect sits
directly against a preceding top-level separator with no space (`|2>&1`,
`;2>&1`, `&2>&1`, `&&2>&1`), dropping the digit is the ONLY thing that used
to keep the separator and the redirect operator apart -- with it gone, the
two operator runs become adjacent (`|2>&1` -> `|>&1`), and
`_guard_raw_segment_spans`/`_guard_tokenize_prepared` read that fused run as
a single redirect rather than a separator followed by one, collapsing two
top-level shell segments into one and losing the split between them.

Reproduced directly, pre-fix, against every one of the four separators the
issue names, plus a same-shape case from the fuzz sweep (`&2>&1` after a
bare `true` rather than after `||`, to make sure the fix is not accidentally
scoped to only `|`):

    guard_command('true|2>&1 gh pr view 1')   -> clean   (should block)
    guard_command('true;2>&1 gh pr view 1')   -> clean   (should block)
    guard_command('true&&2>&1 gh pr view 1')  -> clean   (should block)
    guard_command('true&2>&1 gh pr view 1')   -> clean   (should block)

Chosen fix: `_guard_drop_io_numbers` now inserts a single space between a
preceding top-level separator character (`_GUARD_SEPARATOR_CHARS`) and a
redirect operator it is about to fuse with by dropping the digit between
them -- the same whitespace that already keeps the two operators apart in
the (unaffected) spaced idiom `true| 2>&1 gh pr view 1`. This is a pure
textual insertion in the prepared/matching copy only: `origin_texts` keeps
rendering the UNDROPPED slice (#2195), so nothing the caller typed changes
in what is shown back to them.

Once the fusion is fixed, `_guard_segments_with_origins`'s own
`spans_aligned` check (see the `undropped_spans` comment beside it) should
find `segment_spans`/`undropped_spans` the same length again for this
class, since the fused, undercounted span is exactly what made them
disagree -- so this also settles the pinned KNOWN gap in
`tests/test_guard_numbered_fd_fidelity_2195.py::test_no_space_before_a_numbered_fd_redirect_is_a_KNOWN_pre_existing_gap`,
updated alongside this file to assert the fixed (blocked, correctly split)
behaviour instead of the gap.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

import supertool

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


@pytest.mark.parametrize("sep", ["|", ";", "&&", "&"])
def test_no_space_fused_separator_redirect_is_now_blocked(
        tmp_path, guard_config, sep):
    """The four literal reproductions from the issue: each separator,
    fused with a numbered fd redirect and no intervening space, must still
    split into two top-level segments and the second must still block."""
    guard_config(tmp_path, _PR_OP)
    cmd = f"true{sep}2>&1 gh pr view 1"
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", (cmd, verdict)
    match = verdict.matches[0]
    assert match.command == "gh pr view 1 2>&1" or "gh pr view 1" in match.command, (
        cmd, match.command)


@pytest.mark.parametrize("sep", ["|", ";", "&&", "&"])
def test_fused_separator_redirect_splits_into_two_segments(tmp_path, guard_config, sep):
    """Same shape, checked at the tokenization layer directly rather than
    only through the end-to-end verdict: the fused run must still produce
    two `heads` entries, `true` and the `gh pr view 1` segment."""
    cmd = f"true{sep}2>&1 gh pr view 1"
    heads, _unread, _origins, _origin_texts, _origin_faithful = (
        supertool._guard_segments_with_origins(cmd))
    assert heads == [["true"], ["gh", "pr", "view", "1"]], (cmd, heads)


def test_fuzz_shape_bare_ampersand_after_a_non_pipe_head(tmp_path, guard_config):
    """A same-shape case from the fuzz sweep the issue mentions, distinct
    from the four literal reproductions: `&` (background) rather than `&&`
    or `|`, immediately preceding the numbered fd redirect."""
    guard_config(tmp_path, _PR_OP)
    cmd = "true&2>&1 gh pr view 12"
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict


def test_spaced_idiom_is_unaffected_positive_control(tmp_path, guard_config):
    """Paired 'must still allow' case: the pre-existing SPACED idiom
    (`| 2>&1`, already correct on master) must render identically after
    the fix -- proving the fix does not touch the unaffected case, only
    the no-space fusion."""
    guard_config(tmp_path, _PR_OP)
    cmd = "true| 2>&1 gh pr view 1"
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    heads, _unread, _origins, _origin_texts, _origin_faithful = (
        supertool._guard_segments_with_origins(cmd))
    assert heads == [["true"], ["gh", "pr", "view", "1"]], heads


def test_numbered_fd_redirect_with_no_preceding_separator_is_unaffected(tmp_path):
    """Paired 'must still allow' case: an ordinary numbered fd redirect with
    no separator immediately before it at all (`git status 2>&1`, the
    original #1684/#2195 case) must be untouched by this fix."""
    heads, _unread, _origins, origin_texts, origin_faithful = (
        supertool._guard_segments_with_origins("git status 2>&1"))
    assert heads == [["git", "status"]], heads
    assert origin_texts == ["git status 2>&1"], origin_texts
    assert origin_faithful == [True], origin_faithful


def test_argument_digit_is_still_kept_when_immediately_after_a_separator(tmp_path):
    """Paired 'must still allow' case: a digit that is a real ARGUMENT
    (not an IO_NUMBER, because it is not followed by `<`/`>`) directly
    after a separator must still survive untouched -- the fix must only
    ever insert a space ahead of digits it is about to drop, never ahead
    of digits it keeps."""
    heads, _unread, _origins, origin_texts, origin_faithful = (
        supertool._guard_segments_with_origins("true|2 echo hi"))
    assert heads == [["true"], ["2", "echo", "hi"]], heads
    assert origin_texts == ["true", "2 echo hi"], origin_texts


@pytest.mark.parametrize("sep", ["(", ")"])
def test_fused_parenthesis_separator_still_splits(tmp_path, guard_config, sep):
    """Found in self-review (auditor, #2267): `_GUARD_SEPARATOR_CHARS`
    includes `(`/`)` alongside the four idioms the issue names, and the
    fix's condition is keyed off that same set -- confirm the parenthesis
    shape is covered too, not just the four literal separators, so a
    future narrowing of the character set has a test to break."""
    guard_config(tmp_path, _PR_OP)
    cmd = f"true{sep}2>&1 gh pr view 1"
    heads, _unread, _origins, _origin_texts, _origin_faithful = (
        supertool._guard_segments_with_origins(cmd))
    assert heads == [["true"], ["gh", "pr", "view", "1"]], (cmd, heads)


def test_chained_numbered_fd_redirects_with_no_space_stay_one_segment(tmp_path):
    """Found in self-review (reviewer, #2267): `_GUARD_SEPARATOR_CHARS` also
    contains `<`/`>` themselves, so the fix's space-insertion condition
    also fires when a digit about to be dropped is immediately preceded by
    a redirect operator LEFT BEHIND by an earlier digit-drop in the same
    string -- back-to-back numbered fd redirects with no space
    (`1>2>&1`). This must still tokenize as ONE segment (a redirect
    chain, not a separator), same as before the fix -- the extra space the
    fix inserts here changes what the intermediate text looks like but
    must never turn a redirect chain into a phantom command boundary."""
    heads, _unread, _origins, _origin_texts, _origin_faithful = (
        supertool._guard_segments_with_origins("git status 1>2>&1"))
    assert heads == [["git", "status"]], heads


def test_spans_aligned_no_longer_falls_back_for_the_fused_case(tmp_path):
    """The entangled #2266 finding: once the fusion is fixed, the fidelity
    machinery's `spans_aligned` check should find the two span lists the
    same length again for this class, so `origin_texts`/`origin_faithful`
    render normally instead of hitting the word-rejoin fallback."""
    heads, _unread, _origins, origin_texts, origin_faithful = (
        supertool._guard_segments_with_origins("true|2>&1 gh pr view 1"))
    assert heads == [["true"], ["gh", "pr", "view", "1"]], heads
    assert origin_faithful == [True, True], origin_faithful
    # The raw text a caller actually typed for the second segment is
    # `2>&1 gh pr view 1` -- the redirect sits BEFORE the command word in
    # what was written (a normal, if unusual, shell idiom), and this is
    # the UNDROPPED slice (#2195), so it renders byte for byte rather than
    # a re-join.
    assert origin_texts == ["true", "2>&1 gh pr view 1"], origin_texts
