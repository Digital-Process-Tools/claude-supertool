"""`gh-pr` writes through the mirror as a side effect of rendering (#2472).

Same contract as `tests/test_gh_issue_mirror_write_through_1955.py`, over
`presets/github/pr.py` and `_mirror.write_pr`/`_mirror.read_pr` instead of
the issue-only pair -- see `presets/_mirror.py`'s module docstring for why
PRs get their own SUBDIR rather than sharing the issue manifest.

TDD per the brief: run RED against a `pr.py` with no mirror wiring, then
GREEN once `main()` calls `_mirror.write_pr`. The bar this file exists to
enforce is the same as #1955's: the mirror must hold what the API actually
returned, UNTRUNCATED, even when the terminal render truncates the body for
display.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "presets"))
import _mirror  # noqa: E402

PRESET_PATH = REPO / "presets" / "github" / "pr.py"
_spec = importlib.util.spec_from_file_location("github_pr_mirror_2472", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)


def _fake_gh_result(stdout: str) -> Any:
    return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=stdout, stderr="")


def _pr_payload(*, number: int = 2472, body: str = "ordinary body") -> str:
    return json.dumps({
        "number": number, "title": "mirror test", "state": "OPEN", "author": {"login": "florian"},
        "headRefName": "fix/2472", "baseRefName": "master", "labels": [], "milestone": None,
        "reviewDecision": None, "reviews": [], "mergeCommit": None, "mergedAt": None,
        "mergeable": "MERGEABLE", "isDraft": False, "url": "https://github.com/o/r/pull/2472",
        "body": body, "comments": [], "additions": 1, "deletions": 1, "changedFiles": 1,
        "statusCheckRollup": [], "assignees": [], "createdAt": "2026-09-10T00:00:00Z",
        "updatedAt": "2026-09-10T00:00:00Z", "headRefOid": "abc123",
    })


def _install(monkeypatch, tmp_path, *, body: str = "ordinary body") -> None:
    def fake_gh(args, timeout=10):
        return _fake_gh_result(_pr_payload(body=body))

    monkeypatch.setattr(pr, "_gh", fake_gh)
    monkeypatch.chdir(tmp_path)


def _configure_mirror(tmp_path) -> Path:
    mirror_dir = tmp_path / ".max" / "gh-mirror"
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"gh_mirror_dir": str(mirror_dir.relative_to(tmp_path))}),
        encoding="utf-8",
    )
    return mirror_dir


def test_a_plain_read_populates_the_pr_mirror(monkeypatch, capsys, tmp_path) -> None:
    mirror_dir = _configure_mirror(tmp_path)
    _install(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["pr.py", "2472"])

    rc = pr.main()

    assert rc == 0
    hit = _mirror.read_pr(str(mirror_dir), "2472")
    assert hit.state == _mirror.CACHED, hit
    stored = json.loads((mirror_dir / "prs" / "2472.json").read_text(encoding="utf-8"))
    assert stored["pr"]["body"] == "ordinary body"


def test_a_truncated_render_still_mirrors_the_full_untruncated_body(monkeypatch, capsys, tmp_path) -> None:
    mirror_dir = _configure_mirror(tmp_path)
    long_body = "x" * (pr.DESCRIPTION_MAX + 500)
    _install(monkeypatch, tmp_path, body=long_body)
    monkeypatch.setattr(sys, "argv", ["pr.py", "2472"])

    rc = pr.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "TRUNCATED" in out or "withheld" in out.lower(), (
        "the render itself is not exercising the cap this test is about", out[:200]
    )
    stored = json.loads((mirror_dir / "prs" / "2472.json").read_text(encoding="utf-8"))
    assert len(stored["pr"]["body"]) == len(long_body), (
        "the mirror stored a truncated body -- it must always be the full API reply"
    )


def test_an_issue_mirror_and_a_pr_mirror_for_the_same_number_do_not_collide(monkeypatch, capsys, tmp_path) -> None:
    """The design constraint named in `_mirror.py`'s docstring: issue #2472
    and PR #2472 must never answer for each other."""
    mirror_dir = _configure_mirror(tmp_path)
    _mirror.write_issue(str(mirror_dir), "2472", {"number": 2472, "body": "issue body"})
    _install(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["pr.py", "2472"])

    rc = pr.main()

    assert rc == 0
    issue_hit = _mirror.read_issue(str(mirror_dir), "2472")
    pr_hit = _mirror.read_pr(str(mirror_dir), "2472")
    assert issue_hit.state == _mirror.CACHED
    assert pr_hit.state == _mirror.CACHED
    issue_stored = json.loads((mirror_dir / "issues" / "2472.json").read_text(encoding="utf-8"))
    pr_stored = json.loads((mirror_dir / "prs" / "2472.json").read_text(encoding="utf-8"))
    assert issue_stored["issue"]["body"] == "issue body"
    assert pr_stored["pr"]["body"] == "ordinary body"


def test_no_mirror_configured_is_a_silent_no_op_never_an_error(monkeypatch, capsys, tmp_path) -> None:
    _install(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["pr.py", "2472"])

    rc = pr.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "note: gh mirror" not in out, "an unconfigured mirror must not print anything about itself"


def test_a_non_numeric_api_reply_number_is_never_written_to_the_mirror(monkeypatch, capsys, tmp_path) -> None:
    mirror_dir = _configure_mirror(tmp_path)

    def fake_gh(args, timeout=10):
        return _fake_gh_result(_pr_payload_bad_number())

    monkeypatch.setattr(pr, "_gh", fake_gh)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["pr.py", "2472"])

    rc = pr.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "not written" in out
    assert not (mirror_dir / "prs").exists() or list((mirror_dir / "prs").glob("*.json")) == [], (
        "a non-numeric API number must never produce a mirror body file"
    )


def _pr_payload_bad_number() -> str:
    payload = json.loads(_pr_payload())
    payload["number"] = "not-a-number"
    return json.dumps(payload)


def test_a_mirror_write_failure_does_not_take_the_read_down_with_it(monkeypatch, capsys, tmp_path) -> None:
    _configure_mirror(tmp_path)
    _install(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["pr.py", "2472"])
    monkeypatch.setattr(_mirror, "write_pr", lambda *a, **k: "disk is full")
    monkeypatch.setattr(pr, "_mirror", _mirror)

    rc = pr.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "# #2472 mirror test" in out or "mirror test" in out, "the primary render must still succeed"
