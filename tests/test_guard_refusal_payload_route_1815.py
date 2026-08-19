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


class TestARouteTooBigForTheBudgetIsCountedNotDropped:
    """Three causes, one empty string — this repo's own defect, in the fix.

    `_guard_route_for` returned "" for an op with no route, for an op whose
    route did not fit `_GUARD_TEXT_BUDGET`, and for a registry lookup that
    raised. The first is an answer; the other two are the route going missing
    with nothing said, which is what #1815 was filed about one layer up.
    Found by the audit of this diff, not by the issue.
    """

    @staticmethod
    def _match(op, use, description, command):
        return supertool.GuardMatch(op=op, use=use, description=description,
                                    argv=command, command=command)

    def _verdict(self, n, use_len=200, desc_len=400):
        return supertool.GuardVerdict(
            state="blocked",
            matches=tuple(
                self._match(f"probe-{i}", "x" * use_len, "d" * desc_len,
                            f"cmd{i}")
                for i in range(n)),
            notes=())

    @pytest.fixture
    def every_op_has_a_route(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(supertool, "_at_file_specs",
                            lambda op: [("message", False, False)])

    #: The marker deliberately does NOT reuse the wording of the existing
    #: "further replaced invocation(s) ... are not detailed here" line, which
    #: fires on the same crowded refusal for a different reason. The first
    #: draft of these tests asserted on that phrase and matched the wrong
    #: line — two withholdings that render identically is the same conflation
    #: this class is about.
    WITHHELD = "payload route(s) were not shown"

    #: Long `use` strings and short descriptions: the shape that walks `spent`
    #: up to just under `_GUARD_TEXT_BUDGET` so a later match is rendered with
    #: too little room for its own route, rather than the loop breaking first.
    #: The first draft of these tests used long descriptions and never reached
    #: the branch at all -- it went red proving the fixture wrong, not the code.
    #:
    #: **No shipped registry produces this shape**, measured: the `use` strings
    #: in presets/*.json run to ~40 characters and their descriptions are
    #: capped at 320, so three matches fit with room for every route and the
    #: fourth is already past the budget. It takes a project `.supertool.json`
    #: declaring a `use` several hundred characters long -- which is exactly
    #: the registry-authored text `_guard_quote` and #1391 already treat as
    #: something a stranger wrote. So this class is a boundary, reasoned from
    #: the branch conditions and then pinned, not a live user-facing scenario.
    CROWDED = dict(use_len=200, desc_len=5)

    def test_the_withheld_routes_are_counted(self, every_op_has_a_route):
        text = supertool.guard_refusal(self._verdict(5, **self.CROWDED))
        assert "Payload route: supertool" in text, text
        assert self.WITHHELD in text, text

    def test_the_count_names_how_many(self, every_op_has_a_route):
        text = supertool.guard_refusal(self._verdict(5, **self.CROWDED))
        shown = text.count("Payload route: supertool")
        rendered = text.count("is replaced by supertool")
        withheld = [line for line in text.splitlines()
                    if self.WITHHELD in line]
        assert len(withheld) == 1, text
        # Counted against the matches that were RENDERED, not against all
        # five: a match the budget never reached is already accounted for by
        # the "further replaced invocation(s)" line, and counting it twice
        # would overstate what went missing.
        assert str(rendered - shown) in withheld[0], (rendered, shown, withheld)

    def test_an_op_with_no_route_is_not_counted_as_withheld(
            self, monkeypatch: pytest.MonkeyPatch):
        # The must-not-fire half. Its partner is the class above: without one
        # of them, a line that never renders and a line that always renders
        # both look correct.
        monkeypatch.setattr(supertool, "_at_file_specs", lambda op: [])
        text = supertool.guard_refusal(self._verdict(5, **self.CROWDED))
        assert self.WITHHELD not in text, text

    def test_and_a_route_that_fits_is_still_printed_in_full(
            self, every_op_has_a_route):
        # The other partner: a budget that is not under pressure must still
        # render the whole line, keys and all.
        text = supertool.guard_refusal(self._verdict(1, use_len=10))
        assert "Payload route: supertool 'probe-0:@-' — keys: message" in text
        assert self.WITHHELD not in text, text

    def test_a_registry_that_raises_is_not_read_as_no_route(
            self, monkeypatch: pytest.MonkeyPatch):
        def boom(op):
            raise RuntimeError("registry is broken")
        monkeypatch.setattr(supertool, "_at_file_specs", boom)
        text = supertool.guard_refusal(self._verdict(1, use_len=10))
        # The refusal still renders — a denial that raises is a denial nobody
        # sees — but it does not claim the op has no payload route.
        assert "probe-0" in text, text
        assert "could not be read" in text, text


class TestAntiVacuity:
    """Without these, every assertion above is about a dead registry."""

    def test_the_op_really_has_a_payload_route(self, shipped_presets):
        assert supertool._at_file_specs("git-commit") == [
            ("message", False, False), ("paths", True, True)]

    def test_the_command_really_is_blocked(self, shipped_presets):
        assert supertool.guard_command("git commit -F -").state == "blocked"
