"""#1352 - where the elision actually fires, pinned as a boundary.

`_read_elide_session_key()` carries `os.getppid()`. For `supertool` that is
the process that *ran* it, so the key is a property of the invoking shell,
not of the agent session. Claude Code starts a new shell per Bash tool call,
which makes the key change on every turn - every case #1329 was aimed at.

Both arms are measured here rather than asserted from the source, and they
are deliberately paired: the same-parent arm is the positive control, and
without it "content came back" is equally consistent with the feature being
switched off, the cache being unwritable, or the test spawning wrongly.

This test is a boundary marker, not an endorsement. Whoever changes the key
so that an elision survives a new parent process should expect the
cross-parent arm to go red, and should rewrite it rather than route round it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUPERTOOL = REPO / "supertool.py"

MARKER = "===CALL==="

# Runs supertool N times inside ONE process, so every child sees the same
# getppid(). Spawned with sys.executable; a bare "python3" is refused by
# tests/test_no_bare_python3_spawn.py and would be wrong on Windows anyway.
#
# Bytes the whole way down, deliberately. A read's line prefix is U+2192, and
# `text=True` with no encoding decodes with the locale codec -- on a cp1252
# console that is this repo's own mojibake defect (`HEAD after: bc63422
# CHECKMARK-AS-THREE-CHARS`), and re-encoding it to write it on would raise
# outright. The parent decodes once, explicitly, at the outer boundary.
# `bytes([10])` rather than an escape: this string is Python source twice over.
_BOOTSTRAP = """
import subprocess, sys
st, target, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
for _ in range(n):
    r = subprocess.run([sys.executable, st, "read:" + target],
                       capture_output=True)
    sys.stdout.buffer.write(b"===CALL===" + bytes([10]))
    sys.stdout.buffer.write(r.stdout)
    if r.returncode:
        sys.stderr.buffer.write(r.stderr)
sys.stdout.buffer.flush()
"""


def _run(target: Path, cwd: Path, cache: Path, times: int) -> list:
    """One fresh parent process; `times` supertool calls underneath it."""
    env = dict(os.environ)
    env["XDG_CACHE_HOME"] = str(cache)
    env.pop("SUPERTOOL_READ_NO_ELIDE", None)
    r = subprocess.run(
        [sys.executable, "-c", _BOOTSTRAP, str(SUPERTOOL), str(target), str(times)],
        cwd=str(cwd), env=env, capture_output=True, timeout=120,
        encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr
    return [c for c in r.stdout.split(MARKER) if c.strip()]


def test_two_calls_under_one_parent_do_elide(tmp_path: Path) -> None:
    """Positive control: the feature is armed and the cache is writable."""
    target = tmp_path / "same_parent.py"
    target.write_bytes(b"x = 1\n" * 40)
    first, second = _run(target, tmp_path, tmp_path / "cache-a", 2)
    assert "1→x = 1" in first
    assert "elided" in second
    assert "read:" in second and ":full" in second


def test_a_new_parent_process_never_sees_the_previous_read(tmp_path: Path) -> None:
    """The defect in #1352, as behaviour rather than as a source claim.

    Same user, same cwd, same unchanged file, well inside the 15-minute
    window - and the content comes back in full, because the parent process
    changed. That is one Bash tool call to the next.
    """
    target = tmp_path / "new_parent.py"
    target.write_bytes(b"y = 2\n" * 40)
    cache = tmp_path / "cache-b"
    (first,) = _run(target, tmp_path, cache, 1)
    (second,) = _run(target, tmp_path, cache, 1)
    assert "1→y = 2" in first
    assert "1→y = 2" in second, "elision now survives a new parent - see #1352"
    assert "elided" not in second
