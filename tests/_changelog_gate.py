"""Run `.github/workflows/changelog.yml`'s fragment gate against a real repo.

The gate is a bash `run:` block. It is extracted **structurally** (#731) rather
than grepped, so the two-thirds of that file which is comments cannot satisfy an
assertion, and it is executed -- every caller reads back an exit status and a
receipt from the actual shell, which no comment can produce.

`tests/test_changelog_fragment_deletion_925.py` grew the first copy of this and
still carries its own; this module exists because #1722 needed the same harness
plus a stub `gh`, and a second hand-rolled copy of a structural extractor is how
one of them silently stops finding the step.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "changelog.yml"


def workflow_jobs() -> dict[str, str]:
    from _workflow_parse import job_blocks

    blocks = job_blocks(WORKFLOW.read_text(encoding="utf-8"))
    assert blocks, "parsed no jobs -- a parser finding nothing renders this file green"
    return blocks


def gate_step():
    """The one step named for the fragment gate, as a parsed `Step`."""
    from _workflow_parse import job_steps

    steps = [step for block in workflow_jobs().values() for step in job_steps(block)]
    assert steps, "parsed no steps"
    gates = [s for s in steps if "carries a fragment" in s.name]
    assert len(gates) == 1, (
        "expected exactly one step named for the fragment gate, got "
        + repr([s.name for s in steps])
    )
    assert "changelog.d" in gates[0].run, (
        "the extracted step is not the fragment gate: " + repr(gates[0].run[:200]))
    return gates[0]


def gate_script() -> str:
    return gate_step().run


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True,
                   encoding="utf-8", errors="replace")


def base_repo(tmp_path: Path) -> Path:
    """A repo whose `origin/master` holds one pending fragment."""
    repo = tmp_path / "repo"
    (repo / "changelog.d").mkdir(parents=True)
    (repo / "_supertool.py").write_text('VERSION = "0.44.0"\n', encoding="utf-8")
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


def touch_core(repo: Path) -> None:
    (repo / "_supertool.py").write_text(
        'VERSION = "0.44.0"\n# changed\n', encoding="utf-8")


def gh_stub(tmp_path: Path, *, labels=None, exit_code: int = 0) -> Path:
    """A directory holding a fake `gh` that answers the label read.

    `labels=None, exit_code=1` is the failure arm -- an outage, a missing
    binary, a token without the scope. The stub appends its argv to
    `<dir>/calls.txt`, so a test can tell "the gate asked and got an answer"
    apart from "the gate never asked", which are the two shapes that otherwise
    render identically.
    """
    bindir = tmp_path / "stub-bin"
    bindir.mkdir(exist_ok=True)
    body = ["#!/bin/sh", 'echo "$*" >> "$(dirname "$0")/calls.txt"']
    for label in (labels or []):
        body.append("echo " + shlex.quote(str(label)))
    body.append("exit %d" % exit_code)
    script = bindir / "gh"
    script.write_text("\n".join(body) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return bindir


def gh_calls(bindir: Path) -> list[str]:
    log = bindir / "calls.txt"
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_gate(repo: Path, labels: str = "", *, pr_number: str = "",
             gh_repo: str = "", stub_bin: Path | None = None
             ) -> "subprocess.CompletedProcess[str]":
    """Run the gate. `stub_bin` is prepended to PATH so `gh` resolves to it."""
    path = os.environ["PATH"]
    if stub_bin is not None:
        path = str(stub_bin) + os.pathsep + path
    env = {"PATH": path, "BASE": "master", "LABELS": labels}
    if pr_number:
        env["PR_NUMBER"] = pr_number
    if gh_repo:
        env["GH_REPO"] = gh_repo
    return subprocess.run(
        ["bash", "-c", gate_script()],
        cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
