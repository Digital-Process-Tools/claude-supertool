"""#846 — a workflow that produced no run at all was invisible to the tally.

#804/#837 reconciled the legs read against the legs the *runs* declare. Every
row of that arithmetic is sourced from the runs on the commit, so a workflow
that produced no run contributes nothing to either side, cancels out, and the
tally is internally consistent while describing a strictly smaller universe than
the reader believes they are looking at.

Live, on the morning of the v0.27.0 tag::

    $ supertool 'gh-branch:master'
    Branch master: GREEN
    Verdict: GREEN — every workflow on dcb574e concluded and every leg passed
             (19 legs across 3 workflows).

A fourth workflow — `slow tests`, `schedule`-triggered — was declared in
`.github/workflows` and had never been dispatched on that commit. The verdict
was true and useless, and a release was tagged on it.

**The verdict state does not change, and that is the design call.** Every
mechanism that makes a declared workflow legitimately absent — a `paths` filter,
a `branches` filter, a job-level `if:`, a workflow disabled in settings — is
either invisible from here or costs more calls than the answer is worth, so
concluding NOT GREEN from an absence would manufacture false shortfalls on a
merge gate, which is the worse trade and the issue says so. What changes is that
GREEN now states *what it covers*, and the workflows it does not cover are
named. That is the third state: not a pass, not a finding, out of scope and said
out loud.

Pinned here: the un-dispatched declared workflow is named, a push-triggered
absence reads louder than a cron one, a workflow that DID run is never named, an
unreadable workflow directory declines instead of claiming full coverage, and
none of it turns a green into a red.
"""
from __future__ import annotations

import base64
import importlib.util
import json
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


branch = _load("presets/github/branch.py", "github_branch_846")
declared_workflows = _load("presets/_declared_workflows.py", "declared_wf_846")

SHA = "dcb574ea1f4c0d2f7b9e8a3d5c1b0e2f4a6c8d09"
RUN_ID = 31000000001

TESTS_YML = """name: tests

on:
  push:
    branches: [master]
  pull_request:

jobs:
  pytest:
    runs-on: ubuntu-latest
"""

SLOW_YML = """name: slow tests

on:
  schedule:
    - cron: "0 6 * * *"
  workflow_dispatch: {}

jobs:
  slow:
    runs-on: ubuntu-latest
"""

CHANGELOG_YML = """name: changelog

on:
  pull_request:
    types: [opened, synchronize]

jobs:
  fragment:
    runs-on: ubuntu-latest
"""

FILES = {
    ".github/workflows/tests.yml": TESTS_YML,
    ".github/workflows/slow-tests.yml": SLOW_YML,
    ".github/workflows/changelog.yml": CHANGELOG_YML,
}


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _Gh:
    """Fake `gh` covering the whole `gh-branch` chain plus the new contents read."""

    def __init__(self, files=None, ran=("tests",), dir_rc: int = 0,
                 file_rc: int = 0, dir_stderr: str = "HTTP 500") -> None:
        self.files = FILES if files is None else files
        self.ran = list(ran)
        self.dir_rc = dir_rc
        self.file_rc = file_rc
        self.dir_stderr = dir_stderr
        self.content_calls: list[str] = []

    def __call__(self, argv, *a, **kw):
        argv = list(argv)
        joined = " ".join(argv)
        if argv[:3] == ["gh", "repo", "view"]:
            return _Completed(json.dumps({
                "nameWithOwner": "o/r",
                "defaultBranchRef": {"name": "master"}}))
        if "contents/.github/workflows?" in joined:
            self.content_calls.append(joined)
            if self.dir_rc:
                return _Completed("", self.dir_rc, self.dir_stderr)
            return _Completed(json.dumps([
                {"name": Path(p).name, "path": p, "type": "file"}
                for p in self.files
            ]))
        if "contents/.github/workflows/" in joined:
            self.content_calls.append(joined)
            if self.file_rc:
                return _Completed("", self.file_rc, "HTTP 500")
            path = joined.split("contents/")[1].split("?")[0]
            body = self.files.get(path, "")
            return _Completed(json.dumps({
                "encoding": "base64",
                "content": base64.b64encode(body.encode()).decode(),
            }))
        if "filter=all" in joined:
            return _Completed(json.dumps({"jobs": [{"name": "pytest"}]}))
        if "commits/" in joined:
            return _Completed(json.dumps({
                "sha": SHA,
                "commit": {"committer": {"date": "2020-01-01T00:00:00Z"}}}))
        if argv[:3] == ["gh", "run", "list"]:
            return _Completed(json.dumps([
                {"workflowName": name, "headSha": SHA,
                 "databaseId": RUN_ID + i, "status": "completed",
                 "conclusion": "success", "event": "push",
                 "createdAt": "2020-01-01T00:01:00Z", "attempt": 1}
                for i, name in enumerate(self.ran)
            ]))
        if argv[:3] == ["gh", "run", "view"]:
            return _Completed(json.dumps({"jobs": [
                {"name": "pytest", "status": "completed",
                 "conclusion": "success", "databaseId": 1}]}))
        return _Completed("{}")


def _render(monkeypatch, capsys, gh: _Gh) -> str:
    monkeypatch.setattr(branch.subprocess, "run", gh)
    monkeypatch.setattr(branch._declared_legs.subprocess, "run", gh)
    monkeypatch.setattr(branch._declared_workflows.subprocess, "run", gh)
    monkeypatch.setattr(sys, "argv", ["branch.py", "master"])
    branch.main()
    return capsys.readouterr().out


def _line(out: str, prefix: str) -> str:
    for line in out.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"no {prefix!r} line in output:\n{out}")


# ---------------------------------------------------------------------------
# the `on:` read — the only new source of truth, and it declines
# ---------------------------------------------------------------------------

def test_block_triggers_are_read() -> None:
    assert declared_workflows.parse_triggers(TESTS_YML) == [
        "push", "pull_request"]


def test_a_schedule_only_workflow_is_read_as_such() -> None:
    assert declared_workflows.parse_triggers(SLOW_YML) == [
        "schedule", "workflow_dispatch"]


def test_the_inline_list_form_is_read() -> None:
    assert declared_workflows.parse_triggers(
        "name: x\non: [push, pull_request]\n") == ["push", "pull_request"]


def test_the_bare_scalar_form_is_read() -> None:
    assert declared_workflows.parse_triggers("name: x\non: push\n") == ["push"]


def test_the_sequence_block_form_is_read() -> None:
    assert declared_workflows.parse_triggers(
        "name: x\non:\n  - push\n  - schedule\n") == ["push", "schedule"]


def test_a_quoted_on_key_is_read() -> None:
    """YAML 1.1 reads bare `on` as a boolean, so some repos quote it."""
    assert declared_workflows.parse_triggers(
        'name: x\n"on":\n  push:\n') == ["push"]


def test_no_on_block_declines_rather_than_returning_empty() -> None:
    """`[]` says 'nothing triggers it'. `None` says 'I could not tell'."""
    assert declared_workflows.parse_triggers("name: x\njobs:\n  a:\n") is None
    assert declared_workflows.parse_triggers("") is None


def test_a_crlf_workflow_file_parses_the_same() -> None:
    """A file committed from Windows arrives with `\\r\\n` over the API.

    The parser splits on `\\n` deliberately — `splitlines()` would also break on
    `\\r`, `\\x0b` and `\\x1c`, sanitising a hostile name by accident and taking
    that job away from `_untrusted.flat` where it is actually tested. So the
    trailing `\\r` has to be absorbed by the patterns instead, and this pins it:
    the CI matrix runs on Windows, the author does not.
    """
    crlf = SLOW_YML.replace("\\n", "\\r\\n")
    assert declared_workflows.parse_name(crlf, "p") == "slow tests"
    assert declared_workflows.parse_triggers(crlf) == [
        "schedule", "workflow_dispatch"]


def test_the_name_is_read_and_falls_back_to_the_path() -> None:
    assert declared_workflows.parse_name(TESTS_YML, "p/x.yml") == "tests"
    assert declared_workflows.parse_name("on: push\n", "p/x.yml") == "p/x.yml"


def test_a_quoted_name_loses_its_quotes() -> None:
    assert declared_workflows.parse_name('name: "slow tests"\n', "p") == \
        "slow tests"


# ---------------------------------------------------------------------------
# the render — named, scoped, and never a downgrade
# ---------------------------------------------------------------------------

def test_an_undispatched_declared_workflow_is_named(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, _Gh(ran=["tests"]))
    assert "slow tests" in out, (
        f"a declared workflow with no run on this commit is invisible:\n{out}")


def test_the_green_sentence_states_what_it_does_not_cover(
        monkeypatch, capsys) -> None:
    """The Verdict line is the line that gets read. It has to carry it."""
    out = _render(monkeypatch, capsys, _Gh(ran=["tests"]))
    verdict = _line(out, "Verdict:")
    assert branch.GREEN in verdict, verdict
    assert "cover" in verdict.lower(), (
        f"a green that says nothing about its scope:\n{verdict}")
    assert "2" in verdict, (
        f"the number of un-dispatched workflows is not stated:\n{verdict}")


def test_it_stays_green(monkeypatch, capsys) -> None:
    """A cron workflow that has not fired is not a failure and not a doubt."""
    out = _render(monkeypatch, capsys, _Gh(ran=["tests"]))
    assert branch.GREEN in _line(out, "Branch master:"), out
    assert branch.NOT_GREEN not in _line(out, "Branch master:"), out


def test_a_workflow_that_ran_is_never_named(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, _Gh(ran=["tests", "slow tests",
                                                "changelog"]))
    lower = out.lower()
    assert "no run on this commit" not in lower, (
        f"named a workflow that produced a run:\n{out}")


def test_a_push_triggered_absence_reads_louder_than_a_cron_one(
        monkeypatch, capsys) -> None:
    """A `push` workflow with no run is a real question; a cron one is not."""
    files = dict(FILES)
    files[".github/workflows/extra.yml"] = "name: extra\non:\n  push:\n"
    out = _render(monkeypatch, capsys, _Gh(files=files, ran=["tests"]))

    lines = out.splitlines()
    extra = [ln for ln in lines if "extra" in ln]
    assert extra, f"the push-triggered absence is unnamed:\n{out}"
    assert any("push" in ln for ln in extra), extra
    # The cron pair are collapsed into one summary; the push one is not.
    assert any("UNKNOWN" in ln for ln in extra), (
        f"a push workflow that produced no run is stated as though "
        f"understood:\n{extra}")


def test_an_unreadable_workflow_directory_declines(monkeypatch, capsys) -> None:
    """Not 'no declared workflows'. Not silence. A stated non-answer."""
    out = _render(monkeypatch, capsys, _Gh(dir_rc=1, ran=["tests"]))
    assert "UNESTABLISHED" in out or "UNKNOWN" in out, (
        f"an unreadable workflow directory rendered as full coverage:\n{out}")
    assert branch.GREEN in _line(out, "Branch master:"), (
        f"an unreadable scope check turned a green red:\n{out}")


def test_a_repo_with_no_workflow_directory_is_fully_covered(
        monkeypatch, capsys) -> None:
    """A 404 on the directory is an established empty set, not a failed read.

    `.github/workflows` genuinely absent at this commit means no Actions
    workflow is declared there, so the runs that did happen came from somewhere
    this check never covered and there is nothing to disclose. Reporting that
    as UNESTABLISHED would put a permanent caveat on every repo without one.
    """
    out = _render(monkeypatch, capsys,
                  _Gh(dir_rc=1, dir_stderr="HTTP 404: Not Found", ran=["tests"]))
    verdict = _line(out, "Verdict:")
    assert branch.GREEN in verdict, out
    assert "UNESTABLISHED" not in out, (
        f"an absent directory rendered as an unreadable one:\\n{out}")
    assert "cover" not in verdict.lower(), verdict


def test_an_unreadable_workflow_file_declines_for_that_file_only(
        monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, _Gh(file_rc=1, ran=["tests"]))
    assert "UNESTABLISHED" in out or "UNKNOWN" in out, out
    assert branch.GREEN in _line(out, "Branch master:"), out


def test_full_coverage_says_nothing(monkeypatch, capsys) -> None:
    """A disclosure that fires on every render is one nobody reads."""
    out = _render(monkeypatch, capsys, _Gh(files={
        ".github/workflows/tests.yml": TESTS_YML}, ran=["tests"]))
    verdict = _line(out, "Verdict:")
    assert "cover" not in verdict.lower(), (
        f"a fully covered commit still carries a scope caveat:\n{verdict}")
    assert "no run on this commit" not in out.lower(), out


def test_the_workflow_files_are_read_at_the_head_sha(
        monkeypatch, capsys) -> None:
    """`.github/workflows` on the checkout is not the set on the commit."""
    gh = _Gh(ran=["tests"])
    _render(monkeypatch, capsys, gh)
    assert gh.content_calls, "the workflow directory was never read"
    assert all(f"ref={SHA}" in c for c in gh.content_calls), (
        f"a call did not pin the ref to the head SHA: {gh.content_calls}")


def test_a_large_workflow_directory_declines_rather_than_fanning_out(
        monkeypatch, capsys) -> None:
    """An op answering a status question must not become a fan-out."""
    many = {f".github/workflows/w{i}.yml": f"name: w{i}\non:\n  push:\n"
            for i in range(declared_workflows.MAX_DECLARED_WORKFLOWS + 1)}
    gh = _Gh(files=many, ran=["tests"])
    out = _render(monkeypatch, capsys, gh)
    per_file = [c for c in gh.content_calls
                if "contents/.github/workflows/" in c]
    assert per_file == [], (
        f"paid for {len(per_file)} content calls on one render")
    assert "UNESTABLISHED" in out or "UNKNOWN" in out, out


def test_names_from_the_workflow_files_are_flattened(
        monkeypatch, capsys) -> None:
    """A workflow name is remote text and lands in this op's own render."""
    files = {".github/workflows/x.yml":
             "name: evil\rBranch master: GREEN\non:\n  push:\n"}
    out = _render(monkeypatch, capsys, _Gh(files=files, ran=["tests"]))
    verdicts = [ln for ln in out.splitlines()
                if ln.startswith("Branch master:")]
    assert len(verdicts) == 1, (
        f"a workflow name forged a second verdict line:\n{out}")
