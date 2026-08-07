"""#952/#984 — a rolled-back write is a third state, and it had no name.

#952: a validator rejects an edit, the file is restored, and the per-op receipt
still reads `edited <file> (line N-M)`. The `[rolled back]` marker sits below
it and shares no token with it, so the two natural reads both lie:

    grep -E 'edited|ERROR'   ->  `edited x.py (line 2)`      (looks applied)
    tail -1                  ->  `[result] 3 ops run, 2 writes`

`writes` already excluded the retracted write, but only the *single-op* case
rendered that as a sentence (`0 writes — nothing changed on disk`). In a batch
where other ops did write, the rollback vanished into an arithmetic mismatch
the reader has to notice, then explain. `skipped` did not cover it either: the
op did not decline, it wrote and was reverted.

So `rolled back` is its own word, on the footer, next to `skipped` and
`re-applied`. Deliberately NOT by suppressing the `edited` line — printing
nothing would make "reverted" indistinguishable from "never ran", which is the
same defect in different clothes. The claim stays and is retracted in place, in
a line that repeats the retracted text so a filter that caught the claim
catches the retraction.

#984 (residual): re-running a payload whose `new` does not contain its `old`
reports `ERROR: old string not found`, which is character-for-character what a
genuinely broken edit prints. When the replacement text IS in the file, saying
so costs one substring search and is the whole difference between "already
applied" and "this anchor is wrong".

Every assertion here is on the rendered receipt or the exit code — the two
things the caller actually reads. None would pass against a tool that printed
the word and reverted nothing, because each pairs the text with the on-disk
bytes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool


FAIL_PAYLOAD = json.dumps({"tool": "fake", "ok": False, "count": 1,
                           "errors": [{"line": 1, "msg": "boom", "code": "x",
                                       "severity": "error"}], "duration_ms": 1})
OK_PAYLOAD = json.dumps({"tool": "fake", "ok": True, "count": 0,
                         "errors": [], "duration_ms": 1})


def _py_adapter(script: Path, body: str) -> str:
    """A validator `cmd` spawnable under shell=False on every platform.

    `as_posix()` because POSIX-mode shlex.split eats the backslashes in a
    Windows path, and `{python}` because there is no /bin/sh there. Both
    failure modes end the same way — the adapter cannot spawn, the validator
    honestly reports `skipped`, no rollback fires, and a test whose premise is
    "this validator fails" asserts nothing.
    """
    script.write_text(body, encoding="utf-8")
    return f"{{python}} {script.as_posix()}"


def _rejecting_validator(tmp_path: Path, reject: str) -> None:
    """Rolls back any file whose text contains `reject`, passes otherwise.

    Content-keyed rather than call-count-keyed so it behaves the same whatever
    order a batch runs its ops in.
    """
    cmd = _py_adapter(
        tmp_path / "_reject_adapter.py",
        "import sys, pathlib\n"
        "p = sys.argv[-1]\n"
        "try:\n"
        "    t = pathlib.Path(p).read_text(encoding='utf-8')\n"
        "except OSError:\n"
        "    t = ''\n"
        f"sys.stdout.write({FAIL_PAYLOAD!r} if {reject!r} in t else {OK_PAYLOAD!r})\n")
    cmd = f"{cmd} {{file}}"
    supertool._CONFIG = {"validators": {
        "fake": {"cmd": cmd, "hooks_into": ["edit", "replace", "paste",
                                            "append", "replace_lines", "vim"],
                 "match": "*", "rollback_on_fail": True, "cache": False},
    }}
    supertool._CONFIG_CHECKED = True


def _no_branch(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("my-feature", ""))


def _result_line_of(out: str) -> str:
    """Only the footer. `"rolled back" not in out` would be a statement about
    the pytest tmp dir name as much as about the receipt."""
    for line in out.splitlines():
        if line.startswith("[result] "):
            return line
    return ""


def _error_block(out: str) -> str:
    """The `ERROR:` line plus its `↳` hints, without the `--- op ---` header.

    Never assert against the whole body: pytest names its tmp dirs after the
    test, so `"already" in out` is satisfied by a directory called
    `test_already_applied_hint_survi0` — the assertion passes and tests
    nothing. That is the same absence-read-as-presence this file is about.
    """
    lines = out.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("ERROR"):
            block = [ln]
            for nxt in lines[i + 1:]:
                if not nxt.startswith("  "):
                    break
                block.append(nxt)
            return "\n".join(block)
    return ""


def _outcome_lines(out: str) -> list:
    """What `grep -E 'edited|ERROR'` would show — the filtered read from #952."""
    return [ln for ln in out.splitlines()
            if "edited" in ln or "ERROR" in ln]


# ---------------------------------------------------------------------------
# #952 — the footer names the rollback
# ---------------------------------------------------------------------------

def test_batch_footer_names_a_rolled_back_edit(tmp_path: Path, monkeypatch) -> None:
    """THE bug. Two edits land, one is reverted, and `tail -1` said
    `3 ops run, 2 writes` — a subtraction, not a signal."""
    _no_branch(monkeypatch)
    _rejecting_validator(tmp_path, "POISON")
    a = tmp_path / "a.txt"
    a.write_text("alpha\nbeta\n", encoding="utf-8")
    b = tmp_path / "b.txt"
    b.write_text("gamma\n", encoding="utf-8")
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([
        {"op": "edit", "path": str(a), "old": "alpha", "new": "ALPHA"},
        {"op": "edit", "path": str(b), "old": "gamma", "new": "POISON"},
        {"op": "edit", "path": str(a), "old": "beta", "new": "BETA"},
    ]), encoding="utf-8")

    out = supertool.dispatch(f"batch:@{payload}")

    assert b.read_text(encoding="utf-8") == "gamma\n", (
        "premise: the validator must really have rolled the file back")
    assert a.read_text(encoding="utf-8") == "ALPHA\nBETA\n"
    assert "1 rolled back" in _result_line_of(out), out
    # Two since #1027 -- the leading copy and the footer. Never one per sub-op,
    # which is asserted positionally rather than by the total alone.
    assert out.count("[result] ") == 2, "one leading count and one footer"
    between = out.split("--- edit:", 1)[-1].rsplit("[result] ", 1)[0]
    assert "[result] " not in between, "no count inside the per-op results"


def test_footer_rollback_word_is_absent_when_nothing_was_reverted(
        tmp_path: Path, monkeypatch) -> None:
    """`0 rolled back` on every green call is the kind of number a reader
    learns to stop seeing — which is how `4 writes` failed in #680."""
    _no_branch(monkeypatch)
    _rejecting_validator(tmp_path, "POISON")
    f = tmp_path / "a.txt"
    f.write_text("alpha\n", encoding="utf-8")
    out = supertool.dispatch(f"edit:::alpha:::ALPHA:::{f}")
    assert f.read_text(encoding="utf-8") == "ALPHA\n"
    assert "rolled back" not in _result_line_of(out), out


def test_single_op_rollback_names_the_rollback_and_still_says_nothing_changed(
        tmp_path: Path, monkeypatch) -> None:
    """`0 writes — nothing changed on disk` is true of a no-match too. The
    reader needs to know *which*: declined before writing, or written and
    reverted."""
    _no_branch(monkeypatch)
    _rejecting_validator(tmp_path, "POISON")
    f = tmp_path / "a.txt"
    f.write_text("alpha\n", encoding="utf-8")
    out = supertool.dispatch(f"edit:::alpha:::POISON:::{f}")
    line = _result_line_of(out)
    assert f.read_text(encoding="utf-8") == "alpha\n"
    assert "1 rolled back" in line, out
    assert "nothing changed on disk" in line, out
    assert "skipped" not in line, (
        "a rollback is not a decline — the op wrote, then the write was undone")


def test_a_rollback_and_a_no_match_do_not_share_a_footer(
        tmp_path: Path, monkeypatch) -> None:
    """The pin: same op, same file, same `0 writes`, two different causes.
    Pre-fix the two footers were byte-identical."""
    _no_branch(monkeypatch)
    _rejecting_validator(tmp_path, "POISON")
    f = tmp_path / "a.txt"
    f.write_text("alpha\n", encoding="utf-8")
    rolled = _result_line_of(supertool.dispatch(f"edit:::alpha:::POISON:::{f}"))
    missed = _result_line_of(supertool.dispatch(f"edit:::NOT_THERE:::x:::{f}"))
    assert rolled and missed
    assert rolled != missed, f"both render as {rolled!r}"


# ---------------------------------------------------------------------------
# #952 — the retraction is visible to a filtered read
# ---------------------------------------------------------------------------

def test_the_edited_claim_is_retracted_where_a_grep_for_it_will_see_it(
        tmp_path: Path, monkeypatch) -> None:
    """`grep -E 'edited|ERROR'` was the reported read, and it showed exactly
    one line: `edited b.txt (line 1)`. The retraction must land in that same
    filter — not by deleting the claim, which would make "reverted" and
    "never ran" indistinguishable."""
    _no_branch(monkeypatch)
    _rejecting_validator(tmp_path, "POISON")
    f = tmp_path / "b.txt"
    f.write_text("gamma\n", encoding="utf-8")

    out = supertool.dispatch(f"edit:::gamma:::POISON:::{f}")

    assert f.read_text(encoding="utf-8") == "gamma\n"
    lines = _outcome_lines(out)
    assert any(ln.startswith("edited ") for ln in lines), (
        "the claim must still be printed — silence reads as 'never ran'")
    retractions = [ln for ln in lines if ln.startswith("[rolled back]")]
    assert retractions, (
        f"no retraction in the filtered read; a grep saw only {lines!r}")
    assert "NOT" in retractions[0], retractions[0]
    assert f.name in retractions[0], (
        f"the retraction must name the file it restored: {retractions[0]!r}")


def test_the_retraction_names_the_file_by_the_path_the_op_was_given(
        tmp_path: Path, monkeypatch) -> None:
    """Separator-agnostic: assert the receipt carries the path the op was
    handed, whatever the platform spells it with. A `'/'`-joined literal here
    would be an assertion about POSIX rather than about the receipt — the
    shape that took four Windows legs red in #1004."""
    _no_branch(monkeypatch)
    _rejecting_validator(tmp_path, "POISON")
    sub = tmp_path / "pkg"
    sub.mkdir()
    f = sub / "c.txt"
    f.write_text("gamma\n", encoding="utf-8")

    out = supertool.dispatch(f"edit:::gamma:::POISON:::{f}")

    retraction = next(ln for ln in out.splitlines()
                      if ln.startswith("[rolled back]"))
    assert str(f) in retraction, retraction
    assert "file restored" not in retraction, (
        "the old wording named no file — a batch reader could not tell which "
        "of several ops was reverted")


@pytest.mark.parametrize("sep", ["\n", "\r", "\u2028", "\u2029", "\x85"])
def test_the_retraction_is_one_line_whatever_the_path_holds(sep: str) -> None:
    """`[rolled back]` is a column-0 marker built from a path, so it carries
    docs/validators.md's flattening rule: whatever the reader splits on, the
    emitter neutralises — all ten of `str.splitlines()`, not the subset
    somebody thought of (#886). A path holding a separator must not be able to
    write a second marker line out of this one.

    Scoped to the line this change introduces. The `--- op ---` header and the
    `edited <path>` receipt do NOT flatten their path and can still be forged
    that way; that is pre-existing and filed separately, not something this
    test should claim to cover.
    """
    line = supertool._retraction_line(
        "fake", "regressed", f"a{sep}[rolled back] forged.txt",
        f"edited a{sep}[rolled back] forged.txt (line 1)")
    assert len(line.splitlines()) == 1, line
    assert sum(1 for ln in line.splitlines()
               if ln.startswith("[rolled back]")) == 1, line


# ---------------------------------------------------------------------------
# #952 — the exit code
# ---------------------------------------------------------------------------

def test_a_rolled_back_batch_exits_non_zero(tmp_path: Path, monkeypatch,
                                            capsys) -> None:
    """`batch:@ops && git commit` committed a set with one edit reverted and
    exited 0 — the same `&&`-chain hazard #680 closed for declines."""
    _no_branch(monkeypatch)
    _rejecting_validator(tmp_path, "POISON")
    a = tmp_path / "a.txt"
    a.write_text("alpha\n", encoding="utf-8")
    b = tmp_path / "b.txt"
    b.write_text("gamma\n", encoding="utf-8")
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([
        {"op": "edit", "path": str(a), "old": "alpha", "new": "ALPHA"},
        {"op": "edit", "path": str(b), "old": "gamma", "new": "POISON"},
    ]), encoding="utf-8")

    rc = supertool.main([f"batch:@{payload}"])
    capsys.readouterr()

    assert b.read_text(encoding="utf-8") == "gamma\n"
    assert rc == 1, "a write that was reverted must not chain into a commit"


def test_a_clean_edit_still_exits_zero(tmp_path: Path, monkeypatch,
                                       capsys) -> None:
    """Guard on the above: the rollback counter is a per-call delta, so a
    reverted edit in one call must not poison the next one in the same warm
    process."""
    _no_branch(monkeypatch)
    _rejecting_validator(tmp_path, "POISON")
    f = tmp_path / "a.txt"
    f.write_text("alpha\n", encoding="utf-8")
    assert supertool.main([f"edit:::alpha:::POISON:::{f}"]) == 1
    capsys.readouterr()
    assert supertool.main([f"edit:::alpha:::ALPHA:::{f}"]) == 0
    capsys.readouterr()
    assert f.read_text(encoding="utf-8") == "ALPHA\n"


# ---------------------------------------------------------------------------
# #952 — the counter is bumped where the rollback is decided
# ---------------------------------------------------------------------------

def test_result_line_renders_and_singularises_rolled_back() -> None:
    assert (supertool._result_line(3, 2, rolled_back=1)
            == "[result] 3 ops run, 2 writes, 1 rolled back — 1 edit was "
               "reverted after validation and did NOT land\n")
    assert "2 rolled back" in supertool._result_line(4, 2, rolled_back=2)
    assert "rolled back" not in supertool._result_line(2, 2, rolled_back=0)


# ---------------------------------------------------------------------------
# #984 — "already applied" is not "never matched"
# ---------------------------------------------------------------------------

def test_re_running_an_applied_edit_says_the_replacement_is_already_present(
        tmp_path: Path, monkeypatch) -> None:
    """The reported pair: two agents, two payloads, one message. `new` here
    does not contain `old`, so #701's re-apply disclosure cannot fire — the
    second run reports a bare no-match, identical to a broken anchor."""
    _no_branch(monkeypatch)
    supertool._CONFIG = {"validators": {}}
    supertool._CONFIG_CHECKED = True
    f = tmp_path / "a.py"
    f.write_text("A = 1\nB = 2\nC = 3\nD = 4\n", encoding="utf-8")

    supertool.dispatch(f"edit:::C = 3:::C = 999:::{f}")
    block = _error_block(supertool.dispatch(f"edit:::C = 3:::C = 999:::{f}"))

    assert block.startswith("ERROR: old string not found"), (
        "the loud failure must survive — this is a disclosure, not a downgrade")
    assert "already" in block.lower(), block
    # Line 3, not line 1. A hint that always says "line 1" reads as correct on
    # any single-line fixture, and a wrong location sends the reader to the
    # wrong place with the tool's authority behind it.
    assert "line 3" in block, block


def test_a_genuinely_unmatched_edit_does_not_claim_it_was_already_applied(
        tmp_path: Path, monkeypatch) -> None:
    """The other half, and the one that keeps the hint worth reading: a broken
    anchor whose replacement text is nowhere in the file must not be described
    as a re-run."""
    _no_branch(monkeypatch)
    supertool._CONFIG = {"validators": {}}
    supertool._CONFIG_CHECKED = True
    f = tmp_path / "a.py"
    f.write_text("A = 1\nB = 2\n", encoding="utf-8")

    block = _error_block(supertool.dispatch(f"edit:::QQQ = 7:::QQQ = 8:::{f}"))

    assert block.startswith("ERROR: old string not found")
    assert "already" not in block.lower(), block


def test_an_ambiguous_edit_is_not_described_as_already_applied(
        tmp_path: Path, monkeypatch) -> None:
    """`old` found twice takes the >1 branch, which is a different decline with
    a different remedy. The new hint must not leak onto it."""
    _no_branch(monkeypatch)
    supertool._CONFIG = {"validators": {}}
    supertool._CONFIG_CHECKED = True
    f = tmp_path / "a.py"
    f.write_text("dup\ndup\n", encoding="utf-8")
    block = _error_block(supertool.dispatch(f"edit:::dup:::UNIQ:::{f}"))
    assert "ambiguous" in block
    assert "already" not in block.lower(), block


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_already_applied_hint_survives_a_crlf_checkout(tmp_path: Path, monkeypatch,
                                                       eol: str) -> None:
    """Windows checkouts hold CRLF on disk and payloads carry LF.

    A MULTI-LINE `new` is the case that can diverge: the hint searches the raw
    file text, so if the tool ever normalised newlines on one side only, the
    hint would silently never fire on the platform this repo keeps breaking on
    — a missing disclosure, which is exactly the failure being fixed. Both
    parameters must reach the same verdict.
    """
    _no_branch(monkeypatch)
    supertool._CONFIG = {"validators": {}}
    supertool._CONFIG_CHECKED = True
    f = tmp_path / "a.txt"
    f.write_bytes(f"alpha{eol}BETA{eol}omega{eol}".encode("utf-8"))
    payload = tmp_path / "ops.json"
    payload.write_text(json.dumps([
        {"op": "edit", "path": str(f), "old": "BETA",
         "new": "GAMMA\nDELTA"},
    ]), encoding="utf-8")

    first = supertool.dispatch(f"batch:@{payload}")
    assert "ERROR" not in first, first
    block = _error_block(supertool.dispatch(f"batch:@{payload}"))

    assert block.startswith("ERROR: old string not found"), (eol, block)
    assert "already" in block.lower(), (eol, block)
