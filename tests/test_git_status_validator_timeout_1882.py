"""A git call that did not answer is not a clean working tree (#1882).

`validators/git-status/git-status.py` wrapped each of its four git calls in a
hard-coded `subprocess.run(..., timeout=5)` and, on `TimeoutExpired`, returned
`""`. Downstream, `_parse_state("")` is `"clean"` and `_parse_numstat("")` is
`(0, 0)`, so a validator whose git never answered emitted:

    {"ok": true, "count": 0, "errors": [],
     "metrics": {"lines_added": 0, ..., "state": "clean"}}

— a positive measurement of a file nothing successfully looked at. That is the
exact shape #1202 removed from this adapter's *absent-git* arm via
`refusal.absent()`; the timeout arm was left behind, and it is louder than the
absent one because git being present is the normal case.

`validators/SCHEMA.md` §"`adapter`: the reserved code" names a timeout as one of
the four things `code: "adapter"` is reserved for, so the third state here
arrives through the error channel rather than through `skipped` — see
`test_the_core_reads_a_stall_as_no_verdict` below for the three core predicates
that makes true.

The other half of the issue is the budget itself. 5s is tight for a large, cold
or network-mounted repository (the reporter measured >15s), it was a Python
literal no project could raise, and it was *per call* over four sequential
calls while `.supertool.json` gave the whole adapter 5s — so on any real stall
the core's own budget fired first and SIGKILLed the adapter mid-git. The budget
is now one deadline for the whole adapter, honours `$SUPERTOOL_GIT_TIMEOUT` like
every other git call in this repo, and defaults above 5.

Every "must not fire" case here is paired with a "must fire" case driven through
the same harness: a stall that reports `clean` and a harness that reports
nothing at all are indistinguishable otherwise.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import supertool
from _adapter_budget import adapter_budget

REPO = Path(__file__).resolve().parent.parent
ADAPTER = REPO / "validators" / "git-status" / "git-status.py"

#: Outer wall for every spawn of the adapter below. Derived from the adapter's
#: own `GIT_TIMEOUT_DEFAULT` rather than written here, so raising that constant
#: raises these too and a blown budget means the adapter failed to honour its
#: own — see `tests/_adapter_budget.py` for why three literals became this.
BUDGET = adapter_budget(ADAPTER)

#: The knob the decline message must name. Spelled out here rather than read
#: off the adapter ON PURPOSE: the contract is with the person who exports this
#: variable, so reading the adapter's own constant would assert only that the
#: message agrees with itself and would stay green through a rename that
#: silently broke every project already setting it.
TIMEOUT_ENV = "SUPERTOOL_GIT_TIMEOUT"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _load() -> object:
    spec = importlib.util.spec_from_file_location("git_status_under_test", ADAPTER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _StalledProcess:
    """A child that never answers, and records how it was asked to stop.

    Stands in for git on a repository whose `status` outlives the budget. It
    raises `TimeoutExpired` from `communicate` however long it is given, so an
    adapter that retries or that hands over a longer second budget still stalls
    rather than accidentally passing.
    """

    def __init__(self) -> None:
        self.args = ["git"]
        self.returncode = None
        self.signals: list = []

    def communicate(self, timeout=None):  # noqa: ANN001
        if "kill" in self.signals:
            self.returncode = -9
            return "", ""
        raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout or 0)

    def terminate(self) -> None:
        self.signals.append("terminate")

    def kill(self) -> None:
        self.signals.append("kill")

    def wait(self, timeout=None):  # noqa: ANN001
        self.returncode = -9
        return -9

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def _drive_stalled(monkeypatch: pytest.MonkeyPatch, target: Path,
                   env: "dict[str, str] | None" = None) -> "tuple[dict, list]":
    """Run the real adapter with every git spawn stalling. Returns (payload, children).

    Both spawn routes are patched, so this asserts the adapter's *behaviour*
    rather than which of `subprocess.run` / `subprocess.Popen` it happens to
    use today.
    """
    mod = _load()
    children: list = []

    def _popen(*_args, **_kwargs):
        proc = _StalledProcess()
        children.append(proc)
        return proc

    def _run(*_args, **kwargs):
        proc = _StalledProcess()
        children.append(proc)
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=kwargs.get("timeout", 0))

    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(mod.subprocess, "Popen", _popen)
    monkeypatch.setattr(mod.subprocess, "run", _run)
    monkeypatch.setattr(mod.shutil, "which", lambda _n: "/usr/bin/git")
    monkeypatch.setattr(mod.sys, "argv", [str(ADAPTER), str(target)])

    emitted: list = []
    monkeypatch.setattr("builtins.print",
                        lambda *a, **k: emitted.append(" ".join(map(str, a))))
    mod.main()
    payloads = [json.loads(line) for line in emitted if line.strip().startswith("{")]
    assert payloads, "the adapter emitted no JSON when its git stalled"
    return payloads[-1], children


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.com"},
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t.com")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "base.txt").write_text("line1\nline2\n", encoding="utf-8")
    _git(tmp_path, "add", "base.txt")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


@pytest.fixture()
def stalled(repo: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    payload, _children = _drive_stalled(monkeypatch, repo / "base.txt")
    return payload


# ---------------------------------------------------------------------------
# The positive control. Without this, every assertion below passes on a harness
# that produced nothing: "not clean" is what an empty result says too.
# ---------------------------------------------------------------------------

def test_a_real_clean_file_still_reports_clean(repo: Path) -> None:
    """MUST FIRE. The `clean` this adapter is supposed to emit, unmocked."""
    proc = subprocess.run(
        [sys.executable, str(ADAPTER), str(repo / "base.txt")],
        capture_output=True, text=True, timeout=BUDGET,
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload["ok"] is True, payload
    assert payload["metrics"]["state"] == "clean", payload
    assert "skipped" not in payload, payload


def test_a_real_modified_file_still_reports_modified(repo: Path) -> None:
    """MUST FIRE. The harness can tell one real state from another."""
    (repo / "base.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(ADAPTER), str(repo / "base.txt")],
        capture_output=True, text=True, timeout=BUDGET,
        encoding="utf-8", errors="replace",
    )
    payload = json.loads(proc.stdout.strip())
    assert payload["metrics"]["state"] == "modified", payload
    assert payload["metrics"]["lines_added"] == 1, payload


# ---------------------------------------------------------------------------
# The defect: a stall rendered as a measurement
# ---------------------------------------------------------------------------

def test_a_stall_is_not_reported_as_a_clean_working_tree(stalled: dict) -> None:
    """MUST NOT FIRE. The whole issue, on the adapter's own output."""
    metrics = stalled.get("metrics") or {}
    assert metrics.get("state") != "clean", stalled
    assert stalled.get("ok") is not True, stalled


def test_a_stall_declines_with_the_reserved_code(stalled: dict) -> None:
    """SCHEMA.md reserves `adapter` for a timeout; nothing else is routed."""
    assert stalled["ok"] is False, stalled
    assert stalled["count"] == 1, stalled
    codes = [e["code"] for e in stalled["errors"]]
    assert codes == ["adapter"], stalled
    assert "timed out" in stalled["errors"][0]["msg"].lower(), stalled


def test_the_stall_message_names_git_and_the_budget(stalled: dict) -> None:
    """A decline nobody can act on is only marginally better than a lie."""
    msg = stalled["errors"][0]["msg"]
    assert "git" in msg.lower(), msg
    assert "SUPERTOOL_GIT_TIMEOUT" in msg, msg


def test_a_spawn_failure_is_not_reported_as_a_timeout(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST NOT FIRE: the word `timed out` about a git that never started.

    Both causes produce no measurement and reach the same `adapter` decline,
    which is right. Wording them the same is not: it sends the reader to raise
    a budget that was never the problem — the quiet-wrong-answer shape this
    whole change is about, one layer in.
    """
    mod = _load()

    def _explode(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(mod.subprocess, "Popen", _explode)
    monkeypatch.setattr(mod.subprocess, "run", _explode)
    monkeypatch.setattr(mod.shutil, "which", lambda _n: "/usr/bin/git")
    monkeypatch.setattr(mod.sys, "argv", [str(ADAPTER), str(repo / "base.txt")])
    emitted: list = []
    monkeypatch.setattr("builtins.print",
                        lambda *a, **k: emitted.append(" ".join(map(str, a))))
    mod.main()
    payload = json.loads(emitted[-1])

    assert payload["ok"] is False, payload
    assert payload["errors"][0]["code"] == "adapter", payload
    msg = payload["errors"][0]["msg"]
    assert "timed out" not in msg.lower(), msg
    assert "PermissionError" in msg, msg
    assert "Permission denied" in msg, msg


def test_a_stall_and_a_spawn_failure_do_not_share_a_message(
        stalled: dict, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST FIRE: the stall arm really does say `timed out`.

    The pair is the assertion. The test above passes on an adapter that says
    nothing useful in either arm; this one fails on it.
    """
    assert "timed out" in stalled["errors"][0]["msg"].lower(), stalled
    assert TIMEOUT_ENV in stalled["errors"][0]["msg"], stalled


def test_the_core_reads_a_stall_as_no_verdict(stalled: dict) -> None:
    """The three predicates `code: "adapter"` buys, through the real core."""
    assert supertool._validator_not_checked(stalled) is not None, stalled
    assert supertool._validator_result_is_cacheable(stalled) is False, stalled
    clean = {"tool": "git-status", "ok": True, "count": 0, "errors": []}
    assert supertool._validator_regressed(clean, stalled) is False, stalled
    assert supertool._validator_regressed(None, stalled) is False, stalled


def test_the_adapter_stops_at_the_first_stall(repo: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """One budget for the adapter, not four budgets in a row.

    Four sequential per-call budgets are why the core's own 5s budget always
    won: the adapter could spend 4x its stated wall before saying anything, so
    it was SIGKILLed mid-git rather than declining.
    """
    _payload, children = _drive_stalled(monkeypatch, repo / "base.txt")
    assert len(children) == 1, (
        "the adapter kept spawning git after one call had already blown the "
        "whole budget; children spawned: " + str(len(children)))


# ---------------------------------------------------------------------------
# The budget: configurable, and above 5
# ---------------------------------------------------------------------------

def test_the_default_budget_is_above_the_old_five_seconds() -> None:
    mod = _load()
    assert mod.GIT_TIMEOUT_DEFAULT > 5, mod.GIT_TIMEOUT_DEFAULT


def test_the_default_budget_stays_under_the_configured_validator_timeout() -> None:
    """The adapter must be able to decline before the core kills it.

    A tie is a race (`tests/_adapter_budget.py`), and this pair was 5 == 5.
    """
    config = json.loads((REPO / ".supertool.json").read_text(encoding="utf-8"))
    outer = config["validators"]["git-status"]["timeout"]
    mod = _load()
    assert mod.GIT_TIMEOUT_DEFAULT + mod.TERM_GRACE_S < outer, (
        "inner budget " + str(mod.GIT_TIMEOUT_DEFAULT) + "s + grace "
        + str(mod.TERM_GRACE_S) + "s must leave room under the core's "
        + str(outer) + "s")


@pytest.mark.parametrize("value,expected", [("7", 7), ("120", 120)])
def test_the_budget_honours_the_env_var(value: str, expected: int,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """MUST FIRE. `$SUPERTOOL_GIT_TIMEOUT`, the same knob every other git call reads."""
    mod = _load()
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", value)
    assert mod._budget() == expected


@pytest.mark.parametrize("value", ["", "   ", "nonsense", "0", "-5", "3.5"])
def test_an_unusable_env_value_falls_back_silently(value: str,
                                                   monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture) -> None:
    """MUST NOT FIRE: no prose on stdout.

    `presets/_env.env_int` announces a bad value on stdout, which is correct
    for a preset and fatal here — the core parses this adapter's stdout as
    JSON, so one notice line turns a working validator into `no_verdict`.
    """
    mod = _load()
    monkeypatch.setenv("SUPERTOOL_GIT_TIMEOUT", value)
    assert mod._budget() == mod.GIT_TIMEOUT_DEFAULT
    assert capsys.readouterr().out == ""


def test_the_env_var_is_unset_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load()
    monkeypatch.delenv("SUPERTOOL_GIT_TIMEOUT", raising=False)
    assert mod._budget() == mod.GIT_TIMEOUT_DEFAULT


# ---------------------------------------------------------------------------
# The stranded lock: git is asked to stop before it is killed
# ---------------------------------------------------------------------------

def test_a_stalled_git_is_terminated_before_it_is_killed(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SIGTERM first, SIGKILL only if it does not go.

    `subprocess.run(timeout=)` calls `Popen.kill()` — SIGKILL on POSIX, no
    grace — so git never runs its own cleanup and the `.git/index.lock` it
    holds is stranded. Every later write in that repository then fails.
    """
    _payload, children = _drive_stalled(monkeypatch, repo / "base.txt")
    assert children, "no git child was spawned"
    assert children[0].signals[:1] == ["terminate"], children[0].signals
    assert "kill" in children[0].signals, (
        "a child that ignores SIGTERM must still be killed; signals: "
        + str(children[0].signals))


@pytest.mark.skipif(sys.platform.startswith("win"),
                    reason="POSIX signals: Windows terminate() and kill() are "
                           "both TerminateProcess, so there is no grace period "
                           "to observe and nothing here to assert")
def test_a_real_child_gets_a_real_sigterm_and_can_clean_up(tmp_path: Path) -> None:
    """MUST FIRE, end to end, against a real process and a real signal.

    The in-process test above asserts the adapter's intent. This one asserts
    the platform actually delivers it: a shim standing in for git traps SIGTERM,
    removes the lock it is holding, and exits — which is the whole point of the
    grace period.
    """
    lock = tmp_path / "index.lock"
    lock.write_text("", encoding="utf-8")
    marker = tmp_path / "cleaned"
    shim = tmp_path / "fakegit"
    shim.write_text(
        "#!/bin/sh\n"
        "trap 'rm -f " + str(lock) + "; echo term > " + str(marker) + "; exit 0' TERM\n"
        "while true; do sleep 0.1; done\n",
        encoding="utf-8")
    shim.chmod(0o755)

    target = tmp_path / "subject.txt"
    target.write_text("x\n", encoding="utf-8")

    env = {**os.environ, "GIT_BIN": str(shim), "SUPERTOOL_GIT_TIMEOUT": "1"}
    started = time.time()
    proc = subprocess.run(
        [sys.executable, str(ADAPTER), str(target)],
        capture_output=True, text=True, timeout=BUDGET, env=env,
        encoding="utf-8", errors="replace",
    )
    elapsed = time.time() - started
    assert proc.returncode == 0, proc.stderr

    payload = json.loads(proc.stdout.strip())
    assert payload["ok"] is False, payload
    assert payload["errors"][0]["code"] == "adapter", payload

    assert marker.exists(), (
        "the shim never saw SIGTERM in " + str(round(elapsed, 1)) + "s — it was "
        "SIGKILLed, which is what strands .git/index.lock")
    assert not lock.exists(), "the child was not given time to release its lock"


@pytest.mark.skipif(sys.platform.startswith("win"),
                    reason="POSIX signals: see the sibling test above")
def test_a_child_that_ignores_sigterm_is_still_killed(tmp_path: Path) -> None:
    """MUST NOT HANG. The grace period is bounded, not a promise to wait."""
    shim = tmp_path / "deafgit"
    shim.write_text(
        "#!/bin/sh\n"
        "trap '' TERM\n"
        "while true; do sleep 0.1; done\n",
        encoding="utf-8")
    shim.chmod(0o755)

    target = tmp_path / "subject.txt"
    target.write_text("x\n", encoding="utf-8")

    env = {**os.environ, "GIT_BIN": str(shim), "SUPERTOOL_GIT_TIMEOUT": "1"}
    proc = subprocess.run(
        [sys.executable, str(ADAPTER), str(target)],
        capture_output=True, text=True, timeout=BUDGET, env=env,
        encoding="utf-8", errors="replace",
    )
    payload = json.loads(proc.stdout.strip())
    assert payload["ok"] is False, payload
    assert "timed out" in payload["errors"][0]["msg"].lower(), payload


#: Deletion vocabulary, matched as an attribute (`os.unlink`, `p.unlink()`,
#: `shutil.rmtree`) AND as a bare name, which is what a `from os import remove`
#: leaves behind. Matching only the attribute form was the first version of
#: this guard and it missed every from-import spelling.
_DELETE_NAMES = frozenset({"remove", "unlink", "rmtree", "rmdir", "removedirs"})

#: A shell is a deletion primitive wearing a costume: `os.system("rm -f " +
#: lock)` is the shape this guard exists to refuse and the one an AST walk for
#: `unlink` sails straight past. This adapter has no legitimate use for either.
_SHELL_NAMES = frozenset({"system", "popen"})

#: Every way this file could start a process. The rule below is not "do not
#: spawn" -- it spawns git, that is its job -- it is that the argv of every
#: spawn must begin with `git_bin`, which refuses `subprocess.run(["rm", ...])`
#: without needing to recognise `rm` as dangerous.
_SPAWN_NAMES = frozenset({"run", "call", "check_call", "check_output", "Popen"})


#: argv[0] spellings that mean "this spawn is git". `git_bin` is the adapter's
#: own variable; the bare string is allowed so that hoisting or inlining the
#: binary name is not refused as if it were an attack.
_GIT_ARGV0 = frozenset({"git_bin", "git"})


def _deletion_aliases(tree: ast.Module) -> "set[str]":
    """Local names bound to a deletion primitive by a from-import.

    `from os import remove as rm2` binds `rm2`, and a guard keyed on the
    vocabulary alone never sees it again. Written because the must-fire control
    for exactly this shape failed on the first draft of this guard.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _DELETE_NAMES:
                    names.add(alias.asname or alias.name)
    return names


def _lock_deletion_routes(src: str) -> "list[str]":
    """Every route to deleting a file this walk can see, as readable strings.

    Returns [] for source that only spawns git. See
    `test_no_lock_is_ever_deleted_by_the_adapter` for what this deliberately
    cannot see.
    """
    tree = ast.parse(src)
    delete_names = _DELETE_NAMES | _deletion_aliases(tree)
    found: list = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = func.attr if isinstance(func, ast.Attribute) else None
        bare = func.id if isinstance(func, ast.Name) else None
        name = attr or bare
        if name is None:
            continue

        if any(kw.arg == "shell" and getattr(kw.value, "value", False) is True
               for kw in node.keywords):
            found.append("spawn with shell=True: " + name)
            continue
        if name in delete_names:
            found.append("deletion call: " + name)
            continue
        if name in _SHELL_NAMES:
            found.append("shell call: " + name)
            continue
        if name not in _SPAWN_NAMES:
            continue

        argv = node.args[0] if node.args else None
        # A BARE name is only treated as a spawn when it is shaped like one.
        # This adapter's own inner helper is called `run`, and reading
        # `run("rev-parse", ...)` as an un-gitlike spawn made the guard fire
        # four times on the very file it is guarding. An attribute call
        # (`subprocess.run`) is always checked, because that is how a spawn is
        # actually written and there is no local-helper collision to protect.
        if bare is not None and not isinstance(argv, ast.List):
            continue

        head = None
        if isinstance(argv, ast.List) and argv.elts:
            first = argv.elts[0]
            head = (first.id if isinstance(first, ast.Name)
                    else first.value if isinstance(first, ast.Constant)
                    else None)
        if head not in _GIT_ARGV0:
            found.append("spawn whose argv[0] is not git: "
                         + name + "(" + repr(head) + ")")
    return sorted(found)


def test_no_lock_is_ever_deleted_by_the_adapter() -> None:
    """The issue's fourth suggestion, refused on purpose.

    Removing a `.git/index.lock` the handler believes it orphaned is a
    destructive act on somebody else's repository taken on an inference: this
    adapter cannot tell its own stranded lock from one a concurrent `git
    commit` legitimately holds, and deleting the second corrupts that commit.
    The grace period above fixes the damage without guessing. If this ever
    becomes wanted it needs its own issue and its own argument, not a rider
    here.

    **What this guard covers, and what it cannot (#1883 review).** The first
    version walked only for `ast.Attribute` calls named `unlink`/`remove`/
    `rmtree`/`rmdir`, and its single positive control fed it `os.unlink('x')`.
    Measured against seven realistic spellings, five walked straight through:
    `os.system("rm -f " + lock)`, `subprocess.run(["rm", "-f", p])`,
    `subprocess.call(...)`, `from os import remove as rm2; rm2(p)` and
    `from os import unlink; unlink(p)`. A green guard read as "this adapter
    cannot delete a lock" while proving only "not via four attribute
    spellings" -- this repository's own defect class, an absence produced by
    the checker read as an absence in the world, inside the test written to
    stop that very drift.

    It now covers four routes: the deletion vocabulary as an attribute OR a
    bare name, **with from-import aliases resolved** (`from os import remove as
    rm2` binds `rm2`); any call to a shell (`os.system`, `os.popen`); any call
    passing `shell=True`, which turns an arbitrary string into a command; and
    any spawn whose `argv[0]` is not git, which refuses `subprocess.run(["rm",
    ...])` without this test having to know that `rm` deletes things.

    **Two of those branches exist because the must-fire controls below failed
    on the first draft of this same fix.** The alias branch was claimed in this
    docstring and absent from the code; and treating every bare `run(...)` as a
    spawn made the guard fire four times on the adapter itself, whose own inner
    helper is named `run`. A bare name is therefore only read as a spawn when
    its first argument is a list literal -- which is the deliberate hole that
    lets `run("rev-parse", ...)` through, and it is stated rather than hidden.

    **It still cannot close the class, and saying so is the point.** A name
    assembled at runtime (`getattr(os, "unl" + "ink")(p)`), an `eval`, a
    deletion reached through a helper defined elsewhere, or a **git subcommand
    that deletes for us** (`git clean -fdx` has a git argv[0] and is allowed by
    every branch above) reaches none of these checks. No AST walk closes that
    set. This is a tripwire on the routes a real patch would actually take, not
    a proof of impossibility, and a reader who needs the stronger claim does
    not have it.
    """
    routes = _lock_deletion_routes(ADAPTER.read_text(encoding="utf-8"))
    assert routes == [], (
        "git-status.py can reach a file deletion -- " + str(routes)
        + " -- see this test's docstring before adding lock removal")


@pytest.mark.parametrize("label,src", [
    ("attribute deletion", "import os" + chr(10) + "os.unlink(p)"),
    ("pathlib method", "import pathlib" + chr(10) + "pathlib.Path(p).unlink()"),
    ("shutil.rmtree", "import shutil" + chr(10) + "shutil.rmtree(p)"),
    ("from-import, plain", "from os import unlink" + chr(10) + "unlink(p)"),
    ("from-import, aliased", "from os import remove as rm2" + chr(10) + "rm2(p)"),
    ("os.system", "import os" + chr(10) + "os.system('rm -f ' + lock)"),
    ("os.popen", "import os" + chr(10) + "os.popen('rm ' + lock)"),
    ("subprocess.run rm", "import subprocess"
                          + chr(10) + "subprocess.run(['rm', '-f', p])"),
    ("subprocess.call rm", "import subprocess"
                           + chr(10) + "subprocess.call(['rm', p])"),
    ("subprocess.Popen rm", "import subprocess"
                            + chr(10) + "subprocess.Popen(['rm', p])"),
    ("shell=True at all", "import subprocess"
                          + chr(10) + "subprocess.run('rm -f ' + p, shell=True)"),
    ("a bare spawn name with a list argv", "from subprocess import run"
                                           + chr(10) + "run(['rm', p])"),
    ("git spawn that deletes via a subcommand is NOT covered -- see the "
     "docstring; this row asserts the shell keyword, not the subcommand",
     "import subprocess" + chr(10)
     + "subprocess.run(['git', 'clean', '-fdx'], shell=True)"),
])
def test_the_lock_guard_fires_on_every_shape_it_claims(label: str, src: str) -> None:
    """MUST FIRE, one control per covered shape.

    The rule this file applies everywhere else, applied to the guard itself: a
    negative assertion passes when the matcher is broken, so every branch the
    docstring above claims has to be shown catching something. Five of these
    ten were silently uncovered until #1883.
    """
    assert _lock_deletion_routes(src) != [], label


@pytest.mark.parametrize("label,src", [
    ("the adapter's own git spawn",
     "import subprocess" + chr(10)
     + "subprocess.Popen([git_bin, *args], cwd=d)"),
    ("a git spawn by literal name",
     "import subprocess" + chr(10) + "subprocess.run(['git', 'status'])"),
    ("prose naming index.lock",
     '"""We deliberately do not remove .git/index.lock here."""'),
    ("terminate and kill, which are not deletions",
     "proc.terminate()" + chr(10) + "proc.kill()"),
    ("the adapter's own inner helper, which is called `run`",
     "run('rev-parse', '--is-inside-work-tree')"),
    ("a local helper named `call` taking a string",
     "call('status')"),
])
def test_the_lock_guard_stays_quiet_on_what_it_must_not_refuse(
        label: str, src: str) -> None:
    """MUST NOT FIRE. A guard that refuses the adapter's own git spawn, or its
    own docstring, gets routed around within a week -- and `kill` sits one
    keyword away from the deletion vocabulary in both senses.

    The literal-`git` case is deliberately allowed: the rule is argv[0] is
    `git_bin` or the string `git`, so an author hoisting the binary name to a
    constant has to say so rather than being refused for tidying.
    """
    assert _lock_deletion_routes(src) == [], label


def test_signal_module_is_not_needed_for_the_grace_period() -> None:
    """Documents the portable route: Popen.terminate(), not os.kill(SIGTERM).

    `signal.SIGTERM` exists on Windows but `os.kill` there ignores it and
    terminates regardless; `Popen.terminate()` is the one spelling that means
    "ask nicely where asking exists" on every platform Python supports.
    """
    assert hasattr(signal, "SIGTERM")
    src = ADAPTER.read_text(encoding="utf-8")
    assert "os.kill(" not in src, src
