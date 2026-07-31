"""An adapter's own reply is never a finding about the file (issue #634).

`validate:presets/gitlab.json` reported `jsonlint : 1 err (0ms)` with
`orchestrator  adapter bad json: Expecting value: line 1 column 1 (char 0)`
on a clean tree, against a file stdlib `json.load()` reads happily.

Two defects, stacked:

1. **Config.** `.supertool.json` pointed `jsonlint` at `{python} -m json.tool
   {file}` — a pretty-printer, not a SCHEMA.md adapter. It exits 0 and prints
   the reformatted file, so the orchestrator parsed the last line (`}`) as a
   receipt and got `Expecting value: line 1 column 1 (char 0)` — the same text
   `json.loads("")` raises, which is why it read as an empty response. The
   bundled `validators/jsonlint/jsonlint.py` was sitting unused next to it.

2. **Core.** However it happens, an adapter reply the orchestrator cannot parse
   is an absence of information about the file — not a finding about it. It was
   rendered as `1 err` attributed to the user's file, in the same colour and
   the same position a real malformed-JSON error would print. #263 turned a
   silence into a false clean; this turns a silence into a false finding, and an
   invented error costs the credibility of every real one the validator prints.

These pin both, plus the guard that matters most: the fix must not disarm the
validator. Genuinely malformed JSON still reports an error.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

import supertool

_ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERTOOL_NO_VALIDATOR_CACHE", "1")


def _repo_jsonlint_spec() -> dict:
    cfg = json.loads((_ROOT / ".supertool.json").read_text(encoding="utf-8"))
    return cfg["validators"]["jsonlint"]


def _run(spec: dict, target: Path) -> dict:
    res = supertool._validator_run_one("jsonlint", spec, str(target))
    assert res is not None
    return res


def _finding_text(res: dict) -> str:
    return " ".join(str(e.get("msg", "")) for e in res.get("errors", []))


# ---------------------------------------------------------------------------
# The reproduction, through the repo's own configuration
# ---------------------------------------------------------------------------

def test_valid_json_file_reports_no_error(tmp_path: Path) -> None:
    """The bug: a file stdlib json reads fine reported `1 err`."""
    target = tmp_path / "good.json"
    target.write_text(json.dumps({"a": [1, 2, 3]}), encoding="utf-8")
    res = _run(_repo_jsonlint_spec(), target)
    assert "skipped" not in res, f"a readable file must be judged, got {res!r}"
    assert res.get("ok") is True, f"expected a clean pass, got {res!r}"
    assert res.get("count", 0) == 0


def test_repo_presets_json_reports_no_error() -> None:
    """The literal repro from the issue, on the file it was filed against."""
    target = _ROOT / "presets" / "gitlab.json"
    json.loads(target.read_text(encoding="utf-8"))  # premise: the file is fine
    res = _run(_repo_jsonlint_spec(), target)
    assert "skipped" not in res
    assert res.get("ok") is True, f"expected a clean pass, got {res!r}"


def test_malformed_json_file_still_reports_an_error(tmp_path: Path) -> None:
    """The guard on the fix: do not trade the loud bug for a quiet one.

    Silencing the row would convert a false positive into no coverage at all.
    This is the test that says which failure we chose.
    """
    target = tmp_path / "bad.json"
    target.write_text('{"a": [1, 2,}', encoding="utf-8")
    res = _run(_repo_jsonlint_spec(), target)
    assert "skipped" not in res, f"a malformed file is a finding, not a skip: {res!r}"
    assert res.get("ok") is False
    assert res.get("count", 0) >= 1
    assert res["errors"][0]["line"] is not None, "a real finding names a line"


# ---------------------------------------------------------------------------
# Core contract: an unusable adapter reply is never a file-level finding
# ---------------------------------------------------------------------------

def test_empty_adapter_reply_is_not_a_finding(tmp_path: Path) -> None:
    target = tmp_path / "good.json"
    target.write_text("{}", encoding="utf-8")
    spec = {"cmd": "{python} -c pass", "match": "*.json"}
    res = _run(spec, target)
    assert "skipped" in res, f"empty output is an absence, not an error: {res!r}"
    assert "count" not in res and "errors" not in res and "ok" not in res  # #515
    assert supertool._validator_regressed(None, res) is False


def _multiline_json(tmp_path: Path) -> Path:
    """A file whose pretty-print spans lines, so json.tool's last line is `}`.

    That is the literal #634 stdout: the orchestrator reads the last line of the
    adapter's output, and `json.loads("}")` raises the very
    `Expecting value: line 1 column 1 (char 0)` the issue reported.
    """
    target = tmp_path / "good.json"
    target.write_text(json.dumps({"a": [1, 2, 3], "b": {"c": 4}}, indent=2),
                      encoding="utf-8")
    return target


def test_unparseable_adapter_reply_is_not_a_finding(tmp_path: Path) -> None:
    """The exact shape of #634: adapter exits 0, prints non-receipt output."""
    target = _multiline_json(tmp_path)
    spec = {"cmd": "{python} -m json.tool {file}", "match": "*.json"}
    res = _run(spec, target)
    assert "skipped" in res, f"a bad reply is the adapter's fault, not the file's: {res!r}"
    assert "count" not in res and "errors" not in res and "ok" not in res
    assert supertool._validator_regressed(None, res) is False


def test_unparseable_reply_reason_names_the_adapter_not_the_file(tmp_path: Path) -> None:
    """"adapter bad json" reads as "your JSON is bad" to everyone."""
    target = _multiline_json(tmp_path)
    res = _run({"cmd": "{python} -m json.tool {file}", "match": "*.json"}, target)
    reason = str(res["skipped"]).lower()
    assert reason.startswith("jsonlint adapter"), f"name the adapter first: {reason!r}"
    assert "not json" in reason, f"say whose JSON failed to parse: {reason!r}"
    assert "good.json" not in reason, f"must not blame the file under test: {reason!r}"


def test_parseable_non_receipt_reply_is_not_a_verdict(tmp_path: Path) -> None:
    """Found while running the RED pass, and it is the same defect.

    `json.tool` on a single-line file prints `{}` — valid JSON, so the parse
    succeeds and the orchestrator returned that bare dict as the result. With
    no `ok` key it renders `0 err`, i.e. a *pass* invented out of a reply that
    carried no verdict. Same absence, other polarity, one line away from #634.
    """
    target = tmp_path / "good.json"
    target.write_text("{}", encoding="utf-8")
    res = _run({"cmd": "{python} -m json.tool {file}", "match": "*.json"}, target)
    assert "skipped" in res, f"a reply with no verdict is not a pass: {res!r}"
    assert res.get("ok") is not True


def test_missing_adapter_binary_is_skipped_with_the_reason(tmp_path: Path) -> None:
    """Not installed is a third state. `1 err` says the file is broken."""
    target = tmp_path / "good.json"
    target.write_text("{}", encoding="utf-8")
    spec = {"cmd": "supertool-no-such-binary-634 {file}", "match": "*.json"}
    res = _run(spec, target)
    assert "skipped" in res, f"a missing binary is not a finding: {res!r}"
    assert "supertool-no-such-binary-634" in str(res["skipped"])
    assert supertool._validator_regressed(None, res) is False


def test_missing_binary_reason_names_the_program_on_every_platform(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows leg of #634's own fix: `WinError 2` names nothing.

    POSIX puts the binary in the OSError text (`No such file or directory:
    'jsonlint'`); Windows raises `[WinError 2] The system cannot find the file
    specified` and names no file at all. Sourcing the name from the exception
    told Windows users a checker could not run without telling them which —
    the same platform-shaped hole as #627. The name comes from the spec now, so
    this asserts it on every platform by injecting the Windows-shaped error.
    """
    target = tmp_path / "good.json"
    target.write_text("{}", encoding="utf-8")

    def _winerror(*a, **kw):
        raise OSError(2, "The system cannot find the file specified")

    monkeypatch.setattr(subprocess, "run", _winerror)
    spec = {"cmd": "supertool-no-such-binary-634 {file}", "match": "*.json"}
    res = _run(spec, target)
    assert "skipped" in res
    assert "supertool-no-such-binary-634" in res["skipped"], (
        f"name the program from the spec, not the exception: {res['skipped']!r}")
    assert res["skipped"].count("supertool-no-such-binary-634") == 1, (
        "the program is named once, from the spec — not echoed again out of "
        f"the platform's exception text: {res['skipped']!r}")


def test_install_dir_is_posix_so_cmds_survive_shlex() -> None:
    """Why the product was never hit by the fixture bug this test file had.

    `cmd` is split with POSIX-mode `shlex.split` on every platform, which eats
    backslashes. `{supertool_dir}` is safe because supertool.py:317 stores the
    install dir with `os.sep` already replaced by `/` — a Windows path pasted
    in raw would reach the adapter as `C:Usersrunneradmin...`.
    """
    assert "\\" not in supertool._INSTALL_DIR


def test_windows_style_target_survives_quoting() -> None:
    """`{file}` is the other half: shlex.quote keeps backslashes intact."""
    win = r"C:\Users\runneradmin\work\good.json"
    cmd = "python adapter.py " + shlex.quote(win)
    assert shlex.split(cmd)[-1] == win


def test_skip_row_renders_the_reason(tmp_path: Path) -> None:
    target = tmp_path / "good.json"
    target.write_text("{}", encoding="utf-8")
    res = _run({"cmd": "{python} -c pass", "match": "*.json"}, target)
    row = " ".join(supertool._validator_render_row(res))
    assert "skipped" in row
    assert "jsonlint" in row


# ---------------------------------------------------------------------------
# Guards: the new skips must not swallow anything that ran
# ---------------------------------------------------------------------------

def test_wellformed_receipt_with_errors_still_reports_them(tmp_path: Path) -> None:
    target = tmp_path / "good.json"
    target.write_text("{}", encoding="utf-8")
    receipt = json.dumps({"tool": "jsonlint", "file": str(target), "ok": False,
                          "count": 1, "duration_ms": 3,
                          "errors": [{"line": 4, "col": 2, "severity": "error",
                                      "code": "syntax", "msg": "boom"}]})
    script = tmp_path / "adapter.py"
    script.write_text("import sys; sys.stdout.write(%r)\n" % receipt, encoding="utf-8")
    spec = {"cmd": "{python} " + str(script), "match": "*.json"}
    res = _run(spec, target)
    assert "skipped" not in res
    assert res["ok"] is False and res["count"] == 1
    assert _finding_text(res) == "boom"


def test_adapter_timeout_stays_loud(tmp_path: Path) -> None:
    """A tool that ran and hung is a validator failure, not a refusal to run.

    Deliberately excluded from the skip conversion: the binary exists, it was
    invoked, and it is misbehaving. Guessing towards silence there is how a
    broken validator starts looking clean.
    """
    target = tmp_path / "good.json"
    target.write_text("{}", encoding="utf-8")
    spec = {"cmd": "{python} -c " + repr("import time; time.sleep(5)"),
            "match": "*.json", "timeout": 1}
    res = _run(spec, target)
    assert "skipped" not in res, f"a timeout is a failure, not a skip: {res!r}"
    assert res["ok"] is False


def test_config_points_jsonlint_at_the_bundled_adapter() -> None:
    """The root cause, pinned: a linter is not an adapter.

    Any `cmd` that prints something other than a SCHEMA.md receipt reproduces
    #634 through the shared orchestrator path, for any validator.
    """
    cmd = _repo_jsonlint_spec()["cmd"]
    assert "json.tool" not in cmd, (
        "python -m json.tool pretty-prints the file; the orchestrator parses "
        "its last line as a receipt and reports the failure against the file")
    assert "jsonlint.py" in cmd
    assert (_ROOT / "validators" / "jsonlint" / "jsonlint.py").exists()


def test_every_configured_validator_cmd_is_an_adapter() -> None:
    """The sibling check: no other validator is wired to a raw tool."""
    cfg = json.loads((_ROOT / ".supertool.json").read_text(encoding="utf-8"))
    for name, spec in cfg.get("validators", {}).items():
        cmd = spec.get("cmd")
        if not cmd or spec.get("builtin"):
            continue
        assert "{supertool_dir}" in cmd or "validators" in cmd, (
            f"{name} is wired to a raw tool ({cmd!r}), not a SCHEMA.md adapter")
