"""Every `gh`/`glab` error classifier matches a status, never a number (#1846).

#1823 fixed two radar tiers. The same predicate --

    if "401" in s or "unauthorized" in s or "not logged in" in s:

-- was still in 22 more call sites across `presets/github/` and
`presets/gitlab/`, and five of those widened it again with a bare
``"token" in s``.

`"401"` is three characters tested against the whole of a CLI's stderr.
`gh` writes them into a user id (``API rate limit exceeded for user ID
44012345``); go-gitlab echoes the request URL into every error string, so any
project, job or pipeline id containing them does the same. At every one of
these sites the auth arm sits **above** the rate-limit and permission arms, so
the match is not fenced by anything: a throttle and a server error both render
as *the credential is gone*, and the remedy printed -- ``gh auth login`` -- is
a claim about a cause nothing established. A maintainer loop that reads it
stops, where the correct action was to retry.

``"token" in s`` is wider again. GitHub's own scope error is ``Resource not
accessible by personal access token``, a 403 about permissions; it reached the
auth arm and never got to the permission arm below it.

**The positive controls are the test.** An assertion that a remedy is absent
passes on a classifier that returns nothing at all, so every "must not say it"
case here is paired, at the same site, with a "must still say it" case.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PRESETS = ROOT / "presets"


def _module(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, PRESETS / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


auth_probe = _module("auth_probe_1846", "_auth_probe.py")

gh_run = _module("gh_run_1846", "github/run.py")
gh_pr = _module("gh_pr_1846", "github/pr.py")
gh_issue = _module("gh_issue_1846", "github/issue.py")
gh_labels = _module("gh_labels_1846", "github/labels.py")
gh_branch = _module("gh_branch_1846", "github/branch.py")
gh_check = _module("gh_check_1846", "github/check.py")
gh_job = _module("gh_job_1846", "github/job.py")
gh_follow = _module("gh_follow_1846", "github/follow.py")
gh_star = _module("gh_star_1846", "github/star.py")
gh_following = _module("gh_following_1846", "github/following.py")
gh_starred = _module("gh_starred_1846", "github/starred.py")
gh_batch_follow = _module("gh_batch_follow_1846", "github/batch_follow.py")
gh_batch_star = _module("gh_batch_star_1846", "github/batch_star.py")
gh_issues = _module("gh_issues_1846", "github/issues.py")
gh_prs = _module("gh_prs_1846", "github/prs.py")

gl_mr = _module("gl_mr_1846", "gitlab/mr.py")
gl_job = _module("gl_job_1846", "gitlab/job.py")
gl_pipeline = _module("gl_pipeline_1846", "gitlab/pipeline.py")
gl_issue = _module("gl_issue_1846", "gitlab/issue.py")
gl_runners = _module("gl_runners_1846", "gitlab/runners.py")
gl_api = _module("gl_api_1846", "gitlab/api.py")
gl_mrs = _module("gl_mrs_1846", "gitlab/mrs.py")


# ---------------------------------------------------------------------------
# the inputs
# ---------------------------------------------------------------------------

#: A GitHub throttle. `401` appears inside the user id and nowhere else; the
#: correct arm is the rate-limit one, which sits below the auth arm.
GH_THROTTLE = ("HTTP 403: API rate limit exceeded for user ID 44012345. "
               "(https://api.github.com/user/following/octocat)")

#: A genuine rejected credential -- the positive control.
GH_REAL_401 = "HTTP 401: Bad credentials (https://api.github.com/user)"

#: GitHub's own scope error. It is a 403 about permissions and mentions a
#: token in passing; `"token" in s` routed it to the credential arm.
GH_SCOPE_403 = ("HTTP 403: Resource not accessible by personal access token "
                "(https://api.github.com/repos/o/r/issues)")

#: A GitLab server error. go-gitlab puts the request URL in every error
#: string, so the project id carries the three characters.
GL_SERVER_500 = ("GET https://gitlab.com/api/v4/projects/44012345/"
                 "merge_requests/7: 500 {message: 500 Internal Server Error}")

#: A genuine rejected credential -- the positive control.
GL_REAL_401 = ("GET https://gitlab.com/api/v4/projects/9/merge_requests/7: "
               "401 {message: 401 Unauthorized}")

GH_REMEDY = "gh auth login"
GL_REMEDY = "glab auth login"


class _R:
    def __init__(self, code: int = 1, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = code, out, err


def _fake_proc(mp, mod, err: str) -> None:
    """Every call this module makes to a CLI fails with `err`."""
    mp.setattr(mod.subprocess, "run", lambda *a, **k: _R(1, "", err))


# ---------------------------------------------------------------------------
# one adapter per site -- (monkeypatch, capsys, stderr) -> what it rendered
# ---------------------------------------------------------------------------

def _a_gh_run(mp, cap, err):
    return gh_run._format_error(err, "workflow run", "1")


def _a_gh_pr(mp, cap, err):
    return gh_pr._format_error(err, "PR", "1")


def _a_gh_issue(mp, cap, err):
    return gh_issue._format_error(err, "issue", "1")


def _a_gh_labels(mp, cap, err):
    return gh_labels._format_error(err, "labels")


def _a_gh_branch(mp, cap, err):
    return gh_branch._format_error(err, "branch x")


def _a_gh_check(mp, cap, err):
    kind = gh_check._gh_error_kind(err)
    return GH_REMEDY if kind == "auth" else f"kind={kind}"


def _a_gh_job(mp, cap, err):
    kind = gh_job._gh_error_kind(err)
    return GH_REMEDY if kind == "auth" else f"kind={kind}"


def _a_gh_follow(mp, cap, err):
    _fake_proc(mp, gh_follow, err)
    gh_follow.main("octocat")
    return cap.readouterr().err


def _a_gh_star(mp, cap, err):
    _fake_proc(mp, gh_star, err)
    gh_star.main("o/r")
    return cap.readouterr().err


def _a_gh_following(mp, cap, err):
    _fake_proc(mp, gh_following, err)
    gh_following.main("5")
    return cap.readouterr().err


def _a_gh_starred(mp, cap, err):
    _fake_proc(mp, gh_starred, err)
    gh_starred.main("5")
    return cap.readouterr().err


def _a_gh_batch_follow(mp, cap, err):
    _fake_proc(mp, gh_batch_follow, err)
    return gh_batch_follow.follow("octocat")[1]


def _a_gh_batch_star(mp, cap, err):
    _fake_proc(mp, gh_batch_star, err)
    return gh_batch_star.star("o/r")[1]


def _a_gh_issues_lookup(mp, cap, err):
    mp.setattr(gh_issues._repo_target, "owner_repo", lambda: None)
    _fake_proc(mp, gh_issues, err)
    return gh_issues._lookup_repo()[1] or ""


def _a_gh_issues_list(mp, cap, err):
    _fake_proc(mp, gh_issues, err)
    gh_issues.main_with_args("label=bug")
    return cap.readouterr().err


def _a_gh_prs_list(mp, cap, err):
    _fake_proc(mp, gh_prs, err)
    gh_prs.main_with_args("")
    return cap.readouterr().err


def _a_gl_mr(mp, cap, err):
    return gl_mr._format_error(err, "MR", "1")


def _a_gl_job(mp, cap, err):
    return gl_job._format_error(err, "job", "1")


def _a_gl_pipeline(mp, cap, err):
    return gl_pipeline._format_error(err, "pipeline", "1")


def _a_gl_issue(mp, cap, err):
    return gl_issue._format_error(err, "issue", "1")


def _a_gl_runners(mp, cap, err):
    return gl_runners._format_error(err, "runners")


def _a_gl_api(mp, cap, err):
    return gl_api.classify_error(err, "projects/1")


def _a_gl_mrs_list(mp, cap, err):
    # `gl-mrs` has no `main_with_args` twin -- `main` reads `sys.argv` itself.
    mp.setattr(gl_mrs.sys, "argv", ["gl-mrs", ""])
    mp.setattr(gl_mrs, "_run", lambda *a, **k: _R(1, "", err))
    gl_mrs.main()
    return cap.readouterr().err


#: `(id, adapter, non-status input, genuine-401 input, remedy)`.
SITES = [
    ("gh-run", _a_gh_run, GH_THROTTLE, GH_REAL_401, GH_REMEDY),
    ("gh-pr", _a_gh_pr, GH_THROTTLE, GH_REAL_401, GH_REMEDY),
    ("gh-issue", _a_gh_issue, GH_THROTTLE, GH_REAL_401, GH_REMEDY),
    ("gh-labels", _a_gh_labels, GH_THROTTLE, GH_REAL_401, GH_REMEDY),
    ("gh-branch", _a_gh_branch, GH_THROTTLE, GH_REAL_401, GH_REMEDY),
    ("gh-check", _a_gh_check, GH_THROTTLE, GH_REAL_401, GH_REMEDY),
    ("gh-job", _a_gh_job, GH_THROTTLE, GH_REAL_401, GH_REMEDY),
    ("gh-follow", _a_gh_follow, GH_THROTTLE, GH_REAL_401, GH_REMEDY),
    ("gh-star", _a_gh_star, GH_THROTTLE, GH_REAL_401, GH_REMEDY),
    ("gh-following", _a_gh_following, GH_THROTTLE, GH_REAL_401, GH_REMEDY),
    ("gh-starred", _a_gh_starred, GH_THROTTLE, GH_REAL_401, GH_REMEDY),
    ("gh-batch-follow", _a_gh_batch_follow, GH_THROTTLE, GH_REAL_401, GH_REMEDY),
    ("gh-batch-star", _a_gh_batch_star, GH_THROTTLE, GH_REAL_401, GH_REMEDY),
    ("gh-issues:lookup", _a_gh_issues_lookup, GH_THROTTLE, GH_REAL_401, GH_REMEDY),
    ("gh-issues:list", _a_gh_issues_list, GH_THROTTLE, GH_REAL_401, GH_REMEDY),
    ("gh-prs:list", _a_gh_prs_list, GH_THROTTLE, GH_REAL_401, GH_REMEDY),
    ("gl-mr", _a_gl_mr, GL_SERVER_500, GL_REAL_401, GL_REMEDY),
    ("gl-job", _a_gl_job, GL_SERVER_500, GL_REAL_401, GL_REMEDY),
    ("gl-pipeline", _a_gl_pipeline, GL_SERVER_500, GL_REAL_401, GL_REMEDY),
    ("gl-issue", _a_gl_issue, GL_SERVER_500, GL_REAL_401, GL_REMEDY),
    ("gl-runners", _a_gl_runners, GL_SERVER_500, GL_REAL_401, GL_REMEDY),
    ("gl-api", _a_gl_api, GL_SERVER_500, GL_REAL_401, GL_REMEDY),
    ("gl-mrs:list", _a_gl_mrs_list, GL_SERVER_500, GL_REAL_401, GL_REMEDY),
]

#: The five sites that also matched a bare ``"token" in s``. A 403 about
#: scopes is a permission answer, not a credential one.
TOKEN_SITES = [
    ("gh-run", _a_gh_run),
    ("gh-pr", _a_gh_pr),
    ("gh-issue", _a_gh_issue),
    ("gh-check", _a_gh_check),
    ("gh-job", _a_gh_job),
]


@pytest.mark.parametrize(
    "site,adapter,noise,real,remedy",
    SITES, ids=[s[0] for s in SITES])
def test_a_number_that_is_not_a_status_does_not_print_the_credential_remedy(
        site, adapter, noise, real, remedy, monkeypatch, capsys) -> None:
    rendered = adapter(monkeypatch, capsys, noise)
    assert remedy not in rendered, (
        f"{site}: `401` inside an id was read as a rejected credential, and "
        f"the remedy printed is a claim about a cause nothing established: "
        f"{rendered!r}")


@pytest.mark.parametrize(
    "site,adapter,noise,real,remedy",
    SITES, ids=[s[0] for s in SITES])
def test_a_genuine_401_still_prints_the_credential_remedy(
        site, adapter, noise, real, remedy, monkeypatch, capsys) -> None:
    """The positive control. Without it the assertion above passes on a
    classifier that says nothing at all."""
    rendered = adapter(monkeypatch, capsys, real)
    assert remedy in rendered, (
        f"{site}: a rejected credential no longer names its remedy: "
        f"{rendered!r}")


@pytest.mark.parametrize("site,adapter", TOKEN_SITES, ids=[s[0] for s in TOKEN_SITES])
def test_a_scope_403_mentioning_a_token_is_not_a_credential_answer(
        site, adapter, monkeypatch, capsys) -> None:
    rendered = adapter(monkeypatch, capsys, GH_SCOPE_403)
    assert GH_REMEDY not in rendered, (
        f"{site}: a 403 about token scopes was routed to the credential arm, "
        f"so the permission arm below it is unreachable for it: {rendered!r}")


# ---------------------------------------------------------------------------
# the predicate itself, and the population
# ---------------------------------------------------------------------------

def test_no_marker_anywhere_in_the_probe_is_a_bare_status_number() -> None:
    """The structural pin, carried over from #1823 now that the module is
    shared by every preset rather than by two radar tiers."""
    for name in ("NOT_AUTHENTICATED_MARKERS", "GITLAB_MARKERS"):
        markers = getattr(auth_probe, name)
        assert markers, f"{name} is empty"
        bare = [m for m in markers if m.strip().isdigit()]
        assert bare == [], (
            f"{name}: a bare status number is a substring of request ids, "
            f"user ids and epochs, not a statement about a credential: "
            f"{bare!r}")


def test_no_preset_tests_a_bare_status_number_or_a_bare_token_word() -> None:
    """The population pin. This is a class that came back once already; a
    seventeenth file growing its own copy is how it comes back again."""
    offenders = []
    for directory in ("github", "gitlab"):
        for path in sorted((PRESETS / directory).glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                for probe in ('"401" in', '"token" in', "'401' in", "'token' in"):
                    if probe in line:
                        offenders.append(f"{path.name}:{lineno}: {probe}")
    assert offenders == [], (
        "a bare substring test against a whole stderr is #1846: use "
        "`_auth_probe.says_not_authenticated` -- " + "; ".join(offenders))
