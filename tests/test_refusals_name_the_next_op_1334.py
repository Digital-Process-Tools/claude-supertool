"""#1334 / #1372 — a refusal names what is wrong and not what to do next.

The receipt contract is `what happened / proof / what is next`. These four
refusals had the third column and dropped it, each costing the caller a
round-trip whose answer the refusal already held:

* `edit`, `replace_lines` and `vim` on a path that is not there print
  `file not found` and stop. `paste` creates a file (and its parent dirs) and
  `append` creates or extends; neither is named, so getting unstuck means
  already knowing the roster. They also bypassed `_path_not_found` entirely,
  so the `tried:` line #1300 added — which #1372 assumed was already there as
  the material a create hint would discriminate on — never printed for them.
* the `edit:@-` payload route refuses a missing `old` by naming the field.
  When the payload's own `path` does not exist there is nothing to edit under
  any spelling of `old`, and that is the fact worth printing.
* `grep`'s LIMIT-0 refusal says it will not guess between "unlimited" and
  "the default" — while the op accepts BOTH, as `all` (#1328) and as omission,
  and names neither. A refusal that lists two readings and gives a spelling
  for neither is the third column at its emptiest.

The counter-pressure is #1424's `misdirects` class: a named substitute beats
silence only when it is right for the input the caller gave. `paste` at a
typo'd path writes a second file and leaves the real one unedited, which is
strictly worse than the refusal. So the create clause fires on exactly one arm
— the generic `wrong CWD?` fallback — and is suppressed wherever
`_path_not_found` has positively identified a different mistake.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import supertool


def _stdin(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(supertool.sys, "stdin", io.StringIO(json.dumps(payload)))


# ---------------------------------------------------------------------------
# The edit family: name the op that creates, and print what was stat-ed
# ---------------------------------------------------------------------------

def test_edit_on_a_missing_file_names_the_op_that_creates(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    out = supertool.op_edit("a", "b", "no/such/file.py")
    assert "not found" in out, out
    assert "paste" in out, out
    assert "append" in out, out


def test_edit_on_a_missing_file_prints_the_path_it_tried(
    tmp_path: Path, monkeypatch,
) -> None:
    """#1372 asserts the `tried:` line is already there to discriminate on.

    It is not: `op_edit` returned a bare f-string and never reached
    `_path_not_found`. The create clause is only safe next to it, so the
    routing is part of this fix rather than a tidy-up beside it.
    """
    monkeypatch.chdir(tmp_path)
    out = supertool.op_edit("a", "b", "no/such/file.py")
    assert "tried:" in out, out


def test_replace_lines_on_a_missing_file_names_the_op_that_creates(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    out = supertool.op_replace_lines("no/such/file.py", 1, 2, "x")
    assert "paste" in out, out
    assert "tried:" in out, out


def test_vim_on_a_missing_file_names_the_op_that_creates(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    out = supertool.op_vim("no/such/file.py", "iX")
    assert "paste" in out, out
    assert "file unchanged" in out, out


def test_replace_on_a_missing_file_names_the_op_that_creates(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    out = supertool.op_replace("a", "b", "no/such/file.py")
    assert "paste" in out, out


# ---------------------------------------------------------------------------
# ...and only where no better-identified mistake is on the table (#1424)
# ---------------------------------------------------------------------------

def test_a_path_that_exists_at_the_project_root_is_not_told_to_create(
    tmp_path: Path, monkeypatch,
) -> None:
    """cwd drift is a positively identified cause, and `paste` would be wrong.

    Taking a create suggestion here writes a second file under the drifted cwd
    and leaves the one the caller meant untouched — the `misdirects` failure
    this clause must not introduce.
    """
    (tmp_path / ".supertool.json").write_text("{}\n")
    (tmp_path / "target.py").write_text("hello\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    out = supertool.op_edit("hello", "bye", "target.py")
    assert "exists at" in out, out
    assert "paste" not in out, out


def test_a_directory_is_not_reported_missing_and_is_not_told_to_create(
    tmp_path: Path, monkeypatch,
) -> None:
    """Found writing this fix: `edit` on a directory said `file not found`.

    The path is right there, so the refusal was an absence the tool produced
    read as an absence in the world — and the create clause landed on top of
    it, offering to `paste` a whole file over a directory. Both halves are the
    same missing third state: exists-but-wrong-kind is neither `ok` nor gone.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "adir").mkdir()
    out = supertool.op_edit("a", "b", "adir")
    assert "directory" in out, out
    assert "paste" not in out, out
    assert "wrong CWD?" not in out, out


def test_a_read_op_on_a_missing_file_is_not_told_to_create(
    tmp_path: Path, monkeypatch,
) -> None:
    """`read` has no business naming a creating op — nothing was to be written."""
    monkeypatch.chdir(tmp_path)
    out = supertool.op_read("no/such/file.py")
    assert "not found" in out, out
    assert "paste" not in out, out


def test_around_keeps_its_own_suggestion_and_gains_no_create_clause() -> None:
    """#734's digit heuristic is a positively identified mistake too."""
    out = supertool.op_around("presets/gitlab/mr.py", "681")
    assert "around_line" in out, out
    assert "paste" not in out, out


# ---------------------------------------------------------------------------
# The payload route: a missing field on a path that does not exist
# ---------------------------------------------------------------------------

def test_edit_payload_missing_old_on_a_nonexistent_path_names_paste(
    tmp_path: Path, monkeypatch,
) -> None:
    """The filed case: `path` + `new` + a `create` key that `edit` does not have."""
    monkeypatch.chdir(tmp_path)
    _stdin(monkeypatch, {"path": "no/such/file.py", "new": "hi", "create": True})
    out = supertool.dispatch("edit:@-")
    assert "paste" in out, out


def test_edit_payload_missing_old_on_an_existing_path_still_names_the_field(
    tmp_path: Path, monkeypatch,
) -> None:
    """The file is there, so `old` really is the missing thing. No create hint."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "there.py").write_text("hello\n")
    _stdin(monkeypatch, {"path": "there.py", "new": "hi"})
    out = supertool.dispatch("edit:@-")
    assert "missing required field" in out, out
    assert "paste" not in out, out


# ---------------------------------------------------------------------------
# grep LIMIT 0: name both spellings the refusal says it will not guess between
# ---------------------------------------------------------------------------

def test_grep_zero_limit_names_the_spelling_for_unlimited() -> None:
    out = supertool.dispatch("grep:def:_supertool.py:0:0")
    assert "ERROR" in out, out
    assert ":all" in out, out


def test_grep_zero_limit_names_the_default_it_would_have_applied(
    monkeypatch,
) -> None:
    """The number, resolved at call time so a project override shows.

    Frozen at import it would name the shipped constant while a different cap
    ran — the shape of defect this repo files as an absence the tool produced.
    """
    monkeypatch.setattr(supertool, "MAX_GREP_RESULTS", 37)
    out = supertool.dispatch("grep:def:_supertool.py:0:0")
    assert "37" in out, out


# ---------------------------------------------------------------------------
# The tables are hand-written, so a test is what stops them rotting
# ---------------------------------------------------------------------------

def test_every_hand_written_substitute_is_an_op_this_binary_has() -> None:
    """There is no registry-derived route to these names, so this is the pin.

    `replaces` maps a raw shell command to an op and exists only on preset and
    project ops; every substitute named here is a built-in, and built-ins have
    no registry entry at all (`registry:paste` says so in as many words).
    Nothing can derive them, so the table is written by hand and this test is
    what fails when a target op is renamed or dropped.
    """
    valid = set(supertool._valid_op_names())
    named = set(supertool._CREATING_OPS) | set(supertool._OP_SYNONYMS.values())
    assert named, "the tables are empty — this test would be vacuous"
    missing = sorted(n for n in named if n not in valid)
    assert not missing, f"refusals name ops that do not exist: {missing}"
