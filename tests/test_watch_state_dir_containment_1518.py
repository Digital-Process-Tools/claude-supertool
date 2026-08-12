"""The derived state directory must be one this process made and owns (#1518).

`ensure_state_dir` was `os.makedirs(state_dir, mode=0o700, exist_ok=True)` on
`/tmp/supertool-watch-<name>`. Three things had to be true at once for that to
be a boundary, and none of them were:

* `exist_ok=True` accepts whatever already holds the name, and `os.path.isdir`
  inside `makedirs` follows symlinks -- so a link planted at the leaf is
  adopted, and `claim_pidfile`'s `O_CREAT|O_EXCL` then creates the pid file
  *inside the planter's directory*, where it is a perfectly valid new file.
* `mode=0o700` applies only to directories that call creates. In the pre-taken
  case it is not a defence, it is a comment.
* the name is public: `6047d98` commits `"watch_name": "oss-supertool"` to this
  repo's own `.supertool.json`, and `/tmp` is `drwxrwxrwt`.

The fix reuses `presets/_image_root.ensure`, written one directory over in this
same release for the identical defect (#1493/#1504), rather than hand-rolling a
third boundary. The tests below therefore assert on **the caller's behaviour** --
what exists outside the refused root after the call -- and lean on
`tests/test_attachment_root_ownership_1493.py` for the module's own arms.

**What is not closed, and is asserted nowhere because it is not a defect.**
Another local uid can still squat `/tmp/supertool-watch-<name>` ahead of us with
a directory of their own. We then refuse and no poller spawns on that channel
until it is removed, which is the correct side of the trade and the same
residual `docs/presets/gitlab.md` discloses for the attachment root.

**Windows.** `os.symlink` needs a privilege there and `st_uid` is a constant, so
the two symlink arms carry `require_symlink()`. The ownership arm moves *our*
uid instead of the directory's -- `_image_root._euid` is patched -- which reaches
it from the caller on every platform; a `skipif` there would report a coverage
this suite does not have on the one platform whose reds are load-bearing here,
and asserting it against a fabricated stat would restate #1493's file and pass
with this change reverted. The mode arm is the one genuine skip: the permission
bits are synthesized on Windows, so there is nothing to measure. The
file-at-the-name arm runs everywhere.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from _changelog_findable import assert_change_is_findable
from _symlink import require_symlink

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import naming  # noqa: E402

#: Whether this platform can answer "who owns this" at all. Probed rather than
#: read off `os.name`, because the capability is the question.
POSIX_OWNERSHIP = hasattr(os, "geteuid")


def _derived(name: str = "oss"):
    r = naming.resolve({naming.NAME_ENV: name})
    assert r.state_dir_is_derived, "the fixture stopped exercising the created path"
    return r


# ---------------------------------------------------------------------------
# 1. The reported mechanism: a symlink already on the derived name
# ---------------------------------------------------------------------------

def test_a_symlinked_state_directory_is_refused_rather_than_adopted(tmp_path):
    require_symlink()
    victim = tmp_path / "attacker-owned"
    victim.mkdir()
    root = tmp_path / "supertool-watch-oss"
    os.symlink(victim, root)

    why = naming.ensure_state_dir(_derived(), str(root))

    assert why, "a symlink on the state directory name was accepted in silence"
    assert str(root) in why, why
    assert sorted(p.name for p in victim.iterdir()) == [], (
        "the refusal still let something through into {0}".format(victim))


def test_a_symlinked_state_directory_gets_no_pid_file_written_through_it(
        tmp_path, monkeypatch):
    """The end-to-end shape from the issue: `claim_pidfile` is the caller, and
    what it does with the refusal is the only thing an attacker sees.

    Asserted on the victim directory's contents, not on the return value -- a
    call site can consult a guard and write anyway (#1484)."""
    require_symlink()
    import transport  # noqa: PLC0415

    victim = tmp_path / "attacker-owned"
    victim.mkdir()
    root = tmp_path / "supertool-watch-oss"
    os.symlink(victim, root)
    monkeypatch.setattr(transport, "RESOLVED", _derived())
    monkeypatch.setattr(transport, "STATE_DIR", str(root))

    claim = transport.claim_pidfile("gh-prs", "x")

    landed = sorted(p.name for p in victim.iterdir())
    assert landed == [], "a pid file landed in the planted directory: {0}".format(landed)
    assert claim == transport.CLAIM_UNKNOWN, (
        "an unestablished state directory must not report a slot as claimed "
        "(0) nor name a PID that holds it; got {0!r}".format(claim))


# ---------------------------------------------------------------------------
# 2. The arms that need no privilege
# ---------------------------------------------------------------------------

def test_a_plain_file_on_the_derived_name_is_a_refusal_not_a_traceback(tmp_path):
    """**This one passed before the fix too**, and is kept deliberately: it is
    the only hostile-name arm reachable on a runner with no symlink privilege,
    and the refusal it pins changed wording under the new boundary. Recorded
    here rather than left for a reader to discover, because a test that would
    pass if the code did nothing is not evidence and must not be counted as
    any."""
    root = tmp_path / "supertool-watch-oss"
    root.write_text("not a directory", encoding="utf-8")

    why = naming.ensure_state_dir(_derived(), str(root))

    assert why, "a regular file on the state directory name was accepted"
    assert str(root) in why, why


def test_a_directory_left_loose_by_an_earlier_run_is_tightened(tmp_path):
    """`mode=` on the create is silent about a directory that already exists,
    which is half of why the old spelling was not a boundary."""
    if not POSIX_OWNERSHIP:
        pytest.skip("permission bits are synthesized on this platform")
    root = tmp_path / "supertool-watch-oss"
    root.mkdir()
    os.chmod(root, 0o777)

    why = naming.ensure_state_dir(_derived(), str(root))

    assert why == "", why
    mode = stat.S_IMODE(os.stat(root).st_mode)
    assert mode & 0o077 == 0, "group or other can still reach {0}: {1}".format(
        root, oct(mode))


def test_a_root_owned_by_another_uid_is_refused(tmp_path, monkeypatch):
    """Driven through `ensure_state_dir`, by moving *our* uid rather than the
    directory's: a test cannot chown to a uid it does not have, and `st_uid` is
    a constant on Windows. Patching the module's own `_euid` is what makes this
    arm reachable from the caller on every platform — asserting it against a
    fabricated stat would restate `test_attachment_root_ownership_1493.py` and
    would pass with this file's production change reverted."""
    root = tmp_path / "supertool-watch-oss"
    root.mkdir()
    monkeypatch.setattr(naming._image_root, "_euid", lambda: 4242)

    why = naming.ensure_state_dir(_derived(), str(root))

    assert why, "a directory owned by another uid was accepted as our root"
    assert "4242" in why, why
    assert "no poller slot can be claimed there" in why, why


# ---------------------------------------------------------------------------
# 3. The boundary this function is otherwise all about, unchanged
# ---------------------------------------------------------------------------

def test_a_supplied_state_directory_is_still_never_touched(tmp_path):
    """#693's contract survives the containment fix: only a *derived* directory
    is created, so a hostile one at an operator-supplied path is still that
    operator's business and stays an unanswerable state, not a refusal."""
    target = tmp_path / "supplied"
    r = naming.resolve({naming.NAME_ENV: "oss",
                        naming.STATE_DIR_ENV: str(target)})
    assert not r.state_dir_is_derived

    assert naming.ensure_state_dir(r, str(target)) == ""
    assert not target.exists()


def test_the_change_is_documented():
    assert_change_is_findable(1518)
