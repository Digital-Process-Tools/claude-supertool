"""Integration half of #2100 -- proves the marker actually reaches the body
each forge-write op sends, not just that `apply_forge_disclosure` works in
isolation (`tests/test_forge_disclosure_2100.py`).

Covers the four ops named in the issue: `gh-pr-create`, `gh-pr-edit`,
`gh-issue-create`, and the one GitLab equivalent that exists today,
`gl-issue-create` (there is no `gl-mr-create`/`gl-mr-edit`/`gl-issue-edit`
in this repo to wire).

Conftest suppresses the marker suite-wide (`SUPERTOOL_NO_PUBLISH_DISCLOSURE=1`);
`real_defaults` undoes that, the same pattern
`tests/test_publish_disclosure_wiring_2042.py` uses for the publish ops.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    path = _ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pr_create = _load("gh_pr_create_2100", "presets/github/pr_create.py")
pr_edit = _load("gh_pr_edit_2100", "presets/github/pr_edit.py")
gh_issue_create = _load("gh_issue_create_2100", "presets/github/issue_create.py")
gl_issue_create = _load("gl_issue_create_2100", "presets/gitlab/issue_create.py")

REPO = "Digital-Process-Tools/claude-supertool"


@pytest.fixture
def real_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPERTOOL_NO_PUBLISH_DISCLOSURE", raising=False)
    monkeypatch.delenv("SUPERTOOL_PUBLISH_BODY_ALLOWLIST", raising=False)
    monkeypatch.chdir(tmp_path)
    import _publish_safety
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")
    yield
    if hasattr(_publish_safety, "_CACHED_CONFIG"):
        delattr(_publish_safety, "_CACHED_CONFIG")


def _payload(tmp_path: Path, data: dict) -> str:
    p = tmp_path / "payload.json"
    p.write_text(json.dumps(data))
    return str(p)


def _flag_value(args: list, flag: str) -> str | None:
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return None


# ===========================================================================
# gh-pr-create -- marker must be in the body handed to `gh pr create`
# ===========================================================================

def test_pr_create_body_carries_the_marker_by_default(
    real_defaults, monkeypatch, capsys, tmp_path,
):
    written_bodies: list[str] = []

    def fake_gh(args, timeout=30):
        bf = _flag_value(args, "--body-file")
        if bf:
            written_bodies.append(Path(bf).read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(
            args, 0, f"https://github.com/{REPO}/pull/2100", "")

    def fake_gh_json(args, timeout=30):
        return ({"statusCheckRollup": [], "headRefOid": "a" * 40,
                 "createdAt": "2999-01-01T00:00:00Z"}, "")

    monkeypatch.setattr(pr_create, "_gh", fake_gh)
    monkeypatch.setattr(pr_create, "_gh_json", fake_gh_json)
    monkeypatch.setattr(pr_create, "_current_branch", lambda: ("fix/2100", ""))
    monkeypatch.setattr(sys, "argv", ["pr_create.py", _payload(tmp_path, {
        "repo": REPO, "title": "t", "base": "master",
        "body": "Closes #2100",
    })])

    assert pr_create.main() == 0
    assert written_bodies
    assert "Closes #2100" in written_bodies[0]
    assert written_bodies[0] != "Closes #2100"
    out = capsys.readouterr().out
    assert "Disclosure: appended" in out


def test_pr_create_disclosure_is_appended_after_the_closing_ref_parse(
    real_defaults, monkeypatch, tmp_path,
):
    """A body with no closing reference is refused before `gh pr create` is
    ever reached -- proving the marker is not what the refusal is reading,
    and that the refusal runs first."""
    monkeypatch.setattr(pr_create, "_current_branch", lambda: ("fix/2100", ""))

    def _must_not_be_called(*a, **kw):
        raise AssertionError("gh was called despite no closing reference")
    monkeypatch.setattr(pr_create, "_gh", _must_not_be_called)

    monkeypatch.setattr(sys, "argv", ["pr_create.py", _payload(tmp_path, {
        "repo": REPO, "title": "t", "base": "master", "body": "no refs here",
    })])
    assert pr_create.main() == 1


# ===========================================================================
# gh-pr-edit -- appended on the first edit, not doubled on the second
# ===========================================================================

def _install_edit_harness(monkeypatch, current_body: str, sent_holder: dict):
    def fake_gh_json(args, timeout=30, stdin=None, **kw):
        if args and args[0] == "api" and "-X" not in args:
            return ({"title": "t", "state": "OPEN", "body": current_body}, "")
        # PATCH call -- echo back whatever was sent, like the real API does.
        sent = json.loads(stdin)
        sent_holder["body"] = sent["body"]
        return ({"body": sent["body"], "title": sent.get("title", "t"),
                 "html_url": "u"}, "")
    monkeypatch.setattr(pr_edit, "_gh_json", fake_gh_json)


def test_pr_edit_first_update_appends_the_marker(
    real_defaults, monkeypatch, capsys, tmp_path,
):
    sent: dict = {}
    _install_edit_harness(monkeypatch, current_body="old body", sent_holder=sent)
    monkeypatch.setattr(sys, "argv", ["pr_edit.py", "1", _payload(tmp_path, {
        "repo": REPO, "body": "Closes #2100",
    })])

    assert pr_edit.main() == 0
    assert "Closes #2100" in sent["body"]
    assert sent["body"] != "Closes #2100"
    out = capsys.readouterr().out
    assert "disclosure: appended" in out


def test_pr_edit_second_update_does_not_double_append(
    real_defaults, monkeypatch, tmp_path,
):
    """The published body already carries the marker -- the ordinary case,
    since an edit routinely starts from what is already published. Sending
    it back unchanged must not stack a second copy."""
    from _publish_safety import _DEFAULT_DISCLOSURE_TEXT
    already_marked = f"Closes #2100\n\n{_DEFAULT_DISCLOSURE_TEXT}"

    sent: dict = {}
    _install_edit_harness(monkeypatch, current_body=already_marked, sent_holder=sent)
    monkeypatch.setattr(sys, "argv", ["pr_edit.py", "1", _payload(tmp_path, {
        "repo": REPO, "body": already_marked,
    })])

    assert pr_edit.main() == 0
    assert sent["body"].count(_DEFAULT_DISCLOSURE_TEXT) == 1


# ===========================================================================
# gh-issue-create
# ===========================================================================

def test_gh_issue_create_body_carries_the_marker_by_default(
    real_defaults, monkeypatch, tmp_path,
):
    written_bodies: list[str] = []

    def fake_gh(args, timeout=20):
        bf = _flag_value(args, "--body-file")
        if bf:
            written_bodies.append(Path(bf).read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(
            args, 0, f"https://github.com/{REPO}/issues/42", "")

    monkeypatch.setattr(gh_issue_create, "_gh", fake_gh)
    monkeypatch.setattr(sys, "argv", ["issue_create.py", _payload(tmp_path, {
        "repo": REPO, "title": "t", "body": "Hello world",
    })])

    assert gh_issue_create.main() == 0
    assert written_bodies
    assert "Hello world" in written_bodies[0]
    assert written_bodies[0] != "Hello world"


# ===========================================================================
# gl-issue-create -- the one GitLab equivalent that exists
# ===========================================================================

def test_gl_issue_create_body_carries_the_marker_by_default(
    real_defaults, monkeypatch, tmp_path,
):
    captured: list = []

    def fake_glab(args, timeout=20):
        captured.append(args)
        return subprocess.CompletedProcess(
            args, 0, "https://gitlab.com/fdavid/dvsi/-/issues/7", "")

    monkeypatch.setattr(gl_issue_create, "_glab", fake_glab)
    monkeypatch.setattr(sys, "argv", ["issue_create.py", _payload(tmp_path, {
        "project": "fdavid/dvsi", "title": "t", "description": "Hello world",
    })])

    assert gl_issue_create.main() == 0
    desc = _flag_value(captured[0], "--description")
    assert desc is not None
    assert "Hello world" in desc
    assert desc != "Hello world"
