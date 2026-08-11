"""#1329 — `read` elides a repeat read of a byte-identical file.

The op cannot know whether the caller still holds the first copy: a re-read
after a context compaction is the normal case, not the edge case. So every
test here is about a *bound* on that ignorance rather than about the saving.
The elision is only ever one round-trip from the content, and the command
that recovers it is printed in the line itself.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest

import supertool


@pytest.fixture()
def elide_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Arm the feature against a throwaway cache and a fixed session key."""
    monkeypatch.delenv("SUPERTOOL_READ_NO_ELIDE", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(supertool, "_read_elide_session_key", lambda: "session-A")
    return tmp_path


def test_second_identical_read_is_elided_and_says_how_to_undo_it(elide_on: Path) -> None:
    f = elide_on / "a.py"
    f.write_bytes(b"x = 1\n" * 40)
    first = supertool.op_read(str(f))
    assert "     1→x = 1" in first

    second = supertool.op_read(str(f))
    assert "     1→x = 1" not in second, "content returned again"
    # The line has to carry everything needed to undo it, in the line.
    assert f"read:{f}:full" in second
    # "on disk", not "withheld": a >20KB file is byte-capped on the way out
    # even under :full, so the file's size is not what the first read handed
    # over. The line must not overstate what it is holding back.
    assert f"{len(f.read_bytes()):,} bytes on disk" in second
    digest = hashlib.sha256(f.read_bytes()).hexdigest()
    assert digest[:12] in second


def test_a_changed_file_is_never_elided(elide_on: Path) -> None:
    f = elide_on / "b.py"
    f.write_bytes(b"before\n")
    supertool.op_read(str(f))
    f.write_bytes(b"after\n")
    out = supertool.op_read(str(f))
    assert "     1→after" in out
    assert "elided" not in out


def test_same_byte_count_different_content_is_never_elided(elide_on: Path) -> None:
    """Keyed on sha256, not on size or mtime."""
    f = elide_on / "c.py"
    f.write_bytes(b"aaaa\n")
    supertool.op_read(str(f))
    f.write_bytes(b"bbbb\n")
    out = supertool.op_read(str(f))
    assert "     1→bbbb" in out


def test_an_unanswerable_cache_returns_the_content(
    elide_on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state file that cannot be read is `skipped`, not silence."""
    f = elide_on / "d.py"
    f.write_bytes(b"z = 9\n")
    supertool.op_read(str(f))

    def boom(*_a, **_k):
        raise OSError("cache unreachable")

    monkeypatch.setattr(supertool, "_read_elide_load", boom)
    out = supertool.op_read(str(f))
    assert "     1→z = 9" in out


def test_an_unwritable_cache_still_returns_content_on_the_repeat(
    elide_on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a, **_k):
        raise OSError("read-only cache")

    monkeypatch.setattr(supertool, "_read_elide_record", boom)
    f = elide_on / "e.py"
    f.write_bytes(b"q = 1\n")
    assert "     1→q = 1" in supertool.op_read(str(f))
    assert "     1→q = 1" in supertool.op_read(str(f))


def test_outside_the_recency_window_the_content_comes_back(
    elide_on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = elide_on / "f.py"
    f.write_bytes(b"w = 2\n")
    supertool.op_read(str(f))
    later = time.time() + supertool._READ_ELIDE_WINDOW_SECONDS + 1
    monkeypatch.setattr(supertool.time, "time", lambda: later)
    out = supertool.op_read(str(f))
    assert "     1→w = 2" in out


def test_the_window_is_measured_from_the_last_content_not_the_last_elision(
    elide_on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file polled every minute must not be elided forever."""
    f = elide_on / "g.py"
    f.write_bytes(b"p = 3\n")
    base = time.time()
    window = supertool._READ_ELIDE_WINDOW_SECONDS
    monkeypatch.setattr(supertool.time, "time", lambda: base)
    supertool.op_read(str(f))
    for step in range(1, 6):
        monkeypatch.setattr(supertool.time, "time",
                            lambda s=step: base + s * (window / 5.0))
        supertool.op_read(str(f))
    monkeypatch.setattr(supertool.time, "time", lambda: base + window + 1)
    assert "     1→p = 3" in supertool.op_read(str(f))


def test_full_never_elides_and_is_named_by_the_elision(elide_on: Path) -> None:
    f = elide_on / "h.py"
    f.write_bytes(b"r = 4\n")
    supertool.op_read(str(f))
    assert "elided" in supertool.op_read(str(f))
    forced = supertool.op_read(str(f), force_full=True)
    assert "     1→r = 4" in forced
    assert "elided" not in forced


def test_full_returns_content_and_rearms_the_window(elide_on: Path) -> None:
    """After a forced read the caller demonstrably has the bytes again."""
    f = elide_on / "h2.py"
    f.write_bytes(b"s = 5\n")
    supertool.op_read(str(f), force_full=True)
    assert "elided" in supertool.op_read(str(f))


def test_a_windowed_or_filtered_read_is_never_elided(elide_on: Path) -> None:
    """A recorded whole-file read says nothing about a slice request."""
    f = elide_on / "i.py"
    f.write_bytes(b"".join(b"line%d\n" % n for n in range(1, 51)))
    supertool.op_read(str(f))
    assert "elided" not in supertool.op_read(str(f), offset=0, limit=5)
    assert "elided" not in supertool.op_read(str(f), grep_filter="line7")


def test_a_slice_read_does_not_arm_an_elision_of_the_whole_file(elide_on: Path) -> None:
    f = elide_on / "j.py"
    f.write_bytes(b"".join(b"line%d\n" % n for n in range(1, 51)))
    supertool.op_read(str(f), offset=0, limit=5)
    out = supertool.op_read(str(f))
    assert "     1→line1" in out
    assert "elided" not in out


def test_another_session_never_sees_this_ones_reads(
    elide_on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nine worktrees were live on 2026-08-11; a shared cache root must not
    let one agent's read suppress another's."""
    f = elide_on / "k.py"
    f.write_bytes(b"t = 6\n")
    supertool.op_read(str(f))
    monkeypatch.setattr(supertool, "_read_elide_session_key", lambda: "session-B")
    out = supertool.op_read(str(f))
    assert "     1→t = 6" in out
    assert "elided" not in out


def test_the_session_key_is_not_a_constant() -> None:
    """Two processes, or two worktrees, must not collide."""
    key = supertool._read_elide_session_key()
    assert str(os.getppid()) in key
    assert os.path.realpath(os.getcwd()) in key


def test_a_missing_file_is_not_recorded_or_elided(elide_on: Path) -> None:
    missing = elide_on / "nope.py"
    assert "ERROR: file not found" in supertool.op_read(str(missing))
    assert "ERROR: file not found" in supertool.op_read(str(missing))


def test_the_environment_switch_turns_it_off(
    elide_on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUPERTOOL_READ_NO_ELIDE", "1")
    f = elide_on / "l.py"
    f.write_bytes(b"u = 7\n")
    supertool.op_read(str(f))
    assert "     1→u = 7" in supertool.op_read(str(f))


def test_read_elide_is_a_reaped_cache_kind() -> None:
    """Nine worktrees x every file read leaves entries; gc must know the kind."""
    assert "read-elide" in supertool._GC_DEFAULT_RETENTION_DAYS
