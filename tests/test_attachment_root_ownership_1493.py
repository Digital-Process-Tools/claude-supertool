"""`gl-issue`'s attachment root must be one this process owns (#1493).

On master the root was the literal `"/tmp/supertool-images"`. Two defects in one
constant, and the tests below split the same way:

1. **A fixed name in a shared, world-writable directory.** Any local user can
   take that name first -- as a directory of their own, or as a symlink. And
   `_is_inside` realpaths *both* of its arguments, so a symlink planted at the
   root is resolved through on both sides and containment answers `True` about
   the attacker's directory. The check was never wrong; nothing established what
   it was checking against.
2. **A hardcoded POSIX literal.** On Windows a leading-slash path is anchored to
   the current drive, so it lands in a `tmp/supertool-images` at the drive root
   rather than under the platform temp directory.

The filesystem tests assert on **what exists outside the root after the call**,
not on the return value and not on a guard having been called -- a site can call
a guard and write anyway (#1484's rule, kept here).

`_image_root.refusal` is a pure function of a stat result on purpose. The
ownership and mode arms cannot be reached through the filesystem on Windows --
`st_uid` is a constant there and the permission bits are synthesized -- so
exercising them through the predicate is what keeps those assertions from being
vacuous on the one platform whose reds are load-bearing in this repo. The
reparse-point arm is the Windows-specific one and it is **reasoned from
CPython's documented `st_reparse_tag`, not observed on Windows**, which is the
honest grade for a Windows claim written on macOS (#627's precedent).
"""
from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import tempfile
import types
from pathlib import Path

import pytest

from _symlink import require_symlink

REPO = Path(__file__).resolve().parents[1]

#: Whether this platform can answer "who owns this" at all. Probed, not read off
#: `os.name`: the question is the capability, and a platform-name gate would
#: skip a POSIX host that spells its name unexpectedly.
POSIX_OWNERSHIP = hasattr(os, "geteuid")

URLS = ["https://gitlab.example/-/project/uploads/abc123/shot.png"]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    assert spec is not None and spec.loader is not None, rel
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gl = _load("gitlab_issue_1493", "presets/gitlab/issue.py")


def _image_root():
    """Imported inside the tests that need it, so a missing module reddens only
    the predicate tests rather than erroring the whole file at collection."""
    return _load("image_root_1493", "presets/_image_root.py")


class _Writer:
    """A `subprocess.run` that succeeds, so the bytes really land somewhere.

    A refusal has to be proved by the filesystem being empty, which needs the
    non-refusing path to actually write. Counting calls tells a refusal apart
    from a fetch that failed for its own reasons.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return subprocess.CompletedProcess(args=["glab"], returncode=0, stdout=b"PNGDATA")


def _fake_dir_stat(mode: int, uid: int, reparse: int = 0):
    """A stat-shaped stand-in. `refusal` only reads attributes, and
    `os.stat_result` cannot carry `st_reparse_tag` on a POSIX host."""
    return types.SimpleNamespace(st_mode=mode, st_uid=uid, st_reparse_tag=reparse)


# ---------------------------------------------------------------------------
# 1. The reported mechanism: a symlink at the root itself
# ---------------------------------------------------------------------------

def test_a_symlinked_root_gets_no_bytes_written_through_it(tmp_path, monkeypatch, capsys):
    """The whole of #1493. `_is_inside` realpaths both sides, so on master both
    arguments resolve *through* the planted link and containment approves."""
    require_symlink()
    victim = tmp_path / "attacker-owned"
    victim.mkdir()
    root = tmp_path / "images"
    os.symlink(victim, root)
    monkeypatch.setattr(gl, "IMAGE_DIR", str(root))
    run = _Writer()
    monkeypatch.setattr(gl.subprocess, "run", run)

    got = gl._download_images(URLS, "12345")

    left = sorted(str(p) for p in victim.rglob("*"))
    assert left == [], "wrote through the symlinked root into {0}: {1}".format(victim, left)
    assert got == [], "reported a download after the root was refused: {0}".format(got)
    assert run.calls == 0, "fetched an attachment before the root was established"
    assert "skipped" in capsys.readouterr().out.lower(), "refused in silence"


def test_a_plain_file_at_the_root_is_a_refusal_not_a_traceback(tmp_path, monkeypatch, capsys):
    """Reachable on every platform, no symlink privilege needed. On master
    `os.makedirs` raises straight out of the op."""
    root = tmp_path / "images"
    root.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(gl, "IMAGE_DIR", str(root))
    run = _Writer()
    monkeypatch.setattr(gl.subprocess, "run", run)

    got = gl._download_images(URLS, "12345")

    assert got == []
    assert run.calls == 0
    assert root.read_text(encoding="utf-8") == "not a directory", "clobbered the file"
    assert "skipped" in capsys.readouterr().out.lower()


def test_a_non_directory_on_the_per_issue_name_is_a_refusal_not_a_traceback(
        tmp_path, monkeypatch, capsys):
    """The root can be sound and the name under it still not be usable.

    A root an earlier, looser run left at 0755 could have had a name planted
    inside it before this call tightened it, so the per-issue directory goes
    through the same establishment as the root rather than an
    `os.makedirs(exist_ok=True)` that accepts whatever is on the name -- which on
    master raised `FileExistsError` straight out of the op.

    A *symlink* on that name is deliberately not the case tested here: the
    per-file check anchored to the root (#1484) already refuses anything
    resolving out of it, so a symlink test would pass with this arm removed and
    would be measuring #1484 rather than this. What that check cannot do is stop
    the traceback, keep the per-issue directory at 0700, or refuse before the
    per-file loop is entered.
    """
    root = tmp_path / "images"
    root.mkdir(mode=0o700)
    (root / "12345").write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(gl, "IMAGE_DIR", str(root))
    run = _Writer()
    monkeypatch.setattr(gl.subprocess, "run", run)

    got = gl._download_images(URLS, "12345")

    assert got == []
    assert run.calls == 0
    assert (root / "12345").read_text(encoding="utf-8") == "not a directory"
    assert "skipped" in capsys.readouterr().out.lower(), "refused in silence"


@pytest.mark.skipif(not POSIX_OWNERSHIP, reason="mode bits are not answerable here")
def test_the_per_issue_directory_is_owner_only_too(tmp_path, monkeypatch):
    """A 0700 root with a 0755 directory inside it is still reachable."""
    root = tmp_path / "images"
    monkeypatch.setattr(gl, "IMAGE_DIR", str(root))
    monkeypatch.setattr(gl.subprocess, "run", _Writer())

    got = gl._download_images(URLS, "12345")

    assert len(got) == 1
    mode = stat.S_IMODE(os.stat(str(root / "12345")).st_mode)
    assert mode == 0o700, "per-issue directory is {0}".format(oct(mode))


# ---------------------------------------------------------------------------
# 2. The root this process creates, and what it can prove about it
# ---------------------------------------------------------------------------

def test_the_ordinary_path_still_downloads_into_a_root_it_created(tmp_path, monkeypatch):
    """The refusal must not eat the working case, and the root must be made by
    the call rather than assumed to exist."""
    root = tmp_path / "images"
    monkeypatch.setattr(gl, "IMAGE_DIR", str(root))
    monkeypatch.setattr(gl.subprocess, "run", _Writer())

    got = gl._download_images(URLS, "12345")

    assert len(got) == 1, "numeric iid downloaded nothing: {0}".format(got)
    written = Path(got[0])
    assert written.read_bytes() == b"PNGDATA"
    assert written.parent == root / "12345"


@pytest.mark.skipif(not POSIX_OWNERSHIP, reason="st_uid and mode bits are not answerable here")
def test_the_created_root_is_owner_only_and_owned_by_us(tmp_path, monkeypatch):
    root = tmp_path / "images"
    monkeypatch.setattr(gl, "IMAGE_DIR", str(root))
    monkeypatch.setattr(gl.subprocess, "run", _Writer())

    gl._download_images(URLS, "12345")

    st = os.stat(str(root))
    assert st.st_uid == os.geteuid(), "the root is not ours"
    assert stat.S_IMODE(st.st_mode) == 0o700, (
        "root mode is {0}, so another local user can reach the attachments and "
        "plant names inside them".format(oct(stat.S_IMODE(st.st_mode)))
    )


@pytest.mark.skipif(not POSIX_OWNERSHIP, reason="st_uid and mode bits are not answerable here")
def test_a_root_left_group_writable_by_an_earlier_run_is_tightened(tmp_path, monkeypatch):
    """A root we own but that is reachable by others is repaired, not refused:
    we own it, so tightening it is ours to do, and refusing would strand a user
    behind a directory only they can delete."""
    root = tmp_path / "images"
    root.mkdir(mode=0o755)
    monkeypatch.setattr(gl, "IMAGE_DIR", str(root))
    monkeypatch.setattr(gl.subprocess, "run", _Writer())

    got = gl._download_images(URLS, "12345")

    assert len(got) == 1, "refused a root we own: {0}".format(got)
    assert stat.S_IMODE(os.stat(str(root)).st_mode) == 0o700


# ---------------------------------------------------------------------------
# 3. The POSIX literal
# ---------------------------------------------------------------------------

def test_the_default_root_sits_in_the_platform_temp_directory():
    """`/tmp` is not the temp directory on Windows and is not on macOS either.

    Vacuous where `gettempdir()` happens to *be* `/tmp` -- Linux -- which is why
    the per-user test below carries the other half of the claim.
    """
    assert os.path.dirname(gl.IMAGE_DIR) == tempfile.gettempdir(), (
        "IMAGE_DIR is {0!r}, whose parent is not this platform's temp "
        "directory {1!r}".format(gl.IMAGE_DIR, tempfile.gettempdir())
    )


@pytest.mark.skipif(not POSIX_OWNERSHIP, reason="the temp directory is already per-user here")
def test_the_default_root_is_not_a_name_every_user_derives(monkeypatch):
    """Two users on one machine must not derive one name in a shared `/tmp`.

    Without this the first user to run the op owns a 0700 directory the second
    can only be refused at -- and, before the refusal existed, wrote into.
    """
    image_root = _image_root()
    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    mine = image_root.default_root()
    monkeypatch.setattr(os, "geteuid", lambda: 1002)
    theirs = image_root.default_root()
    assert mine != theirs, "both uids derive {0!r}".format(mine)


# ---------------------------------------------------------------------------
# 4. The predicate -- the arms the filesystem cannot reach on every platform
# ---------------------------------------------------------------------------

def test_a_root_owned_by_another_uid_is_refused():
    image_root = _image_root()
    st = _fake_dir_stat(stat.S_IFDIR | 0o700, uid=4242)
    why = image_root.refusal(st, uid=501)
    assert why, "a directory owned by uid 4242 was accepted for uid 501"
    assert "4242" in why, "the refusal does not name the owner: {0!r}".format(why)


def test_a_root_we_own_at_0700_is_accepted():
    image_root = _image_root()
    st = _fake_dir_stat(stat.S_IFDIR | 0o700, uid=501)
    assert image_root.refusal(st, uid=501) == ""


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777, 0o750])
def test_a_root_reachable_by_group_or_other_is_refused(mode):
    image_root = _image_root()
    st = _fake_dir_stat(stat.S_IFDIR | mode, uid=501)
    assert image_root.refusal(st, uid=501), "{0} was accepted".format(oct(mode))


def test_a_symlink_stat_is_refused_even_when_it_points_at_our_own_directory():
    image_root = _image_root()
    st = _fake_dir_stat(stat.S_IFLNK | 0o700, uid=501)
    assert image_root.refusal(st, uid=501)


def test_a_non_directory_is_refused():
    image_root = _image_root()
    st = _fake_dir_stat(stat.S_IFREG | 0o600, uid=501)
    assert image_root.refusal(st, uid=501)


def test_a_reparse_point_is_refused():
    """Windows spells a junction as a directory carrying `st_reparse_tag`, and
    `stat.S_ISLNK` denies it -- so the symlink arm alone would let one through.
    Reasoned from CPython's documented field, not observed on Windows."""
    image_root = _image_root()
    st = _fake_dir_stat(stat.S_IFDIR | 0o700, uid=501, reparse=0xA0000003)
    assert image_root.refusal(st, uid=501)


def test_the_refusal_never_returns_a_bare_false():
    """Every rejection carries its reason: an absence without one is the
    defect class this repo keeps filing."""
    image_root = _image_root()
    for st in (
        _fake_dir_stat(stat.S_IFLNK | 0o700, 501),
        _fake_dir_stat(stat.S_IFREG | 0o600, 501),
        _fake_dir_stat(stat.S_IFDIR | 0o777, 501),
        _fake_dir_stat(stat.S_IFDIR | 0o700, 4242),
        _fake_dir_stat(stat.S_IFDIR | 0o700, 501, reparse=0xA0000003),
    ):
        why = image_root.refusal(st, uid=501)
        assert isinstance(why, str) and why.strip(), "refused with no reason: {0!r}".format(why)
