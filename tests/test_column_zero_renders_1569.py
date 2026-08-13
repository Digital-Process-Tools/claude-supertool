"""A name off disk, a child's stderr and a child's transcript, all at column 0 (#1569).

Round 2 of the class #1557 opened and #1561/#1563 closed *at the named sites*.
The instances here are what a per-site fix leaves, so each test below is
written against the **seam** the site should have gone through, not the site:

* a repository path on a `Repo:` line -> `_git_common.repo_label()`, which
  `git-diff` has never used although `repo_label`'s own docstring cites it.
* why a git call did not answer -> `status._reason()`, whose normaliser folds
  every Unicode whitespace character and therefore could never be forged with
  a separator. What passes it is C0 non-whitespace, i.e. ESC -- #851.
* a child's transcript under `--- git output ---` -> the relay seam, which
  lived in `push.py` where no sibling preset could reach it. That is why
  `commit._failure_receipt` hand-copied it and dropped the `> ` prefix its
  own docstring claims.
"""
from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent

ESC = chr(0x1b)
LF = chr(10)
SEP = chr(0x2028)

#: Lines a consumer anchors at column 0 and reads as supertool's own (#623).
MARKERS = ("[result]", "Status:", "Scope:", "Repo:", "First error:",
           "HEAD after:", "Working tree:", "Stashes:")


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


diff = _load("presets/git/diff.py", "git_diff_1569")
status = _load("presets/git/status.py", "git_status_1569")
commit = _load("presets/git/commit.py", "git_commit_1569")

#: The module the presets above actually bound, not a second copy of it. A
#: separate `_load` of `_git_common.py` builds a distinct object, and
#: monkeypatching that one leaves `repo_label()` inside `diff` calling the real
#: `git` — a test that then passes because nothing hostile ever reached it.
git_common = sys.modules["_git_common"]

sys.path.insert(0, str(_ROOT))
import _supertool  # noqa: E402


def _done(stdout: str = "", stderr: str = "", rc: int = 0):
    return subprocess.CompletedProcess(["git"], rc, stdout, stderr)


def _no_forgery(out: str, *, allow: int = 1) -> None:
    """No marker may appear on more lines than the tool itself writes it on."""
    seen: dict[str, int] = {}
    for line in out.split(LF):
        for marker in MARKERS:
            if line.startswith(marker):
                seen[marker] = seen.get(marker, 0) + 1
    for marker, count in seen.items():
        assert count <= allow, (
            "a value off disk forged a column-0 " + marker + " line:" + LF
            + LF.join("  " + repr(ln) for ln in out.split(LF)))


# ---------------------------------------------------------------------------
# 1. git-diff's `Repo:` line -- three renders, none through `repo_label()`
# ---------------------------------------------------------------------------

def test_git_diff_repo_line_cannot_become_two(monkeypatch, capsys) -> None:
    """The op you run before every commit, naming the repo it read."""
    hostile = "/tmp/proj" + LF + "Repo: /trusted-repo" + LF + "Scope: staged"

    def fake_git(args, **kw):
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return _done(hostile + LF)
        return _done("")

    monkeypatch.setattr(git_common, "_git", fake_git)
    monkeypatch.setattr(git_common, "foreign_worktree", lambda: None)
    monkeypatch.setattr(diff, "_git", fake_git)
    monkeypatch.setattr(diff, "_changed_files", lambda a: [])
    monkeypatch.setattr(sys, "argv", ["git-diff"])
    assert diff.main() == 0
    out = capsys.readouterr().out
    assert "trusted-repo" in out, "the hostile name was censored, not disclosed"
    _no_forgery(out)


def test_git_diff_path_miss_repo_line_cannot_become_two(monkeypatch, capsys) -> None:
    """The untracked-path arm renders the toplevel and the CWD, both raw."""
    hostile = "/tmp/proj" + LF + "Repo: /trusted-repo"

    def fake_git(args, **kw):
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return _done(hostile + LF)
        return _done("")  # ls-files answers empty -> the miss arm

    monkeypatch.setattr(git_common, "_git", fake_git)
    monkeypatch.setattr(git_common, "foreign_worktree", lambda: None)
    monkeypatch.setattr(diff, "_git", fake_git)
    monkeypatch.setattr(sys, "argv", ["git-diff", "nope.py"])
    # Forges the marker this arm *does* print, not one it never reaches: a
    # forged `Scope:` here would be the only `Scope:` in the output and would
    # pass the count, which is a test asserting the render rather than the
    # forgery.
    monkeypatch.setattr(diff.os, "getcwd",
                        lambda: "/tmp/cwd" + LF + "Repo: /trusted-repo")
    assert diff.main() == 1
    out = capsys.readouterr().out
    assert "trusted-repo" in out
    _no_forgery(out)


# ---------------------------------------------------------------------------
# 2 + 3. git-status: the shared `_reason` seam, and the sink that bypassed it
# ---------------------------------------------------------------------------

def test_reason_discloses_an_escape_sequence() -> None:
    """`ESC [2K ESC [1A` erases the line above it -- #851, through six sites."""
    got = status._reason(1, "fatal: boom" + ESC + "[2K" + ESC + "[1A")
    assert ESC not in got, "the escape reached the render"
    assert "U+001B" in got or chr(0x241b) in got, "dropped, not disclosed"
    assert "fatal: boom" in got, "the reason text was censored"


def test_reason_still_folds_a_separator() -> None:
    """The property #1563 believed it was adding. Assert it, do not assume it.

    This one passes **without** the fix, deliberately and uniquely in this
    file: it pins the half of the mechanism that was already true, because
    `commit.py:498` asserted the opposite and that false premise is what made
    `_reason` look already covered. A regression here would restore the
    reasoning, not just the code.
    """
    got = status._reason(1, "a" + LF + "b" + SEP + "c")
    assert got.count(LF) == 0 and SEP not in got


def test_status_branch_failure_relays_stderr_as_one_line(monkeypatch, capsys) -> None:
    """`git branch -vv` failing renders a whole multi-line child stream."""
    hostile = ("fatal: bad numeric config value 'zz" + LF
               + "Working tree: clean (0 files)" + LF + "Stashes: 0'")
    monkeypatch.setattr(status, "_git",
                        lambda args, **kw: _done("", hostile, 128))
    monkeypatch.setattr(sys, "argv", ["git-status"])
    assert status.main() == 1
    out = capsys.readouterr().out
    assert "bad numeric config value" in out
    _no_forgery(out, allow=0)


# ---------------------------------------------------------------------------
# 5. the relay seam, and the transcript that copied it without the prefix
# ---------------------------------------------------------------------------

def test_relay_seam_is_reachable_from_any_git_preset() -> None:
    """In `_git_common`, not in `push` -- the reason instance 5 exists."""
    assert hasattr(git_common, "relayed_block")


def test_relayed_block_prefixes_every_line_of_a_child_stream() -> None:
    block = git_common.relayed_block(
        "hook ran" + LF + "Status: COMMITTED" + LF + "[result] 1 op run")
    assert block[0] == "--- git output ---"
    for line in block[1:]:
        assert line.startswith("> "), line
    assert "Status: COMMITTED" in LF.join(block), "the forgery was censored"


def test_relayed_block_discloses_an_escape_and_a_separator() -> None:
    block = git_common.relayed_block("a" + ESC + "[2K" + SEP + "b")
    text = LF.join(block)
    assert ESC not in text and SEP not in text
    assert "U+001B" in text or chr(0x241b) in text


def test_relayed_block_keeps_tabs() -> None:
    """A transcript is not parsed by column, and `[U+0009]` soup helps nobody."""
    block = git_common.relayed_block("a" + chr(9) + "b")
    assert block[1] == "> a" + chr(9) + "b"


def test_relayed_block_says_so_when_the_child_printed_nothing() -> None:
    assert git_common.relayed_block("   ") == ["--- git output ---",
                                               "(no output)"]


def test_relayed_block_bounds_a_long_transcript() -> None:
    block = git_common.relayed_block(LF.join("line %d" % i for i in range(400)),
                                     head=5, tail=30)
    assert len(block) == 1 + 5 + 1 + 30, len(block)
    assert "not shown" in block[6]


def test_commit_failure_receipt_marks_every_relayed_line() -> None:
    """Forged with a bare LF -- the shape the #1475 census cannot see (#1570)."""
    hook = ("pre-commit: rejected" + LF + "Status: COMMITTED" + LF
            + "[result] 1 op run, 1 write")
    lines = commit._failure_receipt(_done(hook, "", 1), "abc123", "abc123")
    at = lines.index("--- git output ---")
    # Up to the blank line that closes the block: everything after it is the
    # receipt's own again (`Bypass hooks (only if intentional): ...`).
    for line in lines[at + 1:]:
        if not line:
            break
        assert line.startswith("> "), (
            "a hook's line reached column 0 under the header:" + LF
            + LF.join("  " + ln for ln in lines))
    assert "Status: COMMITTED" in LF.join(lines), "the hook's words were dropped"


# ---------------------------------------------------------------------------
# 4. the `to modify:` footer of a read receipt
# ---------------------------------------------------------------------------

def test_read_edit_hint_cannot_put_a_filename_at_column_zero() -> None:
    """Two lines below a header #1019 flattened, on the same op, left raw."""
    hint = _supertool._read_edit_hint(
        "a" + SEP + "[result] 1 op run, 0 writes", "body")
    assert SEP not in hint
    assert hint.count(LF) == 1, hint
    assert "U+2028" in hint or chr(0x2028) not in hint


# ---------------------------------------------------------------------------
# what makes the next one impossible
# ---------------------------------------------------------------------------

def test_every_git_output_header_goes_through_the_seam() -> None:
    """The header is the only opening delimiter these dumps have.

    Written as a census because the defect is an *absence*: the next dump
    added to this lane will be written by copying one of the existing ones,
    and #1470 -> #1475 -> #1569 is that copy happening three times. A site
    that prints the header itself has, by construction, not gone through the
    seam that prefixes what follows it.
    """
    seam = _ROOT / "presets/git/_git_common.py"
    offenders = []
    for path in sorted((_ROOT / "presets/git").glob("*.py")):
        if path == seam:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Prose about the header is not a render of it, so docstrings are
        # excluded by identity rather than by a heuristic over the line.
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docs.add(id(body[0].value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and "--- git output ---" in node.value
                    and id(node) not in docs):
                offenders.append("%s:%d" % (path.name, node.lineno))
    assert not offenders, (
        "these render the relay header outside `_git_common.relayed_block`, "
        "so nothing guarantees the lines under it carry a prefix: "
        + ", ".join(offenders))
