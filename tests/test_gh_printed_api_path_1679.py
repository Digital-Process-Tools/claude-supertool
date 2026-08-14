"""A `gh api` command printed for a reader names a repository (#1679).

`_repo_target.api_path` leaves gh's `{owner}`/`{repo}` placeholders in place
when no `repo:` target is set, because `gh api` expands them from the cwd's
remote and a target has to *replace* rather than accompany them (#1281). That
is right for a path this process executes and wrong for one pasted by a reader:
the same line names a different repository in every checkout, and says nothing
about having changed meaning. #1670 fixed it in `pr_merge`; these are the four
sites in `gh-check` and `gh-job` it deliberately left out.

The four printed sites are `check.py`'s `_not_found_message` and
`render_check`, and `job.py`'s `_missing_log_message` and the empty-log block
in `main()`. The first three are exercised end to end below; the fourth shares
`api_path_printable` with them and is covered by that function's own tests.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PRESETS = Path(__file__).parent.parent / "presets"


def _load(relative: str, name: str):
    sys.path.insert(0, str(PRESETS))
    try:
        spec = importlib.util.spec_from_file_location(name, PRESETS / relative)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(PRESETS))


rt = _load("_repo_target.py", "rt_1679")
check = _load("github/check.py", "gh_check_1679")
job = _load("github/job.py", "gh_job_1679")

CWD_SLUG = "Digital-Process-Tools/claude-supertool"
TARGET = "other/thing"
PLACEHOLDERS = "repos/{owner}/{repo}/"


@pytest.fixture(autouse=True)
def _no_target(monkeypatch):
    monkeypatch.delenv(rt.ENV_VAR, raising=False)


# ---------------------------------------------------------------------------
# the helper
# ---------------------------------------------------------------------------

def test_a_printable_path_names_the_cwd_repo_when_there_is_no_target(monkeypatch):
    monkeypatch.setattr(rt, "cwd_slug", lambda timeout=15: CWD_SLUG)
    assert rt.api_path_printable("actions/jobs/7") == (
        f"repos/{CWD_SLUG}/actions/jobs/7")


def test_a_target_wins_and_costs_no_call(monkeypatch):
    """Replaced, not accompanied (#1281) — and the slug is never read.

    Under a target the printed line and the executed one must not be able to
    disagree about which repository the call is about, and the cwd's identity
    is not the answer to that question. So nothing is spawned at all: a
    `cwd_slug` that raises proves the branch is not taken.
    """
    monkeypatch.setenv(rt.ENV_VAR, TARGET)

    def _boom(timeout=15):
        raise AssertionError("the cwd slug was read under a repo target")

    monkeypatch.setattr(rt, "cwd_slug", _boom)
    assert rt.api_path_printable("actions/jobs/7") == (
        f"repos/{TARGET}/actions/jobs/7")


@pytest.mark.parametrize("slug", ["", "not-a-slug", "a/b/c", "o/r extra"],
                         ids=["empty", "unsplit", "three", "space"])
def test_an_unreadable_slug_declines_to_the_placeholders(monkeypatch, slug):
    """The third state. A path built out of a partial answer is not a command.

    The placeholders are a *correct* command in the reader's own checkout, so
    falling back to them loses the improvement and nothing else — which is the
    recoverable direction when `gh repo view` cannot answer.
    """
    monkeypatch.setattr(rt, "cwd_slug", lambda timeout=15: slug)
    assert rt.api_path_printable("x") == PLACEHOLDERS + "x"


# ---------------------------------------------------------------------------
# the printed commands
# ---------------------------------------------------------------------------

def _stub_slug(monkeypatch, *mods, slug: str = CWD_SLUG):
    for mod in mods:
        monkeypatch.setattr(mod._repo_target, "cwd_slug",
                            lambda timeout=15: slug)


def test_gh_check_prints_a_named_repo_when_both_namespaces_404(monkeypatch):
    _stub_slug(monkeypatch, check)
    probe = check.GhCall(ok=False, data=None, error="404", absent=False)
    msg = check._not_found_message("123", probe)
    assert "gh api " + PLACEHOLDERS not in msg, msg
    assert f"gh api repos/{CWD_SLUG}/actions/jobs/123" in msg, msg


def test_gh_check_prints_a_named_repo_when_annotations_fail(monkeypatch, capsys):
    _stub_slug(monkeypatch, check)
    monkeypatch.setattr(
        check, "_gh",
        lambda argv, timeout=15: check.GhCall(
            ok=False, data=None, error="gh timed out", absent=False))
    rc = check.render_check("123", {"name": "CodeQL", "status": "completed",
                                    "conclusion": "failure"})
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "gh api " + PLACEHOLDERS not in out, out
    assert f"gh api repos/{CWD_SLUG}/check-runs/123/annotations" in out, out


def test_gh_job_prints_a_named_repo_when_the_job_endpoint_is_silent(monkeypatch):
    _stub_slug(monkeypatch, job)
    msg = job._missing_log_message("456", None, False, "gh timed out")
    assert "gh api " + PLACEHOLDERS not in msg, msg
    assert f"gh api repos/{CWD_SLUG}/actions/jobs/456" in msg, msg


def test_a_repo_target_reaches_the_printed_command_too(monkeypatch):
    """The target already substituted here before #1679 — pinned, not changed.

    The issue body claimed these four sites named the *wrong* repository under
    a target. They did not: every one goes through `_repo_target.api_path`,
    which replaces the placeholders whenever a target is set. The defect was
    only ever the untargeted case, and this test is what says so.
    """
    monkeypatch.setenv(rt.ENV_VAR, TARGET)
    _stub_slug(monkeypatch, job, check, slug="")
    assert f"repos/{TARGET}/actions/jobs/456" in job._missing_log_message(
        "456", None, False, "gh timed out")
    probe = check.GhCall(ok=False, data=None, error="404", absent=False)
    assert f"repos/{TARGET}/actions/jobs/123" in check._not_found_message(
        "123", probe)
