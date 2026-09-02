"""#2076 -- GuardMatch.command loses the caller's quoting/pipe text, so the
refusal's own render line for the MATCHED command (not the discarded ones,
already fixed by #2010/#2017) reads as two shell commands.

`git commit -m "a; rm -rf /tmp/x"` is one safe command. Before this fix,
`GuardMatch.command` was a plain space-join of tokenised argv, so the
refusal rendered `git commit -m a; rm -rf /tmp/x` -- indistinguishable from
two commands, the second one destructive-looking. Same for a pipe hidden
inside a quoted argument.

The fix reuses `origin_texts`/`origin_faithful` -- already threaded through
`guard_command` for the sibling `discarded` field by #2010/#2017/#2023 --
for `GuardMatch` too, via `origins[head_index]`.
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


def test_semicolon_inside_a_quoted_arg_is_not_split_across_two_commands(
        tmp_path, guard_config):
    guard_config(tmp_path, _COMMIT_OP)
    cmd = 'git commit -m "a; rm -rf /tmp/x"'
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    refusal = supertool.guard_refusal(verdict)
    # The original quoted text is preserved -- the semicolon reads as part
    # of the commit message, not as a second shell command.
    assert 'git commit -m "a; rm -rf /tmp/x"' in refusal, refusal
    # The lossy argv-join rendering must not appear anywhere in the refusal.
    assert 'git commit -m a; rm -rf /tmp/x' not in refusal, refusal


def test_pipe_inside_a_quoted_arg_is_not_rendered_as_a_real_pipe(
        tmp_path, guard_config):
    guard_config(tmp_path, _COMMIT_OP)
    cmd = 'git commit -m "hi | there"'
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    refusal = supertool.guard_refusal(verdict)
    assert 'git commit -m "hi | there"' in refusal, refusal
    assert 'git commit -m hi | there' not in refusal, refusal


def test_guard_match_command_faithful_flag_defaults_true(
        tmp_path, guard_config):
    """GuardMatch gains the same origin/faithful pair `discarded` already
    carries -- the field is `command`, populated from the raw origin text,
    with `command_faithful` marking whether that was a faithful slice."""
    guard_config(tmp_path, _COMMIT_OP)
    cmd = 'git commit -m "a; rm -rf /tmp/x"'
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    match = verdict.matches[0]
    assert match.command == 'git commit -m "a; rm -rf /tmp/x"'
    assert match.command_faithful is True


def test_matched_command_unfaithful_marker_uses_the_established_dash(
        tmp_path, guard_config):
    """#2076's own gap, found in review: `command_faithful=False` must be
    marked the same way -- byte for byte -- as the established
    `_guard_discard_line` convention (#2023) it says it mirrors. A
    matched segment can hit the same word-rejoin fallback `discarded`
    already covers -- `xargs -I` inside a punctuation-word idiom -- and the
    render must not present that reconstruction as the caller's own text.
    """
    config = {
        "ops": {
            "xargs-run": {
                "safety": "write",
                "cmd": "true",
                "syntax": "xargs-run:CMD",
                "description": "Run CMD once per input line.",
                "replaces": [
                    {"argv": "xargs -I", "use": "xargs-run:CMD"},
                ],
            },
        }
    }
    guard_config(tmp_path, config)
    cmd = 'xargs -I {} rm -rf {}'
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    match = verdict.matches[0]
    assert match.command_faithful is False, match
    refusal = supertool.guard_refusal(verdict)
    assert "could not be rendered exactly" in refusal, refusal
    # Byte-for-byte the same dash/wording `_guard_discard_line` already
    # uses for the identical fallback (#2023) -- an ASCII "--" here would
    # silently diverge from that established marker.
    assert "\u2014 do not re-send as shown" in refusal, refusal


def test_discarded_line_tests_still_pass_unaffected(tmp_path, guard_config):
    """Sanity: the sibling `discarded` field's own render is untouched --
    this fix only threads the already-computed origin data to `GuardMatch`,
    it does not change `_guard_discarded_segments` or `_guard_discard_line`."""
    config = {
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
    guard_config(tmp_path, config)
    cmd = 'echo "hello   world" > /tmp/x.txt && gh pr view 12'
    verdict = supertool.guard_command(cmd)
    assert verdict.state == "blocked", verdict
    assert verdict.discarded == (
        'echo "hello   world" > /tmp/x.txt',), verdict.discarded
