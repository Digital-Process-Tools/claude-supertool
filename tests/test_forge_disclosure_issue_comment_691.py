"""#691 T4: `gh-issue-comment` was missing the #2100 authorship-disclosure
marker that its siblings (`gh-pr-create`, `gh-pr-edit`, `gh-issue-create`)
already carry -- the exact "correct in one module, absent from its siblings"
shape the 2026-07-31 security review names for this theme. Mirrors
`tests/test_forge_disclosure_wiring_2100.py`'s pattern for the three ops it
already covers.
"""
from __future__ import annotations

import importlib.util
import json
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


issue_comment = _load("gh_issue_comment_691", "presets/github/issue_comment.py")

REPO = "Digital-Process-Tools/claude-supertool"


@pytest.fixture
def real_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPERTOOL_NO_PUBLISH_DISCLOSURE", raising=False)
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


def test_comment_body_carries_the_marker_by_default(
    real_defaults, monkeypatch, capsys, tmp_path,
):
    sent: dict = {}

    def fake_gh_json(args, stdin=None, timeout=30):
        sent["body"] = json.loads(stdin)["body"]
        return ({"body": sent["body"], "id": 1, "html_url": "u"}, "")

    monkeypatch.setattr(issue_comment, "_gh_json", fake_gh_json)
    monkeypatch.setattr(sys, "argv", ["issue_comment.py", "42",
        _payload(tmp_path, {"repo": REPO, "body": "hello there"})])

    assert issue_comment.main() == 0
    assert "hello there" in sent["body"]
    assert sent["body"] != "hello there"
    out = capsys.readouterr().out
    assert "disclosure: appended" in out


def test_disclosure_can_be_suppressed(
    real_defaults, monkeypatch, capsys, tmp_path,
):
    monkeypatch.setenv("SUPERTOOL_NO_PUBLISH_DISCLOSURE", "1")
    sent: dict = {}

    def fake_gh_json(args, stdin=None, timeout=30):
        sent["body"] = json.loads(stdin)["body"]
        return ({"body": sent["body"], "id": 1, "html_url": "u"}, "")

    monkeypatch.setattr(issue_comment, "_gh_json", fake_gh_json)
    monkeypatch.setattr(sys, "argv", ["issue_comment.py", "42",
        _payload(tmp_path, {"repo": REPO, "body": "hello there"})])

    assert issue_comment.main() == 0
    assert sent["body"] == "hello there"
    out = capsys.readouterr().out
    assert "disclosure: suppressed" in out
