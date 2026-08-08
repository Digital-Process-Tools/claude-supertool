"""Create-rollback decides identity on the path the writer never used (#1136).

`_atomic_write` follows a symlink to its target before `os.replace`, on purpose:
replacing the link with a regular file would leave the real file untouched. So
for `paste:link.py` where `link.py -> target.py`, the bytes land in `target.py`.

`_run_with_validators` samples `_pre_existed = os.path.isfile(path)` and rolls
back with `os.unlink(path)` -- both on the UNRESOLVED path. For a symlink whose
target does not exist yet, `isfile(link)` is False (it follows and finds
nothing), so the rollback reads "this call created it" and deletes the LINK,
which the call did not create, while the bytes it did write survive in the
target. The footer then prints `nothing changed on disk` over two changes.

Three failures, one cause: the reader/writer resolved the path and the rollback
did not. v0.29.0 has no create-rollback at all and is honest here (`1 write`,
link intact) -- so a test asserting only "the link survives" would pass on
v0.29.0 for the wrong reason. Each test below also pins that the write was in
fact undone, which is the half v0.29.0 does not do.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import supertool

NL = chr(10)
Q3 = chr(39) * 3
BROKEN = "def f(:" + NL + "    pass" + NL


def _symlink(link: Path, target_name: str) -> None:
    """Create link -> target_name, or skip: Windows needs a privilege for this."""
    try:
        os.symlink(target_name, str(link))
    except (OSError, NotImplementedError, AttributeError) as e:
        pytest.skip("symlinks unavailable on this platform: " + str(e))


def _toml_path(target: Path) -> str:
    return chr(34) + str(target).replace(chr(92), chr(92) * 2) + chr(34)


def _paste(tmp_path: Path, target: Path, content: str) -> str:
    body = (
        "path = " + _toml_path(target) + NL
        + "content = " + Q3 + content + Q3 + NL
    )
    p = tmp_path / "p.toml"
    p.write_text(body, encoding="utf-8")
    return supertool.dispatch("paste:@" + str(p))


def test_a_rolled_back_write_through_a_dangling_link_keeps_the_link(
    tmp_path: Path,
) -> None:
    """The destroy. The link pre-existed; nothing in this call created it."""
    link = tmp_path / "link.py"
    _symlink(link, "target.py")
    out = _paste(tmp_path, link, BROKEN)
    assert os.path.islink(str(link)), (
        "the rollback deleted a symlink the call never created:" + NL + out)


def test_a_rolled_back_write_through_a_dangling_link_removes_the_target(
    tmp_path: Path,
) -> None:
    """The artifact. The bytes went to the target; the undo has to go there too."""
    link = tmp_path / "link.py"
    target = tmp_path / "target.py"
    _symlink(link, "target.py")
    out = _paste(tmp_path, link, BROKEN)
    assert not target.exists(), (
        "the file the write actually created survived its own failed "
        "validation:" + NL + out)


def test_the_footer_is_true_of_the_filesystem(tmp_path: Path) -> None:
    """`nothing changed on disk` is a claim about the disk, not about the loop."""
    link = tmp_path / "link.py"
    target = tmp_path / "target.py"
    _symlink(link, "target.py")
    out = _paste(tmp_path, link, BROKEN)
    assert "rolled back" in out, out
    assert os.path.islink(str(link)) and not target.exists(), (
        "the footer says nothing changed; the filesystem disagrees:" + NL + out)


def test_a_write_through_a_link_to_an_existing_file_is_restored_not_unlinked(
    tmp_path: Path,
) -> None:
    """The other half of the identity bug, and the worse one if the fix overshoots.

    Resolving is only half the answer: having resolved, an EXISTING target must
    still take the `restore` arm. Unlinking here would destroy a real file.
    """
    link = tmp_path / "link.py"
    target = tmp_path / "target.py"
    target.write_text("y = 2" + NL, encoding="utf-8")
    _symlink(link, "target.py")
    out = _paste(tmp_path, link, BROKEN)
    assert target.exists(), "a pre-existing target was deleted:" + NL + out
    assert target.read_text(encoding="utf-8") == "y = 2" + NL, out
    assert os.path.islink(str(link)), out


def test_a_clean_write_through_a_dangling_link_still_lands(tmp_path: Path) -> None:
    """The boundary. Nothing here may make a valid symlinked write stop working."""
    link = tmp_path / "link.py"
    target = tmp_path / "target.py"
    _symlink(link, "target.py")
    out = _paste(tmp_path, link, "x = 1" + NL)
    assert target.exists() and target.read_text(encoding="utf-8").startswith("x = 1"), out
    assert os.path.islink(str(link)), out
    assert "rolled back" not in out, out


def test_a_plain_created_file_is_still_unlinked(tmp_path: Path) -> None:
    """#1088's own case, re-pinned: no symlink, and the create-rollback still fires."""
    target = tmp_path / "plain.py"
    out = _paste(tmp_path, target, BROKEN)
    assert not target.exists(), out
    assert "rolled back" in out, out


def test_the_retraction_names_the_target_not_the_link(tmp_path: Path) -> None:
    """The line this fix could most easily make false.

    `link.py removed` was accurate only while the rollback was deleting the
    wrong object. Deleting the right one without rewording it would tell a
    reader their symlink is gone, which is now the opposite of the truth.
    """
    link = tmp_path / "link.py"
    _symlink(link, "target.py")
    out = _paste(tmp_path, link, BROKEN)
    assert "target.py removed" in out, out
    assert "link.py removed" not in out, (
        "the retraction says the link was removed; it was not:" + NL + out)
    assert "the link is intact" in out, out


def test_the_writer_and_the_rollback_ask_one_function() -> None:
    """Reachable on every platform, including the ones that cannot make a symlink.

    Every other test here needs a real symlink and therefore skips on a Windows
    runner without the create-symlink privilege -- which is exactly where this
    bug class survives unseen. What can be checked anywhere is the invariant the
    fix rests on: the writer resolves the path through `_write_target`, and so
    does the rollback. Two hand-written copies of `realpath if islink` is how
    they came apart in the first place, and platform differences in `islink`
    (Windows junctions) only matter if the two sides can disagree.
    """
    import inspect

    writer = inspect.getsource(supertool._atomic_write)
    rollback = inspect.getsource(supertool._run_with_validators)
    assert "_write_target(path)" in writer, writer
    assert "_write_target(path)" in rollback, (
        "the rollback resolves the write target some other way:" + NL + rollback)
    for src, where in ((writer, "_atomic_write"), (rollback, "_run_with_validators")):
        assert "os.path.realpath(path) if os.path.islink" not in src, (
            where + " re-inlined the resolution instead of calling the shared "
            "helper -- that is the drift this bug came from")
    assert supertool._write_target("no/such/plain/path.py") == "no/such/plain/path.py"


def test_the_refusal_names_the_object_it_refused_to_touch(
    tmp_path: Path, monkeypatch
) -> None:
    """The refuse arm is the one that reports without acting, so its only output
    IS the fix. Naming the link there while the arm beside it names the target
    would describe two files as one."""
    monkeypatch.setattr(supertool, "_rollback_action", lambda pre, content: "refuse")
    link = tmp_path / "link.py"
    target = tmp_path / "target.py"
    target.write_text("y = 2" + NL, encoding="utf-8")
    _symlink(link, "target.py")
    out = _paste(tmp_path, link, BROKEN)
    assert "ROLLBACK NOT POSSIBLE" in out, out
    assert "target.py (which the symlink" in out, (
        "the refusal names the symlink, not the file it declined to undo:"
        + NL + out)
    assert "resolves to), whose pre-edit bytes could not be read" in out, out
