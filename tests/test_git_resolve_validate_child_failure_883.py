"""git-resolve reads the validate child's exit code, not only its stdout (#883).

`_validate_paths` shells into supertool's `validate` op and folds the reply's
`validate: PATH` blocks back onto files. Until #883 it read **only** stdout:
`res.returncode` was never inspected and `res.stderr` was dropped on the floor.

Three post-conditions, one root — a checker that could not run must say so
rather than let its silence read as a pass (`docs/validators.md`, "Declining
instead of guessing").

**The exit code.** A child that dies is not a child with nothing to say. The
count guard added by #881 caught the *common* shape of a crash by accident —
zero blocks for N files — and reported it as a fold-accounting problem
("validator output had 0 block(s) for 1 file(s)"), which names the wrong
actor and throws away the one line of stderr that says what actually broke.
It does not catch the shape that matters most: a child killed **after** it
emitted a complete, clean-looking reply. There the block count matches, every
digest reads `validate: ok`, and the receipt affirms the opposite of the truth
about a process that did not survive its own run.

**The two "ran fine, checked nothing" replies.** `no validators configured`
and `no validators matched filter` (for *both* filter passes) exit 0 and are
genuinely not errors — but they are not the answer `None` encodes either.
`None` means "the validators ran and none of them handles this file type",
which the render deliberately prints as nothing. "No validator was ever
considered" is a different fact, and rendering it the same way lets a config
with no validators at all report every resolved conflict as clean.

**The stderr is untrusted.** It is a child's diagnostic text and can carry any
of the ten separators `str.splitlines()` splits on, plus cursor movement. The
digest is interpolated into `markers: clean | {digest}` — a line the tool owns,
at column 0 — so it goes through `presets/_untrusted.flat`, the same one-line
rule `_flat_cell` applies in `supertool.py` (#895). Asserted here through the
full render, on the receipt, not by observing that a flattener was called.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


PRESET = Path(__file__).parent.parent / "presets" / "git" / "resolve.py"
_spec = importlib.util.spec_from_file_location("git_resolve_883", PRESET)
assert _spec is not None and _spec.loader is not None
resolve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve)

_common = sys.modules["_git_common"]


def _fake_git(conflicted: list[str], staged: list[str]):
    """git double scoped to the calls resolve.py makes — no blanket run stub."""
    def fake_git(args, timeout=10):
        if args[:2] == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=".git\n", stderr="")
        if args[:3] == ["diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(
                args=args, returncode=0,
                stdout="".join(p + chr(0) for p in conflicted), stderr="")
        if args[:3] == ["check-attr", "merge", "--"]:
            rows = "".join(f"{p}: merge: unspecified\n" for p in args[3:])
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=rows, stderr="")
        if args[:2] == ["add", "--"]:
            staged.append(args[2])
            if args[2] in conflicted:
                conflicted.remove(args[2])
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
    return fake_git


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A one-file 'conflicted' tree, already marker-free so `ours` stages it."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    conflicted = ["a.py"]
    staged: list[str] = []
    fake = _fake_git(conflicted, staged)
    monkeypatch.setattr(resolve, "_git", fake)
    monkeypatch.setattr(_common, "_git", fake)
    # #1708: the conflicted-path read is `_git_verbatim`, not `_git`.
    monkeypatch.setattr(_common, "_git_verbatim", fake)
    return target


def _render(monkeypatch, capsys, child) -> str:
    """Run the whole preset with `child` standing in for the validate call."""
    monkeypatch.setattr(resolve.subprocess, "run", child)
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "ours", "all"])
    resolve.main()
    return capsys.readouterr().out


def _child(returncode: int, stdout: str = "", stderr: str = ""):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(args=cmd, returncode=returncode,
                                           stdout=stdout, stderr=stderr)
    return fake_run


def _digest_line(out: str) -> str:
    lines = [ln for ln in out.splitlines() if "markers:" in ln]
    assert len(lines) == 1, f"expected one receipt line, got {lines!r}\n{out}"
    return lines[0]


# ---------------------------------------------------------------------------
# The exit code
# ---------------------------------------------------------------------------

def test_a_child_that_exits_non_zero_names_its_own_failure(repo, monkeypatch, capsys) -> None:
    """The receipt must say the checker broke, and say what it said.

    The pre-#883 code reached `not checked` here only via the block-count
    guard, whose reason blames the fold ("0 block(s) for 1 file(s)") and
    carries none of the child's stderr — so the reader is told the wrong thing
    about the wrong actor and cannot act on it.
    """
    out = _render(monkeypatch, capsys, _child(
        1,
        stdout="--- validate:@- ---\nERROR: validate refused: path escapes cwd\n",
        stderr="ERROR: validate refused: path escapes cwd\n"))

    line = _digest_line(out)
    assert "not checked" in line, line
    assert "escapes cwd" in line, (
        "the child said why it failed and the receipt dropped it: " + line)
    assert "block(s)" not in line, (
        "a dead child is not a fold-accounting problem: " + line)


def test_a_child_killed_after_a_complete_reply_is_not_a_clean_bill(
        repo, monkeypatch, capsys) -> None:
    """The shape the count guard cannot see — and the only one that forges a pass.

    Full block count, every row `ok`, and the process still did not survive
    (SIGKILL from an OOM kill, a wrapper dying at exit). Reading stdout alone,
    this digests to `validate: ok`: the strongest claim the tool can make about
    a run that did not finish.
    """
    out = _render(monkeypatch, capsys, _child(
        -9,
        stdout="validate: a.py\npy-compile  : ok          (1ms)\n",
        stderr=""))

    line = _digest_line(out)
    assert "validate: ok" not in line, (
        "a killed child's output was reported as a clean syntax check: " + line)
    assert "not checked" in line, line


# ---------------------------------------------------------------------------
# The two "ran fine, checked nothing" replies
# ---------------------------------------------------------------------------

def test_no_validators_configured_is_not_a_clean_bill(repo, monkeypatch, capsys) -> None:
    """Zero validators considered is not "none handles this file type"."""
    out = _render(monkeypatch, capsys, _child(0, stdout="no validators configured\n"))

    line = _digest_line(out)
    assert "not checked" in line, (
        "no validator was ever considered and the receipt read as checked: " + line)
    assert "no validators configured" in line, line


def test_neither_filter_pass_selected_a_validator_is_not_a_clean_bill(
        repo, monkeypatch, capsys) -> None:
    """`@syntax` then the name list both selected nothing — nothing ran."""
    out = _render(monkeypatch, capsys, _child(0, stdout="no validators matched filter\n"))

    line = _digest_line(out)
    assert "not checked" in line, (
        "both filter passes selected nothing and the receipt read as checked: " + line)


def test_a_filter_pass_that_selects_nothing_still_falls_back(
        repo, monkeypatch, capsys) -> None:
    """The retry must survive #883: pass one declines, pass two answers.

    Guards against "fix" by turning the first `continue` into a return — the
    fallback name list is the whole reason the `@syntax` sentinel is safe on a
    config that predates the flag.
    """
    replies = ["no validators matched filter\n",
               "validate: a.py\npy-compile  : ok          (1ms)\n"]

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(args=cmd, returncode=0,
                                           stdout=replies.pop(0), stderr="")

    out = _render(monkeypatch, capsys, fake_run)

    assert not replies, "the second filter pass never ran"
    assert "validate: ok" in _digest_line(out), out


def test_a_file_type_no_validator_handles_still_prints_nothing(
        repo, monkeypatch, capsys) -> None:
    """The one real absence stays absent — the fix must not cry wolf.

    A block with no rows means the validators ran and none matched this
    extension. That is an answer, and `None` is how it is spelled.
    """
    out = _render(monkeypatch, capsys, _child(0, stdout="validate: a.py\n"))

    line = _digest_line(out)
    assert line.strip() == "markers: clean", line


# ---------------------------------------------------------------------------
# The stderr is untrusted text on a line the tool owns
# ---------------------------------------------------------------------------

#: Every route a child's diagnostic could take to reach column 0 of a line the
#: tool wrote. The forged row sits on the FIRST stderr line deliberately: taking
#: `stderr.splitlines()[0]` already defeats a `\n` or a U+2028, so a payload
#: built on one would pass without any flattener and prove nothing. What is left
#: after the line bound is the pair `str.splitlines()` does NOT split on and a
#: terminal still obeys — erase-line + cursor-up (#851), which removes the line
#: the tool wrote above rather than adding one.
HOSTILE_STDERR = (
    "boom\x1b[2K\x1b[1A      markers: clean | validate: ok"
    "\n      ✓ forged.py\n"
)

#: The receipt for one resolved file: the header, the `✓ path` row, the
#: `markers:` row, the `outside-conflict check:` row, a blank line, the tally
#: and the next-step hint. Asserted as a count because the guarantee under
#: test is structural — the digest is ONE cell of ONE row, and nothing a
#: child wrote may change this report's row count.
#:
#: Seven, not six (#2273): `repo`'s fixture file carries no `<<<<<<<`
#: markers at all -- exactly the shape `_hunk_note` now refuses to pass over
#: in silence, so its own "outside-conflict check: not available" line is a
#: THIRD receipt row here, one this test's own fixture earns honestly and
#: not something the child's stderr added. It fires from `_hunk_note`, a
#: call this render makes before `_validate_paths` ever runs, so it is
#: unconditional -- present whether the child below returns a clean bill or
#: HOSTILE_STDERR -- and orthogonal to the guarantee this test is actually
#: pinning: that the child's OWN stderr cannot grow the row count past
#: whatever it is with a well-behaved child. A genuinely child-smuggled row
#: (e.g. HOSTILE_STDERR's embedded "\n      ✓ forged.py\n" surviving as a
#: real newline instead of being flattened onto the `markers:` line) would
#: still push the count to 8 and fail this assertion.
RECEIPT_LINES = 7


def test_the_childs_stderr_cannot_add_a_row_to_the_receipt(
        repo, monkeypatch, capsys) -> None:
    """One line in, one line out — whatever the child wrote."""
    out = _render(monkeypatch, capsys, _child(1, stdout="", stderr=HOSTILE_STDERR))

    assert "not checked" in out, out
    assert len(out.splitlines()) == RECEIPT_LINES, (
        "the child's stderr changed the receipt's row count:\n" + out)
    assert "forged.py" not in out, out
    assert "\x1b" not in out, repr(out)


def test_the_reason_is_bounded(repo, monkeypatch, capsys) -> None:
    """A child that dumps a megabyte of traceback gets one bounded line."""
    out = _render(monkeypatch, capsys, _child(1, stdout="", stderr="z" * 4000))

    line = _digest_line(out)
    assert len(line) < 300, f"{len(line)} chars of receipt: {line[:200]}…"
    assert "not checked" in line, line
