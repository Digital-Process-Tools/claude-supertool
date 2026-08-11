"""1400 — `help:OP` printed the colon form and stopped, so the `@-` payload
route was discoverable only by guessing.

An entry that lists one of two invocation forms is worse than no entry: it
reads as complete. `help:paste` said `paste:::PATH:::CONTENT` and nothing else,
and the implementation agent that needed the route to write a multi-line file
guessed `path` / `content` and said so in its report.

The field names are **derived** from the `syntax` string (`_fields_from_syntax`,
ref #770), so a hand-written list in `help:` would be a second copy of a rule
the product already computes — the shape #1363 and #1347 spent a release
removing. These tests pin that the block is rendered from the registries that
drive the route, and that it covers the read ops too: theirs live in
`_READ_OP_AT_FIELDS` rather than in a `:::` syntax, and silence about a route
that exists reads as a route that does not.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import supertool

MARKER = "Payload route"
ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _real_config(monkeypatch):
    """Undo conftest's config blackout for this file only.

    `_disable_rtk_and_config` pins `_CONFIG = {}`, under which `help:` answers
    ERROR for every op and every assertion below would pass vacuously. `help:`
    reads config by definition — there is nothing to test without one.
    """
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    monkeypatch.setattr(supertool, "_AT_FILE_REGISTRY", {})
    monkeypatch.setattr(supertool, "_AT_FILE_REGISTRY_BUILT", False)


def _documented_route_ops() -> list:
    """Every op with an @payload route that `help:` can answer at all."""
    return [name for name in supertool._at_file_route_ops()
            if not supertool.op_help(name).startswith("ERROR")]


def test_help_paste_names_the_payload_fields_it_derives() -> None:
    out = supertool.op_help("paste")
    assert MARKER in out
    assert "paste:@-" in out
    assert "path" in out
    assert "content" in out


def test_help_grep_names_its_read_op_payload_fields() -> None:
    """The read ops route through a second registry and were the half most
    likely to be left silent."""
    out = supertool.op_help("grep")
    assert MARKER in out
    assert "grep:@-" in out
    for field in supertool._READ_OP_AT_FIELDS["grep"]:
        assert field in out, field


def test_every_payload_capable_op_lists_every_field() -> None:
    ops = _documented_route_ops()
    # A sweep over an empty list is a green that means nothing, and this file
    # runs under a fixture that can produce exactly that.
    assert "paste" in ops and "grep" in ops and "vim" in ops
    assert len(ops) >= 10, ops
    for op_name in ops:
        out = supertool.op_help(op_name)
        assert MARKER in out, f"{op_name}: help has no payload route block"
        fields = (supertool._at_file_fields(op_name)
                  or list(supertool._READ_OP_AT_FIELDS.get(op_name, ())))
        assert fields, op_name
        for field in fields:
            assert field in out, f"{op_name}: payload field {field!r} not named"


def test_an_op_with_no_payload_route_gets_no_block() -> None:
    """Silence has to mean one thing. Now that the block is derived for every
    op that has the route, its absence is a claim there is none — so an op
    without one must not print a header with nothing under it."""
    out = supertool.op_help("version")
    assert MARKER not in out
