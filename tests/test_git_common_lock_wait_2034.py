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

#: `_shim_dir` below writes a `#!/bin/sh` script to a file named `git` with no
#: extension. `_git`/`_git_verbatim` invoke it via `subprocess.Popen(["git",
#: ...], shell=False)` -- no `executable=`, so on Windows the child is found
#: (or not) by `CreateProcess` with `lpApplicationName=NULL`, which appends
#: only `.exe` to an extension-less name and never consults `PATHEXT`. That is
#: the same fact already carried by `tests/test_adapter_tool_vs_file_753.py`,
#: `tests/test_phplint_tool_vs_file_745.py`, `tests/test_tsc_check.py` and
#: `tests/test_validators_eslint_667.py` for their own tool shims, and by
#: `tests/_gitshim.py`'s four consumers (`test_git_timeout_disclosure_650.py`,
#: `test_status_swallowed_705.py`, `test_git_repo_probe_timeout_1858.py`,
#: `test_unanswerable_checks_693.py`, `test_git_shim_subcommand_1206.py`) for
#: this exact shim shape -- all five already skip on `os.name == "nt"` rather
#: than shipping a `git.bat` that `CreateProcess` would silently never reach.
#: A `.bat`/`.cmd` shim only intercepts a bare `git` invocation through
#: `cmd.exe`'s own PATHEXT-aware resolution, which requires `shell=True`;
#: `_git`/`_git_verbatim` never pass it, so writing one here would be a shim
#: that never fires while still reading as coverage -- the untested-looking
#: gap this repo's CLAUDE.md asks to prefer over a green leg that tests
#: nothing. So: same convention, same wording, on the five tests that build
#: this shim. The five tests that do not touch PATH (`_lock_wait_budget`,
#: `_diagnose_lock`, `_lock_fd_holder`) are unaffected and keep running on
#: Windows. REASONED, not observed -- there is no Windows machine in this
#: environment to run it on; the CreateProcess/PATHEXT claim matches the
#: identical, already-merged, already-green-on-Windows-CI precedent above.
posix_shim = pytest.mark.skipif(os.name == "nt", reason="POSIX /bin/sh shim")


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

@posix_shim
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
    assert counter.read_text(encoding="utf-8").strip() == "3", "expected exactly two retries"


@posix_shim
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
    assert counter.read_text(encoding="utf-8").strip() == "1", "a non-lock failure was retried"


@posix_shim
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
    assert int(counter.read_text(encoding="utf-8").strip()) >= 2, "never retried at all"
    assert elapsed < 3, f"took {elapsed}s -- the budget is 0.3s"


@posix_shim
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
    assert counter.read_text(encoding="utf-8").strip() == "1"
    assert "lock-diagnosis" not in result.stderr


@posix_shim
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
    assert counter.read_text(encoding="utf-8").strip() == "2"


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


@pytest.mark.skipif(not shutil.which("lsof"), reason="needs a real lsof binary")
def test_lsof_exit_1_with_stderr_is_cannot_tell_not_stale(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST FIRE (cannot tell) / MUST NOT FIRE (stale). lsof's own exit
    convention gives the SAME code (1) to "ran fine, matched nothing" and to
    "an internal lsof error happened" -- verified against real lsof 4.91: a
    target that vanishes mid-scan exits 1 with an error on stderr, not 0 and
    not some other code. Reading exit 1 alone as `False` would misreport an
    lsof failure as a confident negative -- found in review."""
    mod = _load()
    target = tmp_path / "index.lock"
    target.write_text("", encoding="utf-8")

    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[0] == shutil.which("lsof"):
            return subprocess.CompletedProcess(
                cmd, returncode=1, stdout="", stderr="lsof: status error: boom")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod._lock_fd_holder(str(target)) is None, (
        "exit 1 with stderr must be cannot-tell, not a confident 'not held'")

    def fake_run_clean(cmd, **kwargs):
        if cmd[0] == shutil.which("lsof"):
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", fake_run_clean)
    assert mod._lock_fd_holder(str(target)) is False, (
        "exit 1 with EMPTY stderr is the genuine 'ran fine, matched nothing' case"
    )


def test_lock_wait_budget_ignores_infinity(monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST FIRE. `env_float`'s `minimum=` is a floor, not a ceiling --
    `float('inf')` clears any floor, so `SUPERTOOL_GIT_LOCK_WAIT=inf` would
    make the retry loop's deadline infinite and defeat the one property this
    feature is meant to have (bounded). Found in review."""
    mod = _load()
    monkeypatch.setenv("SUPERTOOL_GIT_LOCK_WAIT", "inf")
    budget = mod._lock_wait_budget()
    assert budget < float("inf"), f"budget was allowed to be infinite: {budget}"
    assert budget == mod.LOCK_WAIT_DEFAULT


def test_lock_wait_budget_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST FIRE. A very large but finite value is capped, not honoured
    verbatim -- an unbounded-looking knob is still a knob somebody can set
    to something this feature was never meant to wait out."""
    mod = _load()
    monkeypatch.setenv("SUPERTOOL_GIT_LOCK_WAIT", "999999")
    assert mod._lock_wait_budget() == mod._LOCK_WAIT_CEILING


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
