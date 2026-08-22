"""Every `gh`/`glab` not-found and permission classifier matches a status,
never a number (#1864).

Filed by the #1846 lane, which fixed the identical shape for the credential
reading:

    if "404" in s or "not found" in s or "could not resolve" in s:
        return ...not found...
    if "403" in s or "forbidden" in s:
        return ...permission denied...

`"404"` and `"403"` are three characters tested against the whole of a CLI's
stderr. `gh` renders a run's own API path into its stderr
(``.../actions/runs/12404999``); go-gitlab echoes the request URL into every
error string, so any project, job or pipeline id containing them does the
same. At every one of these sites the not-found arm sits above the auth,
rate-limit and permission arms, so a server error carrying one of those ids
in its URL classifies as *missing* -- and a caller told "not found" stops
retrying, where the correct action was to retry a transient failure.

**The positive controls are the test.** An assertion that a remedy is absent
passes on a classifier that returns nothing at all, so every "must not say
it" case here is paired, at the same site, with a "must still say it" case
using a genuine 404 or 403.
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


status_probe = _module("status_probe_1864", "_status_probe.py")

gh_run = _module("gh_run_1864", "github/run.py")
gh_pr = _module("gh_pr_1864", "github/pr.py")
gh_issue = _module("gh_issue_1864", "github/issue.py")
gh_labels = _module("gh_labels_1864", "github/labels.py")
gh_branch = _module("gh_branch_1864", "github/branch.py")
gh_check = _module("gh_check_1864", "github/check.py")
gh_job = _module("gh_job_1864", "github/job.py")
gh_follow = _module("gh_follow_1864", "github/follow.py")
gh_star = _module("gh_star_1864", "github/star.py")
gh_batch_follow = _module("gh_batch_follow_1864", "github/batch_follow.py")
gh_batch_star = _module("gh_batch_star_1864", "github/batch_star.py")

gl_mr = _module("gl_mr_1864", "gitlab/mr.py")
gl_job = _module("gl_job_1864", "gitlab/job.py")
gl_pipeline = _module("gl_pipeline_1864", "gitlab/pipeline.py")
gl_issue = _module("gl_issue_1864", "gitlab/issue.py")
gl_runners = _module("gl_runners_1864", "gitlab/runners.py")
gl_api = _module("gl_api_1864", "gitlab/api.py")

# Loaded last -- it has no `sys.path.insert` of its own and relies on one of
# the modules above (any `presets/github/*.py` or `presets/gitlab/*.py`) to
# have already put `presets/` on `sys.path`, exactly as `branch.py` does for
# it at production time (#1864).
declared_workflows = _module("declared_workflows_1864", "_declared_workflows.py")


# ---------------------------------------------------------------------------
# the inputs
# ---------------------------------------------------------------------------

#: A GitHub server error whose URL happens to carry the digits `404` and
#: `403` inside a run id -- the issue's own example (#1864).
GH_SERVER_ERROR = ("HTTP 500: Internal Server Error "
                    "(https://api.github.com/repos/o/r/actions/runs/12404999"
                    "-98403123)")

#: A genuine 404 -- the positive control.
GH_REAL_404 = "HTTP 404: Not Found (https://api.github.com/repos/o/r)"

#: A genuine 403 -- the positive control.
GH_REAL_403 = "HTTP 403: Forbidden (https://api.github.com/repos/o/r)"

#: A GitLab server error. go-gitlab puts the request URL in every error
#: string, so the project id carries the digits.
GL_SERVER_ERROR = ("GET https://gitlab.com/api/v4/projects/12404999-98403123/"
                    "jobs/7: 500 {message: 500 Internal Server Error}")

GL_REAL_404 = ("GET https://gitlab.com/api/v4/projects/9/jobs/7: "
               "404 {message: 404 Not Found}")

GL_REAL_403 = ("GET https://gitlab.com/api/v4/projects/9/jobs/7: "
               "403 {message: 403 Forbidden}")

NOT_FOUND_PHRASE = "not found"
FORBIDDEN_PHRASE = "permission denied"


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
    return gh_check._gh_error_kind(err)


def _a_gh_job(mp, cap, err):
    return gh_job._gh_error_kind(err)


def _a_gh_follow(mp, cap, err):
    _fake_proc(mp, gh_follow, err)
    gh_follow.main("octocat")
    return cap.readouterr().err


def _a_gh_star(mp, cap, err):
    _fake_proc(mp, gh_star, err)
    gh_star.main("o/r")
    return cap.readouterr().err


def _a_gh_batch_follow(mp, cap, err):
    _fake_proc(mp, gh_batch_follow, err)
    return gh_batch_follow.follow("octocat")[1]


def _a_gh_batch_star(mp, cap, err):
    _fake_proc(mp, gh_batch_star, err)
    return gh_batch_star.star("o/r")[1]


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


def _a_declared_workflows(mp, cap, err):
    mp.setattr(declared_workflows, "_run",
               lambda *a, **k: _R(1, "", err))
    _, err_out = declared_workflows._api("repos/o/r/x")
    return err_out


#: `(id, adapter, server-error input, genuine-404 input, not-found phrase)`.
NOT_FOUND_SITES = [
    ("gh-run", _a_gh_run, GH_SERVER_ERROR, GH_REAL_404, NOT_FOUND_PHRASE),
    ("gh-pr", _a_gh_pr, GH_SERVER_ERROR, GH_REAL_404, NOT_FOUND_PHRASE),
    ("gh-issue", _a_gh_issue, GH_SERVER_ERROR, GH_REAL_404, NOT_FOUND_PHRASE),
    ("gh-labels", _a_gh_labels, GH_SERVER_ERROR, GH_REAL_404, NOT_FOUND_PHRASE),
    ("gh-branch", _a_gh_branch, GH_SERVER_ERROR, GH_REAL_404, NOT_FOUND_PHRASE),
    ("gh-follow", _a_gh_follow, GH_SERVER_ERROR, GH_REAL_404, NOT_FOUND_PHRASE),
    ("gh-star", _a_gh_star, GH_SERVER_ERROR, GH_REAL_404, NOT_FOUND_PHRASE),
    ("gh-batch-follow", _a_gh_batch_follow, GH_SERVER_ERROR, GH_REAL_404, "not found"),
    ("gh-batch-star", _a_gh_batch_star, GH_SERVER_ERROR, GH_REAL_404, "not found"),
    ("gl-mr", _a_gl_mr, GL_SERVER_ERROR, GL_REAL_404, NOT_FOUND_PHRASE),
    ("gl-job", _a_gl_job, GL_SERVER_ERROR, GL_REAL_404, NOT_FOUND_PHRASE),
    ("gl-pipeline", _a_gl_pipeline, GL_SERVER_ERROR, GL_REAL_404, NOT_FOUND_PHRASE),
    ("gl-issue", _a_gl_issue, GL_SERVER_ERROR, GL_REAL_404, NOT_FOUND_PHRASE),
    ("gl-runners", _a_gl_runners, GL_SERVER_ERROR, GL_REAL_404, NOT_FOUND_PHRASE),
    ("gl-api", _a_gl_api, GL_SERVER_ERROR, GL_REAL_404, "not found"),
]

#: `(id, adapter, server-error input, genuine-403 input, forbidden phrase)`.
FORBIDDEN_SITES = [
    ("gh-run", _a_gh_run, GH_SERVER_ERROR, GH_REAL_403, "permission denied"),
    ("gh-pr", _a_gh_pr, GH_SERVER_ERROR, GH_REAL_403, "permission denied"),
    ("gh-issue", _a_gh_issue, GH_SERVER_ERROR, GH_REAL_403, "permission denied"),
    ("gh-labels", _a_gh_labels, GH_SERVER_ERROR, GH_REAL_403, "permission denied"),
    ("gh-branch", _a_gh_branch, GH_SERVER_ERROR, GH_REAL_403, "permission denied"),
    ("gl-mr", _a_gl_mr, GL_SERVER_ERROR, GL_REAL_403, "permission denied"),
    ("gl-job", _a_gl_job, GL_SERVER_ERROR, GL_REAL_403, "permission denied"),
    ("gl-pipeline", _a_gl_pipeline, GL_SERVER_ERROR, GL_REAL_403, "permission denied"),
    ("gl-issue", _a_gl_issue, GL_SERVER_ERROR, GL_REAL_403, "permission denied"),
    ("gl-runners", _a_gl_runners, GL_SERVER_ERROR, GL_REAL_403, "permission denied"),
    ("gl-api", _a_gl_api, GL_SERVER_ERROR, GL_REAL_403, "permission denied"),
]

#: `_gh_error_kind` sites: kind strings instead of a rendered remedy.
KIND_SITES = [
    ("gh-check", _a_gh_check),
    ("gh-job", _a_gh_job),
]


@pytest.mark.parametrize(
    "site,adapter,noise,real,phrase", NOT_FOUND_SITES,
    ids=[s[0] for s in NOT_FOUND_SITES])
def test_a_number_that_is_not_a_status_does_not_read_as_not_found(
        site, adapter, noise, real, phrase, monkeypatch, capsys) -> None:
    rendered = adapter(monkeypatch, capsys, noise)
    assert "not found" not in (rendered or "").lower(), (
        f"{site}: digits inside an id were read as not found, and the "
        f"digits established nothing about the target: {rendered!r}")


@pytest.mark.parametrize(
    "site,adapter,noise,real,phrase", NOT_FOUND_SITES,
    ids=[s[0] for s in NOT_FOUND_SITES])
def test_a_genuine_404_still_reads_as_not_found(
        site, adapter, noise, real, phrase, monkeypatch, capsys) -> None:
    """The positive control. Without it the assertion above passes on a
    classifier that says nothing at all."""
    rendered = adapter(monkeypatch, capsys, real)
    assert "not found" in (rendered or "").lower(), (
        f"{site}: a genuine 404 no longer reads as not found: {rendered!r}")


@pytest.mark.parametrize(
    "site,adapter,noise,real,phrase", FORBIDDEN_SITES,
    ids=[s[0] for s in FORBIDDEN_SITES])
def test_a_number_that_is_not_a_status_does_not_read_as_forbidden(
        site, adapter, noise, real, phrase, monkeypatch, capsys) -> None:
    rendered = adapter(monkeypatch, capsys, noise)
    assert "permission denied" not in (rendered or "").lower(), (
        f"{site}: digits inside an id were read as permission denied, and "
        f"the digits established nothing about access: {rendered!r}")


@pytest.mark.parametrize(
    "site,adapter,noise,real,phrase", FORBIDDEN_SITES,
    ids=[s[0] for s in FORBIDDEN_SITES])
def test_a_genuine_403_still_reads_as_forbidden(
        site, adapter, noise, real, phrase, monkeypatch, capsys) -> None:
    rendered = adapter(monkeypatch, capsys, real)
    assert "permission denied" in (rendered or "").lower(), (
        f"{site}: a genuine 403 no longer reads as permission denied: "
        f"{rendered!r}")


@pytest.mark.parametrize("site,adapter", KIND_SITES, ids=[s[0] for s in KIND_SITES])
def test_gh_error_kind_number_noise_is_not_notfound_or_forbidden(
        site, adapter, monkeypatch, capsys) -> None:
    assert adapter(monkeypatch, capsys, GH_SERVER_ERROR) not in ("notfound", "forbidden"), (
        f"{site}: a server error whose URL carries `404`/`403` inside an id "
        f"bucketed as notfound/forbidden")


@pytest.mark.parametrize("site,adapter", KIND_SITES, ids=[s[0] for s in KIND_SITES])
def test_gh_error_kind_genuine_404_is_notfound(
        site, adapter, monkeypatch, capsys) -> None:
    assert adapter(monkeypatch, capsys, GH_REAL_404) == "notfound", (
        f"{site}: a genuine 404 no longer bucketed as notfound")


@pytest.mark.parametrize("site,adapter", KIND_SITES, ids=[s[0] for s in KIND_SITES])
def test_gh_error_kind_genuine_403_is_forbidden(
        site, adapter, monkeypatch, capsys) -> None:
    assert adapter(monkeypatch, capsys, GH_REAL_403) == "forbidden", (
        f"{site}: a genuine 403 no longer bucketed as forbidden")


def test_declared_workflows_api_number_noise_is_not_a_404(monkeypatch, capsys) -> None:
    """`_declared_workflows._api` encodes a 404 as the sentinel string
    `"404"`. A server error whose URL carries the digit must not become it."""
    err = _a_declared_workflows(monkeypatch, capsys, GH_SERVER_ERROR)
    assert err != "404", (
        f"a server error whose URL carries `404`/`403` inside an id was "
        f"encoded as the not-found sentinel: {err!r}")


def test_declared_workflows_api_genuine_404_is_still_the_sentinel(monkeypatch, capsys) -> None:
    err = _a_declared_workflows(monkeypatch, capsys, GH_REAL_404)
    assert err == "404", (
        f"a genuine 404 no longer encodes as the not-found sentinel: {err!r}")


def test_gl_runners_radar_report_number_noise_is_not_forbidden(monkeypatch) -> None:
    """`radar_report`'s own `err and ("403" in err or ...)` guard (#1864)."""
    monkeypatch.setattr(gl_runners, "_api",
                         lambda *a, **k: (None, GL_SERVER_ERROR))
    lines, healthy = gl_runners.radar_report({})
    assert not any("cannot read project" in line for line in lines), (
        f"a server error whose URL carries `403` inside an id was read as "
        f"the tier lacking runner-read access: {lines!r}")


def test_gl_runners_radar_report_genuine_403_is_forbidden(monkeypatch) -> None:
    monkeypatch.setattr(gl_runners, "_api",
                         lambda *a, **k: (None, GL_REAL_403))
    lines, healthy = gl_runners.radar_report({})
    assert any("cannot read project" in line for line in lines), (
        f"a genuine 403 no longer reads as the tier lacking runner-read "
        f"access: {lines!r}")


# ---------------------------------------------------------------------------
# the predicate itself, and the population
# ---------------------------------------------------------------------------

def test_no_marker_anywhere_in_the_status_probe_is_a_bare_status_number() -> None:
    for name in ("NOT_FOUND_MARKERS", "FORBIDDEN_MARKERS"):
        markers = getattr(status_probe, name)
        assert markers, f"{name} is empty"
        bare = [m for m in markers if m.strip().isdigit()]
        assert bare == [], (
            f"{name}: a bare status number is a substring of request ids, "
            f"user ids and epochs, not a statement about the target: {bare!r}")


def test_no_preset_tests_a_bare_404_or_403() -> None:
    """The population pin. This is a class that came back once already under
    the auth reading (#1846); a nineteenth file growing its own copy of the
    not-found/forbidden reading is how it comes back again."""
    offenders = []
    targets = [PRESETS / "github", PRESETS / "gitlab",
               PRESETS / "_declared_workflows.py"]
    paths = []
    for t in targets:
        if t.is_dir():
            paths.extend(sorted(t.glob("*.py")))
        else:
            paths.append(t)
    for path in paths:
        if path.name == "_status_probe.py":
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            for probe in ('"404" in', '"403" in', "'404' in", "'403' in"):
                if probe in line:
                    offenders.append(f"{path.name}:{lineno}: {probe}")
    assert offenders == [], (
        "a bare substring test against a whole stderr is #1864: use "
        "`_status_probe.says_not_found` / `says_forbidden` -- "
        + "; ".join(offenders))
