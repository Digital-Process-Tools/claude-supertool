"""Every git preset runs git through the one shared helper (#704).

`presets/git/_git_common.py` has existed since #302, and five presets import
something from it — but only two import `_git`. The other nine each carried
their own `subprocess.run(["git"] + args, ...)`, and `blame.py` carried two
inline. That is what made #685 a half-fix: it repaired `status.py::_git` so a
git call that does not answer stops speaking as though it did, and left the
same defect live in `conflicts.py`, where `git-conflicts` then printed
`No conflicted files.` and exit 0 over live `<<<<<<<` markers until #703.

Two guards, and they are not the same guard:

**The behaviour test** is the post-condition. A git call that expires reports
the same way from every preset — `TIMEOUT_RC` and a stderr that says so, never
a `TimeoutExpired` escaping into the caller's report. Asserting `from
_git_common import _git` would be a proxy for this and would pass on an import
that nothing calls.

**The structural test** is what stops the eleventh copy. It is the shape of
`test_every_mutable_global_...`: a sweep of the tree that fails the build when
a new one appears, rather than a comment asking people not to.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest

from _preset_loader import load_preset_module

GIT_DIR = Path(__file__).resolve().parent.parent / "presets" / "git"

#: The one module allowed to spawn a git process. Everything else asks it.
OWNER = "_git_common.py"


def _load(name: str) -> ModuleType:
    """Execute ``presets/git/<name>.py`` in isolation, path restored after."""
    return load_preset_module("git", name, prefix="git704_")


#: Every preset in presets/git/ that runs git. All of them, deliberately — the
#: point of the issue is that a subset is how the defect survives.
PRESETS = sorted(p.stem for p in GIT_DIR.glob("*.py"))


# ── the behaviour: a git call that does not answer says so, everywhere ──────

@pytest.mark.parametrize("preset", PRESETS)
def test_expired_git_call_reports_the_same_way_from_every_preset(preset: str) -> None:
    """`_git` returns TIMEOUT_RC and names the stall — it never raises.

    The three-state contract (#650): a call that ran and found nothing, a call
    that ran and found something, and a call that could not run are three
    different facts, and the third must not be rendered as either of the first
    two. A `TimeoutExpired` escaping is the loudest version of the same bug —
    `git-status` lost its entire report, stack trace and all, to a stalled
    `rev-list` that was only ever a courtesy line.
    """
    mod = _load(preset)
    git = getattr(mod, "_git", None)
    assert git is not None, (
        f"presets/git/{preset}.py runs git but exposes no `_git` — it should "
        f"import it from {OWNER}")

    class _StalledProc:
        """A `Popen` double whose `communicate()` never answers (#2033 moved
        `_git` off `subprocess.run(timeout=)` onto `Popen`, so this test's own
        double has to move with it -- see `tests/test_git_common_stranded_lock_2033.py`
        for the real-child version of the same claim)."""

        def __init__(self, cmd, **_kw) -> None:
            self.args = cmd
            self.returncode = 0

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout or 0)

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

        def wait(self, timeout=None):
            return 0

    with mock.patch("subprocess.Popen", side_effect=_StalledProc):
        res = git(["status", "--porcelain"])

    assert isinstance(res, subprocess.CompletedProcess)
    assert res.returncode == 124, (
        f"{preset}: an expired git call must carry TIMEOUT_RC (124), got "
        f"{res.returncode}")
    assert "timed out" in res.stderr.lower(), (
        f"{preset}: an expired git call must say why, got {res.stderr!r}")
    assert res.stdout == "", (
        f"{preset}: an expired git call has no output to report")


@pytest.mark.parametrize("preset", PRESETS)
def test_explicit_budget_is_the_budget_that_is_used(preset: str) -> None:
    """A call site that names its own timeout gets it.

    The contract picked in #704: the argument wins, the environment sets the
    default. `git-push` gives its push 300s on purpose; a `SUPERTOOL_GIT_TIMEOUT`
    meant to shorten the courtesy calls in `git-status` must not silently cap
    it at 5 and report a push that is still in flight as failed.
    """
    mod = _load(preset)
    git = getattr(mod, "_git", None)
    assert git is not None

    class _AnsweringProc:
        """A `Popen` double that answers immediately, recording the timeout
        `communicate()` was given (#2033 moved `_git` off `subprocess.run`)."""

        def __init__(self, cmd, **_kw) -> None:
            self.args = cmd
            self.returncode = 0
            self.communicate_timeout = None

        def communicate(self, timeout=None):
            self.communicate_timeout = timeout
            return "", ""

    created: list = []

    def _fake_popen(cmd, **kw):
        proc = _AnsweringProc(cmd, **kw)
        created.append(proc)
        return proc

    with mock.patch("subprocess.Popen", side_effect=_fake_popen):
        git(["status"], timeout=173)
    assert created and created[-1].communicate_timeout == 173, (
        f"{preset}: an explicit timeout must reach Popen.communicate() unchanged")


# ── the structure: one implementation, and it stays one ────────────────────

def _spawns_git(node: ast.AST) -> bool:
    """True for a `subprocess.run([...])` whose argv starts with 'git'."""
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    if not (isinstance(fn, ast.Attribute) and fn.attr == "run"
            and isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"):
        return False
    if not node.args:
        return False
    argv = node.args[0]
    if isinstance(argv, ast.BinOp) and isinstance(argv.op, ast.Add):
        argv = argv.left
    return (isinstance(argv, ast.List) and bool(argv.elts)
            and isinstance(argv.elts[0], ast.Constant)
            and argv.elts[0].value == "git")


def test_only_git_common_spawns_a_git_process() -> None:
    """No preset may run its own `subprocess.run(["git", ...])`.

    This is the test that would have failed when the tenth copy was written,
    and the reason #685 could not have shipped as a fix for one file while
    reading as a fix for the defect. A helper that merely *wraps*
    `_git_common._git` — `status.py` adding the call to its `INCOMPLETE`
    ledger, `blame.py` keeping its own `OSError` wording — is fine and is not
    what this looks for. Spawning the process is what must happen in one place,
    because that is where the timeout, the encoding and the three-state answer
    are decided.
    """
    offenders: list[str] = []
    for path in sorted(GIT_DIR.glob("*.py")):
        if path.name == OWNER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if _spawns_git(node):
                offenders.append(f"presets/git/{path.name}:{node.lineno}")
    assert not offenders, (
        "these run git themselves instead of through "
        f"_git_common._git — a fix to one of them is not a fix to the tool "
        f"(#704): {', '.join(offenders)}")


def test_list_conflicts_has_exactly_one_definition() -> None:
    """`_list_conflicts` lives in `_git_common`, and only there.

    Three copies, and #703 had to repair the three-state return in all three
    at once. The copy in `conflicts.py` is the one that mattered — that preset
    is what you run while stopped mid-merge — but nothing about the other two
    said so, and nothing said they existed.
    """
    definers: list[str] = []
    for path in sorted(GIT_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "_list_conflicts":
                definers.append(f"{path.name}:{node.lineno}")
    files = [d.split(":")[0] for d in definers]
    assert files == [OWNER], (
        f"_list_conflicts must be defined once, in {OWNER}; found: "
        f"{', '.join(definers) or 'nowhere'}")


def test_the_shared_module_holds_no_module_level_mutable_state() -> None:
    """`_git_common` stays stateless — the #689 trap, designed around.

    `presets/_env.py` is the precedent for a module many presets import under
    one name, and it is also the warning: its module-level `_ANNOUNCED` ledger
    became process-global under pytest and needed a `PRESET_RESET_GLOBALS`
    entry in `conftest.py` to stay honest. Eleven presets now import this one.
    A list or dict here would have to be registered there too, and the
    registration is the part that gets forgotten — so there is nothing to
    register. `status.py` keeps its own `_UNANSWERED` ledger, where it is
    already declared and already reset.
    """
    tree = ast.parse((GIT_DIR / OWNER).read_text(encoding="utf-8"))
    mutable: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if isinstance(value, (ast.List, ast.Dict, ast.Set)):
            targets = ([node.target] if isinstance(node, ast.AnnAssign)
                       else node.targets)
            mutable += [t.id for t in targets if isinstance(t, ast.Name)]
    assert not mutable, (
        f"{OWNER} is imported by every git preset; module-level mutable state "
        f"here is process-global under pytest and needs a conftest reset entry "
        f"(#689). Found: {', '.join(mutable)}")
