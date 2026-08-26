"""Receipt diagnostic for `:r` read failures on op_vim (#1763).

A single-platform CI red (windows-latest, 3.9) showed op_vim's `:r` failing
with ENOENT on a path the test itself had just written and closed three
statements earlier. The failure was never reproduced. Rather than chase the
flake, the receipt on that failure arm now carries enough extra fact --
whether the parent directory exists -- that the *next* occurrence can
settle, from the log alone, whether the file was reaped after we closed
it or the path never resolved at all.

These tests do not reproduce the race. They simulate the two shapes the
receipt must be able to tell apart:

- the path's parent directory does not exist at all (path never resolved)
- the parent directory exists but the file itself does not (already-gone --
  the shape a reap would leave behind)
"""
import os
import tempfile

import supertool as st


def _tmp(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".php", text=True)
    os.write(fd, content.encode())
    os.close(fd)
    return path


def test_r_missing_parent_dir_says_parent_does_not_exist():
    """Parent directory absent: the path never resolved in the first
    place. The receipt must say the parent directory is missing."""
    p = _tmp("a\nb\n")
    missing = os.path.join(tempfile.gettempdir(), "st-1763-no-such-dir", "f.txt")
    try:
        r = st.op_vim(p, f":1r {missing}")
        assert "ERROR" in r
        assert "parent" in r.lower() and "does not exist" in r.lower(), (
            f"expected the missing-parent shape: {r!r}"
        )
    finally:
        os.unlink(p)


def test_r_missing_file_in_existing_parent_says_parent_exists():
    """Parent directory present but the file itself is gone: this is the
    shape a reap-after-close would leave. The receipt must say the parent
    exists (distinguishing it from the never-resolved case above)."""
    p = _tmp("a\nb\n")
    src_fd, src_path = tempfile.mkstemp(suffix=".txt", text=True)
    os.close(src_fd)
    os.unlink(src_path)  # gone, but its parent (the temp dir) certainly exists
    try:
        r = st.op_vim(p, f":1r {src_path}")
        assert "ERROR" in r
        assert "parent" in r.lower() and "exists" in r.lower(), (
            f"expected the parent-exists shape: {r!r}"
        )
        assert "does not exist" not in r.lower(), (
            f"must not claim the parent is missing when it is not: {r!r}"
        )
    finally:
        os.unlink(p)


def test_r_success_receipt_carries_no_diagnostic_noise(tmp_path):
    """The common case -- file present, read succeeds -- must not carry
    any of this extra detail. Most `:r` calls succeed; a successful read
    should look exactly as it did before.

    #1973: the source file used to come from a bare `tempfile.mkstemp()`,
    landing it in the shared platform temp root rather than under pytest's
    own per-test `tmp_path`. That is a mitigation against a race under
    xdist load, not a diagnosis of one -- see the issue for what remains
    unestablished. `os.close()` alone does not delete an `mkstemp()` file,
    so the file was never "closed and therefore gone" the way a
    `NamedTemporaryFile(delete=True)` would be; whatever removed it was
    something else."""
    p = _tmp("a\nb\n")
    src_path = str(tmp_path / "src.txt")
    with open(src_path, "wb") as f:
        f.write(b"HI\n")
    try:
        r = st.op_vim(p, f":1r {src_path}")
        assert "ERROR" not in r
        assert "parent" not in r.lower()
    finally:
        os.unlink(p)


def test_r_directory_failure_keeps_receipt_short(tmp_path):
    """A failure that is not 'the file is not there' (here: the path
    names a directory, not a file) must not grow the missing-file
    diagnostic -- that block answers a question this failure never
    asked. Pairs with the two ENOENT cases above so the silence half is
    not decoration.

    Uses pytest's own `tmp_path` rather than `tempfile.mkdtemp` -- the
    directory is pytest's to reap, not this test's to remove (#1635)."""
    p = _tmp("a\nb\n")
    a_dir = tmp_path / "a-directory"
    a_dir.mkdir()
    try:
        r = st.op_vim(p, f":1r {a_dir}")
        assert "ERROR" in r
        assert "parent directory" not in r.lower(), (
            f"a non-ENOENT failure should not carry the ENOENT diagnostic: {r!r}"
        )
    finally:
        os.unlink(p)
