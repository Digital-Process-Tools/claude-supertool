"""Applying `no-changelog` must be able to clear the run that asked for it (#1722).

`changelog.yml` printed its own escape hatch -- "label the PR 'no-changelog' if
it genuinely announces nothing" -- and taking that branch could not clear the
PR. Applying the label starts a fresh run, which passes; nothing removes the run
that failed before the label existed, and the merge gate is conjunctive over
every run on the head sha (#1640), correctly so. Re-running the failed run does
not help either: a re-run replays the **event payload the run was created with**,
so `github.event.pull_request.labels` is the set as it stood before the label.
Measured live on PR #1721, twice.

The failing run was not wrong about anything it could see. It was answering a
question about a label set that no longer existed, and rendering that as a
`finding` -- indistinguishable from "this PR genuinely needs a fragment" -- is
the defect: a run whose input is stale is a run that cannot answer.

The fix reads the labels **live** off `repos/{repo}/issues/{pr}/labels`, so a
re-run sees the current set and the printed remedy becomes true. The stated cost
is real and is asserted below in both directions:

* the verdict is no longer reproducible from the run's own payload -- a label
  *removed* after the run started now reddens it (and must, or the escape hatch
  outlives the decision to use it); and
* the live read can fail. That arm degrades to the payload -- exactly the
  behaviour this job had before #1722 -- so it can turn a finding into a skip
  and never the reverse. An API outage cannot redden a board it did not redden
  yesterday. It says so, and it stops promising a re-run it can no longer honour.

Every "must not fire" case below is paired with a "must fire" case in the same
fixture, because a fragment gate that has stopped firing at all satisfies every
negative assertion in the file.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from _changelog_gate import (  # noqa: E402
    base_repo, gate_step, gh_calls, gh_stub, git, run_gate, touch_core,
    workflow_jobs,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the gate is a bash `run:` block on ubuntu-latest; there is no "
           "Windows leg of it to be honest about",
)

REPO_SLUG = "Digital-Process-Tools/claude-supertool"


def unannounced_pr(tmp_path) -> Path:
    """A PR that changes the core and adds no fragment -- the #1721 shape."""
    repo = base_repo(tmp_path)
    touch_core(repo)
    git(repo, "commit", "-am", "a five-line comment edit, announcing nothing")
    return repo


# ---------------------------------------------------------------------------
# The label the run could not see
# ---------------------------------------------------------------------------

def test_a_label_applied_after_the_run_started_clears_the_gate(tmp_path) -> None:
    """The #1722 case: payload has no label, the PR does. A re-run must pass."""
    repo = unannounced_pr(tmp_path)
    bindir = gh_stub(tmp_path, labels=["no-changelog"])

    res = run_gate(repo, labels="", pr_number="1721", gh_repo=REPO_SLUG,
                   stub_bin=bindir)

    assert res.returncode == 0, (
        "the label was on the PR and the gate still failed -- the remedy it "
        "prints cannot clear the run that printed it:\n" + res.stdout + res.stderr)
    assert "skipped" in res.stdout, res.stdout + res.stderr


def test_the_gate_still_fires_when_the_live_read_shows_no_such_label(tmp_path) -> None:
    """The must-fire half. Same fixture, same stub, an empty label set."""
    repo = unannounced_pr(tmp_path)
    bindir = gh_stub(tmp_path, labels=["priority-medium"])

    res = run_gate(repo, labels="", pr_number="1721", gh_repo=REPO_SLUG,
                   stub_bin=bindir)

    assert res.returncode != 0, (
        "a user-visible change with no fragment and no label passed:\n"
        + res.stdout + res.stderr)
    assert "finding" in res.stdout, res.stdout + res.stderr


def test_the_live_read_names_the_pr_and_the_repository(tmp_path) -> None:
    """A gate that never asks looks identical to one that asked and was told no."""
    repo = unannounced_pr(tmp_path)
    bindir = gh_stub(tmp_path, labels=["no-changelog"])

    run_gate(repo, labels="", pr_number="1721", gh_repo=REPO_SLUG, stub_bin=bindir)

    calls = gh_calls(bindir)
    assert calls, "the gate never called `gh` -- it is still reading the payload"
    assert any("repos/" + REPO_SLUG + "/issues/1721/labels" in call for call in calls), (
        "the live read did not ask for this PR's labels: " + repr(calls))


def test_a_label_removed_after_the_run_started_reddens_the_gate(tmp_path) -> None:
    """The stated cost of a live read, asserted rather than described.

    The payload says `no-changelog`; the PR no longer does. Live wins, and it
    has to: an escape hatch that outlives the decision to use it is the same
    defect pointing the other way.
    """
    repo = unannounced_pr(tmp_path)
    bindir = gh_stub(tmp_path, labels=[])

    res = run_gate(repo, labels="no-changelog", pr_number="1721",
                   gh_repo=REPO_SLUG, stub_bin=bindir)

    assert res.returncode != 0, (
        "the label was removed from the PR and the stale payload still "
        "excused it:\n" + res.stdout + res.stderr)


# ---------------------------------------------------------------------------
# The arm that will bite: the live read fails
# ---------------------------------------------------------------------------

def test_a_failed_live_read_falls_back_to_the_payload_and_still_skips(tmp_path) -> None:
    """Fail-open to *yesterday's* behaviour, not to a pass."""
    repo = unannounced_pr(tmp_path)
    bindir = gh_stub(tmp_path, labels=None, exit_code=1)

    res = run_gate(repo, labels="no-changelog", pr_number="1721",
                   gh_repo=REPO_SLUG, stub_bin=bindir)

    assert res.returncode == 0, (
        "an unreachable API broke the escape hatch that worked before #1722:\n"
        + res.stdout + res.stderr)
    assert "skipped" in res.stdout, res.stdout + res.stderr


def test_a_failed_live_read_does_not_wave_a_missing_fragment_through(tmp_path) -> None:
    """The must-fire half of the failure arm: degraded is not permissive."""
    repo = unannounced_pr(tmp_path)
    bindir = gh_stub(tmp_path, labels=None, exit_code=1)

    res = run_gate(repo, labels="", pr_number="1721", gh_repo=REPO_SLUG,
                   stub_bin=bindir)

    assert res.returncode != 0, (
        "a failed label read waved an unannounced core change through:\n"
        + res.stdout + res.stderr)
    assert "finding" in res.stdout, res.stdout + res.stderr


def test_a_failed_live_read_says_it_could_not_read_live(tmp_path) -> None:
    """A degraded read that renders identically to a clean one is the whole class."""
    repo = unannounced_pr(tmp_path)
    bindir = gh_stub(tmp_path, labels=None, exit_code=1)

    res = run_gate(repo, labels="", pr_number="1721", gh_repo=REPO_SLUG,
                   stub_bin=bindir)
    out = res.stdout

    assert "live label read failed" in out, (
        "the run fell back to a stale payload and said nothing about it:\n" + out)
    assert "event payload" in out, out


def test_a_gate_that_read_the_payload_does_not_promise_a_re_run(tmp_path) -> None:
    """The remedy has to describe the mechanism actually in use.

    Paired with the test below, which asserts the other branch on the same
    fixture -- so neither can pass by the remedy block never being reached.
    """
    repo = unannounced_pr(tmp_path)
    bindir = gh_stub(tmp_path, labels=None, exit_code=1)

    res = run_gate(repo, labels="", pr_number="1721", gh_repo=REPO_SLUG,
                   stub_bin=bindir)
    out = res.stdout

    assert res.returncode != 0, out + res.stderr
    assert "push a commit" in out, (
        "this run cannot see a label applied from now on, and told the reader "
        "to label the PR anyway:\n" + out)
    assert "re-run this check" not in out, (
        "it promised a re-run it cannot honour:\n" + out)


def test_a_gate_that_read_live_promises_a_re_run(tmp_path) -> None:
    repo = unannounced_pr(tmp_path)
    bindir = gh_stub(tmp_path, labels=[])

    res = run_gate(repo, labels="", pr_number="1721", gh_repo=REPO_SLUG,
                   stub_bin=bindir)
    out = res.stdout

    assert res.returncode != 0, out + res.stderr
    assert "no-changelog" in out, "the escape hatch is no longer named: " + out
    assert "re-run this check" in out, (
        "the labels were read live, so a re-run *is* the remedy, and the "
        "receipt does not say so:\n" + out)
    assert "push a commit" not in out, out


# ---------------------------------------------------------------------------
# No PR context at all
# ---------------------------------------------------------------------------

def test_without_a_pr_number_the_payload_still_works(tmp_path) -> None:
    repo = unannounced_pr(tmp_path)
    bindir = gh_stub(tmp_path, labels=[])

    res = run_gate(repo, labels="no-changelog", stub_bin=bindir)

    assert res.returncode == 0, res.stdout + res.stderr
    assert "skipped" in res.stdout, res.stdout
    assert gh_calls(bindir) == [], (
        "no PR number was in scope and the gate called `gh` anyway: "
        + repr(gh_calls(bindir)))


def test_without_a_pr_number_the_gate_still_fires(tmp_path) -> None:
    repo = unannounced_pr(tmp_path)
    bindir = gh_stub(tmp_path, labels=["no-changelog"])

    res = run_gate(repo, labels="", stub_bin=bindir)

    assert res.returncode != 0, res.stdout + res.stderr
    assert "finding" in res.stdout, res.stdout


# ---------------------------------------------------------------------------
# Nothing above may have cost the gate its day job
# ---------------------------------------------------------------------------

def test_a_pr_with_a_fragment_is_still_ok(tmp_path) -> None:
    repo = base_repo(tmp_path)
    touch_core(repo)
    (repo / "changelog.d" / "1722.fixed.md").write_text(
        "- **Mine** ([#1722](x)). Body.\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "core change with a fragment")
    bindir = gh_stub(tmp_path, labels=[])

    res = run_gate(repo, labels="", pr_number="1722", gh_repo=REPO_SLUG,
                   stub_bin=bindir)

    assert res.returncode == 0, res.stdout + res.stderr
    assert "ok" in res.stdout and "1722.fixed.md" in res.stdout, res.stdout


def test_a_docs_only_pr_is_still_skipped(tmp_path) -> None:
    repo = base_repo(tmp_path)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "docs")
    bindir = gh_stub(tmp_path, labels=[])

    res = run_gate(repo, labels="", pr_number="1722", gh_repo=REPO_SLUG,
                   stub_bin=bindir)

    assert res.returncode == 0, res.stdout + res.stderr
    assert "skipped" in res.stdout, res.stdout


# ---------------------------------------------------------------------------
# The wiring, read structurally (#731)
# ---------------------------------------------------------------------------

def test_the_step_is_handed_the_pr_number_and_the_repository() -> None:
    env = gate_step().env or {}
    assert "PR_NUMBER" in env, (
        "the gate cannot address the PR whose labels it reads: " + repr(env))
    assert "pull_request.number" in env["PR_NUMBER"], env["PR_NUMBER"]
    assert "GH_REPO" in env and "GH_TOKEN" in env, repr(env)


def test_the_payload_labels_are_still_wired_as_the_fallback() -> None:
    env = gate_step().env or {}
    assert "pull_request.labels" in env.get("LABELS", ""), (
        "the fallback the failure arm degrades to is not wired: " + repr(env))


def test_the_workflow_asks_for_the_read_scope_the_live_call_needs() -> None:
    """A `pull-requests: read` that is not declared is a 404 on every PR."""
    text = (Path(__file__).resolve().parents[1]
            / ".github" / "workflows" / "changelog.yml").read_text(encoding="utf-8")
    head = text.split("jobs:", 1)[0]
    assert "pull-requests: read" in head, (
        "the workflow's top-level permissions block does not grant the scope "
        "the live label read needs:\n" + head)
    assert workflow_jobs(), "parsed no jobs"
