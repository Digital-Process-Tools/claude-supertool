"""`gh-issue`'s attachment root, and the number the API chose (#1506).

Two defects on master, in the one file, and they are #1493 and #1484 one forge
over -- both already fixed on the GitLab side:

1. `IMAGE_DIR` was the literal `"/tmp/supertool-images/gh"`: a fixed name in a
   shared, world-writable directory, so any local user can take it first -- as a
   directory of their own, or as a symlink pointing anywhere. It is also a POSIX
   literal, which on Windows anchors to the current drive rather than to the
   platform temp directory.
2. `os.path.join(IMAGE_DIR, issue_number)` where `issue_number` is
   `str(d["number"])` out of the `gh issue view --json` reply, with **no numeric
   check and no containment check at all** -- this module had no `_is_inside`.
   `_local_path` sanitizes the basename only, so the directory component was
   whatever the remote said.

These assert on the **filesystem** -- what exists under and outside the root
after the call -- and on the `ImageResult` list the op renders from, never on a
guard having been called. A site can call a guard and write anyway (#1484's
rule, kept here).

`_http.download` is replaced by a stand-in that does exactly what the real one
does *once a body has passed the policy*: `makedirs(parent)`, then write. That
is what makes master's escape reproducible with no network, and it is the reason
the refusal has to happen in the caller: the write is inside `download`, and by
the time it runs the directory is already being created.

The ownership and mode arms of the root check are not re-pinned here --
`tests/test_attachment_root_ownership_1493.py` exercises them through
`_image_root.refusal`, a pure function of a stat result, because the filesystem
cannot reach them on Windows. What is pinned here is that this op is *wired* to
that establishment and reports its refusal instead of writing.
"""
from __future__ import annotations

import importlib.util
import os
import stat
import tempfile
from pathlib import Path

from _symlink import require_symlink

REPO = Path(__file__).resolve().parents[1]

#: An allowlisted host, so nothing below is refused by the #817 destination
#: policy -- these tests are about the destination on *this* machine.
URL = "https://user-images.githubusercontent.com/1/shot.png"
URL2 = "https://user-images.githubusercontent.com/2/other.png"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    assert spec is not None and spec.loader is not None, rel
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


issue = _load("github_issue_1506", "presets/github/issue.py")


class _Writer:
    """`_http.download` from the `makedirs` onward -- the part that reaches disk.

    A refusal is proved by the filesystem being empty, which only means anything
    if the non-refusing path really writes. `dests` tells a refusal apart from a
    fetch that failed for its own reasons.
    """

    def __init__(self) -> None:
        self.dests: list[str] = []

    def __call__(self, url: str, dest: str, **kwargs) -> int:
        self.dests.append(dest)
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(b"PNGDATA")
        return 7


def _wire(monkeypatch, root) -> _Writer:
    writer = _Writer()
    monkeypatch.setattr(issue, "IMAGE_DIR", str(root))
    monkeypatch.setattr(issue._http, "download", writer)
    return writer


# ---------------------------------------------------------------------------
# 1. The root
# ---------------------------------------------------------------------------

def test_the_root_is_under_the_platform_temp_dir_not_a_posix_literal() -> None:
    """`/tmp/...` is anchored to the current drive on Windows, and shared by
    every user on POSIX. Asserted against `gettempdir()` rather than against a
    string, so it is the same claim on both."""
    parent = os.path.dirname(issue.IMAGE_DIR)
    assert parent == tempfile.gettempdir(), (
        "IMAGE_DIR is {0!r}, whose parent is not this platform's temp "
        "directory {1!r}".format(issue.IMAGE_DIR, tempfile.gettempdir())
    )


def test_the_root_is_not_the_same_name_for_every_user_on_the_machine() -> None:
    """A fixed leaf is the whole of #1493: whoever creates it first owns it.

    On a platform with no uid to put in the name there is nothing to assert --
    `gettempdir()` is already inside the user profile there, and the test above
    is the one that answers. Returning is not passing a weaker version of this.
    """
    if not hasattr(os, "geteuid"):
        return
    assert str(os.geteuid()) in os.path.basename(issue.IMAGE_DIR), (
        "IMAGE_DIR {0!r} carries no uid, so every user on this machine races "
        "for the same name".format(issue.IMAGE_DIR)
    )


def test_a_root_that_is_a_symlink_gets_no_bytes_written_through_it(
    tmp_path, monkeypatch
) -> None:
    """`realpath` on both sides of a containment check resolves *through* a
    planted root link, so containment approves the attacker's directory. Nothing
    established what the boundary was."""
    require_symlink()
    victim = tmp_path / "attacker-owned"
    victim.mkdir()
    root = tmp_path / "img"
    os.symlink(victim, root)
    writer = _wire(monkeypatch, root)

    results = issue._download_images([URL], "1506")

    left = sorted(str(p) for p in victim.rglob("*"))
    assert left == [], "wrote through the symlinked root into {0}: {1}".format(victim, left)
    assert writer.dests == [], "fetched before the root was established"
    assert [r.state for r in results] == [issue.IMAGE_REFUSED], results
    assert "symlink" in (results[0].reason or "").lower(), results[0].reason


def test_a_plain_file_at_the_root_is_a_refusal_not_a_traceback(
    tmp_path, monkeypatch
) -> None:
    """Reachable on every platform, no symlink privilege needed. On master the
    `makedirs` inside `download` raises straight out of the op."""
    root = tmp_path / "img"
    root.write_text("not a directory", encoding="utf-8")
    writer = _wire(monkeypatch, root)

    results = issue._download_images([URL], "1506")

    assert root.read_text(encoding="utf-8") == "not a directory", "clobbered the file"
    assert writer.dests == []
    assert [r.state for r in results] == [issue.IMAGE_REFUSED], results


def test_a_root_that_cannot_be_established_refuses_every_url_it_was_given(
    tmp_path, monkeypatch
) -> None:
    """One `ImageResult` per URL is `_download_images`'s contract (#817), and a
    root refusal must not shorten the list -- a URL that vanishes reads as an
    issue that did not have it. Also pins that the op is wired to
    `_image_root.ensure` at all, rather than reimplementing the question."""
    root = tmp_path / "img"
    writer = _wire(monkeypatch, root)
    monkeypatch.setattr(
        issue._image_root,
        "ensure",
        lambda path: (None, "it is owned by uid 4294967294, not by us"),
    )

    results = issue._download_images([URL, URL2], "1506")

    assert [r.state for r in results] == [issue.IMAGE_REFUSED] * 2, results
    assert all("4294967294" in (r.reason or "") for r in results), results
    assert writer.dests == []


# ---------------------------------------------------------------------------
# 2. The number the API chose, joined onto it
# ---------------------------------------------------------------------------

def test_a_traversing_number_from_the_api_creates_nothing(tmp_path, monkeypatch) -> None:
    """`../escaped` built from `os.pardir`, so this is not a POSIX literal.

    The refusal has to land before the root exists: a directory created is
    already a write and no later guard un-creates it.
    """
    root = tmp_path / "img"
    outside = tmp_path / "escaped"
    writer = _wire(monkeypatch, root)

    results = issue._download_images([URL], os.path.join(os.pardir, "escaped"))

    assert not outside.exists(), "created {0} -- outside the root {1}".format(outside, root)
    assert not root.exists(), "created the root for a number that was refused"
    assert writer.dests == [], "fetched after the number was refused"
    assert [r.state for r in results] == [issue.IMAGE_REFUSED], results
    assert "numeric" in (results[0].reason or "").lower(), results[0].reason


def test_an_absolute_number_from_the_api_creates_nothing(tmp_path, monkeypatch) -> None:
    """`os.path.join` discards the root outright when the second part is
    absolute -- no `..` needed, and no containment check downstream can see it
    because the derived directory is its own boundary."""
    root = tmp_path / "img"
    outside = tmp_path / "abs-escaped"
    writer = _wire(monkeypatch, root)

    results = issue._download_images([URL], str(outside))

    assert not outside.exists(), "os.path.join discarded the root: created {0}".format(outside)
    assert writer.dests == []
    assert [r.state for r in results] == [issue.IMAGE_REFUSED], results


def test_a_number_carrying_a_control_character_cannot_rewrite_the_refusal(
    tmp_path, monkeypatch, capsys
) -> None:
    """The refused value is printed, and it is text a remote host chose. A bare
    CR in it lets the number overwrite the line printed about it -- the same
    rule the refused URLs downstream already follow.

    `chr(13)` rather than an escape, so the byte under test is unmistakable in
    the source of the test as well as at runtime.
    """
    cr = chr(13)
    root = tmp_path / "img"
    writer = _wire(monkeypatch, root)

    results = issue._download_images([URL], "1506" + cr + "REFUSED nothing to see here")
    issue._print_images(results)

    out = capsys.readouterr().out
    assert cr not in out, "a CR from the API reply reached the terminal: {0!r}".format(out)
    assert writer.dests == []
    assert [r.state for r in results] == [issue.IMAGE_REFUSED], results


def test_a_symlink_planted_at_the_per_issue_directory_is_not_written_through(
    tmp_path, monkeypatch
) -> None:
    """The arm that stands in for a second `ensure` on the per-issue directory.

    A numeric number cannot escape by itself, so the remaining way out is a link
    planted *inside* a root an earlier, looser run left behind. The leaf check is
    anchored to the established root -- never to the derived directory, which is
    what #1484 was.
    """
    require_symlink()
    root = tmp_path / "img"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "1506")
    writer = _wire(monkeypatch, root)

    results = issue._download_images([URL], "1506")

    left = sorted(str(p) for p in outside.rglob("*"))
    assert left == [], "wrote through the planted link into {0}: {1}".format(outside, left)
    assert writer.dests == [], "fetched before the destination was contained"
    assert [r.state for r in results] == [issue.IMAGE_REFUSED], results


# ---------------------------------------------------------------------------
# 3. The refusal must not eat the ordinary path
# ---------------------------------------------------------------------------

def test_a_numeric_number_still_downloads(tmp_path, monkeypatch) -> None:
    root = tmp_path / "img"
    writer = _wire(monkeypatch, root)

    results = issue._download_images([URL], "1506")

    assert [r.state for r in results] == [issue.IMAGE_FETCHED], results
    written = Path(results[0].path or "")
    assert written.read_bytes() == b"PNGDATA"
    assert written.parent == root / "1506"
    assert writer.dests == [str(written)]


def test_the_established_root_is_owner_only(tmp_path, monkeypatch) -> None:
    """0700, so nothing can be planted inside it between two calls -- which is
    what lets the per-issue directory be created by `download` rather than
    established a second time. Skipped where the bits are synthesized."""
    if not hasattr(os, "geteuid"):
        return
    root = tmp_path / "img"
    _wire(monkeypatch, root)

    issue._download_images([URL], "1506")

    mode = stat.S_IMODE(os.stat(root).st_mode)
    assert not mode & 0o077, "the root is mode {0} -- group or other can reach it".format(oct(mode))
