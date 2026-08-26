"""`repo:OWNER/NAME` — name the repo a read op is about (#673).

Every `gh-*` read op derived its target from the cwd's git remote and offered no
override, so a repo you have not cloned — or one you have cloned somewhere the
call is not standing — was unreachable through the ops. `gh-issue-create`
already took a `repo` key in its payload, so the *vocabulary* existed in the
family and was simply missing on the read side.

Grammar chosen: a leading `repo:OWNER/NAME` op, parallel to `cwd:`, consumed in
a pre-pass and exported as ``SUPERTOOL_REPO`` for the preset subprocess. The
rejected alternative — a trailing `…:repo=OWNER/NAME` token — cannot be parsed
unambiguously in this family: `gh-job:ID:grep:PATTERN` takes an arbitrary regex
in that position, so `gh-job:5:grep:repo=x` is a legitimate log search that a
trailing-token scan would silently steal.

Two behaviours are load-bearing beyond "the flag reaches gh":

* A `repo:` that no op in the call can honour is **refused**, not ignored. A
  silently-dropped target is the failure mode this whole issue is about.
* Once a target exists, the old error — `cwd is not a GitHub repo` — is only
  honest when no target was given. With one, cwd is irrelevant and blaming it
  names a wall that now has a door.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import supertool

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(preset_rel: str, name: str) -> Any:
    path = REPO_ROOT / "presets" / preset_rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO_ROOT / "presets"))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


@pytest.fixture(autouse=True)
def clean_repo_env(monkeypatch):
    """SUPERTOOL_REPO is process-global; never let it bleed between tests.

    `monkeypatch.delenv` before `yield` only ever cleans the *incoming* state.
    `main()`'s `repo:` pre-pass writes `os.environ["SUPERTOOL_REPO"]` directly
    -- real production code, not a test double, and every "accepted" call in
    this file (`test_repo_op_is_stripped_and_exported_before_dispatch` and its
    siblings) drives `main()` far enough to set it for real. That mutation is
    invisible to `monkeypatch`, whose own teardown only reverses ops it
    performed itself, so without an explicit pop here the value survives into
    whichever test runs next in this pytest worker -- the #1962 shape,
    reproduced against this file's GitLab twin
    (`tests/test_gl_repo_target_676.py`) and confirmed here too: a `gh-prs`
    radar test scheduled after this file's tests started resolving
    `Digital-Process-Tools/claude-remember` as its target with nothing in its
    own body naming that repo.
    """
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    yield
    os.environ.pop("SUPERTOOL_REPO", None)


@pytest.fixture
def restore_cwd():
    saved = os.getcwd()
    yield
    os.chdir(saved)


@pytest.fixture
def no_dispatch(monkeypatch):
    """Record the ops that reach dispatch, and the env each one saw."""
    seen: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        supertool, "dispatch",
        lambda a: (seen.append((a, os.environ.get("SUPERTOOL_REPO"))), "")[-1],
    )
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)
    return seen


# ---------------------------------------------------------------------------
# Core: the repo: pre-pass
# ---------------------------------------------------------------------------

def test_repo_op_is_stripped_and_exported_before_dispatch(no_dispatch) -> None:
    rc = supertool.main(["repo:Digital-Process-Tools/claude-remember",
                         "gh-pr:265:status"])

    assert rc == 0
    assert no_dispatch == [
        ("gh-pr:265:status", "Digital-Process-Tools/claude-remember")
    ]


def test_the_env_var_main_sets_does_not_survive_into_the_next_test() -> None:
    """The GitHub twin of `test_gl_repo_target_676.py`'s own pin (#1962).

    `main()`'s `repo:` pre-pass sets `os.environ["SUPERTOOL_REPO"]` for real,
    a mutation `monkeypatch` never tracks, so `clean_repo_env`'s teardown did
    not undo it before the fix above -- left alive, it silently makes a later
    test in the same pytest worker answer as though it were scoped to
    `Digital-Process-Tools/claude-remember` (the repo this file's own tests
    use as their example target). Observed for real: 5 of 12 CI legs on
    #1979 had `presets/watch/tiers/gh_prs.py`'s own tests resolve exactly
    that repo with nothing in their own bodies naming it.

    Asserting `os.environ` inside the leaking test's own body cannot see
    this -- `main()` has already set it and `clean_repo_env`'s teardown has
    not run yet at that point, so the assertion would pass or fail
    identically with or without the fix above. The leak is what survives
    INTO the next test, so this drives a real next test, in a real pytest
    subprocess, and checks it from there -- the same technique
    `test_gl_repo_target_676.py`'s own regression test uses.
    """
    probe = REPO_ROOT / "tests" / "_gh_repo_env_leak_probe_1962.py"
    probe.write_text(
        "import os\n\n\n"
        "def test_probe():\n"
        "    assert 'SUPERTOOL_REPO' not in os.environ, os.environ.get('SUPERTOOL_REPO')\n",
        encoding="utf-8",
    )
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest",
             f"{__file__}::test_repo_op_is_stripped_and_exported_before_dispatch",
             str(probe), "-q", "--no-cov", "-n0"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=REPO_ROOT, timeout=60,
        )
    finally:
        probe.unlink(missing_ok=True)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]


def test_repo_op_allowed_immediately_after_cwd(tmp_path, no_dispatch,
                                               restore_cwd) -> None:
    """cwd: keeps its must-be-first rule; repo: sits directly behind it."""
    rc = supertool.main([f"cwd:{tmp_path}", "repo:owner/name", "gh-run:9"])

    assert rc == 0
    assert no_dispatch == [("gh-run:9", "owner/name")]
    assert os.path.realpath(os.getcwd()) == os.path.realpath(str(tmp_path))


def test_repo_op_rejected_when_not_first(no_dispatch, capsys) -> None:
    rc = supertool.main(["gh-pr:265:status", "repo:owner/name"])

    assert rc == 1
    assert no_dispatch == []                       # nothing ran
    assert "must be the first op" in capsys.readouterr().err


def test_repo_op_rejected_twice(no_dispatch, capsys) -> None:
    rc = supertool.main(["repo:a/b", "repo:c/d", "gh-pr:1:status"])

    assert rc == 1
    assert no_dispatch == []
    assert "only one" in capsys.readouterr().err


@pytest.mark.parametrize("spec", ["repo:", "repo:claude-remember",
                                  "repo:owner/", "repo:/name",
                                  "repo:a/b/c"])
def test_repo_op_rejects_malformed_target(spec, no_dispatch, capsys) -> None:
    """OWNER/NAME or nothing — a half-target must never reach gh."""
    rc = supertool.main([spec, "gh-pr:1:status"])

    assert rc == 1
    assert no_dispatch == []
    assert "OWNER/NAME" in capsys.readouterr().err


def test_repo_op_refuses_ops_that_cannot_honour_it(no_dispatch, capsys) -> None:
    """A target no op in the call can use is refused, never silently dropped."""
    rc = supertool.main(["repo:owner/name", "gh-pr:1:status", "read:foo.py"])

    assert rc == 1
    assert no_dispatch == []
    err = capsys.readouterr().err
    assert "read" in err                # names the op that cannot honour it
    assert "repo:" in err


def test_repo_op_now_reaches_gh_issue_create(no_dispatch) -> None:
    """#1909: a payload-mode op no longer refuses the whole call — `repo:` is
    exported the same as for an `op`-mode read, and reconciling it with the
    payload's own `repo` key is `gh-issue-create`'s own job now."""
    rc = supertool.main(["repo:owner/name", "gh-issue-create:@.max/x.toml"])

    assert rc == 0
    assert no_dispatch == [("gh-issue-create:@.max/x.toml", "owner/name")]


def test_no_repo_op_leaves_the_env_untouched(no_dispatch) -> None:
    """Absence must stay absent — a stale export would retarget a later op."""
    rc = supertool.main(["gh-pr:265:status"])

    assert rc == 0
    assert no_dispatch == [("gh-pr:265:status", None)]


def test_repo_target_ops_index_reads_the_shipped_presets() -> None:
    ops = supertool._repo_target_ops()

    assert {"gh-pr", "gh-prs", "gh-issue", "gh-run", "gh-job"} <= ops
    assert "gh-issue-create" not in ops      # payload key route, not this one
    assert "read" not in ops


# ---------------------------------------------------------------------------
# Shared preset helper
# ---------------------------------------------------------------------------

def test_repo_target_helper_is_silent_without_a_target(monkeypatch) -> None:
    rt = _load("_repo_target.py", "rt")
    assert rt.target() is None
    assert rt.gh_args() == []
    assert rt.owner_repo() is None


def test_repo_target_helper_reads_the_env(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", "Digital-Process-Tools/claude-remember")
    rt = _load("_repo_target.py", "rt")
    assert rt.target() == "Digital-Process-Tools/claude-remember"
    assert rt.gh_args() == ["--repo", "Digital-Process-Tools/claude-remember"]
    assert rt.owner_repo() == ("Digital-Process-Tools", "claude-remember")


# ---------------------------------------------------------------------------
# Presets: the flag actually reaches gh
# ---------------------------------------------------------------------------

def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


_PR_JSON = json.dumps({
    "number": 265, "title": "t", "state": "OPEN", "author": {"login": "max"},
    "headRefName": "h", "baseRefName": "main", "labels": [], "milestone": None,
    "isDraft": False, "mergeable": "MERGEABLE", "reviewDecision": "APPROVED",
    "reviews": [], "mergeCommit": None, "additions": 1, "deletions": 0,
    "changedFiles": 1, "statusCheckRollup": [{"conclusion": "SUCCESS",
                                              "status": "COMPLETED"}],
    "url": "https://github.com/o/r/pull/265", "body": "", "comments": [],
})


def test_gh_pr_passes_repo_to_gh(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", "Digital-Process-Tools/claude-remember")
    pr = _load("github/pr.py", "gh_pr_673")
    calls: list[list[str]] = []

    def run(argv, *a, **kw):
        calls.append(list(argv))
        return _completed(_PR_JSON)

    monkeypatch.setattr(pr.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["pr.py", "265", "status"])
    assert pr.main() == 0

    view = [c for c in calls if c[:3] == ["gh", "pr", "view"]]
    assert view, f"no `gh pr view` call in {calls}"
    assert "--repo" in view[0]
    assert view[0][view[0].index("--repo") + 1] == \
        "Digital-Process-Tools/claude-remember"


def test_gh_pr_sends_no_repo_flag_without_a_target(monkeypatch) -> None:
    pr = _load("github/pr.py", "gh_pr_673b")
    calls: list[list[str]] = []

    def run(argv, *a, **kw):
        calls.append(list(argv))
        return _completed(_PR_JSON)

    monkeypatch.setattr(pr.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["pr.py", "265", "status"])
    assert pr.main() == 0
    assert all("--repo" not in c for c in calls)


def test_gh_pr_never_puts_repo_on_a_graphql_api_call(monkeypatch) -> None:
    """`gh api` has no --repo; the GraphQL calls carry owner/repo as variables."""
    monkeypatch.setenv("SUPERTOOL_REPO", "o/r")
    pr = _load("github/pr.py", "gh_pr_673c")
    calls: list[list[str]] = []

    def run(argv, *a, **kw):
        calls.append(list(argv))
        if "graphql" in argv:
            return _completed(json.dumps(
                {"data": {"repository": {"pullRequest":
                                         {"reviewThreads": {"nodes": []}}}}}))
        return _completed(_PR_JSON)

    monkeypatch.setattr(pr.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["pr.py", "265"])
    assert pr.main() == 0
    api = [c for c in calls if c[:2] == ["gh", "api"]]
    assert api, f"no `gh api` call in {calls}"
    assert all("--repo" not in c for c in api)


def test_gh_issue_passes_repo_to_gh(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", "o/r")
    issue = _load("github/issue.py", "gh_issue_673")
    calls: list[list[str]] = []

    def run(argv, *a, **kw):
        calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "list"]:
            return _completed("[]")
        return _completed(json.dumps(
            {"number": 5, "title": "t", "state": "OPEN", "labels": [],
             "milestone": None, "assignees": [], "author": {"login": "m"},
             "url": "u", "body": "", "comments": []}))

    monkeypatch.setattr(issue.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["issue.py", "5"])
    assert issue.main() == 0
    view = [c for c in calls if c[:3] == ["gh", "issue", "view"]]
    assert view and "--repo" in view[0] and "o/r" in view[0]


def test_gh_run_passes_repo_to_gh(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", "o/r")
    run_mod = _load("github/run.py", "gh_run_673")
    calls: list[list[str]] = []

    def run(argv, *a, **kw):
        calls.append(list(argv))
        if argv[:2] == ["git", "rev-parse"]:
            return _completed("main")
        return _completed(json.dumps(
            {"databaseId": 1, "name": "ci", "status": "completed",
             "conclusion": "success", "event": "push", "headBranch": "main",
             "url": "u", "jobs": []}))

    monkeypatch.setattr(run_mod.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["run.py", "1"])
    assert run_mod.main() == 0
    view = [c for c in calls if c[:3] == ["gh", "run", "view"]]
    assert view and "--repo" in view[0] and "o/r" in view[0]


def test_gh_prs_passes_repo_to_gh(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", "o/r")
    prs = _load("github/prs.py", "gh_prs_673")
    cmd = prs._build_list_cmd({}, 50)
    assert "--repo" in cmd and cmd[cmd.index("--repo") + 1] == "o/r"


def test_gh_job_substitutes_owner_repo_in_the_api_path(monkeypatch) -> None:
    """`gh api repos/{owner}/{repo}/…` expands from cwd — a target must replace
    the placeholders, not sit beside them."""
    monkeypatch.setenv("SUPERTOOL_REPO", "Digital-Process-Tools/claude-remember")
    job = _load("github/job.py", "gh_job_673")

    path = job._api_repo_path("actions/jobs/42")

    assert path == \
        "repos/Digital-Process-Tools/claude-remember/actions/jobs/42"


def test_gh_job_keeps_the_gh_placeholders_without_a_target() -> None:
    job = _load("github/job.py", "gh_job_673b")
    assert job._api_repo_path("actions/jobs/42") == \
        "repos/{owner}/{repo}/actions/jobs/42"


# ---------------------------------------------------------------------------
# The watch column, which a repo target makes unanswerable
# ---------------------------------------------------------------------------
#
# Watch pollers write /tmp/supertool-watch-github-pr__{number}.pid — keyed by
# PR number with no repo dimension. Under a repo target that key is ambiguous:
# a live poller for #12 of the repo you are standing in would mark #12 of the
# targeted repo as watched, and the footer would then NOT offer to watch it.
# Blank is not a safe default either — on this board blank asserts "not
# watched". Three states, not two.

def test_watch_state_is_unknown_under_a_repo_target(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", "o/r")
    prs = _load("github/prs.py", "gh_prs_watch_a")
    assert prs._watched_numbers() is None


def test_watch_state_is_still_read_without_a_repo_target(monkeypatch,
                                                         tmp_path) -> None:
    """The decline must be caused by the target, not by the code path existing."""
    prs = _load("github/prs.py", "gh_prs_watch_b")
    pid_file = tmp_path / "supertool-watch-github-pr__12.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    assert prs._watched_numbers(str(tmp_path)) == {"12"}


def test_board_row_renders_unknown_watch_state_distinctly() -> None:
    board = _load("_board.py", "board_673")
    common = dict(sigil="#", ident="12", status="ok", appr="", age="1d",
                  changes="", branches="a -> b")

    watched = board.render_row(watched=True, **common)
    not_watched = board.render_row(watched=False, **common)
    unknown = board.render_row(watched=None, **common)

    assert watched.startswith(board.EYE)
    assert not_watched.startswith(" ")
    assert unknown.startswith("?")           # neither claim, and not blank
    assert len({watched[0], not_watched[0], unknown[0]}) == 3


def test_footer_does_not_offer_a_cross_repo_watch_command(monkeypatch) -> None:
    """`watch:github-pr:12` under a target would poll the *wrong* repo's #12 —
    an actionable suggestion that does the wrong thing is worse than none."""
    prs = _load("github/prs.py", "gh_prs_watch_c")
    board = [{"number": 12, "_checks": "failed", "_approved": True}]

    known = prs._footer(board, set())
    unknown = prs._footer(board, None)

    assert "unwatched" in known and "watch:github-pr:12" in known
    assert "unwatched" not in unknown
    assert "watch:github-pr:12" not in unknown
    assert "unknown" in unknown              # says so rather than going quiet


def test_render_table_marks_every_row_unknown_under_a_target() -> None:
    prs = _load("github/prs.py", "gh_prs_watch_d")
    board = [{"number": 12, "_checks": "success", "title": "t"},
             {"number": 13, "_checks": "failed", "title": "u"}]

    out = prs._render_table(board, None)

    assert len([ln for ln in out.splitlines() if ln.startswith("?")]) == 2


# ---------------------------------------------------------------------------
# The error message, once the capability exists
# ---------------------------------------------------------------------------

def test_error_without_a_target_names_the_repo_op_as_the_door() -> None:
    """The old text named the wall. It must now name the door too."""
    pr = _load("github/pr.py", "gh_pr_err_a")
    msg = pr._format_error("could not determine git remotes", "PR", "265")

    assert "cwd is not a GitHub repo" in msg
    assert "repo:OWNER/NAME" in msg


def test_error_with_a_target_does_not_blame_the_cwd(monkeypatch) -> None:
    """With a target, cwd is irrelevant — saying otherwise sends the reader to
    fix a thing that is not broken."""
    monkeypatch.setenv("SUPERTOOL_REPO", "Digital-Process-Tools/claude-remember")
    pr = _load("github/pr.py", "gh_pr_err_b")
    msg = pr._format_error("could not determine git remotes", "PR", "265")

    assert "cwd is not a GitHub repo" not in msg
    assert "Digital-Process-Tools/claude-remember" in msg


def test_not_found_with_a_target_names_the_target_not_this_repo(
        monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", "o/r")
    pr = _load("github/pr.py", "gh_pr_err_c")
    msg = pr._format_error("HTTP 404: Not Found", "PR", "265")

    assert "in this repo" not in msg
    assert "o/r" in msg


# ---------------------------------------------------------------------------
# `_repo_target.resolve_or_conflict` — the reconciliation a payload-mode op
# now runs itself, since the pre-pass no longer refuses on its behalf (#1909)
# ---------------------------------------------------------------------------

def test_resolve_or_conflict_is_a_no_op_without_a_target() -> None:
    rt = _load("_repo_target.py", "rt_conflict_no_target")
    payload = {"repo": "o/r"}
    err, source = rt.resolve_or_conflict(payload, "gh-issue-create")
    assert err is None
    assert source == "payload"
    assert payload["repo"] == "o/r"


def test_resolve_or_conflict_is_silent_with_neither_set(monkeypatch) -> None:
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    rt = _load("_repo_target.py", "rt_conflict_neither")
    payload: dict = {}
    err, source = rt.resolve_or_conflict(payload, "gh-issue-create")
    assert err is None
    assert source == ""
    assert "repo" not in payload


def test_resolve_or_conflict_fills_a_silent_payload(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", "owner/name")
    rt = _load("_repo_target.py", "rt_conflict_fill")
    payload: dict = {}
    err, source = rt.resolve_or_conflict(payload, "gh-issue-create")
    assert err is None
    assert source == "repo: op"
    assert payload["repo"] == "owner/name"


def test_resolve_or_conflict_accepts_agreement(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", "owner/name")
    rt = _load("_repo_target.py", "rt_conflict_agree")
    payload = {"repo": "owner/name"}
    err, source = rt.resolve_or_conflict(payload, "gh-issue-create")
    assert err is None
    assert "agrees" in source
    assert payload["repo"] == "owner/name"


def test_resolve_or_conflict_agreement_is_case_insensitive(monkeypatch) -> None:
    """A caller who typed `Owner/Name` in the payload and `owner/name` in the
    repo: op has not disagreed."""
    monkeypatch.setenv("SUPERTOOL_REPO", "owner/name")
    rt = _load("_repo_target.py", "rt_conflict_case")
    payload = {"repo": "Owner/Name"}
    err, _source = rt.resolve_or_conflict(payload, "gh-issue-create")
    assert err is None


def test_resolve_or_conflict_refuses_disagreement_naming_both(monkeypatch) -> None:
    """The load-bearing arm: this must refuse, and name *both* values and the
    op, or a silent precedence reintroduces exactly the defect the pre-pass
    refusal used to prevent wholesale. Pairs with the agreement test above --
    would still pass if the code did nothing without it."""
    monkeypatch.setenv("SUPERTOOL_REPO", "owner/from-repo-op")
    rt = _load("_repo_target.py", "rt_conflict_disagree")
    payload = {"repo": "owner/from-payload"}
    err, source = rt.resolve_or_conflict(payload, "gh-issue-create")
    assert err is not None
    assert source == ""
    assert "gh-issue-create" in err
    assert "owner/from-repo-op" in err
    assert "owner/from-payload" in err
    # untouched — a refusal must never half-apply either value
    assert payload["repo"] == "owner/from-payload"


def test_resolve_or_conflict_uses_the_given_key(monkeypatch) -> None:
    """`gl-issue-create` reads `project`, not `repo` — the refusal must name
    the field the payload validator actually reads."""
    monkeypatch.setenv("SUPERTOOL_REPO", "group/from-repo-op")
    rt = _load("_repo_target.py", "rt_conflict_key")
    payload = {"project": "group/from-payload"}
    err, _source = rt.resolve_or_conflict(payload, "gl-issue-create", "project")
    assert err is not None
    assert "project" in err
    assert "repo = " not in err


def test_repo_reachable_ops_is_a_superset_of_op_mode() -> None:
    """#1909: a payload-mode op is reachable too now — it just does not read
    SUPERTOOL_REPO itself."""
    reachable = supertool._repo_reachable_ops()
    op_mode = supertool._repo_target_ops()

    assert op_mode <= reachable
    assert "gh-issue-create" in reachable
    assert "gh-issue-create" not in op_mode
    assert "read" not in reachable
