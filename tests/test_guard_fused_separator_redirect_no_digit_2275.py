"""#2275 -- a top-level separator fused (no space) to a redirect operator
still hides the following command, even with no digit involved at all.

#2267 fixed this same shape only for the sub-case where `_guard_drop_io_numbers`
had just DROPPED a digit and the space it inserted there was the only defence
(`|2>&1` -> `|>&1`). It never touched the underlying gap: `_guard_raw_segment_spans`
and `_guard_tokenize_prepared` both decided "this whole punctuation run is one
redirect operator, don't split" by asking only whether `<`/`>` appeared
ANYWHERE in the run -- so a bare `;>`, `|>`, `&&>`, `&>`, `;>>`, `;<`, with no
digit anywhere near them, fused exactly the same way #2267 fixed for the
digit-adjacent case, and the guard read `true;>out git commit -m x` as ONE
command (`true`, with `git commit -m x` swallowed as extra words) rather than
two.

Reproduced directly, pre-fix, exactly as the issue table lists them:

    guard_command('true;>out git commit -m x')       -> clean   (should block)
    guard_command('true|>out git commit -m x')        -> clean   (should block)
    guard_command('true&&>out git commit -m x')       -> clean   (should block)
    guard_command('true&>out git commit -m x')        -> clean   (should block)
    guard_command('true;>>out git commit -m x')       -> clean   (should block)
    guard_command('true;<in git commit -m x')         -> clean   (should block)
    guard_command('true; >out git commit -m x')       -> blocked (already correct)

Chosen fix: `_guard_classify_separator_run` reads a maximal
`_GUARD_SEPARATOR_CHARS` run character by character rather than asking only
whether it contains a redirect char, and both `_guard_raw_segment_spans` and
`_guard_tokenize_prepared` now split at the boundary it finds inside the run
instead of only at the run's own ends. `&` is the one ambiguous character
(part of `&&`/bare `&` — a separator — or part of `<&`/`>&` — a redirect
fd-duplication operator that #1684/#2195 already depend on staying fused);
see `_guard_classify_separator_run`'s own docstring for why the ambiguous
`&>`/`&>>` shape is read as separator-then-redirect rather than as bash's own
combined-redirect extension.
"""
from __future__ import annotations

import pytest

import supertool


@pytest.mark.parametrize("cmd", [
    "true;>out git commit -m x",
    "true|>out git commit -m x",
    "true&&>out git commit -m x",
    "true&>out git commit -m x",
    "true;>>out git commit -m x",
    "true;<in git commit -m x",
])
def test_no_space_separator_redirect_fusion_with_no_digit_is_blocked(
        with_preset_op, cmd):
    """Every "should BLOCK" row from the issue table, none of which involve
    a digit anywhere -- the pure #2275 shape, distinct from #2267's."""
    with_preset_op("git-commit")
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", (cmd, verdict)


@pytest.mark.parametrize("cmd", [
    "true;>out git commit -m x",
    "true|>out git commit -m x",
    "true&&>out git commit -m x",
    "true&>out git commit -m x",
    "true;>>out git commit -m x",
    "true;<in git commit -m x",
])
def test_no_space_separator_redirect_fusion_splits_into_two_segments(cmd):
    """Checked at the tokenization layer directly, which needs no ops
    config at all: `true` and the `git commit -m x` segment must come back
    as two separate `heads` entries, not one fused command with
    `git commit -m x` swallowed as extra words of `true`."""
    heads, _unread, _origins, _origin_texts, _origin_faithful = (
        supertool._guard_segments_with_origins(cmd))
    assert heads[0] == ["true"], (cmd, heads)
    assert heads[-1][:2] == ["git", "commit"], (cmd, heads)
    assert len(heads) == 2, (cmd, heads)


def test_spaced_idiom_is_already_correct_positive_control(with_preset_op):
    """From the issue table: one space defeats the fusion already, on
    master. Confirms the fix does not disturb the already-correct case."""
    with_preset_op("git-commit")
    cmd = "true; >out git commit -m x"
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    heads, _unread, _origins, _origin_texts, _origin_faithful = (
        supertool._guard_segments_with_origins(cmd))
    assert heads == [["true"], ["git", "commit", "-m", "x"]], heads


def test_a_command_with_no_separator_redirect_fusion_at_all_stays_clean(
        with_preset_op):
    """Positive control (a silence assertion needs a paired 'must fire'
    case, or it can be passing on a broken harness): a bare, ordinary
    command with no separator anywhere near a redirect must render exactly
    as it did before this fix -- clean."""
    with_preset_op("git-commit")
    verdict = supertool.guard_command("echo hello > out.txt")
    assert verdict.state == "clean", verdict


def test_fd_duplication_redirects_still_do_not_split():
    """Regression guard: `>&`/`<&` (fd duplication -- `2>&1`, `1>&2`) are
    the ONE case where `&` genuinely is part of the redirect operator, not
    a separator, and #1684/#2195's own fixtures depend on this staying
    fused. Must remain a single segment after this fix, exactly as before
    it. Needs no ops config -- purely a tokenization-layer check."""
    heads, _unread, _origins, _origin_texts, _origin_faithful = (
        supertool._guard_segments_with_origins("git status 1>&2 2>&1"))
    assert heads == [["git", "status"]], heads


def test_classify_separator_run_unit_cases():
    """`_guard_classify_separator_run` directly, the smallest possible
    surface for the decision this fix makes: which runs split, and where."""
    classify = supertool._guard_classify_separator_run
    # Pure separator runs: unaffected, whole run is one separator token.
    assert classify(";") == [(0, 1, True)]
    assert classify("&&") == [(0, 2, True)]
    assert classify("||") == [(0, 2, True)]
    # Pure redirect runs: unaffected, whole run is one non-separator token.
    assert classify(">>") == [(0, 2, False)]
    assert classify(">&") == [(0, 2, False)]
    assert classify("<&") == [(0, 2, False)]
    # Mixed runs: split exactly at the separator/redirect boundary.
    assert classify(";>") == [(0, 1, True), (1, 2, False)]
    assert classify("|>") == [(0, 1, True), (1, 2, False)]
    assert classify("&&>") == [(0, 2, True), (2, 3, False)]
    assert classify(";<") == [(0, 1, True), (1, 2, False)]
    # The ambiguous one: bare `&` immediately before a redirect char reads
    # as separator-then-redirect, never as bash's own `&>` operator.
    assert classify("&>") == [(0, 1, True), (1, 2, False)]
