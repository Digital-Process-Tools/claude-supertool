"""#2248 -- `gh-job:ID:artifact:PATH` advertises a download cap
(`GH_JOB_ARTIFACT_DOWNLOAD_MAX_BYTES`) that only applies when GitHub's own
artifact listing carries a numeric `size_in_bytes`. When that field is
missing or not numeric, the `isinstance` guard in `print_artifact`
(`presets/github/job.py`) short-circuits silently: the whole archive is
still downloaded into memory, unbounded, and nothing in the receipt says the
cap was never consulted.

This is the same three-state defect this codebase keeps filing under a new
name: "under the cap" and "the cap could not be checked" must not render the
same way. `presets/gitlab/job.py`'s own `print_artifacts` already draws this
line for its sibling `size` field -- unusable input renders as literal
"unknown size" rather than silently skipping the size line -- and
`_is_past`'s docstring names the same convention for a malformed timestamp.
This fix gives `gh-job:ID:artifact:PATH` a matching disclosure.
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

PRESETS = Path(__file__).parent.parent / "presets"


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PRESETS / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gh_job = _load("github/job.py", "github_job_2248")

_REAL_RUN = subprocess.run
GH_ID = "92792057296"


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _fake_gh(job_meta: dict, run_artifacts: list, zips: dict[int, bytes]):
    def fake_run(args: list[str], **kw: Any):
        if args and args[0] == "git":
            return _REAL_RUN(args, **kw)
        assert args and args[0] == "gh", f"unstubbed command: {args!r}"
        joined = " ".join(str(a) for a in args)
        if "actions/jobs/" in joined and "logs" not in joined:
            return subprocess.CompletedProcess(args, 0, json.dumps(job_meta), "")
        if "/artifacts?" in joined or joined.endswith("/artifacts"):
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"artifacts": run_artifacts}), "")
        if "/zip" in joined:
            for artifact_id, data in zips.items():
                if f"artifacts/{artifact_id}/zip" in joined:
                    return subprocess.CompletedProcess(args, 0, data, b"")
            return subprocess.CompletedProcess(args, 1, b"", b"404 Not Found")
        raise AssertionError(f"unstubbed gh call: {args!r}")
    return fake_run


def test_gh_artifact_missing_size_discloses_the_cap_was_not_checked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """size_in_bytes absent entirely -- the cap must be disclosed as
    unenforced, not silently skipped, and the download must still proceed
    (disclosure, not a refusal -- GitHub reliably supplies this field, so
    refusing outright would punish every caller for a field this op has
    never actually seen missing)."""
    zip_data = _zip_bytes({"error-context.md": b"the render-error box never appeared"})
    artifacts = [{"id": 1, "name": "test-results"}]  # no size_in_bytes at all
    monkeypatch.setattr(sys, "argv", ["job.py", GH_ID, "artifact", "error-context.md"])
    monkeypatch.setattr(gh_job.subprocess, "run",
                        _fake_gh({"run_id": "555"}, artifacts, {1: zip_data}))
    rc = gh_job.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "render-error box never appeared" in out, (
        "disclosure-only must still let the download proceed"
    )
    assert "could not be checked" in out or "size unknown" in out, (
        f"missing size_in_bytes must disclose the cap was never consulted: {out!r}"
    )
    assert "GH_JOB_ARTIFACT_DOWNLOAD_MAX_BYTES" in out


def test_gh_artifact_non_numeric_size_discloses_the_cap_was_not_checked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """size_in_bytes present but non-numeric (a malformed/forged response) --
    same disclosure as the missing-field case, not the numeric-pass case."""
    zip_data = _zip_bytes({"a.txt": b"CONTENT"})
    artifacts = [{"id": 1, "name": "weird", "size_in_bytes": "not-a-number"}]
    monkeypatch.setattr(sys, "argv", ["job.py", GH_ID, "artifact", "a.txt"])
    monkeypatch.setattr(gh_job.subprocess, "run",
                        _fake_gh({"run_id": "555"}, artifacts, {1: zip_data}))
    rc = gh_job.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "could not be checked" in out or "size unknown" in out


def test_gh_artifact_numeric_size_under_cap_renders_no_disclosure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Positive control: a genuine numeric size under the cap is a clean
    pass and must NOT carry the "could not be checked" disclosure -- the
    two states must render differently, which is the whole point."""
    zip_data = _zip_bytes({"a.txt": b"CONTENT"})
    artifacts = [{"id": 1, "name": "fine", "size_in_bytes": len(zip_data)}]
    monkeypatch.setattr(sys, "argv", ["job.py", GH_ID, "artifact", "a.txt"])
    monkeypatch.setattr(gh_job.subprocess, "run",
                        _fake_gh({"run_id": "555"}, artifacts, {1: zip_data}))
    rc = gh_job.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "CONTENT" in out
    assert "could not be checked" not in out
    assert "size unknown" not in out
