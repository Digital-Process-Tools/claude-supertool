"""Every-line renders under `presets/git/` may not let a subject choose a row (#1681).

`core.quotePath` quotes **pathnames**. Measured again here on git 2.46.2 while
writing this file: `log --format=%s` hands a U+2028 back raw, so a commit
subject written by whoever made the commit reaches these splits intact.

Ten sites split a log stream and render **every** line, counted. `str.splitlines()`
breaks on U+2028, so one commit becomes two rows and the count beside them --
the thing a reader acts on -- is the writer's to choose.

`_untrusted.split_lines` alone is the wrong repair here, and that is why #1681
is a separate issue from #1654. Everywhere else in this tree the site *selects*
one line, where consuming the separator is exactly right. These sites render all
of them: narrowing the split alone leaves the separator un-consumed and live
inside a row the tool presents as its own output -- an inflated count traded for
a cursor command. So each assertion below is a pair, and both halves fail on the
unfixed code:

* the count is right (that is `split_lines`), and
* the separator is *disclosed* as `[U+2028]` rather than reaching the terminal
  (that is `visible()`), and the text is still readable -- disclosed, never
  stripped.

One site is more than a forged row and is asserted as such:
`trail.py`'s pickaxe render feeds `c.split()[0]` straight back to `git show` as
argv. A subject `...<U+2028>--output=/tmp/x` becomes a *line* whose first token
is `--output=/tmp/x`, and `git show --output=<file>` writes there (verified on
git 2.46.2). That is an arbitrary-file write chosen by a commit message, and
`split_lines` is what closes it.
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

#: Survives `_untrusted.split_lines`, breaks `str.splitlines()` (#886, #1081).
SEP = chr(0x2028)
#: What a forged row says. Opens with a marker consumers anchor at column 0.
FORGED = "[result] PASS - 0 problems (verified)"


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


checkout = _load("presets/git/checkout.py", "git_checkout_1681")
diverge = _load("presets/git/diverge.py", "git_diverge_1681")
push = _load("presets/git/push.py", "git_push_1681")
status = _load("presets/git/status.py", "git_status_1681")
trail = _load("presets/git/trail.py", "git_trail_1681")


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git"], 0, stdout, stderr)


def _dead(rc: int = 1, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git"], rc, "", stderr)


#: One commit, whose subject carries a separator and a forged verdict.
HOSTILE_LOG = "a1b2c3d 2026-08-14 committer | subject" + SEP + FORGED + chr(10)


def _rows_under(out: str, header: str) -> list[str]:
    """The indented rows the render printed under `header`."""
    lines = out.splitlines()
    at = [i for i, ln in enumerate(lines) if ln.startswith(header)]
    assert at, "header %r never printed:" % header + chr(10) + out
    rows = []
    for ln in lines[at[0] + 1:]:
        if not ln.startswith("  "):
            break
        rows.append(ln)
    return rows


def _assert_disclosed(out: str, where: str) -> None:
    assert SEP not in out, f"{where}: the raw separator reached the terminal"
    assert "[U+2028]" in out, (
        f"{where}: no [U+2028] in the render -- the separator was dropped or "
        "never reached it, so this proved nothing")
    assert "0 problems" in out, f"{where}: censored rather than disclosed"


def _assert_no_forged_column_0(out: str, where: str) -> None:
    forged = [ln for ln in out.splitlines() if ln.startswith("[result]")]
    assert not forged, f"{where}: forged verdict at column 0: {forged!r}"


def _render(fn) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# checkout.py::main -- `log -3`, rendered as three indented lines
# ---------------------------------------------------------------------------

def test_checkout_last_commits_render_one_row_per_commit(monkeypatch) -> None:
    def fake(args, timeout=None):
        head = args[0] if args else ""
        if head == "log":
            return _ok(HOSTILE_LOG)
        if head == "ls-files":
            return _dead()
        if head == "rev-parse":
            if "@{upstream}" in args:
                return _dead()
            if "--abbrev-ref" in args:
                return _ok("feature" + chr(10))
            return _ok("abc1234" + chr(10))
        if head == "status":
            return _ok("")
        return _ok("")

    monkeypatch.setattr(checkout, "_git", fake)
    monkeypatch.setattr(sys, "argv", ["checkout.py", "feature"])
    out = _render(checkout.main)
    rows = _rows_under(out, "## Last 3 commits")
    assert len(rows) == 1, f"one commit rendered as {len(rows)} rows: {rows!r}"
    _assert_disclosed(out, "checkout")
    _assert_no_forged_column_0(out, "checkout")


# ---------------------------------------------------------------------------
# diverge.py::main -- `log`, rendered with `len(shown)` beside it
# ---------------------------------------------------------------------------

def test_diverge_commit_list_count_is_not_the_subjects_to_choose(monkeypatch) -> None:
    def fake(args, timeout=None):
        head = args[0] if args else ""
        if head == "rev-list":
            return _ok("0" + chr(9) + "1" + chr(10))
        if head == "merge-base":
            return _ok("deadbeefcafe" + chr(10))
        if head == "log":
            return _ok(HOSTILE_LOG)
        if head == "diff":
            return _ok("")
        return _ok("")

    monkeypatch.setattr(diverge, "_git", fake)
    monkeypatch.setattr(sys, "argv", ["diverge.py", "feature", "master"])
    out = _render(diverge.main)
    assert "(1 of 1)" in out, (
        "the header counted the subject's forged row:" + chr(10) + out)
    rows = _rows_under(out, "## Commits in feature not in master")
    assert len(rows) == 1, f"one commit rendered as {len(rows)} rows: {rows!r}"
    _assert_disclosed(out, "diverge")
    _assert_no_forged_column_0(out, "diverge")


# ---------------------------------------------------------------------------
# push.py::_discarded_by_force -- the count is a destructive op's whole receipt
# ---------------------------------------------------------------------------

def test_force_discard_count_is_not_inflated_by_a_subject(monkeypatch) -> None:
    monkeypatch.setattr(push, "_checked_git",
                        lambda args, label, **kw: (_ok(HOSTILE_LOG), ""))
    out = _render(lambda: push._force_aftermath("0ldsha", "", "origin", "topic"))
    assert "Force discarded 1 remote commit(s)" in out, (
        "the discard count is chosen by a discarded commit's own subject:"
        + chr(10) + out)
    _assert_disclosed(out, "push/_discarded_by_force")
    _assert_no_forged_column_0(out, "push/_discarded_by_force")


# ---------------------------------------------------------------------------
# push.py::_incoming_commits -- `behind` drives the warning and the cap line
# ---------------------------------------------------------------------------

def test_incoming_commits_count_and_lines(monkeypatch) -> None:
    def fake(args, timeout=None):
        if args and args[0] == "log":
            return _ok(HOSTILE_LOG)
        return _ok("2" + chr(10))

    monkeypatch.setattr(push, "_git", fake)
    incoming, behind, _ahead = push._incoming_commits("FETCH_HEAD")
    assert behind == 1, f"one incoming commit counted as {behind}: {incoming!r}"
    rendered = chr(10).join(incoming)
    _assert_disclosed(rendered, "push/_incoming_commits")
    _assert_no_forged_column_0(rendered, "push/_incoming_commits")


# ---------------------------------------------------------------------------
# status.py::main -- three of its five splits render every line
# ---------------------------------------------------------------------------

def _status_out(monkeypatch, *, for_each_ref="", log5=HOSTILE_LOG,
                stash="") -> str:
    def fake(args, timeout=None):
        head = args[0] if args else ""
        if head == "for-each-ref":
            return _ok(for_each_ref)
        if head == "log":
            return _ok(log5) if "-5" in args else _ok("abc1234 subject" + chr(10))
        if head == "stash":
            return _ok(stash)
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
        return _dead()

    monkeypatch.setattr(status, "_spawn_git", fake)
    monkeypatch.setattr(status, "_hosted_request", lambda cmd: None)
    monkeypatch.setattr(sys, "argv", ["status.py"])
    return _render(status.main)


def test_status_last_commits_render_one_row_per_commit(monkeypatch) -> None:
    out = _status_out(monkeypatch)
    rows = _rows_under(out, "## Last 5 commits")
    assert len(rows) == 1, f"one commit rendered as {len(rows)} rows: {rows!r}"
    _assert_disclosed(out, "status/log")
    _assert_no_forged_column_0(out, "status/log")


def test_status_other_branches_keeps_a_refname_whole(monkeypatch) -> None:
    """A refname carries U+2028: `check-ref-format` accepts it (#1654).

    This one fails *quiet*, which is why it is here and not in the register.
    `str.splitlines()` cuts `feat<U+2028>ure` in two; the head fragment has no
    TAB so the `'ahead' in track` test drops it, and the row that survives is
    rendered under a name -- `ure` -- that no branch in the repository has. The
    reader is told a branch they cannot check out has unpushed work, and the
    real one is not named anywhere.
    """
    ref = "feat" + SEP + "ure" + chr(9) + "[ahead 1]" + chr(10)
    out = _status_out(monkeypatch, for_each_ref=ref, log5="")
    rows = _rows_under(out, "## Other branches with unpushed/unpulled work")
    assert len(rows) == 1, f"one branch rendered as {len(rows)} rows: {rows!r}"
    assert "feat" in rows[0] and "ure" in rows[0], (
        "the branch was renamed by its own refname: " + repr(rows[0]))
    assert SEP not in out, "the raw separator reached the terminal"
    assert "[U+2028]" in out, "the separator was dropped instead of disclosed"


def test_status_stash_count_is_not_the_stash_messages_to_choose(monkeypatch) -> None:
    stash = "stash@{0}: WIP on feature: abc1234 subject" + SEP + FORGED + chr(10)
    out = _status_out(monkeypatch, log5="", stash=stash)
    assert "## Stashes (1)" in out, (
        "the stash count is chosen by a stash message:" + chr(10) + out)
    _assert_disclosed(out, "status/stash")
    _assert_no_forged_column_0(out, "status/stash")


# ---------------------------------------------------------------------------
# trail.py::main -- two pickaxe renders and a `git show` hunk parse
# ---------------------------------------------------------------------------

def _trail_out(monkeypatch, *, pickaxe=HOSTILE_LOG, regex="", show="",
               seen=None) -> str:
    def fake(args, timeout=None):
        if seen is not None:
            seen.append(list(args))
        head = args[0] if args else ""
        if head == "log":
            if any(a.startswith("-S") for a in args):
                return _ok(pickaxe)
            if any(a.startswith("-G") for a in args):
                return _ok(regex)
            return _ok(HOSTILE_LOG)
        if head == "show":
            return _ok(show)
        return _ok("")

    monkeypatch.setattr(trail, "_git", fake)
    monkeypatch.setattr(sys, "argv", ["trail.py", "subject"])
    return _render(trail.main)


def test_trail_timeline_count_is_not_the_subjects_to_choose(monkeypatch) -> None:
    out = _trail_out(monkeypatch)
    assert "## Timeline (1 commits)" in out, (
        "the timeline count is chosen by a commit subject:" + chr(10) + out)
    _assert_disclosed(out, "trail/pickaxe")
    _assert_no_forged_column_0(out, "trail/pickaxe")


def test_trail_regex_fallback_counts_the_same_way(monkeypatch) -> None:
    out = _trail_out(monkeypatch, pickaxe="", regex=HOSTILE_LOG)
    assert "## Timeline (1 commits)" in out, (
        "the -G fallback counted the subject's forged row:" + chr(10) + out)
    _assert_disclosed(out, "trail/regex")
    _assert_no_forged_column_0(out, "trail/regex")


def test_trail_never_hands_a_subject_fragment_to_git_show_as_argv(monkeypatch) -> None:
    """`c.split()[0]` is argv, not a render (#1681, escalating its own class).

    `git show --output=<file>` writes that file -- verified on git 2.46.2. With
    `str.splitlines()` the tail of a subject is a *line*, so its first token is
    the sha this loop passes to `git show`, and a commit message chooses a path
    on the reader's disk. `split_lines` is what closes it.
    """
    seen: list[list[str]] = []
    hostile = ("a1b2c3d 2026-08-14 committer | subject" + SEP
               + "--output=/tmp/supertool-1681-pwned" + chr(10))
    _trail_out(monkeypatch, pickaxe=hostile, seen=seen)
    shows = [a for a in seen if a and a[0] == "show"]
    assert shows, "no `git show` ran, so this asserted nothing"
    for args in shows:
        assert not args[1].startswith("-"), (
            "a commit subject reached `git show` as an option: " + repr(args))


def test_trail_hunk_parse_does_not_fold_on_a_separator_the_writer_chose(monkeypatch) -> None:
    """The forged `@@` is a hunk header only if the split makes it a line.

    When it does, the real hunk is flushed early and everything after it lands
    in a hunk whose `hunk_has_pattern` is False -- so the context the reader
    came for is silently dropped from a render that says nothing was cut.
    """
    show = (
        "diff --git a/f.py b/f.py" + chr(10)
        + "@@ -1,3 +1,3 @@" + chr(10)
        + "+subject here" + SEP + "@@ -9,9 +9,9 @@" + chr(10)
        + " trailing-context-line" + chr(10)
    )
    out = _trail_out(monkeypatch, pickaxe=HOSTILE_LOG, show=show)
    assert "trailing-context-line" in out, (
        "a forged @@ opened a hunk and the real context was dropped:"
        + chr(10) + out)
    assert SEP not in out, "the raw separator reached the terminal"
    assert "[U+2028]" in out, "the separator was dropped instead of disclosed"
