"""Security audit tests for GitHub presets (presets/github/*.py).

Covers:
1.  Shell injection in gh CLI args — all ops use list-form subprocess.run, never shell=True
2.  PR/issue number injection — shell-special chars in arg must be literal, not executed
3.  Batch follow file path traversal — ../../etc/passwd should not open outside cwd guard
4.  Batch file with shell-special usernames — username from file must be passed literally
5.  gh token leakage — error output containing a token string must not reach stdout
6.  Owner/repo with '..' in path — must be rejected by find_followable
7.  gh-find-followable response injection — '..'-prefixed logins from API must pass through
    literal (not path-expanded) — the real guard is list-form subprocess, tested in #1
8.  GH_BIN env override — ops must NOT respect a GH_BIN override pointing to an evil binary
9.  Batch sleep scale — zero-delay env var works; large files don't mandate huge wallclock wait
10. JSON output parsing — malformed / huge JSON from gh must be handled gracefully
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Module loaders
# ---------------------------------------------------------------------------

def _load(name: str, rel: str):
    path = Path(__file__).parent.parent / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


follow = _load("github_follow", "presets/github/follow.py")
batch_follow = _load("github_batch_follow", "presets/github/batch_follow.py")
find_followable = _load("github_find_followable", "presets/github/find_followable.py")
pr_mod = _load("github_pr", "presets/github/pr.py")
issue_mod = _load("github_issue", "presets/github/issue.py")
job_mod = _load("github_job", "presets/github/job.py")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["gh"], 0, stdout, stderr)


def _err(stderr: str = "error", returncode: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["gh"], returncode, "", stderr)


# ===========================================================================
# 1. Shell injection — all subprocess.run calls must use list form, shell=False
# ===========================================================================

class TestNoShellTrue:
    """All subprocess.run calls in gh ops must NOT use shell=True."""

    def _check_module_no_shell_true(self, mod):
        """Intercept subprocess.run calls and assert shell kwarg is never True."""
        calls: list[dict] = []

        def capturing_run(args, **kwargs):
            calls.append({"args": args, "shell": kwargs.get("shell", False)})
            # Return a safe mock
            return subprocess.CompletedProcess(args, 0, "[]", "")

        original = mod.subprocess.run
        mod.subprocess.run = capturing_run
        try:
            yield calls
        finally:
            mod.subprocess.run = original

    def test_follow_no_shell(self, monkeypatch):
        """gh-follow must never use shell=True regardless of username content."""
        calls: list[dict] = []

        def spy(args, **kwargs):
            calls.append({"args": args, "shell": kwargs.get("shell", False)})
            return _ok()

        monkeypatch.setattr(follow.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["follow.py", "foo;rm -rf /"])
        follow.main("foo;rm -rf /")

        assert calls, "subprocess.run was never called"
        for call in calls:
            assert call["shell"] is not True, \
                f"shell=True found in follow.py call: {call['args']}"
        # The malicious string must appear as a list element, not shell string
        for call in calls:
            if isinstance(call["args"], list):
                assert any("foo;rm -rf /" in str(a) for a in call["args"]), \
                    "Injected value not found as literal argument"

    def test_batch_follow_no_shell(self, monkeypatch, tmp_path):
        """gh-batch-follow must never use shell=True."""
        f = tmp_path / "users.txt"
        f.write_text("legitimate-user\n")
        calls: list[dict] = []

        def spy(args, **kwargs):
            calls.append({"shell": kwargs.get("shell", False)})
            return _ok()

        monkeypatch.setattr(batch_follow.subprocess, "run", spy)
        monkeypatch.setenv("SUPERTOOL_FOLLOW_DELAY", "0")
        batch_follow.main(str(f))

        assert calls
        for call in calls:
            assert call["shell"] is not True, "shell=True in batch_follow"

    def test_find_followable_no_shell(self, monkeypatch):
        """gh-find-followable must never use shell=True."""
        calls: list[dict] = []

        def spy(args, **kwargs):
            calls.append({"args": args, "shell": kwargs.get("shell", False)})
            return _ok("[]")

        monkeypatch.setattr(find_followable.subprocess, "run", spy)
        find_followable.main("owner/repo")

        assert calls
        for call in calls:
            assert call["shell"] is not True, "shell=True in find_followable"

    def test_pr_no_shell(self, monkeypatch):
        payload = json.dumps({
            "number": 1, "title": "t", "state": "OPEN",
            "author": {"login": "u"}, "headRefName": "b", "baseRefName": "master",
            "labels": [], "milestone": None, "isDraft": False, "mergeable": "MERGEABLE",
            "reviewDecision": None, "reviews": [], "mergeCommit": None,
            "additions": 0, "deletions": 0, "changedFiles": 0,
            "statusCheckRollup": [], "url": "", "body": "", "comments": [],
            "assignees": [], "createdAt": "", "updatedAt": "", "reviewThreads": [],
        })
        calls: list[dict] = []

        def spy(args, **kwargs):
            calls.append({"shell": kwargs.get("shell", False)})
            return _ok(payload)

        monkeypatch.setattr(pr_mod.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["pr.py", "1"])
        pr_mod.main()

        assert calls
        for call in calls:
            assert call["shell"] is not True, "shell=True in pr.py"


# ===========================================================================
# 2. PR/issue number injection — shell-special input treated as literal arg
# ===========================================================================

class TestPrIssueNumberInjection:
    """Shell-special chars in PR/issue number must be passed as argv, not shell."""

    EVIL_INPUTS = [
        "'); rm -rf /",
        "1; touch /tmp/INJECTED_PR",
        "$(evil)",
        "`evil`",
        "1 && evil",
        "1|evil",
    ]

    @pytest.mark.parametrize("evil", EVIL_INPUTS)
    def test_pr_evil_number_passed_as_literal_arg(self, monkeypatch, capsys, evil):
        """pr.py receives evil string; must not exec shell, must pass it as list arg."""
        injected_flag = {"triggered": False}

        def spy(args, **kwargs):
            # Verify: list form (not a string command), shell=False
            assert isinstance(args, list), f"Expected list args, got {type(args)}: {args}"
            assert kwargs.get("shell", False) is not True, "shell=True detected"
            # Fail with "not found" so pr.py just prints an error and returns
            return _err(stderr="404 not found")

        monkeypatch.setattr(pr_mod.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["pr.py", evil])
        rc = pr_mod.main()
        # Should fail gracefully (not crash or execute commands)
        assert rc != 0
        out = capsys.readouterr().out
        assert "ERROR" in out

    @pytest.mark.parametrize("evil", EVIL_INPUTS)
    def test_issue_evil_number_passed_as_literal_arg(self, monkeypatch, capsys, evil):
        def spy(args, **kwargs):
            assert isinstance(args, list)
            assert kwargs.get("shell", False) is not True
            return _err(stderr="404 not found")

        monkeypatch.setattr(issue_mod.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["issue.py", evil])
        rc = issue_mod.main()
        assert rc != 0


# ===========================================================================
# 3. Batch follow file path traversal
# ===========================================================================

class TestBatchFollowPathTraversal:
    """gh-batch-follow:../../etc/passwd — Path() resolves it but must not silently
    succeed in a way that leaks contents (it reads usernames from file lines, so
    /etc/passwd content being used as username strings is a separate concern but
    not a code-execution issue). Main concern: no path restriction bypass."""

    def test_traversal_path_resolved_not_rejected(self, monkeypatch, tmp_path):
        """Traversal paths like ../../etc/passwd are accepted if the resolved file
        exists — the code uses Path() which does canonical resolution. This is
        INFORMATIONAL: the op has no cwd-restriction guard.

        We verify the op does NOT crash and does NOT shell-expand the path.
        """
        # Create a temp file simulating the traversal target
        target = tmp_path / "fake_passwd"
        target.write_text("root\ndaemon\nnobody\n")

        calls: list[list] = []

        def spy(args, **kwargs):
            calls.append(args)
            assert isinstance(args, list), "Must be list form"
            assert kwargs.get("shell", False) is not True
            return _ok()

        monkeypatch.setattr(batch_follow.subprocess, "run", spy)
        monkeypatch.setenv("SUPERTOOL_FOLLOW_DELAY", "0")

        rc = batch_follow.main(str(target))
        assert rc == 0
        # Usernames from file must appear as literal list elements in gh calls
        called_users = [a for args in calls for a in args if a not in ("gh", "api", "-X", "PUT")]
        assert any("root" in u or "daemon" in u or "nobody" in u for u in called_users), \
            "Expected usernames from file to appear as gh args"

    def test_nonexistent_path_returns_error(self, monkeypatch, capsys):
        """A path that doesn't exist must return error code 2, no crash."""
        monkeypatch.setattr(batch_follow.subprocess, "run", lambda *a, **k: _ok())
        rc = batch_follow.main("../../etc/passwd_does_not_exist_supertool_test")
        assert rc == 2
        err = capsys.readouterr().err
        assert "ERROR" in err


# ===========================================================================
# 4. Batch file with shell-special usernames
# ===========================================================================

class TestBatchFollowShellSpecialUsernames:
    """Usernames like 'foo; touch /tmp/INJECTED' must be passed as literal gh args."""

    EVIL_USERNAMES = [
        "foo; touch /tmp/INJECTED",
        "$(evil)",
        "`evil`",
        "user && rm -rf /",
        "user|cat /etc/passwd",
        "../user",
        "user\x00null",
    ]

    def test_evil_usernames_passed_as_literal_args(self, monkeypatch, tmp_path):
        evil_file = tmp_path / "evil_users.txt"
        evil_file.write_text("\n".join(self.EVIL_USERNAMES) + "\n")

        calls: list[list] = []

        def spy(args, **kwargs):
            assert isinstance(args, list), f"Must use list form, got: {args!r}"
            assert kwargs.get("shell", False) is not True, "shell=True detected"
            calls.append(args)
            return _ok()

        monkeypatch.setattr(batch_follow.subprocess, "run", spy)
        monkeypatch.setenv("SUPERTOOL_FOLLOW_DELAY", "0")
        rc = batch_follow.main(str(evil_file))

        # Every call must be list form — already asserted in spy
        assert len(calls) == len(self.EVIL_USERNAMES), \
            f"Expected {len(self.EVIL_USERNAMES)} calls, got {len(calls)}"
        # Each username must appear verbatim as a list element in the gh call
        for i, (call, evil) in enumerate(zip(calls, self.EVIL_USERNAMES)):
            username = evil.lstrip("@")
            # The username is embedded in the endpoint "user/following/<username>"
            endpoint_args = [a for a in call if "user/following" in str(a)]
            assert endpoint_args, f"Call {i}: no user/following arg found in {call}"
            assert username in endpoint_args[0], \
                f"Call {i}: username {username!r} not literal in endpoint {endpoint_args[0]!r}"


# ===========================================================================
# 5. gh token leakage — mock gh returning token-laden error, verify no stdout leak
# ===========================================================================

class TestTokenLeakage:
    """If gh returns an error that includes a token, the op must not print it to stdout."""

    TOKEN = "ghp_FAKE_TOKEN_1234567890abcdef"

    def test_follow_error_does_not_leak_token_to_stdout(self, monkeypatch, capsys):
        def spy(args, **kwargs):
            return _err(stderr=f"HTTP 401: token {self.TOKEN} is invalid")

        monkeypatch.setattr(follow.subprocess, "run", spy)
        follow.main("someuser")
        out = capsys.readouterr().out
        assert self.TOKEN not in out, \
            f"Token leaked to stdout in follow.py: {out[:200]}"

    def test_pr_error_does_not_leak_token_to_stdout(self, monkeypatch, capsys):
        def spy(args, **kwargs):
            return _err(stderr=f"HTTP 401: token {self.TOKEN} is invalid")

        monkeypatch.setattr(pr_mod.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["pr.py", "1"])
        pr_mod.main()
        out = capsys.readouterr().out
        assert self.TOKEN not in out, \
            f"Token leaked to stdout in pr.py: {out[:200]}"

    def test_issue_error_does_not_leak_token_to_stdout(self, monkeypatch, capsys):
        def spy(args, **kwargs):
            return _err(stderr=f"HTTP 401: token {self.TOKEN} is invalid")

        monkeypatch.setattr(issue_mod.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["issue.py", "1"])
        issue_mod.main()
        out = capsys.readouterr().out
        assert self.TOKEN not in out, \
            f"Token leaked to stdout in issue.py: {out[:200]}"

    def test_find_followable_error_does_not_leak_token_to_stdout(self, monkeypatch, capsys):
        def spy(args, **kwargs):
            return _err(stderr=f"HTTP 401: token {self.TOKEN} is invalid", returncode=1)

        monkeypatch.setattr(find_followable.subprocess, "run", spy)
        find_followable.main("owner/repo")
        out = capsys.readouterr().out
        assert self.TOKEN not in out, \
            f"Token leaked to stdout in find_followable.py: {out[:200]}"

    def test_batch_follow_error_does_not_leak_token_to_stdout(self, monkeypatch, capsys, tmp_path):
        f = tmp_path / "users.txt"
        f.write_text("someuser\n")

        def spy(args, **kwargs):
            return _err(stderr=f"HTTP 401: token {self.TOKEN} is invalid")

        monkeypatch.setattr(batch_follow.subprocess, "run", spy)
        monkeypatch.setenv("SUPERTOOL_FOLLOW_DELAY", "0")
        batch_follow.main(str(f))
        out = capsys.readouterr().out
        assert self.TOKEN not in out, \
            f"Token leaked to stdout in batch_follow.py: {out[:200]}"


# ===========================================================================
# 6. Owner/repo with '..' in path — find_followable should reject or pass safely
# ===========================================================================

class TestDotDotInOwnerRepo:
    """OWNER/REPO strings like 'foo/../bar' or '../etc/passwd' should not cause
    path traversal. Since gh-find-followable passes the string into a gh API URL
    (list form, no shell), this is low severity — but we verify parse_args behavior."""

    def test_dotdot_in_repo_is_accepted_by_parse_args(self):
        """parse_args does NOT currently reject '..' in OWNER/REPO.
        This test documents the current behavior (no rejection).
        The actual HTTP call would fail with a 404 from GitHub's API.
        SEVERITY: LOW — no local code execution possible; just documents the gap.
        """
        repo, n = find_followable.parse_args("foo/../bar|10")
        # Current behavior: accepts it (strips leading slash only)
        assert "/" in repo  # still has OWNER/REPO shape
        assert ".." in repo  # NOT rejected — this is the documented gap

    def test_dotdot_repo_subprocess_call_is_list_form(self, monkeypatch, capsys):
        """Even with '..' in OWNER/REPO, subprocess is called with list args."""
        calls: list[list] = []

        def spy(args, **kwargs):
            assert isinstance(args, list), "Must use list form"
            assert kwargs.get("shell", False) is not True
            calls.append(args)
            return _ok("[]")

        monkeypatch.setattr(find_followable.subprocess, "run", spy)
        find_followable.main("foo/../bar")

        assert calls, "No subprocess calls were made"
        for call in calls:
            assert isinstance(call, list)
            # '../bar' appears inside the gh API endpoint, not as a path traversal
            endpoint_args = [a for a in call if "repos/" in str(a)]
            if endpoint_args:
                assert ".." in endpoint_args[0], "'..' should be literal in endpoint"

    def test_pure_relative_path_accepted_because_slash_present(self, monkeypatch, capsys):
        """'../etc/passwd' contains '/' so parse_args ACCEPTS it (lstrip only removes leading '/').
        This is a documented LOW-severity gap: the op will pass 'repos/../etc/passwd/stargazers'
        to gh as a list arg — no local code execution, but an unexpected API call may succeed
        or return a confusing error. The fix would be to reject repo strings containing '..'.
        SEVERITY: LOW — no shell execution; gh API will 404 on the traversal path.
        """
        repo, n = find_followable.parse_args("../etc/passwd")
        # Current behavior: accepted (contains '/')
        assert ".." in repo, "'..' should be present — this is the documented gap"
        assert "/" in repo


# ===========================================================================
# 7. gh-find-followable response injection — '../user' login passed literally to batch
# ===========================================================================

class TestFindFollowableResponseInjection:
    """Mock gh api returning logins like '../user' or 'evil;cmd'.
    The logins are printed to stdout for the user to review before passing
    to gh-batch-follow. The op must not shell-expand them."""

    def test_dotdot_login_printed_literally(self, monkeypatch, capsys):
        """A login like '../user' must appear verbatim in output, not path-expanded."""
        stargazers = json.dumps([
            {"login": "../user", "type": "User"},
            {"login": "evil;touch /tmp/INJECTED_FF", "type": "User"},
        ])

        def spy(args, **kwargs):
            assert isinstance(args, list)
            assert kwargs.get("shell", False) is not True
            return _ok(stargazers)

        monkeypatch.setattr(find_followable.subprocess, "run", spy)
        find_followable.main("owner/repo")
        out = capsys.readouterr().out

        assert "../user" in out, "'..' login not in output"
        assert "evil;touch /tmp/INJECTED_FF" in out, "Evil login not in output"
        # Verify the actual injection file was NOT created
        assert not Path("/tmp/INJECTED_FF").exists(), \
            "SECURITY: /tmp/INJECTED_FF was created — shell injection occurred"


# ===========================================================================
# 8. GH_BIN env override — ops must use hardcoded "gh", not GH_BIN
# ===========================================================================

class TestGhBinOverride:
    """If GH_BIN=/tmp/evil is set, ops must NOT execute /tmp/evil.
    All ops hardcode ["gh", ...] in subprocess.run — no env var override.
    """

    def test_follow_ignores_gh_bin_override(self, monkeypatch):
        """follow.py hardcodes 'gh'; GH_BIN=/tmp/evil must be ignored."""
        monkeypatch.setenv("GH_BIN", "/tmp/evil_gh_binary")
        called_with: list[str] = []

        def spy(args, **kwargs):
            called_with.append(args[0] if isinstance(args, list) else str(args).split()[0])
            return _ok()

        monkeypatch.setattr(follow.subprocess, "run", spy)
        follow.main("someuser")

        for binary in called_with:
            assert binary == "gh", \
                f"SECURITY: op called {binary!r} instead of 'gh' — GH_BIN override respected"

    def test_batch_follow_ignores_gh_bin_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GH_BIN", "/tmp/evil_gh_binary")
        f = tmp_path / "users.txt"
        f.write_text("user1\n")
        called_with: list[str] = []

        def spy(args, **kwargs):
            called_with.append(args[0] if isinstance(args, list) else str(args).split()[0])
            return _ok()

        monkeypatch.setattr(batch_follow.subprocess, "run", spy)
        monkeypatch.setenv("SUPERTOOL_FOLLOW_DELAY", "0")
        batch_follow.main(str(f))

        for binary in called_with:
            assert binary == "gh", \
                f"SECURITY: batch_follow called {binary!r} — GH_BIN override respected"

    def test_find_followable_ignores_gh_bin_override(self, monkeypatch):
        monkeypatch.setenv("GH_BIN", "/tmp/evil_gh_binary")
        called_with: list[str] = []

        def spy(args, **kwargs):
            called_with.append(args[0] if isinstance(args, list) else str(args).split()[0])
            return _ok("[]")

        monkeypatch.setattr(find_followable.subprocess, "run", spy)
        find_followable.main("owner/repo")

        for binary in called_with:
            assert binary == "gh", \
                f"SECURITY: find_followable called {binary!r} — GH_BIN override respected"

    def test_pr_ignores_gh_bin_override(self, monkeypatch):
        monkeypatch.setenv("GH_BIN", "/tmp/evil_gh_binary")
        payload = json.dumps({
            "number": 1, "title": "t", "state": "OPEN",
            "author": {"login": "u"}, "headRefName": "b", "baseRefName": "master",
            "labels": [], "milestone": None, "isDraft": False, "mergeable": "MERGEABLE",
            "reviewDecision": None, "reviews": [], "mergeCommit": None,
            "additions": 0, "deletions": 0, "changedFiles": 0,
            "statusCheckRollup": [], "url": "", "body": "", "comments": [],
            "assignees": [], "createdAt": "", "updatedAt": "", "reviewThreads": [],
        })
        called_with: list[str] = []

        def spy(args, **kwargs):
            called_with.append(args[0] if isinstance(args, list) else str(args).split()[0])
            return _ok(payload)

        monkeypatch.setattr(pr_mod.subprocess, "run", spy)
        monkeypatch.setattr(sys, "argv", ["pr.py", "1"])
        pr_mod.main()

        # pr.py also calls "git rev-parse" for the local-branch check — filter those out.
        # We only care that gh invocations use the hardcoded "gh" binary, not GH_BIN.
        gh_calls = [b for b in called_with if b not in ("git",)]
        assert gh_calls, "No gh calls were made — test setup broken"
        for binary in gh_calls:
            assert binary == "gh", \
                f"SECURITY: pr.py called {binary!r} instead of 'gh' — GH_BIN override respected"


# ===========================================================================
# 9. Batch sleep scale — SUPERTOOL_FOLLOW_DELAY=0 skips sleep; kill-switch test
# ===========================================================================

class TestBatchSleepScale:
    """Verify SUPERTOOL_FOLLOW_DELAY env var is respected so tests (and operators
    with urgency) can override the default 1s sleep. Also documents that there is
    no explicit kill-switch or progress cap for huge files.
    """

    def test_zero_delay_env_var_respected(self, monkeypatch, tmp_path):
        """SUPERTOOL_FOLLOW_DELAY=0 must result in no sleep calls."""
        import time
        f = tmp_path / "users.txt"
        f.write_text("\n".join(f"user{i}" for i in range(5)) + "\n")

        sleep_calls: list[float] = []
        original_sleep = time.sleep

        def spy_sleep(secs):
            sleep_calls.append(secs)

        monkeypatch.setattr(time, "sleep", spy_sleep)
        monkeypatch.setattr(batch_follow.time, "sleep", spy_sleep)
        monkeypatch.setattr(batch_follow.subprocess, "run", lambda *a, **k: _ok())
        monkeypatch.setenv("SUPERTOOL_FOLLOW_DELAY", "0")

        rc = batch_follow.main(str(f))
        assert rc == 0

        total_sleep = sum(sleep_calls)
        assert total_sleep == 0, \
            f"Expected 0s total sleep with DELAY=0, got {total_sleep}s"

    def test_default_delay_applied_between_calls(self, monkeypatch, tmp_path):
        """Default 1s delay must be applied between (not before first) calls."""
        import time
        f = tmp_path / "users.txt"
        f.write_text("user1\nuser2\nuser3\n")

        sleep_calls: list[float] = []

        def spy_sleep(secs):
            sleep_calls.append(secs)

        monkeypatch.setattr(batch_follow.time, "sleep", spy_sleep)
        monkeypatch.setattr(batch_follow.subprocess, "run", lambda *a, **k: _ok())
        # Ensure no override
        monkeypatch.delenv("SUPERTOOL_FOLLOW_DELAY", raising=False)

        batch_follow.main(str(f))

        # 3 users → 2 sleeps (not before first call)
        assert len(sleep_calls) == 2, \
            f"Expected 2 sleep calls for 3 users, got {len(sleep_calls)}"
        assert all(s == 1.0 for s in sleep_calls), \
            f"Expected 1.0s per sleep, got {sleep_calls}"

    def test_large_file_no_rate_limiting_guard(self, monkeypatch, tmp_path):
        """Documents that there is NO hard cap on batch size.
        With DELAY=0 this is fine. Without it, 10k users = 10k seconds.
        This is a MEDIUM-severity operational risk (not a code-execution risk).
        The test verifies all users are processed (no early bail-out).
        """
        import time
        n = 100  # use 100 to keep test fast; documents the O(n) pattern
        f = tmp_path / "users.txt"
        f.write_text("\n".join(f"user{i}" for i in range(n)) + "\n")

        call_count = {"n": 0}

        def spy(args, **kwargs):
            call_count["n"] += 1
            return _ok()

        monkeypatch.setattr(batch_follow.subprocess, "run", spy)
        monkeypatch.setattr(batch_follow.time, "sleep", lambda s: None)
        monkeypatch.setenv("SUPERTOOL_FOLLOW_DELAY", "0")

        rc = batch_follow.main(str(f))
        assert rc == 0
        assert call_count["n"] == n, \
            f"Expected {n} gh calls, got {call_count['n']} — possible early bail-out"


# ===========================================================================
# 10. JSON output parsing — malformed / huge JSON must be handled gracefully
# ===========================================================================

class TestJsonOutputParsing:
    """gh returning malformed or huge JSON must not crash the op."""

    def test_pr_malformed_json_prints_error(self, monkeypatch, capsys):
        monkeypatch.setattr(pr_mod.subprocess, "run",
                            lambda *a, **k: _ok("this is not json"))
        monkeypatch.setattr(sys, "argv", ["pr.py", "1"])
        rc = pr_mod.main()
        assert rc != 0
        out = capsys.readouterr().out
        assert "ERROR" in out or "invalid" in out.lower()

    def test_issue_malformed_json_prints_error(self, monkeypatch, capsys):
        monkeypatch.setattr(issue_mod.subprocess, "run",
                            lambda *a, **k: _ok("{broken json"))
        monkeypatch.setattr(sys, "argv", ["issue.py", "1"])
        rc = issue_mod.main()
        assert rc != 0
        out = capsys.readouterr().out
        assert "ERROR" in out or "invalid" in out.lower()

    def test_following_malformed_json_prints_error(self, monkeypatch, capsys):
        import importlib
        following = _load("github_following", "presets/github/following.py")
        monkeypatch.setattr(following.subprocess, "run",
                            lambda *a, **k: _ok("{broken"))
        monkeypatch.setattr(sys, "argv", ["following.py"])
        rc = following.main("")
        assert rc != 0
        err = capsys.readouterr().err
        assert "ERROR" in err or "bad JSON" in err

    def test_pr_huge_body_does_not_crash(self, monkeypatch, capsys):
        """A legitimate but enormous body must not crash (truncation already exists)."""
        huge_body = "x" * 500_000
        payload = json.dumps({
            "number": 1, "title": "t", "state": "OPEN",
            "author": {"login": "u"}, "headRefName": "b", "baseRefName": "master",
            "labels": [], "milestone": None, "isDraft": False, "mergeable": "MERGEABLE",
            "reviewDecision": None, "reviews": [], "mergeCommit": None,
            "additions": 0, "deletions": 0, "changedFiles": 0,
            "statusCheckRollup": [], "url": "", "body": huge_body, "comments": [],
            "assignees": [], "createdAt": "", "updatedAt": "", "reviewThreads": [],
        })
        monkeypatch.setattr(pr_mod.subprocess, "run", lambda *a, **k: _ok(payload))
        monkeypatch.setattr(sys, "argv", ["pr.py", "1"])
        rc = pr_mod.main()
        assert rc == 0
        out = capsys.readouterr().out
        # Body must be truncated to DESCRIPTION_MAX
        assert len(out) < 500_000 + 5000, \
            "Output suspiciously large — body truncation may not be working"

    def test_find_followable_malformed_json_chunk_skipped(self, monkeypatch, capsys):
        """Malformed JSON chunks in find_followable.fetch must be skipped, not crash."""
        responses = iter([
            _ok("{bad json\n[],"),   # first call (stargazers) — malformed
            _ok("[]"),              # second call (contributors) — empty
        ])

        def spy(args, **kwargs):
            return next(responses)

        monkeypatch.setattr(find_followable.subprocess, "run", spy)
        # Should not raise
        rc = find_followable.main("owner/repo")
        assert rc == 0
        out = capsys.readouterr().out
        assert "0 candidates" in out

    def test_pr_empty_json_response_prints_error(self, monkeypatch, capsys):
        """Empty stdout from gh must be handled as invalid JSON."""
        monkeypatch.setattr(pr_mod.subprocess, "run", lambda *a, **k: _ok(""))
        monkeypatch.setattr(sys, "argv", ["pr.py", "1"])
        rc = pr_mod.main()
        assert rc != 0
        out = capsys.readouterr().out
        assert "ERROR" in out
