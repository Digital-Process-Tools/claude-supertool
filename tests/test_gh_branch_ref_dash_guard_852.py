"""#852 — `gh-branch` refuses a `-`-leading ref at the door, not by accident of ordering.

There was no exploit here and the issue says so. `main()` took `args[0]` with no
validation and handed it to `gh run list --branch <ref>`, where a value like
`--output` is a flag rather than a branch — but `_head_commit(ref)` runs first,
puts the ref in a **URL path** (`repos/{o}/{r}/commits/<ref>`) where a leading
dash is not a flag, gets a 404 because git refs cannot begin with one, and
returns 1. Nothing reached the vulnerable argv.

**The thing keeping it safe was the call order**, which is invisible at the sink
and which any future edit can reorder without knowing it was load-bearing. Every
other ref site in the repo — `git/checkout.py:80`, `git/merge.py:140`,
`_git_common.py:142`, `gitlab/mr.py`'s `_ORDINARY_REF` — carries the guard that
`fix/818-git-arg-injection` established; a new file dropped it.

Guard, rather than a test pinning the ordering, on two grounds. A repo-wide
invariant enforced at every site is one a reader can check locally, and this one
already has four other sites agreeing; and pinning the ordering would make
`_head_commit`-runs-first a documented requirement of the security model, which
is a heavier thing to owe a future refactor than four lines of validation. The
test below asserts the *observable* property that follows — nothing carrying a
leading dash is handed to `gh` at all — so it fails on both a removed guard and
a reordering that would have made the guard the only defence.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PRESETS = Path(__file__).parent.parent / "presets"


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PRESETS / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


branch = _load("github/branch.py", "github_branch_852")


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _run(monkeypatch, capsys, ref: str) -> tuple[int, str, list[list[str]]]:
    """Drive the op with a faked `gh`, returning every argv it was handed."""
    seen: list[list[str]] = []

    def fake(cmd, *a, **kw):
        argv = [str(x) for x in cmd]
        seen.append(argv)
        if argv[:3] == ["gh", "repo", "view"]:
            return _Completed(json.dumps({
                "nameWithOwner": "o/r",
                "defaultBranchRef": {"name": "master"}}))
        if argv[:2] == ["gh", "api"]:
            return _Completed("", 1, "gh: Not Found (HTTP 404)")
        return _Completed("[]")

    monkeypatch.setattr(branch.subprocess, "run", fake)
    monkeypatch.setattr(sys, "argv", ["branch.py", ref])
    rc = branch.main()
    return rc, capsys.readouterr().out, seen


@pytest.mark.parametrize("ref", [
    "--output=/tmp/x",          # a `gh` flag wearing a branch's clothes
    "-b",                       # short flags are the ones quoting does not stop
    "--limit",                  # would eat the next argv element as its value
    "-",                        # legitimate to `git checkout`, meaningless here
])
def test_a_dash_leading_ref_never_reaches_gh(monkeypatch, capsys, ref) -> None:
    """Refused, and refused before the ref is put in front of `gh` in any form.

    The assertion is on the argv `gh` was handed, not on the exit code: an op
    that printed the error *after* calling `gh api commits/--output=/tmp/x`
    would satisfy a returncode check while leaving the property this guards.
    """
    rc, out, seen = _run(monkeypatch, capsys, ref)
    assert rc == 1
    assert "refusing" in out.lower()
    assert ref in out
    for argv in seen:
        assert ref not in argv, f"the ref reached gh: {argv!r}"
    # `gh repo view` takes no ref and is what resolves the default branch, so
    # it is the only call this path may make.
    assert all(argv[:3] == ["gh", "repo", "view"] for argv in seen), seen


def test_an_ordinary_ref_is_unaffected(monkeypatch, capsys) -> None:
    """The guard is a prefix test, not a character allowlist.

    `release/0.24.0`, `fix/818-git-arg-injection` and every other real branch
    name here contains slashes, dots and digits. A tighter pattern would refuse
    working input, which is its own defect — this op's whole job is answering
    for the branch you just merged.
    """
    rc, out, seen = _run(monkeypatch, capsys, "fix/851-untrusted-check-branch")
    assert "refusing" not in out.lower()
    # It got as far as resolving the head commit — which the fake 404s, so the
    # op declines for that reason instead. The point is that it was asked.
    assert any(argv[:2] == ["gh", "api"] for argv in seen), seen
    assert rc == 1
