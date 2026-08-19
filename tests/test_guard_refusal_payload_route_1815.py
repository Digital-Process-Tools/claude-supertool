"""The refusal truncated its contract before the `@payload` route (#1815).

    $ supertool 'guard:git commit -F -'
    BLOCKED
    `git commit -F -` is replaced by supertool's `git-commit` op.
      Use: supertool 'git-commit:::MESSAGE[:::PATHS...|:::--all]'
      Commit MESSAGE (stages PATHS) — ...… (+3143 chars)

`git commit -F -` is the raw route for a **multi-line** message, and the op's
only multi-line route is `git-commit:@-`. That sentence lives 3.5KB into the
description, so `_GUARD_DESC_CAP` cut it: the caller was refused for using the
one raw form that takes a body, pointed at an op, and shown a contract whose
visible half does not contain the op's own multi-line form. The next call is a
`help:git-commit` round-trip to recover the sentence the refusal was already
trying to deliver.

Truncation here is position-based; relevance is not. So the route is named on
its own line **above** the description rather than left inside it, and derived
from the same registry that drives `@-` (`_at_file_specs`) rather than typed —
a hand-written copy would keep advertising a route a syntax reword had deleted,
which is the failure `_help_payload_route` already carries a docstring about.

Would these pass if the code did nothing? No — at fa2ba903 the refusal for
`git commit -F -` contains no `git-commit:@-` anywhere, truncated or not.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool


@pytest.fixture
def shipped_presets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The real `presets/git.json` and `presets/github.json`, through the loader.

    `tests/conftest.py` disables config discovery for the whole suite, so a
    guard call with no config sees an empty registry and every assertion here
    would pass vacuously against a refusal that was never built.
    """
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"presets": ["git", "github"]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    return supertool._load_config()


def _refusal(command: str) -> str:
    verdict = supertool.guard_command(command)
    assert verdict.state == "blocked", verdict
    return supertool.guard_refusal(verdict)


class TestTheDefect:
    """The issue's own reproduction."""

    def test_the_refusal_names_the_payload_route(self, shipped_presets):
        assert "git-commit:@-" in _refusal("git commit -F -")

    def test_it_names_the_keys_the_payload_wants(self, shipped_presets):
        # Without these the caller still guesses `message` from scratch, which
        # is the round-trip #1003 measured on the same op.
        text = _refusal("git commit -F -")
        assert "message" in text, text
        assert "paths" in text, text

    def test_it_stands_above_the_description_that_gets_cut(
            self, shipped_presets):
        # The whole fix: position. The description IS truncated here, so a
        # route mentioned only inside it does not reach the caller.
        text = _refusal("git commit -F -")
        assert "… (+" in text, "the description is expected to truncate here"
        assert text.index("git-commit:@-") < text.index("… (+"), text

    def test_the_route_line_is_not_itself_the_truncated_half(
            self, shipped_presets):
        # `-F -` and `-m x` are two spellings of the same refusal; the route
        # must survive both, not only the shortest one.
        for command in ("git commit -F -", "git commit -m x",
                        "git commit -F msg.txt"):
            assert "git-commit:@-" in _refusal(command), command


class TestAnOpWithNoRouteSaysNothing:
    """The must-not-fire half — with its must-fire partner in the same class.

    An empty payload line under every refusal is the disclosure nobody reads,
    and "" here is an answer rather than a gap (`_help_payload_route`).
    """

    def test_gh_pr_list_has_no_route_and_no_line(self, shipped_presets):
        # `gh-prs` syntax carries no ':::', so no `@payload` route exists.
        assert supertool._at_file_specs("gh-prs") == []
        text = _refusal("gh pr list")
        assert "gh-prs:@-" not in text, text
        assert "Payload route" not in text, text

    def test_and_the_same_refusal_does_carry_the_rest(self, shipped_presets):
        # The partner: without it the assertions above pass on an empty string.
        text = _refusal("gh pr list")
        assert "gh-prs" in text, text
        assert "Use: supertool" in text, text


class TestTheRouteLineIsBounded:
    """Registry text in a system-authored denial is quoted and capped (#1391)."""

    def test_it_is_one_line(self, shipped_presets):
        text = _refusal("git commit -F -")
        route = [line for line in text.splitlines() if "git-commit:@-" in line]
        assert len(route) == 1, text

    def test_a_hostile_field_name_cannot_forge_a_line(self, shipped_presets):
        # `_guard_quote` flattens; a raw join would let a project-defined op
        # write a line of its own inside the denial.
        line = supertool._guard_payload_route(
            "probe", [(chr(10).join(("a", "b")), False, False)])
        assert chr(10) not in line.strip(chr(10)), repr(line)


class TestAntiVacuity:
    """Without these, every assertion above is about a dead registry."""

    def test_the_op_really_has_a_payload_route(self, shipped_presets):
        assert supertool._at_file_specs("git-commit") == [
            ("message", False, False), ("paths", True, True)]

    def test_the_command_really_is_blocked(self, shipped_presets):
        assert supertool.guard_command("git commit -F -").state == "blocked"
