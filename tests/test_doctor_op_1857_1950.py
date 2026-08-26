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

    def _boom(*a, **k):
        raise OSError("no such sysctl")
    monkeypatch.setattr(supertool.subprocess, "run", _boom)
    info = supertool._doctor_interpreter()
    assert info["rosetta"] is None


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


def test_doctor_default_mode_does_not_invoke_adapters() -> None:
    """Without ':probe', doctor must not run 39 adapters (issue's own bar)."""
    out = supertool.op_doctor("")
    assert "could not tell without probing" in out or "no \"validators\" section" in out


def test_doctor_probe_mode_reaches_the_validators_section() -> None:
    out = supertool.op_doctor("probe")
    assert "Toolchain validators" in out
