from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

PRESET_PATH = Path(__file__).parent.parent / "presets" / "github" / "job.py"
_spec = importlib.util.spec_from_file_location("github_job_1957", PRESET_PATH)
assert _spec is not None and _spec.loader is not None
job = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(job)

# The real refusal `gh` prints for a log containing terminal escape sequences
# when the caller has not passed --allow-escape-sequences (#1957's own repro).
REAL_GH_REFUSAL = (
    "the response contains terminal escape sequences; "
    "pass --allow-escape-sequences to output it anyway"
)

# The real refusal an older `gh`, predating the flag, prints for an unknown
# flag -- a different failure that must not render as the one above.
REAL_GH_UNKNOWN_FLAG = "unknown flag: --allow-escape-sequences"

META = (
    '{"name": "test-job", "status": "completed", "conclusion": "failure", '
    '"run_id": 42, "run_url": "https://github.com/x/y/actions/runs/42"}'
)


def _make_fake_run(ansi_log: str, gh_supports_flag: bool = True):
    """Model gh's own behaviour, not a bypass of it.

    Unlike test_github_job.py's `_make_fake_run`, this one inspects whether
    `--allow-escape-sequences` was passed to the `/logs` call and refuses
    exactly the way real `gh` does when it is missing -- that refusal is the
    bug in #1957, and a fixture that always hands back the log regardless of
    the flag could never reproduce it.
    """

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess:
        cmd = args[1] if len(args) > 1 else ""
        # The url is whichever positional (non-flag) argument follows `api` —
        # a real call site may insert flags like --allow-escape-sequences
        # between `api` and the url, so a fixed index would silently miss it.
        url = next((a for a in args[2:] if not a.startswith("--")), "")
        if cmd == "api" and url.endswith("/logs"):
            has_flag = "--allow-escape-sequences" in args
            if not gh_supports_flag and has_flag:
                return subprocess.CompletedProcess(
                    args=args, returncode=2, stdout="",
                    stderr=REAL_GH_UNKNOWN_FLAG + "\n\nUsage: gh api <endpoint>\n",
                )
            if "\x1b[" in ansi_log and not has_flag:
                return subprocess.CompletedProcess(
                    args=args, returncode=1, stdout="", stderr=REAL_GH_REFUSAL,
                )
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=ansi_log, stderr="",
            )
        if cmd == "api":
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=META, stderr="",
            )
        return subprocess.CompletedProcess(
            args=args, returncode=1, stdout="", stderr="",
        )

    return fake_run


def _run_main(monkeypatch, argv: list[str], ansi_log: str, gh_supports_flag: bool = True) -> int:
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(job.subprocess, "run", _make_fake_run(ansi_log, gh_supports_flag))
    return job.main()


def test_ansi_log_returns_real_diagnostic_lines(monkeypatch, capsys) -> None:
    """A log carrying ANSI escapes must be readable, not refused (#1957).

    The fixture models gh's real refusal when the flag is absent -- so this
    fails today, before the call site passes --allow-escape-sequences, and
    passes once it does and the existing ANSI-strip regex runs on the result.
    """
    ansi_log = (
        "\x1b[31mFAILED tests/test_thing.py::test_x - AssertionError\x1b[0m\n"
        "\x1b[32m1 failed, 10 passed\x1b[0m\n"
        "##[error]Process completed with exit code 1.\n"
    )
    rc = _run_main(monkeypatch, ["job.py", "123", "raw"], ansi_log)
    out = capsys.readouterr().out
    assert rc == 0
    assert "FAILED tests/test_thing.py::test_x - AssertionError" in out
    assert "1 failed, 10 passed" in out
    assert "\x1b[" not in out
    assert REAL_GH_REFUSAL not in out


def test_plain_log_without_ansi_is_unchanged(monkeypatch, capsys) -> None:
    """A log with no escape sequences must behave exactly as it does today."""
    plain_log = "build started\nERROR: thing exploded\nbuild done\n"
    rc = _run_main(monkeypatch, ["job.py", "123"], plain_log)
    out = capsys.readouterr().out
    assert rc == 0
    assert "build started" in out
    assert "ERROR: thing exploded" in out
    assert "build done" in out


def test_gh_without_the_flag_is_reported_as_itself(monkeypatch, capsys) -> None:
    """An older `gh` that rejects --allow-escape-sequences is its own failure.

    It must not render as the escape-sequence refusal (the bug being fixed)
    and not as the generic "gh failed" catch-all either -- #1957 asks for this
    named as itself, as the third of three states.
    """
    ansi_log = "\x1b[31msome coloured output\x1b[0m\n"
    rc = _run_main(monkeypatch, ["job.py", "123", "raw"], ansi_log, gh_supports_flag=False)
    out = capsys.readouterr().out
    assert rc == 1
    assert "--allow-escape-sequences" in out
    assert "does not support" in out
    assert REAL_GH_REFUSAL not in out
    # Not the generic gh-failed catch-all either — a reader searching for
    # "gh failed for" to find an unclassified error must not find this one.
    assert "gh failed for" not in out
