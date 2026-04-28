"""Tests for _format_error in preset scripts — actionable error messages for LLMs."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

PRESETS_DIR = Path(__file__).parent.parent / "presets"


def _load_format_error(preset: str, script: str):
    """Import _format_error from a preset script without running main()."""
    path = PRESETS_DIR / preset / script
    spec = importlib.util.spec_from_file_location(f"{preset}_{script}", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._format_error


# Load all 8 _format_error functions
@pytest.fixture(params=[
    ("gitlab", "issue.py"),
    ("gitlab", "mr.py"),
    ("gitlab", "pipeline.py"),
    ("gitlab", "job.py"),
    ("github", "issue.py"),
    ("github", "pr.py"),
    ("github", "run.py"),
    ("github", "job.py"),
])
def format_error(request):
    """Parametrized fixture returning (preset_name, script_name, _format_error)."""
    preset, script = request.param
    fn = _load_format_error(preset, script)
    return preset, script, fn


class TestNotFound:
    """404 errors produce actionable 'not found' messages."""

    def test_404_in_stderr(self, format_error):
        _, _, fn = format_error
        result = fn("404 Not Found", "Issue", "12345")
        assert "not found" in result.lower()
        assert "12345" in result

    def test_could_not_resolve(self, format_error):
        _, _, fn = format_error
        result = fn("GraphQL: Could not resolve to an issue", "PR", "42")
        assert "not found" in result.lower()
        assert "42" in result

    def test_not_found_text(self, format_error):
        _, _, fn = format_error
        result = fn("HTTP 404: Not Found (url)", "Run", "999")
        assert "not found" in result.lower()


class TestAuth:
    """Auth errors tell the user how to authenticate."""

    def test_unauthorized(self, format_error):
        preset, _, fn = format_error
        result = fn("401 Unauthorized", "Issue", "1")
        assert "auth" in result.lower()
        if preset == "gitlab":
            assert "glab auth login" in result
        else:
            assert "gh auth login" in result

    def test_token_error(self, format_error):
        _, _, fn = format_error
        result = fn("bad token or expired", "MR", "1")
        assert "auth" in result.lower()


class TestForbidden:
    """403 errors mention permission and how to check access."""

    def test_403(self, format_error):
        _, _, fn = format_error
        result = fn("403 Forbidden", "Pipeline", "100")
        assert "permission" in result.lower()
        assert "100" in result

    def test_forbidden_text(self, format_error):
        _, _, fn = format_error
        result = fn("forbidden: insufficient scope", "Job", "5")
        assert "permission" in result.lower()


class TestRateLimit:
    """Rate limit errors (GitHub only — glab doesn't have this)."""

    def test_429(self):
        fn = _load_format_error("github", "issue.py")
        result = fn("429 rate limit exceeded", "Issue", "1")
        assert "rate limit" in result.lower()
        assert "retry" in result.lower()


class TestFallback:
    """Unknown errors still include the raw stderr."""

    def test_unknown_error(self, format_error):
        _, _, fn = format_error
        result = fn("some weird unexpected error", "Issue", "42")
        assert "some weird unexpected error" in result
        assert "42" in result


class TestJobSpecificHints:
    """Job error messages include hints about which op to use first."""

    def test_gitlab_job_hint(self):
        fn = _load_format_error("gitlab", "job.py")
        result = fn("404 Not Found", "Job log", "999")
        assert "gl-pipeline" in result

    def test_github_job_hint(self):
        fn = _load_format_error("github", "job.py")
        result = fn("404 Not Found", "Job log", "999")
        assert "gh-run" in result
