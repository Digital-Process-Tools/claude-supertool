"""A child process's stdout may not become a line the tool wrote (#1475).

The class behind #1470, which closed it for `git-push` only. `gh` writes the
GitHub API's refusal text onto its own stderr, a commit hook writes whatever it
likes, and both reached a receipt at column 0 with no `_untrusted` call between
them. `_untrusted.split_lines` cuts on LF / CR / CRLF alone, by design (#1081),
so a U+2028 survives *inside* a relayed line and everything the reader anchors
at column 0 becomes the writer's to choose.

**The fix is at the seam, not at the sites.** Seven sites were named and the
sweep below finds 139 sites in 32 files, which is what a per-site fix earns:
the same defect re-filed once per call. So

* `_git_common._first_error_line` flattens what it returns. Every caller —
  `git-commit`, `git-push`, and whatever is written next — is covered at once,
  and `_untrusted.flat` is idempotent so the callers that already flattened pay
  a no-op rather than a second substitution. It also splits with
  `_untrusted.split_lines` instead of `str.splitlines()`: it is *parsing* a
  line-oriented stream, and the parse must not fold on a separator the writer
  chose (#1081).
* `pr._format_error` is the one sink for `gh-pr`'s two error prints, and
  flattens there.
* `commit._failure_receipt` is the whole failure render, extracted so the dump
  can be relayed the way `git-push` relays its own (#1448): the child's lines
  under a `> ` prefix, disclosed with `visible(keep=tab)` rather than dropped.

The bar is the one `tests/test_forged_branch_line_965.py` set: assert on what a
consumer counts, not on `flat` having been called — a site can call it and
print the raw value anyway — and assert the forged text is still *readable*,
because disclosed-never-stripped is the trade this repo has already made.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


git_common = _load("presets/git/_git_common.py", "git_common_1475")
commit = _load("presets/git/commit.py", "git_commit_1475")
gh_pr = _load("presets/github/pr.py", "github_pr_1475")

#: Survives `_untrusted.split_lines`, breaks `str.splitlines()` (#886, #1081).
SEP = chr(0x2028)
ESC = chr(0x1b)

#: The two spellings `_untrusted.visible` has for a tab. Unlike U+2028, a tab is
#: in C0 and therefore has a Control Picture, so which of the two a render emits
#: depends on the stream rather than on the code under test. Under pytest
#: `sys.stdout` names utf-8, so a re-flattened tab here spells `TAB_PICTURE` --
#: and the `"0009" not in body` this replaces was the one spelling that could
#: not arrive (#1736).
TAB_ASCII = "[U+0009]"
TAB_PICTURE = chr(0x2409)

FORGED_RESULT = "[result] 1 op run, 1 write"
FORGED_STATUS = "Status: COMMITTED"
MARKERS = ("[result]", "Status:", "First error:", "HEAD after:")


def relayed(lines: list) -> list:
    """Only the child's own lines — everything under `--- git output ---`.

    The three lines above it are supertool's, and they legitimately open with
    the markers below. Scoping the assertion to the relay is what makes it an
    assertion about the *forgery* rather than about the render.
    """
    at = lines.index("--- git output ---")
    return lines[at + 1:]


def assert_no_forged_marker(lines: list) -> None:
    """No line the child wrote may open with a marker the tool owns."""
    for line in lines:
        for marker in MARKERS:
            assert not line.startswith(marker), (
                f"a child stream forged a column-0 {marker} line:" + chr(10)
                + chr(10).join(f"  {i:>3} | {ln}"
                               for i, ln in enumerate(lines, 1))
            )


def assert_disclosed(text: str) -> None:
    """The separator is shown, not deleted, and never survives as itself."""
    assert SEP not in text, "the raw separator reached the render"
    assert "U+2028" in text, "the separator was dropped instead of disclosed"


# ---------------------------------------------------------------------------
# the seam: _first_error_line
# ---------------------------------------------------------------------------

def test_first_error_line_returns_one_line() -> None:
    """It is printed at column 0 by two ops and interpolated into a third."""
    got = git_common._first_error_line(
        "step 1" + chr(10) + "error: refused" + SEP + FORGED_RESULT)
    assert got.splitlines() == [got], got
    assert_disclosed(got)
    assert "1 op run, 1 write" in got, "the refusal text was censored"


def test_first_error_line_discloses_an_escape_sequence() -> None:
    """`ESC [2K ESC [1A` erases a line the tool already wrote (#851)."""
    got = git_common._first_error_line("error: refused" + ESC + "[2K")
    assert ESC not in got
    assert "error: refused" in got


def test_first_error_line_does_not_fold_on_a_separator_the_writer_chose() -> None:
    """The parse is line-oriented, so it cuts on LF/CR/CRLF only (#1081).

    With `str.splitlines()` the forged tail was a *line* of its own, and a
    hostile writer could therefore choose which line the scan returned.
    """
    got = git_common._first_error_line(
        "fatal: real cause" + SEP + "pushed successfully")
    assert "real cause" in got


# ---------------------------------------------------------------------------
# gh-pr's one error sink
# ---------------------------------------------------------------------------

def test_gh_pr_format_error_is_one_line() -> None:
    """`gh` echoes the GitHub API's own message; two prints share this sink."""
    got = gh_pr._format_error("boom" + SEP + FORGED_RESULT, "PR", "42")
    assert got.splitlines() == [got], got
    assert_disclosed(got)
    assert "1 op run, 1 write" in got


# ---------------------------------------------------------------------------
# git-commit's failure receipt
# ---------------------------------------------------------------------------

def _completed(stdout: str = "", returncode: int = 1, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(["git"], returncode, stdout, stderr)


def test_commit_failure_receipt_cannot_be_forged_by_a_hook() -> None:
    """A pre-commit hook chose the bytes; only supertool owns column 0."""
    hook = ("running checks" + chr(10)
            + "error: blocked" + SEP + FORGED_STATUS + SEP + FORGED_RESULT)
    lines = commit._failure_receipt(_completed(stderr=hook),
                                    head_before="a" * 7, head_after="a" * 7)
    assert_no_forged_marker(relayed(lines))
    assert_disclosed(chr(10).join(lines))
    assert "error: blocked" in chr(10).join(lines)


def test_commit_failure_receipt_still_relays_the_whole_transcript() -> None:
    """Containment may not cost the reader the hook output they came for."""
    hook = "line one" + chr(10) + "line two"
    body = chr(10).join(commit._failure_receipt(
        _completed(stderr=hook), head_before="a" * 7, head_after="b" * 7))
    assert "line one" in body and "line two" in body
    assert "Status: commit returned exit 1" in body.splitlines()


def test_a_tab_survives_the_commit_relay() -> None:
    """The other half of #1448's trade, pinned so it is not re-flattened."""
    tab = chr(9)
    body = chr(10).join(commit._failure_receipt(
        _completed(stdout="PASS" + tab + "tests/test_a.py" + chr(10)
                   + "and a separator" + SEP + "here"),
        head_before="a" * 7, head_after="a" * 7))
    assert "PASS" + tab + "tests/test_a.py" in body
    # Both spellings, not the ASCII one alone. `_stream()` reads
    # `sys.stdout.encoding`, which under pytest is utf-8, so a receipt that
    # re-flattened this tab would spell it `TAB_PICTURE` and `"0009" not in
    # body` would have gone green on exactly the regression it was written for
    # -- an absence produced by the assertion, read as an absence in the render
    # (#1736). The four hex characters are also four a sha carries (#1691).
    assert TAB_ASCII not in body, body
    assert TAB_PICTURE not in body, body
    # must-fire, same receipt: the separator on the child's second line *is*
    # disclosed, so the two assertions above say "the tab was kept" rather than
    # "nothing from the child reached the relay".
    assert_disclosed(body)

# ---------------------------------------------------------------------------
# the detector: nothing stopped the eighth
# ---------------------------------------------------------------------------
#
# The seven fixes above are worth less than this section. #1470 closed one op
# and the class re-arrived seven times; the issue says so in its own acceptance
# clause, and its quoted refusal explains why the obvious guard is hard: the
# taint set for a child stream is not `REFNAME_KEYS`. It is `combined`, `blob`,
# loop variables over `split_lines(...)` and helper returns, against parse-only
# consumers that must not trip it. Rushed, it "arrives as an allowlist that
# grows quietly — which is the failure mode of the thing it would be guarding".
#
# **So this is not a zero-assertion, and pretending otherwise is what would
# make it useless.** Measured on this branch: 139 candidate sites in 32 files
# across `presets/git`, `presets/github` and `presets/gitlab`. Not all are
# defects — `push._local_head` returns `r.stdout.strip()`, and that is a SHA —
# and closing them is four lanes of work this PR is not.
#
# What is enforceable today, with no exemption mechanism at all, is a **count
# per file that may only go down**. It is not an allowlist: there is nothing to
# add a site to. A new relay in any of these 34 files bumps its number and
# fails; a fixed one lowers it and fails, with the message telling you to write
# the smaller number down. Both directions are one visible line in a diff,
# which is exactly what "grows quietly" was not.
#
# What it does NOT catch, said plainly: a relay added to a file with no row
# here is caught (its count goes 0 → 1), but a relay added to `push.py` in the
# same commit that fixes another one nets to 33 and passes. That is the price
# of shipping a ratchet instead of a gate, and the gate costs 158 fixes first.
#
# What it also does not catch is every *shape* — see `SINK_SHAPES` below, and
# `UNRESOLVED` for the size of what it cannot see at all (#1626, #1570).

#: The two attributes a `CompletedProcess` exposes, and a parameter carrying
#: either name. Small on purpose, for the reason `REFNAME_KEYS` is: the scan is
#: only worth having while its key set needs no exemptions.
STREAM_ATTRS = frozenset({"stdout", "stderr"})

#: Anything that marks a child's text before it is rendered.
MARKS = frozenset({"flat", "fence", "scrub", "visible", "render_row",
                   "shell_ref"})

#: Method calls that render. `print` and `return` were the whole sink set until
#: #1626, and the gap was measured rather than argued: of the fifteen relays
#: #1606 fixed, four went through `sys.stderr.write` and scored 0 both before
#: and after, so the ratchet was green across them the entire time.
#:
#: `append`/`extend` are counted wherever they appear, not only into a list
#: that is later returned — #1570's `commit._failure_receipt` moved four sites
#: out of the count by moving them into `lines.append(...)`, and the count fell
#: while the exposure did not. Following the list to its render is the precise
#: answer; counting every append is the one that cannot fail low, and low is
#: the direction that reads as progress.
WRITE_SINKS = frozenset({"write", "append", "extend"})

#: One probe per shape the scan recognises, so the disclosure below cannot go
#: stale: `test_every_declared_sink_shape_is_actually_detected` requires each
#: to be found. A shape listed but unmatched reads exactly like a shape with
#: no instances, which is this repo's defect class pointed at its own guard.
SHAPE_PROBES = {
    "print(...)": "    print(f'x: {r.stderr}')",
    "return <expr>": "    return f'x: {r.stderr}'",
    "<obj>.write(...)": "    sys.stderr.write(f'x: {r.stderr}')",
    "<obj>.append(...)": "    lines.append(f'x: {r.stderr}')",
    "<obj>.extend(...)": "    lines.extend([f'x: {r.stderr}'])",
}

#: What the census is a census *of*. A zero from it means "none of these", and
#: never "none" — the scan pattern-matches sink shapes, it does not derive
#: every path from remote bytes to a rendered line (#1626).
SINK_SHAPES = tuple(SHAPE_PROBES)

#: Child-stream values that flow into a call this scan does not model at all —
#: a helper, a formatter, a `join`. **Not a defect count.** It is the size of
#: the population the number above says nothing about, and it is published so
#: that a green from this file is a bounded claim rather than an unbounded one
#: (#1570). Asserted exact in both directions: narrowing the scan lowers it and
#: fails, so the ratchet cannot be quietly made cheaper to pass.
#:
#: Disjoint from `CENSUS` by construction — see `_accounted_for`. A first cut
#: counted 246 by also counting calls *inside* a sink's arguments and on the
#: right of an assignment, both of which the model already speaks for; padding
#: the bound with sites the census reports by name makes it unreadable in the
#: one direction it exists to be read.
#:
#: 104 -> 105 on 2026-08-14, and the arithmetic is worth keeping because the
#: ratchet did its job. #1636 measured 104 against a tree that did not yet
#: contain #1632's `upstream_refs`; both PRs were green on their own
#: merge-bases, and `master` went red the moment the second landed - the #1257
#: shape, which `gh-pr-merge` discloses as `BEHIND: master is N commits ahead`
#: on every merge. The added site is `presets/git/worktrees.py:719`,
#: `for line in _untrusted.split_lines(res.stdout)`: a loop over a child
#: stream, already flattened, and unmodelled by this scan rather than
#: unhandled by the product. Re-baselined after locating it, never before.
#:
#: 105 -> 106 on 2026-08-14 (#1648). The added site is
#: `presets/github/issue.py`'s `_print_linked_prs`, whose decline reason is now
#: `_untrusted.split_lines(result.stderr.strip())[:1]` handed to
#: `_linked_prs_unknown(...)` — a child stream inside a call's arguments, which
#: `_accounted_for` deliberately does not model, and flattened at the seam it
#: reaches. Located before the number moved, never after.
#: 106 -> 108 on 2026-08-14 (#1654). Two added sites, both the same shape as
#: the one #1636 located: `for line in _untrusted.split_lines(<stream>)` — a
#: child stream inside a call's arguments, in a `for` iterable rather than an
#: assignment, so no name carries the taint onward and `_accounted_for` does
#: not model it. They are `presets/gitlab/mr.py`'s `_glab_fail_detail` and
#: `presets/git/worktrees.py`'s `remote_branch_names`. The branch's four other
#: narrowings (`gitlab/issue.py`, `gitlab/mr.py::_get_conflict_hunks`,
#: `git/merge.py`, and `worktrees.py`'s stderr decline) bind the result to a
#: name and did not move the number. Located by differencing the per-file tally
#: against the merge-base, before the number moved — never after.
#:
#: 108 -> 111 on 2026-08-14 (#1681). Three added sites, all the same shape as
#: the four above — `for line in _untrusted.split_lines(<stream>)`, a child
#: stream inside a call's arguments in a `for` iterable, so no name carries the
#: taint onward and `_accounted_for` does not model it. They are
#: `presets/git/checkout.py` (+1, the `log -3` render) and
#: `presets/git/status.py` (+2, the `for-each-ref` and `log -5` renders). The
#: branch's other seven narrowings bind to a name and did not move it. Located
#: by differencing the per-file tally against the merge-base, before the number
#: moved.
#:
#: 111 -> 113 on 2026-08-15 (#1724). Both added sites are the two arguments of
#: ONE `zip(untracked if full else untracked[:10], timed)` in
#: `presets/git/status.py::main` — `zip` is not a call this scan models, and
#: neither argument binds a name that carries the taint onward, so the pair is
#: unresolved by construction rather than unhandled by the product. What the
#: loop then prints is the porcelain path byte-identically to what git wrote,
#: which is the ground `tests/test_preset_git_splitlines_register_1130.py`
#: records for `status.py::main`. Located by differencing this file's own
#: `unresolved_escapes` against the merge-base, before the number moved.
#:
#: 113 -> 109 on 2026-08-22 (#1918), all three from `_unmarked`'s per-value
#: rewrite reading a comprehension's `ifs` the way it now reads `IfExp.test`:
#: a filter condition decides which items pass, it never becomes one of them.
#: `presets/git/commit.py:1155` (-2, both arguments of `_add_failure_lines`):
#: `to_add = [p for p in paths if p not in staged_deletions]` — the old blind
#: walk found `staged_deletions` (genuinely raw) in the filter and tainted
#: `to_add` with it; `_comprehension_keys` does not walk `ifs`, so `to_add`
#: (and `add`, built from it) are read correctly as never touching
#: `staged_deletions`'s own bytes. `presets/github/job.py:1140` and
#: `presets/gitlab/job.py:899` (-1 each, the same site in each preset's
#: `_emit_grep_hits` caller): `match_count = sum(1 for line in lines if
#: rx.search(line))` — the produced value is the literal `1` per match,
#: never `line`; the old walk read `lines` off the generator's `iter` (the
#: same shape `checkout.py`'s CENSUS note describes for a `sum(1 for … in
#: …)` count) and tainted an int with it. Located by differencing this
#: file's own `unresolved_escapes` against the merge-base, before the
#: number moved.
UNRESOLVED = 109

#: Calls whose result cannot be a string, so the taint stops there. A type
#: argument, not an allowlist: `json.loads(r.stdout)` yields a dict, and every
#: field read back off it is the *other* scanner's question (#965).
#:
#: `mentions_gitlab_token` is the one entry here that is not a builtin. It
#: returns `bool` -- `_secrets.py` -- and the four `gl-*` `_format_error`
#: classifiers hand it a lowercased `stderr` to decide one branch (#1645). The
#: taint provably stops at the call, so counting four such sites in
#: `UNRESOLVED` would pad a bound with sites that are not unresolved, which
#: the note above that number says a bound may not be.
#:
#: `says_not_authenticated` is the second, for exactly that argument and no
#: other. It returns `bool` -- `presets/_auth_probe.py`, whose whole body is
#: `any(marker in low for marker in markers)` -- and #1846 gave it 23 call
#: sites across `presets/github/` and `presets/gitlab/`, each handing it a
#: `stderr` to decide one branch. Left out of this set they raised `UNRESOLVED`
#: 113 -> 132, which would have been 19 sites where the taint provably stops
#: added to a number documented as *what this scan cannot see*. The census
#: (`raw_child_stream_sinks`) did not move, and neither did `UNRESOLVED` once
#: the entry was added -- both checked before the number was touched.
#:
#: `says_not_found` and `says_forbidden` are the third and fourth, the same
#: shape one scope over: `presets/_status_probe.py` (#1864) is the same
#: `any(marker in low for marker in markers)` body, and the fix that put a
#: not-found or forbidden reading behind a predicate call rather than a bare
#: `"404" in s` / `"403" in s` gave it 32 call sites across the same two
#: directories plus `presets/_declared_workflows.py`. Left out of this set
#: `UNRESOLVED` rose 113 -> 143 for the identical reason: 30 sites where the
#: taint provably stops (two more read `err` directly with no lowering
#: assignment for this scan to lose) added to a number documented as what
#: this scan cannot see. The census did not move, and neither did
#: `UNRESOLVED` once these two entries were added -- both checked before the
#: number was touched.
NOT_TEXT = frozenset({"loads", "int", "float", "len", "bool",
                      "mentions_gitlab_token", "says_not_authenticated",
                      "says_not_found", "says_forbidden"})

_SCANNED = ("presets/github", "presets/gitlab", "presets/git")

#: file -> how many child-stream relays reach a sink unmarked. May only shrink.
CENSUS = {
    # +2, #1693: `_git_verbatim`'s two `done.std*.decode(...)`. This ratchet is
    # allowed to rise only with a reason in the diff, and the reason is that
    # these two are not renders — they are the TRANSPORT, the same expression
    # `_git` gets for free because `text=True` does its decoding inside
    # `subprocess`. Flattening either would corrupt every stream every caller
    # in this package reads. The sweep matches a shape (`return <expr>` carrying
    # a child stream) and cannot tell a decode from a print, which is exactly
    # the disclosure the count exists to make rather than hide.
    "presets/git/_git_common.py": 7,
    "presets/git/blame.py": 2,
    # 13 -> 10, #1918: not a MARKS site at all — the per-value rewrite of
    # `_unmarked` replaced `_streams_in`'s blind whole-subtree walk with one
    # that reads a comprehension's *value* off `elt`, never `iter`, and that
    # precision removed these three as a side effect. `staged`/`unstaged`/
    # `untracked` at checkout.py:302/304/306 are each `sum(1 for l in lines if
    # …)` — the produced value is the literal `1` counted per match, never `l`
    # itself, so no byte of `lines` (itself genuinely raw, still counted at
    # :238/:248/:271/:273) reaches these three prints. The old walk could not
    # tell "iterates over a raw list" from "the raw list is what gets printed"
    # and counted both the same.
    "presets/git/checkout.py": 10,  # +3, #1626: three `.write` / `.append` sinks
    "presets/git/commit.py": 10,  # -1, #1858: the git-dir read moved to
    # `_git_common.probe_repo`, which flattens it. One site, not zero: the
    # value did not stop existing, it stopped being raw HERE. The seam is where
    # this file's own opening paragraph says the fix belongs.
    "presets/git/conflicts.py": 2,
    "presets/git/diff.py": 2,  # -2, #1569: both `Repo:` renders -> repo_label()
    "presets/git/diverge.py": 3,
    # -2, #1693: the blame render's `author` and `content` now go through
    # `visible()` — one is git's relay of a commit's author field, the other is
    # the blamed file's own line, and both land in a table.
    "presets/git/investigate.py": 2,
    "presets/git/merge.py": 9,  # -1, #1654: `_fresh_merge_ref`'s fetch stderr
    # 32 -> 31, #1918: `_incoming_commits`'s `return incoming, len(incoming),
    # ahead` at push.py:2399 — not the `incoming` list (already safe, the
    # `visible()`-wrapped comprehension `checkout.py`'s note describes) but
    # `ahead = int(mine.stdout.strip()) if mine.returncode == 0 and
    # mine.stdout.strip().isdigit() else 0`. The old blind walk read
    # `mine.stdout` out of the ternary's *test* — used only to decide whether
    # the parse is attempted, never part of the value — and tainted `ahead`
    # with it. `_unmarked` now skips `IfExp.test` (see its docstring), so
    # `ahead`, always genuinely an int, stops being counted as raw `stdout`.
    "presets/git/push.py": 31,  # -1, #1681: `_discarded_by_force`'s log relay
    # 0, not 3: the two `failed.append` relays and the direct print #1638 named
    # are flattened, and the four sites #1626's widening had also disclosed here
    # went with them (they are the same two relays' other arm plus the two
    # dict keys the scan counts beside them).
    "presets/git/resolve.py": 0,  # -6, #1638
    # 18 -> 20, #1724. Two added sites, both LOCATED before the number moved by
    # differencing this file's own `_scan` against the merge-base:
    #   * `status.py::_worktree_root`'s `return top.stdout.strip()` — `git
    #     rev-parse --show-toplevel`. It is a `return <expr>` and so a sink by
    #     this scan's model, but the value is never rendered: its one consumer
    #     joins it onto a path and hands it to `os.stat`. Flattening it would
    #     make it stop opening, which is the case #1557 already argues about
    #     `repo_label()` — a display string is not an openable path. The scan
    #     cannot see "never printed", which is exactly what `UNRESOLVED`'s note
    #     says a number from here does not claim.
    #   * the `... (N more, newest of them written Xs ago)` marker, whose
    #     `extra` traces back to the `--porcelain` read through `timed`. Every
    #     value it interpolates is a FLOAT age formatted by `_age` as an
    #     integer and a unit letter; no byte off any child stream reaches that
    #     string. Marking it with `_untrusted.flat` would satisfy the scan by
    #     flattening a number, which is routing around the guard rather than
    #     answering it.
    # The untracked rows themselves did NOT move the number: the path is still
    # printed byte-identically to what git wrote (the quoting ground the #1130
    # register records), and `l` is a `for` target, which this scan does not
    # taint - as it did not before.
    # 20 -> 19, #1918: `_worktree_root`'s untracked-cap message at
    # status.py:866 traces to `known = [a for a, _w in cut if a is not None]`
    # — a tuple-unpacking comprehension target. The old blind walk credited
    # `cut`'s own taint to `known` regardless of the unpacking; `_comprehension
    # _keys` now applies the same "only a plain `Name` target carries taint
    # onward" rule `_accounted_for` already states for an ordinary assignment,
    # so `known`, then `extra`, then this print stop being counted — see
    # `_unmarked`'s docstring for why this shape has no `UNRESOLVED`
    # disclosure either. Checked by hand rather than by the scan: `a` is
    # `_untracked_age`'s `age` return, always a float or `None`, never text.
    "presets/git/status.py": 19,  # +3 #1626 (three `.append` sinks), +2 #1724
    "presets/git/trail.py": 1,  # -3, #1681: two log renders and _format_error
    "presets/git/worktrees.py": 5,  # -1, #1654: the third `for-each-ref` decline
    "presets/github/_release_gate.py": 2,
    "presets/github/batch_follow.py": 1,
    "presets/github/batch_star.py": 1,
    "presets/github/branch.py": 3,  # -1, #1606: the _format_error relay
    # #1606 fixed check.py's seam and this row did not move: a false positive
    # one line up (`kind = _gh_error_kind(r.stderr)`, reaching a bool
    # comparison) took the fixed site's place in the count (#1626).
    "presets/github/check.py": 1,
    "presets/github/find_starable.py": 0,  # -1, #1648: the `bad JSON` dump
    "presets/github/following.py": 0,  # -1, #1648: the `bad JSON` dump
    "presets/github/issue.py": 1,  # -1, #1648: the invalid-JSON body dump
    "presets/github/issue_create.py": 0,  # -1, #1648: the `url=` fallback arm
    "presets/github/issues.py": 0,  # -2, #1606: the lookup and list relays
    "presets/github/job.py": 4,  # -1, #1606: the _format_error relay
    "presets/github/labels.py": 1,  # -1, #1606: the _format_error relay
    "presets/github/pr.py": 2,  # -4, #1648: both body dumps, both threads relays
    "presets/github/pr_create.py": 2,  # -1, #1648: `_gh_json`'s error selection
    "presets/github/pr_merge.py": 3,  # -2, #1648: `_gh_json`'s error selection
    "presets/github/prs.py": 1,  # -1, #1606: the list-failure relay
    "presets/github/run.py": 1,  # -1, #1648: the invalid-JSON body dump
    "presets/github/starred.py": 0,  # -1, #1648: the `bad JSON` dump
    "presets/gitlab/api.py": 1,
    "presets/gitlab/issue.py": 3,  # +1, #1626: `f.write(result.stdout)`
    # `presets/gitlab/issue_create.py` was 2 and is 0 (#1654) — both arms of
    # the `url=` receipt. Its row is gone rather than zeroed, which is what
    # drops the file count from 34 to 33; `presets/github/issue_create.py`
    # keeps a `: 0` row from #1648 and the asymmetry is only that one file
    # still has a scanned site the model resolves and the other does not.
    "presets/gitlab/job.py": 1,
    "presets/gitlab/mr.py": 3,
    # 4 -> 3, #1918: the noise-count summary at pipeline.py:200 traces through
    # `summary = ", ".join(f"+{n} {status}" for status, n in
    # sorted(counts.items()))` — the same tuple-target comprehension shape
    # `status.py`'s `known` note describes, and the same `_unmarked` docstring
    # blind spot: no `UNRESOLVED` disclosure for it either. Checked by hand:
    # `status`/`n` are `Counter.items()`'s own keys/counts, never the tainted
    # `hidden` list `counts` was built from.
    "presets/gitlab/pipeline.py": 3,
    "presets/gitlab/runners.py": 2,
}

_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def _walk_text(node: ast.AST):
    """`ast.walk`, but never into a call whose result cannot be a string."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Call):
            fn = n.func
            name = (fn.attr if isinstance(fn, ast.Attribute)
                    else getattr(fn, "id", None))
            if name in NOT_TEXT:
                continue
        yield n
        stack.extend(ast.iter_child_nodes(n))


def _streams_in(node: ast.AST, tainted: dict) -> set:
    keys = set()
    for sub in _walk_text(node):
        if isinstance(sub, ast.Attribute) and sub.attr in STREAM_ATTRS:
            keys.add(sub.attr)
        elif isinstance(sub, ast.Name) and sub.id in tainted:
            keys.add(tainted[sub.id])
    return keys


#: A comprehension's own value is its `elt` (or `key`/`value` for a dict) —
#: never a generator's `iter` or its `ifs`. Handled in `_comprehension_keys`.
_COMP_TYPES = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)


def _unmarked(node: ast.AST, tainted: dict) -> set:
    """Per-value: a MARKS call clears only the argument(s) it wraps.

    Until #1918 this gated on whether ANY `MARKS` name appeared ANYWHERE
    in `node` (`MARKS & _call_names(node)`, `_call_names` walking the
    whole subtree) and returned the empty set for the whole node when it
    did — so `print(f'{raw_a} {raw_b} {_untrusted.flat(marked)}')` scored
    zero, discarding `raw_a` and `raw_b` along with the one value that was
    actually marked.

    A mark now only removes the streams reachable *through its own
    arguments*: walking stops at a MARKS call the same way `_walk_text`
    already stops at a NOT_TEXT call — the value inside is not descended
    into, but nothing outside that call's own subtree is touched by it.

    Two node shapes get their own rule rather than a blind walk, because a
    blind walk over them is exactly what would have turned this fix into
    the census-inflating one the issue warned against — every
    `_untrusted.flat(tail[-1]) if tail else "…"` guard in this codebase
    (five of the new census entries this fix first produced) walks `tail`
    twice: once as the boolean condition, once — safely, under `flat` — as
    the value. A blind walk cannot tell those apart and would have scored
    the condition as a second, unmarked read of the same stream.

    * `IfExp.test` is a condition, never the value: `X if cond else Y`
      evaluates to `X` or `Y`, and `cond`'s only role is which one. A
      tainted name read there is not read into the result, so it must not
      taint the expression the way a read in `X`/`Y` would.
    * A comprehension's `elt` (or a dict's `key`/`value`) is what actually
      populates the result; `iter` only says which items are bound. Each
      loop target is tainted inside `elt` exactly when its own `iter` was
      — see `_comprehension_keys` — so `[_untrusted.visible(l) for l in
      _untrusted.split_lines(r.stdout)]` scores clean (`l` is marked
      before it reaches `elt`) while `[l for l in
      _untrusted.split_lines(r.stdout)]` still does not (nothing marks the
      loop variable before it becomes the list's own content).

    What this still cannot see, stated rather than silently assumed:

    * A mark applied to one name and then that same raw value read again
      under a second, untracked name — the same blind spot `_streams_in`
      already has for any alias this scan does not follow through
      `tainted`. It is not a new gap; per-value marking does not widen it.
    * A comprehension whose loop target is a tuple, e.g. `[a for a, _w in
      cut]` — `_comprehension_keys` does not taint `a`/`_w` from `cut`,
      the same "only a plain `Name` target carries taint onward" rule
      `_accounted_for` already states for a top-level `text, code =
      _render(...)`. Unlike that top-level case, there is no `ast.Call`
      node here for `_escapes_from` to hang an `UNRESOLVED` disclosure
      on, so a raw value reaching a sink this way would leave silently.
      Both live instances of this shape were checked by hand rather than
      by the scan: `status.py`'s `known` keeps only `_untracked_age`'s
      `age` float and drops `_w`; `pipeline.py`'s `summary` reads
      `status`/`n` off `Counter.items()`, never the tainted dict passed
      through `iter` directly. A comprehension shaped this way earns its
      own disclosure mechanism if one is ever found to matter; this scan
      does not have one yet.
    """
    keys = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Call):
            fn = n.func
            name = (fn.attr if isinstance(fn, ast.Attribute)
                    else getattr(fn, "id", None))
            if name in MARKS or name in NOT_TEXT:
                continue
        if isinstance(n, ast.IfExp):
            stack.append(n.body)
            stack.append(n.orelse)
            continue
        if isinstance(n, _COMP_TYPES):
            keys |= _comprehension_keys(n, tainted)
            continue
        if isinstance(n, ast.Attribute) and n.attr in STREAM_ATTRS:
            keys.add(n.attr)
        elif isinstance(n, ast.Name) and n.id in tainted:
            keys.add(tainted[n.id])
        stack.extend(ast.iter_child_nodes(n))
    return keys


def _comprehension_keys(node: ast.AST, tainted: dict) -> set:
    """The streams a comprehension's *value* carries — its `elt`, or its
    `key`/`value` for a `DictComp` — never its generators' `iter`/`ifs`
    directly. A loop target is tainted inside that value exactly when the
    `iter` it is bound from was, the same rule an assignment already gets.
    """
    local = dict(tainted)
    for gen in node.generators:
        iter_keys = _unmarked(gen.iter, tainted)
        if isinstance(gen.target, ast.Name):
            if iter_keys:
                local[gen.target.id] = sorted(iter_keys)[0]
            else:
                local.pop(gen.target.id, None)
    if isinstance(node, ast.DictComp):
        return _unmarked(node.key, local) | _unmarked(node.value, local)
    return _unmarked(node.elt, local)


def _is_sink(node: ast.AST) -> bool:
    """`print`, a bare `return`, and the method calls in `WRITE_SINKS`.

    Keying on `print` alone certified `push._open_mr_line`, whose caller does
    the printing one frame up; #1038 learnt that on the sibling scanner. Adding
    `return` was not enough either: `sys.stderr.write` and `lines.append` are
    both renders, and both scored zero until #1626.
    """
    if isinstance(node, ast.Call):
        fn = node.func
        if isinstance(fn, ast.Name):
            return fn.id == "print"
        if isinstance(fn, ast.Attribute):
            return fn.attr in WRITE_SINKS
    return isinstance(node, ast.Return) and node.value is not None


def _sink_args(node: ast.AST) -> list:
    if isinstance(node, ast.Call):
        return list(node.args)
    return [node.value]


def _scopes(tree: ast.AST) -> list:
    """Every function body, plus module level, each with its own taint dict."""
    out = [f for f in ast.walk(tree) if isinstance(f, _FUNC)]
    inner = {id(n) for f in out for n in ast.walk(f) if n is not f}
    out.append(tree)
    return [(s, inner if s is tree else set()) for s in out]


def _params(scope: ast.AST) -> dict:
    """A parameter literally named `stdout`/`stderr` arrives tainted.

    `pr._format_error(stderr, resource, identifier)` is the shape the seven
    named sites had: the attribute read happens at the caller, and the raw
    interpolation happens here. Without this the scan reported neither.
    """
    if not isinstance(scope, _FUNC):
        return {}
    a = scope.args
    names = list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
    return {x.arg: x.arg for x in names if x.arg in STREAM_ATTRS}


def _escapes_from(path: Path, node: ast.Call, tainted: dict) -> list:
    """A stream argument to a call this scan does not model — the third state.

    Not a finding: the callee may flatten, may parse, may never render. Not
    silence either, which is what returning nothing here would have been.

    Keyed by position rather than counted, because `_scopes` hands a nested
    function to both its own scope and its parent's; the sink list is deduped
    for the same reason.
    """
    fn = node.func
    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
    if name in MARKS or name in NOT_TEXT:
        return []
    return [f"{path.name}:{node.lineno}:{node.col_offset}:{i}"
            for i, arg in enumerate(node.args) if _streams_in(arg, tainted)]


def _accounted_for(nodes: list) -> set:
    """Every node the census or the taint model already speaks for.

    A call inside a sink's arguments is reported by name in `CENSUS`; a call on
    the right of an assignment hands its taint to the target and stays tracked.
    Neither has left the model, so neither belongs in `UNRESOLVED` — a bound
    padded with sites the census already names is not a bound.

    The assignment only counts when a target is a plain `Name`, because that is
    the only target `_scan_scope` taints. `text, code = _render(r.stderr)`
    keeps nothing, so the taint really did leave and the escape stands.
    """
    covered = set()
    for node in nodes:
        if _is_sink(node):
            for arg in _sink_args(node):
                covered.update(id(n) for n in ast.walk(arg))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            if any(isinstance(t, ast.Name) for t in targets):
                covered.update(id(n) for n in ast.walk(node.value))
    return covered


def _scan_scope(path: Path, scope: ast.AST, skip: set) -> tuple:
    # Source order, not `ast.walk` order: a name is tainted or cleaned by the
    # last assignment *above* the sink.
    nodes = sorted((n for n in ast.walk(scope) if id(n) not in skip),
                   key=lambda n: (getattr(n, "lineno", 0),
                                  getattr(n, "col_offset", 0)))
    accounted = _accounted_for(nodes)
    tainted = _params(scope)
    found = []
    escaped = []
    for node in nodes:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            keys = _unmarked(node.value, tainted)
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if keys:
                    tainted[target.id] = sorted(keys)[0]
                else:
                    tainted.pop(target.id, None)
        if not _is_sink(node):
            if isinstance(node, ast.Call) and id(node) not in accounted:
                escaped.extend(_escapes_from(path, node, tainted))
            continue
        for arg in _sink_args(node):
            for key in sorted(_unmarked(arg, tainted)):
                found.append(f"{path.name}:{node.lineno} {key}")
    return found, escaped


def _scan(path: Path) -> tuple:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list = []
    escaped: list = []
    for scope, skip in _scopes(tree):
        hits, out = _scan_scope(path, scope, skip)
        found.extend(hits)
        escaped.extend(out)
    return sorted(set(found)), sorted(set(escaped))


def raw_child_stream_sinks(path: Path) -> list:
    return _scan(path)[0]


def unresolved_escapes(path: Path) -> int:
    """How many child-stream values left this scan's model in `path`."""
    return len(_scan(path)[1])


def _measure() -> dict:
    counts = {}
    for directory in _SCANNED:
        for path in sorted((_ROOT / directory).rglob("*.py")):
            n = len(raw_child_stream_sinks(path))
            if n:
                counts[f"{directory}/{path.name}"] = n
    return counts


def test_no_new_child_stream_reaches_a_receipt_raw() -> None:
    """The ratchet. Every number may fall; none may rise; none may appear."""
    counts = _measure()
    grew = sorted(f"  {k}: {CENSUS.get(k, 0)} -> {v}"
                  for k, v in counts.items() if v > CENSUS.get(k, 0))
    assert not grew, (
        "a child process's stream reaches a receipt at column 0 in a place it "
        "did not before (#1475). Route it through `presets/_untrusted` — "
        "`flat` for a field on a line the tool owns, `visible(keep=tab)` per "
        "line for a relayed transcript — rather than raising the number:"
        + chr(10) + chr(10).join(grew))


def test_the_census_shrinks_rather_than_going_stale() -> None:
    """A fixed site must be written down, or the ratchet stops ratcheting.

    Split from the test above so a burn-down PR fails on *this* one, which
    says what to do, instead of on the guard that says something got worse.
    """
    counts = _measure()
    shrank = sorted(f"  {k}: {CENSUS[k]} -> {counts.get(k, 0)}"
                    for k in CENSUS if counts.get(k, 0) < CENSUS[k])
    assert not shrank, (
        "sites were fixed and CENSUS still claims the old number — write the "
        "smaller one down in tests/test_forged_child_stream_line_1475.py:"
        + chr(10) + chr(10).join(shrank))


def test_the_scanner_sees_the_defect_it_was_written_for(tmp_path: Path) -> None:
    """A scanner that cannot fail is not a guard (#851's own lesson)."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def render(r):" + chr(10)
        + "    print(f'ERROR: {r.stderr.strip()}')" + chr(10)
        + "    combined = (r.stdout or '') + (r.stderr or '')" + chr(10)
        + "    print(combined.strip())" + chr(10)
        + "    safe = _untrusted.flat(r.stderr)" + chr(10)
        + "    print(f'ok: {safe}')" + chr(10)
        + "    n = int(r.stdout)" + chr(10)
        + "    print(f'n: {n}')" + chr(10)
        + "def relay(stderr):" + chr(10)
        + "    return f'ERROR: {stderr.strip()}'" + chr(10),
        encoding="utf-8",
    )
    found = raw_child_stream_sinks(sample)
    lines = {int(f.split(":")[1].split()[0]) for f in found}
    assert lines == {2, 4, 10}, found


def test_a_mark_on_one_value_does_not_clear_its_siblings(tmp_path: Path) -> None:
    """#1918: `_unmarked` used to gate on `MARKS & _call_names(whole_node)` —
    any `_untrusted.flat(...)` anywhere in a sink node cleared every raw
    stream in that same node, marked or not. PR #1913 inlined `flat()` into
    one value of a `print` that also carried `mr_state` and `pipe_status`
    raw, and both vanished from the census with nothing fixed.

    A mark call only clears the value it wraps: `mr_state` and
    `pipe_status` are raw streams the sink still carries, `mr_target` is
    the one value actually marked.
    """
    sample = tmp_path / "partial.py"
    sample.write_text(
        "def render(r):" + chr(10)
        + "    mr_state = r.stdout" + chr(10)
        + "    pipe_status = r.stderr" + chr(10)
        + "    mr_target = r.stdout" + chr(10)
        + "    print(f'{mr_state} {pipe_status} "
        + "{_untrusted.flat(mr_target)}')" + chr(10),
        encoding="utf-8",
    )
    found = raw_child_stream_sinks(sample)
    assert len(found) == 2, found


def test_the_same_sink_with_only_the_marked_value_counts_zero(
        tmp_path: Path) -> None:
    """The pair `test_a_mark_on_one_value_does_not_clear_its_siblings`
    needs: with the raw siblings gone, only the marked value remains and
    the sink must count nothing. Without this pair a scanner that counts
    every stream unconditionally — ignoring marks altogether — would also
    pass the first test."""
    sample = tmp_path / "marked_only.py"
    sample.write_text(
        "def render(r):" + chr(10)
        + "    mr_target = r.stdout" + chr(10)
        + "    print(f'{_untrusted.flat(mr_target)}')" + chr(10),
        encoding="utf-8",
    )
    assert raw_child_stream_sinks(sample) == []


# ---------------------------------------------------------------------------
# what the census is a census *of* (#1626, #1570)
# ---------------------------------------------------------------------------


def _probe(tmp_path: Path, body: str) -> list:
    sample = tmp_path / "probe.py"
    sample.write_text("import sys" + chr(10)
                      + "def render(r, lines):" + chr(10) + body + chr(10),
                      encoding="utf-8")
    return raw_child_stream_sinks(sample)


def test_every_declared_sink_shape_is_actually_detected(tmp_path: Path) -> None:
    """A shape listed but unmatched reads exactly like a shape with no sites.

    #1606 paid for this: four of the fifteen relays it fixed went through
    `sys.stderr.write`, which the scan did not model, so they scored 0 both
    before and after the fix and the ratchet stayed green across them.
    """
    for shape, probe in SHAPE_PROBES.items():
        found = _probe(tmp_path, probe)
        assert len(found) == 1, (shape, probe, found)
    assert tuple(SHAPE_PROBES) == SINK_SHAPES, (
        "a shape is declared with no probe, or probed without being declared")


def test_an_unmodelled_escape_is_disclosed_rather_than_read_as_clean(
        tmp_path: Path) -> None:
    """The third state. The scan cannot follow a stream into a helper call, so
    it must say so rather than return the shape of a clean result."""
    sample = tmp_path / "escape.py"
    sample.write_text("def render(r):" + chr(10)
                      + "    _emit(r.stderr)" + chr(10), encoding="utf-8")
    assert raw_child_stream_sinks(sample) == [], "not a finding"
    assert unresolved_escapes(sample) == 1, "and not silence either"


def test_a_site_the_census_already_counts_is_not_also_disclosed(
        tmp_path: Path) -> None:
    """UNRESOLVED is what the census does *not* see, so it may not overlap it.

    `print(_helper(r.stderr))` is already a finding, and an assignment keeps
    the taint tracked. Counting either as unresolved too would inflate the
    disclosure with sites the census reports by name — a bound that is loose
    for a reason nobody can name is not a bound.
    """
    sample = tmp_path / "overlap.py"
    sample.write_text("def render(r):" + chr(10)
                      + "    blob = _helper(r.stderr)" + chr(10)
                      + "    print(_fmt(blob))" + chr(10), encoding="utf-8")
    assert len(raw_child_stream_sinks(sample)) == 1, "the print is a finding"
    assert unresolved_escapes(sample) == 0, (
        "both calls are accounted for: one carries the taint into a tracked "
        "name, the other sits inside a sink the census already counts")


def test_a_tuple_target_does_not_launder_the_escape(tmp_path: Path) -> None:
    """`text, code = _render(r.stderr)` keeps no name tainted, so the taint
    really did leave — four live sites have this shape (`pr.py:857`,
    `push.py:2349` and `:2352`, `status.py:765`)."""
    sample = tmp_path / "tuple.py"
    sample.write_text("def render(r):" + chr(10)
                      + "    text, code = _render(r.stderr)" + chr(10)
                      + "    return code" + chr(10), encoding="utf-8")
    assert raw_child_stream_sinks(sample) == [], "nothing reaches a sink"
    assert unresolved_escapes(sample) == 1, (
        "an assignment only accounts for a stream when some target actually "
        "carries the taint onward")


def test_the_disclosed_escape_count_is_the_measured_one() -> None:
    """Exact in both directions: narrowing the scan lowers it and fails."""
    got = sum(unresolved_escapes(p)
              for d in _SCANNED
              for p in sorted((_ROOT / d).rglob("*.py")))
    assert got == UNRESOLVED, (
        "the number of child-stream values that flow into a call this scan "
        "does not model has changed. It is not a defect count - it is the "
        "size of what a zero from this scan says nothing about. Write the new "
        "number down in UNRESOLVED: " + f"{UNRESOLVED} -> {got}")


#: `N sites in M files`, wherever the prose below publishes the total.
_TOTAL_PHRASE = re.compile(r"(\d+) (?:candidate )?sites in (\d+) files")

#: Every live document that publishes this sweep's total. `CHANGELOG.md` is
#: deliberately absent: it records what a release shipped, including the number
#: that release shipped wrong, and rewriting it would falsify the record.
_PUBLISHES_THE_TOTAL = ("tests/test_forged_child_stream_line_1475.py",
                        "docs/presets/git.md")


def test_the_published_total_is_the_measured_one() -> None:
    """#1570: the docstring said 174 across 36 while `_measure()` said 172.

    Both numbers were typed, twice each, in prose nothing read. `docs/presets/
    git.md` had a third copy that was wrong by twenty by the time this ran.
    """
    counts = _measure()
    want = (sum(counts.values()), len(counts))
    for rel in _PUBLISHES_THE_TOTAL:
        src = (_ROOT / rel).read_text(encoding="utf-8")
        got = [(int(a), int(b)) for a, b in _TOTAL_PHRASE.findall(src)]
        assert got, (
            f"{rel} stopped publishing a total, so this check went blind "
            "there — remove it from _PUBLISHES_THE_TOTAL deliberately or put "
            "the number back")
        assert set(got) == {want}, (
            f"{rel} publishes a total the sweep does not measure. "
            f"measured {want}, prose says {sorted(set(got))}")
