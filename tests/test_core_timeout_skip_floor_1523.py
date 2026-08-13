"""A run in which every gated call declined must not read as a pass (#1523).

#1501 gave 42 unit-test call sites the right to decline when the core spawn wall
fires. Nothing put a floor under that right: on a uniformly loaded runner all 42
decline, pytest prints `0 failed`, and the run has asserted nothing whatsoever
about the validator core while exiting 0. That is this repository own defect --
an absence produced by the tooling, read as an absence in the world -- sitting
inside the harness built to detect it.

The pin is a whole pytest session, run as a child process, because the claim is
about a session exit status and nothing smaller can carry it. Three sessions are
run: one where every gated call declines (must be red), one where a single call
survives (must be green, and must say so with its denominator), and one where no
gated call ran at all (must decline, not pass).

The second instance in #1523 -- `SUPERTOOL_WATCH_NAME` in a maintainer shell
reddening five tests that say nothing about their diff -- is pinned at the
bottom. `tests/conftest.py:pytest_configure` has cleared the three watch
variables since 20d46a4, and nothing asserted that it does. CI exports none of
them, so deleting those three lines is invisible to every leg: the guard existed
and was itself unguarded.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import _core_timeout_census as census  # noqa: E402
from _adapter_verdict import core_timed_out, skip_if_core_timed_out  # noqa: E402

REPO = Path(__file__).parent.parent
TESTS = Path(__file__).parent


# ---------------------------------------------------------------------------
# the verdict itself -- three states, not two
# ---------------------------------------------------------------------------

def test_no_gated_call_declines_rather_than_passing() -> None:
    """`-k` selected none of the 42. That is not evidence of anything."""
    line, finding = census.verdict(0, 0)
    assert finding is False
    assert census.NOT_CHECKED in line
    assert "This is not a pass" in line


def test_every_gated_call_declining_is_a_finding() -> None:
    line, finding = census.verdict(42, 42)
    assert finding is True
    assert census.FINDING in line
    assert "42 of 42" in line
    assert "ZERO adapter verdicts" in line


def test_a_partial_mute_is_reported_with_its_denominator_and_is_not_a_finding() -> None:
    """41 of 42 is loud in the log and still green -- see the module docstring
    for why the bar is categorical rather than a percentage."""
    line, finding = census.verdict(42, 41)
    assert finding is False
    assert "41 of 42" in line
    assert "1 adapter verdicts were actually asserted on" in line


def test_a_clean_run_still_prints_its_line() -> None:
    """Silence would be indistinguishable from never having looked."""
    line, finding = census.verdict(42, 0)
    assert finding is False
    assert "0 of 42" in line
    assert census.FINDING not in line
    assert census.NOT_CHECKED not in line


def test_more_declines_than_calls_is_still_a_finding() -> None:
    """A bookkeeping bug must not fall through into the reassuring branch."""
    _line, finding = census.verdict(1, 5)
    assert finding is True


# ---------------------------------------------------------------------------
# the skip carries the grep handle
# ---------------------------------------------------------------------------

def test_the_skip_reason_carries_the_token() -> None:
    """`N skipped` in a `--tb=no` Windows leg has to be resolvable to a reason.

    Asserted on `core_timed_out`, not by driving a faked core through
    `run_one_or_skip` -- measured while writing this file: that route counts as
    a genuine decline, and a targeted run of this file alone then read
    `11 passed` and exited 1 off one fabricated payload. The census counts real
    spawns; a test asserting on the arm must not enter its denominator.
    """
    reason = core_timed_out(
        {"ok": False, "count": 1, "timeout": True,
         "errors": [{"code": "orchestrator", "msg": "timeout after 10s"}]})
    assert reason is not None
    assert census.TOKEN in reason
    with pytest.raises(pytest.skip.Exception) as excinfo:
        skip_if_core_timed_out(
            {"ok": False, "count": 1, "timeout": True, "errors": []})
    assert census.TOKEN in str(excinfo.value)


# ---------------------------------------------------------------------------
# a whole session, which is the only thing that can carry the claim
# ---------------------------------------------------------------------------

_CHILD = """
import supertool
from _adapter_verdict import run_one_or_skip

WALL = {{"ok": False, "count": 1, "timeout": True,
        "errors": [{{"code": "orchestrator", "msg": "timeout after 10s"}}]}}
REAL = {{"tool": "t", "file": "x", "ok": True, "count": 0, "errors": [],
        "duration_ms": 3}}

SPEC = {{"builtin": "python", "cmd": "x"}}


def _wall(monkeypatch):
    monkeypatch.setattr(supertool, "_validator_run_one", lambda *a, **k: WALL)


def _real(monkeypatch):
    monkeypatch.setattr(supertool, "_validator_run_one", lambda *a, **k: REAL)


def test_one(monkeypatch):
    _wall(monkeypatch)
    assert run_one_or_skip("t", SPEC, "f") is not None


def test_two(monkeypatch):
    _wall(monkeypatch)
    assert run_one_or_skip("t", SPEC, "f") is not None


def test_three(monkeypatch):
    {third}(monkeypatch)
    assert run_one_or_skip("t", SPEC, "f") is not None


def test_ungated():
    assert True
"""


def _child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO), str(TESTS), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    return env


def _session(tmp_path: Path, third: str, extra: list | None = None):
    """Run a child pytest session under the census plugin, nothing else."""
    target = tmp_path / "test_child_1523.py"
    target.write_text(_CHILD.format(third=third), encoding="utf-8")
    argv = [sys.executable, "-m", "pytest", str(target),
            "-p", "no:cacheprovider", "-p", "_core_timeout_census",
            "-q", "-n0", "--no-cov", "--rootdir", str(tmp_path)]
    argv += extra or []
    return subprocess.run(argv, cwd=str(tmp_path), env=_child_env(),
                          capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300)


def test_a_session_where_every_gated_call_declined_exits_non_zero(
        tmp_path: Path) -> None:
    """The issue, verbatim: a whole population muted, reported as a pass."""
    res = _session(tmp_path, "_wall")
    out = res.stdout + res.stderr

    assert "3 skipped" in out, out
    assert census.FINDING in out, out
    assert "3 of 3 gated calls declined" in out, out
    assert res.returncode != 0, (
        "a session that asserted zero adapter verdicts exited 0: " + out)


def test_a_single_surviving_verdict_keeps_the_session_green(
        tmp_path: Path) -> None:
    """The floor is `not all of them`, so one real verdict clears it -- and the
    ratio is still printed, so a 2-of-3 mute is visible in the log."""
    res = _session(tmp_path, "_real")
    out = res.stdout + res.stderr

    assert "2 of 3 gated calls declined" in out, out
    assert census.FINDING not in out, out
    assert res.returncode == 0, out


def test_a_session_with_no_gated_call_declines_rather_than_passing(
        tmp_path: Path) -> None:
    """`-k ungated` selects none of the population. The line must say so rather
    than printing `0 of 0` in the shape of a clean result."""
    res = _session(tmp_path, "_wall", ["-k", "ungated"])
    out = res.stdout + res.stderr

    assert census.NOT_CHECKED in out, out
    assert res.returncode == 0, out


@pytest.mark.parametrize("workers", ["-n0", "-n2"])
def test_the_repo_own_suite_is_wired_to_the_census(workers: str) -> None:
    """The plugin existing is not the plugin running. This asserts the census
    line appears in a session driven by `tests/conftest.py`, with a real
    denominator -- a census wired nowhere prints nothing, and reads identically
    to a census that found nothing.

    **Both worker counts, and `-n2` is the one that matters.** The first wiring
    registered the plugin on the controller only. Serially it counted five gated
    calls and looked right; under `-n auto` -- which is this repo's `addopts`,
    so it is what CI runs -- the counting hook was absent from every process
    where a test actually ran, and the summary read `NOT CHECKED` while 42 gated
    calls went through. A census that reports an absence it produced itself is
    the defect it was built to detect, one layer up.
    """
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_env_field.py",
         "-q", workers, "--no-cov", "-p", "no:cacheprovider"],
        cwd=str(REPO), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300)
    out = res.stdout + res.stderr

    assert census.TOKEN in out, out
    assert census.NOT_CHECKED not in out, (
        "the repo suite ran gated calls and the census counted none of them: "
        + out)
    assert "0 of 5 gated calls declined" in out, out
    assert res.returncode == 0, out


# ---------------------------------------------------------------------------
# instance 2 -- a name in the maintainer environment is not a verdict
# ---------------------------------------------------------------------------

WATCH_VARS = ("SUPERTOOL_WATCH_NAME", "SUPERTOOL_WATCH_SOCK",
              "SUPERTOOL_WATCH_STATE_DIR")

_PROBE = """
import os, sys
sys.path.insert(0, {tests!r})
import conftest


class _PluginManager:
    def register(self, *a, **k):
        pass


class _Config:
    pluginmanager = _PluginManager()


conftest.pytest_configure(_Config())
left = [v for v in {vars!r} if v in os.environ]
sys.path.insert(0, {watch!r})
import naming
import transport
print("LEFT:" + ",".join(left))
print("SOCK:" + transport.SOCK_PATH)
print("WANT:" + naming.resolve({{}}).sock)
"""


def test_an_exported_watch_name_does_not_reach_the_suite() -> None:
    """The five reds of #1523 instance 2, at their root.

    presets/watch/transport, channel and radar resolve their module-level
    constants at *import*, and this repo own `.supertool.json` exports
    `SUPERTOOL_WATCH_NAME=oss-supertool` into every maintainer shell (#1477).
    So the variable decides `SOCK_PATH` before any fixture can intervene, and
    five tests assert on the *default* socket. CI exports none of the three, so
    the whole failure is invisible from the only place that is authoritative --
    which is exactly why the clearing needs a test rather than a comment.
    """
    env = dict(os.environ)
    for var in WATCH_VARS:
        env[var] = "oss-supertool"
    res = subprocess.run(
        [sys.executable, "-c", _PROBE.format(
            tests=str(TESTS), vars=list(WATCH_VARS),
            watch=str(REPO / "presets" / "watch"))],
        cwd=str(REPO), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120)
    out = res.stdout + res.stderr
    lines = out.splitlines()

    assert res.returncode == 0, out
    assert "LEFT:" in lines, (
        "pytest_configure left a watch variable in the environment, so a "
        "maintainer shell decides what the suite measures: " + out)

    # Compared against `naming.resolve({})`, not against a `/tmp/...` literal:
    # the expected default is whatever the platform computes for an empty
    # environment, and a hardcoded POSIX path would assert the wrong thing on
    # Windows. Not vacuous -- with the name still exported, SOCK carries it and
    # the two differ.
    got = [line for line in lines if line.startswith("SOCK:")]
    want = [line for line in lines if line.startswith("WANT:")]
    assert got and want, out
    assert got[0][len("SOCK:"):] == want[0][len("WANT:"):], (
        "the socket path was still derived from the exported name: " + out)
