"""The `gl-*` read ops honour `repo:` — #676.

#673 shipped `repo:OWNER/NAME` for the `gh-*` family and #676 records that the
GitLab family has the same gap and cannot take the same fix, in three places:

* `glab api` has no repo flag at all. The project is named *inside* the path,
  as `projects/<url-encoded full path>`, so the target is substituted into the
  `projects/:id` placeholder rather than appended beside it.
* `OWNER/NAME` is the wrong shape. GitLab allows `GROUP/SUBGROUP/PROJECT`, and
  #673's validator refuses anything that is not exactly one `/`. The shape is
  therefore decided per forge, and the forge is derived from the ops the call
  actually names — a call mixing both families is refused, because one target
  cannot name a project on two forges.
* The watch/radar state key has no project dimension. `gl-mrs` and `gl-runners`
  feed `radar`, which respawns watchers off that key, so they are deliberately
  *not* opted in: a `repo:` call naming one refuses rather than half-applying.

The target is also charset-checked here rather than at the preset, because it is
substituted into an API path and forwarded to a CLI that attaches a live token.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import supertool

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = "group/subgroup/project"
ENCODED = "group%2Fsubgroup%2Fproject"


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
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    yield


@pytest.fixture
def no_dispatch(monkeypatch):
    seen: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        supertool, "dispatch",
        lambda a: (seen.append((a, os.environ.get("SUPERTOOL_REPO"))), "")[-1],
    )
    monkeypatch.setattr(supertool, "log_call", lambda *a, **k: None)
    return seen


# ---------------------------------------------------------------------------
# Core: which ops accept a target, and what shape it may take
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", ["gl-issue", "gl-mr", "gl-job", "gl-pipeline"])
def test_gl_read_ops_are_repo_targetable(op) -> None:
    assert op in supertool._repo_target_ops()


@pytest.mark.parametrize("op", ["gl-mrs", "gl-runners", "gl-api"])
def test_watch_fed_and_freeform_ops_stay_out(op) -> None:
    """#676 draws this boundary itself: radar acts on the watch-state key, and
    that key has no project dimension yet, so a target must refuse rather than
    be attributed to the wrong project. `gl-api` names its own path."""
    assert op not in supertool._repo_target_ops()


def test_subgroup_path_is_accepted_for_a_gitlab_call(no_dispatch) -> None:
    rc = supertool.main([f"repo:{TARGET}", "gl-issue:5"])

    assert rc == 0
    assert no_dispatch == [("gl-issue:5", TARGET)]


def test_two_segment_path_is_accepted_for_a_gitlab_call(no_dispatch) -> None:
    rc = supertool.main(["repo:group/project", "gl-mr:12"])

    assert rc == 0
    assert no_dispatch == [("gl-mr:12", "group/project")]


def test_github_call_still_refuses_a_subgroup_path(no_dispatch, capsys) -> None:
    """The GitLab loosening must not weaken the GitHub check (#676 point 2)."""
    rc = supertool.main([f"repo:{TARGET}", "gh-pr:1:status"])

    assert rc == 1
    assert no_dispatch == []
    assert "OWNER/NAME" in capsys.readouterr().err


def test_a_call_naming_both_forges_is_refused(no_dispatch, capsys) -> None:
    rc = supertool.main(["repo:group/project", "gh-pr:1:status", "gl-mr:12"])

    assert rc == 1
    assert no_dispatch == []
    err = capsys.readouterr().err
    assert "GitHub" in err and "GitLab" in err


def test_gl_mrs_refuses_the_whole_call(no_dispatch, capsys) -> None:
    rc = supertool.main([f"repo:{TARGET}", "gl-mrs"])

    assert rc == 1
    assert no_dispatch == []
    assert "gl-mrs" in capsys.readouterr().err


@pytest.mark.parametrize("target", [
    "group/sub group/project",     # space
    "group/../project",            # traversal segment
    "../project",
    "group//project",              # empty segment
    "group/pro?ject",              # query separator
    "https://evil.example/x/y",    # a host, not a project path
])
def test_targets_outside_the_project_path_charset_are_refused(
        target, no_dispatch, capsys) -> None:
    """This value is substituted into an API path and handed to a CLI that
    attaches the live token, so anything that is not a project path is refused
    rather than encoded and sent."""
    rc = supertool.main([f"repo:{target}", "gl-issue:5"])

    assert rc == 1
    assert no_dispatch == []
    assert capsys.readouterr().err.strip()


# ---------------------------------------------------------------------------
# presets/_repo_target.py — the GitLab half of the shared helper
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_target_mod():
    return _load("_repo_target.py", "_repo_target_676")


def test_gl_args_is_empty_without_a_target(repo_target_mod) -> None:
    assert repo_target_mod.gl_args() == []


def test_gl_args_carries_the_full_project_path(repo_target_mod, monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", TARGET)

    assert repo_target_mod.gl_args() == ["-R", TARGET]


def test_gl_api_path_is_identity_without_a_target(repo_target_mod) -> None:
    assert repo_target_mod.gl_api_path(
        "projects/:id/merge_requests/12") == "projects/:id/merge_requests/12"


@pytest.mark.parametrize("path,expected", [
    ("projects/:id", f"projects/{ENCODED}"),
    ("projects/:id/merge_requests/12", f"projects/{ENCODED}/merge_requests/12"),
    ("projects/:id/jobs?scope[]=pending&per_page=100",
     f"projects/{ENCODED}/jobs?scope[]=pending&per_page=100"),
])
def test_gl_api_path_substitutes_the_placeholder(repo_target_mod, monkeypatch,
                                                 path, expected) -> None:
    """Substituted, not accompanied: glab expands `:id` from the cwd, which is
    exactly what a target has to override. The `/` inside the project path is
    percent-encoded because it is one path segment, not three."""
    monkeypatch.setenv("SUPERTOOL_REPO", TARGET)

    assert repo_target_mod.gl_api_path(path) == expected


@pytest.mark.parametrize("path", [
    "projects/:idle/x",        # `:id` is a prefix of the segment, not the segment
    "groups/:id/projects",
    "user",
])
def test_gl_api_path_leaves_a_non_placeholder_path_alone(
        repo_target_mod, monkeypatch, path) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", TARGET)

    assert repo_target_mod.gl_api_path(path) == path


def test_gl_not_found_hint_names_the_target_rather_than_the_cwd(
        repo_target_mod, monkeypatch) -> None:
    """Under a target the cwd had no part in the lookup, so blaming it sends
    the reader to fix something that is not broken (#673, same argument)."""
    monkeypatch.setenv("SUPERTOOL_REPO", TARGET)
    hint = repo_target_mod.gl_not_found_hint()

    assert TARGET in hint
    assert "right repo" not in hint


# ---------------------------------------------------------------------------
# The presets actually send it
# ---------------------------------------------------------------------------

class _Recorder:
    def __init__(self, stdout: str = "[]") -> None:
        self.calls: list[list[str]] = []
        self.stdout = stdout

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, self.stdout, "")


def test_gl_issue_passes_the_target_to_glab_issue_view(monkeypatch) -> None:
    mod = _load("gitlab/issue.py", "gl_issue_676")
    monkeypatch.setenv("SUPERTOOL_REPO", TARGET)
    rec = _Recorder()
    monkeypatch.setattr(mod.subprocess, "run", rec)

    mod._glab(["issue", "view", "5", "--output", "json"])

    assert rec.calls == [["glab", "issue", "view", "5", "--output", "json",
                          "-R", TARGET]]


def test_gl_issue_api_calls_carry_the_project(monkeypatch) -> None:
    mod = _load("gitlab/issue.py", "gl_issue_676b")
    monkeypatch.setenv("SUPERTOOL_REPO", TARGET)
    rec = _Recorder()
    monkeypatch.setattr(mod.subprocess, "run", rec)

    mod._glab_api("projects/:id/issues/5/notes")

    assert rec.calls == [["glab", "api",
                          f"projects/{ENCODED}/issues/5/notes"]]


def test_gl_mr_passes_the_target_to_glab_mr_view(monkeypatch) -> None:
    mod = _load("gitlab/mr.py", "gl_mr_676")
    monkeypatch.setenv("SUPERTOOL_REPO", TARGET)
    rec = _Recorder()
    monkeypatch.setattr(mod.subprocess, "run", rec)

    mod._glab(["mr", "view", "12", "--output", "json"])

    assert rec.calls == [["glab", "mr", "view", "12", "--output", "json",
                          "-R", TARGET]]


def test_gl_mr_api_calls_carry_the_project(monkeypatch) -> None:
    mod = _load("gitlab/mr.py", "gl_mr_676b")
    monkeypatch.setenv("SUPERTOOL_REPO", TARGET)
    rec = _Recorder()
    monkeypatch.setattr(mod.subprocess, "run", rec)

    mod._glab_api("projects/:id/merge_requests/12/approvals")

    assert rec.calls == [["glab", "api",
                          f"projects/{ENCODED}/merge_requests/12/approvals"]]


def test_gl_pipeline_fetches_jobs_from_the_targeted_project(monkeypatch,
                                                            capsys) -> None:
    mod = _load("gitlab/pipeline.py", "gl_pipeline_676")
    monkeypatch.setenv("SUPERTOOL_REPO", TARGET)
    rec = _Recorder(stdout="[]")
    monkeypatch.setattr(mod.subprocess, "run", rec)
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "77"])

    mod.main()
    capsys.readouterr()

    assert rec.calls
    assert rec.calls[0][2] == f"projects/{ENCODED}/pipelines/77/jobs"


# ---------------------------------------------------------------------------
# The class, not the instance: no unwrapped `projects/:id` in an opted-in op
# ---------------------------------------------------------------------------

TARGETABLE_SOURCES = ["gitlab/issue.py", "gitlab/mr.py", "gitlab/job.py",
                      "gitlab/pipeline.py"]

#: A project path may reach glab only through something that substitutes the
#: target into it: `gl_api_path` itself, or a module-local api runner that
#: calls it on every endpoint it is handed. Anything else is a call site that
#: reads the cwd's project under a target, silently and well-formedly.
_SUBSTITUTING_CALLS = {"gl_api_path", "_glab_api", "_fetch_json", "_fetch_array"}


def _literal_prefix(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return ""


@pytest.mark.parametrize("rel", TARGETABLE_SOURCES)
def test_every_project_path_in_a_targetable_op_is_substituted(rel) -> None:
    """The next call site written here is the one that silently reads the wrong
    project, so the coupling is asserted rather than remembered."""
    tree = ast.parse((REPO_ROOT / "presets" / rel).read_text(encoding="utf-8"))

    wrapped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(
                func, "id", "")
            if name in _SUBSTITUTING_CALLS:
                wrapped.update(id(a) for a in node.args)
    nested = {id(v) for node in ast.walk(tree)
              if isinstance(node, ast.JoinedStr) for v in node.values}

    unwrapped = [ast.unparse(node) for node in ast.walk(tree)
                 if id(node) not in nested
                 and _literal_prefix(node).startswith("projects/:id")
                 and id(node) not in wrapped]

    assert unwrapped == []

# ---------------------------------------------------------------------------
# Review follow-ups: a call's forge is also evidenced by its write ops, and a
# payload route must name the key it actually reads
# ---------------------------------------------------------------------------

def test_a_payload_route_op_still_settles_the_forge(no_dispatch, capsys) -> None:
    """A write op naming its own target is still evidence of which forge this
    call is about. Reading the platform from `op`-mode ops alone let a GitLab
    project path through a GitHub-only call unremarked — refused a line later
    for a different reason, with the shape never mentioned."""
    rc = supertool.main([f"repo:{TARGET}", "gh-issue-create:@.max/x.toml"])

    assert rc == 1
    assert no_dispatch == []
    # Named, not merely refused: the payload refusal quotes `OWNER/NAME` too,
    # so asserting that substring would pass against no change at all.
    assert "GitLab project path" in capsys.readouterr().err


def test_gl_issue_create_names_its_own_payload_key(no_dispatch, capsys) -> None:
    """`gl-issue-create` takes `project`, not `repo`. Telling its caller to set
    `repo = ...` names a field the payload validator does not read, which is a
    worse answer than the generic refusal it used to get."""
    rc = supertool.main(["repo:group/project", "gl-issue-create:@.max/x.toml"])

    assert rc == 1
    assert no_dispatch == []
    err = capsys.readouterr().err
    assert "gl-issue-create" in err
    assert "payload" in err
    assert "project" in err
    assert "repo = " not in err


def test_gh_issue_create_still_names_repo(no_dispatch, capsys) -> None:
    assert "repo" in supertool._repo_refusal("gh-issue-create")


def test_pipeline_not_found_says_check_once(monkeypatch) -> None:
    """Two "check the number" clauses in one sentence, and "in this repo" twice
    with no target — the scope helper already carries both."""
    mod = _load("gitlab/pipeline.py", "gl_pipeline_676b")

    msg = mod._format_error("404 not found", "Pipeline", "77")

    assert msg.lower().count("check") == 1
