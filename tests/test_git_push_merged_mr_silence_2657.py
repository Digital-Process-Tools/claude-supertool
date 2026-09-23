"""#2657 -- `git-push` said nothing when the branch's MR was merged, and the
silence read as "nothing to see" rather than "nobody's fetching this branch".

Observed: a one-commit push to `feat/criteria-search-lot5`, whose MR !35214
had already been merged. The push succeeded, the receipt printed nothing
about an MR (the OPEN lookup genuinely found none, which is correct -- the
MR was merged, not open), and the only trace was the remote's own
`provenance UNKNOWN` stderr line, easy to skim past. A branch whose MR is
merged is a dead end that looks exactly like a brand new branch, and the
commits never reached master.

`query_open_mr_result` is unchanged -- it still answers "no open request"
correctly for a merged MR, and that is not the bug. The bug is what
`_post_push_advisories` did with that TRUE fact: printed nothing, the same
receipt as a branch that never had a request at all. This file pins the new
third branch: when the OPEN lookup answers "none", a second query
(`query_last_mr_result`) asks what the branch's MOST RECENT request was,
whatever its state, and the receipt says which of the two is true.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest import mock

_COMMON = Path(__file__).parent.parent / "presets" / "git" / "_git_common.py"
_cspec = importlib.util.spec_from_file_location("_git_common_2657", _COMMON)
assert _cspec is not None and _cspec.loader is not None
common = importlib.util.module_from_spec(_cspec)
# Registered under the SHARED "_git_common" key only long enough for push.py's
# own `from _git_common import ...` below to bind to THIS isolated instance --
# never left there. A prior version of this setup left the shared key pointing
# at `common` for the rest of the test session: any test file collected AFTER
# this one (alphabetically, "test_git_push_merged_mr_silence_2657.py" sorts
# after "test_git_push.py") then found `sys.modules["_git_common"]` rebound to
# THIS module, not the one `test_git_push.py`'s own `push` had already bound
# its `st_hint`/`install_dir` imports to at ITS OWN collection time -- so a
# `monkeypatch.setattr(sys.modules["_git_common"], "install_dir", ...)` in
# that other file's tests silently patched an object no production call path
# reads, and `test_advisories_mergeability_warn` asserted against the REAL,
# unpatched `install_dir()` instead (#2664 CI red, all 12 legs, not
# reproducible from this file alone or from test_git_push.py alone -- only
# from the combination, in collection order). Saving and restoring the prior
# value the moment this file's own setup is done closes that leak; only the
# shared "_git_common" key is ever read elsewhere (the "_git_common_2657"
# spec name passed to spec_from_file_location above is never itself a
# sys.modules key -- nothing outside this file looks it up).
_PRIOR_GIT_COMMON = sys.modules.get("_git_common")
sys.modules["_git_common"] = common
try:
    _cspec.loader.exec_module(common)

    PRESET = Path(__file__).parent.parent / "presets" / "git" / "push.py"
    _spec = importlib.util.spec_from_file_location("git_push_2657", PRESET)
    assert _spec is not None and _spec.loader is not None
    push = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(push)
finally:
    if _PRIOR_GIT_COMMON is not None:
        sys.modules["_git_common"] = _PRIOR_GIT_COMMON
    else:
        sys.modules.pop("_git_common", None)

# Pins the restore itself, not just this file's own tests: without it, a
# regression here is only ever caught transitively, through
# test_git_push.py::test_advisories_mergeability_warn passing when the WHOLE
# suite is collected together -- exactly the failure mode #2664's CI hit, and
# a mode a per-file or -k run never exercises (this repro needed the whole
# tests/ directory collected in one process; #2664's own report to CI is the
# only evidence this bug ever surfaced on). This assertion is local and
# collection-order-independent: it only checks that THIS file did not leave
# the shared key different from what it found, regardless of what a sibling
# file collected before or after it does with that key.
assert sys.modules.get("_git_common") is _PRIOR_GIT_COMMON, (
    "this file must restore sys.modules['_git_common'] to what it found -- "
    "leaving it pointed at this file's own isolated `common` instance is "
    "the exact #2664 CI regression (a later-collected test's push.st_hint "
    "stays bound to whatever _git_common was cached at ITS OWN collection "
    "time, so a monkeypatch on the now-wrong sys.modules entry is invisible "
    "to it)"
)


def _proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


_MERGED_PR_JSON = (
    '[{"number": 35214, "baseRefName": "master", "state": "MERGED", '
    '"mergedAt": "2026-09-21T10:00:00Z", "closedAt": "2026-09-21T10:00:00Z"}]'
)
_CLOSED_UNMERGED_PR_JSON = (
    '[{"number": 40, "baseRefName": "master", "state": "CLOSED", '
    '"mergedAt": null, "closedAt": "2026-09-20T09:00:00Z"}]'
)


# --- unit level: query_last_mr_result ---------------------------------------


def test_query_last_mr_result_finds_a_merged_pr() -> None:
    with mock.patch.object(common, "which_excluding_cwd",
                            lambda n: "/usr/bin/gh" if n == "gh" else None), \
         mock.patch.object(common.subprocess, "run",
                           lambda *a, **k: _proc(0, stdout=_MERGED_PR_JSON)), \
         mock.patch.object(common, "_remotes_could_host_a_request",
                           return_value=(True, "")):
        res = common.query_last_mr_result("feat/criteria-search-lot5")

    assert res.answered is True
    assert res.mr is not None
    assert res.mr["iid"] == 35214
    assert res.mr["state"] == "merged"
    assert res.mr["merged_at"] == "2026-09-21T10:00:00Z"


def test_query_last_mr_result_finds_a_closed_unmerged_pr() -> None:
    with mock.patch.object(common, "which_excluding_cwd",
                            lambda n: "/usr/bin/gh" if n == "gh" else None), \
         mock.patch.object(common.subprocess, "run",
                           lambda *a, **k: _proc(0, stdout=_CLOSED_UNMERGED_PR_JSON)), \
         mock.patch.object(common, "_remotes_could_host_a_request",
                           return_value=(True, "")):
        res = common.query_last_mr_result("some-branch")

    assert res.answered is True
    assert res.mr is not None
    assert res.mr["state"] == "closed"
    assert res.mr["merged_at"] is None
    assert res.mr["closed_at"] == "2026-09-20T09:00:00Z"


def test_query_last_mr_result_an_empty_list_is_a_genuine_absence() -> None:
    """Positive control: a branch that never had ANY request must still
    answer cleanly, not be mistaken for a lookup that could not run."""
    with mock.patch.object(common, "which_excluding_cwd",
                            lambda n: "/usr/bin/gh" if n == "gh" else None), \
         mock.patch.object(common.subprocess, "run",
                           lambda *a, **k: _proc(0, stdout="[]")), \
         mock.patch.object(common, "_remotes_could_host_a_request",
                           return_value=(True, "")):
        res = common.query_last_mr_result("brand-new-branch")

    assert res.answered is True
    assert res.mr is None


def test_query_last_mr_result_glab_sends_all_not_state() -> None:
    """glab has no --state flag (#948) -- the same constraint applies here,
    and --all is glab's own documented opt-in to every state."""
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return _proc(0, stdout="[]")

    with mock.patch.object(common, "which_excluding_cwd",
                            lambda n: "/usr/bin/glab" if n == "glab" else None), \
         mock.patch.object(common.subprocess, "run", run), \
         mock.patch.object(common, "_remotes_could_host_a_request",
                           return_value=(True, "")):
        common.query_last_mr_result("feature/x")

    assert len(seen) == 1, seen
    argv = seen[0]
    assert "--state" not in argv, argv
    assert "--all" in argv, argv


# --- push.py's rendering of the third branch --------------------------------


def test_dead_mr_lines_names_the_merged_request() -> None:
    lookup = common.MrLookup(None)  # OPEN lookup answered: none open
    with mock.patch.object(push, "query_last_mr_result",
                           return_value=common.MrLookup({
                               "source": "github", "iid": 35214,
                               "target": "master", "state": "merged",
                               "merged_at": "2026-09-21T10:00:00Z",
                               "closed_at": "2026-09-21T10:00:00Z",
                           })):
        lines = push._dead_mr_lines(lookup, "feat/criteria-search-lot5")

    full = "\n".join(lines)
    assert "35214" in full, full
    assert "merged" in full, full
    assert "2026-09-21" in full, full
    assert "master" in full, full
    assert "NOT on master" in full, full


def test_dead_mr_lines_says_new_branch_when_nothing_ever_existed() -> None:
    lookup = common.MrLookup(None)
    with mock.patch.object(push, "query_last_mr_result",
                           return_value=common.MrLookup(None)):
        lines = push._dead_mr_lines(lookup, "brand-new-branch")

    full = "\n".join(lines)
    assert "new branch" in full, full
    assert "nothing tracking it" in full, full


def test_dead_mr_lines_degrades_to_unknown_not_a_false_new_branch_claim() -> None:
    """The second lookup can fail too -- and when it does, this must NOT
    collapse into "new branch, nothing tracking it", which is a positive
    claim the lookup never earned."""
    lookup = common.MrLookup(None)
    with mock.patch.object(push, "query_last_mr_result",
                           return_value=common.MrLookup(
                               None, "gh timed out after 5s")):
        lines = push._dead_mr_lines(lookup, "some-branch")

    full = "\n".join(lines)
    assert "UNKNOWN" in full, full
    assert "new branch" not in full, full


# --- the receipt, end to end through _post_push_advisories ------------------


def test_advisories_print_nothing_extra_when_there_is_an_open_mr(capsys) -> None:
    """Positive control: the common, healthy case must not grow a new line --
    _dead_mr_lines is reached only when lookup.mr is falsy."""
    lookup = common.MrLookup({
        "source": "github", "iid": 7, "target": "main",
        "pipeline": None, "pipeline_id": None, "pipeline_url": None,
        "merge_status": None,
    })
    with mock.patch.object(push, "_uncommitted_leftovers", return_value=(0, "")), \
         mock.patch.object(push, "query_last_mr_result") as qlast:
        push._post_push_advisories(lookup, set(), "origin", "feature/x")
    out = capsys.readouterr().out

    assert qlast.call_count == 0, (
        "an open MR was found -- the second, merged/closed lookup must never "
        "fire on the common path")
    assert "MR: none" not in out, out


def test_advisories_disclose_a_merged_mr_instead_of_staying_silent(capsys) -> None:
    """The #2657 shape itself: OPEN lookup answers none, and there WAS one."""
    lookup = common.MrLookup(None)
    with mock.patch.object(push, "_uncommitted_leftovers", return_value=(0, "")), \
         mock.patch.object(push, "query_last_mr_result",
                           return_value=common.MrLookup({
                               "source": "github", "iid": 35214,
                               "target": "master", "state": "merged",
                               "merged_at": "2026-09-21T10:00:00Z",
                               "closed_at": "2026-09-21T10:00:00Z",
                           })):
        push._post_push_advisories(lookup, set(),
                                   "origin", "feat/criteria-search-lot5")
    out = capsys.readouterr().out

    assert "MR: none open for this branch" in out, out
    assert "35214" in out, out
    assert "NOT on master" in out, out


def test_advisories_say_new_branch_when_genuinely_untracked(capsys) -> None:
    lookup = common.MrLookup(None)
    with mock.patch.object(push, "_uncommitted_leftovers", return_value=(0, "")), \
         mock.patch.object(push, "query_last_mr_result",
                           return_value=common.MrLookup(None)):
        push._post_push_advisories(lookup, set(), "origin", "brand-new")
    out = capsys.readouterr().out

    assert "MR: none -- new branch, nothing tracking it" in out, out


def test_advisories_do_not_raise_when_the_first_lookup_never_answered(capsys) -> None:
    """Positive control, restated for the new branch parameter: an
    UNANSWERED open-lookup must still short-circuit before the second
    query, exactly as it did before #2657."""
    lookup = common.MrLookup(None, "gh could not be run (ENOENT)")
    with mock.patch.object(push, "_uncommitted_leftovers", return_value=(0, "")), \
         mock.patch.object(push, "query_last_mr_result") as qlast:
        push._post_push_advisories(lookup, set(), "origin", "feature/x")
    out = capsys.readouterr().out

    assert qlast.call_count == 0, (
        "the OPEN lookup itself did not answer -- _mr_unknown_line already "
        "covers this, and the merged/closed lookup must not pile on")
    assert "UNKNOWN" in out, out
