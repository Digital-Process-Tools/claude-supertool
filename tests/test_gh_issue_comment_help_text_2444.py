"""#2444 -- `help:gh-issue-comment` documented an `args` payload the op
does not accept.

`gh-issue-comment` takes `@FILE`/`@-` with a NAMED-field payload it parses
itself (`presets/github/issue_comment.py`'s own `ACCEPTED_KEYS = {"repo",
"body", "body_file"}`) -- one of the four ops `repo_target: "payload"`
marks as owning its own payload shape (#2408). `_at_file_payload_hint`,
the function that renders the `Payload route --` block under `help:OP`,
had no branch for that population: its only two cases were a `:::`-derived
named-field registry (`_AT_FILE_REGISTRY`, empty for this op -- the syntax
string is `gh-issue-comment:NUMBER:@FILE | gh-issue-comment:NUMBER:@-`,
with no `:::` fields to derive names from) and the generic args-list
preset route (#1165). So it fell to the generic branch and told a caller
to send `args = [...]`, which the op's own loader refuses outright:

    $ supertool 'gh-issue-comment:809:@comment.toml'   # payload: args = [...]
    ERROR: gh-issue-comment payload carries unrecognised key 'args' --
    nothing was written. Accepted keys: 'body', 'body_file', 'repo'.

Measured live against issue #809, 2026-09-08 (#2444's own filing).

The fix adds `_PRESET_NAMED_PAYLOAD_FIELDS`, a small registry the same
shape as `_READ_OP_AT_FIELDS` already uses for read ops, checked before
the generic-args fallback. This file pins the shipped help text AND pins
the registry against the op's own `ACCEPTED_KEYS` so the two cannot drift
apart the same way the wrong hint text did.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

import supertool

_Q3 = chr(39) * 3
_ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _real_config(monkeypatch):
    """Undo conftest's config blackout for this file only -- `help:` reads
    config by definition, and `tests/test_help_payload_route_1400.py`
    establishes this exact fixture for the same reason.
    """
    monkeypatch.chdir(_ROOT)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    monkeypatch.setattr(supertool, "_AT_FILE_REGISTRY", {})
    monkeypatch.setattr(supertool, "_AT_FILE_REGISTRY_BUILT", False)


def _load_preset_module(relpath: str, name: str):
    """Import a `presets/...py` module in isolation, purely to read a
    module-level constant (`ACCEPTED_KEYS`) -- never executes `main()`.
    Generalised from the gh-issue-comment-only loader this file shipped
    with, once self-review found the same #2444 defect live against the
    three sibling ops below.
    """
    repo_root = Path(supertool.__file__).resolve().parent
    mod_path = repo_root / "presets" / relpath
    spec = importlib.util.spec_from_file_location(name, mod_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_issue_comment_module():
    return _load_preset_module("github/issue_comment.py", "_test_issue_comment_2444")


class TestHelpTextNamesTheRealKeys:
    """MUST fire: the shipped help text for gh-issue-comment names its
    actual accepted keys, never the generic `args` shape.
    """

    def test_help_shows_the_real_payload_keys(self) -> None:
        out = supertool.dispatch("help:gh-issue-comment")
        assert "body_file" in out
        assert ("body = " + _Q3) in out

    def test_help_does_not_offer_the_wrong_args_shape(self) -> None:
        out = supertool.dispatch("help:gh-issue-comment")
        assert "has no named payload fields" not in out
        assert "reads its argv from 'args'" not in out
        assert ("args = [" + _Q3) not in out


class TestHelpTextPositiveControl:
    """MUST NOT regress: an op that genuinely has no named fields (the
    #1165 population this bug's fix must not touch) still gets the
    generic `args` hint -- proves the branch this file pins is a real
    fork, not a change that silently ate the other side of it.
    """

    def test_generic_args_only_op_is_unaffected(self) -> None:
        out = supertool.dispatch("help:gh-job")
        assert "reads its argv from 'args'" in out


class TestRegistryPinnedAgainstTheRealOp:
    """The registry that drives the fixed help text must never itself go
    stale against the op it describes -- the same failure mode #2444 was.
    """

    def test_registry_matches_the_ops_own_accepted_keys(self) -> None:
        mod = _load_issue_comment_module()
        assert supertool._PRESET_NAMED_PAYLOAD_FIELDS["gh-issue-comment"] == (
            "body", "body_file", "repo",
        )
        assert set(
            supertool._PRESET_NAMED_PAYLOAD_FIELDS["gh-issue-comment"]
        ) == mod.ACCEPTED_KEYS


class TestSiblingOpsShareTheSameFixAndFixture:
    """Self-review (reviewer AND auditor, independently) found the #2444
    fix own new code comment naming three sibling ops -- gh-issue-create,
    gh-pr-create, gh-pr-edit -- as sharing the identical broken-help-text
    shape, while the first pass of this fix only added gh-issue-comment to
    the registry. Verified live against the pre-round-2 code: help colon
    gh-issue-create and help colon gh-pr-edit both still printed reads its
    argv from args after the #2444 first commit. This class closes that
    gap and pins all three the same way the class above pins
    gh-issue-comment.
    """

    @pytest.mark.parametrize(
        "op",
        ["gh-issue-create", "gh-pr-create", "gh-pr-edit"],
    )
    def test_help_shows_the_real_payload_keys(self, op: str) -> None:
        out = supertool.dispatch("help:" + op)
        assert "has no named payload fields" not in out
        assert "reads its argv from 'args'" not in out
        assert ("args = [" + _Q3) not in out

    @pytest.mark.parametrize(
        "op,relpath,modname",
        [
            ("gh-issue-create", "github/issue_create.py", "_test_issue_create_2444"),
            ("gh-pr-create", "github/pr_create.py", "_test_pr_create_2444"),
            ("gh-pr-edit", "github/pr_edit.py", "_test_pr_edit_2444"),
        ],
    )
    def test_registry_matches_each_ops_own_accepted_keys(
        self, op: str, relpath: str, modname: str
    ) -> None:
        mod = _load_preset_module(relpath, modname)
        registered = supertool._PRESET_NAMED_PAYLOAD_FIELDS[op]
        assert set(registered) == mod.ACCEPTED_KEYS
