"""#2205 -- git-commit needs a hook-bypass route for a hook that blocks or stalls.

A 3700-file merge commit died on `git-commit`'s 30s hook timeout. The
receipt's own advice -- `git commit --no-verify -m '...'` -- is exactly what
the raw-command guard refuses, and there was no route through supertool to
the bypass the receipt itself recommended. Same posture as `git-push`'s
`:no-verify` (presets/git/push.py): an explicit, opt-in flag, refused by
being absent -- not honoured unless the caller spells it out.

Two paired tests, not one: a hook that BLOCKS must be bypassable with the
flag (`test_no_verify_flag_bypasses_a_blocking_hook`), and an ordinary
commit with the same hook installed must still be blocked by it when the
flag is absent (`test_ordinary_commit_still_runs_the_hook`) -- the flag must
not become the default the way #647's `:no-verifyy` typo silently did on
`git-push`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SUPERTOOL = REPO / "supertool.py"
COAUTHOR = "Test Bot <bot@example.invalid>"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX shebang pre-commit hook")


def _repo(tmp_path: Path) -> Path:
    """Throwaway repo, one seed commit, a pre-commit hook that always fails."""
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=work, check=True)
    (work / ".supertool.json").write_text('{"presets": ["git"]}\n', encoding="utf-8")
    (work / "a.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=work, check=True)

    hooks = work / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'lint failed' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    (work / "b.txt").write_text("2\n", encoding="utf-8")
    subprocess.run(["git", "add", "b.txt"], cwd=work, check=True)
    return work


def _run(args: list, cwd: Path, stdin: str = "") -> tuple:
    env = dict(os.environ)
    env["SUPERTOOL_COAUTHOR"] = COAUTHOR
    proc = subprocess.run(
        [sys.executable, str(SUPERTOOL), *args],
        input=stdin, capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace", cwd=str(cwd), env=env,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _head_sha(work: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=work,
        capture_output=True, text=True, check=True, encoding="utf-8",
    ).stdout.strip()


# --- must NOT fire: an ordinary commit keeps running hooks -------------------


def test_ordinary_commit_still_runs_the_hook_and_is_blocked(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    before = _head_sha(work)

    code, out = _run(["git-commit:::add b"], cwd=work)

    assert _head_sha(work) == before, out  # nothing landed
    assert "lint failed" in out, out
    assert "HOOKS SKIPPED" not in out, out


# --- must fire: the flag gets a blocked commit through ------------------------


def test_no_verify_flag_bypasses_a_blocking_hook(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    before = _head_sha(work)

    code, out = _run(["git-commit:::add b:::--no-verify"], cwd=work)

    assert _head_sha(work) != before, out  # the commit landed
    assert code == 0, out
    assert "HOOKS SKIPPED" in out, out


def test_no_verify_payload_route_also_bypasses(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    before = _head_sha(work)

    payload = 'message = "add b"\nno_verify = true\n'
    code, out = _run(["git-commit:@-"], cwd=work, stdin=payload)

    assert _head_sha(work) != before, out
    assert code == 0, out
    assert "HOOKS SKIPPED" in out, out


def test_no_verify_payload_false_still_runs_the_hook(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    before = _head_sha(work)

    payload = 'message = "add b"\nno_verify = false\n'
    code, out = _run(["git-commit:@-"], cwd=work, stdin=payload)

    assert _head_sha(work) == before, out
    assert "HOOKS SKIPPED" not in out, out
