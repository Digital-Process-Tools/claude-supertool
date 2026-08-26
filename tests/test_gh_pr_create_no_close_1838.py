"""gh-pr-create refuses a body with no working closing reference unless the
payload deliberately says the PR closes nothing (#1838).

The escape token is the whole design: a `Part of #N` pull request is a real,
recurring case this repo's own merge gates protect by name, so the refusal
must stay openable rather than becoming a gate every payload template
disables.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

MOD_PATH = Path(__file__).parent.parent / "presets" / "github" / "pr_create.py"
_spec = importlib.util.spec_from_file_location("github_pr_create_no_close_1838", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

REPO = "Digital-Process-Tools/claude-supertool"
URL = f"https://github.com/{REPO}/pull/1838"


class _Harness:
    def __init__(self):
        self.create_calls: list = []

    def gh(self, args, timeout=30):
        if args[:2] == ["pr", "create"]:
            self.create_calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, URL, "")
        raise AssertionError(f"unexpected gh call: {args}")

    def gh_json(self, args, timeout=30):
        if args[:2] == ["pr", "view"]:
            return ({"statusCheckRollup": [], "headRefOid": "a" * 40,
                     "createdAt": "2999-01-01T00:00:00Z"}, "")
        raise AssertionError(f"unexpected gh_json call: {args}")


def _payload(tmp_path: Path, data: dict) -> str:
    p = tmp_path / "pr.json"
    p.write_text(json.dumps(data))
    return str(p)


def _install(monkeypatch, h: _Harness, arg: str):
    monkeypatch.setattr(m, "_gh", h.gh)
    monkeypatch.setattr(m, "_gh_json", h.gh_json)
    monkeypatch.setattr(m, "_current_branch", lambda: ("fix/1838", ""))
    monkeypatch.setattr(sys, "argv", ["pr_create.py", arg])


BASE = {"repo": REPO, "title": "a change", "base": "master"}


# ===========================================================================
# must fire: no closing reference, no escape -- refused
# ===========================================================================

def test_body_with_no_closing_reference_is_refused_before_creating(
        monkeypatch, capsys, tmp_path):
    h = _Harness()
    _install(monkeypatch, h, _payload(
        tmp_path, {**BASE, "body": "just some changes, no keyword here"}))
    assert m.main() == 1
    out = capsys.readouterr().out
    assert h.create_calls == [], "a PR was opened with no closing reference"
    assert "no_close" in out


def test_a_keyword_inside_a_code_span_binds_nothing_and_is_refused(
        monkeypatch, capsys, tmp_path):
    """The adjacent trap the issue names: a keyword in a code span reads like
    a reference but GitHub does not honour it, and `closing_issue_refs`
    already strips code spans before matching -- so this must refuse too."""
    h = _Harness()
    _install(monkeypatch, h, _payload(
        tmp_path, {**BASE, "body": "See `Closes #1838` for context"}))
    assert m.main() == 1
    assert h.create_calls == []


# ===========================================================================
# must not fire: a real closing reference publishes normally
# ===========================================================================

def test_body_with_a_working_closing_reference_is_not_refused(
        monkeypatch, capsys, tmp_path):
    h = _Harness()
    _install(monkeypatch, h, _payload(
        tmp_path, {**BASE, "body": "Closes #1838"}))
    assert m.main() == 0
    assert len(h.create_calls) == 1


# ===========================================================================
# the escape token: a deliberate no-close publishes and the receipt names it
# ===========================================================================

def test_no_close_escape_hatch_publishes_a_part_of_pr(monkeypatch, capsys,
                                                       tmp_path):
    h = _Harness()
    _install(monkeypatch, h, _payload(
        tmp_path, {**BASE, "body": "Part of #1838", "no_close": True}))
    assert m.main() == 0
    out = capsys.readouterr().out
    assert len(h.create_calls) == 1
    # The receipt makes the deliberate case legible rather than routine --
    # a payload template carrying `no_close = true` unconditionally must be
    # visibly wrong to anyone reading a receipt.
    assert "no_close" in out
