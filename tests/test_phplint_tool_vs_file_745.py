"""phplint must not blame the file for `php` falling over (#745).

`php -l` exits non-zero for two categories of reason, and the adapter used to
collapse them into one:

- it read the file and the file does not parse — a finding, `code: "parse"`;
- it never got as far as a verdict about the file (interpreter cannot start,
  an extension fails to load fatally, the path could not be opened at all) —
  not a finding about the file, and publishing it as `code: "parse"` tells
  someone their good file is broken and points at a line the adapter invented.

The rule implemented: the tool is taken to have spoken about the file only when
its output carries a recognisable PHP lint diagnostic — the linter's own
`Errors parsing <file>` verdict line, or a `Parse error:` / `Fatal error:`
banner. Anything else on a non-zero exit is `code: "adapter"`, still `ok:
False` and still one error, so nothing vanishes from a caller's error list; the
message names the exit code and the raw output so the reader can see why.

Fixtures are a fake `php` on PATH. The failures that matter here are exactly
the ones a real, healthy PHP cannot be made to produce on demand, and stubbing
only the spawned binary leaves every line of the adapter under test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _adapter_budget import adapter_budget  # noqa: E402
from _adapter_verdict import describe, verdict  # noqa: E402

PHPLINT = Path(__file__).parent.parent / "validators" / "phplint" / "phplint.py"


def _fake_php(tmp_path: Path, *, exit_code: int, stdout: str) -> Path:
    """A `php` on PATH that prints `stdout` and exits `exit_code`.

    Written as a Python script plus a per-platform launcher so the same fixture
    works on POSIX and on Windows, where `subprocess.run(["php", ...])` resolves
    through PATHEXT rather than a shebang.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "fake_php.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        launcher = bindir / "php.bat"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = bindir / "php"
        launcher.write_text(
            "#!/bin/sh\n"
            f"exec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8")
        launcher.chmod(0o755)
    return bindir


def _run(tmp_path: Path, bindir: Path, target: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    r = subprocess.run(
        [sys.executable, str(PHPLINT), str(target)],
        capture_output=True, text=True, env=env,
        timeout=adapter_budget(PHPLINT),
    )
    return verdict(r, adapter="phplint")


def _target(tmp_path: Path) -> Path:
    f = tmp_path / "subject.php"
    f.write_text("<?php\n$x = 1;\n$y = 2;\n$z = 3;\n", encoding="utf-8")
    return f


# --- the tool fell over: not a finding about the file ----------------------

def test_extension_load_failure_is_an_adapter_fault_not_a_parse_error(tmp_path: Path) -> None:
    """The honest shape of "php exited 1 and never looked at your file".

    Note the `in Unknown on line 0` tail: the startup warning carries an
    `on line N` of its own, which the old regex matched happily. So this exit
    did not merely become `code: "parse"` — it became a parse error located at
    line 0 of a file PHP never opened.
    """
    bindir = _fake_php(
        tmp_path, exit_code=1,
        stdout=("PHP Warning:  PHP Startup: Unable to load dynamic library "
                "'sodium.so' in Unknown on line 0\n"))
    data = _run(tmp_path, bindir, _target(tmp_path))

    assert data["errors"][0]["code"] == "adapter", describe(data)
    assert data["errors"][0]["line"] is None, describe(data)


def test_could_not_open_input_file_is_an_adapter_fault(tmp_path: Path) -> None:
    """`php -l` answers a missing or unreadable path with exit 1 and this line.

    Real PHP, no fixture needed to produce it — and it says nothing about the
    syntax of anything. Calling it `parse` reports a syntax error in a file
    that was never read.
    """
    bindir = _fake_php(
        tmp_path, exit_code=1,
        stdout="Could not open input file: subject.php\n")
    data = _run(tmp_path, bindir, _target(tmp_path))

    assert data["errors"][0]["code"] == "adapter", describe(data)


def test_a_tool_fault_still_fails_loudly_and_names_the_exit_code(tmp_path: Path) -> None:
    """`adapter` reclassifies; it must never silence.

    This is what makes the ambiguous call cheap in both directions: a
    misclassified parse error is still one error, still `ok: False`, still
    rollback-triggering. Only the label and the invented line move.
    """
    bindir = _fake_php(tmp_path, exit_code=78, stdout="totally unrecognised babble\n")
    data = _run(tmp_path, bindir, _target(tmp_path))

    assert data["ok"] is False, describe(data)
    assert data["count"] == 1, describe(data)
    assert len(data["errors"]) == 1, describe(data)
    assert data["errors"][0]["severity"] == "error", describe(data)
    assert data["errors"][0]["code"] == "adapter", describe(data)
    msg = data["errors"][0]["msg"]
    assert "78" in msg, f"the exit code is missing from: {msg}"
    assert "babble" in msg, f"the raw output is missing from: {msg}"


def test_silent_non_zero_exit_says_so_rather_than_rendering_blank(tmp_path: Path) -> None:
    """A tool that dies without a word is the case a message template built
    around the output renders as an empty string. The reader gets the exit code
    and an explicit statement that there was no output."""
    bindir = _fake_php(tmp_path, exit_code=139, stdout="")
    data = _run(tmp_path, bindir, _target(tmp_path))

    assert data["errors"][0]["code"] == "adapter", describe(data)
    assert "139" in data["errors"][0]["msg"], describe(data)
    assert data["errors"][0]["msg"].strip(), "the message must never be blank"


# --- the tool spoke about the file: still a parse error --------------------

def test_a_real_parse_error_keeps_code_parse_and_its_line(tmp_path: Path) -> None:
    """The regression guard. Reclassifying is only safe if the classifier does
    not sweep genuine findings into `adapter`."""
    bindir = _fake_php(
        tmp_path, exit_code=255,
        stdout=("Parse error: syntax error, unexpected token \"{\" in "
                "subject.php on line 3\nErrors parsing subject.php\n"))
    data = _run(tmp_path, bindir, _target(tmp_path))

    assert data["ok"] is False, describe(data)
    assert data["errors"][0]["code"] == "parse", describe(data)
    assert data["errors"][0]["line"] == 3, describe(data)


def test_a_startup_warning_does_not_steal_the_parse_error_line(tmp_path: Path) -> None:
    """Both failures at once — the shape a real broken PHP install produces on
    a genuinely broken file. `in Unknown on line 0` is printed first, so a bare
    `on line (\\d+)` search reports the parse error at line 0 and renders
    source context for a line that does not exist."""
    bindir = _fake_php(
        tmp_path, exit_code=255,
        stdout=("PHP Warning:  PHP Startup: Unable to load dynamic library "
                "'sodium.so' in Unknown on line 0\n"
                "Parse error: syntax error, unexpected end of file in "
                "subject.php on line 3\nErrors parsing subject.php\n"))
    data = _run(tmp_path, bindir, _target(tmp_path))

    assert data["errors"][0]["code"] == "parse", describe(data)
    assert data["errors"][0]["line"] == 3, describe(data)


def test_a_fatal_error_from_the_linter_is_a_finding_about_the_file(tmp_path: Path) -> None:
    """`php -l` reports compile-time fatals (a redeclared function, an illegal
    inheritance) as `Fatal error:`, not `Parse error:`. Those are findings."""
    bindir = _fake_php(
        tmp_path, exit_code=255,
        stdout=("Fatal error: Cannot redeclare foo() in subject.php on line 4\n"))
    data = _run(tmp_path, bindir, _target(tmp_path))

    assert data["errors"][0]["code"] == "parse", describe(data)
    assert data["errors"][0]["line"] == 4, describe(data)


def test_a_clean_lint_is_untouched(tmp_path: Path) -> None:
    bindir = _fake_php(
        tmp_path, exit_code=0, stdout="No syntax errors detected in subject.php\n")
    data = _run(tmp_path, bindir, _target(tmp_path))

    assert data["ok"] is True, describe(data)
    assert data["count"] == 0, describe(data)


def test_the_verdict_json_is_the_only_thing_on_stdout(tmp_path: Path) -> None:
    """SCHEMA.md: one JSON object on stdout, nothing else — including on the
    new branch, whose input is arbitrary tool output that must not leak raw."""
    bindir = _fake_php(tmp_path, exit_code=1, stdout="noise\nmore noise\n")
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    r = subprocess.run(
        [sys.executable, str(PHPLINT), str(_target(tmp_path))],
        capture_output=True, text=True, env=env, timeout=adapter_budget(PHPLINT))
    assert r.returncode == 0, r.stderr
    json.loads(r.stdout.strip())
    assert r.stdout.strip().count("\n") == 0, r.stdout

# --- the reclassification has to reach the cache ---------------------------

def test_an_adapter_fault_is_not_cached() -> None:
    """Naming the fault correctly only helps if the core stops freezing it.

    The validator cache is keyed on the file's content hash. A `php` that
    cannot load an extension for ten minutes is not a property of the file, so
    caching that red replays it on every later run until someone edits the file
    — the shape of the 2100-poisoned-entries incident `_validator_result_is_cacheable`
    was written for. Before this issue such an exit reached the cache wearing
    `code: "parse"`, which reads as a real finding and is cached on purpose.
    """
    import supertool

    fault = {"ok": False, "count": 1, "errors": [
        {"line": None, "col": None, "severity": "error", "code": "adapter",
         "msg": "php -l exited 1 without reporting anything about the file"}]}
    assert supertool._validator_result_is_cacheable(fault) is False

    finding = {"ok": False, "count": 1, "errors": [
        {"line": 3, "col": None, "severity": "error", "code": "parse",
         "msg": "Parse error: syntax error"}]}
    assert supertool._validator_result_is_cacheable(finding) is True
