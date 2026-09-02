"""#1889: no supported way to get a stack out of a stuck pytest run.

`faulthandler_timeout` in pyproject.toml turns on pytest's built-in
faulthandler plugin repo-wide -- stdlib, no new dependency. This proves the
mechanism actually fires rather than merely being declared: a real hung test,
run in a throwaway child pytest session with a short `faulthandler_timeout`,
must produce a thread dump naming the hung frame on stderr.

The same shape as `tests/test_core_timeout_skip_floor_1523.py`'s child
sessions: a hang test cannot run inside *this* process without hanging this
suite, so it is confined to a spawned subprocess with a hard outer timeout,
which is killed regardless of what faulthandler does -- the outer timeout is
a safety net, not the thing under test.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_HUNG_TEST = """
import time

def test_this_one_never_returns():
    time.sleep(6)
"""


def _run_child(tmp_path: Path, faulthandler_timeout: str | None) -> subprocess.CompletedProcess:
    target = tmp_path / "test_hung_child_1889.py"
    target.write_text(_HUNG_TEST, encoding="utf-8")
    ini = tmp_path / "pytest.ini"
    body = "[pytest]\n"
    if faulthandler_timeout is not None:
        body += f"faulthandler_timeout = {faulthandler_timeout}\n"
    ini.write_text(body, encoding="utf-8")
    argv = [
        sys.executable, "-m", "pytest", str(target),
        "-p", "no:cacheprovider", "--no-cov", "-n0", "-q",
        "--rootdir", str(tmp_path), "-c", str(ini),
    ]
    return subprocess.run(
        argv, cwd=str(tmp_path), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=20,
    )


def test_a_hung_test_dumps_its_stack_when_faulthandler_timeout_is_set(tmp_path: Path) -> None:
    res = _run_child(tmp_path, "2")
    out = res.stdout + res.stderr
    assert "Timeout" in out and "test_this_one_never_returns" in out, (
        "faulthandler_timeout=2 did not surface a thread dump naming the "
        f"hung test within the outer 20s guard: {out}"
    )


def test_without_faulthandler_timeout_no_dump_appears(tmp_path: Path) -> None:
    """The positive control's negative twin (#1889's own instruction): a
    config with no `faulthandler_timeout` must NOT produce the dump, or the
    assertion above is not testing what it claims to."""
    res = _run_child(tmp_path, None)
    out = res.stdout + res.stderr
    assert "test_this_one_never_returns" not in out, (
        "a dump appeared with no faulthandler_timeout configured -- the "
        f"positive control above proves nothing: {out}"
    )
