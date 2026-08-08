"""The "carries a fragment" gate is satisfied by *deleting* one (#925).

`.github/workflows/changelog.yml` reads the PR's fragment state out of
`git diff --name-only`, and that lists a deletion identically to an addition.
So a PR that changes the core and *removes* somebody else's pending fragment
passed green, announced nothing, and dropped an approved entry from the next
release. The gate reported that a fragment was touched, which is true and is
not what the line it prints means.

These run the workflow's own `run:` block -- extracted structurally, never
grepped (#731) -- against real git repositories built per test. An assertion
phrased against the exit status of the actual shell cannot be satisfied by the
comment above it, and none of these would pass if the block did nothing: each
one builds a specific diff shape and reads the status and the receipt back.

The release shape is pinned here too, and deliberately. The one-flag fix the
issue proposes (`--diff-filter=AM` on the single diff) reddens **every release
PR**: a cut modifies `_supertool.py`, writes `CHANGELOG.md`, and deletes the
fragments it consumed, so it adds no fragment at all. `bc0c3b0` (v0.29.0) is
exactly that shape.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "changelog.yml"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the gate is a bash `run:` block on ubuntu-latest; there is no "
           "Windows leg of it to be honest about",
)


def gate_script() -> str:
    """The fragment step's `run:` body, read out of the workflow's structure."""
    from _workflow_parse import job_blocks, job_steps

    blocks = job_blocks(WORKFLOW.read_text(encoding="utf-8"))
    assert blocks, "parsed no jobs -- a parser finding nothing renders this file green"
    steps = [step for block in blocks.values() for step in job_steps(block)]
    assert steps, "parsed no steps"
    gates = [s for s in steps if "carries a fragment" in s.name]
    assert len(gates) == 1, (
        "expected exactly one step named for the fragment gate, got "
        + repr([s.name for s in steps])
    )
    body = gates[0].run
    assert "changelog.d" in body, (
        "the extracted step is not the fragment gate: " + repr(body[:200]))
    return body


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


def base_repo(tmp_path: Path) -> Path:
    """A repo whose `origin/master` holds one pending fragment."""
    repo = tmp_path / "repo"
    (repo / "changelog.d").mkdir(parents=True)
    (repo / "_supertool.py").write_text('VERSION = "0.29.0"\n', encoding="utf-8")
    (repo / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    (repo / "changelog.d" / "906.added.md").write_text(
        "- **Somebody else's approved entry** ([#906](x)). Body.\n", encoding="utf-8")
    git(repo, "init", "-b", "master")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base")
    git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    git(repo, "checkout", "-q", "-b", "pr")
    return repo


def run_gate(repo: Path, labels: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["bash", "-c", gate_script()],
        cwd=repo, capture_output=True, text=True,
        env={"PATH": os.environ["PATH"], "BASE": "master", "LABELS": labels},
    )


def touch_core(repo: Path) -> None:
    (repo / "_supertool.py").write_text(
        'VERSION = "0.29.0"\n# changed\n', encoding="utf-8")


# ---------------------------------------------------------------------------
# The bypass
# ---------------------------------------------------------------------------

def test_deleting_someone_elses_fragment_does_not_satisfy_the_gate(tmp_path) -> None:
    repo = base_repo(tmp_path)
    touch_core(repo)
    git(repo, "rm", "-q", "changelog.d/906.added.md")
    git(repo, "commit", "-am", "core change, and drop a pending fragment")

    res = run_gate(repo)

    assert res.returncode != 0, (
        "a user-visible change whose only fragment line is a DELETION passed "
        "the gate:\n" + res.stdout + res.stderr)
    assert "finding" in res.stdout, res.stdout + res.stderr


def test_the_receipt_names_the_deletion_rather_than_the_absence(tmp_path) -> None:
    """`no fragment` is the wrong sentence for a PR that removed one."""
    repo = base_repo(tmp_path)
    touch_core(repo)
    git(repo, "rm", "-q", "changelog.d/906.added.md")
    git(repo, "commit", "-am", "core change, and drop a pending fragment")

    res = run_gate(repo)
    out = res.stdout

    assert res.returncode != 0, out + res.stderr
    assert "delet" in out.lower(), (
        "the receipt calls a removal an absence: " + out)
    assert "906.added.md" in out, (
        "the receipt must name the fragment that was removed, not just report "
        "that none was added:\n" + out)


def test_a_pr_that_adds_one_and_deletes_another_is_still_a_finding(tmp_path) -> None:
    """The added fragment is not a licence to drop an unrelated one."""
    repo = base_repo(tmp_path)
    touch_core(repo)
    (repo / "changelog.d" / "925.fixed.md").write_text(
        "- **Mine** ([#925](x)). Body.\n", encoding="utf-8")
    git(repo, "rm", "-q", "changelog.d/906.added.md")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "add mine, drop theirs")

    res = run_gate(repo)

    assert res.returncode != 0, (
        "a PR carrying its own fragment silently removed another PR's:\n"
        + res.stdout + res.stderr)
    assert "906.added.md" in res.stdout, res.stdout


# ---------------------------------------------------------------------------
# What must keep passing
# ---------------------------------------------------------------------------

def test_a_release_cut_is_not_a_finding(tmp_path) -> None:
    """A release consumes fragments into CHANGELOG.md and adds none.

    This is the shape the issue's one-flag fix breaks, and it is not
    hypothetical: `bc0c3b0` modified `_supertool.py` and `CHANGELOG.md` and
    deleted three fragments.
    """
    repo = base_repo(tmp_path)
    touch_core(repo)
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [0.30.0]\n\n- **Somebody else's entry** ([#906](x)).\n",
        encoding="utf-8")
    git(repo, "rm", "-q", "changelog.d/906.added.md")
    git(repo, "commit", "-am", "release: 0.30.0")

    res = run_gate(repo)

    assert res.returncode == 0, (
        "the release cut was blocked by the fragment gate:\n"
        + res.stdout + res.stderr)
    assert "ok" in res.stdout, res.stdout


def test_adding_a_fragment_still_passes(tmp_path) -> None:
    repo = base_repo(tmp_path)
    touch_core(repo)
    (repo / "changelog.d" / "925.fixed.md").write_text(
        "- **Mine** ([#925](x)). Body.\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "core change with a fragment")

    res = run_gate(repo)

    assert res.returncode == 0, res.stdout + res.stderr
    assert "ok" in res.stdout and "925.fixed.md" in res.stdout, res.stdout


def test_a_docs_only_pr_is_still_skipped(tmp_path) -> None:
    repo = base_repo(tmp_path)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "docs")

    res = run_gate(repo)

    assert res.returncode == 0, res.stdout + res.stderr
    assert "skipped" in res.stdout, res.stdout


def test_the_no_changelog_label_still_wins(tmp_path) -> None:
    repo = base_repo(tmp_path)
    touch_core(repo)
    git(repo, "rm", "-q", "changelog.d/906.added.md")
    git(repo, "commit", "-am", "core change, drop a fragment, labelled")

    res = run_gate(repo, labels="no-changelog")

    assert res.returncode == 0, res.stdout + res.stderr
    assert "skipped" in res.stdout, res.stdout
