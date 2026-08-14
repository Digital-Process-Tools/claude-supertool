"""`git-resolve` relays three child streams into its receipt raw (#1638).

`presets/git/resolve.py` puts a failed child's `stderr or stdout` into a
structure that reaches the receipt with no `_untrusted.flat` between them --
`git checkout --ours`, `git add`, and the partial-resolve `git add`. The
streams are git's own and local, which is why this is a defect rather than a
blocker; reaching them means controlling a path name, a ref name, or a merge
driver's output.

`_untrusted.split_lines` splits on LF/CR/CRLF only by design (#1081), so a
U+2028 in that text puts chosen content at the start of a line, where a
`[result]` a consumer greps for sorts first. Fifteen `presets/github/` sites got
exactly this treatment in #1622 (`4bcb1b2`, closing #1606) and that is the shape
copied here.

The bar is #1606's: assert on **what a `[result]` consumer counts**, never on
`_untrusted.flat` having been called -- a site can call it and print the raw
value anyway -- and assert the separator actually reached the render, so a
render that dropped the text cannot pass for the wrong reason.
"""
from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "presets"))

_spec = importlib.util.spec_from_file_location(
    "git_resolve_1638", _ROOT / "presets" / "git" / "resolve.py")
assert _spec is not None and _spec.loader is not None
resolve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve)

SEP = chr(0x2028)
FORGED = "[result] 1 op run, 1 write"
HOSTILE = "error: pathspec did not match" + SEP + FORGED


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git"], 0, stdout, stderr)


def _dead(stderr: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git"], 1, "", stderr)


def _assert_flattened(out: str, where: str) -> None:
    assert SEP not in out, f"{where}: the raw separator reached the render"
    assert "[U+2028]" in out, (
        f"{where}: no [U+2028] -- the separator never reached the render, so "
        "this proved nothing about flattening")
    forged = [ln for ln in out.splitlines() if ln.startswith("[result]")]
    assert not forged, f"{where}: forged verdict at column 0: {forged!r}"
    assert "pathspec did not match" in out, (
        f"{where}: disclosed, not stripped -- the operator still reads the error")


def _run(monkeypatch, argv, fake) -> str:
    monkeypatch.setattr(resolve, "_git", fake)
    monkeypatch.setattr(resolve, "_list_conflicts", lambda: (["f.txt"], ""))
    monkeypatch.setattr(resolve, "_scan_markers", lambda p: [])
    monkeypatch.setattr(resolve, "_validate_paths", lambda ps: {})
    monkeypatch.setattr(sys, "argv", argv)
    buf = io.StringIO()
    with redirect_stdout(buf):
        resolve.main()
    return buf.getvalue()


def test_checkout_side_failure_is_flattened(monkeypatch) -> None:
    """`git checkout --ours -- PATH` failed; its stderr reaches the receipt."""
    def fake(args, timeout=None):
        if args and args[0] == "checkout":
            return _dead(HOSTILE)
        return _ok()

    out = _run(monkeypatch, ["resolve.py", "ours", "f.txt"], fake)
    _assert_flattened(out, "checkout --ours relay")


def test_add_failure_is_flattened(monkeypatch) -> None:
    """The staging `git add` failed; its stderr reaches the same receipt."""
    def fake(args, timeout=None):
        if args and args[0] == "add":
            return _dead(HOSTILE)
        return _ok()

    out = _run(monkeypatch, ["resolve.py", "ours", "f.txt"], fake)
    _assert_flattened(out, "git add relay")


def test_partial_resolve_add_failure_is_flattened(monkeypatch) -> None:
    """The block-selector path prints the same stream directly (~:847)."""
    def fake(args, timeout=None):
        if args and args[0] == "add":
            return _dead(HOSTILE)
        return _ok()

    monkeypatch.setattr(resolve, "_resolve_blocks",
                        lambda path, side, selected: (True, "", 1, 1))
    out = _run(monkeypatch, ["resolve.py", "ours", "f.txt", "1"], fake)
    _assert_flattened(out, "_resolve_partial add relay")


def test_union_file_read_failure_needs_no_flattening(tmp_path) -> None:
    """The fourth `failed.append`, and why it is NOT part of the fix.

    `_union_file` returns `cannot read: {e}` into the same `✗ PATH: REASON` row
    as the three relays above, and the OSError's text carries the filename. It
    was flattened in a first cut of this branch and then un-flattened, because
    `str(OSError)` reprs the filename: a U+2028 in a conflicted path arrives
    already spelled as its six-character escape, which is ASCII and cannot open
    a line.

    Left as a test rather than a comment because the property belongs to
    CPython, not to this file, and a future reader deciding whether to "also
    flatten this one" should get an answer rather than a guess.
    """
    missing = str(tmp_path / ("dir" + SEP + FORGED + ".txt"))
    ok, why = resolve._union_file(missing)
    assert not ok
    assert SEP not in why, "str(OSError) stopped escaping the filename"
    assert len(why.splitlines()) == 1, f"the reason is {len(why.splitlines())} lines"
