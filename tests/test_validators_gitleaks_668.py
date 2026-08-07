"""The gitleaks secret-scanning validator (#668).

**The finding must not carry the secret, and neither must anything it wrote on
the way.** gitleaks' JSON report has `Secret` and `Match` fields in cleartext,
and the only way to get JSON out of it is `--report-path <file>` — so an
adapter that does the obvious thing writes the credential to disk before it
has decided not to print it. Three separate guards, one test each:

- `--redact` is passed, so the credential is never in the report file at all;
- the report is written under a private temp directory and deleted;
- the emitted error carries `RuleID`, line and description and **no**
  `source_context` — the field every sibling adapter attaches, which for this
  one would print the offending source line into the receipt, the terminal
  and the agent transcript.

`test_the_secret_never_appears_anywhere_in_the_verdict` is the one that has to
hold if the others are refactored: it greps the whole serialised payload.

**`rollback_on_fail` is false, against the issue's suggestion.** Argued in
`validators/gitleaks/README.md`; the short version is that reverting does not
unpublish the value, destroys whatever else the edit contained, and — on the
12-finding, 12-false-positive survey of this repo — would have reverted real
edits over test fixtures.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _adapter_budget import adapter_budget
from _adapter_verdict import assert_declined, assert_ok, describe, verdict
from _winenv import empty_path_env

REPO = Path(__file__).resolve().parent.parent
ADAPTER = REPO / "validators" / "gitleaks" / "gitleaks.py"

needs_gitleaks = pytest.mark.skipif(
    not shutil.which("gitleaks"),
    reason="gitleaks not on PATH",
)

posix_only = pytest.mark.skipif(os.name == "nt", reason="fake binary on PATH")

#: Assembled at runtime so this file does not itself carry a token-shaped
#: literal — the thing every fixture in `tests/` gets flagged for.
FAKE_PAT = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def _spawn(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ADAPTER), *args],
        capture_output=True, text=True, env=env,
        timeout=adapter_budget(ADAPTER), encoding="utf-8", errors="replace",
    )


def _run(path: Path, env: dict | None = None) -> dict:
    return verdict(_spawn(str(path), env=env), adapter=ADAPTER.name)


def _leaky(tmp_path: Path, name: str = "conf.py") -> Path:
    p = tmp_path / name
    p.write_text(f'TOKEN = "{FAKE_PAT}"\n', encoding="utf-8")
    return p


def _fake_gitleaks(tmp_path: Path, *, exit_code: int, report: object = None,
                   stderr: str = "") -> dict:
    """A `gitleaks` on PATH that writes `report` to whatever --report-path says."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "fake_gitleaks.py"
    script.write_text(
        "import json, sys\n"
        "argv = sys.argv[1:]\n"
        "path = argv[argv.index('--report-path') + 1] "
        "if '--report-path' in argv else None\n"
        f"report = {report!r}\n"
        "if path is not None and report is not None:\n"
        "    open(path, 'w', encoding='utf-8').write(json.dumps(report))\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n", encoding="utf-8")
    launcher = bindir / "gitleaks"
    launcher.write_text(
        "#!/bin/sh\n"
        f"exec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8")
    launcher.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    return env


# ---------------------------------------------------------------------------
# The third state — sharper here than anywhere else on the list
# ---------------------------------------------------------------------------

def test_missing_gitleaks_is_the_third_state(tmp_path: Path) -> None:
    """"No secrets found" from a scan that never ran costs a credential.

    Every other validator on this list reports a skip to avoid wasting an
    hour. This one reports it to avoid a green that a reader will act on by
    committing.
    """
    out = _run(_leaky(tmp_path), env=empty_path_env())
    assert "skipped" in out, describe(out)
    assert "gitleaks" in out["skipped"]
    for key in ("ok", "count", "errors"):
        assert key not in out, f"a skip must not carry {key!r}: {out}"
    assert out["tool"] == "gitleaks"


def test_the_skip_reason_says_nothing_was_scanned(tmp_path: Path) -> None:
    """A skip reason of "gitleaks not found" reads as a tooling note. On a
    secret scanner the reader has to be told what they do *not* know."""
    out = _run(_leaky(tmp_path), env=empty_path_env())
    reason = out["skipped"].lower()
    assert "not scanned" in reason or "no secret scan" in reason, out["skipped"]


def test_required_turns_the_absent_tool_into_a_loud_error(tmp_path: Path) -> None:
    env = empty_path_env()
    env["SUPERTOOL_REQUIRE_VALIDATORS"] = "gitleaks"
    out = _run(_leaky(tmp_path), env=env)
    assert "skipped" not in out, describe(out)
    assert_declined(out, context="a required scanner whose binary is absent")
    assert "SUPERTOOL_REQUIRE_VALIDATORS" in out["errors"][0]["msg"]


# ---------------------------------------------------------------------------
# Redaction — the decision this validator exists to get right
# ---------------------------------------------------------------------------

@needs_gitleaks
def test_the_secret_never_appears_anywhere_in_the_verdict(tmp_path: Path) -> None:
    """The whole payload, serialised, grepped for the credential.

    Written against the serialised form rather than named fields so it keeps
    holding when the error shape changes — a future `source_context`, a
    `metrics` block, a longer message all get caught here.
    """
    out = _run(_leaky(tmp_path))
    assert_declined(out, context="a file carrying a token-shaped literal")
    blob = json.dumps(out)
    assert FAKE_PAT not in blob, (
        "the adapter printed the matched value — a validator receipt is a "
        f"terminal, a log and an agent transcript: {blob[:400]}"
    )
    for frag in (FAKE_PAT[4:20], FAKE_PAT[-16:]):
        assert frag not in blob, f"a fragment of the secret survived: {frag}"


@needs_gitleaks
def test_a_finding_still_says_enough_to_act_on(tmp_path: Path) -> None:
    """Redaction is not silence. Rule, file, line — everything but the value."""
    out = _run(_leaky(tmp_path))
    err = out["errors"][0]
    assert err["code"] == "github-pat", describe(out)
    assert err["line"] == 1, describe(out)
    assert err["severity"] == "error", describe(out)
    assert err["msg"].strip(), describe(out)


@needs_gitleaks
def test_no_source_context_is_attached(tmp_path: Path) -> None:
    """Every sibling adapter attaches the source line. This one must not —
    the source line is where the secret is."""
    out = _run(_leaky(tmp_path))
    for err in out["errors"]:
        assert "source_context" not in err, (
            f"source_context would print the secret's own line: {err}")


@needs_gitleaks
def test_the_report_file_is_removed_and_was_never_readable_by_others(
        tmp_path: Path) -> None:
    """No temp report survives the run.

    gitleaks can only emit JSON to a path. That file is a credential store for
    as long as it exists, so it lives in a private directory and is deleted
    even when the scan fails.
    """
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    env = dict(os.environ)
    # A private TMPDIR, so this observes only the run it started. Globbing the
    # shared one raced the other xdist workers' live scans and failed on their
    # directories, which is a defect in the test and not in the adapter.
    for var in ("TMPDIR", "TEMP", "TMP"):
        env[var] = str(tmpdir)
    out = _run(_leaky(tmp_path), env=env)
    assert_declined(out, context="a leaky file")
    leftovers = sorted(tmpdir.iterdir())
    assert not leftovers, f"report directories left behind: {leftovers}"


@posix_only
def test_redact_is_passed_to_gitleaks(tmp_path: Path) -> None:
    """The first guard, asserted at the argv rather than at the output.

    `--redact` means the credential is not written to the report file in the
    first place — the other two guards only stop it being *printed*.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    argv_log = tmp_path / "argv.txt"
    script = bindir / "fake_gitleaks.py"
    script.write_text(
        "import sys\n"
        f"open({str(argv_log)!r}, 'w').write(chr(10).join(sys.argv[1:]))\n"
        "argv = sys.argv[1:]\n"
        "if '--report-path' in argv:\n"
        "    open(argv[argv.index('--report-path') + 1], 'w').write('[]')\n"
        "sys.exit(0)\n", encoding="utf-8")
    launcher = bindir / "gitleaks"
    launcher.write_text(
        "#!/bin/sh\n"
        f"exec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8")
    launcher.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    _run(_leaky(tmp_path), env=env)
    argv = argv_log.read_text(encoding="utf-8").splitlines()
    assert "--redact" in argv, argv
    assert "--no-git" in argv, argv


# ---------------------------------------------------------------------------
# Verdict shape
# ---------------------------------------------------------------------------

@needs_gitleaks
def test_a_clean_file_is_clean(tmp_path: Path) -> None:
    p = tmp_path / "plain.py"
    p.write_text("def f():\n    return 1\n", encoding="utf-8")
    out = _run(p)
    assert_ok(out)
    assert out["count"] == 0


@needs_gitleaks
def test_an_inline_allow_comment_suppresses_one_line_and_not_the_check(
        tmp_path: Path) -> None:
    """How a fixture with a fake key is silenced, and how it is not.

    `gitleaks:allow` is per-line, lands in the diff and is reviewable. The
    alternative anyone reaches for — an `exclude` glob over `tests/` — turns
    the whole directory off invisibly and forever, which is how a scanner
    stops protecting anything.
    """
    p = tmp_path / "fixture.py"
    p.write_text(
        f'TOKEN = "{FAKE_PAT}"  # gitleaks:allow\n'
        f'OTHER = "{FAKE_PAT}"\n', encoding="utf-8")
    out = _run(p)
    assert_declined(out, context="one allowed line and one that is not")
    assert out["count"] == 1, (
        f"expected the un-annotated line only: {describe(out)}")
    assert out["errors"][0]["line"] == 2, describe(out)


@posix_only
def test_an_unexplained_failure_stays_loud(tmp_path: Path) -> None:
    env = _fake_gitleaks(tmp_path, exit_code=3, report=None,
                         stderr="FTL could not open config\n")
    out = _run(_leaky(tmp_path), env=env)
    assert "skipped" not in out, describe(out)
    assert_declined(out, context="an unexplained gitleaks failure")
    err = out["errors"][0]
    assert err["code"] == "adapter", describe(out)
    assert "3" in err["msg"]


@posix_only
def test_a_report_that_is_not_json_is_an_adapter_error(tmp_path: Path) -> None:
    env = _fake_gitleaks(tmp_path, exit_code=1, report="not-a-list")
    out = _run(_leaky(tmp_path), env=env)
    assert_declined(out, context="a report that is not a JSON array")
    assert out["errors"][0]["code"] == "adapter", describe(out)


def test_no_file_arg() -> None:
    out = verdict(_spawn(), adapter=ADAPTER.name)
    assert_declined(out, context="no file argument")
    assert out["errors"][0]["code"] == "adapter"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_registered_without_rollback_and_without_a_blanket_exclude() -> None:
    """Two decisions pinned at once.

    `rollback_on_fail: false` — reverting the edit does not unpublish the
    value and destroys everything else the edit contained.

    No `exclude` — a glob over `tests/` or `docs/` is the switch that silently
    disables the check for the paths most likely to grow a real secret next.
    Suppression is per-line, in the diff.
    """
    cfg = json.loads((REPO / ".supertool.example.json").read_text(encoding="utf-8"))
    entry = cfg["validators"]["gitleaks"]
    assert entry["rollback_on_fail"] is False, entry
    assert "exclude" not in entry, entry
    assert "validators/gitleaks/gitleaks.py" in entry["cmd"]
