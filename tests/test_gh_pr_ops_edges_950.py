"""The error paths of gh-pr-create / gh-pr-merge (#950).

Every branch here is one where something outside the process failed — gh is
missing, a payload is unreadable, the API answered with a shape nobody expected.
They are the branches that only ever run on a bad day, which is exactly why an
untested one stays broken until that day.

Sibling to `test_gh_pr_{create,merge}_main_950.py`, which pin the refusals; this
file pins what happens when the tool itself cannot answer.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent / "presets" / "github"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"edges_{name}", _ROOT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


create = _load("pr_create")
merge = _load("pr_merge")

REPO = "Digital-Process-Tools/claude-supertool"


# ===========================================================================
# the gh wrapper builds the argv it claims to
# ===========================================================================

@pytest.mark.parametrize("mod", [create, merge], ids=["create", "merge"])
def test_gh_prefixes_the_binary_and_forwards_the_args(monkeypatch, mod):
    seen = {}

    def fake(argv, **kw):
        seen["argv"] = argv
        seen["timeout"] = kw.get("timeout")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(mod.subprocess, "run", fake)
    mod._gh(["pr", "view", "1"], timeout=11)
    assert seen["argv"] == ["gh", "pr", "view", "1"]
    assert seen["timeout"] == 11


@pytest.mark.parametrize("mod", [create, merge], ids=["create", "merge"])
def test_gh_json_reports_an_os_error_rather_than_raising(monkeypatch, mod):
    def boom(*a, **kw):
        raise OSError("no fork for you")

    monkeypatch.setattr(mod, "_gh", boom)
    data, err = mod._gh_json(["repo", "view"])
    assert data is None
    assert "could not be run" in err and "no fork for you" in err


@pytest.mark.parametrize("mod", [create, merge], ids=["create", "merge"])
def test_a_silent_nonzero_exit_still_produces_a_reason(monkeypatch, mod):
    """gh failing with no output at all must not render as an empty error."""
    monkeypatch.setattr(mod, "_gh", lambda a, timeout=30:
                        subprocess.CompletedProcess(a, 7, "", ""))
    data, err = mod._gh_json(["repo", "view"])
    assert data is None
    assert "7" in err


# ===========================================================================
# pr_create edges
# ===========================================================================

def test_current_branch_returns_the_name_on_the_happy_path(monkeypatch):
    monkeypatch.setattr(create.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, "fix/950\n", ""))
    name, err = create._current_branch()
    assert (name, err) == ("fix/950", "")


def test_a_toml_payload_without_a_toml_parser_says_what_to_install(monkeypatch,
                                                                   tmp_path):
    p = tmp_path / "pr.toml"
    p.write_text('title = "t"\n')
    monkeypatch.setattr(create, "tomllib", None)
    with pytest.raises(ValueError, match="tomli"):
        create._load_payload(str(p))


def test_a_payload_that_becomes_a_directory_mid_read_is_refused(monkeypatch,
                                                                capsys,
                                                                tmp_path):
    """The TOCTOU window between the is_dir() check and the read."""
    p = tmp_path / "pr.json"
    p.write_text("{}")

    def boom(path):
        raise IsADirectoryError()

    monkeypatch.setattr(create, "_load_payload", boom)
    monkeypatch.setattr(sys, "argv", ["pr_create.py", str(p)])
    assert create.main() == 1
    assert "is a directory" in capsys.readouterr().out


def test_an_unreadable_payload_is_a_permission_error_not_a_directory(
        monkeypatch, capsys, tmp_path):
    """Deliberately a different sentence from 'is a directory'.

    A locked or wrong-ownership file raises PermissionError too, and reporting
    that as 'is a directory' is a confidently wrong disclosure rather than
    merely an unhelpful one.
    """
    p = tmp_path / "pr.json"
    p.write_text("{}")

    def boom(path):
        raise PermissionError("locked")

    monkeypatch.setattr(create, "_load_payload", boom)
    monkeypatch.setattr(sys, "argv", ["pr_create.py", str(p)])
    assert create.main() == 1
    out = capsys.readouterr().out
    assert "permission denied reading payload" in out
    assert "is a directory" not in out


def test_an_unreadable_body_file_is_refused_before_creating(monkeypatch,
                                                            capsys, tmp_path):
    body = tmp_path / "body.md"
    body.write_text("hi")
    payload = tmp_path / "pr.json"
    payload.write_text(json.dumps({
        "repo": REPO, "title": "t", "base": "master",
        "body_file": str(body)}))

    real = Path.read_text

    def boom(self, *a, **kw):
        if self == body:
            raise PermissionError("locked")
        return real(self, *a, **kw)

    calls: list = []
    monkeypatch.setattr(Path, "read_text", boom)
    monkeypatch.setattr(create, "_current_branch", lambda: ("fix/950", ""))
    monkeypatch.setattr(create, "_gh", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(sys, "argv", ["pr_create.py", str(payload)])
    assert create.main() == 1
    assert calls == [], "a PR was created from a body nobody could read"
    assert "permission denied reading body_file" in capsys.readouterr().out


@pytest.mark.parametrize("exc,needle", [
    (FileNotFoundError(), "gh not found"),
    (subprocess.TimeoutExpired("gh", 60), "gh timed out"),
])
def test_gh_never_running_at_all_is_reported(monkeypatch, capsys, tmp_path,
                                             exc, needle):
    payload = tmp_path / "pr.json"
    payload.write_text(json.dumps({"repo": REPO, "title": "t",
                                   "base": "master", "body": "Closes #950"}))

    def boom(*a, **kw):
        raise exc

    monkeypatch.setattr(create, "_current_branch", lambda: ("fix/950", ""))
    monkeypatch.setattr(create, "_gh", boom)
    monkeypatch.setattr(sys, "argv", ["pr_create.py", str(payload)])
    assert create.main() == 1
    assert needle in capsys.readouterr().out


# ===========================================================================
# pr_merge edges
# ===========================================================================

def test_the_pr_module_seam_really_loads_gh_prs_own_helpers():
    """The gate and the `gh-pr` dashboard must derive the tally identically.

    Loading by path is easy to break silently — a rename in `pr.py`, a package
    move — and the failure would be a gate falling back to a different number
    from the one the dashboard prints.
    """
    mod = merge._load_pr_module()
    assert callable(mod._declared_for_commit)
    assert callable(mod._actions_leg_names)


def test_a_long_pending_list_is_capped_with_a_disclosure():
    rollup = [{"name": f"leg-{i}", "status": "QUEUED", "detailsUrl": ""}
              for i in range(9)]
    pr = {"number": 1, "state": "OPEN", "isDraft": False,
          "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
          "reviewDecision": "", "headRefOid": "a" * 40, "headRefName": "b",
          "baseRefName": "master", "body": "", "statusCheckRollup": rollup}
    allowed, lines = merge.gate(pr, declared=9)
    body = "\n".join(lines)
    assert allowed is False
    assert f"+{9 - merge._checks.NAMED_CAP} more" in body, body
    # The cap is a disclosure, not a silent truncation.
    assert "leg-0" in body


def test_bound_refs_declines_when_nodes_is_not_a_list(monkeypatch):
    monkeypatch.setattr(merge, "_gh_json", lambda a, timeout=30: (
        {"data": {"repository": {"pullRequest": {
            "closingIssuesReferences": {"nodes": "nope"}}}}}, ""))
    assert merge._bound_refs("1", REPO) is None


def test_bound_refs_skips_a_malformed_node_rather_than_crashing(monkeypatch):
    monkeypatch.setattr(merge, "_gh_json", lambda a, timeout=30: (
        {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {
            "nodes": ["not a dict",
                      {"repository": {"nameWithOwner": REPO}},
                      {"number": 3, "repository": {"nameWithOwner": REPO}}]}}}}},
        ""))
    # The node with no number names no issue; dropping it is right. Inventing
    # one, or dying on it, are the two wrong answers.
    assert merge._bound_refs("1", REPO) == ["#3"]


def test_a_bare_force_token_is_accepted(monkeypatch, capsys):
    """`gh-pr-merge:944:force` as well as `gh-pr-merge:944|force`."""
    monkeypatch.setattr(merge, "_repo_identity", lambda: (REPO, "master", ""))
    monkeypatch.setattr(merge, "_gh_json", lambda a, timeout=30: (None, "nope"))
    monkeypatch.setattr(sys, "argv", ["pr_merge.py", "944", "force"])
    # It gets past argument parsing (an unrecognised token would have exited
    # naming itself) and dies on the PR read instead.
    assert merge.main() == 1
    out = capsys.readouterr().out
    assert "could not be read" in out
    assert "unrecognised token" not in out


def test_an_unresolvable_repo_is_named_rather_than_assumed(monkeypatch, capsys):
    """`no auth` is gh *not answering*, so the message must not claim the cwd
    is not a GitHub repo (#1789). It asserted exactly that until this site
    started handing `ident_err` over — during a GraphQL outage a working clone
    of a real repository was told it was not a GitHub repo, and the sentence's
    third remedy routes this op onto raw `gh pr merge`."""
    monkeypatch.setattr(merge, "_repo_identity", lambda: ("", "", "no auth"))
    monkeypatch.setattr(sys, "argv", ["pr_merge.py", "944"])
    assert merge.main() == 1
    out = capsys.readouterr().out
    assert "cwd is not a GitHub repo" not in out, out
    assert "did not answer" in out and "no auth" in out, out


def test_a_cwd_that_really_is_not_a_repo_is_still_told_so(monkeypatch, capsys):
    """The positive control for the test above. The hedged sentence must not
    become the only one this site can produce."""
    monkeypatch.setattr(merge, "_repo_identity",
                        lambda: ("", "", "fatal: not a git repository"))
    monkeypatch.setattr(sys, "argv", ["pr_merge.py", "944"])
    assert merge.main() == 1
    assert "cwd is not a GitHub repo" in capsys.readouterr().out


def test_gh_pr_merge_never_running_is_unverified_not_merged(monkeypatch,
                                                            capsys):
    """`gh` itself failing to launch must not read as a merge."""
    pr = {"number": 944, "title": "t", "state": "OPEN", "isDraft": False,
          "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
          "reviewDecision": "", "baseRefName": "master",
          "headRefName": "fix/924", "headRefOid": "c" * 40,
          "url": f"https://github.com/{REPO}/pull/944", "body": "",
          "statusCheckRollup": [{"name": "t", "conclusion": "SUCCESS",
                                 "status": "COMPLETED", "detailsUrl": ""}]}

    def gh_json(args, timeout=30):
        if args[:2] == ["pr", "view"] and "statusCheckRollup" in args[-1]:
            return (pr, "")
        if args[:2] == ["pr", "view"]:
            return ({"state": "OPEN", "mergedAt": None, "mergeCommit": None}, "")
        if args[:2] == ["api", "graphql"]:
            return (None, "skip")
        return (None, "unexpected")

    def boom(*a, **kw):
        raise FileNotFoundError("no gh here")

    class _M:
        @staticmethod
        def _declared_for_commit(d):
            # 4 values since #1181 — see test_gh_pr_merge_tally_seam_1181.py.
            return (1, ["t"], [], "")

        @staticmethod
        def _actions_leg_names(rollup):
            return ["t"]

    monkeypatch.setattr(merge, "_repo_identity", lambda: (REPO, "master", ""))
    monkeypatch.setattr(merge, "_gh_json", gh_json)
    monkeypatch.setattr(merge, "_gh", boom)
    monkeypatch.setattr(merge, "_load_pr_module", lambda: _M())
    monkeypatch.setattr(merge.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setenv("SUPERTOOL_NO_PUBLISH_CONFIRM", "1")
    monkeypatch.setattr(sys, "argv", ["pr_merge.py", "944"])

    assert merge.main() == 1
    out = capsys.readouterr().out
    assert "gh pr merge did not complete" in out
    assert "[result] MERGE UNVERIFIED" in out
