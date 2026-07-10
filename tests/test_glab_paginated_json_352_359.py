"""Regression tests for #352 / #359 — paginated glab /jobs payload parsing.

`glab api .../jobs --paginate` concatenates one JSON array per page into a
single stdout stream, e.g. `[{job1}][{job2}]`. A lone `json.loads()` on that
multi-document string raises JSONDecodeError, surfaced as "invalid JSON from
glab". This blocks:

  #352  gl-pipeline:ID          (default full board)
  #359  gl-pipeline:ID:failed   (failed-jobs filter)

These tests mock subprocess to return a realistic two-page concatenated
payload and assert both pages' jobs are parsed and merged. They also guard the
common single-page case and edge cases (empty page, empty result).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PRESET_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "pipeline.py"
_spec = importlib.util.spec_from_file_location("gitlab_pipeline", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
pipe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pipe)


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _job(i: int, status: str) -> dict:
    return {
        "name": f"job_{i}_{status}",
        "stage": "test",
        "status": status,
        "duration": 5.0,
        "id": 1000 + i,
        "web_url": f"https://gl/job/{1000 + i}",
        "pipeline": {"status": "running"},
    }


def _page(*jobs: dict) -> str:
    """One page = one JSON array, exactly as glab emits per page."""
    return json.dumps(list(jobs))


def _paginated(*pages: str) -> str:
    """glab --paginate concatenates page bodies back-to-back: [..][..]."""
    return "".join(pages)


def _patch(monkeypatch, payload: str, argv: list[str]) -> None:
    monkeypatch.setattr(
        pipe.subprocess, "run",
        lambda *a, **k: _Result(payload, "", 0),
    )
    monkeypatch.setattr(sys, "argv", argv)


# ---------------------------------------------------------------------------
# #352 — default full board with a multi-page (concatenated) payload
# ---------------------------------------------------------------------------

def test_352_full_board_parses_two_pages(monkeypatch, capsys) -> None:
    payload = _paginated(
        _page(_job(0, "success"), _job(1, "failed")),
        _page(_job(2, "success"), _job(3, "running")),
    )
    _patch(monkeypatch, payload, ["pipeline.py", "151111"])
    assert pipe.main() == 0
    out = capsys.readouterr().out
    assert "invalid JSON" not in out
    # Jobs from BOTH pages are present.
    assert "job_0_success" in out
    assert "job_1_failed" in out
    assert "job_3_running" in out
    # The failed job from page 1 is picked up in the failed detail.
    assert "## Failed jobs (1)" in out


# ---------------------------------------------------------------------------
# #359 — failed filter with a multi-page (concatenated) payload
# ---------------------------------------------------------------------------

def test_359_failed_filter_parses_two_pages(monkeypatch, capsys) -> None:
    # A failed job lives on the SECOND page — only merging both finds it.
    payload = _paginated(
        _page(_job(0, "success"), _job(1, "running")),
        _page(_job(2, "failed"), _job(3, "success")),
    )
    _patch(monkeypatch, payload, ["pipeline.py", "151165", "failed"])
    assert pipe.main() == 0
    out = capsys.readouterr().out
    assert "invalid JSON" not in out
    assert "job_2_failed" in out
    assert "## Failed jobs (1)" in out
    assert "https://gl/job/1002" in out
    # No failed job should be reported as "No failed jobs".
    assert "No failed jobs." not in out


# ---------------------------------------------------------------------------
# regression — single-page (common case) still works
# ---------------------------------------------------------------------------

def test_single_page_still_parses(monkeypatch, capsys) -> None:
    payload = _page(_job(0, "success"), _job(1, "failed"))
    _patch(monkeypatch, payload, ["pipeline.py", "42"])
    assert pipe.main() == 0
    out = capsys.readouterr().out
    assert "invalid JSON" not in out
    assert "job_0_success" in out
    assert "job_1_failed" in out


# ---------------------------------------------------------------------------
# edge cases
# ---------------------------------------------------------------------------

def test_empty_page_among_pages(monkeypatch, capsys) -> None:
    # A trailing empty page ([]) is valid glab output and must not break parsing.
    payload = _paginated(
        _page(_job(0, "success")),
        _page(),
    )
    _patch(monkeypatch, payload, ["pipeline.py", "42", "failed"])
    assert pipe.main() == 0
    out = capsys.readouterr().out
    assert "invalid JSON" not in out
    assert "No failed jobs." in out


def test_all_empty_pages(monkeypatch, capsys) -> None:
    payload = _paginated(_page(), _page())
    _patch(monkeypatch, payload, ["pipeline.py", "42"])
    assert pipe.main() == 0
    assert "invalid JSON" not in capsys.readouterr().out


def test_helper_parses_concatenated_arrays() -> None:
    # Directly exercise the merge helper on the documented glab shape.
    merged = pipe._parse_paginated_json("[{\"a\":1}][{\"a\":2},{\"a\":3}]")
    assert merged == [{"a": 1}, {"a": 2}, {"a": 3}]


def test_helper_single_array() -> None:
    assert pipe._parse_paginated_json("[{\"a\":1}]") == [{"a": 1}]


# ---------------------------------------------------------------------------
# regression — a non-array top-level doc (bare error object) must not be
# rendered as a garbage job row; it keeps the explicit error message.
# ---------------------------------------------------------------------------

def test_helper_rejects_non_array_document() -> None:
    import pytest

    with pytest.raises(ValueError):
        pipe._parse_paginated_json("{\"message\":\"401 Unauthorized\"}")


def test_non_array_response_surfaces_unexpected_format(monkeypatch, capsys) -> None:
    _patch(monkeypatch, "{\"message\":\"401 Unauthorized\"}", ["pipeline.py", "42"])
    assert pipe.main() == 1
    out = capsys.readouterr().out
    assert "unexpected response format" in out
    assert "401 Unauthorized" not in out
