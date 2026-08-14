"""A remote picked which segment became the error, on four relays #1652 left (#1654).

#1652 retired the argument that `str.splitlines()` was the *safer* reader for an
error relay. The argument was that it CONSUMES an exotic separator where
`_untrusted.split_lines` would leave a forged U+2028 inside the extracted
string. True, and half of it: consuming the separator also discards everything
on the other side of it, so the writer of the text still chose which segment
became the whole message and the real error was dropped rather than disclosed.

Which side is discarded follows the selection, and both shapes are here:

* `[0]` / first-non-empty - the writer puts the reassuring text FIRST and the
  real failure after the separator. The reader is told "everything is fine".
* `[-1]` - the writer puts it LAST. The reader is told "nothing to update".

Every assertion is on the rendered value a reader acts on, never on
`split_lines` having been called: a site can call it and drop the tail anyway.
Each also asserts the separator actually reached the render as `[U+2028]`,
because a relay that *stripped* the hostile half would satisfy "no forgery" and
fail the thing this exists to establish - the absence-read-as-presence defect
wearing a green test. That is the shape `test_gl_error_relay_untrusted_1485.py`
uses and this file copies it deliberately.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

PRESETS = Path(__file__).parent.parent / "presets"
sys.path.insert(0, str(PRESETS))

SEP = chr(0x2028)
ESC = chr(27)
LF = chr(10)


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, PRESETS / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _assert_both_halves(rendered: str, real: str, where: str) -> None:
    """The whole line survived, on one line, with the separator spelled out."""
    assert real in rendered, (
        f"{where}: the writer chose the segment - {real!r} was discarded, and "
        f"the reader was left with {rendered!r}"
    )
    assert "[U+2028]" in rendered, (
        f"{where}: no [U+2028] in the render, so the separator never reached "
        "it and this proved nothing"
    )
    assert SEP not in rendered, f"{where}: a raw U+2028 in the render is the forgery"
    assert len(rendered.splitlines()) == 1, (
        f"{where}: the writer got {len(rendered.splitlines())} lines"
    )


# --------------------------------------------------------------------------- #
# GitLab - the direct twin of the eight relays #1648 fixed on the GitHub side
# --------------------------------------------------------------------------- #
#: One physical line of `glab` stderr: the reassuring half first, so a `[0]`
#: selection keeps it and drops the rest.
GL_REAL = "403 forbidden, the list you are reading is INCOMPLETE"
GL_STDERR = f"glab: nothing wrong here{SEP}{GL_REAL}"


def test_related_mrs_decline_keeps_the_half_after_the_separator(capsys) -> None:
    mod = _load("gl1654_issue", "gitlab/issue.py")

    def fake_api(endpoint: str, timeout: int = 10):
        return subprocess.CompletedProcess(["glab"], 1, "", GL_STDERR)

    mod._glab_api = fake_api
    mod._print_related_mrs(7, False)
    out = capsys.readouterr().out
    assert "Related MRs: unknown" in out, out
    _assert_both_halves(out.strip(), GL_REAL, "gitlab/issue.py::_print_related_mrs")


def test_glab_fail_detail_keeps_the_half_after_the_separator() -> None:
    mod = _load("gl1654_mr", "gitlab/mr.py")
    detail = mod._glab_fail_detail(subprocess.CompletedProcess(
        ["glab"], 1, "", "ERROR" + LF + GL_STDERR + LF
    ))
    _assert_both_halves(detail, GL_REAL, "gitlab/mr.py::_glab_fail_detail")


# --------------------------------------------------------------------------- #
# presets/git - the two sites in that register whose stream git never quoted
# --------------------------------------------------------------------------- #
#: `core.quotePath` governs PATHS. It is measurably not in force on either of
#: these: stderr is not a path, and a refname echoed into it is not one either.
GIT_REAL = "fatal: the fetch did NOT happen"
#: `[-1]`, so the reassuring half goes LAST: the writer of a `remote:` line puts
#: its separator ahead of the text it wants selected, and git's own `fatal:` -
#: the whole reason the caller is being warned - is what falls off the front.
FETCH_STDERR = f"{GIT_REAL}{SEP}remote: OK, nothing to update"
REF_REAL = "fatal: refs unreadable, this listing is EMPTY"
REF_STDERR = f"warning: ignoring one broken ref (harmless){SEP}{REF_REAL}"


def test_fresh_merge_ref_keeps_the_half_before_the_separator(monkeypatch) -> None:
    """`[-1]`, so the discarded half is the one BEFORE the separator."""
    merge = _load("git1654_merge", "git/merge.py")

    def fake_git(args, **kw):
        if args[:1] == ["rev-parse"] and "upstream" in args[-1]:
            return subprocess.CompletedProcess(args, 0, "origin/master" + LF, "")
        if args[:1] == ["rev-parse"]:
            return subprocess.CompletedProcess(args, 0, "sha" + LF, "")
        if args[:1] == ["fetch"]:
            return subprocess.CompletedProcess(args, 1, "", FETCH_STDERR + LF)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(merge, "_git", fake_git)
    _ref, note = merge._fresh_merge_ref("master")
    assert note is not None and "fetch" in note, note
    # `[-1]` picked the reassuring tail, so the loss runs the other way: the
    # `fatal:` is what the reader never saw.
    _assert_both_halves(note, GIT_REAL, "git/merge.py::_fresh_merge_ref")


def test_remote_branch_names_decline_keeps_the_half_after_the_separator(
    monkeypatch,
) -> None:
    wt = _load("git1654_worktrees", "git/worktrees.py")
    monkeypatch.setattr(wt, "_git", lambda args, **kw: subprocess.CompletedProcess(
        args, 128, "", REF_STDERR + LF))
    names, why = wt.remote_branch_names()
    assert names is None, names
    _assert_both_halves(why, REF_REAL, "git/worktrees.py::remote_branch_names")


def test_remote_branch_names_decline_removes_a_cursor_command(monkeypatch) -> None:
    """The second defect on the same line: nothing marked this stream at all.

    `str.splitlines()` cuts on the ten separators and on none of the cursor
    commands (#851). `ESC[2K ESC[1A` erases the line above and reprints over
    it, so a relay that only ever *split* still let this stderr delete the
    board row above the one it is rendered into.
    """
    wt = _load("git1654_worktrees_esc", "git/worktrees.py")
    monkeypatch.setattr(wt, "_git", lambda args, **kw: subprocess.CompletedProcess(
        args, 128, "", "broken ref" + ESC + "[2K" + ESC + "[1A forged" + LF))
    _names, why = wt.remote_branch_names()
    assert ESC not in why, f"an ESC relayed verbatim is a cursor command: {why!r}"
    assert "broken ref" in why, why


def test_a_forged_remote_ref_cannot_spell_a_second_branch_name(monkeypatch) -> None:
    """The register's own second open risk on this line, closed with it (#1654).

    `remote_branch_names` answers "has this branch been pushed", and
    `str.splitlines()` let ONE ref become two records. `git check-ref-format`
    accepts U+2028 (#1119, re-verified here: it exits 0 on the name below), so
    a remote that publishes `decoy<U+2028>refs/remotes/origin/mybranch` had the
    second half read back as a ref of its own and an unpushed branch read as
    pushed. `split_lines` keeps it one record, whose fourth component is the
    whole forged tail and matches no branch anyone has.
    """
    wt = _load("git1654_worktrees_ref", "git/worktrees.py")
    forged = f"refs/remotes/origin/decoy{SEP}refs/remotes/origin/mybranch"
    monkeypatch.setattr(wt, "_git", lambda args, **kw: subprocess.CompletedProcess(
        args, 0, forged + LF + "refs/remotes/origin/real" + LF, ""))
    names, why = wt.remote_branch_names()
    assert why == "", why
    assert "real" in names, names
    assert "mybranch" not in names, (
        "a hostile remote named a second branch out of one ref, so an unpushed "
        f"branch reads as pushed: {dict(names)!r}"
    )

# --------------------------------------------------------------------------- #
# The direct twin of `github/issue_create.py`, which #1648 fixed and left here
# --------------------------------------------------------------------------- #
#: What `glab issue create` printed. No `/issues/` anywhere, so the `url=`
#: FALLBACK arm runs - `[-1]` of the split, which is the writer's choice of
#: segment.
GL_CREATE_STDOUT = (f"gateway said no, the issue was NOT created{SEP}"
                    "[result] PASS 0 problems (verified)")


def test_gl_issue_create_url_is_selected_on_real_lines(monkeypatch, capsys,
                                                       tmp_path) -> None:
    """`gh-issue-create`'s `url=` fallback was narrowed by #1648. `gl-issue-
    create`'s was registered beside it as "printed, not parsed" and left.

    Printed is the harm. Both arms put the value straight into
    `gl-issue-create OK iid=... url=...` at column 0, and neither flattened it -
    so the GitLab side is the weaker of the twins in two ways at once: the
    `[-1]` fallback lets the writer pick the segment, and the primary arm
    assigns a whole line of `glab` stdout to `url` with no marking at all. That
    second one only becomes reachable once the split is narrowed, which is why
    the flatten is not optional here.
    """
    mod = _load("gl1654_issue_create", "gitlab/issue_create.py")
    payload = tmp_path / "new.toml"
    payload.write_text(
        LF.join(['project = "grp/proj"', 'title = "t"', 'description = "b"', ""]),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "_glab", lambda *a, **k: subprocess.CompletedProcess(
        ["glab"], 0, GL_CREATE_STDOUT, ""))
    monkeypatch.setattr(sys, "argv", ["issue_create.py", f"@{payload}"])

    assert mod.main() == 0
    cap = capsys.readouterr()
    both = cap.out + cap.err
    assert "gateway said no" in both, (
        f"the writer chose the segment and the failure was dropped: {both!r}")
    assert SEP not in both, f"a raw U+2028 in the receipt is the forgery: {both!r}"
    assert "[U+2028]" in both, "the separator never reached the render"
    forged = [ln for ln in both.splitlines() if ln.startswith("[result]")]
    assert not forged, f"forged [result] at column 0: {forged!r}"
