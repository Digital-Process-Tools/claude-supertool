"""`gl-issue`'s related-MR section has #815's gap too — and one worse (#815).

#815's second comment verified on a live DVSI issue that `gl-issue` prints an
MR's state, title and branch and says nothing about its pipeline: the identical
gap to the GitHub side, and the comment asks for both families in one change or
the fix becomes the parity gap it was meant to close.

Two things are pinned here.

**1. The pipeline status.** `head_pipeline` is already in the payload the op
fetches — verified against a live GitLab instance on 2026-08-07, on
`projects/:id/issues/12634/related_merge_requests`, which returns the MR
*detail* representation including `head_pipeline.status`. So this costs no
extra call, same as the GitHub side.

It is a **status, not a leg tally**, and that asymmetry is deliberate rather
than an oversight: GitLab's MR payload carries no per-job breakdown, and
reaching one means `projects/:id/pipelines/N/jobs` per MR — a real extra call
per related MR, which is exactly the cost #815 says not to incur silently. A
status GitLab actually gave us beats a tally bought at N requests.

**2. A failed lookup vanishes the whole section.** `if mr_result.returncode ==
0:` has no `else`, and the enclosing `except (TimeoutExpired, JSONDecodeError):
pass` swallows the rest — so a failed related-MR query prints *nothing at all*,
and the reader sees no `Related MRs` line and concludes there are none. That is
#780 item 1 on the GitHub side, still live on the GitLab one, and it is a
stronger version of it: GitHub at least prints `Linked PRs: unknown`.

The comment on #815 states that neither family omits the section — true at
*zero*, which is what was tested there. It is not true on *failure*.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parent.parent
PRESET = ROOT / "presets" / "gitlab" / "issue.py"
_spec = importlib.util.spec_from_file_location("gitlab_issue_815", PRESET)
assert _spec is not None and _spec.loader is not None
issue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(issue)


def _ok(stdout: str) -> Any:
    return subprocess.CompletedProcess(
        args=["glab"], returncode=0, stdout=stdout, stderr="")


ISSUE_PAYLOAD = json.dumps({
    "title": "Plan", "state": "opened", "labels": [], "milestone": None,
    "assignees": [], "author": {"username": "florian"}, "iid": 815,
    "web_url": "", "description": "body", "project_id": 1,
})


def _mr(iid: int, pipeline: object, *, with_key: bool = True) -> dict:
    mr = {"iid": iid, "title": f"MR {iid}", "state": "merged",
          "source_branch": f"feat/{iid}"}
    if with_key:
        mr["head_pipeline"] = pipeline
    return mr


def _render(monkeypatch, capsys, mrs: object, *, related_rc: int = 0,
            raises: Exception | None = None, raw: str | None = None) -> str:
    def fake_glab(args, timeout=10):
        return _ok(ISSUE_PAYLOAD)

    def fake_glab_api(endpoint, timeout=10):
        if "related_merge_requests" in endpoint:
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(
                args=["glab"], returncode=related_rc,
                stdout=json.dumps(mrs) if raw is None else raw, stderr="boom")
        return _ok("[]")

    monkeypatch.setattr(issue, "_glab", fake_glab)
    monkeypatch.setattr(issue, "_glab_api", fake_glab_api)
    monkeypatch.setattr(issue, "_download_images", lambda urls, n: [])
    monkeypatch.setattr(sys, "argv", ["issue.py", "815"])
    assert issue.main() == 0
    return capsys.readouterr().out


def _section(out: str) -> str:
    assert "Related MRs" in out, f"no related-MR section at all:\n{out}"
    return out.split("Related MRs")[1].split("## Description")[0]


def test_a_failed_pipeline_is_visible(monkeypatch, capsys) -> None:
    """The #815 gap: state and branch, nothing about CI."""
    body = _section(_render(monkeypatch, capsys, [
        _mr(32848, {"id": 154290, "status": "failed"})]))
    assert "failed" in body, (
        f"the MR's pipeline failed and the section does not say so:\n{body}")


def test_a_failed_pipeline_is_distinguishable_from_a_passing_one(
        monkeypatch, capsys) -> None:
    red = _section(_render(monkeypatch, capsys, [
        _mr(32848, {"id": 154290, "status": "failed"})]))
    ok = _section(_render(monkeypatch, capsys, [
        _mr(32848, {"id": 154290, "status": "success"})]))
    assert red != ok, f"a red and a green MR render identically:\n{ok}"
    assert "success" in ok, ok


def test_no_pipeline_is_declined_not_called_absent(monkeypatch, capsys) -> None:
    """`head_pipeline: null` — GitLab makes one at push time, so this is ambiguous."""
    body = _section(_render(monkeypatch, capsys, [_mr(32848, None)]))
    assert "UNKNOWN" in body, (
        f"'no pipeline' is being asserted where nothing established it:\n{body}")


def test_a_missing_key_is_also_declined(monkeypatch, capsys) -> None:
    """An older GitLab, or a list representation without the detail fields."""
    body = _section(_render(monkeypatch, capsys, [
        _mr(32848, None, with_key=False)]))
    assert "UNKNOWN" in body, body


def test_a_failed_lookup_does_not_vanish_the_section(monkeypatch, capsys) -> None:
    """The worse defect: no section at all reads as 'this issue has no MRs'."""
    out = _render(monkeypatch, capsys, [], related_rc=1)
    assert "Related MRs" in out, (
        "the related-MR lookup failed and the op printed nothing, so the "
        f"reader concludes there are none:\n{out}")
    assert "unknown" in _section(out).lower(), out
    assert "none" not in _section(out).lower(), (
        f"'none' is a claim the op cannot support here:\n{out}")


@pytest.mark.parametrize("boom", [
    subprocess.TimeoutExpired(cmd="glab", timeout=1),
    # Windows raises this from `subprocess.run(["glab", ...])` when glab is not
    # on PATH — `[WinError 2] The system cannot find the file specified` —
    # where a POSIX shell may resolve differently. It escaped the old
    # `except (TimeoutExpired, JSONDecodeError)` entirely, so the op crashed
    # instead of reaching its own "the tool failed" arm. #997's mechanism.
    FileNotFoundError(2, "No such file or directory: 'glab'"),
    PermissionError(13, "Permission denied: 'glab'"),
], ids=["timeout", "glab-not-on-path", "glab-not-executable"])
def test_a_spawn_failure_does_not_vanish_the_section(
        monkeypatch, capsys, boom) -> None:
    out = _render(monkeypatch, capsys, [], raises=boom)
    assert "Related MRs" in out, (
        f"{type(boom).__name__} was swallowed silently:\n{out}")
    assert "unknown" in _section(out).lower(), out


def test_malformed_json_does_not_vanish_the_section(monkeypatch, capsys) -> None:
    """The realistic shape: glab exits 0 and prints something unparseable."""
    out = _render(monkeypatch, capsys, [], raw="{not json")
    assert "Related MRs" in out, out
    assert "unknown" in _section(out).lower(), out
    assert "none" not in _section(out).lower(), out


def test_an_answered_zero_still_says_none(monkeypatch, capsys) -> None:
    """The control: 'none' must stay available for a real, answered zero."""
    out = _render(monkeypatch, capsys, [])
    assert "Related MRs: none" in out, out
    assert "unknown" not in _section(out).lower(), out
