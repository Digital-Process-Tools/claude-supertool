"""A leaked git environment must never redirect an op to another repo (#692).

#416 taught this lesson once and applied it to the *test runner*: the pre-push
hook unsets git's repo pointers before starting pytest. The ops never got the
same treatment. `supertool.py` launches every preset with `dict(os.environ)`,
so a `GIT_DIR` set anywhere in the parent — most commonly by git itself, which
exports it to every hook, and `.githooks/pre-commit` invokes `./supertool
'git-diff:staged'` — silently retargets the op at the repository that variable
names rather than the one the caller is standing in.

Proven before the fix, by hand: `git-commit` run with cwd=repoB and
GIT_DIR=repoA/.git wrote the commit into repoA. The receipt named no
repository at all, so nothing in the output contradicted the caller's entirely
reasonable belief that the commit went where they were.

Two halves are tested here, and the second is the one that matters longer:

* the scrub — the op acts on the repo at cwd;
* the receipt — the ops that *write* say which repo they wrote to, as
  `git-diff` already did.

The control test below is load-bearing. Every assertion here would pass
vacuously against a git that ignored `GIT_DIR`, or against two "repos" that
were secretly the same one, so one test asserts that raw git *does* honour the
leak in exactly this fixture. A green suite therefore means the scrub acted,
not that the mechanism was never there.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import conftest
import supertool

SUITE_ROOT = Path(__file__).resolve().parent.parent
SUPERTOOL = SUITE_ROOT / "supertool.py"

_ID = ["-c", "user.email=fixture@example.invalid", "-c", "user.name=fixture"]


def _git(args, cwd, env=None):
    return subprocess.run(
        ["git", *_ID, *args], cwd=str(cwd), env=env,
        capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
    )


def _make_repo(path: Path, name: str, with_remote: bool = False,
               branch: str = "main") -> Path:
    """A real repo with one commit, supertool's git preset enabled.

    Returns the bare remote when `with_remote`, else the repo itself. Each repo
    carries a file whose name embeds `name`, so a commit landing in the wrong
    one is visible in the tree, not only in the log message.
    """
    path.mkdir(parents=True)
    _git(["init", "-q", "-b", branch], path)
    # Identity as repo CONFIG, not only as the `-c` flags in `_ID`. Those apply
    # to the git commands this file runs; the commit under test is made by
    # supertool's own `git` child, which inherits none of them. A machine with a
    # global user.email cannot see the difference — local and the macOS legs
    # passed while every ubuntu and windows leg failed with
    # `fatal: empty ident name`. Same pattern as test_git_commit.py and five
    # other files here.
    _git(["config", "user.email", "fixture@example.invalid"], path)
    _git(["config", "user.name", "fixture"], path)
    # The op budget is raised for the fixture only, and the shipped default is
    # not touched. `git-status` costs ~1.3s against its shipped 10s on an idle
    # machine — seven times the headroom, and still not enough on a two-core
    # Windows runner under `-n auto`, where each of its ~8 git spawns pays
    # Defender and a slower process creation. It blew the budget on
    # `windows-latest, 3.12` twice while `3.9`/`3.10`/`3.11` passed the same
    # code, which is the signature of contention rather than of a defect.
    #
    # Same call as `SUPERTOOL_LINT_TIMEOUT=30` in `conftest.py`: a budget in the
    # suite is a guard against an op that stalled, not a stopwatch on the
    # runner, and this file asserts on which repository the op acted — never on
    # how fast it did so. A genuine hang is still caught by the 180s ceiling on
    # `_run_op`'s own `subprocess.run`.
    #
    # A project `ops` entry deep-merges key-by-key over the preset's, so naming
    # `timeout` alone keeps `presets/git.json`'s cmd (see `_merge_presets`).
    (path / ".supertool.json").write_text(
        '{"presets": ["git"], "ops": {"git-status": {"timeout": 60},'
        ' "git-commit": {"timeout": 60}, "git-push": {"timeout": 60},'
        ' "git-diff": {"timeout": 60}}}\n'
    )
    (path / f"{name}.txt").write_text(f"{name}\n")
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", f"base {name}"], path)
    if not with_remote:
        return path
    # `-b branch` on the bare repo too: without it HEAD points at git's default
    # branch name, `git log` in the remote reads an unborn ref, and the test
    # sees an empty history whether or not the push landed.
    remote = path.parent / f"{name}-remote.git"
    _git(["init", "-q", "--bare", "-b", branch, str(remote)], path.parent)
    _git(["remote", "add", "origin", str(remote)], path)
    _git(["push", "-q", "-u", "origin", branch], path)
    return remote


def _run_op(op: str, cwd: Path, git_dir: Path | None = None):
    # Literal, not supertool.GIT_ENV_VARS: the behavioural tests below must
    # fail on behaviour, not on an AttributeError from their own harness.
    env = dict(os.environ)
    for name in EXPECTED_VARS:
        env.pop(name, None)
    if git_dir is not None:
        env["GIT_DIR"] = str(git_dir)
    return subprocess.run(
        [sys.executable, str(SUPERTOOL), op],
        cwd=str(cwd), capture_output=True, text=True, timeout=180, env=env, encoding="utf-8", errors="replace",
    )


def _subjects(repo: Path) -> list[str]:
    return _git(["log", "--format=%s"], repo).stdout.split("\n")


def _toplevel(repo: Path) -> str:
    return _git(["rev-parse", "--show-toplevel"], repo).stdout.strip()


# --------------------------------------------------------------------------
# The scrub set itself
# --------------------------------------------------------------------------

EXPECTED_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
)


def test_pinned_var_set():
    """Pinned so a variable is added or dropped deliberately, never by drift.

    The membership rule is "does it change which repository, index, or refs a
    git command reads or writes" — not "does git export it to hooks", which is
    how the #416 five were chosen. GIT_COMMON_DIR and GIT_NAMESPACE meet the
    rule and were not in that five: the first redirects config and refs for a
    worktree, the second redirects every ref a push writes.

    Deliberately absent: GIT_CEILING_DIRECTORIES and
    GIT_DISCOVERY_ACROSS_FILESYSTEM. Both only *restrict* discovery, so
    neither can land an op on a different repo — the worst they do is make it
    find none. Both are also set on purpose by people with slow network
    mounts, and scrubbing them would make supertool disagree with the user's
    own shell about whether they are in a repo at all.
    """
    assert supertool.GIT_ENV_VARS == EXPECTED_VARS


def test_op_scrub_set_covers_the_test_runner_set():
    """One lesson, one list. The op-level set must never drift below #416's."""
    assert set(conftest.GIT_ENV_VARS) <= set(supertool.GIT_ENV_VARS)


def test_scrub_removes_every_pinned_var_and_reports_them():
    env = {name: "/leaked" for name in EXPECTED_VARS}
    env["PATH"] = "/usr/bin"
    removed = supertool.scrub_git_env(env)
    assert removed == list(EXPECTED_VARS)
    assert env == {"PATH": "/usr/bin"}


def test_scrub_reports_nothing_when_the_environment_is_clean():
    env = {"PATH": "/usr/bin"}
    assert supertool.scrub_git_env(env) == []
    assert env == {"PATH": "/usr/bin"}


def test_conftest_and_ops_scrub_one_agreed_set():
    """The pytest fixture and the op launcher, in agreement.

    Two consumers of the same idea drifting apart is how #692 happened in the
    first place. There used to be a third — `.githooks/pre-push`, read out of
    the tree by this test — and the hook was removed in favour of CI as the
    only gate, so the pair is what is left to hold together.
    """
    assert conftest.GIT_ENV_VARS == supertool.GIT_ENV_VARS


# --------------------------------------------------------------------------
# The control — without this, everything below could pass vacuously
# --------------------------------------------------------------------------

def test_control_raw_git_really_does_honour_the_leak(tmp_path):
    """Raw git, same fixture, no supertool: the commit lands in the OTHER repo.

    This is the behaviour every test below asserts supertool no longer has. If
    this ever goes green-by-not-happening — two repos that are secretly one, a
    git that stopped reading GIT_DIR — the rest of this file proves nothing.
    """
    repo_a = _make_repo(tmp_path / "repoA", "a")
    repo_b = _make_repo(tmp_path / "repoB", "b")
    (repo_b / "new.txt").write_text("from B\n")

    env = dict(os.environ)
    env["GIT_DIR"] = str(repo_a / ".git")
    _git(["add", "new.txt"], repo_b, env=env)
    _git(["commit", "-q", "-m", "leaked"], repo_b, env=env)

    assert "leaked" in _subjects(repo_a), "GIT_DIR is not being honoured at all"
    assert "leaked" not in _subjects(repo_b)


# --------------------------------------------------------------------------
# git-commit
# --------------------------------------------------------------------------

def test_git_commit_under_a_leaked_git_dir_commits_to_the_cwd_repo(tmp_path):
    """The load-bearing invariant: the op acts where the caller is standing."""
    repo_a = _make_repo(tmp_path / "repoA", "a")
    repo_b = _make_repo(tmp_path / "repoB", "b")
    a_head_before = _git(["rev-parse", "HEAD"], repo_a).stdout.strip()
    (repo_b / "new.txt").write_text("from B\n")

    r = _run_op("git-commit:::landed here:::new.txt", repo_b,
                git_dir=repo_a / ".git")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "landed here" in _subjects(repo_b), r.stdout
    assert "landed here" not in _subjects(repo_a)
    assert _git(["rev-parse", "HEAD"], repo_a).stdout.strip() == a_head_before


def test_git_commit_receipt_names_the_repo_it_wrote_to(tmp_path):
    """The half the issue calls worse: git-diff said where, the writers did not."""
    repo_b = _make_repo(tmp_path / "repoB", "b")
    (repo_b / "new.txt").write_text("from B\n")

    r = _run_op("git-commit:::named:::new.txt", repo_b)

    assert r.returncode == 0, r.stdout + r.stderr
    assert f"Repo: {_toplevel(repo_b)}" in r.stdout, r.stdout


def test_git_commit_names_the_repo_even_when_the_commit_fails(tmp_path):
    """A refused commit is exactly when 'which repo?' is being asked."""
    repo_b = _make_repo(tmp_path / "repoB", "b")

    r = _run_op("git-commit:::nothing staged", repo_b)

    assert r.returncode != 0
    assert f"Repo: {_toplevel(repo_b)}" in r.stdout, r.stdout


def test_the_scrub_is_reported_not_silent(tmp_path):
    """Scrubbing without a trace hides that the caller leaked (#416's wording).

    Silence would trade a loud failure for a quiet one in the other direction:
    a caller who set GIT_DIR on purpose would watch the op ignore it and be
    told nothing. The op still refuses to honour it — a redirect that survives
    into `git push` is not recoverable — but it says so.
    """
    repo_a = _make_repo(tmp_path / "repoA", "a")
    repo_b = _make_repo(tmp_path / "repoB", "b")
    (repo_b / "new.txt").write_text("from B\n")

    r = _run_op("git-commit:::reported:::new.txt", repo_b,
                git_dir=repo_a / ".git")

    assert "scrubbed inherited git env" in r.stdout, r.stdout
    assert "GIT_DIR" in r.stdout


def test_a_clean_environment_produces_no_scrub_notice(tmp_path):
    """No leak, no line. The notice has to mean something when it appears."""
    repo_b = _make_repo(tmp_path / "repoB", "b")
    (repo_b / "new.txt").write_text("from B\n")

    r = _run_op("git-commit:::quiet:::new.txt", repo_b)

    assert "scrubbed inherited git env" not in r.stdout, r.stdout


# --------------------------------------------------------------------------
# git-push — the op that acts outward, and the hardest to undo
# --------------------------------------------------------------------------

def test_git_push_under_a_leaked_git_dir_pushes_the_cwd_repo(tmp_path):
    remote_a = _make_repo(tmp_path / "repoA", "a", with_remote=True)
    remote_b = _make_repo(tmp_path / "repoB", "b", with_remote=True)
    repo_a, repo_b = tmp_path / "repoA", tmp_path / "repoB"
    (repo_b / "new.txt").write_text("from B\n")
    _git(["add", "new.txt"], repo_b)
    _git(["commit", "-q", "-m", "pushed from B"], repo_b)
    a_remote_head = _git(["rev-parse", "main"], remote_a).stdout.strip()

    r = _run_op("git-push", repo_b, git_dir=repo_a / ".git")

    assert "pushed from B" in _subjects(remote_b), r.stdout + r.stderr
    assert _git(["rev-parse", "main"], remote_a).stdout.strip() == a_remote_head


def test_git_push_receipt_names_the_repo_it_pushed_from(tmp_path):
    _make_repo(tmp_path / "repoB", "b", with_remote=True)
    repo_b = tmp_path / "repoB"
    (repo_b / "new.txt").write_text("from B\n")
    _git(["add", "new.txt"], repo_b)
    _git(["commit", "-q", "-m", "named push"], repo_b)

    r = _run_op("git-push", repo_b)

    assert f"Repo: {_toplevel(repo_b)}" in r.stdout, r.stdout + r.stderr


# --------------------------------------------------------------------------
# Not just the two ops that were named
# --------------------------------------------------------------------------

def test_the_scrub_protects_a_preset_that_never_used_the_shared_helper(tmp_path):
    """`git-status` defines its own `_git`, as eight other presets do.

    Fixing `presets/git/_git_common.py` would have protected the two presets
    that import its `_git` and silently missed the nine that shadow it —
    this repo's signature defect arriving inside the fix for it. The scrub
    lives at the launcher instead, so a preset is covered by being launched,
    not by remembering to opt in.

    The two repos are distinguished by BRANCH NAME rather than by an untracked
    file: `GIT_DIR` alone leaves cwd as the work tree, so repoB's untracked
    files show up either way and asserting on one would pass on the broken
    behaviour. The branch comes from the leaked git dir and nowhere else.
    """
    repo_a = _make_repo(tmp_path / "repoA", "a", branch="branch-of-repo-a")
    repo_b = _make_repo(tmp_path / "repoB", "b", branch="branch-of-repo-b")

    r = _run_op("git-status", repo_b, git_dir=repo_a / ".git")

    assert "branch-of-repo-b" in r.stdout, r.stdout + r.stderr
    assert "branch-of-repo-a" not in r.stdout
