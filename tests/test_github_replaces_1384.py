"""#1384 step 3 -- the remaining `gh` ops declare which raw call they supersede.

Steps 1 (`presets/git.json`, PR #1420) and 2 (`presets/gitlab.json`, #1393)
shipped. `presets/github.json` carried four `replaces` blocks against 21 ops,
so `gh issue view`, `gh issue create`, `gh pr list`, `gh run view`,
`gh run view --log` and `gh label list` were all allowed **silently** while
their GitLab twins were mapped.

Two things this file pins that are decisions rather than coverage:

* **`--web` / `-w` is excluded everywhere it exists.** No op opens a browser,
  so a mapping that claims `gh pr view 1 --web` is a dead end with no
  per-command way past -- the shape #1394 and #1420 both settled as the
  expensive direction. #1347's four shipped entries had no such exclusion;
  adding it is a widening of what stays usable, not of what blocks.
* **`gh run list` is mapped only through `--branch` / `--commit`.** Those are
  the two shapes `gh-branch` answers, and `gh run list --branch <default>
  --limit 1` is the exact command whose wrong answer `gh-branch` exists for
  (it returns whichever workflow started last). A bare `gh run list` is an
  enumeration no op produces and stays clean.

The absences below are asserted as hard as the presences: `replaces` has no
per-command escape hatch and `raw_command_guard: false` is repo-global, so one
over-broad `gh` entry disarms git's and GitLab's mappings with it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent
_GH_OPS = json.loads(
    (_ROOT / "presets" / "github.json").read_text(encoding="utf-8"))["ops"]


@pytest.fixture
def shipped_github(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The real github preset, as the effective registry.

    Fed through `ops` rather than `presets` so every assertion below is a
    statement about the file in this commit, not about preset resolution order.
    """
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": _GH_OPS}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    supertool._load_config()
    return tmp_path


def _uses(command: str):
    verdict = supertool.guard_command(command)
    assert verdict.state == "blocked", (command, verdict)
    return sorted(match.use for match in verdict.matches)


# --------------------------------------------------------------------------
# The mappings step 3 adds
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command,use", [
    ("gh issue view 1384", "gh-issue:NUMBER"),
    ("gh issue view 1384 --json body", "gh-issue:NUMBER"),
    ("gh issue view 1384 --comments", "gh-issue:NUMBER:full"),
    ("gh issue view 1384 -c", "gh-issue:NUMBER:full"),
    ("gh issue create --title x --body y", "gh-issue-create:@-"),
    ("gh pr list --state open --limit 50", "gh-prs"),
    ("gh run view 17123456", "gh-run:NUMBER[:attempt=K]"),
    # #1715: `--attempt` was already claimed by the bare entry — `unless_flag`
    # names only `--web`/`-w` — but the op it named could not serve it, so the
    # refusal was a dead end of exactly the kind the specificity rule exists to
    # prevent. The op can now, and the `use` string says so.
    ("gh run view 17123456 --attempt 1", "gh-run:NUMBER[:attempt=K]"),
    ("gh run view 17123456 --log", "gh-job:NUMBER:raw"),
    ("gh run view 17123456 --log-failed", "gh-job:NUMBER:fail"),
    ("gh run list --branch master --limit 1", "gh-branch:BRANCH"),
    ("gh run list --commit db6f205", "gh-branch:COMMIT_SHA"),
    ("gh label list", "gh-labels"),
    ("gh label list --limit 200", "gh-labels"),
    # Not decoration: without a blocking form the `--watch` exclusion below
    # would pass with `gh pr checks` unmapped, which is the state this commit
    # changes.
    ("gh pr checks 1424", "gh-pr:NUMBER:status"),
    ("gh pr view 1424 --comments", "gh-pr:NUMBER:full"),
])
def test_a_raw_gh_call_names_the_op_that_answers_it(shipped_github, command, use):
    assert _uses(command) == [use], command


def test_the_log_flag_beats_the_bare_run_view_entry(shipped_github):
    """`gh run view --log` is gh-job's question, not gh-run's.

    Both entries match the same argv; the flagged one scores higher, so the
    refusal names one op rather than two. Without this the reader is sent to
    `gh-run`, which lists jobs and never prints a log line.
    """
    verdict = supertool.guard_command("gh run view 17123456 --log")
    assert [m.op for m in verdict.matches] == ["gh-job"], verdict.matches


def test_the_refusal_carries_the_gh_op_own_words(shipped_github):
    verdict = supertool.guard_command("gh issue view 1384")
    text = supertool.guard_refusal(verdict)
    assert "gh-issue" in text
    # Read off the registry at match time, so it cannot describe a mode the op
    # no longer has.
    assert "linked PRs" in text


# --------------------------------------------------------------------------
# The exclusions: shapes of a mapped command that no op answers
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command,why", [
    ("gh issue view 1384 --web", "no op opens a browser"),
    ("gh issue view 1384 -w", "same, short spelling"),
    ("gh pr view 1424 --web", "same, and this one is a widening of #1347"),
    ("gh pr diff 1424 --web", "same"),
    ("gh pr list --web", "same"),
    ("gh issue list --web", "same"),
    ("gh issue create --web", "same, and this is how a long body is written"),
    ("gh pr create --web", "same"),
    ("gh run view 17123456 --web", "same"),
    ("gh label list --web", "same"),
    ("gh pr checks 1424 --watch", "gh-pr:NUMBER:status is one read, not a "
                                  "poller; `watch` is the op for polling and "
                                  "takes a PR, not a check list"),
    # The one asymmetry in this family, and it is evidence-driven rather than
    # a rule: `gh-prs` renders a BOARD and has no field-selection vocabulary,
    # and the maintainer skill's branch-reap recipe is a live user of exactly
    # this shape -- `gh pr list --state merged --limit 400 --json headRefName
    # -q '.[].headRefName'`, which no op produces. A missed block is the safe
    # direction; a dead end has no per-command way past.
    ("gh pr list --state merged --limit 400 --json headRefName",
     "no op emits arbitrary PR fields"),
    ("gh pr list --json number,title", "same"),
])
def test_an_excluded_shape_of_a_mapped_command_stays_usable(
        shipped_github, command, why):
    assert supertool.guard_command(command).state == "clean", (command, why)


# --------------------------------------------------------------------------
# The absences, which are load-bearing: the opt-out is repo-global
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command,why", [
    ("gh api repos/o/r/git/refs -X POST -f ref=refs/tags/v0.35.0",
     "the documented tagging route; no op writes a ref"),
    ("gh api -X DELETE repos/o/r/git/refs/heads/feat-x",
     "the documented ref-delete route, and gh-pr-merge|cleanup's own call"),
    ("gh api repos/o/r/actions/jobs/123/logs",
     "the documented fallback when `gh run view --log` comes back empty"),
    ("gh release create v0.35.0 --notes-file notes.md", "no release op"),
    ("gh run rerun 17123456 --failed", "no rerun op"),
    ("gh run list", "an enumeration no op produces; gh-branch answers for a "
                    "branch or a commit, which is why only those two flags "
                    "are claimed"),
    ("gh run list --limit 5", "same"),
    ("gh run list --workflow tests.yml", "same"),
    ("gh run download 17123456", "no artifact op"),
    ("gh run watch 17123456", "no run poller; `watch` polls a PR"),
    ("gh issue edit 1384 --add-label priority-high",
     "no label-writing op; the triager does this raw"),
    ("gh issue close 1384", "no close op"),
    ("gh pr checkout 1424", "no checkout op"),
    ("gh pr ready 1424", "no draft-state op"),
    ("gh pr comment 1424 --body x", "no comment op"),
    ("gh label create priority-high", "gh-labels reads; nothing writes one"),
    ("gh workflow run tests.yml", "no dispatch op"),
    ("gh repo view --json defaultBranchRef", "no repo op"),
    ("gh auth status", "no auth op"),
    ("gh cache list", "no cache op"),
    ("gh api user/starred/simonw/llm -X PUT",
     "gh-star's own route; the social ops all sit on `gh api`, which is the "
     "escape hatch and is never claimed"),
])
def test_a_gh_shape_with_no_supertool_answer_stays_usable(
        shipped_github, command, why):
    assert supertool.guard_command(command).state == "clean", (command, why)


@pytest.mark.parametrize("command", [
    "git push origin master",
    "glab mr view 5",
    "pytest tests/test_github_replaces_1384.py -q",
    "grep -rn 'gh issue view' docs",
    "echo 'gh label list'",
    "python3 supertool.py 'gh-issue:1384'",
])
def test_nothing_added_here_fires_on_an_unrelated_command(
        shipped_github, command):
    assert supertool.guard_command(command).state == "clean", command


def test_exactly_these_github_ops_declare_a_mapping():
    """The record of which absences were chosen, next to the ones that were not.

    Nine of twenty-two ops declare nothing. The reasons live in
    `tests/test_replaces_census_1384.py`, which partitions every preset op in
    the repository into mapped and deliberately-absent and cannot drift.
    """
    declared = {name for name, definition in _GH_OPS.items()
                if "replaces" in definition}
    assert declared == {"gh-issue", "gh-issue-create", "gh-issue-comment",
                        "gh-issues", "gh-job",
                        "gh-labels", "gh-branch", "gh-pr", "gh-pr-create",
                        "gh-pr-edit", "gh-pr-merge", "gh-prs",
                        "gh-run"}, sorted(declared)
