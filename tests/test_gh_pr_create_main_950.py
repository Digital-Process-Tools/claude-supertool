"""gh-pr-create end to end: the base refusal actually stops the create (#950).

The pure-function tests pin what `validate()` returns. These pin what `main()`
does with it — that a refused payload never reaches `gh pr create`, which is a
different claim and the one a user's safety rests on.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

MOD_PATH = Path(__file__).parent.parent / "presets" / "github" / "pr_create.py"
_spec = importlib.util.spec_from_file_location("github_pr_create_main", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

REPO = "Digital-Process-Tools/claude-supertool"
URL = f"https://github.com/{REPO}/pull/957"

_DEFAULT = object()


class _Harness:
    """Routes gh, and records whether a PR was actually created.

    `rollup` takes a sentinel rather than `None` because `None` is one of the
    states under test — "the rollup did not come back" — and an `or` default
    would swallow it.
    """

    def __init__(self, *, create_rc: int = 0, create_stdout: str = URL,
                 rollup=_DEFAULT, created_age: str = "",
                 branch: tuple = ("fix/950", ""),
                 fail_readback: str = ""):
        self.create_rc = create_rc
        self.create_stdout = create_stdout
        self.rollup = [] if rollup is _DEFAULT else rollup
        self.created_age = created_age or "2999-01-01T00:00:00Z"
        self.branch = branch
        self.fail_readback = fail_readback
        self.create_calls: list = []

    def gh(self, args, timeout=30):
        if args[:2] == ["pr", "create"]:
            self.create_calls.append(list(args))
            return subprocess.CompletedProcess(
                args, self.create_rc, self.create_stdout,
                "" if self.create_rc == 0 else "a pull request already exists")
        raise AssertionError(f"unexpected gh call: {args}")

    def gh_json(self, args, timeout=30):
        if args[:2] == ["repo", "view"]:
            return ({"defaultBranchRef": {"name": "master"}}, "")
        if args[:2] == ["pr", "view"]:
            if self.fail_readback:
                return (None, self.fail_readback)
            return ({"statusCheckRollup": self.rollup,
                     "headRefOid": "a" * 40,
                     "createdAt": self.created_age}, "")
        raise AssertionError(f"unexpected gh_json call: {args}")


def _payload(tmp_path: Path, data: dict) -> str:
    p = tmp_path / "pr.json"
    p.write_text(json.dumps(data))
    return str(p)


def _install(monkeypatch, h: _Harness, arg: str):
    monkeypatch.setattr(m, "_gh", h.gh)
    monkeypatch.setattr(m, "_gh_json", h.gh_json)
    monkeypatch.setattr(m, "_current_branch", lambda: h.branch)
    monkeypatch.setattr(sys, "argv", ["pr_create.py", arg])


FULL = {"repo": REPO, "title": "a change", "base": "master",
        "body": "Closes #950"}


# ===========================================================================
# refusals never reach gh pr create
# ===========================================================================

def test_a_missing_base_never_reaches_gh(monkeypatch, capsys, tmp_path):
    h = _Harness()
    _install(monkeypatch, h,
             _payload(tmp_path, {"repo": REPO, "title": "t", "body": "b"}))
    assert m.main() == 1
    out = capsys.readouterr().out
    assert h.create_calls == [], "a PR was created without a base"
    assert "never guessed" in out
    # The default is named, and deliberately not used.
    assert "default branch is 'master'" in out


def test_a_missing_title_never_reaches_gh(monkeypatch, capsys, tmp_path):
    h = _Harness()
    _install(monkeypatch, h,
             _payload(tmp_path, {"repo": REPO, "base": "master", "body": "b"}))
    assert m.main() == 1
    assert h.create_calls == []


def test_base_equal_to_head_never_reaches_gh(monkeypatch, capsys, tmp_path):
    h = _Harness()
    _install(monkeypatch, h, _payload(
        tmp_path, {"repo": REPO, "title": "t", "base": "master",
                   "head": "master", "body": "b"}))
    assert m.main() == 1
    assert h.create_calls == []
    assert "cannot be merged into itself" in capsys.readouterr().out


def test_a_detached_head_never_reaches_gh(monkeypatch, capsys, tmp_path):
    h = _Harness(branch=("", "detached HEAD"))
    _install(monkeypatch, h, _payload(tmp_path, FULL))
    assert m.main() == 1
    assert h.create_calls == []
    assert "detached HEAD" in capsys.readouterr().out


def test_no_payload_is_usage(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pr_create.py", ""])
    assert m.main() == 1
    assert "needs a payload" in capsys.readouterr().out


def test_a_missing_payload_file_is_named(monkeypatch, capsys, tmp_path):
    h = _Harness()
    _install(monkeypatch, h, str(tmp_path / "nope.toml"))
    assert m.main() == 1
    assert "payload file not found" in capsys.readouterr().out


def test_a_directory_payload_is_refused(monkeypatch, capsys, tmp_path):
    h = _Harness()
    _install(monkeypatch, h, str(tmp_path))
    assert m.main() == 1
    assert "is a directory" in capsys.readouterr().out


def test_an_unparseable_payload_is_refused(monkeypatch, capsys, tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json at all")
    h = _Harness()
    _install(monkeypatch, h, str(p))
    assert m.main() == 1
    assert "failed to parse payload" in capsys.readouterr().out
    assert h.create_calls == []


def test_a_missing_body_file_is_refused_before_creating(monkeypatch, capsys,
                                                        tmp_path):
    h = _Harness()
    _install(monkeypatch, h, _payload(tmp_path, {
        "repo": REPO, "title": "t", "base": "master",
        "body_file": str(tmp_path / "nope.md")}))
    assert m.main() == 1
    assert h.create_calls == []
    assert "body_file not found" in capsys.readouterr().out


def test_a_directory_body_file_is_refused(monkeypatch, capsys, tmp_path):
    h = _Harness()
    _install(monkeypatch, h, _payload(tmp_path, {
        "repo": REPO, "title": "t", "base": "master",
        "body_file": str(tmp_path)}))
    assert m.main() == 1
    assert h.create_calls == []
    assert "body_file is a directory, not a file" in capsys.readouterr().out


def test_gh_failing_is_surfaced_and_says_no_pr_was_created(monkeypatch, capsys,
                                                           tmp_path):
    h = _Harness(create_rc = 1)
    _install(monkeypatch, h, _payload(tmp_path, FULL))
    assert m.main() == 1
    out = capsys.readouterr().out
    assert "already exists" in out
    assert "[result] no PR created" in out


# ===========================================================================
# the happy path and its receipt
# ===========================================================================

def test_base_and_head_are_echoed_with_their_source(monkeypatch, capsys,
                                                    tmp_path):
    h = _Harness()
    _install(monkeypatch, h, _payload(tmp_path, FULL))
    assert m.main() == 0
    out = capsys.readouterr().out
    assert "Base: master  (from payload — never defaulted)" in out
    assert "Head: fix/950  (from current branch)" in out
    args = h.create_calls[0]
    assert args[args.index("--base") + 1] == "master"
    assert args[args.index("--head") + 1] == "fix/950"


def test_an_explicit_head_is_labelled_as_coming_from_the_payload(monkeypatch,
                                                                 capsys,
                                                                 tmp_path):
    h = _Harness()
    _install(monkeypatch, h,
             _payload(tmp_path, dict(FULL, head="feat/other")))
    assert m.main() == 0
    assert "(from payload)" in capsys.readouterr().out


def test_optional_fields_reach_gh(monkeypatch, capsys, tmp_path):
    h = _Harness()
    _install(monkeypatch, h, _payload(tmp_path, dict(
        FULL, draft=True, labels=["bug", "lane:x"], assignees=["me"],
        reviewers=["you"], milestone="0.13.0")))
    assert m.main() == 0
    args = h.create_calls[0]
    assert "--draft" in args
    assert args[args.index("--label") + 1] == "bug,lane:x"
    assert args[args.index("--assignee") + 1] == "me"
    assert args[args.index("--reviewer") + 1] == "you"
    assert args[args.index("--milestone") + 1] == "0.13.0"


def test_a_body_file_is_read_and_its_closing_ref_parsed(monkeypatch, capsys,
                                                        tmp_path):
    body = tmp_path / "body.md"
    body.write_text("Some prose.\n\nCloses #950\n")
    h = _Harness()
    _install(monkeypatch, h, _payload(tmp_path, {
        "repo": REPO, "title": "t", "base": "master",
        "body_file": str(body)}))
    assert m.main() == 0
    out = capsys.readouterr().out
    assert "Issue: #950" in out


def test_zero_checks_is_rendered_as_nothing_created(monkeypatch, capsys,
                                                    tmp_path):
    h = _Harness(rollup=[])
    _install(monkeypatch, h, _payload(tmp_path, FULL))
    assert m.main() == 0
    out = capsys.readouterr().out
    assert "zero check runs" in out
    assert "nothing has been created" in out.lower()
    assert "[result] PR #957 opened; no checks created yet" in out


def test_checks_that_exist_render_the_shared_tally(monkeypatch, capsys,
                                                   tmp_path):
    h = _Harness(rollup=[{"name": "tests", "conclusion": "SUCCESS"},
                         {"name": "e2e", "conclusion": "CANCELLED"}])
    _install(monkeypatch, h, _payload(tmp_path, FULL))
    assert m.main() == 0
    out = capsys.readouterr().out
    assert "2 total: 1 passed, 0 failed, 0 pending, 1 cancelled" in out
    assert "e2e" in out


def test_an_unreadable_rollup_is_unknown_not_zero(monkeypatch, capsys,
                                                  tmp_path):
    h = _Harness(fail_readback="gh timed out")
    _install(monkeypatch, h, _payload(tmp_path, FULL))
    assert m.main() == 0
    out = capsys.readouterr().out
    assert "gh timed out" in out
    assert "check state unknown" in out
    assert "zero check runs" not in out


def test_a_body_with_no_closing_keyword_is_refused_before_creating(
        monkeypatch, capsys, tmp_path):
    """Superseded by #1838: a body with no closing keyword used to publish
    with a loud note (see tests/test_gh_pr_create_no_close_1838.py for the
    refusal and its `no_close` escape hatch); it no longer reaches `gh pr
    create` at all without that acknowledgment."""
    h = _Harness()
    _install(monkeypatch, h, _payload(tmp_path, dict(FULL, body="just prose")))
    assert m.main() == 1
    out = capsys.readouterr().out
    assert h.create_calls == []
    assert "no working closing reference" in out


def test_a_url_gh_did_not_return_is_not_invented(monkeypatch, capsys, tmp_path):
    h = _Harness(create_stdout="")
    _install(monkeypatch, h, _payload(tmp_path, FULL))
    assert m.main() == 0
    out = capsys.readouterr().out
    assert "(not returned by gh)" in out
    assert "gh returned no PR number to read back" in out


def test_the_next_commands_name_the_merge_op(monkeypatch, capsys, tmp_path):
    h = _Harness()
    _install(monkeypatch, h, _payload(tmp_path, FULL))
    m.main()
    out = capsys.readouterr().out
    assert "gh-pr-merge:957" in out
    assert "|force" in out


def test_a_toml_payload_is_accepted(monkeypatch, capsys, tmp_path):
    p = tmp_path / "pr.toml"
    p.write_text(
        f'repo = "{REPO}"\ntitle = "t"\nbase = "master"\n'
        "body = '''Closes #950\n\nA body: with a colon.'''\n")
    h = _Harness()
    _install(monkeypatch, h, "@" + str(p))
    assert m.main() == 0
    assert "Issue: #950" in capsys.readouterr().out


def test_repo_defaults_from_the_remote_when_absent(monkeypatch, capsys,
                                                   tmp_path):
    h = _Harness()
    _install(monkeypatch, h, _payload(
        tmp_path, {"title": "t", "base": "master", "body": "Closes #950"}))
    monkeypatch.setattr(m._rd, "resolve", lambda *a: REPO)
    assert m.main() == 0
    args = h.create_calls[0]
    assert args[args.index("--repo") + 1] == REPO


# ===========================================================================
# plumbing
# ===========================================================================

def test_gh_json_reports_a_missing_binary(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError()
    monkeypatch.setattr(m, "_gh", boom)
    data, err = m._gh_json(["repo", "view"])
    assert data is None and "gh not found" in err


def test_gh_json_reports_a_timeout(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired("gh", 30)
    monkeypatch.setattr(m, "_gh", boom)
    data, err = m._gh_json(["repo", "view"])
    assert data is None and "timed out" in err


def test_gh_json_reports_invalid_json(monkeypatch):
    monkeypatch.setattr(m, "_gh", lambda a, timeout=30:
                        subprocess.CompletedProcess(a, 0, "nope", ""))
    data, err = m._gh_json(["repo", "view"])
    assert data is None and "invalid JSON" in err


def test_current_branch_reports_a_detached_head(monkeypatch):
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, "HEAD\n", ""))
    name, err = m._current_branch()
    assert name == "" and err == "detached HEAD"


def test_current_branch_reports_a_git_failure(monkeypatch):
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 128, "", "not a repo\n"))
    name, err = m._current_branch()
    assert name == "" and "not a repo" in err


def test_current_branch_reports_a_missing_git(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError()
    monkeypatch.setattr(m.subprocess, "run", boom)
    name, err = m._current_branch()
    assert name == "" and "git did not answer" in err


def test_an_unparseable_timestamp_is_not_read_as_fresh():
    assert m._age_secs("not a date") is None
    assert m._age_secs("") is None
