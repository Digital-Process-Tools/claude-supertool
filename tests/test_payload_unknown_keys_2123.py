"""An unknown payload key must be refused before anything is created, and a
few natural-language aliases (`body` <-> `description`) must be accepted
(#2123).

`gl-issue-create` and `gh-issue-create` opened opposite issues empty, minutes
apart, because each op silently dropped a key the *other* op recognises:
`body` on `gl-issue-create` (wants `description`), then `description` on
`gh-issue-create` (wants `body`). Both returned PASS with a number and a URL.

The must-fire / must-not-fire pair this test file exists for:

* must fire -- a payload carrying a key none of these ops consume is refused,
  before any `gh`/`glab` call, naming the key and the accepted set.
* must fire -- `body` reaches the description on `gl-issue-create`, and
  `description` reaches the body on `gh-issue-create` (the alias, both
  directions).
* must not fire -- every payload shape valid today still validates, including
  the real payload shapes `gh-pr-create` and `gh-pr-edit` take when this
  maintainer loop opens or corrects its own pull requests.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load(rel: str, name: str):
    mod_path = Path(__file__).parent.parent / rel
    spec = importlib.util.spec_from_file_location(name, mod_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gh_issue = _load("presets/github/issue_create.py", "gh_issue_create_2123")
gl_issue = _load("presets/gitlab/issue_create.py", "gl_issue_create_2123")
gh_pr_create = _load("presets/github/pr_create.py", "gh_pr_create_2123")
gh_pr_edit = _load("presets/github/pr_edit.py", "gh_pr_edit_2123")
payload_keys = _load("presets/_payload_keys.py", "payload_keys_2123")


# ===========================================================================
# must fire -- an unknown key is refused, naming the key and the accepted set
# ===========================================================================

def test_gh_issue_create_refuses_an_unknown_key():
    err = payload_keys.check(
        {"repo": "o/r", "title": "t", "body": "b", "wat": "?"},
        gh_issue.ACCEPTED_KEYS, gh_issue.ALIASES, "gh-issue-create")
    assert err is not None
    assert "wat" in err
    assert "gh-issue-create" in err
    for key in sorted(gh_issue.ACCEPTED_KEYS):
        assert key in err


def test_gl_issue_create_refuses_an_unknown_key():
    err = payload_keys.check(
        {"project": "o/r", "title": "t", "description": "b", "wat": "?"},
        gl_issue.ACCEPTED_KEYS, gl_issue.ALIASES, "gl-issue-create")
    assert err is not None
    assert "wat" in err
    assert "gl-issue-create" in err


def test_gh_pr_create_refuses_an_unknown_key():
    err = payload_keys.check(
        {"repo": "o/r", "title": "t", "base": "master", "body": "b",
         "wat": "?"},
        gh_pr_create.ACCEPTED_KEYS, gh_pr_create.ALIASES, "gh-pr-create")
    assert err is not None
    assert "wat" in err


def test_gh_pr_edit_refuses_an_unknown_key():
    err = payload_keys.check(
        {"repo": "o/r", "body": "b", "wat": "?"},
        gh_pr_edit.ACCEPTED_KEYS, gh_pr_edit.ALIASES, "gh-pr-edit")
    assert err is not None
    assert "wat" in err


def test_refusal_names_the_accepted_set_not_just_the_bad_key():
    err = payload_keys.check(
        {"repo": "o/r", "title": "t", "body": "b", "wat": "?"},
        gh_issue.ACCEPTED_KEYS, gh_issue.ALIASES, "gh-issue-create")
    assert "milestone" in err  # a real accepted key, not just "wat"


def test_end_to_end_main_refuses_before_any_gh_call(monkeypatch, tmp_path):
    """The full main() path -- a bad key never reaches `gh`."""
    import json
    called = []
    monkeypatch.setattr(gh_issue, "_gh", lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
        AssertionError("gh must not be called")))
    payload = tmp_path / "p.json"
    payload.write_text(json.dumps(
        {"repo": "o/r", "title": "t", "body": "b", "wat": "?"}))
    rc = gh_issue.main.__wrapped__(["@" + str(payload)]) if hasattr(
        gh_issue.main, "__wrapped__") else None
    # main() reads sys.argv directly -- exercise it that way instead.
    import sys as _sys
    old_argv = _sys.argv
    try:
        _sys.argv = ["gh-issue-create", "@" + str(payload)]
        rc = gh_issue.main()
    finally:
        _sys.argv = old_argv
    assert rc == 1
    assert called == []


# ===========================================================================
# must fire -- the body/description alias, both directions
# ===========================================================================

def test_gl_issue_create_accepts_body_as_description_alias():
    payload = {"project": "o/r", "title": "t", "body": "hello"}
    resolved, err = payload_keys.resolve_aliases(payload, gl_issue.ALIASES)
    assert err is None
    assert resolved.get("description") == "hello"
    assert "body" not in resolved


def test_gl_issue_create_accepts_body_file_as_description_file_alias():
    payload = {"project": "o/r", "title": "t", "body_file": "/tmp/x.md"}
    resolved, err = payload_keys.resolve_aliases(payload, gl_issue.ALIASES)
    assert err is None
    assert resolved.get("description_file") == "/tmp/x.md"


def test_gh_issue_create_accepts_description_as_body_alias():
    payload = {"repo": "o/r", "title": "t", "description": "hello"}
    resolved, err = payload_keys.resolve_aliases(payload, gh_issue.ALIASES)
    assert err is None
    assert resolved.get("body") == "hello"
    assert "description" not in resolved


def test_conflicting_alias_and_canonical_with_different_values_is_refused():
    payload = {"project": "o/r", "title": "t", "description": "A", "body": "B"}
    resolved, err = payload_keys.resolve_aliases(payload, gl_issue.ALIASES)
    assert err is not None
    assert "description" in err and "body" in err


def test_alias_and_canonical_agreeing_is_not_a_conflict():
    payload = {"project": "o/r", "title": "t", "description": "same", "body": "same"}
    resolved, err = payload_keys.resolve_aliases(payload, gl_issue.ALIASES)
    assert err is None
    assert resolved.get("description") == "same"


# ===========================================================================
# must not fire -- every payload shape valid today still validates
# ===========================================================================

GH_ISSUE_FULL = {
    "repo": "o/r", "title": "t", "body": "b", "labels": ["a"],
    "assignees": ["x"], "milestone": "1.0",
}

GL_ISSUE_FULL = {
    "project": "fdavid/dvsi", "title": "Full issue",
    "description": "Full description", "milestone_id": 171,
    "labels": ["AGY_OMS", "Todo"], "assignee_ids": [2, 5],
    "estimate": "4h", "links": [{"target_iid": 12240, "type": "relates_to"}],
}

GH_PR_CREATE_FULL = {
    "repo": "o/r", "title": "t", "base": "master", "head": "feature",
    "body": "Closes #1", "draft": True, "labels": ["a"],
    "assignees": ["x"], "reviewers": ["y"], "milestone": "1.0",
    "literal_backslashes": True, "no_close": False,
}

# This maintainer loop's own gh-pr-edit shape -- a create payload reused
# verbatim to correct a published pull request. base/head/draft/labels/
# assignees/reviewers/milestone are all present but NOT_APPLIED by
# `gh-pr-edit`; they must stay accepted rather than start being refused.
GH_PR_EDIT_REUSED_CREATE_PAYLOAD = dict(GH_PR_CREATE_FULL)


def test_gh_issue_create_full_payload_has_no_unknown_keys():
    err = payload_keys.check(GH_ISSUE_FULL, gh_issue.ACCEPTED_KEYS,
                             gh_issue.ALIASES, "gh-issue-create")
    assert err is None


def test_gl_issue_create_full_payload_has_no_unknown_keys():
    err = payload_keys.check(GL_ISSUE_FULL, gl_issue.ACCEPTED_KEYS,
                             gl_issue.ALIASES, "gl-issue-create")
    assert err is None


def test_gh_pr_create_full_payload_has_no_unknown_keys():
    err = payload_keys.check(GH_PR_CREATE_FULL, gh_pr_create.ACCEPTED_KEYS,
                             gh_pr_create.ALIASES, "gh-pr-create")
    assert err is None


def test_gh_pr_edit_accepts_a_reused_create_payload():
    err = payload_keys.check(GH_PR_EDIT_REUSED_CREATE_PAYLOAD,
                             gh_pr_edit.ACCEPTED_KEYS, gh_pr_edit.ALIASES,
                             "gh-pr-edit")
    assert err is None


def test_gh_pr_create_validate_still_works_after_alias_wiring():
    assert gh_pr_create.validate({"repo": "o/r", "title": "t",
                                  "base": "master", "body": "b"}) is None


def test_gh_pr_edit_validate_still_works_after_alias_wiring():
    assert gh_pr_edit.validate({"repo": "o/r", "body": "b"}) is None
