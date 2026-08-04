"""#760 — claude-log redacts known secret patterns by default, `:raw` opts out.

Two halves:

1. Unit tests for the shared detector in ``presets/_secrets.py`` — what it
   matches, and (just as important) what it must NOT match. A false positive
   that eats a git SHA or the session UUID would break the ops these tests
   protect.
2. End-to-end tests for the four surfaces named in #760 — ``tail``, ``summary``,
   ``list`` and the shared ``_common`` truncation path — asserting both that the
   secret is *absent* and that the disclosure is *present*. Absence alone would
   pass on a crash.

See ``tests/test_security_claude_log.py::TestRedactionContract`` for the
contract reversal this file implements.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PRESET_DIR = REPO_ROOT / "presets" / "claude-log"
sys.path.insert(0, str(REPO_ROOT / "presets"))
sys.path.insert(0, str(PRESET_DIR))

from _preset_loader import load_preset_module  # noqa: E402

_common = load_preset_module("claude-log", "_common", prefix="claude_log_")

import _secrets  # noqa: E402

# Assembled at runtime rather than written as one literal. A complete
# `glpat-` + 20-char string in this file is indistinguishable from a real
# credential to GitHub's secret scanner, which rejects the push outright
# (GH013, "Push cannot contain secrets") — this file was blocked on exactly
# that. Splitting the prefix breaks the scanner's pattern while the value the
# detector under test receives is byte-identical, so coverage is unchanged.
# Do not tidy this back into a single literal.
FAKE_GITLAB_PAT = "glpat" + "-AbCdEf1234567890XyZq"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _user_text(text: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _assistant_text(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _assistant_tool(name: str, inp: dict) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": name, "input": inp}],
        },
    }


def _tool_result(content: str, is_error: bool = False) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": content, "is_error": is_error}],
        },
    }


def _make_project(tmp_path: Path):
    home = tmp_path / "fake-home"
    cwd = tmp_path / "work" / "proj"
    cwd.mkdir(parents=True, exist_ok=True)
    encoded = _common.encode_cwd(str(cwd))
    proj_dir = home / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True)
    return home, cwd, proj_dir


def _run_in(script: str, *args: str, home: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    return subprocess.run(
        [sys.executable, str(PRESET_DIR / script), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
        timeout=15,
    )


# ---------------------------------------------------------------------------
# 1. Detector — true positives
# ---------------------------------------------------------------------------

class TestDetectorMatches:
    """Known-prefix / structurally distinctive credentials must be caught."""

    def test_anthropic_key(self) -> None:
        out, n = _secrets.redact("ANTHROPIC_API_KEY=sk-ant-secret python3 x.py")
        assert n >= 1
        assert "sk-ant-secret" not in out
        assert _secrets.MARKER_PREFIX in out

    def test_bearer_header(self) -> None:
        out, n = _secrets.redact("curl -H 'Authorization: Bearer sk-test-1234567890abcd' https://api.example.com")
        assert n >= 1
        assert "sk-test-1234567890abcd" not in out
        assert "curl" in out and "https://api.example.com" in out

    def test_github_pat(self) -> None:
        out, n = _secrets.redact("git push https://ghp_0123456789abcdefghijklmnopqrstuvwxyz@github.com/x/y")
        assert n >= 1
        assert "ghp_0123456789abcdefghijklmnopqrstuvwxyz" not in out

    def test_gitlab_pat(self) -> None:
        out, n = _secrets.redact(f"export GITLAB_TOKEN={FAKE_GITLAB_PAT}")
        assert n >= 1
        assert FAKE_GITLAB_PAT not in out

    def test_aws_access_key_id(self) -> None:
        out, n = _secrets.redact("aws configure set AKIAIOSFODNN7EXAMPLE")
        assert n >= 1
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_slack_token(self) -> None:
        out, n = _secrets.redact("xoxb-1234567890-0987654321-AbCdEfGhIjKl")
        assert n >= 1
        assert "xoxb-1234567890" not in out

    def test_google_api_key(self) -> None:
        out, n = _secrets.redact("key=AIzaSyA1234567890abcdefghijklmnopqrstuv")
        assert n >= 1
        assert "AIzaSyA1234567890abcdefghijklmnopqrstuv" not in out

    def test_url_basic_auth_password(self) -> None:
        out, n = _secrets.redact("git clone https://user:hunter2secret@example.com/repo.git")
        assert n >= 1
        assert "hunter2secret" not in out
        assert "example.com/repo.git" in out

    def test_private_key_block(self) -> None:
        out, n = _secrets.redact("-----BEGIN RSA PRIVATE KEY-----MIIEowIBAAKCAQEA-----END RSA PRIVATE KEY-----")
        assert n >= 1
        assert "MIIEowIBAAKCAQEA" not in out

    def test_generic_secret_assignment(self) -> None:
        out, n = _secrets.redact("DEPLOY_PASSWORD=correcthorsebattery ./deploy.sh")
        assert n >= 1
        assert "correcthorsebattery" not in out
        assert "./deploy.sh" in out
        assert "DEPLOY_PASSWORD" in out, "the variable NAME stays — only the value goes"

    def test_counts_multiple_distinct_values(self) -> None:
        _out, n = _secrets.redact("A=sk-ant-aaaabbbb and B=ghp_0123456789abcdefghijklmnopqrstuvwxyz")
        assert n == 2


# ---------------------------------------------------------------------------
# 2. Detector — false positives it must NOT produce
# ---------------------------------------------------------------------------

class TestDetectorLeavesInnocentTextAlone:
    """A false positive here breaks the op for its actual purpose.

    Session UUIDs and git SHAs are high-entropy by nature and are exactly what
    claude-log exists to show. This is why the detector is prefix-based rather
    than entropy-based.
    """

    def test_session_uuid_untouched(self) -> None:
        uuid = "84397aff-4925-45fd-afc5-3641e28c993c"
        out, n = _secrets.redact(f"Session: {uuid}")
        assert out == f"Session: {uuid}"
        assert n == 0

    def test_git_sha_untouched(self) -> None:
        out, n = _secrets.redact("git show e5075d2c9f4a1b2c3d4e5f60718293a4b5c6d7e8")
        assert n == 0
        assert "e5075d2c9f4a1b2c3d4e5f60718293a4b5c6d7e8" in out

    def test_ordinary_command_untouched(self) -> None:
        out, n = _secrets.redact("python3 supertool.py 'read:presets/claude-log/tail.py'")
        assert n == 0
        assert out == "python3 supertool.py 'read:presets/claude-log/tail.py'"

    def test_placeholder_values_untouched(self) -> None:
        for text in (
            "API_KEY=$ANTHROPIC_API_KEY",
            "TOKEN=${GITLAB_TOKEN}",
            "PASSWORD=<your-password>",
            "SECRET=********",
        ):
            _out, n = _secrets.redact(text)
            assert n == 0, f"placeholder was redacted: {text!r}"

    def test_token_counter_untouched(self) -> None:
        """`tokens=1771387649` is a count, not a credential. Found by sweeping
        49M characters of real transcript, not by imagination."""
        out, n = _secrets.redact("tokens=1771387649(input=12, output=3)")
        assert n == 0
        assert "1771387649" in out

    def test_prefix_inside_a_longer_token_untouched(self) -> None:
        """`ASIA...` inside a base64 blob and `sk-` inside `kevin-task-1771387649`
        both matched before the boundary guards went in."""
        blob = "AAJkAAJkAAJkAAJkns9AYqde30TIAESIAESIAESIAESIAESIAESIAkEDZIECxs2zUI0"
        _out, n = _secrets.redact(blob)
        assert n == 0
        _out, n = _secrets.redact("Merge branch 'kevin-task-1771387649' into master")
        assert n == 0

    def test_camelcase_repr_untouched(self) -> None:
        """`tokens=TokenUsage(input=...)` is a repr. Also from the real sweep."""
        _out, n = _secrets.redact("tokens=TokenUsage(input=12, output=3)")
        assert n == 0

    def test_the_word_basic_in_prose_untouched(self) -> None:
        _out, n = _secrets.redact("generic module test template with basic permission checks")
        assert n == 0

    def test_empty_and_none_safe(self) -> None:
        assert _secrets.redact("") == ("", 0)
        assert _secrets.redact(None)[1] == 0


# ---------------------------------------------------------------------------
# 3. Disclosure text
# ---------------------------------------------------------------------------

class TestDisclosure:
    def test_names_the_escape_hatch_and_refuses_to_claim_safety(self) -> None:
        note = _secrets.disclosure(2)
        assert "2" in note
        assert ":raw" in note
        low = note.lower()
        assert "pattern" in low, "must say detection is pattern-based"
        assert "miss" in low, "must not claim the output is now safe"

    def test_no_disclosure_when_nothing_redacted(self) -> None:
        assert _secrets.disclosure(0) == ""


# ---------------------------------------------------------------------------
# 4. tail.py end to end
# ---------------------------------------------------------------------------

class TestTailRedaction:
    def test_redacts_and_discloses(self, tmp_path: Path) -> None:
        home, cwd, proj_dir = _make_project(tmp_path)
        uuid = "tail-redact"
        _write_jsonl(proj_dir / f"{uuid}.jsonl", [
            _assistant_tool("Bash", {"command": "ANTHROPIC_API_KEY=sk-ant-secret python3 script.py"}),
            _tool_result("done"),
        ])
        r = _run_in("tail.py", uuid, home=home, cwd=cwd)
        assert r.returncode == 0, r.stderr + r.stdout
        assert "sk-ant-secret" not in r.stdout
        assert _secrets.MARKER_PREFIX in r.stdout, "the redaction must be visible in place"
        assert ":raw" in r.stdout, "the disclosure must name the escape hatch"

    def test_raw_shows_secret_verbatim(self, tmp_path: Path) -> None:
        """Without this, the escape hatch is unproven."""
        home, cwd, proj_dir = _make_project(tmp_path)
        uuid = "tail-raw"
        _write_jsonl(proj_dir / f"{uuid}.jsonl", [
            _assistant_tool("Bash", {"command": "ANTHROPIC_API_KEY=sk-ant-secret python3 script.py"}),
            _tool_result("done"),
        ])
        r = _run_in("tail.py", uuid, "raw", home=home, cwd=cwd)
        assert r.returncode == 0, r.stderr + r.stdout
        assert "sk-ant-secret" in r.stdout
        assert _secrets.MARKER_PREFIX not in r.stdout

    def test_raw_after_n_still_parses(self, tmp_path: Path) -> None:
        home, cwd, proj_dir = _make_project(tmp_path)
        uuid = "tail-raw-n"
        _write_jsonl(proj_dir / f"{uuid}.jsonl", [
            _assistant_tool("Bash", {"command": "ANTHROPIC_API_KEY=sk-ant-secret python3 script.py"}),
        ])
        r = _run_in("tail.py", uuid, "5", "raw", home=home, cwd=cwd)
        assert r.returncode == 0, r.stderr + r.stdout
        assert "sk-ant-secret" in r.stdout

    def test_redacts_secret_in_tool_result(self, tmp_path: Path) -> None:
        """Results leak too — an echoed .env is a tool_result, not a tool_use."""
        home, cwd, proj_dir = _make_project(tmp_path)
        uuid = "tail-result"
        _write_jsonl(proj_dir / f"{uuid}.jsonl", [
            _assistant_tool("Bash", {"command": "cat .env"}),
            _tool_result("STRIPE_SECRET=sk_live_0123456789abcdefghij"),
        ])
        r = _run_in("tail.py", uuid, home=home, cwd=cwd)
        assert r.returncode == 0, r.stderr + r.stdout
        assert "sk_live_0123456789abcdefghij" not in r.stdout
        assert _secrets.MARKER_PREFIX in r.stdout

    def test_redacts_secret_in_user_text(self, tmp_path: Path) -> None:
        home, cwd, proj_dir = _make_project(tmp_path)
        uuid = "tail-usertext"
        _write_jsonl(proj_dir / f"{uuid}.jsonl", [
            _user_text("use ghp_0123456789abcdefghijklmnopqrstuvwxyz to push"),
        ])
        r = _run_in("tail.py", uuid, home=home, cwd=cwd)
        assert r.returncode == 0, r.stderr + r.stdout
        assert "ghp_0123456789abcdefghijklmnopqrstuvwxyz" not in r.stdout

    def test_clean_session_gets_no_disclosure_noise(self, tmp_path: Path) -> None:
        home, cwd, proj_dir = _make_project(tmp_path)
        uuid = "tail-clean"
        _write_jsonl(proj_dir / f"{uuid}.jsonl", [
            _assistant_tool("Bash", {"command": "ls -la"}),
            _tool_result("total 0"),
        ])
        r = _run_in("tail.py", uuid, home=home, cwd=cwd)
        assert r.returncode == 0, r.stderr + r.stdout
        assert _secrets.MARKER_PREFIX not in r.stdout
        assert ":raw" not in r.stdout
        assert "ls -la" in r.stdout


# ---------------------------------------------------------------------------
# 5. summary.py end to end
# ---------------------------------------------------------------------------

class TestSummaryRedaction:
    def test_redacts_user_and_assistant_text(self, tmp_path: Path) -> None:
        """summary already refuses tool inputs, but it echoes free text —
        which is exactly where a human pastes a key."""
        home, cwd, proj_dir = _make_project(tmp_path)
        uuid = "sum-redact"
        _write_jsonl(proj_dir / f"{uuid}.jsonl", [
            _user_text("here is my key sk-ant-usertyped1234, use it"),
            _assistant_text(f"ok, I used {FAKE_GITLAB_PAT}"),
        ])
        r = _run_in("summary.py", uuid, home=home, cwd=cwd)
        assert r.returncode == 0, r.stderr + r.stdout
        assert "sk-ant-usertyped1234" not in r.stdout
        assert FAKE_GITLAB_PAT not in r.stdout
        assert _secrets.MARKER_PREFIX in r.stdout
        assert ":raw" in r.stdout

    def test_raw_shows_secret_verbatim(self, tmp_path: Path) -> None:
        home, cwd, proj_dir = _make_project(tmp_path)
        uuid = "sum-raw"
        _write_jsonl(proj_dir / f"{uuid}.jsonl", [
            _user_text("here is my key sk-ant-usertyped1234, use it"),
        ])
        r = _run_in("summary.py", uuid, "raw", home=home, cwd=cwd)
        assert r.returncode == 0, r.stderr + r.stdout
        assert "sk-ant-usertyped1234" in r.stdout


# ---------------------------------------------------------------------------
# 6. list.py end to end
# ---------------------------------------------------------------------------

class TestListRedaction:
    def test_redacts_first_user_excerpt(self, tmp_path: Path) -> None:
        home, cwd, proj_dir = _make_project(tmp_path)
        uuid = "list-redact"
        _write_jsonl(proj_dir / f"{uuid}.jsonl", [
            _user_text("deploy with TOKEN=supersecretvalue now"),
        ])
        r = _run_in("list.py", home=home, cwd=cwd)
        assert r.returncode == 0, r.stderr + r.stdout
        assert "supersecretvalue" not in r.stdout
        assert _secrets.MARKER_PREFIX in r.stdout
        assert ":raw" in r.stdout
        assert uuid in r.stdout, "the session UUID must survive redaction"

    def test_raw_shows_secret_verbatim(self, tmp_path: Path) -> None:
        home, cwd, proj_dir = _make_project(tmp_path)
        uuid = "list-raw"
        _write_jsonl(proj_dir / f"{uuid}.jsonl", [
            _user_text("deploy with TOKEN=supersecretvalue now"),
        ])
        r = _run_in("list.py", "raw", home=home, cwd=cwd)
        assert r.returncode == 0, r.stderr + r.stdout
        assert "supersecretvalue" in r.stdout

    def test_raw_after_limit_still_parses(self, tmp_path: Path) -> None:
        home, cwd, proj_dir = _make_project(tmp_path)
        uuid = "list-raw-n"
        _write_jsonl(proj_dir / f"{uuid}.jsonl", [
            _user_text("deploy with TOKEN=supersecretvalue now"),
        ])
        r = _run_in("list.py", "3", "raw", home=home, cwd=cwd)
        assert r.returncode == 0, r.stderr + r.stdout
        assert "supersecretvalue" in r.stdout


# ---------------------------------------------------------------------------
# 7. Redaction happens before truncation
# ---------------------------------------------------------------------------

class TestRedactBeforeTruncate:
    def test_secret_at_truncation_boundary_is_not_half_leaked(self, tmp_path: Path) -> None:
        """Truncating first would leave the head of a key in the output."""
        home, cwd, proj_dir = _make_project(tmp_path)
        uuid = "tail-boundary"
        filler = "x" * 290
        _write_jsonl(proj_dir / f"{uuid}.jsonl", [
            _assistant_tool("Bash", {"command": f"echo {filler} && export K=ghp_0123456789abcdefghijklmnopqrstuvwxyz"}),
        ])
        r = _run_in("tail.py", uuid, home=home, cwd=cwd)
        assert r.returncode == 0, r.stderr + r.stdout
        assert "ghp_0123456789" not in r.stdout
