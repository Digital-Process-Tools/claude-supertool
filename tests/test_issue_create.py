"""Tests for presets/gitlab/issue_create.py and presets/github/issue_create.py."""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Load modules under test
# ---------------------------------------------------------------------------

GL_PATH = Path(__file__).parent.parent / "presets" / "gitlab" / "issue_create.py"
_gl_spec = importlib.util.spec_from_file_location("gitlab_issue_create", GL_PATH)
assert _gl_spec is not None and _gl_spec.loader is not None
gl = importlib.util.module_from_spec(_gl_spec)
_gl_spec.loader.exec_module(gl)

GH_PATH = Path(__file__).parent.parent / "presets" / "github" / "issue_create.py"
_gh_spec = importlib.util.spec_from_file_location("github_issue_create", GH_PATH)
assert _gh_spec is not None and _gh_spec.loader is not None
gh = importlib.util.module_from_spec(_gh_spec)
_gh_spec.loader.exec_module(gh)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _err(stderr: str, returncode: int = 1) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def _write_payload(tmp_path: Path, data: dict) -> str:
    p = tmp_path / "payload.json"
    p.write_text(json.dumps(data))
    return str(p)


def _write_body_file(tmp_path: Path, content: str) -> str:
    p = tmp_path / "body.md"
    p.write_text(content)
    return str(p)


def _flag_value(args: list[str], flag: str) -> str | None:
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return None


# ===========================================================================
# GitLab tests
# ===========================================================================

GL_MINIMAL = {
    "project": "fdavid/dvsi",
    "title": "Test issue",
    "description": "Hello world",
}

GL_FULL = {
    "project": "fdavid/dvsi",
    "title": "Full issue",
    "description": "Full description",
    "milestone_id": 171,
    "labels": ["AGY_OMS", "Todo"],
    "assignee_ids": [2, 5],
    "estimate": "4h",
    "links": [{"target_iid": 12240, "type": "relates_to"}],
}

GL_URL = "https://gitlab.dp.tools/fdavid/dvsi/-/issues/12370"


class TestGitlabIssueCreate:
    def test_minimal_payload(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, GL_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        calls: list[list[str]] = []

        def fake_glab(args, timeout=20):
            calls.append(args)
            return _ok(GL_URL)

        def fake_glab_api(method, endpoint, *extra, timeout=15):
            return _ok("{}")

        monkeypatch.setattr(gl, "_glab", fake_glab)
        monkeypatch.setattr(gl, "_glab_api", fake_glab_api)

        rc = gl.main()
        out = capsys.readouterr().out

        assert rc == 0
        assert "gl-issue-create OK" in out
        assert "iid=12370" in out
        assert "url=" in out
        assert any("issue" in " ".join(c) and "create" in " ".join(c) for c in calls)

    def test_full_payload_passes_all_fields(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, GL_FULL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        glab_calls: list[list[str]] = []
        api_calls: list[tuple] = []

        def fake_glab(args, timeout=20):
            glab_calls.append(args)
            return _ok(GL_URL)

        def fake_glab_api(method, endpoint, *extra, timeout=15):
            api_calls.append((method, endpoint, extra))
            return _ok("{}")

        monkeypatch.setattr(gl, "_glab", fake_glab)
        monkeypatch.setattr(gl, "_glab_api", fake_glab_api)

        rc = gl.main()
        out = capsys.readouterr().out

        assert rc == 0
        assert "gl-issue-create OK" in out

        cmd = " ".join(glab_calls[0]) if glab_calls else ""
        assert "AGY_OMS" in cmd
        assert "171" in cmd
        assert "2" in cmd

        assert len(api_calls) == 1
        assert "links" in api_calls[0][1]
        assert "12240" in str(api_calls[0][2])

    def test_description_file(self, monkeypatch, capsys, tmp_path):
        body_path = _write_body_file(tmp_path, "# From file\n\nContent here.")
        payload = {
            "project": "fdavid/dvsi",
            "title": "File desc issue",
            "description_file": str(body_path),
        }
        payload_file = _write_payload(tmp_path, payload)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        captured: list[list[str]] = []

        def fake_glab(args, timeout=20):
            captured.append(args)
            return _ok(GL_URL)

        monkeypatch.setattr(gl, "_glab", fake_glab)
        monkeypatch.setattr(gl, "_glab_api", lambda *a, **kw: _ok("{}"))

        rc = gl.main()
        assert rc == 0
        assert captured, "glab not called"
        assert "--description-file" not in captured[0], "should use --description, not --description-file"
        desc = _flag_value(captured[0], "--description")
        assert desc is not None, "--description arg not passed"
        assert "From file" in desc

    def test_estimate_appended_to_description(self, monkeypatch, capsys, tmp_path):
        payload = {
            "project": "fdavid/dvsi",
            "title": "Estimated issue",
            "description": "Work item",
            "estimate": "4h",
        }
        payload_file = _write_payload(tmp_path, payload)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        captured: list[list[str]] = []

        def fake_glab(args, timeout=20):
            captured.append(args)
            return _ok(GL_URL)

        monkeypatch.setattr(gl, "_glab", fake_glab)
        monkeypatch.setattr(gl, "_glab_api", lambda *a, **kw: _ok("{}"))

        rc = gl.main()
        assert rc == 0
        assert captured
        desc = _flag_value(captured[0], "--description")
        assert desc is not None
        assert "/estimate 4h" in desc

    def test_uses_description_flag_not_file(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, GL_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        captured: list[list[str]] = []
        monkeypatch.setattr(gl, "_glab", lambda args, timeout=20: captured.append(args) or _ok(GL_URL))
        monkeypatch.setattr(gl, "_glab_api", lambda *a, **kw: _ok("{}"))

        rc = gl.main()
        assert rc == 0
        assert "--description-file" not in captured[0]
        assert _flag_value(captured[0], "--description") == "Hello world"

    def test_at_prefix_stripped(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, GL_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", "@" + payload_file])
        monkeypatch.setattr(gl, "_glab", lambda args, timeout=20: _ok(GL_URL))
        monkeypatch.setattr(gl, "_glab_api", lambda *a, **kw: _ok("{}"))

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "gl-issue-create OK" in out

    def test_stdin_via_at_dash(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["issue_create.py", "@-"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(GL_MINIMAL)))
        monkeypatch.setattr(gl, "_glab", lambda args, timeout=20: _ok(GL_URL))
        monkeypatch.setattr(gl, "_glab_api", lambda *a, **kw: _ok("{}"))

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "gl-issue-create OK" in out

    def test_error_path_propagates(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, GL_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setattr(gl, "_glab", lambda args, timeout=20: _err("401 Unauthorized"))
        monkeypatch.setattr(gl, "_glab_api", lambda *a, **kw: _ok("{}"))

        rc = gl.main()
        out = capsys.readouterr().out

        assert rc != 0
        assert "ERROR" in out
        assert "401" in out

    def test_missing_project_field(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, {"title": "No project"})
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        # No config default and no matching git remote → still an error.
        monkeypatch.setattr(gl._rd, "resolve", lambda *a, **k: None)

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "project" in out

    def test_project_defaulted_when_missing(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, {"title": "No project", "description": "x"})
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setattr(gl._rd, "resolve", lambda *a, **k: "fdavid/dvsi")

        captured: list[list[str]] = []
        monkeypatch.setattr(gl, "_glab", lambda args, timeout=20: captured.append(args) or _ok(GL_URL))
        monkeypatch.setattr(gl, "_glab_api", lambda *a, **kw: _ok("{}"))

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "gl-issue-create OK" in out
        assert _flag_value(captured[0], "--repo") == "fdavid/dvsi"

    def test_missing_title_field(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, {"project": "fdavid/dvsi"})
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "title" in out

    def test_conflicting_description_fields(self, monkeypatch, capsys, tmp_path):
        payload = {
            "project": "fdavid/dvsi",
            "title": "Conflict",
            "description": "inline",
            "description_file": "/tmp/body.md",
        }
        payload_file = _write_payload(tmp_path, payload)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "description" in out

    def test_estimate_invalid_format_rejected(self, monkeypatch, capsys, tmp_path):
        for i, bad in enumerate(["abc", "4h; /spend 8h", "4h\n/spend", "4 h"]):
            sub = tmp_path / f"bad_{i}"
            sub.mkdir()
            payload = {**GL_MINIMAL, "estimate": bad}
            payload_file = _write_payload(sub, payload)
            monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
            monkeypatch.setattr(gl, "_glab", lambda args, timeout=20: _ok(GL_URL))
            monkeypatch.setattr(gl, "_glab_api", lambda *a, **kw: _ok("{}"))
            rc = gl.main()
            out = capsys.readouterr().out
            assert rc != 0, f"expected failure for estimate={bad!r}"
            assert "invalid estimate" in out.lower() or "error" in out.lower()

    def test_links_skipped_when_iid_non_numeric(self, monkeypatch, capsys, tmp_path):
        payload = {
            **GL_MINIMAL,
            "links": [{"target_iid": 100, "type": "relates_to"}],
        }
        payload_file = _write_payload(tmp_path, payload)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        api_calls: list = []

        def fake_glab(args, timeout=20):
            return _ok("https://gitlab.dp.tools/fdavid/dvsi/-/issues/some-slug")

        def fake_glab_api(method, endpoint, *extra, timeout=15):
            api_calls.append((method, endpoint))
            return _ok("{}")

        monkeypatch.setattr(gl, "_glab", fake_glab)
        monkeypatch.setattr(gl, "_glab_api", fake_glab_api)

        rc = gl.main()
        err = capsys.readouterr().err
        assert rc == 0
        assert len(api_calls) == 0, "link API should not be called for non-numeric iid"
        assert "links skipped" in err

    def test_batch_two_issues(self, monkeypatch, capsys, tmp_path):
        urls = [
            "https://gitlab.dp.tools/fdavid/dvsi/-/issues/100",
            "https://gitlab.dp.tools/fdavid/dvsi/-/issues/101",
        ]
        call_count = [0]

        def fake_glab(args, timeout=20):
            url = urls[call_count[0]]
            call_count[0] += 1
            return _ok(url)

        monkeypatch.setattr(gl, "_glab", fake_glab)
        monkeypatch.setattr(gl, "_glab_api", lambda *a, **kw: _ok("{}"))

        for i, expected_iid in enumerate([100, 101]):
            sub = tmp_path / f"gl_batch_{i}"
            sub.mkdir()
            payload_file = _write_payload(sub, {
                "project": "fdavid/dvsi",
                "title": f"Issue {i}",
                "description": f"Body {i}",
            })
            monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
            rc = gl.main()
            out = capsys.readouterr().out
            assert rc == 0
            assert f"iid={expected_iid}" in out


# ===========================================================================
# GitHub tests
# ===========================================================================

GH_MINIMAL = {
    "repo": "Digital-Process-Tools/claude-supertool",
    "title": "Test issue",
    "body": "Hello world",
}

GH_FULL = {
    "repo": "Digital-Process-Tools/claude-supertool",
    "title": "Full issue",
    "body": "Full body",
    "labels": ["bug", "enhancement"],
    "assignees": ["fdavid"],
    "milestone": "v1.0",
}

GH_URL = "https://github.com/Digital-Process-Tools/claude-supertool/issues/42"


class TestGithubIssueCreate:
    def test_minimal_payload(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, GH_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        calls: list[list[str]] = []

        def fake_gh(args, timeout=20):
            calls.append(args)
            return _ok(GH_URL)

        monkeypatch.setattr(gh, "_gh", fake_gh)

        rc = gh.main()
        out = capsys.readouterr().out

        assert rc == 0
        assert "gh-issue-create OK" in out
        assert "number=42" in out
        assert "url=" in out
        assert any("issue" in " ".join(c) and "create" in " ".join(c) for c in calls)

    def test_full_payload_passes_all_fields(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, GH_FULL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        gh_calls: list[list[str]] = []

        def fake_gh(args, timeout=20):
            gh_calls.append(args)
            return _ok(GH_URL)

        monkeypatch.setattr(gh, "_gh", fake_gh)

        rc = gh.main()
        out = capsys.readouterr().out

        assert rc == 0
        assert "gh-issue-create OK" in out

        cmd = " ".join(gh_calls[0]) if gh_calls else ""
        assert "bug" in cmd
        assert "fdavid" in cmd
        assert "v1.0" in cmd

    def test_body_file(self, monkeypatch, capsys, tmp_path):
        body_path = _write_body_file(tmp_path, "# From file\n\nGH content.")
        payload = {
            "repo": "Digital-Process-Tools/claude-supertool",
            "title": "File body issue",
            "body_file": str(body_path),
        }
        payload_file = _write_payload(tmp_path, payload)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        written_bodies: list[str] = []

        def fake_gh(args, timeout=20):
            for i, a in enumerate(args):
                if a == "--body-file" and i + 1 < len(args):
                    written_bodies.append(Path(args[i + 1]).read_text())
            return _ok(GH_URL)

        monkeypatch.setattr(gh, "_gh", fake_gh)

        rc = gh.main()
        assert rc == 0
        assert written_bodies
        assert "From file" in written_bodies[0]

    def test_error_path_propagates(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, GH_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setattr(gh, "_gh", lambda args, timeout=20: _err("gh: not authenticated"))

        rc = gh.main()
        out = capsys.readouterr().out

        assert rc != 0
        assert "ERROR" in out

    def test_missing_repo_field(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, {"title": "No repo"})
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        # No config default and no matching git remote → still an error.
        monkeypatch.setattr(gh._rd, "resolve", lambda *a, **k: None)

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "repo" in out

    def test_repo_defaulted_when_missing(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, {"title": "No repo", "body": "x"})
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setattr(gh._rd, "resolve", lambda *a, **k: "Digital-Process-Tools/claude-supertool")

        captured: list[list[str]] = []
        monkeypatch.setattr(gh, "_gh", lambda args, timeout=20: captured.append(args) or _ok(GH_URL))

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "gh-issue-create OK" in out
        assert _flag_value(captured[0], "--repo") == "Digital-Process-Tools/claude-supertool"

    def test_missing_title_field(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, {"repo": "org/repo"})
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "title" in out

    def test_conflicting_body_fields(self, monkeypatch, capsys, tmp_path):
        payload = {
            "repo": "org/repo",
            "title": "Conflict",
            "body": "inline",
            "body_file": "/tmp/body.md",
        }
        payload_file = _write_payload(tmp_path, payload)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "body" in out

    def test_url_extraction_with_warning_lines(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, GH_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        noisy_stdout = (
            "Opening in browser...\n"
            "Warning: 2 assignees found with username 'fdavid'\n"
            "https://github.com/Digital-Process-Tools/claude-supertool/issues/42\n"
        )

        monkeypatch.setattr(gh, "_gh", lambda args, timeout=20: _ok(noisy_stdout))

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "number=42" in out
        assert "url=https://github.com/Digital-Process-Tools/claude-supertool/issues/42" in out

    def test_batch_two_issues(self, monkeypatch, capsys, tmp_path):
        urls = [
            "https://github.com/Digital-Process-Tools/claude-supertool/issues/10",
            "https://github.com/Digital-Process-Tools/claude-supertool/issues/11",
        ]
        call_count = [0]

        def fake_gh(args, timeout=20):
            url = urls[call_count[0]]
            call_count[0] += 1
            return _ok(url)

        monkeypatch.setattr(gh, "_gh", fake_gh)

        for i, expected_number in enumerate([10, 11]):
            sub = tmp_path / f"gh_batch_{i}"
            sub.mkdir()
            payload_file = _write_payload(sub, {
                "repo": "Digital-Process-Tools/claude-supertool",
                "title": f"Issue {i}",
                "body": f"Body {i}",
            })
            monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
            rc = gh.main()
            out = capsys.readouterr().out
            assert rc == 0
            assert f"number={expected_number}" in out

    def test_at_prefix_stripped(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, GH_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", "@" + payload_file])
        monkeypatch.setattr(gh, "_gh", lambda args, timeout=20: _ok(GH_URL))

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "gh-issue-create OK" in out

    def test_stdin_via_at_dash(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["issue_create.py", "@-"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(GH_MINIMAL)))
        monkeypatch.setattr(gh, "_gh", lambda args, timeout=20: _ok(GH_URL))

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "gh-issue-create OK" in out
