"""#1557 — a directory name chooses lines in the op's own report.

#1553 taught `git-status`, `git-worktrees` and every op with a `Repo:` line to
name the tree a write from a copied worktree actually lands in. It interpolated
that name **raw**, and the name comes off disk:

* `.git/worktrees/<name>/gitdir` is a file whose whole content is the path, so
  anything that wrote it chooses the bytes; and
* git itself writes that file, from the path handed to `git worktree add`. So a
  worktree whose directory name contains a line separator injects through the
  ordinary route, with no hostile file and no attacker — measured below.

A separator in that value put `### Staged (0)` at column 0 of `git-status`,
indistinguishable from the section the op writes itself.

The value is a **path**, so the flattening discloses the separator rather than
eliding it (`_untrusted.flat(..., disclose_newline=True)`): whoever reads the
banner still has to be able to identify the directory, including when its name
legitimately contains an odd character. A space in its place would render a
name that is not on disk.

The seam is `foreign_worktree()` itself, not the three renders it feeds — the
tuple reaches `foreign_worktree_note()`, the two prose lines under it in
`status.py` and `worktrees.py`, and `repo_label()`. Fixing per render is how
the fourth one gets missed.
"""
from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_GIT_DIR = _ROOT / "presets" / "git"


def _load(name: str, filename: str, directory: Path = _GIT_DIR):
    path = directory / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


common = _load("git_common_1557", "_git_common.py")
status = _load("git_status_1557", "status.py")
worktrees = _load("git_worktrees_1557", "worktrees.py")

#: The two payloads. `LF` is what git writes when a worktree's own directory
#: name contains one, and what any writer of the backpointer can put there. It
#: is not a legal NTFS filename character, but it is legal in the *content* of
#: `gitdir`, so the file-content route below runs on every platform.
#:
#: `U+2028` is the one that needs neither: it is a legal filename character on
#: NTFS, APFS and ext4, and `str.splitlines()` — which every consumer of this
#: render counts lines with — splits on it (#886). So the two are not the same
#: test twice; the second is the one that answers for Windows.
LF = chr(10)
LS = chr(0x2028)

#: What a forged line looks like when it lands: a section header `git-status`
#: writes itself, at column 0.
FORGED = "### Staged (0)"


def _layout(root: Path, registered_name: str) -> tuple[Path, Path]:
    """(the copy we stand in, the registered path the backpointer names).

    The registered path is written as *content*, never created on disk, which
    is what lets the `LF` case run on Windows.
    """
    main = root / "repo"
    admin = main / ".git" / "worktrees" / "wt"
    admin.mkdir(parents=True)
    (main / ".git" / "HEAD").write_text("ref: refs/heads/master" + LF, encoding="utf-8")
    registered = root / registered_name
    (admin / "gitdir").write_text(f"{registered}{os.sep}.git" + LF, encoding="utf-8")
    copy = root / "wtcopy"
    copy.mkdir()
    (copy / ".git").write_text(f"gitdir: {admin}" + LF, encoding="utf-8")
    return copy, registered


def _lines(text: str) -> list[str]:
    """The consumer's own reader — `str.splitlines()`, ten separators (#886)."""
    return text.splitlines()


def _forged(text: str) -> list[str]:
    """Lines of `text` the payload put at column 0.

    `startswith`, not equality: the note appends ` (#1536)` after the value, so
    the forged line lands as `### Staged (0) (#1536)`. An equality assertion
    passed against the unfixed code — a test that would have gone green if the
    flattening did nothing.
    """
    return [line for line in _lines(text) if line.startswith(FORGED)]


# -- the seam ---------------------------------------------------------------


@pytest.mark.parametrize("sep", [LF, LS], ids=["newline", "u2028"])
def test_a_separator_in_the_registered_path_cannot_make_a_line(tmp_path, sep) -> None:
    copy, _registered = _layout(tmp_path, f"wt{sep}{FORGED}")
    found = common.foreign_worktree(str(copy))
    assert found is not None
    note = common.foreign_worktree_note(found)
    assert len(_lines(note)) == 1, (
        f"a directory name chose {len(_lines(note))} lines in a banner the op "
        f"writes; the second reads as a section of the report: {note!r}"
    )


@pytest.mark.parametrize("sep", [LF, LS], ids=["newline", "u2028"])
def test_the_separator_is_disclosed_and_not_elided(tmp_path, sep) -> None:
    """The value is a path: the reader still has to be able to identify it."""
    copy, _registered = _layout(tmp_path, f"wt{sep}{FORGED}")
    found = common.foreign_worktree(str(copy))
    assert found is not None
    rendered = found[1]
    disclosure = {LF: (chr(0x240A), "[U+000A]"), LS: (chr(0x2028), "[U+2028]")}[sep]
    assert any(d in rendered for d in disclosure), (
        f"the separator was elided, so the banner names a directory that is "
        f"not the one on disk: {rendered!r}"
    )
    assert sep not in rendered


def test_this_directorys_own_path_cannot_make_a_line(tmp_path) -> None:
    """The other half of the tuple. `status.py` and `worktrees.py` each print
    it in their own prose line, which no fix inside the note would reach.

    U+2028 rather than a newline because this one has to exist on disk, and a
    newline is not a legal NTFS filename character."""
    root = tmp_path / f"tree{LS}{FORGED}"
    root.mkdir()
    copy, _registered = _layout(root, "wt")
    found = common.foreign_worktree(str(copy))
    assert found is not None
    assert len(_lines(found[0])) == 1, (
        f"the directory the op was run from made a second line: {found[0]!r}"
    )


def test_an_escape_sequence_in_the_registered_path_is_disclosed(tmp_path) -> None:
    """`ESC [2K ESC [1A` erases the line above and moves onto it — strictly
    worse than adding a line, because it removes one the op wrote (#851)."""
    copy, _registered = _layout(tmp_path, "wt" + chr(27) + "[2K" + chr(27) + "[1A")
    found = common.foreign_worktree(str(copy))
    assert found is not None
    assert chr(27) not in found[1]


def test_an_ordinary_path_is_byte_identical(tmp_path) -> None:
    """The flattening must not touch the render everybody actually sees."""
    copy, registered = _layout(tmp_path, "wt")
    found = common.foreign_worktree(str(copy))
    assert found is not None
    assert found[0] == os.path.abspath(str(copy))
    assert found[1] == str(registered)


# -- the three sinks --------------------------------------------------------


def _ok(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["git"], returncode=0, stdout=stdout, stderr="")


def _dead(rc: int) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["git"], returncode=rc, stdout="", stderr="")


def test_git_status_prints_no_forged_section(tmp_path, monkeypatch) -> None:
    copy, _registered = _layout(tmp_path, f"wt{LF}{FORGED}")
    monkeypatch.chdir(copy)

    def fake(args, timeout=None):
        head = args[0] if args else ""
        if head in ("status", "diff", "stash", "for-each-ref"):
            return _ok("")
        if head == "branch":
            return _ok("* wtb abc1234 subject" + LF)
        if head == "rev-parse":
            if "--abbrev-ref" in args:
                return _ok("wtb" + LF)
            if args[-1:] == ["HEAD"]:
                return _ok("abc1234" + LF)
            return _dead(1)
        if head == "rev-list":
            return _ok("0\t0" + LF)
        if head == "log":
            return _ok("abc1234 2026-08-13 t | subject" + LF)
        return _dead(1)

    monkeypatch.setattr(status, "_spawn_git", fake)
    monkeypatch.setattr(status, "_hosted_request", lambda cmd: None)
    monkeypatch.setattr(sys, "argv", ["status.py"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        status.main()
    out = buf.getvalue()
    assert not _forged(out), (
        "a directory name wrote a `git-status` section header at column 0; a "
        "reader, or a consumer grepping the render, sees a staged section the "
        f"op never wrote: {out!r}"
    )


def test_git_worktrees_prints_no_forged_section(tmp_path, monkeypatch) -> None:
    copy, _registered = _layout(tmp_path, f"wt{LF}{FORGED}")
    monkeypatch.chdir(copy)
    monkeypatch.setattr(worktrees, "_git", lambda args, timeout=None: _dead(1))
    monkeypatch.setattr(sys, "argv", ["worktrees.py"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        worktrees.main()
    out = buf.getvalue()
    assert not _forged(out), out


def test_repo_label_prints_no_forged_line(tmp_path, monkeypatch) -> None:
    """The third sink, and the one on the ops that WRITE: `git-commit` and
    `git-push` say where the write landed through this line (#692, #1553)."""
    copy, _registered = _layout(tmp_path, f"wt{LF}{FORGED}")
    monkeypatch.chdir(copy)
    monkeypatch.setattr(
        common, "_git",
        lambda args, timeout=None: _ok(f"{copy}" + LF),
    )
    label = common.repo_label()
    assert not _forged(label), label
    assert common.FOREIGN_WORKTREE_MARKER in label


def test_the_repo_label_toplevel_cannot_make_a_line(monkeypatch) -> None:
    """`--show-toplevel` is a path too, interpolated into the same line. A tree
    whose own directory name carries a separator forges the `Repo:` line with
    no copied worktree involved at all (#1557, adjacent)."""
    monkeypatch.setattr(
        common, "_git",
        lambda args, timeout=None: _ok(f"/x/tree{LF}{FORGED}" + LF),
    )
    monkeypatch.setattr(common, "foreign_worktree", lambda start=None: None)
    label = common.repo_label()
    assert not _forged(label), label


# -- the premise ------------------------------------------------------------


@pytest.mark.skipif(
    os.name == "nt",
    reason="a newline is not a legal NTFS filename character, so this route "
           "needs a POSIX filesystem; the gitdir-content route above is the "
           "one that answers for Windows and it runs everywhere",
)
def test_real_git_writes_a_newline_bearing_path_into_the_backpointer(tmp_path) -> None:
    """The premise, measured rather than reasoned: no hostile write is needed,
    because `git worktree add` puts the path it is given into `gitdir` as-is."""
    git = shutil.which("git")
    if git is None:  # pragma: no cover - git is present on every CI leg
        pytest.skip("git not installed")
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.x",
        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e.x",
        GIT_CONFIG_GLOBAL=str(tmp_path / "nonexistent-gitconfig"),
        GIT_CONFIG_SYSTEM=str(tmp_path / "nonexistent-gitconfig"),
    )

    def run(args, cwd):
        return subprocess.run(
            [git, *args], cwd=str(cwd), env=env, capture_output=True,
            encoding="utf-8", errors="replace",
        )

    main = tmp_path / "origrepo"
    main.mkdir()
    run(["init", "-q"], main)
    run(["commit", "-q", "--allow-empty", "-m", "c1"], main)
    wt = tmp_path / f"wt{LF}{FORGED}"
    added = run(["worktree", "add", "-q", str(wt), "-b", "feat"], main)
    assert wt.is_dir(), f"git worktree add did not run: {added.stderr}"
    written = list((main / ".git" / "worktrees").rglob("gitdir"))
    assert written, "git left no backpointer; the whole detector rests on it"
    assert LF in written[0].read_text(encoding="utf-8").strip(), (
        "git no longer writes the worktree path into `gitdir` verbatim — the "
        "no-attacker-required half of #1557 would be stale"
    )

    copy = tmp_path / "wtcopy"
    shutil.copytree(wt, copy, symlinks=True)
    found = common.foreign_worktree(str(copy))
    assert found is not None
    assert len(_lines(common.foreign_worktree_note(found))) == 1
