"""`gh-issue` writes through the mirror as a side effect of rendering (#1955).

TDD per the brief: run RED against master's `issue.py` (no mirror wiring),
then GREEN once `main()` calls `_mirror.write_issue`. See the developer
report for both runs. Loads the real preset the way
`tests/test_gh_issue_classify_2049.py` already does, and stubs `_gh` the same
way -- nothing here shells out to a real `gh`.

The bar this file exists to enforce: the mirror must hold what the API
actually returned, UNTRUNCATED, even when the terminal render truncates the
body/comments for display -- a mirror of a truncated body would lie by
omission, which is the issue's own point 1 verbatim.
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

PRESET_PATH = REPO / "presets" / "github" / "issue.py"
_spec = importlib.util.spec_from_file_location("github_issue_mirror_1955", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
issue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issue)


def _fake_gh_result(stdout: str) -> Any:
    return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=stdout, stderr="")


def _install(monkeypatch, tmp_path, *, body: str, comments: list | None = None) -> None:
    long_body = body
    issue_payload = json.dumps({
        "number": 1955, "title": "mirror test", "state": "OPEN", "labels": [],
        "milestone": None, "assignees": [], "author": {"login": "florian"},
        "url": "https://github.com/o/r/issues/1955", "body": long_body,
        "comments": comments or [],
    })
    pr_payload = json.dumps([])

    def fake_gh(args, timeout=10):
        if args and args[0] == "pr":
            return _fake_gh_result(pr_payload)
        return _fake_gh_result(issue_payload)

    monkeypatch.setattr(issue, "_gh", fake_gh)
    monkeypatch.setattr(issue, "_download_images", lambda urls, n: [])
    monkeypatch.chdir(tmp_path)


def _configure_mirror(tmp_path) -> Path:
    mirror_dir = tmp_path / ".max" / "gh-mirror"
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"gh_mirror_dir": str(mirror_dir.relative_to(tmp_path))}),
        encoding="utf-8",
    )
    return mirror_dir


def test_a_plain_read_populates_the_mirror(monkeypatch, capsys, tmp_path) -> None:
    mirror_dir = _configure_mirror(tmp_path)
    _install(monkeypatch, tmp_path, body="ordinary body")
    monkeypatch.setattr(sys, "argv", ["issue.py", "1955"])

    rc = issue.main()

    assert rc == 0
    hit = _mirror.read_issue(str(mirror_dir), "1955")
    assert hit.state == _mirror.CACHED, hit
    stored = json.loads((mirror_dir / "issues" / "1955.json").read_text(encoding="utf-8"))
    assert stored["issue"]["body"] == "ordinary body"


def test_a_truncated_render_still_mirrors_the_full_untruncated_body(monkeypatch, capsys, tmp_path) -> None:
    """The whole point of #1955's point 1: a display cap must never reach
    the mirror. desc_max is DESCRIPTION_MAX (3000) for a non-`:full` call."""
    mirror_dir = _configure_mirror(tmp_path)
    long_body = "x" * (issue.DESCRIPTION_MAX + 500)
    _install(monkeypatch, tmp_path, body=long_body)
    monkeypatch.setattr(sys, "argv", ["issue.py", "1955"])

    rc = issue.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "TRUNCATED" in out or "withheld" in out.lower(), (
        "the render itself is not exercising the cap this test is about", out[:200]
    )
    stored = json.loads((mirror_dir / "issues" / "1955.json").read_text(encoding="utf-8"))
    assert len(stored["issue"]["body"]) == len(long_body), (
        "the mirror stored a truncated body -- it must always be the full API reply"
    )


def test_no_mirror_configured_is_a_silent_no_op_never_an_error(monkeypatch, capsys, tmp_path) -> None:
    """The mirror is opt-in. With no `gh_mirror_dir` key at all, `gh-issue`
    must render exactly as it always has -- no new output, no failure."""
    _install(monkeypatch, tmp_path, body="ordinary body")
    monkeypatch.setattr(sys, "argv", ["issue.py", "1955"])

    rc = issue.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "note: gh mirror" not in out, "an unconfigured mirror must not print anything about itself"


def test_a_mirror_write_failure_does_not_take_the_read_down_with_it(monkeypatch, capsys, tmp_path) -> None:
    """A side effect must not be able to fail the primary read it rides on."""
    _configure_mirror(tmp_path)
    _install(monkeypatch, tmp_path, body="ordinary body")
    monkeypatch.setattr(sys, "argv", ["issue.py", "1955"])
    monkeypatch.setattr(_mirror, "write_issue", lambda *a, **k: "disk is full")
    monkeypatch.setattr(issue, "_mirror", _mirror)

    rc = issue.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "# #1955 mirror test" in out, "the primary render must still succeed"
