"""Three core refusals that were silent or pointed somewhere else.

#1551 — `_at_file_to_parts` is the one input surface that dropped an
unrecognised payload key without a word. Its two siblings already refuse:
`_read_op_from_payload` ("unknown field(s) ... — accepted: ...") and
`_ordered_batch_fields` ("unknown field(s) ... in batch ..."). A constraint
silently not applied reads, in the receipt, exactly like one that was.

#1554 / #1560 — `misdirects`: a refusal whose remedy is not the thing refused.

* `_unknown_op_message` told a caller standing outside any project to prefix
  `cwd:<project-path>`. Measured: `cwd:` moves the directory the op ACTS on as
  well as the one the config is read from, so obeying it runs `git-commit`
  against a different repository than the one the caller is in. The guard's
  block names the same op and never states the precondition at all.
* `_dropped_tokens_refusal` prescribed widening the template to `{args}` for an
  op that declares `"paths": {"args": [1]}`. Measured against a fixture op:
  `showit:a.txt:../../../etc/hosts` is refused at position 1 and printed
  `/etc/hosts` from position 2 once the template was widened as instructed. The
  module's own comment names that manoeuvre as the reason it will not widen the
  placeholder internally, and then handed it to the operator.
"""
from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any, Dict

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent


def _reader(tmp_path: Path) -> str:
    """A portable stand-in for `cat`: print every path argv names."""
    script = tmp_path / "reader.py"
    script.write_text(
        "import sys" + chr(10)
        + "for p in sys.argv[1:]:" + chr(10)
        + "    sys.stdout.write(open(p, encoding='utf-8').read())" + chr(10),
        encoding="utf-8")
    return shlex.quote(script.as_posix())


# --------------------------------------------------------------------------
# #1551 — an unrecognised payload key is refused, not dropped
# --------------------------------------------------------------------------


def test_unknown_payload_key_is_refused_by_name() -> None:
    with pytest.raises(ValueError) as exc:
        supertool._at_file_to_parts(
            "edit", {"old": "a", "new": "b", "path": "x.txt", "count": 1})
    text = str(exc.value)
    assert "count" in text
    # The accepted set, so the caller is not left guessing the spelling.
    for field in ("old", "new", "path"):
        assert field in text


def test_recognised_payload_keys_still_run() -> None:
    parts, replace_all = supertool._at_file_to_parts(
        "edit", {"old": "a", "new": "b", "path": "x.txt", "replace_all": True})
    assert parts == ["edit", "a", "b", "x.txt"]
    assert replace_all is True


def test_literal_backslashes_optin_is_not_an_unknown_key() -> None:
    """It is a route-level key read at the top level of a payload (#1096).

    For a single-op payload the top level IS the op table, so refusing it here
    would break the one spelling that says "I meant two backslashes".
    """
    parts, _ = supertool._at_file_to_parts(
        "edit", {"old": "a", "new": "b", "path": "x.txt",
                 "literal_backslashes": True})
    assert parts == ["edit", "a", "b", "x.txt"]


def test_batch_sub_item_op_key_is_not_an_unknown_key() -> None:
    parts, _ = supertool._at_file_to_parts(
        "edit", {"op": "edit", "old": "a", "new": "b", "path": "x.txt"})
    assert parts == ["edit", "a", "b", "x.txt"]


def test_unknown_key_refusal_reaches_the_caller(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: the dispatch route prints it, and nothing is written."""
    target = tmp_path / "f.txt"
    target.write_text("alpha" + chr(10), encoding="utf-8")
    payload = tmp_path / "p.toml"
    payload.write_text(
        'path = "f.txt"' + chr(10)
        + "old = 'alpha'" + chr(10)
        + "new = 'gamma'" + chr(10)
        + "count = 1" + chr(10),
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = supertool.dispatch("edit:@" + payload.name)
    assert "count" in out
    assert "ERROR" in out
    assert target.read_text(encoding="utf-8") == "alpha" + chr(10)


def test_batch_wrapper_unknown_key_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Adjacent to #1551, same route, same class.

    `continue_on_error` is a safety flag, and a misspelling of it was dropped
    silently: the batch then ran on past a failed op while the payload said it
    should stop.
    """
    target = tmp_path / "f.txt"
    target.write_text("alpha" + chr(10), encoding="utf-8")
    payload = tmp_path / "b.toml"
    payload.write_text(
        "contineu_on_error = false" + chr(10)
        + "[[ops]]" + chr(10)
        + 'op = "edit"' + chr(10)
        + 'path = "f.txt"' + chr(10)
        + "old = 'alpha'" + chr(10)
        + "new = 'gamma'" + chr(10),
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = supertool.dispatch("batch:@" + payload.name)
    assert "contineu_on_error" in out, out
    assert "ERROR" in out, out
    assert target.read_text(encoding="utf-8") == "alpha" + chr(10)


def test_batch_wrapper_known_keys_still_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "f.txt"
    target.write_text("alpha" + chr(10), encoding="utf-8")
    payload = tmp_path / "b.toml"
    payload.write_text(
        "continue_on_error = false" + chr(10)
        + "literal_backslashes = true" + chr(10)
        + "[[ops]]" + chr(10)
        + 'op = "edit"' + chr(10)
        + 'path = "f.txt"' + chr(10)
        + "old = 'alpha'" + chr(10)
        + "new = 'gamma'" + chr(10),
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = supertool.dispatch("batch:@" + payload.name)
    assert "ERROR" not in out, out
    # A containment check, not a byte-for-byte one: the assertion is that the
    # recognised keys still ran, and the writer's line ending is not the same
    # byte on every leg of the matrix.
    assert "gamma" in target.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# #1560 — the dropped-token refusal must not prescribe defeating containment
# --------------------------------------------------------------------------


_DECLARING_ENTRY: Dict[str, Any] = {
    "cmd": "cat {file}",
    "syntax": "showit:FILE",
    "paths": {"args": [1], "root": "cwd"},
}

_NON_DECLARING_ENTRY: Dict[str, Any] = {
    "cmd": "echo {arg}",
    "syntax": "shout:WORD",
    "paths": {"args": []},
}


def test_declaring_op_is_not_told_to_widen_to_args() -> None:
    text = supertool._dropped_tokens_refusal(
        "showit", _DECLARING_ENTRY, "cat {file}", ["b.txt"])
    # The remedy that defeats the op's own containment declaration must not be
    # handed over bare. Either it is absent, or the same lines say the
    # declaration has to be extended with it.
    if "{args}" in text:
        assert "paths" in text, text


def test_declaring_op_refusal_names_the_gated_positions() -> None:
    text = supertool._dropped_tokens_refusal(
        "showit", _DECLARING_ENTRY, "cat {file}", ["b.txt"])
    assert "paths" in text, text
    # The positions the declaration actually contains, so the reader can see
    # what a widened template would leave outside it.
    assert "1" in text


def test_non_declaring_op_keeps_the_plain_remedy() -> None:
    """`"args": []` is a claim that no argument here is a path, so widening
    the template contradicts nothing and the escape hatch stays plain."""
    text = supertool._dropped_tokens_refusal(
        "shout", _NON_DECLARING_ENTRY, "echo {arg}", ["extra"])
    assert "{args}" in text
    assert "{argjoin}" in text


def test_widened_template_really_does_escape_the_boundary(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The measurement the refusal is wrong about, pinned.

    Position 1 is refused; position 2 reaches the child. If this ever stops
    being true the refusal above can go back to naming `{args}` plainly.
    """
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET-1560" + chr(10), encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.txt").write_text("inside" + chr(10), encoding="utf-8")
    monkeypatch.chdir(project)
    widened = {
        # `{python}` and a script rather than `cat`: the CI matrix runs four
        # Windows legs, where `cat` is not a program and a test that cannot
        # spawn its child proves nothing about containment.
        "cmd": "{python} " + _reader(tmp_path) + " {args}",
        "syntax": "showit:FILE",
        "paths": {"args": [1], "root": "cwd"},
    }
    supertool._CONFIG = {"ops": {"showit": widened}}
    escape = os.path.join("..", "outside.txt")
    gated = supertool._resolve_custom_op("showit", ["showit", escape])
    assert gated is not None and "escapes" in gated, gated
    ungated = supertool._resolve_custom_op(
        "showit", ["showit", "a.txt", escape])
    assert ungated is not None and "SECRET-1560" in ungated, ungated


# --------------------------------------------------------------------------
# #1554 — a remedy that cannot run where the command was refused
# --------------------------------------------------------------------------


def test_unavailable_op_refusal_says_cwd_moves_the_target(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    text = supertool._unknown_op_message("git-commit")
    assert "unavailable here" in text
    assert "cwd:" in text
    # Naming `cwd:` without this is the misdirect: obeying it commits a
    # different repository than the one the caller is standing in.
    assert "ACTS on" in text or "acts on" in text


def test_guard_block_states_the_availability_precondition() -> None:
    config: Dict[str, Any] = {
        "ops": {
            "git-commit": {
                "syntax": "git-commit:::MESSAGE[:::PATHS...]",
                "description": "commit staged work",
                "replaces": [{"argv": "git commit",
                              "use": "git-commit:::MESSAGE:::PATHS"}],
            }
        },
        "_op_sources": {"git-commit": {"preset": "git"}},
    }
    verdict = supertool.guard_command("git commit -m x", config)
    assert verdict.state == "blocked"
    refusal = supertool.guard_refusal(verdict)
    assert ".supertool.json" in refusal
    # The op named above does not exist in a directory no project covers, and
    # `cwd:` is not a way to reach one from outside — both have to be said, or
    # the reader spends a round-trip to learn the first and is misdirected by
    # the second.
    assert "cwd:" in refusal
