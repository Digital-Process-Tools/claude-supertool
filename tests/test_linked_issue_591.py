"""#591 — the closing keyword was optional, so `Issue:` reported any `#N`.

Both GitHub renderers carried the same pattern:

    (?:closes|fixes|resolves)?\\s*#(\\d+)

The `?` reduces it to "the first `#<digits>` anywhere in the body". A body that
cites a precedent before naming its own subject — the well-written body — then
gets a confident `Issue: #263` for a PR that closes #591. A stated wrong number
is acted on; a missing one sends the reader to the body, so this failed in the
worse direction.

These tests pin the *discrimination*, not the extraction. A fixture asserting
"some number is printed" passes on the broken code, and so does one asserting
`#591` appears in a body whose only reference is `#591`. Every test here names
the number that must appear *and* the number that must not, or pins the absence
leg as its own distinguishable sentence.
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent

_CHECKS_PATH = _ROOT / "presets" / "_checks.py"
_c_spec = importlib.util.spec_from_file_location("checks_591", _CHECKS_PATH)
assert _c_spec is not None and _c_spec.loader is not None
checks = importlib.util.module_from_spec(_c_spec)
_c_spec.loader.exec_module(checks)

_STATUS_PATH = _ROOT / "presets" / "git" / "status.py"
_s_spec = importlib.util.spec_from_file_location("git_status_591", _STATUS_PATH)
assert _s_spec is not None and _s_spec.loader is not None
status = importlib.util.module_from_spec(_s_spec)
_s_spec.loader.exec_module(status)

_PR_PATH = _ROOT / "presets" / "github" / "pr.py"
_p_spec = importlib.util.spec_from_file_location("github_pr_591", _PR_PATH)
assert _p_spec is not None and _p_spec.loader is not None
prmod = importlib.util.module_from_spec(_p_spec)
_p_spec.loader.exec_module(prmod)


# The body from the issue: a precedent cited first, the real subject last.
PRECEDENT_BODY = (
    "This fixes the same class of bug as #263 (a precedent, not what this PR "
    "closes).\n\nCloses #591\n"
)


# ---------------------------------------------------------------------------
# The extractor, as a pure function — no gh, no git
# ---------------------------------------------------------------------------

def test_a_precedent_cited_before_the_closing_keyword_is_not_claimed() -> None:
    """The whole defect, in one assertion pair."""
    assert checks.closing_issue_refs(PRECEDENT_BODY) == ["#591"]


def test_a_bare_mention_with_no_keyword_anywhere_claims_nothing() -> None:
    """`see #454` is not a closing reference to GitHub, so not to us either."""
    assert checks.closing_issue_refs("Background: see #454 for the history.") == []


def test_a_keyword_not_bound_to_a_number_claims_nothing() -> None:
    """Over-matching is the same defect with extra steps.

    `fixes` appears, and so does `#263` — but not together. Accepting this
    reintroduces "the first number in the body" through the back door.
    """
    assert checks.closing_issue_refs("This fixes the bug filed as #263") == []
    assert checks.closing_issue_refs("A fix. Also #12 exists.") == []


def test_every_closing_reference_is_reported_not_just_the_first() -> None:
    """#584 closed #571 and #572. Picking one is the same defect, smaller."""
    body = "Closes #571 and closes #572"
    assert checks.closing_issue_refs(body) == ["#571", "#572"]


def test_github_s_full_keyword_set_is_honoured() -> None:
    """GitHub's set decides whether merging closes the issue. Match it."""
    for word in ("close", "closes", "closed", "fix", "fixes", "fixed",
                 "resolve", "resolves", "resolved"):
        assert checks.closing_issue_refs(f"{word} #591") == ["#591"], word
        assert checks.closing_issue_refs(f"{word.title()} #591") == ["#591"], word
        assert checks.closing_issue_refs(f"{word.upper()} #591") == ["#591"], word


def test_the_separator_shapes_github_accepts_are_accepted() -> None:
    """`Closes: #591`, double space, `GH-591` — all close the issue on GitHub."""
    assert checks.closing_issue_refs("Closes: #591") == ["#591"]
    assert checks.closing_issue_refs("closes  #591") == ["#591"]
    assert checks.closing_issue_refs("Fixed #591") == ["#591"]
    assert checks.closing_issue_refs("Closes GH-591") == ["#591"]


def test_a_keyword_separated_by_a_line_break_is_not_bound_to_the_number() -> None:
    """`\\s*` spanning newlines is the optional-keyword bug in a thin disguise.

    "This fixes\\n\\n#263 is a precedent" would extract #263 from a body whose
    keyword belongs to a sentence that never names a number.
    """
    assert checks.closing_issue_refs("This fixes\n\n#263 is a precedent") == []


def test_cross_repo_and_url_references_keep_the_repo_they_name() -> None:
    """`#5` and `octo/other#5` are different issues. Never flatten one to the other."""
    assert checks.closing_issue_refs("Closes octo/other#5") == ["octo/other#5"]
    assert checks.closing_issue_refs(
        "Fixes https://github.com/octo/other/issues/5") == ["octo/other#5"]


def test_repeated_references_are_deduped_in_order() -> None:
    body = "Closes #591\n\nAlso closes #571.\nAnd, again, fixes #591."
    assert checks.closing_issue_refs(body) == ["#591", "#571"]


def test_a_missing_or_empty_body_claims_nothing() -> None:
    assert checks.closing_issue_refs("") == []
    assert checks.closing_issue_refs(None) == []


def test_the_absence_leg_is_its_own_sentence_and_names_no_number() -> None:
    """Third state: "declares no closing reference", not nothing and not a guess."""
    line = checks.NO_CLOSING_REF
    assert "none declared" in line
    assert "#N" in line
    assert not any(ch.isdigit() for ch in line.replace("#N", ""))
    # `UNKNOWN` is the check tally's word for a state it declined to conclude,
    # and this line prints in the same `git-status` block. The first wording used
    # it and turned `test_fresh_commit_on_an_open_pr_says_not_yet` red, which is
    # the collision working as designed: a second unrelated UNKNOWN in that
    # output reads as a check verdict. It also overstated — a Development-panel
    # link closes an issue on merge and is not in the body — so the claim is
    # scoped to the body instead.
    assert "UNKNOWN" not in line
    assert "in the body" in line


def test_the_render_helper_pluralises_and_lists_every_reference() -> None:
    assert checks.linked_issue_line(["#591"]) == "Issue: #591"
    assert checks.linked_issue_line(["#571", "#572"]) == "Issues: #571, #572"
    assert checks.linked_issue_line([]) == f"Issue: {checks.NO_CLOSING_REF}"


# ---------------------------------------------------------------------------
# git-status — the rendered `Issue:` line
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "f").write_text("x\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "checkout", "-b", "fix/591")
    (repo / "g").write_text("y\n")
    _git(repo, "add", "g")
    _git(repo, "commit", "-m", "the work")
    return repo


def _status_payload(head_oid: str, body: str) -> dict:
    return {
        "number": 591,
        "title": "fix: linked issue keyword",
        "state": "OPEN",
        "baseRefName": "master",
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        "headRefOid": head_oid,
        "body": body,
        "additions": 1,
        "deletions": 1,
        "changedFiles": 1,
    }


def _run_status(repo: Path, monkeypatch, body: str) -> str:
    real_run = subprocess.run
    head = _git(repo, "rev-parse", "HEAD")
    payload = _status_payload(head, body)

    def fake_run(args, *a, **kw):
        argv = [str(x) for x in args]
        if argv and argv[0] == "gh":
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=json.dumps(payload), stderr="")
        if argv and argv[0] == "glab":
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(status.subprocess, "run", fake_run)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(status.sys, "argv", ["status.py"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert status.main() == 0
    return buf.getvalue()


def _issue_line(out: str) -> str:
    return next(l for l in out.splitlines()
                if l.startswith("Issue: ") or l.startswith("Issues: "))


def test_git_status_reports_the_closed_issue_not_the_precedent(
        tmp_path: Path, monkeypatch) -> None:
    out = _run_status(_init_repo(tmp_path), monkeypatch, PRECEDENT_BODY)
    assert _issue_line(out) == "Issue: #591"
    assert "#263" not in _issue_line(out)


def test_git_status_states_the_absence_rather_than_the_first_number(
        tmp_path: Path, monkeypatch) -> None:
    out = _run_status(_init_repo(tmp_path), monkeypatch,
                      "Background: see #454 for the history.")
    line = _issue_line(out)
    assert "none declared" in line
    assert "#454" not in line


def test_git_status_lists_both_closed_issues(tmp_path: Path, monkeypatch) -> None:
    out = _run_status(_init_repo(tmp_path), monkeypatch,
                      "Closes #571 and closes #572")
    assert _issue_line(out) == "Issues: #571, #572"


def test_git_status_never_prints_a_bare_issue_line_with_no_evidence(
        tmp_path: Path, monkeypatch) -> None:
    """An empty body must still produce the stated third state, not silence."""
    out = _run_status(_init_repo(tmp_path), monkeypatch, "")
    assert "none declared" in _issue_line(out)


# ---------------------------------------------------------------------------
# gh-pr — which issue is *fetched*, not just which number is printed
# ---------------------------------------------------------------------------

def _pr_payload(body: str, **over: Any) -> dict:
    base = {
        "number": 591,
        "title": "fix: linked issue keyword",
        "state": "OPEN",
        "author": {"login": "max"},
        "headRefName": "fix/591",
        "baseRefName": "master",
        "labels": [],
        "milestone": None,
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": None,
        "reviews": [],
        "mergeCommit": None,
        "additions": 1,
        "deletions": 1,
        "changedFiles": 1,
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        "url": "https://github.com/Digital-Process-Tools/claude-supertool/pull/591",
        "body": body,
        "comments": [],
        "assignees": [],
        "createdAt": "2026-07-30T00:00:00Z",
        "updatedAt": "2026-07-30T00:00:00Z",
    }
    base.update(over)
    return base


class _Gh:
    """Fake `gh` that answers `issue view` separately from `pr view`.

    The ledger is the point: the number the renderer *fetched* is the number it
    believes the PR closes, and asserting on printed text alone would miss a
    renderer that fetches #263 and prints #591.
    """

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.issues_fetched: list[str] = []

    def __call__(self, args: list[str], **kwargs: Any) -> Any:
        argv = [str(a) for a in args]
        if argv and argv[0] == "git":
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="boom")
        if argv[:3] == ["gh", "issue", "view"]:
            num = argv[3]
            self.issues_fetched.append(num)
            return subprocess.CompletedProcess(
                args=args, returncode=0, stderr="",
                stdout=json.dumps({
                    "number": int(num), "title": f"the issue {num}",
                    "state": "OPEN", "labels": [], "assignees": [],
                }))
        return subprocess.CompletedProcess(
            args=args, returncode=0, stderr="", stdout=json.dumps(self.payload))


def _run_pr(monkeypatch, gh: _Gh, *argv: str) -> str:
    monkeypatch.setattr(prmod.subprocess, "run", gh)
    monkeypatch.setattr(sys, "argv", ["pr.py", *argv])
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert prmod.main() == 0
    return buf.getvalue()


def test_gh_pr_fetches_the_closed_issue_not_the_precedent(monkeypatch) -> None:
    gh = _Gh(_pr_payload(PRECEDENT_BODY))
    out = _run_pr(monkeypatch, gh, "591")
    assert gh.issues_fetched == ["591"]
    assert "## Issue #591 — the issue 591" in out
    assert "## Issue #263" not in out


def test_gh_pr_fetches_every_closed_issue(monkeypatch) -> None:
    gh = _Gh(_pr_payload("Closes #571 and closes #572"))
    out = _run_pr(monkeypatch, gh, "591")
    assert gh.issues_fetched == ["571", "572"]
    assert "## Issue #571 — the issue 571" in out
    assert "## Issue #572 — the issue 572" in out


def test_gh_pr_states_the_absence_and_fetches_nothing(monkeypatch) -> None:
    gh = _Gh(_pr_payload("Background: see #454 for the history."))
    out = _run_pr(monkeypatch, gh, "591")
    assert gh.issues_fetched == []
    assert "none declared" in out
    assert "## Issue #454" not in out


def test_gh_pr_never_resolves_a_cross_repo_number_in_this_repo(monkeypatch) -> None:
    """`gh issue view 5` here is a *different* issue #5. Print, do not fetch."""
    gh = _Gh(_pr_payload("Closes octo/other#5"))
    out = _run_pr(monkeypatch, gh, "591")
    assert gh.issues_fetched == []
    assert "octo/other#5" in out
