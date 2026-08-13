"""#1536 — a copied worktree writes into the original repository, unannounced.

A linked worktree's `.git` is a **gitfile**: one line of text naming the real
git directory. `cp -a` copies that pointer, so every git command run in the copy
reads and writes the *original* worktree's index, HEAD and refs. Observed live:
a `git checkout <sha> -- <path>` run inside a copy staged a revert of two
production files into a worktree nobody was watching, and `git-status` there
rendered it as ordinary staged work.

Two halves, and the ops can answer both:

1. **Which repository am I writing to.** The copy is detectable exactly and
   locally, with no filesystem scan: `.git/worktrees/<name>/gitdir` holds the
   path of the `.git` file git registered for that worktree. If this directory's
   own `.git` is not that path, this directory is not the registered one.
2. **Whose staged work is this.** It cannot be known. `git-status` can only see
   that the index differs from HEAD while the file on disk matches HEAD — staged
   content that no file here has, which is the shape a stray `checkout <sha> --`
   leaves, and also the shape of a stage-then-revert done by hand. So the render
   names what it could not determine rather than listing it as ordinary staged
   work, and it never suppresses the list.
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

_ROOT = Path(__file__).parent.parent
_GIT_DIR = _ROOT / "presets" / "git"


def _load(name: str, filename: str):
    path = _GIT_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


common = _load("git_common_1536", "_git_common.py")
status = _load("git_status_1536", "status.py")


# ── the on-disk shape, fabricated ────────────────────────────────────────────
#
# Verified against real git by `test_real_git_leaves_exactly_this_shape` below;
# fabricating it keeps the render tests free of a git spawn.


def _layout(root: Path) -> tuple[Path, Path, Path]:
    """(main repo, registered worktree, `cp -a` copy of the worktree)."""
    main = root / "repo"
    admin = main / ".git" / "worktrees" / "wt"
    admin.mkdir(parents=True)
    (main / ".git" / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    wt = root / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    (admin / "gitdir").write_text(f"{wt / '.git'}\n", encoding="utf-8")
    copy = root / "wtcopy"
    shutil.copytree(wt, copy)
    return main, wt, copy


def _same(a: str, b) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(
        os.path.realpath(str(b))
    )


# ── half 1: which repository am I writing to ─────────────────────────────────


def test_a_copied_worktree_is_named_as_a_copy(tmp_path) -> None:
    _main, wt, copy = _layout(tmp_path)
    found = common.foreign_worktree(str(copy))
    assert found is not None, (
        "a `cp -a` copy of a worktree was read as an ordinary tree; every git "
        "command run there writes into the original repository"
    )
    here, registered = found
    assert _same(here, copy)
    assert _same(registered, wt)


def test_the_registered_worktree_is_not_flagged(tmp_path) -> None:
    _main, wt, _copy = _layout(tmp_path)
    assert common.foreign_worktree(str(wt)) is None


def test_a_subdirectory_of_the_copy_is_flagged(tmp_path) -> None:
    """git walks up for `.git`; so must this, or `cd src` hides the answer."""
    _main, _wt, copy = _layout(tmp_path)
    sub = copy / "src" / "deep"
    sub.mkdir(parents=True)
    assert common.foreign_worktree(str(sub)) is not None


def test_an_ordinary_repository_is_not_flagged(tmp_path) -> None:
    plain = tmp_path / "plain"
    (plain / ".git").mkdir(parents=True)
    assert common.foreign_worktree(str(plain)) is None


def test_a_submodule_gitfile_is_not_flagged(tmp_path) -> None:
    """A submodule's `.git` is a gitfile too, into `.git/modules/` — not a copy."""
    main = tmp_path / "repo"
    admin = main / ".git" / "modules" / "sub"
    admin.mkdir(parents=True)
    sub = main / "sub"
    sub.mkdir()
    (sub / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    assert common.foreign_worktree(str(sub)) is None


def test_a_directory_outside_any_repository_is_not_flagged(tmp_path) -> None:
    assert common.foreign_worktree(str(tmp_path)) is None


def test_an_unreadable_backpointer_is_not_a_copy(tmp_path) -> None:
    """No answer is not a finding: the admin dir with no `gitdir` file says
    nothing either way, and claiming a copy there would cry wolf on every
    render."""
    _main, _wt, copy = _layout(tmp_path)
    backptr = tmp_path / "repo" / ".git" / "worktrees" / "wt" / "gitdir"
    backptr.unlink()
    assert common.foreign_worktree(str(copy)) is None


def test_real_git_leaves_exactly_this_shape(tmp_path) -> None:
    """The whole detector rests on git's own layout — so measure it, once."""
    git = shutil.which("git")
    if git is None:  # pragma: no cover - git is present on every CI leg
        import pytest

        pytest.skip("git not installed")
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.x",
        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e.x",
        GIT_CONFIG_GLOBAL=str(tmp_path / "nonexistent-gitconfig"),
        GIT_CONFIG_SYSTEM=str(tmp_path / "nonexistent-gitconfig"),
    )

    def run(args, cwd):
        # encoding/errors spelled out: the locale codec is cp1252 on the
        # Windows runners and the decode raises inside subprocess's reader
        # thread (#856, pinned by tests/test_encoding_seam.py).
        return subprocess.run(
            [git, *args], cwd=str(cwd), env=env, capture_output=True,
            encoding="utf-8", errors="replace",
        )

    main = tmp_path / "origrepo"
    main.mkdir()
    run(["init", "-q"], main)
    (main / "f.txt").write_text("v1\n", encoding="utf-8")
    run(["add", "f.txt"], main)
    run(["commit", "-qm", "c1"], main)
    wt = tmp_path / "wt"
    added = run(["worktree", "add", "-q", str(wt), "-b", "feat"], main)
    # A setup that failed silently would leave every assertion below vacuous,
    # and the vacuous run is the one on the platform this was not written on.
    assert wt.is_dir(), f"git worktree add did not run: {added.stderr}"
    copy = tmp_path / "wtcopy"
    shutil.copytree(wt, copy, symlinks=True)

    assert common.foreign_worktree(str(wt)) is None
    found = common.foreign_worktree(str(copy))
    assert found is not None, (
        "real git's copied worktree was not detected; the fabricated layout "
        "the other tests use no longer matches what git writes"
    )
    here, registered = found
    assert _same(here, copy)
    assert _same(registered, wt)


def test_repo_label_discloses_the_copy(tmp_path, monkeypatch) -> None:
    """`Repo:` exists so a write says where it landed (#692). In a copy it named
    the copy, which is the one directory the write does NOT reach."""
    _main, wt, copy = _layout(tmp_path)
    monkeypatch.chdir(copy)
    monkeypatch.setattr(
        common,
        "_git",
        lambda args, timeout=None: subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout=f"{copy}\n", stderr=""
        ),
    )
    label = common.repo_label()
    assert common.FOREIGN_WORKTREE_MARKER in label
    assert str(wt) in label


# ── half 2: the render ───────────────────────────────────────────────────────


def _ok(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["git"], returncode=0, stdout=stdout, stderr="")


def _dead(returncode: int, stderr: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout="", stderr=stderr
    )


#: `git rev-parse --verify --quiet HEAD` in a repository that has commits.
_HEAD_EXISTS = _ok("abc1234\n")

#: The same call in one that has none. Exit 1 and an EMPTY stderr is
#: `--quiet`'s contract for "no such ref", and it is what tells an unborn HEAD
#: apart from a git that failed — without reading git's English.
_HEAD_UNBORN = _dead(1, "")


def _render(monkeypatch, *, porcelain: str, diff_head, head_probe=_HEAD_EXISTS) -> str:
    def fake(args, timeout=None):
        head = args[0] if args else ""
        if head == "status":
            return _ok(porcelain)
        if head == "diff":
            return diff_head
        if head == "stash":
            return _ok("")
        if head == "branch":
            return _ok("* fix/1536 abc1234 [origin/fix/1536] subject\n")
        if head == "rev-parse":
            if "--abbrev-ref" in args:
                return _ok("fix/1536\n")
            if args[-1:] == ["HEAD"]:
                return head_probe
            return _dead(1, "")
        if head == "rev-list":
            return _ok("0\t0\n")
        if head == "log":
            return _ok("abc1234 2026-08-13 t | subject\n")
        if head == "for-each-ref":
            return _ok("")
        return _dead(1, "")

    monkeypatch.setattr(status, "_spawn_git", fake)
    monkeypatch.setattr(status, "_hosted_request", lambda cmd: None)
    monkeypatch.setattr(sys, "argv", ["status.py"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        status.main()
    return buf.getvalue()


# Measured on git 2.x, in a real repository, because the two readers do NOT
# agree and the whole comparison rests on it:
#
#   git status --porcelain=v1     M  "with space.txt"     M  "uni \303\251.txt"
#   git diff --name-only HEAD        with space.txt          "uni \303\251.txt"
#
# porcelain quotes a space (its own format is space-separated); `--name-only`
# does not. The raw path is the only form both can be brought to, and `-z` is
# what gets it from the diff side.


def test_a_space_in_a_staged_path_is_not_a_false_alarm(tmp_path, monkeypatch) -> None:
    """Ordinary staged work on a file whose name has a space in it."""
    monkeypatch.chdir(tmp_path)
    out = _render(
        monkeypatch,
        porcelain='M  "with space.txt"\n',
        diff_head=_ok("with space.txt\0"),
    )
    assert status.STAGED_ABSENT_MARKER not in out, (
        "a staged path git quoted on one side and not the other was reported "
        "as content no file here has — a loud false alarm on an ordinary edit"
    )


def test_an_octal_escaped_staged_path_is_not_a_false_alarm(tmp_path, monkeypatch) -> None:
    """`core.quotePath` octal-escapes the UTF-8 bytes; `-z` hands them back raw."""
    monkeypatch.chdir(tmp_path)
    out = _render(
        monkeypatch,
        porcelain='M  "uni \\303\\251.txt"\n',
        diff_head=_ok("uni é.txt\0"),
    )
    assert status.STAGED_ABSENT_MARKER not in out


def test_a_quoted_path_really_absent_is_still_flagged(tmp_path, monkeypatch) -> None:
    """The unquoting must not disable the check it exists to make work."""
    monkeypatch.chdir(tmp_path)
    out = _render(
        monkeypatch,
        porcelain='MM "with space.txt"\n',
        diff_head=_ok(""),
    )
    assert status.STAGED_ABSENT_MARKER in out


def test_a_rename_from_a_quoted_source_reads_its_destination(tmp_path, monkeypatch) -> None:
    """` -> ` inside the quoted source half must not be read as the separator."""
    monkeypatch.chdir(tmp_path)
    out = _render(
        monkeypatch,
        porcelain='R  "old -> file.py" -> new.py\n',
        diff_head=_ok("old -> file.py\0new.py\0"),
    )
    assert status.STAGED_ABSENT_MARKER not in out


def test_staged_content_no_file_here_has_is_disclosed(tmp_path, monkeypatch) -> None:
    """The live shape: index reverted to an old blob, working tree untouched.

    `git status` renders `MM` and the op listed it under Staged like any other
    staged change."""
    monkeypatch.chdir(tmp_path)
    out = _render(monkeypatch, porcelain="MM validators/fence.py\n", diff_head=_ok(""))
    assert status.STAGED_ABSENT_MARKER in out, (
        "staged content that no file in this tree has was rendered as ordinary "
        "staged work"
    )
    assert "validators/fence.py" in out
    # It discloses; it never hides the list it could not vouch for.
    assert "### Staged (1)" in out


def test_ordinary_staged_work_gains_no_marker(tmp_path, monkeypatch) -> None:
    """The common case must stay silent, or the marker is noise nobody reads."""
    monkeypatch.chdir(tmp_path)
    out = _render(
        monkeypatch,
        porcelain="M  validators/fence.py\n",
        diff_head=_ok("validators/fence.py\0"),
    )
    assert status.STAGED_ABSENT_MARKER not in out
    assert "### Staged (1)" in out


def test_a_staged_rename_is_read_on_its_destination(tmp_path, monkeypatch) -> None:
    """`R  old -> new`: the path git reports in a diff is the destination."""
    monkeypatch.chdir(tmp_path)
    out = _render(
        monkeypatch,
        porcelain="R  a.py -> b.py\n",
        diff_head=_ok("a.py\0b.py\0"),
    )
    assert status.STAGED_ABSENT_MARKER not in out


def test_the_check_that_could_not_run_says_so(tmp_path, monkeypatch) -> None:
    """Three states. A `git diff` that did not answer is not a clean answer."""
    monkeypatch.chdir(tmp_path)
    out = _render(
        monkeypatch,
        porcelain="M  validators/fence.py\n",
        diff_head=_dead(128, "fatal: unable to read index"),
    )
    assert status.STAGED_PROVENANCE_UNKNOWN in out
    assert "unable to read index" in out
    assert status.STAGED_ABSENT_MARKER not in out


def test_an_unborn_head_says_nothing_at_all(tmp_path, monkeypatch) -> None:
    """`git init && git add .` is an ordinary state, not a failed check.

    With no HEAD there is nothing the index could be a revert of, so the
    question is meaningless rather than unanswered — and a `git diff HEAD`
    that legitimately cannot run must not spend a paragraph plus the
    INCOMPLETE footer saying so on every fresh repository.
    """
    monkeypatch.chdir(tmp_path)
    out = _render(
        monkeypatch,
        porcelain="A  a.txt\n",
        diff_head=_dead(128, "fatal: ambiguous argument 'HEAD': unknown revision"),
        head_probe=_HEAD_UNBORN,
    )
    assert status.STAGED_PROVENANCE_UNKNOWN not in out
    assert status.STAGED_ABSENT_MARKER not in out
    assert status.INCOMPLETE_MARKER not in out, (
        "a fresh repository was reported as a run with a skipped section"
    )
    assert "### Staged (1)" in out


def test_a_failed_diff_in_a_repository_that_has_commits_still_says_unknown(
    tmp_path, monkeypatch
) -> None:
    """The silence above is bought by one probe, and only that probe buys it:
    a HEAD that exists and a diff that failed is still an unanswered check."""
    monkeypatch.chdir(tmp_path)
    out = _render(
        monkeypatch,
        porcelain="A  a.txt\n",
        diff_head=_dead(128, "fatal: unable to read index"),
        head_probe=_HEAD_EXISTS,
    )
    assert status.STAGED_PROVENANCE_UNKNOWN in out
    assert status.INCOMPLETE_MARKER in out


def test_a_probe_that_did_not_answer_does_not_buy_silence(tmp_path, monkeypatch) -> None:
    """A `rev-parse` that timed out has not established there is no HEAD."""
    monkeypatch.chdir(tmp_path)
    out = _render(
        monkeypatch,
        porcelain="A  a.txt\n",
        diff_head=_dead(128, "fatal: unable to read index"),
        head_probe=_dead(status.TIMEOUT_RC, "timed out after 5s"),
    )
    assert status.STAGED_PROVENANCE_UNKNOWN in out


def test_a_clean_tree_asks_nothing_and_says_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    out = _render(monkeypatch, porcelain="", diff_head=_dead(128, "never asked"))
    assert status.STAGED_ABSENT_MARKER not in out
    assert status.STAGED_PROVENANCE_UNKNOWN not in out
    assert "never asked" not in out


def test_the_render_leads_with_the_copy(tmp_path, monkeypatch) -> None:
    """Every number below the header is about the other tree, so the reader has
    to meet that before any of them."""
    _main, wt, copy = _layout(tmp_path)
    monkeypatch.chdir(copy)
    out = _render(monkeypatch, porcelain="MM f.txt\n", diff_head=_ok(""))
    assert common.FOREIGN_WORKTREE_MARKER in out
    assert str(wt) in out
    head = out.splitlines()[:6]
    assert any(common.FOREIGN_WORKTREE_MARKER in l for l in head), (
        f"the disclosure was not at the top of the render: {head}"
    )


def test_an_ordinary_worktree_gains_no_banner(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    out = _render(monkeypatch, porcelain="", diff_head=_ok(""))
    assert common.FOREIGN_WORKTREE_MARKER not in out


# ── git-worktrees ────────────────────────────────────────────────────────────


def _worktrees_output(monkeypatch) -> str:
    """`git-worktrees` up to its first git call, which is made to fail.

    The banner has to precede everything, including the op's own refusals —
    the listing it would print is a listing of the other repository.
    """
    worktrees = _load("git_worktrees_1536", "worktrees.py")
    monkeypatch.setattr(
        worktrees, "_git",
        lambda args, timeout=None: _dead(128, "fatal: not a git repository"),
    )
    monkeypatch.setattr(sys, "argv", ["worktrees.py"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        worktrees.main()
    return buf.getvalue()


def test_git_worktrees_names_the_copy_it_cannot_list(tmp_path, monkeypatch) -> None:
    _main, wt, copy = _layout(tmp_path)
    monkeypatch.chdir(copy)
    out = _worktrees_output(monkeypatch)
    assert common.FOREIGN_WORKTREE_MARKER in out, (
        "the op that exists to say whose tree is whose said nothing about the "
        "one directory whose answer is not the one it printed"
    )
    assert str(wt) in out


def test_git_worktrees_in_the_registered_tree_gains_no_banner(
    tmp_path, monkeypatch
) -> None:
    _main, wt, _copy = _layout(tmp_path)
    monkeypatch.chdir(wt)
    assert common.FOREIGN_WORKTREE_MARKER not in _worktrees_output(monkeypatch)
