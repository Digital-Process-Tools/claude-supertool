"""#998 — no op listed a repo's labels, so every triage run hand-rolled one.

Before applying a label you have to know which labels exist; inventing one is
the failure `gh issue edit --add-label` does not protect you from. The only way
to answer it was a `gh label list --json … | jq | grep` pipeline — the exact
surface #454 exists because of — paid on the *first* call of every triage or
release-planning run, forever, by every fresh agent.

And the spelling is not portable: `claude-supertool` uses `priority-high`,
`claude-remember` uses `priority:high` and has no `lane-*` at all. An agent
carrying one repo's spelling into the other mislabels or silently no-ops.

What is pinned here is mostly the *third state*. A bare list is easy; a list
with usage counts has three ways to be wrong and each one is this repo's house
defect:

* the issue enumeration failed → every count is `?`, never `0`;
* the enumeration hit its cap → counts are a floor and say `>=`, because a
  label used only by issue 301 of 300 is otherwise reported as unused, and
  "unused" is the input to a decision to delete it;
* it read every open issue → `0` is a real zero and means the label is dead.
"""
from __future__ import annotations

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


labels = _load("presets/github/labels.py", "github_labels_998")

LABELS = [
    {"name": "priority-high", "description": "drop everything", "color": "b60205"},
    {"name": "priority-medium", "description": "", "color": "fbca04"},
    {"name": "priority-low", "description": "", "color": "0e8a16"},
    {"name": "lane-tracker-ops", "description": "the tracker family", "color": "1d76db"},
    {"name": "lane-watch", "description": "", "color": "1d76db"},
    {"name": "bug", "description": "something is wrong", "color": "d73a4a"},
]

ISSUES = [
    {"number": 1, "labels": [{"name": "bug"}, {"name": "priority-high"}]},
    {"number": 2, "labels": [{"name": "bug"}]},
    {"number": 3, "labels": [{"name": "lane-tracker-ops"}]},
    {"number": 4, "labels": []},
]


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _Gh:
    def __init__(self, label_rows=None, issue_rows=None,
                 labels_rc: int = 0, issues_rc: int = 0) -> None:
        self.label_rows = LABELS if label_rows is None else label_rows
        self.issue_rows = ISSUES if issue_rows is None else issue_rows
        self.labels_rc = labels_rc
        self.issues_rc = issues_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv, *a, **kw):
        argv = list(argv)
        self.calls.append(argv)
        joined = " ".join(argv)
        if "/labels" in joined:
            if self.labels_rc:
                return _Completed("", self.labels_rc, "HTTP 403")
            return _Completed(json.dumps(self.label_rows))
        if "issue" in joined or "/issues" in joined:
            if self.issues_rc:
                return _Completed("", self.issues_rc, "HTTP 500")
            return _Completed(json.dumps(self.issue_rows))
        return _Completed("[]")


def _render(monkeypatch, capsys, gh: _Gh, argv=("labels.py",)) -> str:
    monkeypatch.setattr(labels.subprocess, "run", gh)
    monkeypatch.setattr(sys, "argv", list(argv))
    labels.main()
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# the vocabulary itself
# ---------------------------------------------------------------------------

def test_every_label_is_listed(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, _Gh())
    for row in LABELS:
        assert row["name"] in out, f"{row['name']} missing:\n{out}"


def test_descriptions_are_carried(monkeypatch, capsys) -> None:
    """A name alone does not say what the label means, which is the decision."""
    out = _render(monkeypatch, capsys, _Gh())
    assert "the tracker family" in out, out


def test_names_are_grouped_by_their_prefix(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, _Gh())
    assert "priority-" in out and "lane-" in out, out


def test_a_colon_spelling_groups_too(monkeypatch, capsys) -> None:
    """`claude-remember` spells them `priority:high`. Same grouping question."""
    rows = [{"name": "priority:high", "description": "", "color": "x"},
            {"name": "priority:low", "description": "", "color": "x"}]
    out = _render(monkeypatch, capsys, _Gh(label_rows=rows, issue_rows=[]))
    assert "priority:" in out, out


def test_grouping_is_disclosed_as_inferred(monkeypatch, capsys) -> None:
    """GitHub has no prefix concept — a reader must not think it does."""
    out = _render(monkeypatch, capsys, _Gh())
    assert "infer" in out.lower() or "convention" in out.lower(), out


def test_a_lone_prefix_is_not_a_group(monkeypatch, capsys) -> None:
    """One `wontfix-ish` label does not make `wontfix-` a family."""
    rows = [{"name": "solo-thing", "description": "", "color": "x"},
            {"name": "bug", "description": "", "color": "x"}]
    out = _render(monkeypatch, capsys, _Gh(label_rows=rows, issue_rows=[]))
    assert "solo-thing" in out, out
    assert "## solo-" not in out, f"invented a family of one:\n{out}"


# ---------------------------------------------------------------------------
# counts, and the three states of a count
# ---------------------------------------------------------------------------

def test_counts_are_open_issues_carrying_the_label(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, _Gh())
    bug = [ln for ln in out.splitlines() if "bug" in ln][0]
    assert "2" in bug, f"bug is on 2 open issues:\n{bug}"


def test_an_unused_label_reads_zero_when_every_issue_was_read(
        monkeypatch, capsys) -> None:
    """A real zero is the whole value of the count — it is a delete candidate."""
    out = _render(monkeypatch, capsys, _Gh())
    low = [ln for ln in out.splitlines() if "priority-low" in ln][0]
    assert "0" in low, low
    assert "?" not in low, f"a readable zero rendered as unknown:\n{low}"


def test_a_failed_issue_read_renders_unknown_and_never_zero(
        monkeypatch, capsys) -> None:
    """The house defect: 'I did not look' must not print as 'nothing found'."""
    out = _render(monkeypatch, capsys, _Gh(issues_rc=1))
    body = [ln for ln in out.splitlines() if "priority-high" in ln][0]
    assert "?" in body, f"an unread count rendered as a number:\n{body}"
    assert "0" not in body.split("priority-high")[-1], (
        f"an unread count rendered as zero:\n{body}")
    assert "UNKNOWN" in out or "unknown" in out.lower(), (
        f"counts were unreadable and nothing says so:\n{out}")


def test_a_capped_enumeration_reports_a_floor(monkeypatch, capsys) -> None:
    """Past the cap a count is a lower bound, and must not read as exact."""
    many = [{"number": n, "labels": [{"name": "bug"}]} for n in range(400)]
    monkeypatch.setenv("GH_LABELS_ISSUE_CAP", "400")
    out = _render(monkeypatch, capsys, _Gh(issue_rows=many))
    assert ">=" in out or "at least" in out.lower(), (
        f"a capped enumeration reported exact counts:\n{out}")
    assert "GH_LABELS_ISSUE_CAP" in out, (
        f"the cap bit and the knob that governs it is unnamed:\n{out}")


def test_an_uncapped_enumeration_claims_no_floor(monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, _Gh())
    assert ">=" not in out, f"claimed a floor over a complete read:\n{out}"


# ---------------------------------------------------------------------------
# failure is a sentence, not a traceback or an empty list
# ---------------------------------------------------------------------------

def test_an_unreadable_label_list_is_an_error_not_an_empty_one(
        monkeypatch, capsys) -> None:
    monkeypatch.setattr(labels.subprocess, "run", _Gh(labels_rc=1))
    monkeypatch.setattr(sys, "argv", ["labels.py"])
    rc = labels.main()
    out = capsys.readouterr().out
    assert rc != 0, f"a failed read exited 0:\n{out}"
    assert "ERROR" in out, out
    assert "no labels" not in out.lower(), (
        f"an unreadable list rendered as an empty repo:\n{out}")


def test_a_genuinely_empty_repo_says_so_in_its_own_words(
        monkeypatch, capsys) -> None:
    out = _render(monkeypatch, capsys, _Gh(label_rows=[], issue_rows=[]))
    assert "ERROR" not in out, f"an empty label set is not a failure:\n{out}"
    assert "no labels" in out.lower(), out


def test_gh_missing_is_a_sentence(monkeypatch, capsys) -> None:
    def boom(*a, **kw):
        raise FileNotFoundError(2, "No such file or directory", "gh")

    monkeypatch.setattr(labels.subprocess, "run", boom)
    monkeypatch.setattr(sys, "argv", ["labels.py"])
    rc = labels.main()
    out = capsys.readouterr().out
    assert rc != 0 and "ERROR" in out, out
    assert "Traceback" not in out, out


def test_a_spawn_failure_on_the_issue_read_declines_rather_than_escaping(
        monkeypatch, capsys) -> None:
    """Windows raises FileNotFoundError where POSIX may not fail at all (#997).

    The label read having succeeded, a spawn failure on the *count* read must
    land on the `?` arm, not propagate out of `main`.
    """
    gh = _Gh()
    inner = gh.__call__

    def half_broken(argv, *a, **kw):
        if "/labels" in " ".join(argv):
            return inner(argv, *a, **kw)
        raise FileNotFoundError(2, "No such file or directory", "gh")

    monkeypatch.setattr(labels.subprocess, "run", half_broken)
    monkeypatch.setattr(sys, "argv", ["labels.py"])
    rc = labels.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "priority-high" in out, out
    assert "?" in out, f"a spawn failure produced counts anyway:\n{out}"
