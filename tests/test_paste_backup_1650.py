"""#1650 - a paste over an existing file destroyed bytes nobody could get back.

The #1642 agent pasted a 1990-byte working note over a path it believed did not
exist. It did: an earlier agent had left 8922 bytes there. The receipt was
honest and complete -- `rewrote ... 8922 -> 1990 bytes` -- and it printed one
step after the only moment it could have helped. The path was gitignored, so
there was no git copy, and the disposable clone used for the suite did not carry
it. Unrecoverable.

What is asserted here is deliberately NOT a refusal. A guard that refuses a
legitimate overwrite is trained away by the `force` token it has to offer, and
`paste` over an existing file is a documented, ordinary thing to do. What these
tests pin instead is that the outgoing bytes survive the write, that the receipt
says where they went, and that the store is reaped -- so the fix cannot recreate
the 1.0 GB cache the reaper in #474 was written for.

`paste` is the only op singled out, and the asymmetry is the argument: `edit`,
`replace` and `vim` match or read the existing bytes before touching them, and
`replace_lines` refuses a range past the file length. `paste` is the one op that
writes a whole file having never looked at what is there.
"""
from pathlib import Path

import _supertool
import supertool

NL = chr(10)
Q3 = chr(39) * 3

# The measured shapes from the incident, so a reader can match them to the
# receipt quoted in #1650.
OLD_BYTES = 8922
NEW_BYTES = 1990


def _toml_path(target: Path) -> str:
    return chr(34) + str(target).replace(chr(92), chr(92) * 2) + chr(34)


def _paste(tmp_path: Path, target: Path, content: str) -> str:
    body = (
        "path = " + _toml_path(target) + NL
        + "content = " + Q3 + content + Q3 + NL
    )
    p = tmp_path / "payload.toml"
    # write_bytes, not write_text, throughout this file: on Windows text mode
    # rewrites every NL to CRLF, which would put the payload's CONTENT one byte
    # per line away from what the assertions count and turn the
    # identical-rewrite case into a differing one. The bug class of #1004.
    p.write_bytes(body.encode("utf-8"))
    return supertool.dispatch("paste:@" + str(p))


def _cache(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(root))
    return root / "supertool" / "paste-backup"


def _note(n: int) -> str:
    """A body of exactly `n` bytes, recognisable in a snapshot."""
    stem = "prior agent note - do not lose this. "
    return (stem * (n // len(stem) + 1))[: n - 1] + NL


def test_the_outgoing_bytes_survive_an_overwrite(tmp_path: Path, monkeypatch) -> None:
    """The issue, asserted against the filesystem rather than the receipt.

    A guard that did nothing leaves this directory empty, which is the whole
    point of reading the snapshot back rather than reading the receipt.
    """
    store = _cache(tmp_path, monkeypatch)
    target = tmp_path / "notes" / "fix-1598-1584.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    old = _note(OLD_BYTES)
    target.write_bytes(old.encode("utf-8"))

    out = _paste(tmp_path, target, _note(NEW_BYTES))

    assert target.read_bytes().startswith(b"prior agent note")
    snaps = sorted(store.glob("*")) if store.is_dir() else []
    assert snaps, "the overwrite took no snapshot:" + NL + out
    assert len(snaps) == 1, [str(s) for s in snaps]
    assert snaps[0].read_bytes() == old.encode("utf-8")


def test_the_receipt_names_the_snapshot(tmp_path: Path, monkeypatch) -> None:
    """A snapshot nobody can find is the unrecoverable case with extra steps."""
    store = _cache(tmp_path, monkeypatch)
    target = tmp_path / "note.md"
    target.write_bytes(_note(OLD_BYTES).encode("utf-8"))

    out = _paste(tmp_path, target, _note(NEW_BYTES))

    snaps = sorted(store.glob("*")) if store.is_dir() else []
    assert snaps, out
    assert str(snaps[0]) in out, out
    assert "rewrote" in out, out


def test_creating_a_file_takes_no_snapshot_and_adds_no_line(
    tmp_path: Path, monkeypatch
) -> None:
    """The case the guard was NOT written for, and the common one.

    Most pastes create. If this path grew a snapshot or a receipt line, the
    guard would be paying a cost on every write to buy nothing -- there are no
    prior bytes to lose.
    """
    store = _cache(tmp_path, monkeypatch)
    target = tmp_path / "brand-new.md"

    out = _paste(tmp_path, target, _note(NEW_BYTES))

    assert "created" in out, out
    assert "backup" not in out.lower(), out
    assert not store.exists() or not list(store.glob("*"))


def test_an_identical_rewrite_takes_no_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    """Nothing is lost, so nothing is stored. The boundary on the cheap side."""
    store = _cache(tmp_path, monkeypatch)
    target = tmp_path / "same.md"
    same = _note(OLD_BYTES)
    target.write_bytes(same.encode("utf-8"))

    out = _paste(tmp_path, target, same)

    assert "backup" not in out.lower(), out
    assert not store.exists() or not list(store.glob("*"))


def test_a_growing_rewrite_is_still_snapshotted(
    tmp_path: Path, monkeypatch
) -> None:
    """The hole a shrink-ratio trigger would leave.

    #1650 proposes a shrink ratio or a byte-loss floor. Both miss the case
    where 8922 bytes are replaced by 9000 DIFFERENT bytes, which loses exactly
    as much and is exactly as unrecoverable. `paste` replaces the whole file by
    definition, so the trigger is the op semantics, not a threshold.
    """
    store = _cache(tmp_path, monkeypatch)
    target = tmp_path / "grown.md"
    old = _note(OLD_BYTES)
    target.write_bytes(old.encode("utf-8"))

    out = _paste(tmp_path, target, "totally different" + NL + _note(OLD_BYTES + 78))

    snaps = sorted(store.glob("*")) if store.is_dir() else []
    assert snaps, "a same-size-or-larger overwrite lost the old bytes:" + NL + out
    assert snaps[0].read_bytes() == old.encode("utf-8")


def test_a_snapshot_that_cannot_be_taken_is_declared(
    tmp_path: Path, monkeypatch
) -> None:
    """Three states, not two: taken, not needed, and could not.

    A store that silently fails is the defect this repo keeps having -- an
    absence produced by the tool, read as an absence in the world. The write
    still goes through: refusing here would make an unwritable cache directory
    into a broken `paste`.
    """
    blocker = tmp_path / "cache"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocker))
    target = tmp_path / "note.md"
    target.write_bytes(_note(OLD_BYTES).encode("utf-8"))

    out = _paste(tmp_path, target, _note(NEW_BYTES))

    assert "no backup" in out.lower(), out
    assert target.read_bytes().startswith(b"prior agent note")


def test_the_store_is_reaped(tmp_path: Path) -> None:
    """#474: ~/.cache/supertool reached 1.0 GB because a writer shipped without
    a retention entry. A new writer that is not in the table is that bug again.
    """
    assert "paste-backup" in _supertool._GC_DEFAULT_RETENTION_DAYS
    out = supertool.dispatch("gc:dry")
    assert "paste-backup" in out, out


def test_the_snapshot_store_is_flat(tmp_path: Path, monkeypatch) -> None:
    """`_gc_sweep_kind` is non-recursive and unlinks regular files only, so a
    snapshot written into a subdirectory is one the reaper counts as `skipped`
    and never removes -- a reaped-looking store that grows forever.
    """
    store = _cache(tmp_path, monkeypatch)
    target = tmp_path / "deep" / "a" / "b" / "note.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_note(OLD_BYTES).encode("utf-8"))

    _paste(tmp_path, target, _note(NEW_BYTES))

    entries = list(store.iterdir())
    assert entries
    assert all(e.is_file() for e in entries), [str(e) for e in entries]


def test_an_oversized_file_is_declined_out_loud(tmp_path: Path, monkeypatch) -> None:
    """A copy on every overwrite is a resource claim, so it is bounded.

    The bound is NOT the trigger #1650 argued about -- it does not decide
    whether the bytes are worth protecting, it decides whether supertool is
    willing to spend the disk. That distinction is why it has to be said out
    loud: an unbacked overwrite that reads exactly like a backed one is the
    absence-read-as-presence defect, and it would land on the largest files,
    where the loss is worst.
    """
    store = _cache(tmp_path, monkeypatch)
    monkeypatch.setattr(_supertool, "_PASTE_BACKUP_MAX_BYTES", 128)
    target = tmp_path / "big.md"
    target.write_bytes(_note(OLD_BYTES).encode("utf-8"))

    out = _paste(tmp_path, target, _note(NEW_BYTES))

    assert "no backup" in out.lower(), out
    assert str(OLD_BYTES) in out, out
    assert not store.exists() or not list(store.glob("*"))
    assert target.read_bytes().startswith(b"prior agent note")


def test_vim_still_refuses_a_file_that_does_not_exist(tmp_path: Path) -> None:
    """The asymmetry this fix rests on, pinned rather than assumed.

    `paste` is guarded alone because it is the only op that writes a whole file
    without first establishing what is there. `vim` can empty a file just as
    completely (`ggdG`), but it errors on a path that does not exist, so the
    #1642 mechanism -- writing to a path you believe is empty -- cannot happen
    through it. If `vim` ever grows a create-if-missing arm, that hole opens
    silently and nothing else in the suite would notice.
    """
    target = tmp_path / "absent.md"
    out = supertool.dispatch("vim:::" + str(target) + ":::ggdGiX" + chr(27))
    assert "ERROR" in out, out
    assert not target.exists()
