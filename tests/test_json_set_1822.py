"""`json-set` — a structured field-set for JSON-shaped files (#1822).

`paste` is whole-file and `edit` needs an exact `old` match; neither is
proportional to a small change buried in a large JSON document. Measured in
#1822: updating a 22 KB JSON report after a rebase changed 8 fields out of
~100, and the only route available was a full re-send — which pushed one
agent to a raw, unvalidated `python3 -c` transform instead.

`json-set` takes a dotted field path per changed value and rewrites the file
by parsing it, setting each leaf, and re-serializing the whole document —
never guessing at the bytes around an anchor the way a text-level patch
would. That construction means a *well-formed* call can never itself produce
syntactically invalid JSON, so the "must roll back on a syntax failure" case
(the requirement `edit`/`paste` already meet) is proven the same way this
repo's own paste-rollback suite proves its formatter-rollback arm
(`tests/test_paste_create_rollback_1088.py`,
`test_a_failing_formatter_also_removes_a_created_file`): by forcing a
post-write validator to report a regression and checking the generic
`_run_with_validators` plumbing this op is wired into actually undoes the
write.
"""
from __future__ import annotations

import json
from pathlib import Path

import supertool

NL = chr(10)


def _payload_file(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return "@" + str(p)


def _json_set(tmp_path: Path, target: Path, fields: dict, *, payload_name: str = "p.json") -> str:
    # JSON, not TOML: `set` is a nested object, and the only TOML shape for
    # that is a `[set]` table header -- unsupported by `_mini_toml_loads`,
    # the fallback parser this repo runs on Python <3.11 (no stdlib
    # `tomllib`; #1595's own docstring names it: "No single [table]"). A
    # TOML payload here parsed fine under this dev session's 3.11+
    # `tomllib` and failed on every <3.11 CI leg (#1822 follow-up). JSON
    # sidesteps the fallback parser entirely -- `_load_at_file_raw` detects
    # format from the first non-whitespace character and routes `{`/`[` to
    # `json.loads`, never to TOML.
    payload = json.dumps({"path": str(target), "set": fields})
    return supertool.dispatch(
        "json-set:" + _payload_file(tmp_path, payload_name, payload))


def _write_json(target: Path, obj) -> None:
    target.write_text(json.dumps(obj, indent=2) + NL, encoding="utf-8")


def test_json_set_updates_a_nested_key(tmp_path: Path) -> None:
    """The measured case: a value several levels deep, changed in place."""
    target = tmp_path / "report.json"
    _write_json(target, {
        "head": "abc1234",
        "tests": {"green": {"result": "old", "count": 3}},
        "untouched": "keep-me",
    })
    out = _json_set(tmp_path, target, {"tests.green.result": "new"})
    assert "ERROR" not in out, out
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert doc["tests"]["green"]["result"] == "new", out
    assert doc["tests"]["green"]["count"] == 3, "an untouched sibling moved:" + NL + out
    assert doc["untouched"] == "keep-me", "an untouched top-level field moved:" + NL + out


def test_json_set_multiple_fields_in_one_call(tmp_path: Path) -> None:
    """The whole point: several fields, one call, payload proportional to
    the change rather than to the file."""
    target = tmp_path / "report.json"
    _write_json(target, {"head": "abc1234", "state": "pending"})
    out = _json_set(tmp_path, target, {"head": "7cd0237", "state": "done"})
    assert "ERROR" not in out, out
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert doc["head"] == "7cd0237", out
    assert doc["state"] == "done", out


def test_json_set_on_a_file_that_is_not_json_refuses(tmp_path: Path) -> None:
    """Must refuse, not guess, at a file that never parsed."""
    target = tmp_path / "report.json"
    target.write_text("{not json at all", encoding="utf-8")
    out = _json_set(tmp_path, target, {"head": "x"})
    assert "ERROR" in out, out
    assert target.read_text(encoding="utf-8") == "{not json at all", (
        "a refused set touched the file:" + NL + out)


def test_json_set_on_a_file_that_is_valid_json_succeeds(tmp_path: Path) -> None:
    """The positive control paired with the refusal above — a harness that
    always refuses would pass the previous test for the wrong reason."""
    target = tmp_path / "report.json"
    _write_json(target, {"head": "x"})
    out = _json_set(tmp_path, target, {"head": "y"})
    assert "ERROR" not in out, out
    assert json.loads(target.read_text(encoding="utf-8"))["head"] == "y", out


def test_json_set_missing_intermediate_segment_refuses_without_guessing(
    tmp_path: Path,
) -> None:
    """`edit`/`paste` never fabricate structure the caller did not name, and
    neither does this: a dotted path through a segment that does not exist
    is refused rather than silently created."""
    target = tmp_path / "report.json"
    _write_json(target, {"tests": {"green": {"result": "old"}}})
    out = _json_set(tmp_path, target, {"tests.red.result": "new"})
    assert "ERROR" in out, out
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert "red" not in doc["tests"], "a missing segment was silently created:" + NL + out


def test_json_set_existing_nested_segment_succeeds(tmp_path: Path) -> None:
    """Positive control for the refusal above, on the same fixture shape:
    the sibling segment that DOES exist must still work."""
    target = tmp_path / "report.json"
    _write_json(target, {"tests": {"green": {"result": "old"}}})
    out = _json_set(tmp_path, target, {"tests.green.result": "new"})
    assert "ERROR" not in out, out
    assert json.loads(target.read_text(encoding="utf-8"))["tests"]["green"]["result"] == "new"


def test_json_set_on_a_missing_file_refuses(tmp_path: Path) -> None:
    target = tmp_path / "does_not_exist.json"
    out = _json_set(tmp_path, target, {"head": "x"})
    assert "ERROR" in out, out
    assert not target.exists(), out


def test_json_set_rolled_back_when_a_post_write_validator_regresses(
    tmp_path: Path, monkeypatch
) -> None:
    """`json-set` must be wired into the same validator/rollback path
    `edit` and `paste` use (#1822's hard requirement) — proven by forcing a
    validator regression on an otherwise legitimate write and checking the
    original bytes come back, the same technique
    `test_a_failing_formatter_also_removes_a_created_file` uses in
    tests/test_paste_create_rollback_1088.py for the create-path gap that
    method targets. `json-set`'s own construction (parse, mutate in memory,
    re-serialize the whole document) cannot itself emit syntactically
    invalid JSON from a well-formed call, so this is what actually exercises
    'must roll back' for this op rather than a scenario the op's own logic
    could ever reach unassisted.
    """
    target = tmp_path / "report.json"
    original = json.dumps({"head": "abc1234"}, indent=2) + NL
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        supertool, "_applicable_validators",
        lambda op, path: {"fake-validator": {"rollback_on_fail": True}})
    calls = {"n": 0}

    def _fake_batch(applicable, path, *a, **kw):
        calls["n"] += 1
        ok = calls["n"] == 1  # BEFORE the write is clean; AFTER regresses.
        return {"fake-validator": {
            "tool": "fake-validator", "ok": ok, "count": 0 if ok else 1,
            "errors": [] if ok else [{"line": None, "col": None,
                                       "severity": "error", "code": "fake",
                                       "msg": "forced regression"}]}}

    monkeypatch.setattr(supertool, "_validators_run_batch", _fake_batch)
    out = _json_set(tmp_path, target, {"head": "7cd0237"})
    assert "rolled back" in out, out
    assert target.read_text(encoding="utf-8") == original, (
        "the rollback did not restore the pre-write bytes:" + NL + out)


def test_json_set_not_rolled_back_when_validators_pass(tmp_path: Path) -> None:
    """Positive control for the rollback test above — a harness that always
    rolls back would pass it for the wrong reason."""
    target = tmp_path / "report.json"
    _write_json(target, {"head": "abc1234"})
    out = _json_set(tmp_path, target, {"head": "7cd0237"})
    assert "rolled back" not in out, out
    assert json.loads(target.read_text(encoding="utf-8"))["head"] == "7cd0237", out


def test_json_set_is_wired_into_the_real_jsonlint_validator() -> None:
    """Not a dispatch test — the suite's own autouse fixture
    (`tests/conftest.py::_disable_rtk_and_config`) blanks `_CONFIG` for
    every test, deliberately, so a live `dispatch()` call here would answer
    from an empty validator set regardless of what's on disk (the rollback
    test above forces the same regression through a monkeypatch for that
    reason). This reads the real, committed `.supertool.json` directly:
    `json-set` must be in jsonlint's `hooks_into`, or every write this op
    makes ships with no post-write JSON syntax check at all in a real
    repo — the exact gap #1822's own argument is about. The wiring itself
    is exercised live in the worktree this fix was built in (see the PR
    body), which is not something a pytest fixture that resets config can
    show."""
    root = Path(__file__).parent.parent
    cfg = json.loads((root / ".supertool.json").read_text(encoding="utf-8"))
    hooks = cfg["validators"]["jsonlint"]["hooks_into"]
    assert "json-set" in hooks, (
        f".supertool.json's jsonlint.hooks_into is missing 'json-set': {hooks}")
