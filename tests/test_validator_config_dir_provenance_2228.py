"""#2228 -- `_validator_run_one` stamps `SUPERTOOL_CONFIG_DIR` into every
validator adapter's environment, the same shape #475 already pins for
`SUPERTOOL_MCP_AUTOSPAWN`.

This is the plumbing half of the fix: `new-file-lint.py` and
`changelog-fragment.py` read this variable to tell "the project that wired
this validator" from "whatever repo happens to contain the edited file"
before trusting a convention-based script found by walking up from the
target and importing it -- see
`tests/test_new_file_lint_untrusted_checkout_2228.py` and
`tests/test_changelog_fragment_untrusted_checkout_2228.py` for the adapter
half. Without this test, a future change to `_validator_run_one` could stop
setting the variable (or start setting it conditionally) and every adapter
would silently fall back to trusting nothing was ever wrong -- `_config_dir`
in both adapters treats a genuinely-absent variable as "no scope claim
being made" precisely so a *test harness* can still exercise them directly,
which means a regression here would not show up as a red adapter test.

Would these pass if the code did nothing? No: with the plumbing removed,
`captured["env"]` carries no `SUPERTOOL_CONFIG_DIR` key at all, and the
first assertion in each test fails.
"""
from __future__ import annotations

import os
import subprocess
import types

import pytest

from _adapter_verdict import run_one_or_skip


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path, spec: dict) -> dict:
    """Run one validator with subprocess.run faked; return the child env."""
    captured: dict = {}

    def fake_run(argv, **kwargs):  # noqa: ANN001
        captured["env"] = kwargs.get("env")
        return types.SimpleNamespace(
            stdout='{"tool": "fake", "ok": true, "count": 0, "errors": []}',
            stderr="", returncode=0,
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    target = tmp_path / "x.py"
    target.write_text("x = 1" + chr(10), encoding="utf-8")
    run_one_or_skip("fake", spec, str(target))
    return captured


def test_config_dir_reaches_the_adapter_when_a_config_was_loaded(
        monkeypatch: pytest.MonkeyPatch, tmp_path, request) -> None:
    """The real case: this repo's own `.supertool.json` is what supertool
    loaded for the whole test session, so the adapter env must carry the
    directory it lives in."""
    import _supertool
    monkeypatch.setattr(_supertool, "_CONFIG_PATH",
                        str(tmp_path / "project" / ".supertool.json"))
    captured = _run(monkeypatch, tmp_path,
                    {"cmd": "echo {file}", "cache": False, "timeout": 3})
    env = captured["env"]
    assert env is not None, "#2228: validator child env must be explicit"
    assert "SUPERTOOL_CONFIG_DIR" in env, (
        "#2228: SUPERTOOL_CONFIG_DIR missing from validator child env -- an "
        "adapter that imports and executes a found script has no way to "
        "tell which project's config wired it")
    assert env["SUPERTOOL_CONFIG_DIR"] == os.path.realpath(
        str(tmp_path / "project")), env["SUPERTOOL_CONFIG_DIR"]


def test_config_dir_is_empty_string_not_absent_when_no_config_loaded(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """No `.supertool.json` found anywhere -- the variable is still SET, to
    an empty string, never simply omitted. `_config_dir()` in both adapters
    reads presence-with-empty-value as a definite 'no directory to trust'
    (refuse convention-based execution), which is a different claim from
    'this adapter has no idea whether it is under supertool's wiring at
    all' (absent -- old, pre-#2228 behaviour). Folding the two together
    would make an adapter invoked with strict oversight behave exactly like
    one invoked by hand."""
    import _supertool
    monkeypatch.setattr(_supertool, "_CONFIG_PATH", None)
    captured = _run(monkeypatch, tmp_path,
                    {"cmd": "echo {file}", "cache": False, "timeout": 3})
    env = captured["env"]
    assert env is not None
    assert env.get("SUPERTOOL_CONFIG_DIR") == "", (
        f"#2228: expected SUPERTOOL_CONFIG_DIR='' with no config loaded, "
        f"got {env.get('SUPERTOOL_CONFIG_DIR')!r}")


def test_config_dir_does_not_clobber_spec_env(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Stamping this provenance must not drop the validator's own env
    block or the other provenance #475 already stamps."""
    import _supertool
    monkeypatch.setattr(_supertool, "_CONFIG_PATH",
                        str(tmp_path / ".supertool.json"))
    captured = _run(monkeypatch, tmp_path,
                    {"cmd": "echo {file}", "cache": False,
                     "env": {"MY_VALIDATOR_VAR": "kept"}})
    env = captured["env"]
    assert env.get("MY_VALIDATOR_VAR") == "kept"
    assert env.get("SUPERTOOL_MCP_AUTOSPAWN") == "0"
    assert "SUPERTOOL_CONFIG_DIR" in env
