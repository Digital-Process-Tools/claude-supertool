"""#2034 -- a contended `.git/index.lock` used to fail instantly and say
nothing about whether the lock is live or stale.

`_git`/`_git_verbatim` are the one chokepoint most of `presets/git` reaches
git through. Neither retried a lock-contention failure and neither said
anything about the lock itself -- the failure surfaced as whatever the op
does with a non-zero git, which for most callers is "report git failed" with
no further detail.

Two things, tested separately because they are separate risks (the issue's
own framing): a bounded retry that must not paper over a genuinely wedged
repository, and a liveness read for the lock itself that must say `cannot
tell` rather than guess when it cannot look.

**Must-fire / must-not-fire, paired in the same fixture**: a shim `git` on
PATH lets each test control exactly how many times contention appears,
without a real git process ever taking a real lock -- except for the two
tests that need a *real* open file descriptor to prove `_lock_fd_holder`
actually distinguishes held from released, which no shim can stand in for.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

PRESET = Path(__file__).parent.parent / "presets" / "git" / "_git_common.py"


def _load():
    spec = importlib.util.spec_from_file_location("git_common_2034", PRESET)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _shim_dir(tmp_path: Path, script: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "git"
    shim.write_text(script, encoding="utf-8")
    shim.chmod(0o755)
    return bindir


LOCK_ERR = "fatal: Unable to create '{lock}': File exists."


# ---------------------------------------------------------------------------
# The retry loop
# ---------------------------------------------------------------------------

def test_retries_and_succeeds_once_the_lock_clears(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST FIRE. Contention that clears inside the budget must not be
    reported as a failure at all."""
    mod = _load()
    lock = tmp_path / "index.lock"
    counter = tmp_path / "calls"
    # Fails with the lock error on the first two calls, succeeds on the third.
    bindir = _shim_dir(tmp_path, (
        "#!/bin/sh\n"
        "n=$(cat '" + str(counter) + "' 2>/dev/null || echo 0)\n"
        "n=$((n + 1))\n"
        "echo $n > '" + str(counter) + "'\n"
        "if [ \"$n\" -lt 3 ]; then\n"
        "  echo \"" + LOCK_ERR.format(lock=lock) + "\" >&2\n"
        "  exit 128\n"
        "fi\n"
        "echo ok\n"
        "exit 0\n"))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("SUPERTOOL_GIT_LOCK_WAIT", "2")

    result = mod._git(["commit", "-m", "x"])

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    assert counter.read_text().strip() == "3", "expected exactly two retries"


def test_a_non_lock_failure_is_never_retried(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST NOT FIRE. An ordinary git failure (wrong args, not a repo, a real
    conflict) must return on the first call -- retrying it would just be a
    slower way to fail, and would mask which git call actually errored."""
    mod = _load()
    counter = tmp_path / "calls"
    bindir = _shim_dir(tmp_path, (
        "#!/bin/sh\n"
        "n=$(cat '" + str(counter) + "' 2>/dev/null || echo 0)\n"
        "n=$((n + 1))\n"
        "echo $n > '" + str(counter) + "'\n"
        "echo 'fatal: not a git repository' >&2\n"
        "exit 128\n"))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("SUPERTOOL_GIT_LOCK_WAIT", "2")

    result = mod._git(["status"])

    assert result.returncode == 128
    assert counter.read_text().strip() == "1", "a non-lock failure was retried"


def test_exhausting_the_budget_appends_a_diagnosis_not_another_retry(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST FIRE. A lock that never clears must stop retrying at the stated
    ceiling and say which of live/stale/cannot-tell it thinks it hit."""
    mod = _load()
    lock = tmp_path / "index.lock"
    lock.write_text("", encoding="utf-8")
    counter = tmp_path / "calls"
    bindir = _shim_dir(tmp_path, (
        "#!/bin/sh\n"
        "n=$(cat '" + str(counter) + "' 2>/dev/null || echo 0)\n"
        "n=$((n + 1))\n"
        "echo $n > '" + str(counter) + "'\n"
        "echo \"" + LOCK_ERR.format(lock=lock) + "\" >&2\n"
        "exit 128\n"))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("SUPERTOOL_GIT_LOCK_WAIT", "0.3")
    # No fd holder either way -- pin the diagnosis so this test does not
    # depend on lsof/proc being installed on the runner.
    monkeypatch.setattr(mod, "_lock_fd_holder", lambda path, **k: False)

    started = time.monotonic()
    result = mod._git(["commit", "-m", "x"])
    elapsed = time.monotonic() - started

    assert result.returncode == 128
    assert "lock-diagnosis: stale" in result.stderr, result.stderr
    assert int(counter.read_text().strip()) >= 2, "never retried at all"
    assert elapsed < 3, f"took {elapsed}s -- the budget is 0.3s"


def test_lock_wait_zero_disables_the_retry_entirely(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST NOT FIRE. Setting the budget to 0 is an opt-out, not a 1-retry
    budget -- the first failure must return immediately, undiagnosed."""
    mod = _load()
    lock = tmp_path / "index.lock"
    counter = tmp_path / "calls"
    bindir = _shim_dir(tmp_path, (
        "#!/bin/sh\n"
        "n=$(cat '" + str(counter) + "' 2>/dev/null || echo 0)\n"
        "n=$((n + 1))\n"
        "echo $n > '" + str(counter) + "'\n"
        "echo \"" + LOCK_ERR.format(lock=lock) + "\" >&2\n"
        "exit 128\n"))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("SUPERTOOL_GIT_LOCK_WAIT", "0")

    result = mod._git(["commit", "-m", "x"])

    assert result.returncode == 128
    assert counter.read_text().strip() == "1"
    assert "lock-diagnosis" not in result.stderr


def test_git_verbatim_gets_the_same_retry(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST FIRE. `_git_verbatim` is the same chokepoint with translation off."""
    mod = _load()
    lock = tmp_path / "index.lock"
    counter = tmp_path / "calls"
    bindir = _shim_dir(tmp_path, (
        "#!/bin/sh\n"
        "n=$(cat '" + str(counter) + "' 2>/dev/null || echo 0)\n"
        "n=$((n + 1))\n"
        "echo $n > '" + str(counter) + "'\n"
        "if [ \"$n\" -lt 2 ]; then\n"
        "  echo \"" + LOCK_ERR.format(lock=lock) + "\" >&2\n"
        "  exit 128\n"
        "fi\n"
        "echo ok\n"))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("SUPERTOOL_GIT_LOCK_WAIT", "2")

    result = mod._git_verbatim(["status"])

    assert result.returncode == 0
    assert counter.read_text().strip() == "2"


# ---------------------------------------------------------------------------
# The liveness read
# ---------------------------------------------------------------------------

def test_diagnose_lock_reports_live_stale_and_cannot_tell(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST FIRE, all three states. `_lock_fd_holder` is monkeypatched so this
    does not depend on lsof/proc being installed on the runner -- the point
    here is that `_diagnose_lock` renders each of the three answers it can be
    handed, not that the underlying scan works (that is the next test)."""
    mod = _load()
    lock = tmp_path / "index.lock"
    lock.write_text("", encoding="utf-8")

    monkeypatch.setattr(mod, "_lock_fd_holder", lambda path, **k: True)
    assert "live" in mod._diagnose_lock(str(lock))

    monkeypatch.setattr(mod, "_lock_fd_holder", lambda path, **k: False)
    assert "stale" in mod._diagnose_lock(str(lock))

    monkeypatch.setattr(mod, "_lock_fd_holder", lambda path, **k: None)
    verdict = mod._diagnose_lock(str(lock))
    assert "cannot tell" in verdict
    assert "live" not in verdict.split("cannot tell")[0]


def test_diagnose_lock_on_an_already_released_lock(tmp_path: Path) -> None:
    """MUST FIRE. The positive control for the case that needs no scan at
    all: if the file is gone by the time this runs, there is nothing to be
    live, and saying so must not require lsof or /proc."""
    mod = _load()
    missing = tmp_path / "index.lock"
    verdict = mod._diagnose_lock(str(missing))
    assert "gone" in verdict or "released" in verdict


@pytest.mark.skipif(
    sys.platform.startswith("win") or not (shutil.which("lsof") or os.path.isdir("/proc")),
    reason="needs a real fd-holder scan: lsof or /proc")
def test_lock_fd_holder_distinguishes_a_real_open_file_from_a_closed_one(
        tmp_path: Path) -> None:
    """MUST FIRE (held) and MUST NOT FIRE (released), against a real fd --
    the one thing no shim can stand in for."""
    mod = _load()
    target = tmp_path / "index.lock"
    target.write_text("", encoding="utf-8")

    handle = open(target, "r", encoding="utf-8")
    try:
        held = mod._lock_fd_holder(str(target))
        assert held is True, "a process holds this file open right now"
    finally:
        handle.close()

    released = mod._lock_fd_holder(str(target))
    assert released is False, "nothing holds this file open any more"
