"""git-status's MR diff line drops (+a -d) on every failure that is not an
empty diff -- a timeout and an unfetched ref render identically to a clean run.

#1933. `presets/git/status.py::main` gated the shortstat figure on
`returncode == 0 and stdout.strip()` alone, so a timed-out `git diff
--shortstat`, an unresolvable target ref, and any other git failure all
collapsed into the exact same render as a genuinely empty diff: bare
`Diff: N files`, no line counts, no explanation anywhere on the line.

Decision recorded here (see presets/git/status.py for the corresponding
comment): the silence was NOT intended for any of these three causes. This
file's own `changes_count` guard already establishes the diff is non-empty
before the shortstat call is even made, so a caller reading a bare
`Diff: N files` inside that branch has no way to tell "git could not answer"
from "there happen to be no line changes" -- and the second reading is
impossible in this branch by construction. Three states, not two: print the
counts, or say the call did not answer and why.

The control pair, straight from the issue: a fetched target ref with a real
diff must still print `(+a -d)` (unchanged from #977's own coverage), and a
target ref that does not resolve must render distinguishably from that same
success line -- not as a second flavour of silence.
"""
from __future__ import annotations

import importlib.util
import io
import re
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "presets"))


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


status = _load("presets/git/status.py", "git_status_1933")


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git"], 0, stdout, stderr)


def _dead(rc: int = 1, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git"], rc, "", stderr)


def _timeout(budget: int = 3) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        ["git"], status.TIMEOUT_RC, "", f"timed out after {budget}s")


def _render(fn) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    return buf.getvalue()


def _status_out(monkeypatch, shortstat_result: subprocess.CompletedProcess) -> str:
    """Drive `status.main()` with one MR whose `diff --shortstat` answers
    with *shortstat_result*. Every other git call answers plainly so the MR
    diff line is the only thing under test.
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
            return _dead()
        if head == "rev-list":
            return _ok("0" + chr(9) + "0" + chr(10))
        if head == "diff" and "--shortstat" in args:
            return shortstat_result
        return _ok("")

    monkeypatch.setattr(status, "_spawn_git", fake)
    monkeypatch.setattr(
        status, "_hosted_request",
        lambda cmd: {
            "iid": 1, "title": "Fix", "state": "opened",
            "target_branch": "master", "changes_count": "1",
        } if cmd[:2] == ["glab", "mr"] else None,
    )
    monkeypatch.setattr(sys, "argv", ["status.py"])
    status._UNANSWERED.clear()
    return _render(status.main)


def test_a_fetched_target_ref_with_a_real_diff_still_prints_counts(monkeypatch) -> None:
    """Positive control: the success path must not regress."""
    out = _status_out(
        monkeypatch,
        _ok(" 5 files changed, 126 insertions(+), 72 deletions(-)" + chr(10)))

    assert "(+126 -72)" in out, (
        "a real diff's line counts did not reach the render: " + out)


def test_an_unresolvable_target_ref_renders_distinguishably_from_empty(
        monkeypatch) -> None:
    """The bug: `git diff --shortstat` failing because the ref is not
    present locally used to render byte-identically to a genuinely empty
    diff -- bare `Diff: 1 files`, no figures, no explanation. That reading is
    impossible here: `changes_count` already proved the diff is non-empty.
    """
    out = _status_out(monkeypatch, _dead(128, "fatal: bad revision origin/master...HEAD"))

    assert "Diff: 1 files" in out, out
    silent_render = "Diff: 1 files" + chr(10)
    assert silent_render not in out, (
        "an unresolvable target ref rendered as a bare file count -- "
        "identical to a genuinely empty diff, which this branch has already "
        "ruled out: " + out)
    diff_line = out.split("Diff:", 1)[1].split(chr(10), 1)[0]
    assert not re.search(r"\(\+\d", diff_line), (
        "a failed shortstat must not fabricate line counts: " + out)


def test_a_shortstat_timeout_renders_distinguishably_from_empty(monkeypatch) -> None:
    """Same defect, the timeout cause: `_git` already records a TIMEOUT_RC
    call in `_UNANSWERED` for the footer, but the diff line itself must not
    read as a clean, figure-free success -- a caller piping this through
    `brief` or truncating at the top never reaches the footer.
    """
    out = _status_out(monkeypatch, _timeout())

    assert "Diff: 1 files" in out, out
    silent_render = "Diff: 1 files" + chr(10)
    assert silent_render not in out, (
        "a shortstat timeout rendered as a bare file count -- identical to "
        "a genuinely empty diff: " + out)
    diff_line = out.split("Diff:", 1)[1].split(chr(10), 1)[0]
    assert not re.search(r"\(\+\d", diff_line), (
        "a timed-out shortstat must not fabricate line counts: " + out)
    # `_git`'s own TIMEOUT_RC handling already records this call so the
    # footer names it -- unchanged by this fix, and worth pinning so a
    # future edit to the diff line does not silently drop the footer entry.
    assert any("diff --shortstat" in c for c, _w in status._UNANSWERED), (
        "the timed-out shortstat call was not recorded for the footer: "
        + repr(status._UNANSWERED))
