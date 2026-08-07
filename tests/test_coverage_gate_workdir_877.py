"""The coverage gate's scratch directory must not be world-guessable (#877).

`_work_dir()` was `Path(tempfile.gettempdir()) / "supertool-coverage-gate"` with
`exist_ok=True` and no ownership, symlink or mode check. Three things follow
from that, and only the first is the one that got filed:

1. **The gate executes what it finds there.** `write_config()` writes
   `coverage_gate.ini` into that directory and `run_suite()` runs
   `coverage run --rcfile=<it>` *and* exports `COVERAGE_PROCESS_START=<it>` into
   every spawned child. A coverage rcfile can declare `plugins =`, which
   coverage imports. Control of that file is code execution inside the process
   that decides whether the release's coverage floors pass.

2. **On Linux `gettempdir()` is `/tmp`**, world-writable and shared. Any local
   process can pre-create the fixed name, or land a symlink on it, before the
   gate's `mkdir(exist_ok=True)` — which accepts an existing directory of any
   ownership without a word. `main()` then `unlink()`s everything matching
   `.coverage*` *inside whatever that turned out to be*.

3. **One fixed name is shared by every checkout on the machine.** With five
   worktrees open, two concurrent gate runs interleave their `parallel = true`
   data files under one path, and `--report` reads the mixture. That is a wrong
   *number* out of the release gate, with no error anywhere — which is the same
   defect class as the security one and arrives far more often.

The fix is a repo-local, per-worktree directory created at mode 0700, and a
**refusal** — not a fallback — when it cannot be created or written. A gate that
cannot write its config must never reach `report()`: with no data to combine,
every bucket totals zero statements, and "nothing was measured" printed beside a
floor is one code path away from reading as a pass. Three states, per
`docs/validators.md` §"Declining instead of guessing": pass (0), floor failure
(1), refused (2).

These assertions are about *where the file lands and what mode it has*, never
about which function was called to put it there.
"""
from __future__ import annotations

import importlib.util
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GATE_PATH = REPO / ".github" / "scripts" / "coverage_gate.py"

POSIX_MODES = sys.platform != "win32"


def _load_gate():
    spec = importlib.util.spec_from_file_location("coverage_gate_877", GATE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_work_dir_is_derived_from_the_checkout_not_from_gettempdir(tmp_path) -> None:
    """The scratch path must be a function of the checkout, not of `gettempdir()`.

    Asserted this way rather than as "does not contain /tmp", because the defect
    is guessability by another user, not the particular directory. A uid suffix
    under `/tmp` would still fail this and should: whoever can write to `/tmp`
    knows your uid. Two checkouts must also get two paths — the old fixed name
    was shared by every worktree on the machine, so two concurrent runs combined
    each other's `parallel = true` data files into one reported number.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    gate.REPO = tmp_path / "a"
    first = gate._work_dir().resolve()
    gate.REPO = tmp_path / "b"
    second = gate._work_dir().resolve()

    assert first != second, (
        f"two different checkouts both got {first} — one fixed path shared by "
        f"every worktree is a wrong number out of the release gate, silently")
    assert (tmp_path / "a").resolve() in first.parents, (
        f"work dir {first} is not inside its own checkout {tmp_path / 'a'}")
    assert (tmp_path / "b").resolve() in second.parents, (
        f"work dir {second} is not inside its own checkout {tmp_path / 'b'}")


def test_work_dir_of_the_real_checkout_is_inside_it() -> None:
    """And the same holds unmonkeypatched, for this repository.

    The pair above would pass for any REPO-derived path; this pins that the
    shipped default is the checkout and not a temp-root name, which is the state
    the issue describes. Skipped rather than failed if somebody's clone genuinely
    lives under the system temp root — a real answer, not a quiet pass.
    """
    temp_root = Path(tempfile.gettempdir()).resolve()
    if temp_root in REPO.resolve().parents:
        pytest.skip(f"this checkout is itself under {temp_root}")
    gate.REPO = REPO
    work = gate._work_dir().resolve()
    assert REPO.resolve() in work.parents, (
        f"work dir {work} is outside the checkout {REPO}")
    assert temp_root not in work.parents, (
        f"work dir {work} is under the shared temp root {temp_root} — any local "
        f"process can pre-create or symlink that path, and the gate executes "
        f"the rcfile it finds there")


@pytest.mark.skipif(not POSIX_MODES, reason="POSIX mode bits")
def test_work_dir_is_created_0700(tmp_path) -> None:
    """Not group- or world-readable, and not writable by anyone else.

    Same reason `validators/gitleaks/gitleaks.py` chmods its `mkdtemp` to 0700:
    for as long as it exists this directory decides what a process imports.
    """
    gate.REPO = tmp_path
    work = gate._work_dir()
    assert _mode(work) == 0o700, (
        f"work dir {work} is mode {oct(_mode(work))}, expected 0o700")


@pytest.mark.skipif(not POSIX_MODES, reason="POSIX mode bits")
def test_rcfile_lands_in_the_private_dir_and_is_not_world_writable(tmp_path) -> None:
    """The executed artefact itself, not just its parent."""
    gate.REPO = tmp_path
    work = gate._work_dir()
    config = gate.write_config(work / ".coverage")
    assert config.parent.resolve() == work.resolve(), (
        f"rcfile {config} is not inside the private work dir {work}")
    mode = _mode(config)
    assert not mode & 0o077, (
        f"rcfile {config} is mode {oct(mode)} — group/other have access to a "
        f"file the gate feeds to `coverage run --rcfile=` and exports as "
        f"COVERAGE_PROCESS_START to every child")


def test_the_work_dir_is_gitignored() -> None:
    """A repo-local scratch dir that dirties `git status` gets deleted by hand."""
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    entries = {line.strip().rstrip("/") for line in ignored if line.strip()}
    gate.REPO = REPO
    name = gate._work_dir().name
    assert name in entries, (
        f"{name!r} is not in .gitignore — the gate's own scratch directory "
        f"would show up as an untracked path in every working tree")


def test_refuses_when_the_work_dir_path_is_a_symlink(tmp_path, capsys) -> None:
    """A pre-existing symlink is the attack, and it must stop the run.

    `mkdir(exist_ok=True)` follows a symlink to a directory and returns happily,
    which is precisely how somebody else's rcfile ends up being the one the gate
    executes. Refuse, exit 2, print why — never continue against it.
    """
    if not POSIX_MODES:
        pytest.skip("symlink creation needs privileges on Windows")
    repo = tmp_path / "repo"
    repo.mkdir()
    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir()
    gate.REPO = repo
    name = ""
    try:
        gate.REPO = REPO
        name = gate._work_dir().name
    finally:
        gate.REPO = repo
    (repo / name).symlink_to(elsewhere, target_is_directory=True)

    def _must_not_run(*a, **k):  # pragma: no cover - reached only on regression
        raise AssertionError(
            "measure() was reached after the work dir was rejected — the gate "
            "continued against an attacker-controlled path")

    original = gate.measure
    gate.measure = _must_not_run
    try:
        rc = gate.main(["--report"])
    finally:
        gate.measure = original
    out = capsys.readouterr().out
    assert rc == 2, f"expected refusal exit 2, got {rc}"
    assert "REFUSED" in out, f"refusal was not announced; stdout was:\n{out}"
    assert "coverage gate: pass" not in out


def test_refuses_when_the_work_dir_cannot_be_created(tmp_path, capsys) -> None:
    """Cannot write the config -> refuse. Never fall through to a report.

    With no rcfile there is no data, every bucket totals zero statements, and
    the difference between "measured nothing" and "measured and it was fine" is
    one branch. The gate must not be one branch away from reporting a pass it
    did not earn.
    """
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    gate.REPO = blocker / "repo"

    def _must_not_run(*a, **k):  # pragma: no cover - reached only on regression
        raise AssertionError("measure() was reached with no writable work dir")

    original = gate.measure
    gate.measure = _must_not_run
    try:
        rc = gate.main([])
    finally:
        gate.measure = original
    out = capsys.readouterr().out
    assert rc == 2, f"expected refusal exit 2, got {rc}"
    assert "REFUSED" in out, f"refusal was not announced; stdout was:\n{out}"
    assert "coverage gate: pass" not in out


def test_tests_workflow_declares_a_read_only_token() -> None:
    """`tests.yml` publishes nothing, so it should ask for nothing (#877 part 2).

    Declared rather than inherited: with no block the token gets whatever the
    repository default is, and that default is a settings page nobody re-reads
    when it widens. Same reasoning, same two lines, as `slow-tests.yml`.
    """
    text = (REPO / ".github" / "workflows" / "tests.yml").read_text(
        encoding="utf-8")
    lines = [ln.rstrip() for ln in text.splitlines()]
    assert "permissions:" in lines, (
        "tests.yml declares no top-level `permissions:` block — the workflow "
        "inherits the repository default token scope")
    idx = lines.index("permissions:")
    assert lines[idx + 1].strip() == "contents: read", (
        f"expected `contents: read` under the permissions block, got "
        f"{lines[idx + 1]!r}")


@pytest.fixture(autouse=True)
def _restore_repo():
    original = gate.REPO
    yield
    gate.REPO = original
