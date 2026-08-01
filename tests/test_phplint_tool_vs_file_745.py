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

## Two layers, because the fixture cannot be portable and the rule must be

The rule itself is string classification over `php`'s output. It is
platform-independent by construction, so it is tested **in process** against
real `php -l` transcripts, on every platform, with no subprocess at all. That
is the layer that has to hold everywhere, and it does.

Driving the whole adapter needs a `php` that fails on demand, which means a
fake one on `PATH` — and that fixture is **POSIX-only, for a reason worth
writing down rather than working around.** `phplint` spawns `["php", "-l", f]`,
a list, so Python calls `CreateProcess` with an extensionless program name. That
API appends `.exe` and **does not consult `PATHEXT`**, so a `php.bat` shim in
the first `PATH` entry is invisible to it — the search walks straight past and
finds the real `php.exe` the `windows-latest` runner image ships.

The first version of this file did exactly that, and the tell was
unmistakable: two of its ten cases *passed* — precisely the two a genuine
healthy `php` linting a valid file also passes — while the rest reported
`ok=True count=0` for inputs designed to fail. A green there would have meant a
fake nobody ran, which is why `test_the_fake_php_is_the_one_that_answers`
exists below: an unintercepted shim yields a *clean* verdict, and a clean
verdict is indistinguishable from a pass unless something asserts otherwise.

Making it work on Windows would mean shipping a compiled `php.exe` in the test
tree, or changing the adapter's own binary resolution to suit a test. Neither
is worth it. What Windows would add over the POSIX legs is coverage of the
spawn and decode path, and the pre-existing `test_phplint_adapter_*` cases in
`test_validators.py` already exercise that there against real `php`. The
classification — the thing this issue is about — is proven on every platform by
the in-process layer.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _adapter_budget import adapter_budget  # noqa: E402
from _adapter_verdict import describe, verdict  # noqa: E402

PHPLINT = Path(__file__).parent.parent / "validators" / "phplint" / "phplint.py"

_spec = importlib.util.spec_from_file_location("phplint_745", PHPLINT)
assert _spec and _spec.loader
phplint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(phplint)

sys.path.insert(0, str(PHPLINT.parent.parent / "common"))
from refusal import tool_fault  # noqa: E402


# Real `php -l` transcripts. Everything below is output PHP actually emits;
# nothing here is invented to make a branch reachable.

# A startup warning. Note the `in Unknown on line 0` tail — PHP's marker for a
# message about the interpreter rather than about any file. It is printed
# BEFORE any parse error, so a bare `on line (\d+)` search over the output
# reaches it first, and that is how a parse error on line 3 was reported at 0.
STARTUP_WARNING = ("PHP Warning:  PHP Startup: Unable to load dynamic library "
                   "'sodium.so' in Unknown on line 0\n")
# `php -l` on a path it cannot read. Exit 1, and no diagnostic of any kind.
NO_INPUT_FILE = "Could not open input file: subject.php\n"
PARSE_ERROR = ("\nParse error: syntax error, unexpected token \"{\" in "
               "subject.php on line 3\nErrors parsing subject.php\n")
# `php -l` reports compile-time fatals — redeclarations, illegal inheritance —
# under `Fatal error:` rather than `Parse error:`. Still a finding.
FATAL_ERROR = "\nFatal error: Cannot redeclare foo() in subject.php on line 4\n"
# Some SAPIs prefix the diagnostic with `PHP `; hence a search, not a match.
PHP_PREFIXED = ("PHP Parse error:  syntax error, unexpected end of file in "
                "subject.php on line 7\n")
CLEAN = "No syntax errors detected in subject.php\n"


# --- layer 1: the rule, in process, on every platform ----------------------

@pytest.mark.parametrize("out, why", [
    (STARTUP_WARNING, "an extension that failed to load says nothing about the file"),
    (NO_INPUT_FILE, "a path php could not open was never parsed"),
    ("totally unrecognised babble\n", "output in no shape the classifier knows"),
    ("", "a process that died without a word"),
])
def test_output_with_no_diagnostic_is_not_a_verdict_about_the_file(out: str, why: str) -> None:
    line = phplint.diagnostic_line(out)
    assert phplint.spoke_about_file(out, line) is False, why
    assert line is None, f"a line number was invented for: {out!r}"


@pytest.mark.parametrize("out, expected_line", [
    (PARSE_ERROR, 3),
    (FATAL_ERROR, 4),
    (PHP_PREFIXED, 7),
    # Both failures at once — the shape a broken PHP install produces on a
    # genuinely broken file. The warning is printed first and carries its own
    # `on line 0`; it must not be allowed to donate that to the real finding.
    (STARTUP_WARNING + PARSE_ERROR, 3),
])
def test_a_diagnostic_is_a_finding_and_keeps_its_own_line(out: str, expected_line: int) -> None:
    line = phplint.diagnostic_line(out)
    assert line == expected_line, f"wrong line read from: {out!r}"
    assert phplint.spoke_about_file(out, line) is True


def test_a_lint_verdict_with_no_line_is_still_a_finding() -> None:
    """`Errors parsing <file>` alone carries no location. Absence of a line is
    not absence of a verdict — the file was read and rejected."""
    out = "Errors parsing subject.php\n"
    line = phplint.diagnostic_line(out)
    assert line is None
    assert phplint.spoke_about_file(out, line) is True


def test_ambiguity_falls_towards_the_file() -> None:
    """A located message in no banner shape the classifier recognises is still
    counted as a finding.

    This is the deliberate direction, not an accident of the regex. A PHP whose
    message shape nobody anticipated must not have its findings relabelled out
    of a caller's error list; the reverse mistake is bounded, because an
    `adapter` result still fails loudly and still prints the raw output.
    """
    out = "some unanticipated diagnostic in subject.php on line 12\n"
    line = phplint.diagnostic_line(out)
    assert line == 12
    assert phplint.spoke_about_file(out, line) is True


def test_the_fault_message_names_the_exit_code_and_the_output() -> None:
    msg = tool_fault("php -l", 78, "totally unrecognised babble")
    assert "78" in msg and "babble" in msg


def test_the_fault_message_never_renders_blank() -> None:
    """A tool that dies without a word is the input a message template built
    around the output renders as an empty tail. It is stated instead."""
    msg = tool_fault("php -l", 139, "")
    assert "139" in msg
    assert msg.strip()
    assert "no output" in msg


# --- layer 2: the whole adapter, driven by a fake php on PATH --------------
#
# POSIX only. See the module docstring: `CreateProcess` ignores `PATHEXT` for an
# extensionless program name, so a `php.bat` shim cannot intercept the spawn on
# Windows and the runner's real `php.exe` answers instead.

posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason=("a fake `php` on PATH cannot intercept phplint's extensionless "
            "list spawn on Windows: CreateProcess appends .exe and ignores "
            "PATHEXT, so a php.bat shim is skipped and the runner's real "
            "php.exe answers. The classification these cases drive is covered "
            "in process above, on every platform."))


def _fake_php(tmp_path: Path, *, exit_code: int, stdout: str) -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "fake_php.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    launcher = bindir / "php"
    launcher.write_text(
        "#!/bin/sh\n"
        f"exec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8")
    launcher.chmod(0o755)
    return bindir


def _spawn(bindir: Path, target: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [sys.executable, str(PHPLINT), str(target)],
        capture_output=True, text=True, env=env,
        timeout=adapter_budget(PHPLINT),
    )


def _run(bindir: Path, target: Path) -> dict:
    return verdict(_spawn(bindir, target), adapter="phplint")


def _target(tmp_path: Path) -> Path:
    f = tmp_path / "subject.php"
    f.write_text("<?php\n$x = 1;\n$y = 2;\n$z = 3;\n", encoding="utf-8")
    return f


@posix_only
def test_the_fake_php_is_the_one_that_answers(tmp_path: Path) -> None:
    """The guard the first version of this file lacked.

    Every case below is meaningless if the shim is not the binary that ran, and
    a shim that is skipped produces a *clean* verdict — indistinguishable from
    a pass. So one case asserts the interception itself, with an exit code and
    a message no real `php` would ever produce.
    """
    bindir = _fake_php(tmp_path, exit_code=42, stdout="I-AM-THE-FAKE\n")
    data = _run(bindir, _target(tmp_path))
    assert data["ok"] is False, describe(data)
    assert "I-AM-THE-FAKE" in data["errors"][0]["msg"], describe(data)


@posix_only
def test_extension_load_failure_is_an_adapter_fault_not_a_parse_error(tmp_path: Path) -> None:
    bindir = _fake_php(tmp_path, exit_code=1, stdout=STARTUP_WARNING)
    data = _run(bindir, _target(tmp_path))
    assert data["errors"][0]["code"] == "adapter", describe(data)
    assert data["errors"][0]["line"] is None, describe(data)


@posix_only
def test_could_not_open_input_file_is_an_adapter_fault(tmp_path: Path) -> None:
    bindir = _fake_php(tmp_path, exit_code=1, stdout=NO_INPUT_FILE)
    data = _run(bindir, _target(tmp_path))
    assert data["errors"][0]["code"] == "adapter", describe(data)


@posix_only
def test_a_tool_fault_still_fails_loudly_and_names_the_exit_code(tmp_path: Path) -> None:
    """`adapter` reclassifies; it must never silence. This is what makes the
    ambiguous call cheap in both directions."""
    bindir = _fake_php(tmp_path, exit_code=78, stdout="totally unrecognised babble\n")
    data = _run(bindir, _target(tmp_path))
    assert data["ok"] is False, describe(data)
    assert data["count"] == 1, describe(data)
    assert len(data["errors"]) == 1, describe(data)
    assert data["errors"][0]["severity"] == "error", describe(data)
    assert data["errors"][0]["code"] == "adapter", describe(data)
    msg = data["errors"][0]["msg"]
    assert "78" in msg and "babble" in msg, msg


@posix_only
def test_silent_non_zero_exit_says_so_rather_than_rendering_blank(tmp_path: Path) -> None:
    bindir = _fake_php(tmp_path, exit_code=139, stdout="")
    data = _run(bindir, _target(tmp_path))
    assert data["errors"][0]["code"] == "adapter", describe(data)
    assert "139" in data["errors"][0]["msg"], describe(data)
    assert data["errors"][0]["msg"].strip(), "the message must never be blank"


@posix_only
def test_a_real_parse_error_keeps_code_parse_and_its_line(tmp_path: Path) -> None:
    bindir = _fake_php(tmp_path, exit_code=255, stdout=PARSE_ERROR)
    data = _run(bindir, _target(tmp_path))
    assert data["ok"] is False, describe(data)
    assert data["errors"][0]["code"] == "parse", describe(data)
    assert data["errors"][0]["line"] == 3, describe(data)
    assert data["errors"][0]["source_context"], describe(data)


@posix_only
def test_a_startup_warning_does_not_steal_the_parse_error_line(tmp_path: Path) -> None:
    bindir = _fake_php(tmp_path, exit_code=255,
                       stdout=STARTUP_WARNING + PARSE_ERROR)
    data = _run(bindir, _target(tmp_path))
    assert data["errors"][0]["code"] == "parse", describe(data)
    assert data["errors"][0]["line"] == 3, describe(data)


@posix_only
def test_a_fatal_error_from_the_linter_is_a_finding_about_the_file(tmp_path: Path) -> None:
    bindir = _fake_php(tmp_path, exit_code=255, stdout=FATAL_ERROR)
    data = _run(bindir, _target(tmp_path))
    assert data["errors"][0]["code"] == "parse", describe(data)
    assert data["errors"][0]["line"] == 4, describe(data)


@posix_only
def test_a_clean_lint_is_untouched(tmp_path: Path) -> None:
    bindir = _fake_php(tmp_path, exit_code=0, stdout=CLEAN)
    data = _run(bindir, _target(tmp_path))
    assert data["ok"] is True, describe(data)
    assert data["count"] == 0, describe(data)


@posix_only
def test_the_verdict_json_is_the_only_thing_on_stdout(tmp_path: Path) -> None:
    """SCHEMA.md: one JSON object on stdout, nothing else — including on the
    new branch, whose input is arbitrary tool output that must not leak raw."""
    bindir = _fake_php(tmp_path, exit_code=1, stdout="noise\nmore noise\n")
    r = _spawn(bindir, _target(tmp_path))
    assert r.returncode == 0, r.stderr
    json.loads(r.stdout.strip())
    assert r.stdout.strip().count("\n") == 0, r.stdout


# --- the reclassification has to reach the cache ---------------------------

def test_an_adapter_fault_is_not_cached() -> None:
    """Naming the fault correctly only helps if the core stops freezing it.

    The validator cache is keyed on the file's content hash. A `php` that
    cannot load an extension for ten minutes is not a property of the file, so
    caching that red replays it on every later run until someone edits the file
    — the shape of the 2100-poisoned-entries incident
    `_validator_result_is_cacheable` was written for. Before this issue such an
    exit reached the cache wearing `code: "parse"`, which reads as a real
    finding and is cached on purpose.
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
