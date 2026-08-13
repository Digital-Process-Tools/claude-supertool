"""A refusal phrase in phpstan's output erased a report it sat next to (#1527).

`validators/phpstan/phpstan.py` runs its refusal test on
`stdout + stderr` **concatenated**, and `refusal.is_refusal` is a lowercased
substring test. So the check answers "does this blob contain a refusal phrase
anywhere", not "did phpstan decline". It fires before the two arms below it,
so it wins over both of them.

What that is reachable for, and what it is not:

- **Not** reachable: findings dropped from a parseable report. The whole block
  sits behind `if data is None`, so a run whose JSON parses never reaches it.
  The issue's stated mechanism — "a line of phpstan output containing e.g.
  `no files found to analyse`, echoed source, a comment" — does not happen
  under `--error-format=json`: measured against phpstan 2.1.55, findings go to
  stdout as one JSON object and the refusal goes to stderr with stdout empty.
- **Reachable**: a report that arrived on stdout and would not parse — a PHP
  deprecation or warning printed ahead of the JSON by the analysed project's
  own bootstrap, or any `PHPSTAN_SKIP_PATTERNS` entry the repo added that the
  noise happens to match. phpstan reported; the adapter could not read it.

**The issue's rollback claim does not hold, and neither did the first draft of
this file.** Measured against the real core predicates on master:

    skipped -> regressed False, no_verdict None,  not_checked None
    fault   -> regressed False, no_verdict <msg>, not_checked <msg>

Neither verdict rolls back: `_validator_regressed` returns False for a
`skipped` on its first line, and an `adapter`-coded result is a non-verdict
too, so it is never subtracted from a baseline either
(`docs/validators.md` §"Declining instead of guessing"). Nothing is left on
disk that should have been reverted. The class is a misreport.

What the misreport costs is the **exit code**, and that is unconditional. An
`adapter`-only result is recorded by `_note_not_checked` and exits 1 whatever
`$SUPERTOOL_REQUIRE_VALIDATORS` says; an adapter's own `skipped` is row four of
that section's table — "exits 0, never escalates". So the chain the variable
exists for, `supertool 'edit:...' && git commit`, ran on green over a phpstan
report the adapter had thrown away, and the row said `skipped` where it should
have said `NOT CHECKED (phpstan output not json: ...)`.

The rule this pins: **findings live on stdout and a declination does not, so
anything phpstan wrote to stdout that is not itself the refusal means it
reported rather than declined.** A skip now requires stdout to hold nothing
beyond the refusal statement itself. Stated as a limit rather than implied: a
line carrying both the noise and the report with no break between them is
still classified as a refusal line, and stderr is unchanged — a fault dump on
stderr that mentions a refusal phrase still skips.
"""
from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

import pytest

import supertool

REPO = Path(__file__).resolve().parent.parent
PHPSTAN_PY = REPO / "validators" / "phpstan" / "phpstan.py"

FINDINGS_JSON = json.dumps({
    "totals": {"errors": 0, "file_errors": 2},
    "files": {"A.php": {"errors": 2, "messages": [
        {"line": 2, "identifier": "return.type", "message": "returns string"},
        {"line": 9, "identifier": "argument.type", "message": "wrong arg"},
    ]}},
    "errors": [],
})

NOISE = "Warning: no files found to analyse in the result cache dir"


def _load():
    spec = importlib.util.spec_from_file_location("phpstan_under_test", PHPSTAN_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _drive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *,
           stdout: str = "", stderr: str = "", rc: int = 1) -> dict:
    """Run the real adapter against a canned phpstan exit, in process.

    In process rather than through a PATH shim so it runs on Windows too: the
    sibling suite for this adapter is `skipif(os.name == "nt")` for its
    `/bin/sh` shim, and a platform this never executes on is a platform it
    reports nothing about.
    """
    mod = _load()
    target = tmp_path / "A.php"
    target.write_text("<?php" + chr(10), encoding="utf-8")
    monkeypatch.setattr(mod.shutil, "which", lambda _b: "/usr/bin/php")
    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(stdout=stdout, stderr=stderr,
                                              returncode=rc))
    monkeypatch.setattr(mod.sys, "argv", ["phpstan.py", str(target)])
    seen: list = []
    monkeypatch.setattr(mod, "emit", seen.append)
    mod.main()
    assert len(seen) == 1, seen
    return seen[0]


def _is_fault(data: dict) -> bool:
    return ("skipped" not in data
            and data.get("ok") is False
            and any(e.get("code") == "adapter" for e in data.get("errors") or []))


# ---------------------------------------------------------------------------
# The defect: a report on stdout displaced by a phrase somewhere in the blob
# ---------------------------------------------------------------------------

def test_noise_before_the_report_does_not_make_it_a_declination(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A stock refusal phrase in bootstrap noise ahead of the JSON.

    phpstan wrote a report. The adapter could not parse it. That is an adapter
    fault about a file phpstan *did* look at, not phpstan declining to look.
    """
    data = _drive(monkeypatch, tmp_path,
                  stdout=NOISE + chr(10) + FINDINGS_JSON, rc=1)
    assert _is_fault(data), json.dumps(data)


def test_house_skip_pattern_cannot_erase_a_report(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`PHPSTAN_SKIP_PATTERNS` widens the phrase list; it must not widen this.

    The env var exists so a repo can name a house-specific declination without
    waiting for a release. A pattern that also matches ordinary PHP noise then
    turned every polluted run into a whole-file non-verdict.
    """
    monkeypatch.setenv("PHPSTAN_SKIP_PATTERNS", "deprecated")
    stdout = ("Deprecated: Return type of X::y() should be compatible"
              + chr(10) + FINDINGS_JSON)
    data = _drive(monkeypatch, tmp_path, stdout=stdout, rc=1)
    assert _is_fault(data), json.dumps(data)


def test_the_unreadable_line_is_named_not_just_counted(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`phpstan output not json` on its own tells the reader nothing to act on."""
    data = _drive(monkeypatch, tmp_path,
                  stdout=NOISE + chr(10) + FINDINGS_JSON, rc=1)
    msg = data["errors"][0]["msg"]
    assert "not json" in msg
    assert "totals" in msg or "file_errors" in msg, msg


def test_the_escalation_is_what_changed_not_the_rollback(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Both halves, asserted against the core predicates that decide them.

    The half the issue claimed: neither verdict rolls back, so no edit survives
    that should have been reverted. Pinned so the wrong severity cannot be
    re-derived from the wrong premise.

    The half that is real: only the fault reaches `_note_not_checked`, and that
    is what makes the call exit non-zero regardless of
    `$SUPERTOOL_REQUIRE_VALIDATORS`. Laundering it into a `skipped` returned a
    green to a `&& git commit`.
    """
    clean = {"tool": "phpstan", "file": "A.php", "ok": True, "count": 0, "errors": []}
    fault = _drive(monkeypatch, tmp_path,
                   stdout=NOISE + chr(10) + FINDINGS_JSON, rc=1)
    declined = _drive(monkeypatch, tmp_path,
                      stderr=" [ERROR] No files found to analyse.", rc=1)

    assert supertool._validator_regressed(clean, fault) is False, fault
    assert supertool._validator_regressed(clean, declined) is False, declined

    assert supertool._validator_not_checked(fault) is not None, fault
    assert supertool._validator_not_checked(declined) is None, declined


# ---------------------------------------------------------------------------
# Pins: the declinations that must keep declining
# ---------------------------------------------------------------------------

def test_refusal_on_stderr_with_empty_stdout_still_skips(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The measured shape. phpstan 2.1.55, empty dir, `--error-format=json`:
    stdout empty, stderr a blank line then ` [ERROR] No files found to analyse.`,
    exit 1.
    """
    data = _drive(monkeypatch, tmp_path,
                  stderr=chr(10) + " [ERROR] No files found to analyse.", rc=1)
    assert "skipped" in data, json.dumps(data)
    assert "no files found to analyse" in data["skipped"].lower()


def test_refusal_alone_on_stdout_still_skips(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Some builds put the declination on stdout. Nothing else is there, so it
    is still a declination — the pin from `test_validators_phpstan_scope_263`,
    restated here because this change is the one that could break it."""
    data = _drive(monkeypatch, tmp_path,
                  stdout=" [ERROR] No files found to analyse.", rc=1)
    assert "skipped" in data, json.dumps(data)


def test_preamble_on_stderr_does_not_block_the_skip(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A real run prints `Note: Using configuration file ...` first. stderr is
    not where a report lives, so nothing there disqualifies a declination."""
    data = _drive(monkeypatch, tmp_path, stderr=(
        "Note: Using configuration file /repo/phpstan.neon." + chr(10)
        + chr(10)
        + " [ERROR] No files found to analyse."), rc=1)
    assert "skipped" in data, json.dumps(data)
    assert "Using configuration file" not in data["skipped"]


def test_fatal_with_empty_stdout_stays_an_error(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = _drive(monkeypatch, tmp_path, rc=255, stderr=(
        "PHP Fatal error:  Allowed memory size of 2147483648 bytes exhausted"))
    assert _is_fault(data), json.dumps(data)


def test_parseable_findings_are_untouched(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = _drive(monkeypatch, tmp_path, stdout=FINDINGS_JSON, rc=1)
    assert "skipped" not in data
    assert data["ok"] is False and data["count"] == 2
    assert {e["code"] for e in data["errors"]} == {"return.type", "argument.type"}


def test_genuine_clean_is_still_clean(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = _drive(monkeypatch, tmp_path,
                  stdout=json.dumps({"totals": {"file_errors": 0}, "files": {}}),
                  rc=0)
    assert data["ok"] is True and data["count"] == 0 and "skipped" not in data
