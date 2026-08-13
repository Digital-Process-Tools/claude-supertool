"""Every `setup-python` step in `tests.yml` caches pip, honestly (#890).

Fifteen-plus legs re-ran `pip install pytest pytest-cov pytest-xdist
pytest-timeout ruff` (and, on top of that, `pip install -e .`) from cold on
every push. `actions/setup-python`'s built-in `cache: 'pip'` fixes that, but
its default dependency-file glob is `**/requirements.txt` and this repo ships
none — a bare `cache: 'pip'` fails the step outright with "No file matched to
[**/requirements.txt]", redding the whole board.

So `cache-dependency-path` must point at a file that (a) exists and (b) is
the actual source of the installed dependency set — otherwise the cache key
never changes when the real list does, which is a stale-cache bug dressed up
as a fix. `pyproject.toml` is honest here only because every install step was
rewritten to install from it (`pip install -e .[dev]`) rather than from a
hand-typed package list that could drift out of sync with it unnoticed.
"""

from __future__ import annotations

from pathlib import Path

from _workflow_parse import job_blocks, job_steps, run_blocks

REPO = Path(__file__).resolve().parents[1]

_ALL_JOBS = ("pytest", "coverage", "notifiers", "lint-new")


def _setup_python_steps(job_name: str) -> list:
    steps = [s for s in job_steps(job_blocks()[job_name])
              if s.uses.startswith("actions/setup-python")]
    assert steps, (
        f"job `{job_name}` has no actions/setup-python step — either the "
        "workflow moved or the parser broke, and every guard below is now "
        "checking nothing")
    return steps


def test_the_job_discovery_still_finds_every_job() -> None:
    assert set(job_blocks()) == set(_ALL_JOBS), (
        "the set of CI jobs changed; this file's job list needs updating too")


def test_every_setup_python_step_declares_pip_cache() -> None:
    for job_name in _ALL_JOBS:
        for step in _setup_python_steps(job_name):
            assert step.with_.get("cache") == "pip", (
                f"job `{job_name}`'s setup-python step does not cache pip "
                f"(with: {step.with_})")


def test_every_cache_dependency_path_resolves_to_a_real_file() -> None:
    """A bare `cache: 'pip'` fails outright: this repo has no requirements.txt
    for the action's default `**/requirements.txt` glob to match."""
    for job_name in _ALL_JOBS:
        for step in _setup_python_steps(job_name):
            path = step.with_.get("cache-dependency-path")
            assert path, (
                f"job `{job_name}`'s setup-python step caches pip but sets no "
                "cache-dependency-path — it will fall back to the default "
                "**/requirements.txt glob, which this repo has no file for, "
                "and the step will fail outright")
            assert (REPO / path).is_file(), (
                f"job `{job_name}`'s cache-dependency-path {path!r} does not "
                "resolve to a real file in the repo")


def test_the_hashed_file_is_the_real_source_of_the_installed_deps() -> None:
    """The honesty check: hashing pyproject.toml is only a valid cache key if
    installation actually reads its dependency set, not a hand-typed list
    that can drift out of sync with it unnoticed."""
    for job_name in _ALL_JOBS:
        for step in _setup_python_steps(job_name):
            path = step.with_.get("cache-dependency-path")
            if path != "pyproject.toml":
                continue
            block = job_blocks()[job_name]
            runs = "\n".join(run_blocks(job_steps(block)))
            assert ".[dev]" in runs, (
                f"job `{job_name}` hashes pyproject.toml for its pip cache "
                "key but does not install from its [dev] extra anywhere in "
                "the job — the cache key would not change when the real "
                "install list does")


def test_the_notifiers_job_caches_the_bun_install() -> None:
    """`bun install --frozen-lockfile` reads a real, committed lockfile
    (notifiers/claude-channel/bun.lock), so caching it on the lockfile hash
    is honest the same way the pip cache is."""
    steps = job_steps(job_blocks()["notifiers"])
    cache_steps = [s for s in steps if s.uses.startswith("actions/cache")]
    assert cache_steps, (
        "the notifiers job installs bun deps via `bun install "
        "--frozen-lockfile` but caches nothing for it")
    assert any(
        "notifiers/claude-channel/bun.lock" in s.with_.get("key", "")
        for s in cache_steps
    ), (
        "no actions/cache step in the notifiers job keys on "
        "notifiers/claude-channel/bun.lock — a cache not keyed on the "
        "lockfile would serve stale deps across a dependency bump")
