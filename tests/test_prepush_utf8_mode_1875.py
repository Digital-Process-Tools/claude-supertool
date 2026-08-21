"""#1875: the pre-push hook must render failure messages the way CI does.

`.github/workflows/tests.yml` runs pytest under `-X utf8` and does so
deliberately (#546, pinned by `tests/test_ci_encoding_546.py`).
`.githooks/pre-push` ran the same suite without it, so the hook and the
workflow disagreed about the one flag that decides what a non-ASCII failure
message looks like. The hook is the exposed half: CI's Windows leg carries the
flag, a local `git push` on Windows did not.

**What is observed and what is reasoned.** Observed here, on any machine that
has a non-UTF-8 locale: with a latin-1 stdio codec a pytest failure message
containing U+0662 comes out as its six-character backslash-u escape, and adding
`-X utf8` restores the character itself. Reasoned, not observed: that a Windows
console codepage (cp1252/cp437) behaves the same way. Nobody on this project
has reproduced the Windows half directly; #546's CI logs are the closest
evidence and they show a *third* outcome, U+FFFD, described below.

**The issue's stated symptom is wrong and this file does not reproduce it.**
#1875 says the missing flag makes pytest raise `UnicodeEncodeError` while
reporting the failure. It does not: `TerminalWriter.write_raw` in
`_pytest/_io/terminalwriter.py` catches exactly that and re-emits the line
unicode-escaped. So the strict-stream outcome is a *degraded* diagnostic, not a
destroyed one — the character is still recoverable from the escape. The
destroyed outcome #546 actually observed on `windows-latest` is U+FFFD, which
happens on a stream that *substitutes* rather than raising: no exception, so
pytest's fallback never fires and nothing downstream can recover the character.
`-X utf8` prevents both, because it fixes the emitter rather than the message —
which is also why the alternative #1875 offered, `ascii()` at each assertion
site, was rejected. It cannot reach pytest's rendering of *compared values*
(`assert DIGITS.match(probe)` prints the probe with no message involved), and
this tree carries 430 assert messages across 184 test files with a literal
non-ASCII character in them, not the ten the issue estimated.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_pre_push_interpreter_572 import _Sandbox

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / ".githooks" / "pre-push"
WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"

#: A real character, not a backslash-u escape. An escape would leave this file
#: pure ASCII and the reproduction below would pass against the defect — the
#: same trap `tests/test_ci_encoding_546.py` names about locale pins.
PROBE = "٢"  # U+0662 ARABIC-INDIC DIGIT TWO, the #1727/#1748 probe

UTF8_FLAG = "-X utf8"

#: Locales that may yield a stdio codec which cannot encode PROBE. More than
#: one because no single spelling works everywhere: BSD/macOS name it
#: `en_US.ISO8859-1`, glibc wants the C locale with PEP 538 coercion switched
#: off. Each is *probed* rather than assumed — see the selector below.
CANDIDATE_CONSOLES = (
    {"LC_ALL": "en_US.ISO8859-1", "LANG": "en_US.ISO8859-1"},
    {"LC_ALL": "en_US.iso88591", "LANG": "en_US.iso88591"},
    {"LC_ALL": "C", "LANG": "C", "PYTHONCOERCECLOCALE": "0"},
)

#: Stripped from the inherited environment, because each one would decide the
#: question under test. `PYTHONIOENCODING` in particular *beats* `-X utf8`
#: (measured 2026-08-21: under `LC_ALL=en_US.ISO8859-1` plus
#: `PYTHONIOENCODING=cp1252`, `python3 -X utf8 -c "import sys;
#: print(sys.stdout.encoding)"` still prints `cp1252`), so a reproduction built
#: on it could never show the flag working. `tests/test_ci_encoding_546.py`
#: strips the same keys for the same reason: inherit what an interpreter needs
#: in order to boot, strip only what you are testing.
_DECIDING_KEYS = ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONCOERCECLOCALE",
                  "LC_ALL", "LC_CTYPE", "LANG", "PYTEST_ADDOPTS")


def _clean_env(**overrides: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in _DECIDING_KEYS:
        env.pop(key, None)
    env.update(overrides)
    return env


@pytest.fixture
def box(tmp_path: Path) -> _Sandbox:
    """The #572 sandbox: a PATH with nothing on it but git and stub pythons."""
    return _Sandbox(tmp_path)


# ---------------------------------------------------------------------------
# the divergence, pinned on what the hook actually executes
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32",
                    reason=".githooks/pre-push is a bash script")
def test_the_hook_runs_pytest_in_utf8_mode(box: _Sandbox) -> None:
    """Behavioural, not a substring match on the file.

    The stub interpreter logs its own argv, so this asserts against the command
    the hook really issued rather than against a line of text that could be in
    a comment or a branch that never runs.
    """
    box.add_python("python3.12")
    result = box.run()
    assert result.returncode == 0, result.stdout + result.stderr
    suite_runs = [line for line in box.invocations() if "-m pytest" in line]
    assert suite_runs, (
        "the hook never reached the suite at all, so this test proves nothing "
        f"about the flag: {box.invocations()}")
    for line in suite_runs:
        assert UTF8_FLAG in line, (
            "the hook runs the suite without `-X utf8`, so a failure message "
            "carrying a non-ASCII character is rendered on a stream whose "
            "codec is the local console codepage. CI passes the flag "
            f"deliberately (#546); this invocation does not: {line}")


def test_the_hook_and_the_workflow_agree_on_utf8_mode() -> None:
    """The divergence itself — the half of #1875 that is observable anywhere.

    Read off both files rather than off one, so the day CI drops the flag this
    goes red too instead of quietly enforcing a decision nobody makes any more.
    """
    workflow_runs = [line.strip() for line in
                     WORKFLOW.read_text(encoding="utf-8").splitlines()
                     if "-m pytest" in line and not line.strip().startswith("#")]
    hook_runs = [line.strip() for line in
                 HOOK.read_text(encoding="utf-8").splitlines()
                 if "-m pytest" in line and not line.strip().startswith("#")]
    assert workflow_runs, "no pytest invocation found in the workflow at all"
    assert hook_runs, "no pytest invocation found in the hook at all"

    assert all(UTF8_FLAG in line for line in workflow_runs), (
        "the workflow lost `-X utf8`; #546 put it there and "
        f"tests/test_ci_encoding_546.py pins it: {workflow_runs}")
    assert all(UTF8_FLAG in line for line in hook_runs), (
        "the hook and the workflow disagree about `-X utf8`. The hook is the "
        "exposed half: CI's Windows leg carries the flag and a local `git "
        f"push` does not. Hook invocations: {hook_runs}")


def test_the_hook_sets_the_flag_on_the_interpreter_not_in_the_environment() -> None:
    """#546's blast-radius half, restated for the hook.

    A guard, not a fix — it passed before #1875 too. `PYTHONUTF8` or
    `PYTHONIOENCODING` exported by the hook would reach pytest *and* every
    subprocess the suite spawns, including
    `tests/test_git_commit_payload_route.py::test_commit_succeeds_on_a_non_utf8_console`,
    which reproduces a Windows encoding defect by overriding `PYTHONIOENCODING`
    itself. `-X` is a flag on one interpreter and is not inherited.
    """
    live = [line for line in HOOK.read_text(encoding="utf-8").splitlines()
            if line.split("#", 1)[0].strip()]
    for var in ("PYTHONUTF8", "PYTHONIOENCODING"):
        offenders = [line.strip() for line in live if var in line]
        assert not offenders, (
            f"{var} is inherited by every subprocess the suite spawns, "
            "including the tests that reproduce Windows encoding defects by "
            "leaving the environment alone. Use `-X utf8` on the pytest "
            f"invocation instead — same fix, one process: {offenders}")


# ---------------------------------------------------------------------------
# what the flag actually buys, reproduced rather than asserted
# ---------------------------------------------------------------------------

def _stdio_encoding(env_overrides: dict[str, str]) -> str | None:
    """The codec a fresh interpreter uses for stdout under this locale.

    Written to stderr rather than stdout so a locale warning printed on stdout
    cannot be mistaken for the answer.
    """
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.stderr.write(sys.stdout.encoding)"],
        capture_output=True, timeout=60, env=_clean_env(**env_overrides),
    )
    if proc.returncode != 0:
        return None
    return proc.stderr.decode("ascii", "replace").strip().lower()


def _a_console_that_cannot_encode_the_probe() -> dict[str, str] | None:
    """The first candidate locale that really produces a non-UTF-8 stdio codec.

    Probed, never assumed. Measured 2026-08-21 on darwin: `LC_ALL=C` yields a
    UTF-8 codec there whatever `PYTHONCOERCECLOCALE` says, and
    `en_US.ISO8859-1` is not a locale glibc has. Believing either one would
    turn this reproduction into a test that runs and proves nothing.
    """
    for candidate in CANDIDATE_CONSOLES:
        encoding = _stdio_encoding(candidate)
        if encoding is None:
            continue
        try:
            PROBE.encode(encoding)
        except UnicodeEncodeError:
            return candidate
        except LookupError:
            continue
    return None


def _run_failing_suite(tmp_path: Path, *, utf8_mode: bool,
                       console: dict[str, str]) -> bytes:
    """A real pytest run of a real failing test, output captured as bytes.

    Bytes on purpose: the whole question is which bytes leave the process, and
    decoding here before asserting would hide what is being pinned. `cwd` is
    under tmp_path so this repo's own conftest and addopts never load.
    """
    inner = tmp_path / "inner"
    inner.mkdir(exist_ok=True)
    (inner / "test_inner.py").write_text(
        "def test_it_fails():\n"
        "    assert False, \"digit " + PROBE + " was rejected\"\n",
        encoding="utf-8")
    flags = ["-X", "utf8"] if utf8_mode else []
    proc = subprocess.run(
        [sys.executable, *flags, "-m", "pytest", "-p", "no:cacheprovider",
         "--tb=short", "-q", "test_inner.py"],
        cwd=str(inner), capture_output=True, timeout=300,
        env=_clean_env(**console),
    )
    return proc.stdout + proc.stderr


def test_utf8_mode_is_what_decides_whether_the_probe_survives(
        tmp_path: Path) -> None:
    """Both halves assert a presence, so neither can pass on silence.

    Without the flag the escape must be *there* — that is the positive control,
    and it fails loudly if the locale has stopped reproducing anything. With
    the flag the raw character must be there. One pair, one fixture, one
    machine.
    """
    console = _a_console_that_cannot_encode_the_probe()
    if console is None:
        pytest.skip(
            "no locale on this machine yields a stdio codec that cannot encode "
            f"{PROBE!a}; tried {[c['LC_ALL'] for c in CANDIDATE_CONSOLES]}. "
            "UNTESTED HERE: that `-X utf8` changes how a non-ASCII failure "
            "message is rendered. The static pins in this file still ran.")

    raw = PROBE.encode("utf-8")
    #: Exactly what pytest falls back to — `TerminalWriter.write_raw` does this
    #: same `unicode-escape` encode — rather than a hand-typed literal.
    escape = PROBE.encode("unicode-escape")

    without = _run_failing_suite(tmp_path, utf8_mode=False, console=console)
    assert b"was rejected" in without, (
        "the inner suite did not report a failure at all, so neither half of "
        f"this pair means anything: {without!r}")
    assert escape in without, (
        "positive control: under a stdio codec that cannot encode the probe, "
        "pytest falls back to unicode-escape (TerminalWriter.write_raw in "
        "_pytest/_io/terminalwriter.py). It did not, so this locale no longer "
        "reproduces anything and the assertion below proves nothing: "
        f"{without!r}")
    assert raw not in without, (
        f"the raw character survived without `-X utf8` under {console}, so "
        f"there is no rendering difference here for the flag to make: {without!r}")

    with_flag = _run_failing_suite(tmp_path, utf8_mode=True, console=console)
    assert b"was rejected" in with_flag, (
        f"the inner suite did not report a failure at all: {with_flag!r}")
    assert raw in with_flag, (
        "`-X utf8` did not restore the character, which is the entire reason "
        f"the hook passes it: {with_flag!r}")
    assert escape not in with_flag, (
        f"still escaped under `-X utf8`: {with_flag!r}")
