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

    def test_description_file_not_found(self, monkeypatch, capsys, tmp_path):
        payload = {
            "project": "fdavid/dvsi",
            "title": "Missing desc file",
            "description_file": str(tmp_path / "does_not_exist.md"),
        }
        payload_file = _write_payload(tmp_path, payload)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "description_file not found" in out
        assert "does_not_exist.md" in out
        assert "Traceback" not in out

    def test_description_file_is_directory(self, monkeypatch, capsys, tmp_path):
        desc_dir = tmp_path / "a_directory"
        desc_dir.mkdir()
        payload = {
            "project": "fdavid/dvsi",
            "title": "Dir desc file",
            "description_file": str(desc_dir),
        }
        payload_file = _write_payload(tmp_path, payload)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "description_file is a directory" in out
        assert str(desc_dir) in out
        assert "Traceback" not in out

    def test_description_file_permission_error(self, monkeypatch, capsys, tmp_path):
        desc_file = tmp_path / "locked_body.md"
        desc_file.write_text("secret")
        payload = {
            "project": "fdavid/dvsi",
            "title": "Locked desc file",
            "description_file": str(desc_file),
        }
        payload_file = _write_payload(tmp_path, payload)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        real_read_text = Path.read_text

        def _raiser(self, *a, **kw):
            if self.name == "locked_body.md":
                raise PermissionError(13, "Permission denied", str(self))
            return real_read_text(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", _raiser)

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "permission denied reading description_file" in out.lower()
        assert "is a directory" not in out
        assert "Traceback" not in out

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

    def test_no_arg_reports_missing_payload(self, monkeypatch, capsys):
        # supertool never actually omits argv[1] — see test_empty_arg below —
        # but a direct `python issue_create.py` invocation can.
        monkeypatch.setattr(sys, "argv", ["issue_create.py"])

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "gl-issue-create needs a payload" in out
        assert "gl-issue-create:@FILE" in out

    def test_empty_arg_reports_missing_payload_not_traceback(self, monkeypatch, capsys):
        # This is what `gl-issue-create` with no `@FILE` actually produces:
        # supertool's {arg} substitution always fills in *something*, so an
        # omitted argument arrives as an empty string, not a missing argv slot.
        monkeypatch.setattr(sys, "argv", ["issue_create.py", ""])

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "gl-issue-create needs a payload" in out
        assert "gl-issue-create:@FILE" in out
        assert "Traceback" not in out
        assert "IsADirectoryError" not in out

    def test_at_only_reports_missing_payload(self, monkeypatch, capsys):
        # "@" with nothing after it is the same empty-path shape via the
        # @FILE marker.
        monkeypatch.setattr(sys, "argv", ["issue_create.py", "@"])

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "gl-issue-create needs a payload" in out

    def test_directory_path_reports_directory_not_traceback(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(sys, "argv", ["issue_create.py", str(tmp_path)])

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "is a directory" in out
        assert str(tmp_path) in out
        assert "Traceback" not in out

    def test_directory_check_happens_before_any_read_attempt(self, monkeypatch, capsys, tmp_path):
        # The directory verdict must come from Path.is_dir(), not from
        # catching whichever OSError subtype a given platform happens to
        # raise on read (IsADirectoryError on POSIX, PermissionError on
        # Windows — see #627 review). Proven here by making a read attempt
        # itself a test failure: if the fix regresses to "try the read,
        # catch what falls out", this fires instead of silently relying on
        # exception-type-specific behaviour this suite can't exercise
        # cross-platform.
        def _must_not_be_called(path):
            raise AssertionError("_load_payload was called for a directory path")

        monkeypatch.setattr(gl, "_load_payload", _must_not_be_called)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", str(tmp_path)])

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "is a directory" in out

    def test_permission_error_on_a_file_is_not_reported_as_a_directory(self, monkeypatch, capsys, tmp_path):
        # A real permission failure (wrong ownership, locked file — also
        # PermissionError on POSIX, and the *only* thing a directory read
        # raises on Windows) must never be disclosed as "is a directory".
        # That would be a confidently wrong statement, worse than the
        # traceback #620/#627 replaced.
        payload_file = tmp_path / "locked.json"
        payload_file.write_text(json.dumps(GL_MINIMAL))
        monkeypatch.setattr(sys, "argv", ["issue_create.py", str(payload_file)])

        def _raise_permission_error(path):
            raise PermissionError(13, "Permission denied", path)

        monkeypatch.setattr(gl, "_load_payload", _raise_permission_error)

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "permission" in out.lower()
        assert "is a directory" not in out
        assert "Traceback" not in out

    def test_unparseable_payload_names_expected_shape(self, monkeypatch, capsys, tmp_path):
        bad = tmp_path / "notes.md"
        bad.write_text("# just some markdown\n\nnot a payload")
        monkeypatch.setattr(sys, "argv", ["issue_create.py", str(bad)])

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "failed to parse payload" in out
        assert "JSON or TOML" in out
        assert "title" in out


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
                    written_bodies.append(Path(args[i + 1]).read_text(encoding="utf-8"))
            return _ok(GH_URL)

        monkeypatch.setattr(gh, "_gh", fake_gh)

        rc = gh.main()
        assert rc == 0
        assert written_bodies
        assert "From file" in written_bodies[0]

    def test_body_file_not_found(self, monkeypatch, capsys, tmp_path):
        payload = {
            "repo": "Digital-Process-Tools/claude-supertool",
            "title": "Missing body file",
            "body_file": str(tmp_path / "does_not_exist.md"),
        }
        payload_file = _write_payload(tmp_path, payload)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "body_file not found" in out
        assert "does_not_exist.md" in out
        assert "Traceback" not in out

    def test_body_file_is_directory(self, monkeypatch, capsys, tmp_path):
        body_dir = tmp_path / "a_directory"
        body_dir.mkdir()
        payload = {
            "repo": "Digital-Process-Tools/claude-supertool",
            "title": "Dir body file",
            "body_file": str(body_dir),
        }
        payload_file = _write_payload(tmp_path, payload)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "body_file is a directory" in out
        assert str(body_dir) in out
        assert "Traceback" not in out

    def test_body_file_permission_error(self, monkeypatch, capsys, tmp_path):
        body_file = tmp_path / "locked_body.md"
        body_file.write_text("secret")
        payload = {
            "repo": "Digital-Process-Tools/claude-supertool",
            "title": "Locked body file",
            "body_file": str(body_file),
        }
        payload_file = _write_payload(tmp_path, payload)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        real_read_text = Path.read_text

        def _raiser(self, *a, **kw):
            if self.name == "locked_body.md":
                raise PermissionError(13, "Permission denied", str(self))
            return real_read_text(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", _raiser)

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "permission denied reading body_file" in out.lower()
        assert "is a directory" not in out
        assert "Traceback" not in out

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

    def test_no_arg_reports_missing_payload(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["issue_create.py"])

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "gh-issue-create needs a payload" in out
        assert "gh-issue-create:@FILE" in out

    def test_empty_arg_reports_missing_payload_not_traceback(self, monkeypatch, capsys):
        # This is the real defect from #620: `gh-issue-create` invoked with
        # no `@FILE` reaches main() with argv[1] == "" (supertool's {arg}
        # substitution always fills in something), and Path("").read_text()
        # resolves to Path(".") — IsADirectoryError, five-frame traceback.
        monkeypatch.setattr(sys, "argv", ["issue_create.py", ""])

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "gh-issue-create needs a payload" in out
        assert "gh-issue-create:@FILE" in out
        assert "Traceback" not in out
        assert "IsADirectoryError" not in out

    def test_at_only_reports_missing_payload(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["issue_create.py", "@"])

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "gh-issue-create needs a payload" in out

    def test_directory_path_reports_directory_not_traceback(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(sys, "argv", ["issue_create.py", str(tmp_path)])

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "is a directory" in out
        assert str(tmp_path) in out
        assert "Traceback" not in out

    def test_directory_check_happens_before_any_read_attempt(self, monkeypatch, capsys, tmp_path):
        # See the gitlab twin of this test for the full rationale: the
        # directory verdict must come from Path.is_dir(), not from catching
        # whichever OSError subtype a given platform raises on read
        # (IsADirectoryError on POSIX, PermissionError on Windows).
        def _must_not_be_called(path):
            raise AssertionError("_load_payload was called for a directory path")

        monkeypatch.setattr(gh, "_load_payload", _must_not_be_called)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", str(tmp_path)])

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "is a directory" in out

    def test_permission_error_on_a_file_is_not_reported_as_a_directory(self, monkeypatch, capsys, tmp_path):
        payload_file = tmp_path / "locked.json"
        payload_file.write_text(json.dumps(GH_MINIMAL))
        monkeypatch.setattr(sys, "argv", ["issue_create.py", str(payload_file)])

        def _raise_permission_error(path):
            raise PermissionError(13, "Permission denied", path)

        monkeypatch.setattr(gh, "_load_payload", _raise_permission_error)

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "permission" in out.lower()
        assert "is a directory" not in out
        assert "Traceback" not in out

    def test_unparseable_payload_names_expected_shape(self, monkeypatch, capsys, tmp_path):
        bad = tmp_path / "notes.md"
        bad.write_text("# just some markdown\n\nnot a payload")
        monkeypatch.setattr(sys, "argv", ["issue_create.py", str(bad)])

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "failed to parse payload" in out
        assert "JSON or TOML" in out
        assert "title" in out


# ===========================================================================
# #1790 — a GraphQL-mutation-path outage falls back to REST, named as such
# ===========================================================================

class TestGithubIssueCreateTransportFallback:
    """`gh issue create` goes through GraphQL. Observed 2026-08-17: that path
    can 503 while REST is healthy (#1790), and the fix is not "retry the same
    transport" -- it is a REST fallback that (1) names which transport
    answered, (2) never fires on an ordinary refusal, and (3) never files a
    duplicate if the earlier mutation actually landed despite the 503."""

    def test_graphql_success_names_its_transport(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, GH_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setattr(gh, "_gh", lambda args, timeout=20: _ok(GH_URL))

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "transport=graphql" in out

    def test_transport_503_falls_back_to_rest_and_names_it(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, GH_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])

        monkeypatch.setattr(
            gh, "_gh",
            lambda args, timeout=20: _err(
                "HTTP 503: No server is currently available to service your "
                "request. (https://api.github.com/graphql)"))

        rest_calls: list[list[str]] = []

        def fake_gh_json(args, stdin=None, timeout=30):
            rest_calls.append(args)
            if "POST" not in args:
                return ([], "")  # dedup lookup (GET): no open issue with this title
            if args[0] == "api" and "POST" in args:
                return ({"number": 99,
                          "html_url": "https://github.com/Digital-Process-Tools/"
                                       "claude-supertool/issues/99"}, "")
            raise AssertionError(f"unexpected _gh_json call: {args}")

        monkeypatch.setattr(gh, "_gh_json", fake_gh_json)

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "transport=rest" in out
        assert "number=99" in out
        assert rest_calls, "REST fallback never called _gh_json"
        # `gh api` defaults to POST the instant a `-f` parameter is present
        # (its own --help says so), so a lookup with no explicit `-X GET`
        # silently becomes a write against the wrong endpoint and 422s on a
        # real `gh` binary -- caught by review, not by a fake that only
        # checks the return value (#1790).
        dedup_call = rest_calls[0]
        assert "GET" in dedup_call, (
            f"the dedup lookup did not pin its HTTP method to GET: {dedup_call}")

    def test_ordinary_failure_never_triggers_rest_fallback(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, GH_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setattr(gh, "_gh", lambda args, timeout=20: _err("gh: not authenticated"))

        def _must_not_be_called(*a, **kw):
            raise AssertionError("_gh_json was called on an ordinary (non-transport) failure")

        monkeypatch.setattr(gh, "_gh_json", _must_not_be_called)

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "ERROR" in out
        assert "transport" not in out

    def test_dedup_guard_reuses_existing_open_issue_instead_of_reposting(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, GH_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setattr(
            gh, "_gh",
            lambda args, timeout=20: _err("HTTP 503: No server is currently "
                                           "available to service your request. "
                                           "(https://api.github.com/graphql)"))

        existing = {"number": 55, "title": GH_MINIMAL["title"],
                    "html_url": "https://github.com/Digital-Process-Tools/"
                                 "claude-supertool/issues/55"}

        def fake_gh_json(args, stdin=None, timeout=30):
            if "POST" not in args:
                return ([existing], "")
            raise AssertionError("a POST was attempted despite an existing match")

        monkeypatch.setattr(gh, "_gh_json", fake_gh_json)

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "number=55" in out
        assert "transport=rest" in out

    def test_dedup_lookup_failure_refuses_to_write_blind(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, GH_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setattr(
            gh, "_gh",
            lambda args, timeout=20: _err("HTTP 503: No server is currently "
                                           "available to service your request. "
                                           "(https://api.github.com/graphql)"))

        def fake_gh_json(args, stdin=None, timeout=30):
            if "POST" not in args:
                return (None, "gh timed out")
            raise AssertionError("a POST was attempted after a failed dedup lookup")

        monkeypatch.setattr(gh, "_gh_json", fake_gh_json)

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "ERROR" in out
        assert "duplicate" in out.lower()

    def test_dedup_full_first_page_pages_on_to_find_the_match(self, monkeypatch, capsys, tmp_path):
        """#2021: a full page (100 items) with no match on it is not proof
        there is no duplicate -- GitHub orders open issues newest-first, so
        the entries pushed off the first page are exactly the long-lived
        ones a stalled maintainer loop is most likely to be re-filing. The
        guard must page onward rather than treating a full page as `no
        match`. Would still pass if the code did nothing UNLESS paired with
        `test_dedup_partial_page_with_no_match_does_not_page_further` below,
        which proves a short page ends the loop instead of looping forever."""
        payload_file = _write_payload(tmp_path, GH_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setattr(
            gh, "_gh",
            lambda args, timeout=20: _err("HTTP 503: No server is currently "
                                           "available to service your request. "
                                           "(https://api.github.com/graphql)"))

        page_one = [{"number": n, "title": f"other issue {n}"} for n in range(100)]
        existing = {"number": 55, "title": GH_MINIMAL["title"],
                    "html_url": "https://github.com/Digital-Process-Tools/"
                                 "claude-supertool/issues/55"}
        page_two = [existing]

        def fake_gh_json(args, stdin=None, timeout=30):
            if "POST" in args:
                raise AssertionError(
                    "a POST was attempted despite an existing match on page 2")
            if "page=2" in args:
                return (page_two, "")
            return (page_one, "")

        monkeypatch.setattr(gh, "_gh_json", fake_gh_json)

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "number=55" in out
        assert "transport=rest" in out

    def test_dedup_partial_page_with_no_match_does_not_page_further(self, monkeypatch, capsys, tmp_path):
        """The counterpart to the test above: a page shorter than per_page
        (100) is proof there is no next page, so the guard must proceed to
        file rather than issuing a second request it does not need."""
        payload_file = _write_payload(tmp_path, GH_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setattr(
            gh, "_gh",
            lambda args, timeout=20: _err("HTTP 503: No server is currently "
                                           "available to service your request. "
                                           "(https://api.github.com/graphql)"))

        calls: list[list[str]] = []

        def fake_gh_json(args, stdin=None, timeout=30):
            calls.append(args)
            if "POST" not in args:
                return ([{"number": 1, "title": "unrelated"}], "")  # partial page
            return ({"number": 99,
                      "html_url": "https://github.com/Digital-Process-Tools/"
                                   "claude-supertool/issues/99"}, "")

        monkeypatch.setattr(gh, "_gh_json", fake_gh_json)

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "number=99" in out
        get_calls = [c for c in calls if "POST" not in c]
        assert len(get_calls) == 1, (
            f"a partial page should end the pagination loop, got "
            f"{len(get_calls)} lookup calls")

    def test_dedup_lookup_refuses_rather_than_paging_forever(self, monkeypatch, capsys, tmp_path):
        """A pathological repo (or a broken response) that returns a full
        page every time must not loop forever or eventually POST blind --
        it refuses the same way an outright `_gh_json` error does."""
        payload_file = _write_payload(tmp_path, GH_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setattr(
            gh, "_gh",
            lambda args, timeout=20: _err("HTTP 503: No server is currently "
                                           "available to service your request. "
                                           "(https://api.github.com/graphql)"))

        always_full_page = [{"number": n, "title": f"other issue {n}"} for n in range(100)]

        def fake_gh_json(args, stdin=None, timeout=30):
            if "POST" in args:
                raise AssertionError(
                    "a POST was attempted after the lookup should have refused")
            return (always_full_page, "")

        monkeypatch.setattr(gh, "_gh_json", fake_gh_json)

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "ERROR" in out
        assert "duplicate" in out.lower() or "could not" in out.lower()

    def test_unresolved_milestone_is_named_not_applied_on_rest_fallback(self, monkeypatch, capsys, tmp_path):
        payload = dict(GH_MINIMAL)
        payload["milestone"] = "v9.9"
        payload_file = _write_payload(tmp_path, payload)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setattr(
            gh, "_gh",
            lambda args, timeout=20: _err("HTTP 503: No server is currently "
                                           "available to service your request. "
                                           "(https://api.github.com/graphql)"))

        def fake_gh_json(args, stdin=None, timeout=30):
            joined = " ".join(args)
            if "POST" not in args and "milestones" in joined:
                return ([], "")  # no milestone named v9.9
            if "POST" not in args:
                return ([], "")  # dedup lookup: nothing open with this title
            if "POST" in args:
                return ({"number": 12,
                          "html_url": "https://github.com/Digital-Process-Tools/"
                                       "claude-supertool/issues/12"}, "")
            raise AssertionError(f"unexpected _gh_json call: {args}")

        monkeypatch.setattr(gh, "_gh_json", fake_gh_json)

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "NOT APPLIED" in out
        assert "v9.9" in out

    def test_resolved_milestone_is_sent_as_its_rest_number(self, monkeypatch, capsys, tmp_path):
        payload = dict(GH_MINIMAL)
        payload["milestone"] = "v1.0"
        payload_file = _write_payload(tmp_path, payload)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setattr(
            gh, "_gh",
            lambda args, timeout=20: _err("HTTP 503: No server is currently "
                                           "available to service your request. "
                                           "(https://api.github.com/graphql)"))

        post_fields: list[dict] = []

        def fake_gh_json(args, stdin=None, timeout=30):
            joined = " ".join(args)
            if "POST" not in args and "milestones" in joined:
                return ([{"title": "v1.0", "number": 7}], "")
            if "POST" not in args:
                return ([], "")  # dedup lookup: nothing open with this title
            if "POST" in args:
                post_fields.append(json.loads(stdin))
                return ({"number": 21,
                          "html_url": "https://github.com/Digital-Process-Tools/"
                                       "claude-supertool/issues/21"}, "")
            raise AssertionError(f"unexpected _gh_json call: {args}")

        monkeypatch.setattr(gh, "_gh_json", fake_gh_json)

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "NOT APPLIED" not in out
        assert post_fields, "the REST POST never happened"
        assert post_fields[0].get("milestone") == 7

    def test_rest_fallback_write_failure_reports_nothing_written(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, GH_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setattr(
            gh, "_gh",
            lambda args, timeout=20: _err("HTTP 503: No server is currently "
                                           "available to service your request. "
                                           "(https://api.github.com/graphql)"))

        def fake_gh_json(args, stdin=None, timeout=30):
            if "POST" not in args:
                return ([], "")  # dedup lookup: nothing open with this title
            return (None, "422 Unprocessable Entity")

        monkeypatch.setattr(gh, "_gh_json", fake_gh_json)

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc != 0
        assert "ERROR" in out
        assert "nothing was written" in out.lower()


# ===========================================================================
# #1909 — repo: reconciled against the payload's own repo field
# ===========================================================================

class TestGithubIssueCreateRepoTarget:
    """`main()`'s own reconciliation of `SUPERTOOL_REPO` against `payload["repo"]`
    -- the counterpart to `tests/test_repo_target_673.py`'s pre-pass tests,
    which stop at "the call was not refused" and never dispatch this far."""

    def test_repo_op_supplies_a_silent_payload(self, monkeypatch, capsys, tmp_path):
        """target set, payload silent -> the target wins, stated with its
        source in the receipt."""
        payload = {"title": "No repo in payload", "body": "x"}
        payload_file = _write_payload(tmp_path, payload)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setenv("SUPERTOOL_REPO", "owner/from-repo-op")
        monkeypatch.setenv("SUPERTOOL_REPO_FROM_OP", "1")

        captured: list[list[str]] = []
        monkeypatch.setattr(gh, "_gh", lambda args, timeout=20: captured.append(args) or _ok(GH_URL))

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert _flag_value(captured[0], "--repo") == "owner/from-repo-op"
        assert "repo from repo: op" in out

    def test_repo_op_and_agreeing_payload_proceed(self, monkeypatch, capsys, tmp_path):
        """Both present and agreeing is not ambiguous -- it proceeds."""
        payload_file = _write_payload(tmp_path, GH_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setenv("SUPERTOOL_REPO", GH_MINIMAL["repo"])
        monkeypatch.setenv("SUPERTOOL_REPO_FROM_OP", "1")

        captured: list[list[str]] = []
        monkeypatch.setattr(gh, "_gh", lambda args, timeout=20: captured.append(args) or _ok(GH_URL))

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert _flag_value(captured[0], "--repo") == GH_MINIMAL["repo"]

    def test_repo_op_and_disagreeing_payload_refuse(self, monkeypatch, capsys, tmp_path):
        """Both present and disagreeing must refuse, naming both values and
        never guessing which wins -- the one arm that proves the check is
        for real. Would still pass if the code did nothing UNLESS paired
        with the agreeing case above, which proceeds."""
        payload_file = _write_payload(tmp_path, GH_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setenv("SUPERTOOL_REPO", "owner/somewhere-else")
        monkeypatch.setenv("SUPERTOOL_REPO_FROM_OP", "1")

        called: list[list[str]] = []
        monkeypatch.setattr(gh, "_gh", lambda args, timeout=20: called.append(args) or _ok(GH_URL))

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc == 1
        assert called == [], "gh was invoked despite the disagreement"
        assert GH_MINIMAL["repo"] in out
        assert "owner/somewhere-else" in out
        assert "gh-issue-create" in out


class TestGitlabIssueCreateRepoTarget:
    """The GitLab twin: `gl-issue-create` reconciles `SUPERTOOL_REPO` against
    `payload["project"]`, using GitLab's own key name (#1909)."""

    def test_repo_op_supplies_a_silent_payload(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, {"title": "No project", "description": "x"})
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setenv("SUPERTOOL_REPO", "group/from-repo-op")
        monkeypatch.setenv("SUPERTOOL_REPO_FROM_OP", "1")

        captured: list[list[str]] = []
        monkeypatch.setattr(gl, "_glab", lambda args, timeout=20: captured.append(args) or _ok(GL_URL))
        monkeypatch.setattr(gl, "_glab_api", lambda *a, **kw: _ok("{}"))

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert _flag_value(captured[0], "--repo") == "group/from-repo-op"
        assert "project from repo: op" in out

    def test_repo_op_and_agreeing_payload_proceed(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, GL_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setenv("SUPERTOOL_REPO", GL_MINIMAL["project"])
        monkeypatch.setenv("SUPERTOOL_REPO_FROM_OP", "1")

        captured: list[list[str]] = []
        monkeypatch.setattr(gl, "_glab", lambda args, timeout=20: captured.append(args) or _ok(GL_URL))
        monkeypatch.setattr(gl, "_glab_api", lambda *a, **kw: _ok("{}"))

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert _flag_value(captured[0], "--repo") == GL_MINIMAL["project"]

    def test_repo_op_and_disagreeing_payload_refuse(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, GL_MINIMAL)
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setenv("SUPERTOOL_REPO", "group/somewhere-else")
        monkeypatch.setenv("SUPERTOOL_REPO_FROM_OP", "1")

        called: list[list[str]] = []
        monkeypatch.setattr(gl, "_glab", lambda args, timeout=20: called.append(args) or _ok(GL_URL))
        monkeypatch.setattr(gl, "_glab_api", lambda *a, **kw: _ok("{}"))

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc == 1
        assert called == [], "glab was invoked despite the disagreement"
        assert GL_MINIMAL["project"] in out
        assert "group/somewhere-else" in out
        assert "gl-issue-create" in out


class TestGitlabIssueCreateAmbientRepoEnv:
    """The GitLab twin of `TestGithubIssueCreateAmbientRepoEnv` below, added
    while closing #1990/#1993: `gl-issue-create` reconciles SUPERTOOL_REPO
    through the identical `resolve_or_conflict` call `gh-issue-create` does,
    but until now only the GitHub side had an end-to-end pin -- exactly the
    asymmetry #1990 warns would let a future regression in one of the other
    three payload-mode write ops keep the shared unit test green."""

    def test_ambient_repo_with_no_marker_is_ignored_end_to_end(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, {"title": "No project in payload", "description": "x"})
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setenv("SUPERTOOL_REPO", "someone-else/their-project")
        monkeypatch.delenv("SUPERTOOL_REPO_FROM_OP", raising=False)
        # Deterministic fallback so this test does not depend on this
        # worktree's actual git remote / .supertool.json default.
        monkeypatch.setattr(gl._rd, "resolve", lambda *a, **k: "fallback/project")

        captured: list[list[str]] = []
        monkeypatch.setattr(gl, "_glab", lambda args, timeout=20: captured.append(args) or _ok(GL_URL))
        monkeypatch.setattr(gl, "_glab_api", lambda *a, **kw: _ok("{}"))

        rc = gl.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert _flag_value(captured[0], "--repo") == "fallback/project"
        assert "someone-else/their-project" not in out


class TestGithubIssueCreateAmbientRepoEnv:
    """End-to-end (#1986): an ambient SUPERTOOL_REPO with no repo: op in
    this call must not direct the write, all the way through main() -- not
    just at the resolve_or_conflict() unit tested directly in
    tests/test_repo_target_673.py. A regression specific to how this module
    threads resolve_or_conflict's return value would not be caught there."""

    def test_ambient_repo_with_no_marker_is_ignored_end_to_end(self, monkeypatch, capsys, tmp_path):
        payload_file = _write_payload(tmp_path, {"title": "No repo in payload", "body": "x"})
        monkeypatch.setattr(sys, "argv", ["issue_create.py", payload_file])
        monkeypatch.setenv("SUPERTOOL_REPO", "someone-else/their-repo")
        monkeypatch.delenv("SUPERTOOL_REPO_FROM_OP", raising=False)
        # Deterministic fallback so this test does not depend on this
        # worktree's actual git remote.
        monkeypatch.setattr(gh._rd, "resolve", lambda *a, **k: "fallback/repo")

        captured: list[list[str]] = []
        monkeypatch.setattr(gh, "_gh", lambda args, timeout=20: captured.append(args) or _ok(GH_URL))

        rc = gh.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert _flag_value(captured[0], "--repo") == "fallback/repo"
        assert "someone-else/their-repo" not in out
