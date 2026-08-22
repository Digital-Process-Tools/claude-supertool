"""git-status's timeout footer can carry an unflattened line break.

#1939. `_git()` at `presets/git/status.py` records a timed-out command in
`_UNANSWERED` as `("git " + " ".join(args), res.stderr)` -- the args are
whatever the caller passed, unflattened. One caller is the MR/PR shortstat
diff, whose ref is built from `mr_target_raw`, the target branch exactly as
the tracker reported it (`status.py:950`). The same file already flattens
that value for the `Target:` line (#977, #851, #977) with `_untrusted.flat`,
but the guard sits on that one render and does not reach `_git()`'s generic
timeout-recording path, which is otherwise reachable from any of `_git()`'s
17 call sites.

So a target branch carrying U+2028 (accepted by `git check-ref-format`,
#1654) forced to time out puts a raw line separator into the string stored
in `_UNANSWERED`, and `_incomplete_note()` renders it straight into the
`INCOMPLETE` footer -- a receipt this repository parses -- as an extra,
unaccounted line.

Control pair, per the issue: a hostile target branch forced to time out
must produce a single-line footer; an ordinary target branch forced to
time out must still name the `git diff --shortstat ...` command it names
today, unmangled.
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


status = _load("presets/git/status.py", "git_status_1939")


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git"], 0, stdout, stderr)


def _timeout(budget: int = 3) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        ["git"], status.TIMEOUT_RC, "", f"timed out after {budget}s")


def _render(fn) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    return buf.getvalue()


def _status_out(monkeypatch, target_branch: str) -> str:
    """Drive `status.main()` with one MR whose `diff --shortstat` call
    always times out. Every other git call answers plainly so the footer
    entry for the shortstat timeout is the only thing under test.
    """
    def fake(args, timeout=None):
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
            return _ok("", "")
        if head == "rev-list":
            return _ok("0" + chr(9) + "0" + chr(10))
        if head == "diff" and "--shortstat" in args:
            return _timeout()
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
    status._UNANSWERED.clear()
    return _render(status.main)


def test_ordinary_branch_timeout_still_names_the_shortstat_command(
        monkeypatch) -> None:
    """Positive control: the footer must still name today's command for an
    unremarkable branch -- the fix must not blank the entry, only flatten
    the hostile case.
    """
    out = _status_out(monkeypatch, "master")

    assert "INCOMPLETE" in out, out
    assert "git diff --shortstat origin/master...HEAD" in out, out


def test_hostile_branch_timeout_footer_is_a_single_line(monkeypatch) -> None:
    """The bug: a target branch carrying U+2028, forced to time out, put the
    raw separator into `_UNANSWERED`'s command string, and the footer line
    built from it carried an extra, unaccounted line break.
    """
    hostile_target = "release" + SEP + "train"
    out = _status_out(monkeypatch, hostile_target)

    assert "INCOMPLETE" in out, out
    footer = out.split(status.INCOMPLETE_MARKER, 1)[1]
    footer_line = footer.splitlines()[0]
    # The footer's own sentence spans several *printed* lines by design
    # (the marker line, then prose) -- what must never happen is a break
    # injected by the untrusted branch name itself, inside what render as
    # one entry in the `calls` list.
    assert SEP not in out, (
        "the raw U+2028 separator reached the terminal: " + repr(out))
    assert "[U+2028]" in out, (
        "the separator was dropped from the footer instead of disclosed: "
        + repr(out))
    assert footer_line.count(chr(10)) == 0
