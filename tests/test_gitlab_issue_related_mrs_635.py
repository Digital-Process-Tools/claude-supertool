"""gl-issue caps the related-MR list at 10 — it must say so (#635).

The half-silent case: the total is printed correctly, the list under it is cut,
and nothing marks the cut. A reader who sees `Related MRs: 47` above ten rows
concludes the numbers disagree with reality, not that ten is a ceiling.

The fake is the glab API response — the boundary the op actually reads — and
every assertion is on the rendered stdout, not on a return value. Swapping the
implementation for one that prints the same text keeps these passing; deleting
the disclosure does not.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "issue.py"
_spec = importlib.util.spec_from_file_location("gitlab_issue_635", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
issue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issue)


def _fake_run(stdout: str) -> Any:
    return subprocess.CompletedProcess(args=["glab"], returncode=0, stdout=stdout, stderr="")


def _install_fakes(monkeypatch, *, related: int) -> None:
    issue_payload = json.dumps({
        "title": "Plan", "state": "opened", "labels": [], "milestone": None,
        "assignees": [], "author": {"username": "florian"}, "iid": 635,
        "web_url": "", "description": "body", "project_id": 1,
    })
    related_payload = json.dumps([
        {"iid": 1000 + i, "title": f"MR {i}", "state": "opened",
         "source_branch": f"feat/{i}"}
        for i in range(related)
    ])

    def fake_glab(args, timeout=10):
        return _fake_run(issue_payload)

    def fake_glab_api(endpoint, timeout=10):
        if "related_merge_requests" in endpoint:
            return _fake_run(related_payload)
        return _fake_run("[]")

    monkeypatch.setattr(issue, "_glab", fake_glab)
    monkeypatch.setattr(issue, "_glab_api", fake_glab_api)
    monkeypatch.setattr(issue, "_download_images", lambda urls, n: [])


def _related_header(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("Related MRs:"):
            return line
    raise AssertionError(f"no 'Related MRs:' header in output:\n{out}")


def test_capped_related_mrs_disclose_in_the_header(monkeypatch, capsys) -> None:
    _install_fakes(monkeypatch, related=47)
    monkeypatch.setattr(sys, "argv", ["issue.py", "635"])
    assert issue.main() == 0
    out = capsys.readouterr().out

    # The fixture really ran and the cut really happened.
    assert out.count("    branch: ") == 10, out

    header = _related_header(out)
    assert "10" in header and "47" in header, f"header hides the cut: {header!r}"
    assert "full" in header, f"header names no way to see the rest: {header!r}"


def test_capped_related_mrs_disclose_after_the_list(monkeypatch, capsys) -> None:
    _install_fakes(monkeypatch, related=47)
    monkeypatch.setattr(sys, "argv", ["issue.py", "635"])
    assert issue.main() == 0
    out = capsys.readouterr().out

    # Bounded at "Closing MRs:" now that #1607 item 2 renders that section
    # immediately after this one, off a separately-stubbed endpoint.
    body = out.split("Related MRs:")[1].split("Closing MRs:")[0]
    lines = [line for line in body.strip().splitlines() if line.strip()]
    assert "37" in lines[-1], f"no footer marker after the list: {lines[-1]!r}"


def test_uncut_related_mrs_say_nothing_extra(monkeypatch, capsys) -> None:
    _install_fakes(monkeypatch, related=3)
    monkeypatch.setattr(sys, "argv", ["issue.py", "635"])
    assert issue.main() == 0
    out = capsys.readouterr().out

    assert out.count("    branch: ") == 3, out
    assert _related_header(out) == "Related MRs: 3", out
    body = out.split("Related MRs:")[1].split("Closing MRs:")[0]
    assert "shown" not in body and "full" not in body, body


def test_full_shows_every_related_mr_and_says_nothing(monkeypatch, capsys) -> None:
    _install_fakes(monkeypatch, related=47)
    monkeypatch.setattr(sys, "argv", ["issue.py", "635", "full"])
    assert issue.main() == 0
    out = capsys.readouterr().out

    assert out.count("    branch: ") == 47, out
    assert _related_header(out) == "Related MRs: 47", out
