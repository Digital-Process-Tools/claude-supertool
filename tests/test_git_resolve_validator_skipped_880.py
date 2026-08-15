"""A syntax validator that declined is not a file that checked out clean (#880).

`_validate_paths` distinguishes three states and `_not_checked` renders the
third one — but only for failures of the *batch*: the child crashed, timed out,
was never found, or answered with a block count that does not fold. Inside a
batch that succeeded, one file's rows are condensed by `_digest_block`, and
that function only ever counted rows matching `tool: ok` / `tool: N err`.

`_validator_render_row` emits a fourth row shape:

    phplint     : skipped — php not installed

which is the validator saying, in the repo's own vocabulary, that it declined.
`_digest_block` did not match it, so `ran` stayed False and the file digested
to `None` — the value reserved for "the validators ran and none of them handles
this file type", which the caller deliberately prints as nothing. The receipt
therefore read

    ✓ a.py: 1 of 1 block(s) resolved — all blocks clean, staged
        markers: clean

byte-identical to a file whose parser ran and passed, for a file nobody parsed.

This is the route #880 did not name, and the only one of its four still live:
routes 1 and 2 became `_not_checked` in #883, and route 3 (a path excluded for
containing `:` or `,`) stopped existing when #878's sender-side filter was
replaced by the `@payload` route. It is also the likeliest to be hit in
practice — a `.php` conflict resolved on a machine without php, a `.ts` one
without tsc — because it needs no crash, only a missing interpreter.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


PRESET = Path(__file__).parent.parent / "presets" / "git" / "resolve.py"
_spec = importlib.util.spec_from_file_location("git_resolve_880", PRESET)
assert _spec is not None and _spec.loader is not None
resolve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve)

_common = sys.modules["_git_common"]


def _fake_git(conflicted: list[str], staged: list[str]):
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
    return target


def _render(monkeypatch, capsys, stdout: str) -> str:
    """Whole preset, with `stdout` standing in for the validate child's reply."""
    def child(cmd, **kw):
        return subprocess.CompletedProcess(args=cmd, returncode=0,
                                           stdout=stdout, stderr="")
    monkeypatch.setattr(resolve.subprocess, "run", child)
    monkeypatch.setattr(resolve.sys, "argv", ["resolve.py", "ours", "all"])
    resolve.main()
    return capsys.readouterr().out


def _digest_line(out: str) -> str:
    lines = [ln for ln in out.splitlines() if "markers:" in ln]
    assert len(lines) == 1, f"expected one receipt line, got {lines!r}\n{out}"
    return lines[0]


# ---------------------------------------------------------------------------
# _digest_block — the fold, in isolation
# ---------------------------------------------------------------------------

def test_a_block_whose_only_row_is_skipped_is_not_silence() -> None:
    """The whole defect in one call: `skipped` must not condense to `None`.

    `None` is a claim — "the validators ran and none handles this type" — and
    the caller prints nothing for it. A validator that matched this file and
    then declined established nothing at all.
    """
    digest = resolve._digest_block("phplint     : skipped — php not installed")
    assert digest is not None, (
        "a declined validator digested to the value meaning 'no validator "
        "handles this file type', which renders as a clean bill")
    assert "not checked" in digest, digest
    assert "php not installed" in digest, (
        "the validator said why it declined and the digest dropped it: "
        + str(digest))


def test_a_skip_beside_a_pass_still_costs_a_word() -> None:
    """One validator answered, another declined — `validate: ok` is too strong.

    The digest may say the check that ran passed. It may not say that on its
    own, because the reader takes the line as the verdict for the file.
    """
    digest = resolve._digest_block(
        "ruff        : ok\nphplint     : skipped — php not installed")
    assert digest is not None
    assert "phplint" in digest, (
        "a file half-checked reported exactly like a file fully checked: "
        + str(digest))


def test_a_block_with_no_rows_at_all_is_still_none() -> None:
    """The one state that legitimately renders as silence — do not widen it.

    No row means no validator in the selected scope matched this file type,
    which is a real answer about the world. Turning it into a decline would
    put a warning under every `.txt` and `.md` in every resolve, and a line
    that appears on every call stops being read.
    """
    assert resolve._digest_block("") is None
    assert resolve._digest_block("some prose the adapter printed") is None


def test_a_passing_block_is_unchanged() -> None:
    assert resolve._digest_block("ruff        : ok") == "validate: ok"


def test_a_failing_block_is_unchanged() -> None:
    assert resolve._digest_block("ruff        : 3 err") == "validate: ⚠ ruff 3 err"


# ---------------------------------------------------------------------------
# The receipt — the line a reader uses to decide whether to trust the resolve
# ---------------------------------------------------------------------------

def test_the_receipt_does_not_report_an_unparsed_file_as_clean(
        repo, monkeypatch, capsys) -> None:
    """End to end, through the render, with the child standing in for `validate`.

    The child ran fine and emitted a well-formed block. Everything above
    `_digest_block` is therefore satisfied — which is why the batch-level
    `_not_checked` routes cannot catch this one.
    """
    out = _render(monkeypatch, capsys,
                  "--- validate:@- ---\n"
                  "validate: a.py\n"
                  "phplint     : skipped — php not installed\n")

    line = _digest_line(out)
    assert "not checked" in line, (
        "the receipt claimed a clean bill for a file no parser read: " + line)
    assert "php not installed" in line, line
    assert line.strip() != "markers: clean", line


def test_a_real_pass_still_says_nothing_extra(repo, monkeypatch, capsys) -> None:
    """The healthy path must not grow a line — that is how a warning dies."""
    out = _render(monkeypatch, capsys,
                  "--- validate:@- ---\n"
                  "validate: a.py\n"
                  "ruff        : ok\n")

    line = _digest_line(out)
    assert "not checked" not in line, line
    assert "validate: ok" in line, line
