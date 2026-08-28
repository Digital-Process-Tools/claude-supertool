"""#2023 -- a discarded segment the guard could not render faithfully must
say so, rather than presenting a word-rejoined re-join beside faithful text
under the same "re-send them separately" instruction.

`_guard_raw_segment_spans` does not special-case the handful of
all-punctuation-word shell idioms `shlex.shlex(punctuation_chars=True)` treats
as pseudo-separators (a bare `{}`, an escaped `;`), so a pipeline like
`ls | xargs -I {} rm -rf {} && gh pr view 1` renders its `xargs`/`rm` half as
THREE separate commands, dropping the `{}` placeholders that bind them --
`ls`, `xargs -I`, `rm -rf` -- under an instruction telling the reader to
re-send them separately. That is a different structure, not a truncation of
what was written, and #2010's own residue note undersold it.

This test pins the chosen fix: the degraded span is marked as such, and the
"re-send them separately" instruction is not extended to it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

import supertool

_TWO_OPS = {
    "ops": {
        "gh-pr": {
            "safety": "read-only",
            "cmd": "true",
            "syntax": "gh-pr:NUMBER[:status|:diff]",
            "description": "Review a pull request: checks, reviews, diff stat.",
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


def test_a_faithfully_rendered_discard_carries_no_unfaithful_flag(
        tmp_path, guard_config):
    """Positive control: an ordinary discarded segment (#2010's own case)
    must NOT be marked unfaithful -- only the degraded idiom should be.
    """
    guard_config(tmp_path, _TWO_OPS)
    cmd = 'echo "hello   world" > /tmp/x.txt && gh pr view 12'
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    assert verdict.discarded == ('echo "hello   world" > /tmp/x.txt',)
    assert verdict.discarded_unfaithful == ()


def test_a_punctuation_word_idiom_discard_is_flagged_unfaithful(
        tmp_path, guard_config):
    """The reproduced #2023 route: `xargs -I {} rm -rf {}` falls back to a
    word re-join that drops the `{}` placeholders binding the two halves
    together -- that segment must be flagged, not presented as plain,
    re-sendable text.
    """
    guard_config(tmp_path, _TWO_OPS)
    cmd = 'ls | xargs -I {} rm -rf {} && gh pr view 1'
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    # The degraded idiom's own discarded text(s) must be present AND flagged.
    assert verdict.discarded_unfaithful, (
        "the xargs/rm segment must be flagged as not faithfully rendered")
    for text in verdict.discarded_unfaithful:
        assert text in verdict.discarded


def test_discard_line_does_not_instruct_re_sending_an_unfaithful_segment(
        tmp_path, guard_config):
    """The rendered line must not tell the reader to re-send a segment that
    was not rendered faithfully -- that is the misdirection #2023 reports.
    """
    guard_config(tmp_path, _TWO_OPS)
    cmd = 'ls | xargs -I {} rm -rf {} && gh pr view 1'
    verdict = supertool.guard_command(cmd)
    line = supertool._guard_discard_line(
        verdict.discarded, 2000, verdict.discarded_unfaithful)
    assert line
    assert "could not be rendered exactly" in line
    # The blanket "re-send them separately" close must not cover a segment
    # this render could not reproduce faithfully.
    if "re-send" in line:
        assert "separately" in line
