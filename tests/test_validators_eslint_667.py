"""The eslint validator adapter (#667), and the four ways it can say nothing.

Three of the four are absences, and every one of them arrives looking like a
clean file unless the adapter goes out of its way:

| what happened | what eslint does | what it must render as |
|---|---|---|
| eslint not installed | nothing runs | `skipped` |
| eslint installed, no config | exit 2, **stdout empty**, message on stderr | `skipped` |
| file matched an ignore pattern | exit **0**, one `ruleId: null` warning, no findings | `skipped` |
| file has findings | exit 1, JSON array | `ok: false` |

The second is the trap #667 names. The third is not in the issue and is worse,
because it exits *zero*: an adapter that reads only `messages[]` for real rules
publishes `ok: true, count: 0` about a file eslint refused to lint. Verified
against eslint 10.8.0, whose exact reply is reproduced in `IGNORED_MESSAGE`.

**No fallback config is shipped, and `test_no_config_declines` is what holds
that line.** Inventing one would make the validator report rules the project
never adopted — the argument already written into `validators/ruff/ruff.py`,
and the first thing anyone does about findings they did not opt into is switch
the validator off. A repo that wants JS linted adds `eslint.config.js`; until
it does, the honest answer is that nobody checked.

The config-absent and ignore paths are driven by a fake `eslint` on PATH
reproducing the real streams, so they run without a node toolchain. The
real-eslint cases are gated on availability.
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
ADAPTER = REPO / "validators" / "eslint" / "eslint.py"

#: eslint 10.8.0, `eslint -f json a.js` in a directory with no flat config.
#: Exit 2, stdout empty, this on stderr.
NO_CONFIG_STDERR = """
Oops! Something went wrong! :(

ESLint: 10.8.0

ESLint couldn't find an eslint.config.(js|mjs|cjs) file.

From ESLint v9.0.0, the default configuration file is now eslint.config.js.
"""

#: eslint 10.8.0 on a file under `node_modules/`. Exit 0 — the whole problem.
IGNORED_MESSAGE = (
    "File ignored by default because it is located under the node_modules "
    'directory. Use ignore pattern "!**/node_modules/" to disable file ignore '
    'settings or use "--no-warn-ignored" to suppress this warning.'
)

posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason=("a fake binary on PATH cannot intercept an extensionless list "
            "spawn on Windows: CreateProcess appends .exe and ignores PATHEXT"),
)

needs_eslint = pytest.mark.skipif(
    not shutil.which("eslint"),
    reason="eslint not on PATH",
)


def _spawn(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ADAPTER), *args],
        capture_output=True, text=True, env=env,
        timeout=adapter_budget(ADAPTER), encoding="utf-8", errors="replace",
    )


def _run(path: Path, env: dict | None = None) -> dict:
    return verdict(_spawn(str(path), env=env), adapter=ADAPTER.name)


def _fake_eslint(tmp_path: Path, *, exit_code: int, stdout: str = "",
                 stderr: str = "") -> dict:
    """An `eslint` on PATH printing the given streams. Returns an env dict."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "fake_eslint.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n", encoding="utf-8")
    launcher = bindir / "eslint"
    launcher.write_text(
        "#!/bin/sh\n"
        f"exec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8")
    launcher.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    return env


def _js(tmp_path: Path, body: str = "var x = 1\n", name: str = "a.js") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Absence one: not installed
# ---------------------------------------------------------------------------

def test_missing_eslint_is_the_third_state(tmp_path: Path) -> None:
    out = _run(_js(tmp_path), env=empty_path_env())
    assert "skipped" in out, describe(out)
    assert "eslint" in out["skipped"]
    for key in ("ok", "count", "errors"):
        assert key not in out, f"a skip must not carry {key!r}: {out}"
    assert out["tool"] == "eslint"
    assert isinstance(out["duration_ms"], int)


def test_required_turns_the_absent_tool_into_a_loud_error(tmp_path: Path) -> None:
    env = empty_path_env()
    env["SUPERTOOL_REQUIRE_VALIDATORS"] = "eslint"
    out = _run(_js(tmp_path), env=env)
    assert "skipped" not in out, describe(out)
    assert_declined(out, context="a required validator whose tool is absent")
    assert "SUPERTOOL_REQUIRE_VALIDATORS" in out["errors"][0]["msg"]


# ---------------------------------------------------------------------------
# Absence two: installed, no resolvable config — #667's trap
# ---------------------------------------------------------------------------

@posix_only
def test_no_config_declines_rather_than_inventing_one(tmp_path: Path) -> None:
    """Exit 2 with empty stdout must not become `count: 0`.

    And it must not become a fallback ruleset either: a validator that lints
    against rules the repo never chose reports findings nobody agreed to, and
    gets switched off inside a day.
    """
    env = _fake_eslint(tmp_path, exit_code=2, stdout="", stderr=NO_CONFIG_STDERR)
    out = _run(_js(tmp_path), env=env)
    assert "skipped" in out, describe(out)
    assert "config" in out["skipped"].lower(), out["skipped"]
    for key in ("ok", "count", "errors"):
        assert key not in out, f"a skip must not carry {key!r}: {out}"


@posix_only
def test_no_config_is_loud_when_required(tmp_path: Path) -> None:
    env = _fake_eslint(tmp_path, exit_code=2, stdout="", stderr=NO_CONFIG_STDERR)
    env["SUPERTOOL_REQUIRE_VALIDATORS"] = "eslint"
    out = _run(_js(tmp_path), env=env)
    assert "skipped" not in out, describe(out)
    assert_declined(out, context="a required validator with no config")


# ---------------------------------------------------------------------------
# Absence three: eslint ran, exited 0, and linted nothing
# ---------------------------------------------------------------------------

@posix_only
def test_an_ignored_file_declines_even_though_eslint_exited_zero(
        tmp_path: Path) -> None:
    """The absence #667 does not mention, and the one that reads greenest.

    `ruleId: null`, `fatal: false`, no `line` — and exit 0. Dropping the row
    as "not a rule violation" leaves `errors: []` and publishes a clean
    verdict about a file eslint declined to look at.
    """
    payload = json.dumps([{
        "filePath": str(tmp_path / "a.js"),
        "messages": [{"ruleId": None, "fatal": False, "severity": 1,
                      "message": IGNORED_MESSAGE}],
        "suppressedMessages": [], "errorCount": 0, "warningCount": 1,
        "fatalErrorCount": 0,
    }])
    env = _fake_eslint(tmp_path, exit_code=0, stdout=payload)
    out = _run(_js(tmp_path), env=env)
    assert "skipped" in out, describe(out)
    assert "ignore" in out["skipped"].lower(), out["skipped"]


# ---------------------------------------------------------------------------
# The verdict path
# ---------------------------------------------------------------------------

@posix_only
def test_findings_map_onto_the_schema(tmp_path: Path) -> None:
    payload = json.dumps([{
        "filePath": str(tmp_path / "a.js"),
        "messages": [
            {"ruleId": "no-var", "severity": 2, "line": 1, "column": 1,
             "message": "Unexpected var, use let or const instead."},
            {"ruleId": "eqeqeq", "severity": 1, "line": 2, "column": 7,
             "message": "Expected '===' and instead saw '=='."},
        ],
        "errorCount": 1, "warningCount": 1, "fatalErrorCount": 0,
    }])
    env = _fake_eslint(tmp_path, exit_code=1, stdout=payload)
    out = _run(_js(tmp_path, "var x = 1\nif (x == 1) {}\n"), env=env)
    assert_declined(out, context="a file with two findings")
    assert out["count"] == 2, describe(out)
    by_code = {e["code"]: e for e in out["errors"]}
    assert by_code["no-var"]["severity"] == "error", describe(out)
    assert by_code["eqeqeq"]["severity"] == "warning", describe(out)
    assert by_code["no-var"]["line"] == 1
    assert by_code["no-var"]["col"] == 1
    assert by_code["no-var"]["source_context"], describe(out)


@posix_only
def test_a_parse_error_is_an_error(tmp_path: Path) -> None:
    """`fatal: true` with a null ruleId is a broken file, not an ignore."""
    payload = json.dumps([{
        "filePath": str(tmp_path / "a.js"),
        "messages": [{"ruleId": None, "fatal": True, "severity": 2,
                      "message": "Parsing error: Unexpected token ;",
                      "line": 1, "column": 11}],
        "errorCount": 1, "warningCount": 0, "fatalErrorCount": 1,
    }])
    env = _fake_eslint(tmp_path, exit_code=1, stdout=payload)
    out = _run(_js(tmp_path, "const x = ;\n"), env=env)
    assert_declined(out, context="a file that does not parse")
    err = out["errors"][0]
    assert err["severity"] == "error", describe(out)
    assert err["line"] == 1
    assert "parsing error" in err["msg"].lower(), err["msg"]


@posix_only
def test_a_clean_file_is_clean(tmp_path: Path) -> None:
    payload = json.dumps([{
        "filePath": str(tmp_path / "a.js"), "messages": [],
        "suppressedMessages": [], "errorCount": 0, "warningCount": 0,
        "fatalErrorCount": 0,
    }])
    env = _fake_eslint(tmp_path, exit_code=0, stdout=payload)
    out = _run(_js(tmp_path, "const x = 1;\n"), env=env)
    assert_ok(out)
    assert out["count"] == 0
    assert out["errors"] == []


@posix_only
def test_an_unexplained_failure_stays_loud(tmp_path: Path) -> None:
    """An exit the adapter cannot explain is a fault, not a third state.

    Swallowing an unknown failure is the same category mistake as reporting a
    skip as `ok` — it just points the other way.
    """
    env = _fake_eslint(tmp_path, exit_code=2, stdout="",
                       stderr="Cannot find module 'eslint-plugin-nope'\n")
    out = _run(_js(tmp_path), env=env)
    assert "skipped" not in out, describe(out)
    assert_declined(out, context="an unexplained eslint failure")
    err = out["errors"][0]
    assert err["code"] == "adapter", describe(out)
    assert "2" in err["msg"]
    assert "eslint-plugin-nope" in err["msg"], err["msg"]


@posix_only
def test_unparseable_stdout_is_an_adapter_error(tmp_path: Path) -> None:
    env = _fake_eslint(tmp_path, exit_code=1, stdout="not json at all\n")
    out = _run(_js(tmp_path), env=env)
    assert_declined(out, context="eslint stdout that is not JSON")
    assert out["errors"][0]["code"] == "adapter", describe(out)


def test_no_file_arg() -> None:
    out = verdict(_spawn(), adapter=ADAPTER.name)
    assert_declined(out, context="no file argument")
    assert out["errors"][0]["code"] == "adapter"


# ---------------------------------------------------------------------------
# Absence one, on the machine it actually happens on: npx present, eslint not
# ---------------------------------------------------------------------------

#: npm 11's real reply to `npx --no-install eslint` where eslint is not
#: installed: exit 1, **stdout empty**, this on stderr. Older npm says
#: "could not determine executable to run" for the same condition.
NPX_UNKNOWN_STDERR = (
    'Unknown command: "eslint"\n'
    "To see a list of supported npm commands, run:\n  npm help\n"
)
NPX_NO_EXECUTABLE_STDERR = "npm error could not determine executable to run\n"

#: npm 10.9.4, reproduced on this machine (#1948): npx refuses BEFORE it ever
#: tries to resolve a command, so neither spelling above matches. `--no` and
#: `--no-install` give the identical message and exit code -- the flag is not
#: what changed, only npm's own wording did.
NPX_CANCELED_STDERR = (
    'npm error npx canceled due to missing packages and no YES option: '
    '["eslint@10.9.0"]\n'
)


def _fake_npx(tmp_path: Path, stderr: str) -> dict:
    """`npx` on PATH and no `eslint` — the common laptop.

    The bin dir is the *whole* PATH, so `shutil.which("eslint")` genuinely
    fails and the adapter takes its documented npx fallback.
    """
    bindir = tmp_path / "npxbin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "fake_npx.py"
    script.write_text(
        "import sys\n"
        f"sys.stderr.write({stderr!r})\n"
        "sys.exit(1)\n", encoding="utf-8")
    launcher = bindir / "npx"
    launcher.write_text(
        "#!/bin/sh\n"
        f"exec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8")
    launcher.chmod(0o755)
    env = empty_path_env()
    env["PATH"] = str(bindir)
    return env


@posix_only
@pytest.mark.parametrize("stderr", [NPX_UNKNOWN_STDERR,
                                    NPX_NO_EXECUTABLE_STDERR,
                                    NPX_CANCELED_STDERR],
                         ids=["npm11", "npm8", "npm10_9_4"])
def test_npx_without_eslint_is_an_absent_eslint_not_a_failed_one(
        tmp_path: Path, stderr: str) -> None:
    """Case 1 of this module's docstring, on the machine it happens on.

    `shutil.which("eslint")` is false and `shutil.which("npx")` is true on
    every laptop with node — so `_resolve_cmd` returns the npx fallback,
    `base` is truthy, and the INSTALL_HINT branch is never reached. npx then
    exits 1 with empty stdout, misses `_NO_CONFIG`, and lands on
    `_adapter_error`. The reader is told eslint *failed*, which sends them to
    debug a linter that is not installed.
    """
    out = _run(_js(tmp_path), env=_fake_npx(tmp_path, stderr))
    assert "skipped" in out, describe(out)
    assert "npm install" in out["skipped"], out["skipped"]


@posix_only
def test_npx_without_eslint_is_loud_when_required(tmp_path: Path) -> None:
    """And it escalates like every other absence, naming the variable."""
    env = _fake_npx(tmp_path, NPX_UNKNOWN_STDERR)
    env["SUPERTOOL_REQUIRE_VALIDATORS"] = "eslint"
    out = _run(_js(tmp_path), env=env)
    assert "skipped" not in out, describe(out)
    assert_declined(out, context="a required eslint that npx cannot resolve")
    assert "SUPERTOOL_REQUIRE_VALIDATORS" in out["errors"][0]["msg"]
    assert "npm install" in out["errors"][0]["msg"], out["errors"][0]["msg"]


@posix_only
def test_a_real_npx_failure_is_still_a_failure(tmp_path: Path) -> None:
    """The narrow half. npx that fails for any other reason stays loud —
    swallowing an unknown failure is the mistake pointing the other way."""
    env = _fake_npx(tmp_path, "npm error code EACCES\nnpm error syscall open\n")
    out = _run(_js(tmp_path), env=env)
    assert "skipped" not in out, describe(out)
    assert_declined(out, context="an npx failure that is not a missing eslint")
    assert out["errors"][0]["code"] == "adapter", describe(out)


# ---------------------------------------------------------------------------
# Against a real eslint, where there is one
# ---------------------------------------------------------------------------

@needs_eslint
def test_real_eslint_reports_no_var(tmp_path: Path) -> None:
    (tmp_path / "eslint.config.mjs").write_text(
        'export default [{rules:{"no-var":"error"}}];\n', encoding="utf-8")
    out = _run(_js(tmp_path, "var x = 1;\nexport default x;\n"))
    assert_declined(out, context="a `var` under a no-var config")
    assert "no-var" in [e["code"] for e in out["errors"]], describe(out)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_registered_without_rollback() -> None:
    cfg = json.loads((REPO / ".supertool.example.json").read_text(encoding="utf-8"))
    entry = cfg["validators"]["eslint"]
    assert entry["rollback_on_fail"] is False, entry
    assert "validators/eslint/eslint.py" in entry["cmd"]
