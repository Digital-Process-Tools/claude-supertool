"""An absent tool is never a clean verdict, and $SUPERTOOL_REQUIRE_VALIDATORS reaches every adapter (#1202).

Filed against `ruff` alone: its absent-tool arm emits `skipped` and never
consults `refusal.required()`, so naming it in the variable does nothing and
setting the variable is indistinguishable from not setting it.

The audit that issue asked for turned one adapter into two lists.

**The lesser half is what was filed** — adapters that decline honestly but
cannot be escalated: `ruff`, `html-check`, and the four MCP adapters, whose
`DaemonUnavailable` arm is the same absence wearing a different exception.

**The worse half was not filed and is on the adjacent line.** Ten adapters
answered an absent tool with `{"ok": true, "count": 0, "errors": []}` — a clean
verdict about a file nothing opened, which is the failure `validators/SCHEMA.md`
introduced the third state to end and which `refusal.py` says this directory has
filed eleven times. A reader cannot tell that green from a real one, and neither
can `rollback_on_fail`, the before/after delta, or CI.

So the contract asserted here is one sentence in two directions, for every
adapter that can find its tool missing:

* unset, an absent tool is `skipped` and carries no verdict key at all;
* named in the variable, the same absence is a loud `adapter` error that says
  the file was NOT checked and names the variable that caused the escalation.

Written as a table rather than per-adapter so that a new adapter with a
fabricated pass is a red here rather than a discovery in somebody elses repo.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from _adapter_verdict import assert_declined, describe
from _winenv import empty_path_env

VALIDATORS = Path(__file__).resolve().parent.parent / "validators"


def _adapter(name: str) -> Path:
    return VALIDATORS / name / f"{name}.py"


#: (validator name as it appears in the payload, sample filename, sample body).
#: The body is irrelevant — every case here is reached before the tool runs —
#: but the extension is not: several adapters read the suffix first.
ABSENT_TOOL_ADAPTERS = [
    ("ruff", "s.py", "x = 1\n"),
    ("pyright", "s.py", "x = 1\n"),
    ("tsc-check", "s.ts", "const x: number = 1;\n"),
    ("markdownlint", "s.md", "# t\n"),
    ("ruby-check", "s.rb", "puts 1\n"),
    ("cargo-check", "s.rs", "fn main() {}\n"),
    ("hadolint", "Dockerfile", "FROM scratch\n"),
    ("gofmt-check", "s.go", "package main\n"),
    ("terraform-check", "s.tf", "variable \"x\" {}\n"),
    ("git-status", "s.txt", "x\n"),
    ("html-check", "s.html", "<p>hi</p>\n"),
    ("actionlint", "s.yml", "on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"),
]


def _run(name: str, target: Path, env: dict) -> dict:
    """Spawn the adapter by absolute interpreter path with PATH emptied.

    `sys.executable` rather than `python`, so the interpreter is still reachable
    when `shutil.which` inside the adapter genuinely cannot find anything —
    which is the whole point of the environment being empty.
    """
    r = subprocess.run([sys.executable, str(_adapter(name)), str(target)],
                       capture_output=True, text=True, env=env,
                       encoding="utf-8", errors="replace")
    assert r.stdout.strip(), (
        f"{name}: adapter printed no verdict at all; stderr={r.stderr!r}")
    return json.loads(r.stdout)


def _sample(tmp_path: Path, filename: str, body: str) -> Path:
    f = tmp_path / filename
    f.write_text(body, encoding="utf-8")
    return f


@pytest.mark.parametrize("name,filename,body", ABSENT_TOOL_ADAPTERS,
                         ids=[a[0] for a in ABSENT_TOOL_ADAPTERS])
def test_an_absent_tool_is_the_third_state_not_a_pass(
        tmp_path: Path, name: str, filename: str, body: str) -> None:
    """No verdict key, because there is no verdict — only an attempt."""
    out = _run(name, _sample(tmp_path, filename, body), empty_path_env())
    assert "skipped" in out, describe(out)
    for key in ("ok", "count", "errors"):
        assert key not in out, f"{name}: a skip must not carry {key!r}: {out}"
    assert out["tool"] == name, out


@pytest.mark.parametrize("name,filename,body", ABSENT_TOOL_ADAPTERS,
                         ids=[a[0] for a in ABSENT_TOOL_ADAPTERS])
def test_required_turns_the_absent_tool_into_a_loud_error(
        tmp_path: Path, name: str, filename: str, body: str) -> None:
    """The escalation is addressed by validator name, not by binary name.

    `tsc-check` runs `tsc`, `ruby-check` runs `ruby`, `html-check` runs `node`.
    The core reads `$SUPERTOOL_REQUIRE_VALIDATORS` against the key a repo wrote
    in `.supertool.json`, so an adapter that consulted `required("tsc")` would
    ignore the only spelling anyone can configure.
    """
    env = empty_path_env()
    env["SUPERTOOL_REQUIRE_VALIDATORS"] = name
    out = _run(name, _sample(tmp_path, filename, body), env)
    assert "skipped" not in out, describe(out)
    assert_declined(out, context=f"{name} required but absent")
    msg = out["errors"][0]["msg"]
    assert "SUPERTOOL_REQUIRE_VALIDATORS" in msg, msg
    assert "NOT checked" in msg, msg
    assert out["errors"][0]["code"] == "adapter", out


def test_the_unescalated_skip_survives_a_variable_naming_someone_else(
        tmp_path: Path) -> None:
    """Escalation is opt-in per validator, and the opt-in is a list membership.

    A substring test would make `SUPERTOOL_REQUIRE_VALIDATORS=ruff` escalate
    `ruff-format` too. `refusal.required` splits before comparing; this holds
    it to that.
    """
    env = empty_path_env()
    env["SUPERTOOL_REQUIRE_VALIDATORS"] = "shellcheck,gitleaks"
    out = _run("ruff", _sample(tmp_path, "s.py", "x = 1\n"), env)
    assert "skipped" in out, describe(out)


# ---------------------------------------------------------------------------
# yaml-check — the absent dependency is a library, not a binary
# ---------------------------------------------------------------------------

def _no_pyyaml_env(tmp_path: Path) -> dict:
    """An importable `yaml` that raises on import, ahead of any real PyYAML.

    Emptying PATH cannot reach this adapters cant-run arm: its dependency is
    imported, not spawned. Uninstalling PyYAML for one test is not available, so
    the import is shadowed instead.
    """
    stub = tmp_path / "_stub"
    stub.mkdir()
    (stub / "yaml.py").write_text(
        "raise ImportError('PyYAML shadowed by the test')\n", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(stub)
    return env


def test_yaml_check_without_pyyaml_is_a_skip_not_a_pass(tmp_path: Path) -> None:
    out = _run("yaml-check", _sample(tmp_path, "s.yaml", "a: 1\n"),
               _no_pyyaml_env(tmp_path))
    assert "skipped" in out, describe(out)
    for key in ("ok", "count", "errors"):
        assert key not in out, f"a skip must not carry {key!r}: {out}"


def test_yaml_check_without_pyyaml_escalates_when_required(tmp_path: Path) -> None:
    env = _no_pyyaml_env(tmp_path)
    env["SUPERTOOL_REQUIRE_VALIDATORS"] = "yaml-check"
    out = _run("yaml-check", _sample(tmp_path, "s.yaml", "a: 1\n"), env)
    assert "skipped" not in out, describe(out)
    assert_declined(out, context="yaml-check required without PyYAML")
    assert "SUPERTOOL_REQUIRE_VALIDATORS" in out["errors"][0]["msg"]


# ---------------------------------------------------------------------------
# The MCP adapters — same absence, raised as an exception instead of a `which`
# ---------------------------------------------------------------------------

MCP_ADAPTERS = ["phpstan-mcp", "rector-mcp", "phpunit-mcp", "phpmd-mcp"]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), _adapter(name))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_mcp(name: str, tmp_path: Path, capsys, monkeypatch) -> dict:
    """Drive the adapters own `main` with the daemon reporting itself absent.

    `DaemonUnavailable` is raised when the analyser is not installed *for this
    working directory*, which is exactly the absent-tool case the other half of
    this file reaches through `shutil.which` — the two look different only
    because a daemon has to be started before it can be missed.
    """
    mod = _load(name)
    from refusal import DaemonUnavailable

    def _absent(*_a, **_k):
        raise DaemonUnavailable(f"{name} is not installed for this directory")

    monkeypatch.setattr(mod, "ensure_daemon", _absent)
    f = _sample(tmp_path, "s.php", "<?php\n")
    capsys.readouterr()
    mod.main([str(_adapter(name)), str(f)])
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.mark.parametrize("name", MCP_ADAPTERS)
def test_an_absent_mcp_daemon_is_a_skip(
        name: str, tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.delenv("SUPERTOOL_REQUIRE_VALIDATORS", raising=False)
    out = _run_mcp(name, tmp_path, capsys, monkeypatch)
    assert "skipped" in out, describe(out)


@pytest.mark.parametrize("name", MCP_ADAPTERS)
def test_an_absent_mcp_daemon_escalates_when_required(
        name: str, tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", name)
    out = _run_mcp(name, tmp_path, capsys, monkeypatch)
    assert "skipped" not in out, describe(out)
    assert_declined(out, context=f"{name} required, daemon absent")
    assert "SUPERTOOL_REQUIRE_VALIDATORS" in out["errors"][0]["msg"]

# ---------------------------------------------------------------------------
# `which` said yes and exec said no — the second absent-tool arm
# ---------------------------------------------------------------------------
#
# Found by the review of the first commit, and it is why the fix is larger than
# the audit. Emptying `PATH` reaches only the `shutil.which` guard; seven
# adapters carried a *second* absent-tool arm underneath it, catching the
# `FileNotFoundError` that a PATH entry vanishing between the lookup and the
# spawn produces — and every one of them still answered it with `ok: true`. The
# docstrings rewritten in the same commit claimed otherwise, so for one commit
# the prose was more wrong than the code it described.
#
# Reached by shadowing `shutil.which` and `subprocess.run` and running the
# adapter through `runpy`, the technique `tests/test_yaml_check.py` already
# uses: portable, and it exercises the real module rather than a stub of it. A
# shebang pointing at a missing interpreter would be the more honest
# reproduction and is POSIX-only, which would leave this arm unasserted on the
# platform that breaks most often.

EXEC_FAILS_ADAPTERS = [
    ("ruff", "s.py", "x = 1\n"),
    ("pyright", "s.py", "x = 1\n"),
    ("tsc-check", "s.ts", "const x: number = 1;\n"),
    ("markdownlint", "s.md", "# t\n"),
    ("ruby-check", "s.rb", "puts 1\n"),
    ("hadolint", "Dockerfile", "FROM scratch\n"),
    ("gofmt-check", "s.go", "package main\n"),
    ("terraform-check", "s.tf", "variable \"x\" {}\n"),
    ("cargo-check", "src/main.rs", "fn main() {}\n"),
    ("actionlint", "s.yml", "on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"),
]


def _run_with_exec_failing(name: str, tmp_path: Path, filename: str,
                           body: str, env: dict) -> dict:
    target = tmp_path / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    if name == "cargo-check":
        # Reached only past the crate-root walk, which is a scope refusal and
        # deliberately not an escalation.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "x"\nversion = "0.1.0"\n', encoding="utf-8")

    shim = tmp_path / "_exec_fails_shim.py"
    shim.write_text(
        "import runpy, shutil, subprocess, sys\n"
        "shutil.which = lambda *a, **k: 'a-path-that-resolved'\n"
        "def _gone(*a, **k):\n"
        "    raise FileNotFoundError(2, 'No such file or directory')\n"
        "subprocess.run = _gone\n"
        f"runpy.run_path({str(_adapter(name))!r}, run_name='__main__')\n",
        encoding="utf-8")

    r = subprocess.run([sys.executable, str(shim), str(target)],
                       capture_output=True, text=True, env=env,
                       encoding="utf-8", errors="replace")
    assert r.stdout.strip(), (
        f"{name}: adapter printed no verdict at all; stderr={r.stderr!r}")
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("name,filename,body", EXEC_FAILS_ADAPTERS,
                         ids=[a[0] for a in EXEC_FAILS_ADAPTERS])
def test_a_tool_that_resolved_and_would_not_run_is_the_third_state(
        tmp_path: Path, name: str, filename: str, body: str) -> None:
    env = dict(os.environ)
    env.pop("SUPERTOOL_REQUIRE_VALIDATORS", None)
    out = _run_with_exec_failing(name, tmp_path, filename, body, env)
    assert "skipped" in out, describe(out)
    for key in ("ok", "count", "errors"):
        assert key not in out, f"{name}: a skip must not carry {key!r}: {out}"


@pytest.mark.parametrize("name,filename,body", EXEC_FAILS_ADAPTERS,
                         ids=[a[0] for a in EXEC_FAILS_ADAPTERS])
def test_a_tool_that_would_not_run_escalates_when_required(
        tmp_path: Path, name: str, filename: str, body: str) -> None:
    env = dict(os.environ)
    env["SUPERTOOL_REQUIRE_VALIDATORS"] = name
    out = _run_with_exec_failing(name, tmp_path, filename, body, env)
    assert "skipped" not in out, describe(out)
    assert_declined(out, context=f"{name} required, resolved but unrunnable")
    assert "SUPERTOOL_REQUIRE_VALIDATORS" in out["errors"][0]["msg"]
