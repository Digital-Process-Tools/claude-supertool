"""#1685 - paste's snapshot wrote the displaced bytes at the umask default.

#1650 made `paste` copy the outgoing bytes aside before it overwrites a file.
`_paste_snapshot` wrote that copy with a bare `open(dest, "wb")`, so the copy
landed at `0666 & ~umask` -- 0644 on a stock box -- whatever the source's mode
was. A `paste` over a 0600 `.env`, `id_rsa` or `.netrc` therefore left the
secret group- and world-readable under `~/.cache/supertool/` for the 7-day
retention window, with no redaction (correctly: it is a backup) and no word in
the receipt.

The fix is not a refusal. Declining to snapshot a mode-restricted file would
close the disclosure by deleting the data-loss net #1650 exists to be, which is
choosing a different failure rather than removing one. What changes is that the
copy inherits a mode no wider than the source's, and that the receipt says
which mode it used.

Every assertion here reads the mode off disk. Asserting the receipt's wording
alone would pass against a snapshot still sitting at 0644, which is the whole
bug.
"""
import os
import stat
from pathlib import Path

import pytest

import supertool
from _symlink import require_symlink

NL = chr(10)
Q3 = chr(39) * 3

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "Windows has no POSIX mode bits -- os.chmod honours only the read-only "
        "flag and st_mode reads back 0o666 whatever was asked for, so both the "
        "source mode these tests set and the snapshot mode they assert would be "
        "fictions. A vacuous pass here is worse than no test: it would report "
        "coverage of a disclosure on a platform where nothing was checked."
    ),
)


def _toml_path(target: Path) -> str:
    return chr(34) + str(target).replace(chr(92), chr(92) * 2) + chr(34)


def _paste(tmp_path: Path, target: Path, content: str) -> str:
    body = (
        "path = " + _toml_path(target) + NL
        + "content = " + Q3 + content + Q3 + NL
    )
    p = tmp_path / "payload.toml"
    p.write_bytes(body.encode("utf-8"))
    return supertool.dispatch("paste:@" + str(p))


def _cache(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(root))
    return root / "supertool" / "paste-backup"


def _secret(tmp_path: Path, mode: int) -> Path:
    target = tmp_path / "secretdemo" / ".env"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        ("AWS_SECRET=hunter2" + NL + "glpat-AAAAAAAAAAAAAAAAAAAA" + NL).encode(
            "utf-8"
        )
    )
    os.chmod(target, mode)
    return target


def _one_snapshot(store: Path, out: str) -> Path:
    snaps = sorted(store.glob("*")) if store.is_dir() else []
    assert snaps, "the overwrite took no snapshot:" + NL + out
    assert len(snaps) == 1, [str(s) for s in snaps]
    return snaps[0]


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_a_0600_source_does_not_get_a_world_readable_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    """The issue, asserted against the filesystem.

    The bytes are the credentials verbatim -- that part is #1650 working as
    designed and is deliberately re-asserted here, so a "fix" that stops taking
    the snapshot fails this test rather than passing it.
    """
    store = _cache(tmp_path, monkeypatch)
    target = _secret(tmp_path, 0o600)

    out = _paste(tmp_path, target, "AWS_SECRET=rotated" + NL)

    snap = _one_snapshot(store, out)
    assert b"glpat-AAAAAAAAAAAAAAAAAAAA" in snap.read_bytes(), out
    assert _mode(snap) & 0o077 == 0, (
        "the snapshot of a 0600 file is readable beyond its owner: "
        + oct(_mode(snap))
    )
    assert _mode(snap) == 0o600, oct(_mode(snap))


def test_the_snapshot_is_never_wider_than_the_source(
    tmp_path: Path, monkeypatch
) -> None:
    """The general rule, on a mode that is neither 0600 nor the umask default.

    0640 is group-readable and not world-readable, so a snapshot that merely
    clamped everything to 0600 and one that copied the mode are told apart
    here, and so is one that kept writing at 0644.
    """
    store = _cache(tmp_path, monkeypatch)
    target = _secret(tmp_path, 0o640)

    out = _paste(tmp_path, target, "AWS_SECRET=rotated" + NL)

    snap = _one_snapshot(store, out)
    assert _mode(snap) & ~0o640 == 0, oct(_mode(snap))
    assert _mode(snap) == 0o640, oct(_mode(snap))


def test_the_receipt_states_the_mode_it_used(tmp_path: Path, monkeypatch) -> None:
    """#1275 shipped `_created_mode_note` because a widening nobody is told
    about is the defect being removed -- and put it on the CREATE path only,
    not on the path carrying the DISPLACED bytes.

    Asserted with the mode read back off the snapshot rather than a literal, so
    this cannot pass by printing a constant.
    """
    store = _cache(tmp_path, monkeypatch)
    target = _secret(tmp_path, 0o600)

    out = _paste(tmp_path, target, "AWS_SECRET=rotated" + NL)

    snap = _one_snapshot(store, out)
    assert str(snap) in out, out
    assert "{0:04o}".format(_mode(snap)) in out, out


def test_a_symlinked_store_is_declined_rather_than_followed(
    tmp_path: Path, monkeypatch
) -> None:
    """`store.mkdir(exist_ok=True)` succeeds on a symlink to a directory and
    `open(dest, "wb")` follows it, so a `paste-backup` symlink planted once
    under a shared cache root redirects every later snapshot. The `time_ns()`
    leaf is unpredictable; the directory is not.

    Declined, not silently redirected: the write still goes through, and the
    receipt says there is no backup -- three states, not two.
    """
    require_symlink()
    store = _cache(tmp_path, monkeypatch)
    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir()
    store.parent.mkdir(parents=True, exist_ok=True)
    store.symlink_to(elsewhere, target_is_directory=True)
    target = _secret(tmp_path, 0o600)

    out = _paste(tmp_path, target, "AWS_SECRET=rotated" + NL)

    assert list(elsewhere.iterdir()) == [], [str(p) for p in elsewhere.iterdir()]
    assert "no backup" in out.lower(), out
    assert target.read_bytes() == b"AWS_SECRET=rotated" + NL.encode("utf-8")
