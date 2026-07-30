"""A Windows failure message has to be readable, or the leg is not diagnosable (#546).

The measured before-state, so the after-state means something. On
`windows-latest` a pytest failure line reaches the log through pytest's own
terminal writer, on a stream whose codec is the runner's console codepage:

    run 30485881190, job 90691288041 (windows-latest, 3.9)
      ...runtime dir C:\\...\\rt on this platform \\xEF\\xBF\\xBD os.geteuid...
    run 30500828589, job 90739894758 (windows-latest, 3.12)
      ...presets/mcp/_spawn.py is not scanned \\xEF\\xBF\\xBD the walk has been...

`EF BF BD` is U+FFFD, the replacement character, where each file's source holds
an em dash. Both logs also carry two *intact* em dashes (`E2 80 94`) echoed from
this workflow's own step names, and the macOS and ubuntu failure logs from the
same day carry zero U+FFFD — so the transport is fine and the emitter is CI's.

Two guards and a live reproduction:

* the workflow runs pytest under UTF-8 mode, which fixes the emitter;
* the workflow does *not* export UTF-8 to every child, which would disarm the
  tests that reproduce Windows encoding defects by leaving the environment
  alone;
* `.github/scripts/junit_summary.py` prints the full message on a stream it pins
  itself, and that is exercised here under a real cp437 stdio on every leg,
  rather than being taken on trust from the Windows column.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"
REPORTER = REPO / ".github" / "scripts" / "junit_summary.py"

#: A real em dash, not a `\u2014` escape. An escape would leave this file pure
#: ASCII and the reproduction would pass against the defect — the same trap
#: `tests/test_encoding_seam.py` names about locale pins.
EM_DASH = "—"

#: The Windows console codepage the runners actually use. Reproducing it with
#: PYTHONIOENCODING works on any OS, which is the point: this pin does not
#: depend on the Windows legs to fail.
CONSOLE_CP = "cp437"


def _junit(message: str, *, ident: str = "tests.test_thing.test_case") -> str:
    classname, _, name = ident.rpartition(".")
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuites><testsuite name="pytest" tests="1" failures="1">'
        f'<testcase classname="{classname}" name="{name}">'
        f'<failure message="{message}">long traceback body</failure>'
        "</testcase></testsuite></testsuites>\n"
    )


#: Inherited, minus the keys that would decide the question under test. Copying
#: `os.environ` and stripping is `tests/test_encoding_seam.py::_run`'s pattern,
#: and the reason for it is not tidiness: a *minimal* env (`{"PATH": ...}`) took
#: the Windows 3.9 and 3.10 legs down with `Fatal Python error:
#: _Py_HashRandomization_Init: failed to get random numbers`, because CPython
#: reaches the Windows CryptoAPI through `SystemRoot` on those versions. Pinning
#: an env by allowlist means guessing what an interpreter needs to boot on a
#: platform you are not on. Strip what you are testing; inherit the rest.
_ENCODING_KEYS = ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONCOERCECLOCALE",
                  "LC_ALL", "LC_CTYPE", "LANG")


def _clean_env(**overrides: str) -> dict[str, str]:
    import os
    env = dict(os.environ)
    for key in _ENCODING_KEYS:
        env.pop(key, None)
    env.update(overrides)
    return env


def _run_reporter(path: Path, *, console_cp: str | None = None) -> tuple[int, bytes]:
    """The reporter, as CI runs it: a subprocess, output captured as bytes.

    Bytes on purpose — the whole question is which bytes leave the process, and
    decoding here before asserting would hide the defect being pinned.
    """
    env = _clean_env(**({"PYTHONIOENCODING": console_cp} if console_cp else {}))
    proc = subprocess.run(
        [sys.executable, str(REPORTER), str(path)],
        capture_output=True, cwd=str(path.parent), env=env, timeout=60,
    )
    return proc.returncode, proc.stdout + proc.stderr


# --- the live reproduction -------------------------------------------------


def test_the_reporter_keeps_the_glyph_a_cp437_console_cannot_encode(
        tmp_path: Path) -> None:
    """The after-state, reproduced on every leg rather than only on Windows."""
    junit = tmp_path / "junit.xml"
    junit.write_text(_junit(f"AssertionError: left {EM_DASH} right"),
                     encoding="utf-8")
    code, out = _run_reporter(junit, console_cp=CONSOLE_CP)
    assert code == 0, out
    assert EM_DASH.encode("utf-8") in out, out
    assert b"\xef\xbf\xbd" not in out, (
        "the reporter emitted U+FFFD, which is the #546 defect: the character "
        "is destroyed at emit time and nothing downstream can recover it")


def test_the_cp437_reproduction_is_real_and_not_a_no_op() -> None:
    """A control. Without the fix this environment does destroy the character.

    Asserting only the after-state would pass against a reproduction that had
    quietly stopped reproducing anything — which is how a locale pin stops
    testing, per `tests/test_encoding_seam.py`. So the naive route is run too:
    a plain `print()` on a cp437 stream either raises (strict handler, POSIX) or
    substitutes U+FFFD (Windows). Either way the glyph does not survive.
    """
    proc = subprocess.run(
        [sys.executable, "-c", f"print('left {EM_DASH} right')"],
        capture_output=True, timeout=60,
        env=_clean_env(PYTHONIOENCODING=CONSOLE_CP),
    )
    survived = EM_DASH.encode("utf-8") in proc.stdout
    assert not survived, (
        f"PYTHONIOENCODING={CONSOLE_CP} no longer reproduces the Windows "
        "console: an unpinned print kept the em dash, so the sibling test "
        "above proves nothing. Find a codepage that still cannot encode it.")


def test_the_reporter_prints_the_message_pytest_would_elide(tmp_path: Path) -> None:
    """pytest truncates the middle of a long summary line to the terminal width.

    On the real `test_xml_lint` failure the elided part held the word `timeout`,
    which was the diagnosis — `assert 'POST-EDIT LINT FAILED' in 'vim C:\\...`
    then `(...int (5s) ---`. junit.xml carries the whole thing.
    """
    buried = "THE-WORD-THAT-MATTERED"
    message = "AssertionError: " + ("x" * 400) + buried + ("y" * 400)
    junit = tmp_path / "junit.xml"
    junit.write_text(_junit(message), encoding="utf-8")
    code, out = _run_reporter(junit)
    assert code == 0, out
    assert buried.encode() in out, out
    assert b"tests.test_thing.test_case" in out, out


# --- absences that must not read as passes ---------------------------------


def test_a_missing_junit_xml_does_not_read_as_no_failures(tmp_path: Path) -> None:
    code, out = _run_reporter(tmp_path / "junit.xml")
    assert code == 0, out
    assert b"no junit.xml was written" in out or b"did not reach the end" in out, out
    assert b"no failures" not in out, (
        "a file that was never written and a file recording zero failures are "
        "opposite situations; printing one sentence for both is the defect the "
        "step this replaced had")


def test_an_unparseable_junit_xml_says_so_rather_than_nothing(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text("<testsuites><broken", encoding="utf-8")
    code, out = _run_reporter(junit)
    assert code == 0, out
    assert b"not parseable" in out, out
    assert b"which is not the same thing as the suite passing" in out, out


def test_a_clean_run_says_it_found_nothing(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuites><testsuite name="pytest" tests="1" failures="0">'
        '<testcase classname="tests.t" name="ok"/></testsuite></testsuites>\n',
        encoding="utf-8")
    code, out = _run_reporter(junit)
    assert code == 0, out
    assert b"no failures or errors recorded" in out, out


@pytest.mark.parametrize("content", [None, "<broken", "valid"])
def test_the_reporter_can_never_turn_a_green_run_red(
        tmp_path: Path, content: str | None) -> None:
    """It runs under `if: always()` beside the step that owns the verdict."""
    junit = tmp_path / "junit.xml"
    if content == "valid":
        junit.write_text(_junit("boom"), encoding="utf-8")
    elif content is not None:
        junit.write_text(content, encoding="utf-8")
    code, out = _run_reporter(junit)
    assert code == 0, out


# --- the workflow decision, pinned ----------------------------------------


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_the_pytest_invocation_runs_in_utf8_mode() -> None:
    """`-X utf8` on the interpreter that renders the failure lines."""
    lines = [line.strip() for line in _workflow_text().splitlines()
             if "-m pytest" in line or line.strip().startswith("run: pytest")]
    assert lines, "no pytest invocation found in the workflow at all"
    for line in lines:
        assert "-X utf8" in line, (
            f"this pytest invocation renders failure messages on a stream whose "
            f"codec is the runner's console codepage, and #546 is what that "
            f"costs on Windows: {line}")


def test_the_workflow_does_not_export_utf8_to_every_child_process() -> None:
    """The blast-radius half of #546's decision, and the reason for `-X`.

    `PYTHONUTF8`/`PYTHONIOENCODING` in a workflow `env:` reach pytest *and*
    every subprocess it spawns. `tests/test_encoding_seam.py` strips both from
    the environment it hands its children, so it would survive; but
    `tests/test_git_commit_payload_route.py::test_commit_succeeds_on_a_non_utf8_console`
    copies `os.environ` and overrides only `PYTHONIOENCODING`, so an inherited
    `PYTHONUTF8=1` would change what it reproduces. Fixing a reporting defect by
    weakening the tests that would catch its regression is not a trade worth
    making, and `-X utf8` does not make it: a command-line flag is not inherited
    by child processes, an environment variable is.
    """
    live = [line for line in _workflow_text().splitlines()
            if line.split("#", 1)[0].strip()]
    for var in ("PYTHONUTF8", "PYTHONIOENCODING"):
        offenders = [line.strip() for line in live if var in line]
        assert not offenders, (
            f"{var} is inherited by every subprocess the suite spawns, "
            f"including the tests that reproduce Windows encoding defects by "
            f"leaving the environment alone. Use `-X utf8` on the pytest "
            f"invocation instead — same fix, one process: {offenders}")


def test_the_failure_summary_step_uses_the_reporter_and_masks_nothing() -> None:
    text = _workflow_text()
    assert "junit_summary.py junit.xml" in text, (
        "the always-run summary step no longer calls the reporter")
    step = text.split("Show failing tests")[1]
    assert "||" not in step.split("run:")[1].split("\n")[0], (
        "a `|| echo` on this step reports a specific cause — 'pytest crashed "
        "before generating it' — for any failure of the reporter itself, "
        "including one that merely could not encode what it was printing")


def test_the_reporter_exists_where_the_workflow_says_it_does() -> None:
    assert REPORTER.is_file(), REPORTER


def test_the_reporter_pins_its_own_stream_rather_than_trusting_the_environment() -> None:
    """The layer that cannot be bypassed: in-process, not via env.

    The reporter is the one emitter that has to work when everything else about
    the run has already gone wrong, so it must not depend on a variable a runner
    image, a shell profile or a future workflow edit can remove.
    """
    source = REPORTER.read_text(encoding="utf-8")
    assert 'reconfigure(encoding="utf-8"' in source, source[:200]
    assert 'errors="replace"' not in source, (
        "`replace` is what produced the U+FFFD in the CI logs. On a diagnostic "
        "the handler must disclose, not substitute — backslashreplace keeps the "
        "byte recoverable")
