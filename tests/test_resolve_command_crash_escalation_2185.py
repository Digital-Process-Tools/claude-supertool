"""#2185 -- a `resolve`-class command's crash / could-not-look reason must
escalate the same way the other core-detected "this validator is too broken
to answer for itself" cases already do (`_validator_unusable_reply`'s four:
no output, non-JSON output, could not be spawned, no verdict key) -- not a
plain `skipped` that is invisible to `$SUPERTOOL_REQUIRE_VALIDATORS`.

Follow-up from #2174/#2177 (PR #2181). Before this, `_validator_run_one`'s
`_VALIDATOR_RESOLVE_ERROR_PREFIX` branch returned a bare
`{"tool": name, "skipped": reason}` with no `no_verdict` marker -- so
`_validator_gate_did_not_run` never saw it, `SUPERTOOL_REQUIRE_VALIDATORS`
exited 0 over a resolve step that never ran (git absent, timed out, not a
repo, unspawnable, or its own `guard_main` crash receipt), and the row never
escalated to `NOT CHECKED` the way `test_require_validators_core_975.py`'s
five broken-adapter cases do.

Deliberately does NOT make this render as loud as a self-reported adapter
crash (`code: "adapter"`, unconditional `NOT CHECKED` via
`_validator_no_verdict`) -- that channel is for an adapter healthy enough to
report on itself (#967); this is the core inferring breakage from a
subprocess that did not behave, exactly the class `_validator_unusable_reply`
already treats more conservatively (gated behind `$SUPERTOOL_REQUIRE_VALIDATORS`,
quiet otherwise) for the documented reason that a gate crying wolf on every
ordinary edit is the quiet bug traded for a louder one (#665, #975).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import supertool  # noqa: E402
from _adapter_verdict import skip_if_core_timed_out  # noqa: E402


def _resolve_error_spec(tmp_path: Path) -> dict:
    """A `resolve` command that always prints the shared RESOLVE-ERROR protocol."""
    stub = tmp_path / "resolve_error_stub.py"
    stub.write_text(
        "import sys\n"
        "print('RESOLVE-ERROR: simulated could-not-look')\n",
        encoding="utf-8",
    )
    return {"resolve": "{python} " + stub.as_posix() + " {file}"}


def test_a_resolve_error_still_renders_as_a_skip_by_default(tmp_path: Path) -> None:
    """MUST NOT FIRE (ordinary case): with nobody requiring this validator, the
    result is still a quiet skip -- the fix must not make every unrequired
    edit noisier."""
    f = tmp_path / "f.txt"
    f.write_text("x\n")
    spec = _resolve_error_spec(tmp_path)
    result = skip_if_core_timed_out(supertool._validator_run_one("ci-lint", spec, str(f)))
    assert result is not None
    assert "skipped" in result, result
    assert "simulated could-not-look" in result["skipped"], result


def test_a_resolve_error_now_sets_no_verdict_for_the_gate(tmp_path: Path) -> None:
    """MUST FIRE: this is the actual defect -- a resolve-command crash was
    invisible to `_validator_gate_did_not_run`, unlike the four
    `_validator_unusable_reply` cases it already covers."""
    f = tmp_path / "f.txt"
    f.write_text("x\n")
    spec = _resolve_error_spec(tmp_path)
    result = skip_if_core_timed_out(supertool._validator_run_one("ci-lint", spec, str(f)))
    assert result.get("no_verdict") is True, (
        "a resolve-command could-not-look reason must set the same "
        "`no_verdict` marker `_validator_unusable_reply` gives the other "
        "four core-detected breakdowns, or $SUPERTOOL_REQUIRE_VALIDATORS "
        "cannot see it: " + repr(result))


def test_required_validator_with_a_broken_resolve_step_exits_nonzero(
        tmp_path: Path, capsys, monkeypatch) -> None:
    """MUST FIRE: reproduces #975's own assertion shape, one call frame up --
    a `resolve`-class command that could not look must not let
    `$SUPERTOOL_REQUIRE_VALIDATORS` exit 0 over an edit nobody actually
    checked."""
    stub = tmp_path / "resolve_error_stub.py"
    stub.write_text(
        "import sys\n"
        "print('RESOLVE-ERROR: simulated could-not-look')\n",
        encoding="utf-8",
    )
    spec = {"resolve": "{python} " + stub.as_posix() + " {file}",
            "cmd": "{python} -c \"import json; print(json.dumps({'tool': 'fake', 'ok': True, 'count': 0, 'errors': [], 'duration_ms': 1}))\" {file}",
            "match": "*", "cache": False,
            "hooks_into": ["edit", "replace", "replace_lines", "paste",
                           "append", "vim"]}
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("f", ""))
    monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", "fake")
    supertool._CONFIG = {"validators": {"fake": spec}}
    supertool._CONFIG_CHECKED = True
    f = tmp_path / "s.sh"
    f.write_text("exit 0\n", encoding="utf-8")
    rc = supertool.main([f"edit:::exit :::exit:::{f}"])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "NOT CHECKED" in out, out
