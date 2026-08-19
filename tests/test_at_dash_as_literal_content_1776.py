"""`@-` in a content field is refused, not written over the file (#1776).

`paste:PATH:@-` did not read stdin. It wrote the two characters `@-` over the
file and reported `rewrote victim.txt (1 lines, 39 -> 3 bytes)`. `append`,
`edit`, `replace` and `replace_lines` had the same shape: the @payload route
is gated on `parts[1]`, so a `@-` in any LATER field fell through to the
handlers as ordinary content.

The caller gets there by generalising a convention the tool uses everywhere
else -- `edit:@-`, `gh-issue-create:@FILE` -- onto the field position where it
was never wired. What came back was a success receipt, and the only thing that
distinguished it from a real write was a byte count.

What is asserted here is the FILE, never the receipt: a test that reads the
refusal text would pass just as well if the op stopped writing altogether.
Each refusal case is therefore paired with a case that must still write --
both colon forms, and the payload route that is the documented way to send a
multi-line body.
"""
import io
import json
import sys
from pathlib import Path

import pytest

import _supertool
import supertool

NL = chr(10)
Q3 = chr(39) * 3
REPO_ROOT = Path(__file__).resolve().parent.parent

VICTIM = "IMPORTANT EXISTING CONTENT" + NL + "second line" + NL


def _victim(tmp_path: Path, name: str = "victim.txt") -> Path:
    # write_bytes, not write_text: Windows text mode rewrites NL to CRLF, which
    # would put the fixture one byte per line away from what is asserted (the
    # bug class of #1004).
    p = tmp_path / name
    p.write_bytes(VICTIM.encode("utf-8"))
    return p


def _stdin(monkeypatch, text: str) -> None:
    """Give the call a real stdin, so the refusal is not merely the absence of
    one. On master this is never read -- that is the defect."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def _toml_path(target: Path) -> str:
    return chr(34) + str(target).replace(chr(92), chr(92) * 2) + chr(34)


# --- the destruction, asserted on disk ---------------------------------------

def test_paste_at_dash_does_not_overwrite_the_file(tmp_path, monkeypatch):
    v = _victim(tmp_path)
    _stdin(monkeypatch, "new one" + NL + "new two" + NL)
    out = supertool.dispatch("paste:" + str(v) + ":@-")
    assert v.read_bytes() == VICTIM.encode("utf-8"), out
    assert "ERROR" in out


def test_append_at_dash_does_not_touch_the_file(tmp_path, monkeypatch):
    v = _victim(tmp_path)
    _stdin(monkeypatch, "appended" + NL)
    out = supertool.dispatch("append:" + str(v) + ":@-")
    assert v.read_bytes() == VICTIM.encode("utf-8"), out
    assert "ERROR" in out


def test_edit_at_dash_does_not_write_the_literal(tmp_path, monkeypatch):
    v = _victim(tmp_path)
    _stdin(monkeypatch, "replacement" + NL)
    out = supertool.dispatch("edit:second line:@-:" + str(v))
    assert v.read_bytes() == VICTIM.encode("utf-8"), out
    assert "ERROR" in out


def test_replace_lines_at_dash_does_not_write_the_literal(tmp_path, monkeypatch):
    v = _victim(tmp_path)
    _stdin(monkeypatch, "replacement" + NL)
    out = supertool.dispatch("replace_lines:" + str(v) + ":1:1:@-")
    assert v.read_bytes() == VICTIM.encode("utf-8"), out
    assert "ERROR" in out


def test_replace_at_dash_does_not_write_the_literal(tmp_path, monkeypatch):
    v = _victim(tmp_path)
    _stdin(monkeypatch, "replacement" + NL)
    out = supertool.dispatch("replace:second line:@-:" + str(v))
    assert v.read_bytes() == VICTIM.encode("utf-8"), out
    assert "ERROR" in out


def test_replace_dry_at_dash_is_refused(tmp_path, monkeypatch):
    """`replace_dry` writes nothing either way, so this is not about the file.
    It is about the scope claim: the guard names eight ops and five of them had
    a test, so a future edit could drop the other three and stay green."""
    v = _victim(tmp_path)
    _stdin(monkeypatch, "replacement" + NL)
    out = supertool.dispatch("replace_dry:second line:@-:" + str(v))
    assert v.read_bytes() == VICTIM.encode("utf-8"), out
    assert "ERROR" in out and "replace_dry:@-" in out


def test_vim_at_dash_is_refused_by_the_guard_not_by_the_verb_parser(
        tmp_path, monkeypatch):
    """`vim` declined this before the guard existed -- its script parser has no
    verb `@`. That is a loud failure that names the wrong thing, so what is
    pinned is WHICH refusal answers: the one that names `vim:@-`."""
    v = _victim(tmp_path)
    _stdin(monkeypatch, "ggdG" + NL)
    out = supertool.dispatch("vim:" + str(v) + ":@-")
    assert v.read_bytes() == VICTIM.encode("utf-8"), out
    assert "vim:@-" in out
    assert "unknown verb" not in out


def test_the_field_named_in_the_message_is_the_one_that_holds_the_sigil():
    """The name is read off the registry by position, so a message naming the
    wrong field means the mapping has drifted. Two ops, two positions, because
    one op cannot tell an off-by-one from a coincidence.

    The guard is called directly rather than through `dispatch` here and below.
    That is not a convenience: for `git-commit` the dispatch form can only run
    inside a real repository -- a preset op is unavailable elsewhere and that
    check answers first -- so a guard that regressed would COMMIT the working
    tree the suite runs in."""
    edit_out = _supertool._stdin_ref_in_value_field(
        "edit", ["edit", "old text", "@-", "f.txt"])
    assert "the new field is" in edit_out, edit_out
    rl_out = _supertool._stdin_ref_in_value_field(
        "replace_lines", ["replace_lines", "f.txt", "1", "1", "@-"])
    assert "the content field is" in rl_out, rl_out


@pytest.fixture
def git_commit_route(with_preset_op):
    """Make `git-commit`'s payload route exist for the duration of one test.

    Without this the op is invisible here, always. Its route is derived from
    the preset manifest rather than from `_AT_FILE_BUILTIN_DEFAULTS`, and
    `tests/conftest.py`'s autouse `_disable_rtk_and_config` sets
    `supertool._CONFIG = {}` for every test in the suite -- so
    `_at_file_fields("git-commit")` is empty on every invocation, not on some
    of them. An earlier version of this file skipped when the route was absent
    and called that honest; it is not, because the condition is never false.
    A test that skips on every run in CI reads as coverage in the count and
    asserts nothing, which is the defect class this whole file exists about,
    sitting inside the fix for it.

    The mechanics moved to `conftest.with_preset_op` in #1812, which is the
    same argument made once for the whole suite rather than rediscovered per
    file: the entry still comes off the shipped manifest rather than being
    typed, so rewording `syntax` until `_fields_from_syntax` stops deriving
    clean identifiers still reddens instead of skipping, and `payload_route=
    True` is what says out loud that this file needs the route and not merely
    the op.
    """
    yield with_preset_op("git-commit", payload_route=True)["git-commit"]["syntax"]


def test_git_commit_at_dash_is_refused(git_commit_route):
    """`git-commit` is the most destructive op the guard covers and the only one
    whose membership in the registry is derived rather than declared."""
    # The route is really there now -- assert that before assuming it, or this
    # test degrades back into the silent pass it replaced.
    assert _supertool._at_file_fields("git-commit") == ["message", "paths"]

    out = _supertool._stdin_ref_in_value_field(
        "git-commit", ["git-commit", "a message", "@-"])
    assert "ERROR" in out
    assert "git-commit:@-" in out
    # `paths`, not `message` and not `content`: the name is read off the
    # registry by position, so a message naming another field means the
    # derivation drifted.
    assert "the paths field is" in out, out

    # And the guard stays quiet where it should: `git-commit:@-` is the payload
    # reference itself, intercepted upstream, never this refusal.
    assert _supertool._stdin_ref_in_value_field(
        "git-commit", ["git-commit"]) == ""


def _claim_sentence(op, field, parts):
    """The refusal's first line with the op and field names taken out of it.

    That line is where the whole claim lives; everything after it is the
    payload hint. Normalising the two names out is what lets one op's sentence
    be compared against another's.
    """
    out = _supertool._stdin_ref_in_value_field(op, parts)
    assert out, op
    return out.splitlines()[0].replace(op, "OP").replace(field, "FIELD")


def test_one_template_serves_every_op_so_it_cannot_claim_a_write(
        git_commit_route):
    """The message may not assert anything that is false for some guarded op.

    Three of them never put the field on disk: `replace_dry` is a preview,
    `vim`'s field is a macro script, `git-commit`'s is a pathspec. The
    protection is structural rather than a banned-word list -- assert that the
    sentence is the SAME for an op that writes and for ops that do not, so a
    consequence added for `paste` cannot be added without also being asserted
    about `git-commit`. A previous version of this test checked that the
    string `written to disk` was absent; that phrase was never in the message
    at all, so it would have passed against any rewrite whatsoever.
    """
    writes = _claim_sentence("paste", "content", ["paste", "f.txt", "@-"])
    for op, field, parts in (
        ("git-commit", "paths", ["git-commit", "a message", "@-"]),
        ("vim", "script", ["vim", "f.txt", "@-"]),
        ("replace_dry", "new", ["replace_dry", "a", "@-", "f.txt"]),
    ):
        assert _claim_sentence(op, field, parts) == writes, op

    # The template's own claim, stated positively: what it says happened is
    # that nothing did. This is the half that fails if the sentence is rewritten
    # into something true of `paste` alone.
    assert "Nothing ran." in writes, writes
    for lie in ("written to disk", "was written", "overwrit", "wrote"):
        assert lie not in writes, (lie, writes)


def test_triple_colon_at_dash_is_refused_too(tmp_path, monkeypatch):
    """The `:::` form is the documented literal-content route, but a caller who
    has mistyped the sigil there has mistyped it the same way -- and the escape
    hatch below still writes the two characters for anyone who means them."""
    v = _victim(tmp_path)
    _stdin(monkeypatch, "new" + NL)
    out = supertool.dispatch("paste:::" + str(v) + ":::@-")
    assert v.read_bytes() == VICTIM.encode("utf-8"), out
    assert "ERROR" in out


def test_the_refusal_names_the_route_that_works(tmp_path, monkeypatch):
    v = _victim(tmp_path)
    _stdin(monkeypatch, "new" + NL)
    out = supertool.dispatch("paste:" + str(v) + ":@-")
    # The remedy, not only the fault: the op that DOES read stdin, and the
    # keys its payload wants.
    assert "paste:@-" in out
    assert "content" in out


# --- the positive controls: these must still write ---------------------------

def test_paste_payload_route_still_writes_a_multi_line_body(tmp_path, monkeypatch):
    """The documented multi-line write. If the guard reached this, every caller
    with a body to write would be left with no route at all."""
    target = tmp_path / "made.txt"
    _stdin(monkeypatch, "path = " + _toml_path(target) + NL
           + "content = " + Q3 + "alpha" + NL + "beta" + NL + Q3 + NL)
    out = supertool.dispatch("paste:@-")
    assert target.read_bytes() == ("alpha" + NL + "beta" + NL).encode("utf-8"), out


def test_paste_triple_colon_still_writes(tmp_path):
    target = tmp_path / "plain.txt"
    out = supertool.dispatch("paste:::" + str(target) + ":::hello" + NL + "world")
    assert target.read_bytes() == ("hello" + NL + "world" + NL).encode("utf-8"), out


def test_paste_single_colon_still_writes(tmp_path):
    target = tmp_path / "single.txt"
    out = supertool.dispatch("paste:" + str(target) + ":hello")
    assert target.read_bytes() == b"hello" + NL.encode("utf-8"), out


def test_append_triple_colon_still_appends(tmp_path):
    v = _victim(tmp_path, "appendme.txt")
    out = supertool.dispatch("append:::" + str(v) + ":::third line")
    assert v.read_bytes() == (VICTIM + "third line" + NL).encode("utf-8"), out


def test_content_merely_containing_at_dash_still_writes(tmp_path):
    """The guard is an equality test on the whole field, not a substring hunt.
    A field that merely holds the two characters is ordinary content."""
    target = tmp_path / "mention.txt"
    body = "see supertool 'edit:@-' for the payload route"
    out = supertool.dispatch("paste:::" + str(target) + ":::" + body)
    assert target.read_bytes() == (body + NL).encode("utf-8"), out


def test_a_literal_at_dash_is_still_writable_through_the_payload(tmp_path, monkeypatch):
    """The escape hatch. A refusal with no way through is this repo's own
    defect class, so the route the refusal names must be able to write the very
    bytes the colon form now declines."""
    target = tmp_path / "literal.txt"
    _stdin(monkeypatch, "path = " + _toml_path(target) + NL
           + "content = " + Q3 + "@-" + Q3 + NL)
    out = supertool.dispatch("paste:@-")
    assert target.read_bytes() == ("@-" + NL).encode("utf-8"), out


def test_edit_still_edits(tmp_path):
    v = _victim(tmp_path, "edited.txt")
    out = supertool.dispatch("edit:second line:third line:" + str(v))
    assert b"third line" in v.read_bytes(), out
