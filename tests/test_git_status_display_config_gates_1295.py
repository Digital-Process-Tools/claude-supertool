"""A gate that reads `git status` with inherited display config (#1295).

`status.showUntrackedFiles=no` is an ordinary user or repo preference. It
suppresses `??` records - and, as #1290 established on the ignored-files gate,
`!!` records too. A consumer running `git status --porcelain` with inherited
config therefore receives an **empty list from a dirty tree**, and cannot tell
"nothing there" from "not looked". That is this repo's standing defect class:
an absence produced by the tool, read as an absence in the world.

#1290 fixed one site. #1295 swept and found eight more. This file covers the
ones that are **gates** - code that decides whether to proceed - rather than
renders. The split is the whole judgment, so it is written down in `REGISTER`
below rather than left to the next reader to re-derive. There were three; the
`scripts/oss_train.py` gate left with the script in #1472, and `TREES` is
still swept for `scripts/` so a gate reappearing there is red rather than
unnoticed.

**Why the pin goes on the command line.** `-c` outranks config files *and* the
environment: `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=... GIT_CONFIG_VALUE_0=no`
loses to `-c status.showUntrackedFiles=normal`. That is asserted below rather
than believed.

**`normal`, not `all`.** `all` additionally defeats git's own directory
collapse, so an untracked `venv/` arrives as N lines instead of one. Every gate
here only needs to know the tree is not empty; `normal` establishes that and
`all` would flood a refusal with paths.
"""
from __future__ import annotations

import ast
import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent


def _load(relpath: str, name: str):
    """Import a preset or script by path - `tests/` is not reliably importable."""
    spec = importlib.util.spec_from_file_location(name, REPO / relpath)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def poisoned_repo(tmp_path):
    """A git repo that is dirty and configured not to mention it.

    The dirt is deliberately **untracked** and not a modified tracked file:
    `status.showUntrackedFiles=no` hides only the untracked half, so a gate
    reading a modified tracked file would still see something and every test
    below would pass for the wrong reason.
    """
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=str(tmp_path), check=True,
            capture_output=True, text=True, encoding="utf-8", errors="replace")

    git("init", "-q", "-b", "master")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "tracked.txt").write_text("one\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-qm", "init")

    (tmp_path / "forgotten.txt").write_text("work nobody committed\n",
                                            encoding="utf-8")
    git("config", "status.showUntrackedFiles", "no")

    # The premise, asserted rather than assumed: an inherited-config read really
    # is blind here. Without this, a future git that changed what the setting
    # covers would leave every test below passing while proving nothing.
    blind = git("status", "--porcelain")
    assert blind.stdout.strip() == "", (
        "premise broken: an inherited-config read still saw the tree, so "
        "nothing below tests the suppression it claims to test")
    return tmp_path


# ===========================================================================
# Gate 1 - the pre-push dirty-tree guard (presets/git/push.py)
# ===========================================================================

def test_push_leftovers_check_sees_a_tree_the_config_hid(poisoned_repo,
                                                          monkeypatch):
    """`_uncommitted_leftovers` is the "you forgot to commit X" catch.

    It already had three states for a `git status` that *failed*. It had none
    for a `git status` that succeeded and had been configured not to look,
    which renders identically to a clean tree - silence in the receipt, on the
    run right after a push landed.
    """
    push = _load("presets/git/push.py", "st_push_1295")
    monkeypatch.chdir(poisoned_repo)
    leftovers, why = push._uncommitted_leftovers()
    assert why == ""
    assert leftovers is not None
    assert any("forgotten.txt" in ln for ln in leftovers), (
        "the pre-push guard reported a clean tree while an uncommitted file "
        "sat in it")


def test_the_pin_outranks_the_environment_too(poisoned_repo, monkeypatch):
    """`-c` beats `GIT_CONFIG_*`, which is why the pin is on the argv.

    Carried from #1290 as a measurement rather than a belief: a fix that set
    the value through the environment would lose to a user who had set it
    through the environment, and nothing on the command line would say so.

    Index 0 is `tests/conftest.py`'s own `core.fsmonitor` suppression
    (#1892) -- overwriting it with `GIT_CONFIG_COUNT=1` the way this test
    used to would clobber that entry for the one `git status` call below,
    reopening the leak this test has no reason to reopen. Index 1 is what
    this test actually asserts on: `GIT_CONFIG_COUNT`/`KEY_n`/`VALUE_n` is
    positional, not additive, so both entries have to be named together
    rather than one `setenv` per line.
    """
    push = _load("presets/git/push.py", "st_push_env_1295")
    monkeypatch.chdir(poisoned_repo)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "status.showUntrackedFiles")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "no")
    leftovers, why = push._uncommitted_leftovers()
    assert why == ""
    assert leftovers is not None
    assert any("forgotten.txt" in ln for ln in leftovers)


# ===========================================================================
# Gate 2 - `--all` resolution (presets/git/commit.py)
# ===========================================================================
#
# There were three gates here. The second was `scripts/oss_train.py::train`,
# whose #1298 repair this file pinned; #1472 deleted that script, so the gate
# went with it and the third was renumbered rather than left with a hole. The
# register below is the load-bearing half and it is derived from the tree, so
# it reports the remaining population without depending on this comment.

def test_commit_all_token_sees_a_tree_the_config_hid(poisoned_repo,
                                                      monkeypatch):
    """#1295 files this one under "render". It is the strongest gate of the three.

    `_worktree_changes` feeds `_resolve_all_token`, which produces the list of
    paths `git-commit:::MSG:::--all` then **stages and commits**. A suppressed
    untracked half does not merely render short: the op commits a subset of
    what the caller asked for, under a receipt naming that subset as the answer
    to `--all`. Nothing in the output distinguishes it from a tree whose
    untracked half was genuinely empty.

    A wrong render is a `misreports`. This is a wrong *write*, chosen by a read
    that was configured not to look - so it is pinned with the other two.
    """
    commit = _load("presets/git/commit.py", "st_commit_1295")
    monkeypatch.chdir(poisoned_repo)
    modified, untracked, unknown = commit._worktree_changes()
    assert unknown == ""
    assert "forgotten.txt" in untracked, (
        "`--all` resolved to a list missing an untracked file, and would have "
        "committed the subset under a receipt claiming --all")


# ===========================================================================
# The register - every `git status` read under presets/ and scripts/, judged
# ===========================================================================

#: `PIN` - the site must carry `-c status.showUntrackedFiles=normal` on the
#: argv it builds. `PIN_IN_RUNNER` - the module prepends the pins in a shared
#: runner instead, and a dedicated test below checks that runner. Any other
#: value is a recorded reason for deliberately leaving the user's display
#: setting alone, and must say why the site decides nothing.
PIN = "PIN"
PIN_IN_RUNNER = "PIN_IN_RUNNER"

#: Keyed `path::enclosing function`, never on line numbers - a register that
#: goes stale on every unrelated edit teaches people to regenerate it without
#: reading it. Same construction as the splitlines (#1130) and symlink (#1232)
#: registers.
REGISTER: dict[str, str] = {
    # -- gates: something branches on the result, or a write is derived from it
    "presets/git/push.py::_uncommitted_leftovers": PIN,
    "presets/git/commit.py::_worktree_changes": PIN,
    "presets/github/pr_merge.py::_worktree_dirt": PIN_IN_RUNNER,
    # #1751. The reap board's uncommitted-work column, and the most literal
    # instance of this defect class on the register: the op's exit code
    # branches on it, and a caller gates `git worktree remove` on that integer.
    # With the setting inherited, a tree whose only work is untracked reports
    # clean and the gated call deletes it.
    "presets/git/worktrees.py::dirty_for": PIN,

    # -- renders: shown to a human, and nothing else reads the result.
    # Pinning these would override a display preference the user set on
    # purpose, in the two places whose entire job is to display it.
    "presets/git/status.py::main":
        "render only - prints the working-tree section for a human and "
        "branches on nothing. A user who set showUntrackedFiles=no wants "
        "git-status quiet about untracked files, and this op is exactly where "
        "that preference is meant to take effect. It already carries the third "
        "state for a status that did not answer (#1002).",
    "presets/git/checkout.py::main":
        "render only - a one-line 'Working tree: N staged, N untracked' "
        "courtesy note beside the branch report. Nothing downstream reads it "
        "and no action is gated on it, so a quiet count is the user's own "
        "setting doing what they asked for.",
}

#: Core sites, deliberately outside this register's scope: `_supertool.py` was
#: under concurrent rewrite when #1295 was implemented, so its three consumers
#: were left rather than merged into someone else's live edit. Named here so
#: the zero above reads as "out of scope" and not as "none exist" - which is
#: the very defect this file is about.
UNSWEPT_CORE = ("_supertool.py:4095", "_supertool.py:4280",
                "_supertool.py:16715")

TREES = ("presets/git", "presets/github", "scripts")

#: A tree in `TREES` that does not exist on disk contributes zero call sites,
#: and a zero produced by a missing directory renders identically to a zero
#: produced by a directory with nothing in it - which is this file's entire
#: subject, arriving in the sweep rather than in the code being swept.
#:
#: `scripts/` held exactly one file, `oss_train.py`, and #1472 deleted it. The
#: name is kept in `TREES` deliberately: a `scripts/` written again next month
#: must be swept, and a name removed from the sweep is a sweep nobody
#: remembers narrowing (#861 is the same shape one directory up). So the tree
#: stays listed, its absence is declared here with the reason, and the test
#: below reds in both directions - an undeclared absence, and a declared one
#: that came back.
ABSENT_TREES: dict[str, str] = {
    "scripts": (
        "emptied by #1472, which deleted `scripts/oss_train.py` - the only "
        "file it ever held - along with the `scripts/oss_train.py::train` "
        "gate this file used to pin. Kept in TREES so a new script here is "
        "swept on the day it lands rather than on the day someone notices."
    ),
}


def _status_call_sites() -> dict[str, list[list[str]]]:
    """`{path::func: [string-literals, ...]}` for every `git status` argv.

    AST, not grep: docstrings in this tree mention `git status` constantly and
    none of them is a call site. Every list/tuple literal inside a function is
    examined rather than only a call's arguments, because `status.py` builds
    `status_cmd = ["status", ...]` and passes the name - a collector reading
    only call arguments would miss it and report a clean sweep.

    Keyed on an element that is exactly `"status"`: the subcommand. A pin
    (`status.showUntrackedFiles=normal`) also begins with `status`, so a prefix
    match would make every pinned site look like two.
    """
    found: dict[str, list[list[str]]] = {}
    for tree in TREES:
        root = REPO / tree
        # Skipped explicitly rather than left to `rglob` returning empty for a
        # missing directory: the silent version is the same zero this file
        # exists to refuse, and `ABSENT_TREES` is what makes it a statement.
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            parsed = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(parsed):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for lit in ast.walk(node):
                    if not isinstance(lit, (ast.List, ast.Tuple)):
                        continue
                    strs = [e.value for e in lit.elts
                            if isinstance(e, ast.Constant)
                            and isinstance(e.value, str)]
                    if "status" in strs:
                        found.setdefault(f"{rel}::{node.name}", []).append(strs)
    return found


def test_every_git_status_read_has_been_judged():
    """A new `git status` under these trees is red until someone decides.

    This is the alternative to a shared helper, and it was chosen over one for
    a reason the helper cannot meet: **the answer is not the same at every
    site.** A helper that pins everything overrides a display preference at the
    two render sites, which #1295 explicitly declines to do; a helper that pins
    nothing is the defect itself. Only a per-site judgment expresses that - and
    only a register makes the judgment visible at the moment a new site is
    written, which is the one place a helper hides it.
    """
    sites = _status_call_sites()
    unjudged = sorted(set(sites) - set(REGISTER))
    assert not unjudged, (
        "new `git status` call site(s) under " + ", ".join(TREES)
        + " with no entry in REGISTER: " + ", ".join(unjudged)
        + ". Decide whether the site is a GATE (something branches on the "
        "result, or a write is derived from it) or a RENDER (shown to a human, "
        "and nothing else reads it). A gate gets "
        "`-c status.showUntrackedFiles=normal` on its argv and the value PIN; "
        "a render gets a written reason. See #1290/#1295.")

    stale = sorted(set(REGISTER) - set(sites))
    assert not stale, (
        "REGISTER names call site(s) that no longer exist: " + ", ".join(stale)
        + ". A register that outlives its sites stops being checked.")


def test_every_gate_actually_carries_the_pin():
    """The half a register usually forgets: that PIN means something.

    Without this the register is a table of intentions. `REGISTER` saying PIN
    while the argv carries no `-c` is precisely the inert-rule shape this repo
    keeps filing - a gate that reads as enforced and has never fired.
    """
    sites = _status_call_sites()
    missing = []
    for key, judgment in REGISTER.items():
        if judgment != PIN:
            continue
        for strs in sites.get(key, []):
            if "status.showUntrackedFiles=normal" not in strs:
                missing.append(f"{key} -> {strs}")
    assert not missing, (
        "registered as a gate, but the invocation does not pin the display "
        "setting on the command line: " + "; ".join(missing))


def test_the_runner_pinned_site_really_pins_in_its_runner():
    """`PIN_IN_RUNNER` is checked, not taken on trust.

    `pr_merge._worktree_dirt` builds an argv with no `-c` in it; `_dirt_read`
    prepends `_DIRT_PINS`. That is a legitimate arrangement and it is also
    exactly how a pin goes missing without any call site changing, so the
    constant is asserted rather than assumed.
    """
    pr_merge = _load("presets/github/pr_merge.py", "st_pr_merge_1295")
    assert "status.showUntrackedFiles=normal" in pr_merge._DIRT_PINS
    registered = [k for k, v in REGISTER.items() if v == PIN_IN_RUNNER]
    assert registered == ["presets/github/pr_merge.py::_worktree_dirt"]


def test_no_gate_pins_untracked_files_all():
    """`all` defeats git's directory collapse and floods a refusal.

    Measured under #1290: an untracked `venv/` arrives as one collapsed entry
    under `normal` and as N entries under `all`. Every gate here only needs to
    establish that the tree is non-empty, which `normal` does.
    """
    offenders = []
    for key, lits in _status_call_sites().items():
        if REGISTER.get(key) not in (PIN, PIN_IN_RUNNER):
            continue
        for strs in lits:
            if "--untracked-files=all" in strs:
                offenders.append(key)
    assert not offenders, (
        "a gate asked for --untracked-files=all: " + ", ".join(offenders))


def test_a_swept_tree_that_is_gone_is_declared_rather_than_contributing_zero():
    """The sweep's own version of the defect the sweep looks for.

    Both directions. An undeclared missing tree means the register reports a
    clean `scripts/` it never opened; a declared tree that came back means the
    reason above is describing a directory that is now being swept for real,
    and the note would read as an exemption it is not.
    """
    absent = sorted(t for t in TREES if not (REPO / t).is_dir())
    assert absent == sorted(ABSENT_TREES), (
        "TREES and ABSENT_TREES disagree about which swept trees exist. "
        f"absent on disk: {absent}; declared absent: {sorted(ABSENT_TREES)}. "
        "A tree that vanished contributes zero call sites and reads exactly "
        "like a tree with none.")
    for tree, why in ABSENT_TREES.items():
        assert tree in TREES, (
            tree + " is declared absent but is not swept at all, so the "
            "declaration guards nothing")
        assert len(why) > 40, (tree, why)


def test_the_core_sites_are_named_rather_than_silently_absent():
    """A zero meaning "out of scope" must not read as "none found".

    `_supertool.py` is not in `TREES`, so this register says nothing about its
    three consumers. That is a scoping decision, and an unstated one would be
    the same absence-read-as-absence defect the register exists to catch.
    """
    assert len(UNSWEPT_CORE) == 3
    assert all(s.startswith("_supertool.py:") for s in UNSWEPT_CORE)
