"""#2086 -- `doctor` reports formatter toolchains the same three-state way it
already reports validator toolchains (#1857/#1950): resolves / absent / could
not tell, plus "not applicable" and "no formatters configured".

Before this, `doctor` said nothing about `formatters`, so a repo with no
working Python formatter (#2085) had no way to learn that from the one op
that exists to answer this question -- the silence read exactly like
formatters being fine, this repo's own defect class aimed at its own
diagnostic (CLAUDE.md, "The defect this codebase keeps having").
"""
from __future__ import annotations

import supertool


def test_doctor_reports_no_formatters_configured_when_section_absent() -> None:
    out = supertool.op_doctor("")
    assert "no " + chr(34) + "formatters" + chr(34) + " section in .supertool.json" in out


def test_doctor_warns_when_formatters_key_is_absent_2316(monkeypatch) -> None:
    """#2316 -- nobody has DECLARED anything, so writes go out unformatted
    with nothing saying so. That is a WARN, not an unweighted bullet sitting
    beside a genuine not-applicable (no tracked file matches)."""
    monkeypatch.setattr(supertool, "_load_config", lambda: {})
    out = supertool.op_doctor("")
    assert "no " + chr(34) + "formatters" + chr(34) + " section in .supertool.json" in out
    assert supertool.mark("⚠") in out.split("## Formatters")[1].split("##")[0]


def test_doctor_is_ok_when_formatters_is_explicitly_an_empty_dict_2316(monkeypatch) -> None:
    """#2316 -- an empty dict is a DECISION on record (no formatter, and
    the repo said so), the same shape claude-oss's own changelog_untagged
    already uses for absent-vs-empty. It must not warn."""
    monkeypatch.setattr(supertool, "_load_config", lambda: {"formatters": {}})
    out = supertool.op_doctor("")
    section = out.split("## Formatters")[1].split("##")[0]
    assert supertool.mark("⚠") not in section
    assert "0 configured" in section


def test_doctor_warns_on_a_malformed_non_dict_formatters_value_2316(monkeypatch) -> None:
    """A falsy value that is NOT a dict ([], "", 0, False) is a malformed
    config, not the deliberate {} decision -- it must not be laundered into
    the same 'declared, no formatter' ok line the empty-dict case gets
    (oss:auditor review of #2314x2316, class A). It must WARN, and the WARN
    must name what was actually found rather than repeat the {} claim."""
    for bad in ([], "", 0, False):
        monkeypatch.setattr(supertool, "_load_config", lambda bad=bad: {"formatters": bad})
        out = supertool.op_doctor("")
        section = out.split("## Formatters")[1].split("##")[0]
        assert supertool.mark("⚠") in section, (bad, section)
        assert '"formatters": {}' not in section, (bad, section)


def test_doctor_formatters_section_present_and_labelled() -> None:
    out = supertool.op_doctor("")
    assert "## Formatters (#2086)" in out


def test_doctor_formatters_default_mode_does_not_invoke_adapters(monkeypatch) -> None:
    """Without ':probe', doctor must not spawn a single formatter adapter."""
    calls = []

    def _fake_run_one(name, spec, target):
        calls.append(name)
        return {"tool": name, "file": target, "ok": True, "count": 0,
                "errors": [], "duration_ms": 1}

    monkeypatch.setattr(supertool, "_formatter_run_one", _fake_run_one)
    monkeypatch.setattr(supertool, "_load_config", lambda: {
        "formatters": {
            "fake-fmt": {
                "cmd": "true {file}",
                "match": "*.py",
                "hooks_into": ["edit"],
            }
        }
    })

    bare = supertool.op_doctor("")
    assert calls == [], "bare doctor must not invoke any formatter adapter"
    assert "could not tell without probing" in bare


def test_doctor_formatters_probe_mode_actually_probes(monkeypatch) -> None:
    calls = []

    def _fake_run_one(name, spec, target):
        calls.append(name)
        return {"tool": name, "file": target, "ok": True, "count": 0,
                "errors": [], "duration_ms": 1}

    monkeypatch.setattr(supertool, "_formatter_run_one", _fake_run_one)
    monkeypatch.setattr(supertool, "_load_config", lambda: {
        "formatters": {
            "fake-fmt": {
                "cmd": "true {file}",
                "match": "*.py",
                "hooks_into": ["edit"],
            }
        }
    })
    monkeypatch.setattr(supertool, "_doctor_tracked_files", lambda: ["a.py"])

    probed = supertool.op_doctor("probe")
    assert calls == ["fake-fmt"], "doctor:probe must invoke the in-scope formatter"
    assert "resolves" in probed


def test_doctor_formatters_reports_not_applicable_when_no_match(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_load_config", lambda: {
        "formatters": {
            "fake-fmt": {
                "cmd": "true {file}",
                "match": "*.rs",
                "hooks_into": ["edit"],
            }
        }
    })
    monkeypatch.setattr(supertool, "_doctor_tracked_files", lambda: ["a.py"])

    out = supertool.op_doctor("probe")
    assert "not applicable" in out
    assert "no tracked file matches" in out


def test_doctor_formatters_reports_could_not_tell_when_scope_unknown(monkeypatch) -> None:
    monkeypatch.setattr(supertool, "_load_config", lambda: {
        "formatters": {
            "fake-fmt": {
                "cmd": "true {file}",
                "match": "*.py",
                "hooks_into": ["edit"],
            }
        }
    })
    monkeypatch.setattr(supertool, "_doctor_tracked_files", lambda: None)

    out = supertool.op_doctor("probe")
    assert "could not tell whether this tree has a matching file" in out


def test_doctor_classify_formatter_probe_resolves() -> None:
    data = {"tool": "fake", "file": "x.py", "ok": True, "count": 0,
            "errors": [], "duration_ms": 5}
    state, _ = supertool._doctor_classify_formatter_probe(data)
    assert state == "resolves"


def test_doctor_classify_formatter_probe_absent_via_inline_adapter_error() -> None:
    """Every shipped SCHEMA formatter (php-cs-fixer, phpcbf, prettier-write,
    ruff-format) reports an absent binary this way -- an inline `errors`
    entry with `code: "adapter"`, never a `skipped` key (formatters have no
    third state, unlike validators)."""
    data = {"tool": "ruff-format", "file": "x.py", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "RUFF_BIN not found: ruff"}],
            "duration_ms": 5}
    state, detail = supertool._doctor_classify_formatter_probe(data)
    assert state == "absent"
    assert "not found" in detail


def test_doctor_classify_formatter_probe_absent_via_legacy_msg() -> None:
    """A legacy, non-JSON formatter (a bare `cmd`, no adapter script) carries
    no `errors` list at all -- only `_formatter_run_one`'s own `msg` on an
    `OSError`."""
    data = {"name": "fake", "ok": False, "msg": "[Errno 2] No such file or "
            "directory: 'fake-that-does-not-exist'", "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0}}
    state, detail = supertool._doctor_classify_formatter_probe(data)
    assert state == "absent"
    assert "No such file" in detail


def test_doctor_classify_formatter_probe_never_defaults_an_unreadable_verdict_to_resolves() -> None:
    """An adapter that answers something this classifier cannot place must
    land on 'could not tell', never on 'resolves' -- the same rule #1950
    states for the validator classifier, one payload shape over."""
    state, _ = supertool._doctor_classify_formatter_probe({"something": "else"})
    assert state == "could not tell"
    state, _ = supertool._doctor_classify_formatter_probe(None)
    assert state == "could not tell"


def test_doctor_classify_formatter_probe_failed_run_that_is_not_absence() -> None:
    """ok=False with no absence-shaped message must not be laundered into
    'absent' -- the toolchain resolved fine and reported a real failure on
    this file, which is 'resolves' (reachable), never a claim the file
    passed. Mirrors the validator classifier's own symmetry: `"ok" in data`
    answers 'resolves' regardless of the value of `ok` (#1950)."""
    data = {"tool": "ruff-format", "file": "x.py", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "ruff-format", "msg": "error: failed to parse x.py"}],
            "duration_ms": 5}
    state, _ = supertool._doctor_classify_formatter_probe(data)
    assert state == "resolves"


def test_doctor_classify_formatter_probe_never_calls_a_crashed_adapter_resolves() -> None:
    """A custom formatter script that raises before calling `emit()` produces
    the same shape `_formatter_run_one`'s legacy-fallback branch gives a
    genuinely failing raw-`cmd` tool: `ok: False`, no `errors`, no `msg`, only
    `raw` holding whatever the crashing process printed. A traceback in that
    `raw` text is the one signal that tells the two apart, and it must land
    on 'could not tell', never on 'resolves' -- a broken adapter script is
    not evidence the toolchain works."""
    data = {"name": "fake-fmt", "ok": False,
            "raw": "Traceback (most recent call last):\n  File \"fake.py\", "
                   "line 3, in <module>\nZeroDivisionError: division by zero",
            "duration_ms": 12, "metrics": {"lines_added": 0, "lines_removed": 0}}
    state, detail = supertool._doctor_classify_formatter_probe(data)
    assert state == "could not tell"
    assert "exception" in detail


def test_doctor_classify_formatter_probe_legacy_failure_without_traceback_still_resolves() -> None:
    """The sibling case: a legacy raw-`cmd` tool that ran and genuinely could
    not format the file (no traceback, just its own error text in `raw`) is
    still 'resolves' -- the toolchain is reachable, it just found something
    wrong with this file. Only a traceback flips this to 'could not tell'."""
    data = {"name": "fake-fmt", "ok": False, "raw": "fake-fmt: syntax error",
            "duration_ms": 12, "metrics": {"lines_added": 0, "lines_removed": 0}}
    state, _ = supertool._doctor_classify_formatter_probe(data)
    assert state == "resolves"

