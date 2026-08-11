"""Tests for presets/github/issue.py — :full flag fetches untruncated body+comments."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PRESET_PATH = Path(__file__).parent.parent / "presets" / "github" / "issue.py"
_spec = importlib.util.spec_from_file_location("github_issue", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
issue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issue)


def _fake_run(stdout: str, returncode: int = 0) -> Any:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=""
    )


def _install_fakes(monkeypatch, *, body: str, comments: list[dict]) -> None:
    issue_payload = json.dumps({
        "number": 42,
        "title": "Plan",
        "state": "OPEN",
        "labels": [],
        "milestone": None,
        "assignees": [],
        "author": {"login": "florian"},
        "url": "",
        "body": body,
        "comments": comments,
    })
    pr_payload = json.dumps([])

    def fake_gh(args, timeout=10):
        if args and args[0] == "pr":
            return _fake_run(pr_payload)
        return _fake_run(issue_payload)

    monkeypatch.setattr(issue, "_gh", fake_gh)
    monkeypatch.setattr(issue, "_download_images", lambda urls, n: [])


def _comment(login: str, body: str) -> dict:
    return {
        "author": {"login": login},
        "body": body,
        "createdAt": "2026-05-07T20:00:00Z",
    }


def test_default_truncates_long_body(monkeypatch, capsys) -> None:
    long_body = "x" * (issue.DESCRIPTION_MAX + 500)
    _install_fakes(monkeypatch, body=long_body, comments=[])
    monkeypatch.setattr(sys, "argv", ["issue.py", "42"])
    rc = issue.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert ("x" * (issue.DESCRIPTION_MAX + 500)) not in out


def test_full_flag_keeps_full_body(monkeypatch, capsys) -> None:
    long_body = "x" * (issue.DESCRIPTION_MAX + 500)
    _install_fakes(monkeypatch, body=long_body, comments=[])
    monkeypatch.setattr(sys, "argv", ["issue.py", "42", "full"])
    rc = issue.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "x" * (issue.DESCRIPTION_MAX + 500) in out


def test_default_caps_at_10_comments(monkeypatch, capsys) -> None:
    """Ten of fifteen, and since #738 they are the first three and the last
    seven rather than the last ten — so the comment that vanishes is one from
    the middle, not the opening one."""
    comments = [_comment(f"u{i}", f"msg-{i}") for i in range(15)]
    _install_fakes(monkeypatch, body="d", comments=comments)
    monkeypatch.setattr(sys, "argv", ["issue.py", "42"])
    issue.main()
    out = capsys.readouterr().out
    assert "msg-14" in out
    assert "msg-0" in out
    assert "msg-5" not in out
    assert "use :full" in out


def test_full_flag_shows_all_comments(monkeypatch, capsys) -> None:
    comments = [_comment(f"u{i}", f"msg-{i}") for i in range(15)]
    _install_fakes(monkeypatch, body="d", comments=comments)
    monkeypatch.setattr(sys, "argv", ["issue.py", "42", "full"])
    issue.main()
    out = capsys.readouterr().out
    assert "msg-0" in out
    assert "msg-14" in out


def test_full_flag_keeps_long_comment_body(monkeypatch, capsys) -> None:
    long_body = "y" * (issue.COMMENT_MAX + 500)
    _install_fakes(monkeypatch, body="d", comments=[_comment("florian", long_body)])
    monkeypatch.setattr(sys, "argv", ["issue.py", "42", "full"])
    issue.main()
    out = capsys.readouterr().out
    assert long_body in out
    assert "truncated" not in out


def test_default_truncates_long_comment_body(monkeypatch, capsys) -> None:
    long_body = "y" * (issue.COMMENT_MAX + 500)
    _install_fakes(monkeypatch, body="d", comments=[_comment("florian", long_body)])
    monkeypatch.setattr(sys, "argv", ["issue.py", "42"])
    issue.main()
    out = capsys.readouterr().out
    assert "truncated" in out


def test_default_body_truncation_is_disclosed_with_amount_and_escape_hatch(
    monkeypatch, capsys
) -> None:
    """#681: a truncated body must be distinguishable from a complete one —
    the disclosure has to state how much was withheld and how to get the
    rest, not just that *something* happened."""
    long_body = "x" * (issue.DESCRIPTION_MAX + 500)
    _install_fakes(monkeypatch, body=long_body, comments=[])
    monkeypatch.setattr(sys, "argv", ["issue.py", "42"])
    rc = issue.main()
    out = capsys.readouterr().out
    assert rc == 0
    # Withheld amount is stated in exact numbers, not just "truncated".
    assert "500" in out
    assert "use :full" in out


def test_full_flag_body_carries_no_truncation_disclosure(monkeypatch, capsys) -> None:
    """A complete read must not carry the marker meant for a partial one —
    otherwise the two are indistinguishable in the other direction."""
    long_body = "x" * (issue.DESCRIPTION_MAX + 500)
    _install_fakes(monkeypatch, body=long_body, comments=[])
    monkeypatch.setattr(sys, "argv", ["issue.py", "42", "full"])
    issue.main()
    out = capsys.readouterr().out
    assert "withheld" not in out
    assert "TRUNCATED" not in out


def test_default_body_truncation_marker_is_visible_before_the_body(
    monkeypatch, capsys
) -> None:
    """#681's exact failure mode: the marker existing somewhere is not
    enough if a reader stops at the top. The disclosure must appear in the
    header, before '## Description', not only after the cut content."""
    long_body = "x" * (issue.DESCRIPTION_MAX + 500)
    _install_fakes(monkeypatch, body=long_body, comments=[])
    monkeypatch.setattr(sys, "argv", ["issue.py", "42"])
    issue.main()
    out = capsys.readouterr().out
    desc_idx = out.index("## Description")
    disclosure_idx = out.index("withheld")
    assert disclosure_idx < desc_idx


def test_truncation_lands_on_a_line_boundary_not_mid_heading(
    monkeypatch, capsys
) -> None:
    """#681's own repro: a raw byte-cut landed 3 chars into a heading
    ('## The'), which is both malformed markdown and gives the output a
    natural-looking ending ('## Comments (0)' right after) that reads as
    complete. The cut must back off to the previous line break."""
    prefix = "x" * (issue.DESCRIPTION_MAX - 5)
    heading = "## The rest of this heading, well beyond the cap " + "z" * 200
    body = prefix + "\n" + heading
    _install_fakes(monkeypatch, body=body, comments=[])
    monkeypatch.setattr(sys, "argv", ["issue.py", "42"])
    issue.main()
    out = capsys.readouterr().out
    # A raw body[:DESCRIPTION_MAX] slice here produces the trailing
    # fragment "## T" — the same shape as the issue's "## The".
    assert "## T" not in out
