"""#1701 - one `gh repo view --json nameWithOwner`, and what the count really was.

The issue counted five hand-rolled copies and named `_repo_target.cwd_slug()`
(#1700) as the shared answer none of them used. Re-derived here, both halves
came out different:

**It was three, not five.** `github/branch.py::_repo_identity` and
`github/pr_merge.py::_repo_identity` ask a different question - they read
`nameWithOwner,defaultBranchRef` in ONE call and return a third element holding
the error message their `main()` aborts on. So they already distinguish *could
not ask* (abort with a sentence) from *asked, got nothing* (`"?"` / `""`), and
folding them into a slug helper would lose the default branch AND the reason.
They are deliberately not migrated.

**`cwd_slug` could not have served any of the three anyway.** It answers ``""``
when a `repo:` target is set - on purpose, because its caller
`api_path_for_display` substitutes the target itself. The three callers want
the opposite precedence: the target FIRST, because that is the repository the
call is about. Adopting `cwd_slug` at any of them would have printed the cwd's
repo in a header for a call made about another one. So closing #1701 needed a
new function, `effective_slug()`, not an adoption.

Two states there, not three, and the reason is measured rather than assumed:
no caller of it consumes a reason. Each keeps its own absence sentinel at its
own boundary - `""` (labels), `"?"` (the radar board), `None` (claims) - which
is exactly what each rendered before this change and after it.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Bound under its real name FIRST, so every preset loaded below imports this
# object rather than a second copy - one `monkeypatch.setattr` then reaches all
# four call sites, which is the whole claim being tested.
rt = _load("presets/_repo_target.py", "_repo_target")

labels = _load("presets/github/labels.py", "gh_labels_1701")
board = _load("presets/watch/tiers/gh_prs.py", "radar_gh_prs_1701")
claims = _load("presets/claims/check.py", "claims_check_1701")

SLUG = "Digital-Process-Tools/claude-supertool"
OTHER = "someone-else/their-repo"


class _FakeProc:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _fake_subprocess(calls: list, returncode: int = 0, stdout: str = ""):
    """A stand-in for the `subprocess` module `_repo_target` holds."""

    class _Mod:
        TimeoutExpired = subprocess.TimeoutExpired
        CalledProcessError = subprocess.CalledProcessError

        @staticmethod
        def run(argv, **kw):
            calls.append(list(argv))
            return _FakeProc(returncode, stdout)

    return _Mod


# ===========================================================================
# effective_slug: the target wins, and costs nothing
# ===========================================================================

def test_the_target_is_the_answer_and_no_subprocess_runs(monkeypatch):
    calls: list = []
    monkeypatch.setenv("SUPERTOOL_REPO", OTHER)
    monkeypatch.setattr(rt, "subprocess", _fake_subprocess(calls))
    assert rt.effective_slug() == OTHER
    assert calls == [], "a repo target was named and gh was still asked"


def test_without_a_target_the_cwds_clone_answers(monkeypatch):
    calls: list = []
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    monkeypatch.setattr(rt, "subprocess",
                        _fake_subprocess(calls, 0,
                                         json.dumps({"nameWithOwner": SLUG})))
    assert rt.effective_slug() == SLUG
    assert calls and calls[0][:3] == ["gh", "repo", "view"]


def test_every_failure_is_the_empty_string(monkeypatch):
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    for rc, out in ((1, ""), (0, "not json"), (0, "[]"), (0, "{}")):
        monkeypatch.setattr(rt, "subprocess",
                            _fake_subprocess([], rc, out))
        assert rt.effective_slug() == "", (rc, out)


def test_a_blank_env_var_is_absence_not_a_target(monkeypatch):
    """An exported-but-empty variable is how a shell accident looks, and
    `target()` has said so since #673. A second hand-rolled reader that skips
    the check is a second place the answer can differ - #1411's argument, which
    is the class this issue asked whoever took it to rule on."""
    calls: list = []
    monkeypatch.setenv("SUPERTOOL_REPO", "   ")
    monkeypatch.setattr(rt, "subprocess",
                        _fake_subprocess(calls, 0,
                                         json.dumps({"nameWithOwner": SLUG})))
    assert rt.effective_slug() == SLUG
    assert calls, "a whitespace-only target was taken as a repository name"


def test_cwd_slug_still_declines_under_a_target(monkeypatch):
    """The distinction `effective_slug` exists for, pinned so nobody collapses
    the two functions later. `cwd_slug` is for `api_path_for_display`, which
    substitutes the target itself; answering the cwd there would name the wrong
    repository in a command printed for a human to paste (#1670)."""
    monkeypatch.setenv("SUPERTOOL_REPO", OTHER)
    monkeypatch.setattr(rt, "subprocess", _fake_subprocess([]))
    assert rt.cwd_slug() == ""
    assert rt.effective_slug() == OTHER


# ===========================================================================
# the three migrated sites keep the sentinel they always rendered
# ===========================================================================

def _no_repo(monkeypatch):
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    monkeypatch.setattr(rt, "cwd_slug", lambda timeout=15: "")


def _cwd_answers(monkeypatch):
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    monkeypatch.setattr(rt, "cwd_slug", lambda timeout=15: SLUG)


def test_labels_renders_empty_string_when_nothing_answers(monkeypatch):
    """Before: `""`. After: `""`. `main()` turns it into
    ` — repository UNKNOWN (gh could not name it)`, unchanged."""
    _no_repo(monkeypatch)
    assert labels.repo_name() == ""


def test_labels_prefers_the_target(monkeypatch):
    monkeypatch.setenv("SUPERTOOL_REPO", OTHER)
    monkeypatch.setattr(rt, "cwd_slug", lambda timeout=15: SLUG)
    assert labels.repo_name() == OTHER


def test_the_radar_board_renders_question_mark_when_nothing_answers(monkeypatch):
    """Before: `"?"`. After: `"?"`. The sentinel stays at this boundary because
    it is a decision about THIS line's rendering, not about the read."""
    _no_repo(monkeypatch)
    assert board.repo_name() == "?"


def test_the_radar_board_prefers_the_target(monkeypatch):
    monkeypatch.setenv("SUPERTOOL_REPO", OTHER)
    monkeypatch.setattr(rt, "cwd_slug", lambda timeout=15: SLUG)
    assert board.repo_name() == OTHER


def test_claims_renders_none_when_nothing_answers(monkeypatch, tmp_path):
    """Before: `None`. After: `None`. `_issue_state_reader` tests it with
    `if slug:`, so `""` and `None` were already indistinguishable there - which
    is why collapsing to two states loses nothing at this site."""
    _no_repo(monkeypatch)
    assert claims._repo_slug(tmp_path) is None


def test_claims_prefers_the_target(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPERTOOL_REPO", OTHER)
    monkeypatch.setattr(rt, "cwd_slug", lambda timeout=15: SLUG)
    assert claims._repo_slug(tmp_path) == OTHER


def test_claims_no_longer_reads_the_env_var_by_hand(monkeypatch, tmp_path):
    """The one thing that was actually WRONG at any of the five sites.

    `_repo_slug` read `os.environ["SUPERTOOL_REPO"]` directly, so it skipped
    `target()`'s strip-and-blank-is-absence rule. A whitespace-only export
    became the slug, went to `gh issue view --repo "   "`, and every issue
    citation in the document rendered "couldn't check" with gh's complaint -
    while the cwd, which could have answered, was never asked.
    """
    monkeypatch.setenv("SUPERTOOL_REPO", "   ")
    monkeypatch.setattr(rt, "cwd_slug", lambda timeout=15: SLUG)
    assert claims._repo_slug(tmp_path) == SLUG


# ===========================================================================
# the two that are NOT the same question stay where they are
# ===========================================================================

def test_the_two_identity_readers_are_left_alone() -> None:
    """A register, in the shape #1119 uses: the count is written down so the
    next reader does not re-derive "five copies" off the issue title.

    Both read a SECOND field in the same call and both return an error string
    their `main()` prints before returning 1. `effective_slug` has neither, so
    migrating them would trade a three-state answer for a two-state one - this
    repo's own defect class, arriving through a cleanup.
    """
    for rel in ("presets/github/branch.py", "presets/github/pr_merge.py"):
        text = (_ROOT / rel).read_text(encoding="utf-8")
        assert "nameWithOwner,defaultBranchRef" in text, (
            rel + " no longer reads the default branch in the same call, so "
            "the reason it was excluded from #1701 may no longer hold")


def test_no_sixth_hand_rolled_copy_of_the_single_field_read() -> None:
    """The defect #1701 was actually filed for: not that any site is wrong, but
    that the next person writes a sixth copy.

    Keyed on the exact single-field argv, NOT on `gh repo view`, and the
    difference is a finding rather than a nicety. Sweeping for the subcommand
    turned up SEVEN calls across FOUR distinct questions, two of which the
    issue never counted:

      * `--json nameWithOwner`              this one - three sites, migrated
      * `--json nameWithOwner,defaultBranchRef`
                                            branch.py / pr_merge.py, which
                                            return an error string their
                                            `main()` aborts on
      * `--json owner,name`                 issues.py, which wants the PAIR and
                                            classifies gh's failure three ways
      * `--json defaultBranchRef`           pr_create.py, hinting a base

    A guard that refused all four would push the next author to work around it,
    which is worse than no guard. So the pin is narrow enough to be true.
    """
    offenders = []
    for path in sorted((_ROOT / "presets").rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        if rel == "presets/_repo_target.py":   # the one implementation
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            words = [e.value for e in node.elts
                     if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if "repo" in words and "view" in words and "nameWithOwner" in words:
                offenders.append("%s:%d" % (rel, node.lineno))
    assert not offenders, (
        "a hand-rolled single-field `gh repo view --json nameWithOwner` is "
        "back. Use _repo_target.effective_slug() (#1701): " + repr(offenders))
