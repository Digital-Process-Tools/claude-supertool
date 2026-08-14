"""#1119, second site - `gl-mr` split `git merge-tree` hunk output the same way.

Found by the #1119 audit, not by the issue. The conflict-hunk parser reads a
stream that is not porcelain: it is the CONFLICTED FILE CONTENT of the two
branches, verbatim, with `errors="replace"` so binary survives as replacement
characters. Both of its anchors are at column 0:

    _MERGE_TREE_HEADER_RE  ^(changed in both|added in local|...)
    _MERGE_TREE_PATH_RE    ^  (?:base|our|their) <mode> <sha> <path>

A header match calls `_flush()` and clears `current_path`, so a forged one
ends the current file's hunk early and drops every line after it until the
next real path line. The render then prints a conflict preview that silently
omits the conflict, under a heading naming the file - the absence-read-as-
presence shape `docs/validators.md` is about.

Verified against real `git merge-tree` output, not assumed: a file whose
content carries U+2028 immediately before the text `changed in both` puts that
text at column 0 under `str.splitlines()`. Filenames cannot do this - git
octal-quotes any non-ASCII byte in a path it prints, which is why
`_get_conflicting_files` is left alone; blob content is not quoted, which is
why this one is not.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any

_PRESET = Path(__file__).parent.parent / "presets" / "gitlab" / "mr.py"
_spec = importlib.util.spec_from_file_location("gitlab_mr_1119", _PRESET)
assert _spec is not None and _spec.loader is not None
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)

FORGED = tuple(chr(c) for c in (0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029))
LF = chr(10)

BASE_SHA = "d05162806645ddae65b2822a77d5c59d8133a8d0"


def _merge_tree_stdout(payload_line: str) -> str:
    """The shape `git merge-tree <base> <ours> <theirs>` really emits."""
    return LF.join([
        "changed in both",
        "  base   100644 f0f2307464291 app.py",
        "  our    100644 f10ee2ffbe440 app.py",
        "  their  100644 da967e82ffad0 app.py",
        "@@ -1,3 +1,7 @@",
        " l1",
        "+<<<<<<< .our",
        " Y",
        "+=======",
        "+" + payload_line,
        "+>>>>>>> .their",
        " l3",
    ]) + LF


def _fake_git(stdout: str):
    def run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        if len(args) > 1 and args[1] == "merge-base":
            return subprocess.CompletedProcess(args, 0, BASE_SHA + LF, "")
        return subprocess.CompletedProcess(args, 1, stdout, "")

    return run


def _hunks(monkeypatch, payload_line: str) -> tuple[dict[str, str], str | None]:
    monkeypatch.setattr(mr.subprocess, "run", _fake_git(_merge_tree_stdout(payload_line)))
    return mr._get_conflict_hunks("feature", "master", 1)


def test_a_clean_conflict_hunk_is_read_whole(monkeypatch) -> None:
    """The control. Every line of the hunk reaches the caller."""
    hunks, err = _hunks(monkeypatch, "X")
    assert err is None
    assert "app.py" in hunks
    assert ">>>>>>> .their" in hunks["app.py"], hunks


def test_a_forged_header_cannot_end_a_files_hunk_early(monkeypatch) -> None:
    """A truncated preview under a heading naming the file is a lie about it."""
    for sep in FORGED:
        hunks, err = _hunks(monkeypatch, "X" + sep + "changed in both")
        assert err is None
        assert "app.py" in hunks, (sep, hunks)
        assert ">>>>>>> .their" in hunks["app.py"], (
            f"{sep!r} forged a record boundary and cut the hunk short: {hunks!r}"
        )


def test_a_forged_path_line_cannot_reattribute_a_hunk(monkeypatch) -> None:
    """The path is the only thing tying a hunk to the file it came from."""
    for sep in FORGED:
        hunks, err = _hunks(
            monkeypatch, "X" + sep + "  their  100644 aaaaaaaaaaaaa evil.py"
        )
        assert err is None
        assert list(hunks) == ["app.py"], (
            f"{sep!r} produced a hunk attributed to a file it did not come from: {hunks!r}"
        )


def test_the_parser_hands_back_the_files_own_bytes(monkeypatch) -> None:
    """The separator is no longer consumed - it survives, on purpose.

    A caller of `_get_conflict_hunks` wants the conflicted file's content, not
    a sanitised version of it. That is what makes the render's disclosure the
    other half of the fix rather than a duplicate of it.
    """
    for sep in FORGED:
        hunks, _ = _hunks(monkeypatch, "X" + sep + "tail")
        assert sep in hunks["app.py"], (sep, hunks)


def test_a_surviving_separator_is_disclosed_before_the_render() -> None:
    """Narrowing alone would put a live cursor movement under the `  ` indent."""
    for sep in FORGED:
        shown = mr._hunk_display_lines("+X" + sep + "tail")
        assert len(shown) == 1, (sep, shown)
        assert sep not in shown[0], (
            f"{sep!r} reached a row the render prints under a two-space indent"
        )


def test_the_stderr_extractions_disclose_the_separator_instead_of_eating_it(
    monkeypatch,
) -> None:
    """The sweep this test's own docstring asked for, done (#1654).

    #1119 narrowed the two structural parses and kept the splits that lift one
    line out of a subprocess's stderr, on the argument that narrowing would be
    worse - it would leave a forged break INSIDE the extracted string instead
    of consuming it. #1648 retired that argument on the GitHub side and this
    version's predecessor said, in these words, that consuming the separator
    "answers *forgery* and does not answer *loss*: the text before a U+2028 is
    still discarded rather than shown, which is what a sweep of these six would
    fix."

    Both halves are now answered at both sites, and the assertions flip with
    them: `_untrusted.split_lines` decides the boundary, so the whole line
    survives and the writer no longer picks the segment; `_untrusted.flat`
    spells the separator `[U+2028]`, so nothing forged reaches column 0. What
    was `forged not in reason` is now `forged IS in reason, and readable` -
    the tail is disclosed rather than dropped.

    git's half is unchanged and still worth stating: `check-ref-format` REJECTS
    every ASCII control character in a refname while ACCEPTING U+2028, and
    `merge-tree` is called on `origin/<source>`, a branch name the MR's author
    chose. So U+2028 is exactly the character that reaches this stderr.
    """
    def run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        if len(args) > 1 and args[1] == "merge-base":
            return subprocess.CompletedProcess(args, 0, BASE_SHA + LF, "")
        return subprocess.CompletedProcess(
            args, 128, "", "fatal: not a valid object" + chr(0x2028) + "forged" + LF
        )

    monkeypatch.setattr(mr.subprocess, "run", run)
    _, reason = mr._get_conflict_hunks("feature", "master", 1)
    assert reason is not None
    assert chr(0x2028) not in reason, reason
    assert "[U+2028]" in reason, reason
    assert "not a valid object" in reason, reason
    assert "forged" in reason, (
        "the tail was dropped rather than disclosed, so git's own reason and "
        f"the forgery are both gone from the decline: {reason!r}")
    assert len(reason.splitlines()) == 1, reason

    detail = mr._glab_fail_detail(subprocess.CompletedProcess(
        ["glab"], 1, "", "ERROR" + LF + "boom" + chr(0x2028) + "forged" + LF
    ))
    assert chr(0x2028) not in detail, detail
    assert "[U+2028]" in detail, detail
    assert "boom" in detail and "forged" in detail, detail
    assert len(detail.splitlines()) == 1, detail


def test_a_hunks_indentation_is_kept() -> None:
    """A hunk is a block; the leading space of a context line is content."""
    assert mr._hunk_display_lines(" l1" + chr(9) + "x") == [" l1" + chr(9) + "x"]
