"""`validate:@-` drops the `[result]`/`[branch:]` footer `validate:PATH` prints (#1158).

The payload route (`_read_op_from_payload`, called from `_dispatch_impl`)
returned its body directly -- `header + _read_warnings + _read_body` -- before
dispatch ever reached the footer block #990/#381 built for the colon route.
Same op, same file, same accumulated `acc_validated`/`acc_not_checked` rows;
two different verdicts depending on which route carried the call. The payload
route exists specifically so a path containing ':' or ',' can be validated at
all (#878) -- exactly the caller for whom the summary line matters most, since
it is the one who could not have used the colon form to begin with.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import supertool

CLEAN = json.dumps({"tool": "fake", "ok": True, "count": 0, "errors": [],
                    "duration_ms": 1})
REAL_FINDING = json.dumps({
    "tool": "fake", "ok": False, "count": 1,
    "errors": [{"line": 1, "col": 1, "severity": "error",
                "code": "E999", "msg": "unterminated object"}],
    "duration_ms": 1,
})


def _adapter(tmp_path: Path, reply_by_name: dict) -> str:
    script = tmp_path / "_adapter.py"
    script.write_text(
        "import os, sys" + chr(10)
        + f"replies = {reply_by_name!r}" + chr(10)
        + "name = os.path.basename(sys.argv[-1])" + chr(10)
        + f"sys.stdout.write(replies.get(name, {CLEAN!r}))" + chr(10),
        encoding="utf-8",
    )
    return f"{{python}} {script.as_posix()} {{file}}"


def _configure(cmd: str) -> None:
    supertool._CONFIG = {"validators": {
        "fake": {"cmd": cmd, "match": "*.json", "cache": False, "timeout": 10},
    }}
    supertool._CONFIG_CHECKED = True


def _result_line(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("[result]"):
            return line
    return ""


def _payload_run(monkeypatch, arg: str, payload: str) -> str:
    monkeypatch.setattr(supertool.sys, "stdin", io.StringIO(payload))
    return supertool.dispatch(arg)


def test_validate_at_dash_prints_the_result_footer(tmp_path, capsys, monkeypatch) -> None:
    """The exact side-by-side comparison the issue describes.

    `validate:PATH` (colon route) ends on `[result] ...`; `validate:@-`
    (payload route), for the identical file and identical config, must too.
    """
    _configure(_adapter(tmp_path, {}))
    (tmp_path / "a.json").write_text('{"a": 1}' + chr(10), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    supertool.main(["validate:a.json"])
    colon_out = capsys.readouterr().out
    assert _result_line(colon_out) == \
        "[result] 1 file, 0 with findings, 0 not checked", colon_out

    payload_out = _payload_run(monkeypatch, "validate:@-", 'path = "a.json"')
    assert _result_line(payload_out) == \
        "[result] 1 file, 0 with findings, 0 not checked", payload_out


def test_validate_at_dash_counts_findings(tmp_path, capsys, monkeypatch) -> None:
    _configure(_adapter(tmp_path, {"b.json": REAL_FINDING}))
    (tmp_path / "a.json").write_text('{"a": 1}' + chr(10), encoding="utf-8")
    (tmp_path / "b.json").write_text('{"a": 1}' + chr(10), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    payload_out = _payload_run(
        monkeypatch, "validate:@-", 'paths = ["a.json", "b.json"]')
    assert _result_line(payload_out) == \
        "[result] 2 files, 1 with findings, 0 not checked", payload_out


def test_validate_at_dash_carries_the_branch_footer(tmp_path, capsys, monkeypatch) -> None:
    """[branch: X] is #381's own footer -- payload route must carry it too."""
    _configure(_adapter(tmp_path, {}))
    (tmp_path / "a.json").write_text('{"a": 1}' + chr(10), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_BRANCH_CACHE", [("main", "")])

    payload_out = _payload_run(monkeypatch, "validate:@-", 'path = "a.json"')
    lines = payload_out.rstrip(chr(10)).splitlines()
    assert lines[-1] == "[branch: main]", payload_out
