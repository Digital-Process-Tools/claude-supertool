"""git-status flattened an MR target branch and then built a git ref from it.

#977 (found by the v0.26.0 audit). `presets/git/status.py::main` did:

    mr_target = _untrusted.flat(str(mr.get("target_branch", "?")))
    ...
    target_ref = f"origin/{mr_target}" if mr_target != "?" else ""
    _git(["diff", "--shortstat", f"{target_ref}...HEAD"], timeout=3)

`flat()` is byte-identical for ordinary names, so this was inert until a
target branch carried U+2028 or U+2029 -- both accepted by `git
check-ref-format` (#1654) -- at which point `flat()` disclosed the separator
as `[U+2028]`/`[U+2029]` instead of passing it through, the ref built from
that mangled name did not exist, `git diff --shortstat` failed, and the
`(+a -d)` figure was silently dropped with no error shown anywhere.

The fix is the pattern `presets/github/job.py` already uses for
`pr_branch`/`_local_branch_check`: keep the raw name for the functional
consumer (the ref), flatten only at the print. Two positive controls, per
the brief -- an ordinary branch must still work exactly as before, and the
hostile one must be refused as a ref (the diff call answers about a ref that
does not exist) while still being disclosed, not silently dropped, in the
`Target:` line.
"""
from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "presets"))

#: Accepted by `git check-ref-format`, breaks `str.splitlines()` (#1654/#886).
SEP = chr(0x2028)


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


status = _load("presets/git/status.py", "git_status_977")


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git"], 0, stdout, stderr)


def _dead(rc: int = 1, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git"], rc, "", stderr)


def _render(fn) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    return buf.getvalue()


def _status_out(monkeypatch, target_branch: str, seen: list) -> str:
    """Drive `status.main()` with one MR whose target is `target_branch`.

    Every other git call answers with the plain, uneventful shape so the MR
    section is the only thing under test.
    """
    def fake(args, timeout=None):
        seen.append(list(args))
        head = args[0] if args else ""
        if head == "for-each-ref":
            return _ok("")
        if head == "log":
            return _ok("abc1234 2026-08-22 me | subject" + chr(10))
        if head == "stash":
            return _ok("")
        if head == "status":
            return _ok("")
        if head == "branch":
            return _ok("* feature abc1234 [origin/feature] subject" + chr(10))
        if head == "rev-parse":
            if "--abbrev-ref" in args:
                return _ok("feature" + chr(10))
            return _dead()
        if head == "rev-list":
            return _ok("0" + chr(9) + "0" + chr(10))
        if head == "diff" and "--shortstat" in args:
            # The double stands in for git: an ordinary ref answers, a ref
            # built from a mangled name (the `[U+2028]` disclosure `flat()`
            # would have substituted for the separator) does not exist and
            # git refuses.
            ref_arg = args[-1]
            target = ref_arg.split("...", 1)[0]
            if target == f"origin/{target_branch}":
                return _ok(" 1 file changed, 2 insertions(+), 1 deletion(-)" + chr(10))
            return _dead(128, f"fatal: bad revision '{ref_arg}'")
        return _ok("")

    monkeypatch.setattr(status, "_spawn_git", fake)
    monkeypatch.setattr(
        status, "_hosted_request",
        lambda cmd: {
            "iid": 1, "title": "Fix", "state": "opened",
            "target_branch": target_branch, "changes_count": "1",
        } if cmd[:2] == ["glab", "mr"] else None,
    )
    monkeypatch.setattr(sys, "argv", ["status.py"])
    return _render(status.main)


def test_ordinary_target_branch_still_builds_a_working_ref(monkeypatch) -> None:
    """Positive control: an unremarkable target branch must not regress."""
    seen: list = []
    out = _status_out(monkeypatch, "master", seen)

    diffs = [a for a in seen if a and a[0] == "diff" and "--shortstat" in a]
    assert diffs, "no `git diff --shortstat` call was made"
    assert diffs[-1][-1] == "origin/master...HEAD", diffs[-1]
    assert "Target: master" in out, out
    assert "(+2 -1)" in out, (
        "the shortstat figure did not reach the render: " + out)


def test_a_target_branch_carrying_u2028_still_reaches_the_ref_raw(monkeypatch) -> None:
    """The bug: `flat()` used to feed the ref, silently dropping the figure.

    `check-ref-format` accepts U+2028 (#1654); the double above answers only
    when the *raw* name reaches the ref unmangled. Before the fix, `flat()`
    had already disclosed the separator as `[U+2028]` by the time the ref
    was built, so the double's `_dead()` branch fired and `(+a -d)` never
    appeared -- silently, with no error line anywhere in the render.
    """
    hostile_target = "release" + SEP + "train"
    seen: list = []
    out = _status_out(monkeypatch, hostile_target, seen)

    diffs = [a for a in seen if a and a[0] == "diff" and "--shortstat" in a]
    assert diffs, "no `git diff --shortstat` call was made"
    assert diffs[-1][-1] == f"origin/{hostile_target}...HEAD", (
        "the ref was built from the flattened name, not the raw one: "
        + repr(diffs[-1]))
    assert "(+2 -1)" in out, (
        "the raw name reached the ref but the figure still did not reach "
        "the render: " + out)

    # And the *display* half still goes through `flat()`: the raw separator
    # never reaches the terminal, and its removal is disclosed rather than
    # silent.
    assert SEP not in out, "the raw U+2028 separator reached the terminal"
    assert "[U+2028]" in out, (
        "the separator was dropped from the Target: line instead of "
        "disclosed: " + out)
