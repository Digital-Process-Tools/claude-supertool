"""Git's hook-exported environment must never reach the test suite (#416).

Git exports GIT_DIR (and friends) to every hook it runs. `.githooks/pre-push`
runs pytest, so without a scrub the whole suite inherits a GIT_DIR pointing at
the real repository: every test that shells out to git then operates on *this*
repo instead of its own tmp_path fixture. Observed once, in one push: two
fixture commits titled `init` stacked on master, a tree holding a single
`f.txt`, core.bare flipped to true, index desynced from ref and worktree.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import conftest

SUITE_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)

_ID = ["-c", "user.email=fixture@example.invalid", "-c", "user.name=fixture"]

INNER_TEST = '''
import subprocess

GIT = ["git", "-c", "user.email=f@example.invalid", "-c", "user.name=f"]


def test_fixture_repo_keeps_its_own_commits(tmp_path):
    (tmp_path / "f.txt").write_text("x")
    for args in (["init", "-q"], ["add", "f.txt"], ["commit", "-q", "-m", "init"]):
        r = subprocess.run(GIT + args, cwd=str(tmp_path), capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    assert (tmp_path / ".git").exists(), "git init did not create the fixture repo"
    log = subprocess.run(
        GIT + ["log", "--oneline"], cwd=str(tmp_path), capture_output=True, text=True
    )
    assert "init" in log.stdout
'''


def _git(args, cwd, env=None):
    return subprocess.run(
        ["git", *_ID, *args], cwd=str(cwd), env=env, capture_output=True, text=True
    )


def _make_outer_repo(path):
    path.mkdir(parents=True)
    _git(["init", "-q"], path)
    _git(["commit", "-q", "--allow-empty", "-m", "outer base"], path)
    return _git(["rev-parse", "HEAD"], path).stdout.strip()


def _build_project(root, with_conftest):
    tests_dir = root / "tests"
    tests_dir.mkdir(parents=True)
    target = root / "supertool.py"
    try:
        target.symlink_to(SUITE_ROOT / "supertool.py")
    except OSError:
        shutil.copy(SUITE_ROOT / "supertool.py", target)
    if with_conftest:
        shutil.copy(SUITE_ROOT / "tests" / "conftest.py", tests_dir / "conftest.py")
    (tests_dir / "test_leak.py").write_text(INNER_TEST)
    return tests_dir / "test_leak.py"


def _run_pytest_with_leaked_git_dir(project, target, git_dir):
    env = dict(os.environ)
    env["GIT_DIR"] = str(git_dir)
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(target), "--no-cov",
         "-p", "no:cacheprovider"],
        cwd=str(project), capture_output=True, text=True, env=env,
    )


def test_pinned_var_set():
    assert conftest.GIT_ENV_VARS == EXPECTED_VARS


def test_scrub_removes_every_pinned_var_from_a_mapping():
    env = {name: "/leaked" for name in EXPECTED_VARS}
    env["PATH"] = "/usr/bin"
    removed = conftest.scrub_git_env(env)
    assert removed == list(EXPECTED_VARS)
    assert env == {"PATH": "/usr/bin"}


def test_scrub_defaults_to_os_environ(monkeypatch):
    for name in EXPECTED_VARS:
        monkeypatch.setenv(name, "/leaked")
    removed = conftest.scrub_git_env()
    assert removed == list(EXPECTED_VARS)
    assert [n for n in EXPECTED_VARS if n in os.environ] == []


def test_scrub_reports_nothing_when_the_environment_is_clean():
    assert conftest.scrub_git_env({"PATH": "/usr/bin"}) == []


def test_scrub_fixture_runs_for_every_test(request):
    assert "_scrub_git_env" in request.fixturenames
    assert [n for n in EXPECTED_VARS if n in os.environ] == []


def test_a_leak_left_by_a_previous_test_is_gone():
    """Deliberately leak; the pair below asserts the autouse fixture cleaned it.

    Order-dependent by design (pytest runs a file top to bottom). Under xdist
    the two may land on different workers, which only makes the pair weaker,
    never falsely red — the subprocess test is the order-independent proof.
    """
    os.environ["GIT_DIR"] = "/leaked/by/the/previous/test"


def test_the_deliberate_leak_did_not_survive():
    assert "GIT_DIR" not in os.environ


def test_conftest_and_hook_scrub_the_same_variables():
    hook = (SUITE_ROOT / ".githooks" / "pre-push").read_text()
    unset = [line for line in hook.splitlines() if line.startswith("unset ")]
    assert len(unset) == 1, "pre-push should scrub in exactly one unset line"
    assert unset[0].split()[1:] == list(conftest.GIT_ENV_VARS)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell hook")
def test_pre_push_hook_scrubs_before_invoking_pytest(tmp_path):
    repo = tmp_path / "repo"
    _make_outer_repo(repo)
    shutil.copy(SUITE_ROOT / ".githooks" / "pre-push", repo / "pre-push")
    stub = repo / "fake-python"
    stub.write_text(
        "#!/bin/sh\n"
        'echo "SEEN GIT_DIR=[${GIT_DIR-<unset>}]"\n'
        'echo "SEEN GIT_INDEX_FILE=[${GIT_INDEX_FILE-<unset>}]"\n'
        'echo "SEEN GIT_WORK_TREE=[${GIT_WORK_TREE-<unset>}]"\n'
        'echo "SEEN GIT_OBJECT_DIRECTORY=[${GIT_OBJECT_DIRECTORY-<unset>}]"\n'
        'echo "SEEN GIT_ALTERNATE_OBJECT_DIRECTORIES='
        '[${GIT_ALTERNATE_OBJECT_DIRECTORIES-<unset>}]"\n'
        "exit 0\n"
    )
    stub.chmod(0o755)

    env = dict(os.environ)
    env["PYTHON"] = str(stub)
    for name in EXPECTED_VARS:
        env[name] = str(repo / ".git")
    result = subprocess.run(
        ["bash", "pre-push"], cwd=str(repo), capture_output=True, text=True, env=env
    )

    assert result.returncode == 0, result.stderr
    seen = [line for line in result.stdout.splitlines() if line.startswith("SEEN ")]
    assert len(seen) == len(EXPECTED_VARS)
    assert all(line.endswith("=[<unset>]") for line in seen), result.stdout
    assert "scrubbed inherited git env" in result.stdout


def test_leaked_git_dir_never_reaches_a_fixture_repo(tmp_path):
    """The load-bearing invariant: fixture commits land in the fixture repo.

    A pytest run started with GIT_DIR pointing at an outer repo must leave that
    repo untouched, and its fixture repos must own their own commits.
    """
    outer = tmp_path / "outer"
    head_before = _make_outer_repo(outer)
    project = tmp_path / "project"
    target = _build_project(project, with_conftest=True)

    result = _run_pytest_with_leaked_git_dir(project, target, outer / ".git")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "scrubbed inherited git env" in result.stdout
    assert _git(["rev-parse", "HEAD"], outer).stdout.strip() == head_before
    assert "init" not in _git(["log", "--oneline"], outer).stdout
    assert _git(["config", "--get", "core.bare"], outer).stdout.strip() == "false"
    assert _git(["status", "--porcelain"], outer).returncode == 0


def test_control_the_same_run_without_the_scrub_corrupts_the_outer_repo(tmp_path):
    """Negative control: prove the test above can fail.

    Identical setup minus tests/conftest.py — i.e. the pre-fix world. The
    fixture's commits land on the outer repo's HEAD. If this ever goes green,
    the invariant test above has stopped proving anything.
    """
    outer = tmp_path / "outer"
    head_before = _make_outer_repo(outer)
    project = tmp_path / "project"
    target = _build_project(project, with_conftest=False)

    result = _run_pytest_with_leaked_git_dir(project, target, outer / ".git")

    assert result.returncode != 0, "the fixture repo should not have been created"
    assert _git(["rev-parse", "HEAD"], outer).stdout.strip() != head_before
    assert "init" in _git(["log", "--oneline"], outer).stdout
