"""#1857/#1950 — `doctor` reports the environment supertool runs in and, per
adapter, whether the toolchain it dispatches to actually resolves here.

The honest red for a new op is the op not existing. These tests assert the
*three-state* rendering for the environment half (#1857 — architecture is
`native` / `translated (rosetta)` / could-not-tell, never guessed) and the
toolchain half (#1950 — `resolves` / `absent` / `could not tell`, never
folded into two), because a `doctor` that renders `could not tell` as
`resolves` would launder a silent gap into a clean bill — the exact failure
the issue exists to end.
"""
from __future__ import annotations

import supertool


def test_doctor_dispatches_and_is_read_only() -> None:
    out = supertool.dispatch("doctor")
    assert "unknown operation" not in out
    assert "doctor" in supertool._valid_op_names()
    assert supertool._OP_SAFETY_BUILTIN.get("doctor") == "read-only"
    assert "doctor" in supertool._PARALLEL_SAFE_OPS


def test_doctor_reports_interpreter_facts() -> None:
    out = supertool.op_doctor("")
    assert supertool.sys.executable in out
    assert supertool.platform.python_version() in out
    # The architecture line is present under some spelling regardless of host.
    assert "rosetta" in out.lower() or "not applicable" in out.lower()


def test_doctor_never_asserts_native_without_evidence(monkeypatch) -> None:
    """A darwin host where the check could not run must say so, not 'native'.

    Forces sys.platform to darwin and the sysctl probe to fail, the way it
    genuinely does on an Intel Mac where sysctl.proc_translated does not
    exist — and asserts the interpreter dict lands on None (could not tell),
    never on False (native), which is the state a naive except-and-default
    would produce.
    """
    monkeypatch.setattr(supertool.sys, "platform", "darwin")
    # x86_64, not arm64: the fix for the sibling test below short-circuits
    # arm64 straight to `False` (Rosetta can never translate an arm64
    # process), so exercising the "sysctl failed" path needs the OTHER
    # architecture, where a real Intel Mac genuinely has no
    # sysctl.proc_translated node to answer.
    monkeypatch.setattr(supertool.platform, "machine", lambda: "x86_64")

    def _boom(*a, **k):
        raise OSError("no such sysctl")
    monkeypatch.setattr(supertool.subprocess, "run", _boom)
    info = supertool._doctor_interpreter()
    assert info["rosetta"] is None


def test_doctor_rosetta_flag_never_trusts_a_translated_ancestor(monkeypatch) -> None:
    """`sysctl.proc_translated` can read `1` for a NATIVE arm64 interpreter
    when an ancestor process in the exec chain (a wrapper compiled x86_64,
    e.g. Homebrew's `timeout`) was itself translated — confirmed on real
    hardware: `timeout 5 python3 -c '...sysctl.proc_translated...'` answers
    `1` for a python3 binary `file` reports as `Mach-O 64-bit executable
    arm64`. Rosetta only ever translates an x86_64 binary; an arm64 process
    can never itself be the thing being translated, so `machine() == "arm64"`
    must force `rosetta` to `False` regardless of what the sysctl answers,
    rather than publish a false 'install a native interpreter' banner about
    an interpreter that already is one.
    """
    monkeypatch.setattr(supertool.sys, "platform", "darwin")
    monkeypatch.setattr(supertool.platform, "machine", lambda: "arm64")

    class _Result:
        returncode = 0
        stdout = "1\n"
    monkeypatch.setattr(supertool.subprocess, "run", lambda *a, **k: _Result())
    info = supertool._doctor_interpreter()
    assert info["rosetta"] is False


def test_doctor_cpu_topology_states_uniform_or_could_not_tell_or_split() -> None:
    topo = supertool._doctor_cpu_topology()
    assert topo["logical_cpus"] == supertool.os.cpu_count()
    assert topo["state"] in ("split", "uniform", "unknown")
    if topo["state"] == "split":
        assert isinstance(topo["performance"], int)
        assert isinstance(topo["efficiency"], int)


def test_doctor_classifies_an_absent_tool_as_absent_not_resolves() -> None:
    data = {"tool": "fake", "file": "x.py", "skipped": "fake not found on PATH"}
    state, detail = supertool._doctor_classify_probe(data)
    assert state == "absent"
    assert "not found" in detail


def test_doctor_classifies_adapter_crash_as_could_not_tell() -> None:
    data = {"tool": "fake", "file": "x.py", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "fake adapter crashed"}],
            "duration_ms": 5}
    state, detail = supertool._doctor_classify_probe(data)
    assert state == "could not tell"


def test_doctor_classifies_a_real_verdict_as_resolves() -> None:
    data = {"tool": "fake", "file": "x.py", "ok": True, "count": 0,
            "errors": [], "duration_ms": 5}
    state, _ = supertool._doctor_classify_probe(data)
    assert state == "resolves"


def test_doctor_classifies_ambiguous_skip_as_could_not_tell_never_resolves() -> None:
    """A scope-shaped skip must not be laundered into 'resolves' by default."""
    data = {"tool": "fake", "file": "x.py", "skipped": "no target resolved"}
    state, _ = supertool._doctor_classify_probe(data)
    assert state == "could not tell"


def test_doctor_classifies_an_inline_adapter_error_absence_as_absent() -> None:
    """Several shipped adapters (phpstan, xmllint, node-check, prettier-check,
    bash-check, phpmd, psr, lsp-diag) report a missing binary as an inline
    `errors` entry with `code: "adapter"`, never through `skipped()` at all —
    the same shape `$SUPERTOOL_REQUIRE_VALIDATORS`'s `required_but_absent()`
    always uses too. A classifier that only applied the "not found" heuristic
    to the `skipped` branch reported every one of these as "could not tell"
    for a tool that is, in fact, definitively absent.
    """
    data = {"tool": "fake", "file": "x.py", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "phpstan binary not found"}],
            "duration_ms": 5}
    state, detail = supertool._doctor_classify_probe(data)
    assert state == "absent"
    assert "not found" in detail


def test_doctor_default_mode_does_not_invoke_adapters() -> None:
    """Without ':probe', doctor must not run 39 adapters (issue's own bar)."""
    out = supertool.op_doctor("")
    assert "could not tell without probing" in out or "no \"validators\" section" in out


def test_doctor_probe_mode_actually_probes_not_just_prints_the_header(
        monkeypatch, shipped_config) -> None:
    """The section header is printed unconditionally by both modes, so
    asserting on it alone would pass even if `doctor:probe` never called
    `_validator_run_one` at all. This pins the actual behavioural difference:
    bare `doctor` never reaches the classifier, `doctor:probe` does.

    `shipped_config`: without it, `_load_config()` answers the autouse-reset
    empty config (#1812) rather than this repo's real `.supertool.json`, so
    `_doctor_validators_section` finds no `"validators"` section at all and
    the loop this test is pinning never runs — the fake would report zero
    calls whether or not the probe path was actually reached.
    """
    calls = []

    def _fake_run_one(name, spec, target, doc_maybe_stale=False):
        calls.append(name)
        return {"tool": name, "file": target, "ok": True, "count": 0,
                "errors": [], "duration_ms": 1}

    monkeypatch.setattr(supertool, "_validator_run_one", _fake_run_one)

    bare = supertool.op_doctor("")
    assert calls == [], "bare doctor must not invoke any adapter"
    assert "could not tell without probing" in bare

    probed = supertool.op_doctor("probe")
    assert calls, "doctor:probe must have invoked at least one adapter"
    assert "resolves" in probed


def test_doctor_probe_bypasses_the_validator_cache(monkeypatch, shipped_config) -> None:
    """`doctor:probe` answers 'does this resolve NOW', which a stale cache
    hit from before a binary was installed or removed would silently
    contradict — checked by asserting the cache-disable env var is set for
    every real `_validator_run_one` call `doctor:probe` makes, and restored
    to its prior value afterward. See the sibling test above for why
    `shipped_config` is required.
    """
    seen_env = []

    def _fake_run_one(name, spec, target, doc_maybe_stale=False):
        seen_env.append(supertool.os.environ.get("SUPERTOOL_NO_VALIDATOR_CACHE"))
        return {"tool": name, "file": target, "ok": True, "count": 0,
                "errors": [], "duration_ms": 1}

    monkeypatch.setattr(supertool, "_validator_run_one", _fake_run_one)
    monkeypatch.delenv("SUPERTOOL_NO_VALIDATOR_CACHE", raising=False)

    supertool.op_doctor("probe")
    assert seen_env, "doctor:probe never reached _validator_run_one"
    assert all(v == "1" for v in seen_env)
    assert "SUPERTOOL_NO_VALIDATOR_CACHE" not in supertool.os.environ
