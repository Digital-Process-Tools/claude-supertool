"""gh-pr-create refuses a body carrying literal backslash-quote sequences,
because the paste/edit route already refuses exactly this shape and gh-pr-create
was the one route that published it silently (#1967).
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

MOD_PATH = Path(__file__).parent.parent / "presets" / "github" / "pr_create.py"
_spec = importlib.util.spec_from_file_location("github_pr_create_bsq_1967", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

REPO = "Digital-Process-Tools/claude-supertool"
URL = f"https://github.com/{REPO}/pull/1967"


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
    monkeypatch.setattr(m, "_current_branch", lambda: ("fix/1967", ""))
    monkeypatch.setattr(sys, "argv", ["pr_create.py", arg])


BASE = {"repo": REPO, "title": "a change", "base": "master"}

# The decoded string this JSON payload carries is:  ev.get(\"repo\") or repo
# -- a literal backslash immediately followed by a quote, twice.
BODY_WITH_LITERAL_BSQ = 'Closes #1967\n\nev.get(\\"repo\\") or repo'


def test_body_with_literal_backslash_quote_is_refused_before_creating(
        monkeypatch, capsys, tmp_path):
    h = _Harness()
    _install(monkeypatch, h, _payload(
        tmp_path, {**BASE, "body": BODY_WITH_LITERAL_BSQ}))
    assert m.main() == 1
    out = capsys.readouterr().out
    assert h.create_calls == [], "a PR was created carrying a literal backslash-quote"
    assert "literal backslash" in out
    assert "2" in out  # two occurrences


def test_body_with_literal_backslash_quote_and_escape_hatch_is_published(
        monkeypatch, capsys, tmp_path):
    h = _Harness()
    _install(monkeypatch, h, _payload(
        tmp_path, {**BASE, "body": BODY_WITH_LITERAL_BSQ,
                   "literal_backslashes": True}))
    assert m.main() == 0
    assert len(h.create_calls) == 1


def test_body_file_with_literal_backslash_quote_is_also_refused(
        monkeypatch, capsys, tmp_path):
    h = _Harness()
    body_file = tmp_path / "body.md"
    body_file.write_text(BODY_WITH_LITERAL_BSQ)
    _install(monkeypatch, h, _payload(
        tmp_path, {**BASE, "body_file": str(body_file)}))
    assert m.main() == 1
    assert h.create_calls == []


def test_a_lone_backslash_with_no_following_quote_is_not_flagged(
        monkeypatch, capsys, tmp_path):
    """Control pair: a Windows path or a regex is a legitimate lone backslash
    and must publish unchanged -- would this test still pass if the code did
    nothing? No: without the fix this body would never have been refused
    anyway, so a red run here would mean the detector over-fires."""
    h = _Harness()
    _install(monkeypatch, h, _payload(
        tmp_path, {**BASE, "body": "Closes #1967\n\nSee C:\\Users\\me\\file.md"}))
    assert m.main() == 0
    assert len(h.create_calls) == 1
