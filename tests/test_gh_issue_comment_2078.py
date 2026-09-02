"""`gh-issue-comment` -- comment on an issue with the same guarantee gh-pr-edit
gives a published body: a read-back that proves what landed (#2078).

Every other write op in this family reads back what it wrote. Commenting was
the one route left to raw `gh issue comment --body-file`, which is a write
nobody reads: it exits 0 the moment the API accepts the POST and never
compares the stored body against what was sent.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent
MOD_PATH = _ROOT / "presets" / "github" / "issue_comment.py"
_spec = importlib.util.spec_from_file_location("github_issue_comment", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

_GH_OPS = json.loads(
    (_ROOT / "presets" / "github.json").read_text(encoding="utf-8"))["ops"]


# ===========================================================================
# argument parsing
# ===========================================================================

def test_number_and_payload_are_read_off_the_colon_form():
    number, path, err = m.parse_args(["2078", "@.max/comment.toml"])
    assert err == ""
    assert number == "2078"
    assert path == "@.max/comment.toml"


def test_a_missing_payload_is_refused():
    _n, _p, err = m.parse_args(["2078"])
    assert err != ""
    assert "payload" in err.lower(), err


def test_a_missing_number_is_refused():
    _n, _p, err = m.parse_args([])
    assert err != ""


def test_a_non_ascii_digit_number_is_refused_not_coerced():
    # U+0661 ARABIC-INDIC ONE. `int()` accepts it; GitHub does not.
    _n, _p, err = m.parse_args(["١737", "@-"])
    assert err != ""


def test_a_windows_drive_letter_is_reassembled_not_read_as_a_stray_token():
    number, path, err = m.parse_args(["2078", "@C", "\\repo\\comment.toml"])
    assert err == "", err
    assert number == "2078"
    assert path == "@C:\\repo\\comment.toml"


def test_a_trailing_token_this_op_does_not_have_is_refused():
    _n, _p, err = m.parse_args(["2078", "@-", "unlink"])
    assert err != ""


# ===========================================================================
# payload
# ===========================================================================

def test_an_empty_body_string_is_still_a_body_and_is_not_refused():
    assert m.validate({"repo": "o/r", "body": ""}) is None


def test_a_payload_with_no_body_at_all_is_refused():
    err = m.validate({"repo": "o/r"})
    assert err is not None and "body" in err.lower()


def test_a_payload_with_both_body_and_body_file_is_refused():
    err = m.validate({"repo": "o/r", "body": "x", "body_file": "f"})
    assert err is not None and "body_file" in err


def test_a_payload_missing_repo_is_refused():
    err = m.validate({"body": "x"})
    assert err is not None and "repo" in err.lower()


# ===========================================================================
# landed verdict -- the read-back
# ===========================================================================

def test_byte_identical_is_exact():
    state, msg = m.landed_verdict("hello", "hello")
    assert state == m.LANDED_EXACT
    assert "identical" in msg


def test_crlf_normalised_by_the_server_is_normalised_not_exact():
    state, _msg = m.landed_verdict("a\r\nb", "a\nb")
    assert state == m.LANDED_NORMALISED


def test_different_bytes_is_mismatch_and_names_the_first_difference():
    state, msg = m.landed_verdict("line one\nline two", "line one\nDIFFERENT")
    assert state == m.LANDED_MISMATCH
    assert "line 2" in msg


def test_a_response_with_no_body_field_is_unknown_never_a_pass():
    state, msg = m.landed_verdict("hello", None)
    assert state == m.LANDED_UNKNOWN
    assert "UNKNOWN" in msg


# ===========================================================================
# end-to-end through main() -- the write and the read-back together
# ===========================================================================

def _payload(tmp_path: Path, data: dict) -> str:
    p = tmp_path / "comment.json"
    p.write_text(json.dumps(data))
    return str(p)


def test_main_writes_and_confirms_an_exact_landing(monkeypatch, capsys, tmp_path):
    payload_file = _payload(tmp_path, {"repo": "o/r", "body": "scoping note"})
    monkeypatch.setattr(sys, "argv", ["issue_comment.py", "2078", payload_file])
    monkeypatch.setattr(
        m, "_gh_json",
        lambda args, stdin=None, timeout=30: (
            {"id": 555, "body": "scoping note", "html_url": "https://x/555"}, ""))

    assert m.main() == 0
    out = capsys.readouterr().out
    assert "byte-identical" in out
    assert "[result]" in out
    assert "555" in out


def test_main_reports_mismatch_and_exits_nonzero(monkeypatch, capsys, tmp_path):
    payload_file = _payload(tmp_path, {"repo": "o/r", "body": "scoping note"})
    monkeypatch.setattr(sys, "argv", ["issue_comment.py", "2078", payload_file])
    monkeypatch.setattr(
        m, "_gh_json",
        lambda args, stdin=None, timeout=30: (
            {"id": 555, "body": "something else entirely", "html_url": "https://x/555"}, ""))

    assert m.main() == 1
    out = capsys.readouterr().out
    assert "NOT" in out


def test_main_refuses_to_write_when_the_post_itself_fails(monkeypatch, capsys, tmp_path):
    payload_file = _payload(tmp_path, {"repo": "o/r", "body": "scoping note"})
    monkeypatch.setattr(sys, "argv", ["issue_comment.py", "2078", payload_file])
    monkeypatch.setattr(
        m, "_gh_json", lambda args, stdin=None, timeout=30: (None, "422 Unprocessable"))

    assert m.main() == 1
    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "NOT" in out


def test_an_unrecognised_payload_key_is_refused_before_anything_is_written(
        monkeypatch, capsys, tmp_path):
    payload_file = _payload(
        tmp_path, {"repo": "o/r", "body": "x", "description": "wrong field"})
    monkeypatch.setattr(sys, "argv", ["issue_comment.py", "2078", payload_file])

    def _must_not_be_called(*a, **k):
        raise AssertionError("gh must not be called when the payload is refused")
    monkeypatch.setattr(m, "_gh_json", _must_not_be_called)

    assert m.main() == 1
    out = capsys.readouterr().out
    assert "unrecognised" in out


# ===========================================================================
# the registry -- the op ships and the guard names it for the working route
# ===========================================================================

def test_the_op_is_registered_with_a_safety_class_and_a_grammar():
    entry = _GH_OPS["gh-issue-comment"]
    assert entry["safety"] == "acts"
    assert "gh-issue-comment:" in entry["syntax"]


@pytest.fixture
def shipped_github(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": _GH_OPS}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    supertool._load_config()
    return tmp_path


@pytest.mark.parametrize("command", [
    "gh issue comment 2078 --body-file note.md",
    "gh issue comment 2078 --body text",
    "gh issue comment 2078 -b text",
])
def test_the_raw_comment_command_is_refused(shipped_github, command):
    verdict = supertool.guard_command(command)
    assert verdict.state == "blocked", (command, verdict)
    assert any("gh-issue-comment" in match.use for match in verdict.matches), verdict


@pytest.mark.parametrize("command", [
    "gh issue comment 2078 --body-file note.md --web",
    "gh issue comment --help",
])
def test_the_shapes_this_op_does_not_answer_stay_clean(shipped_github, command):
    assert supertool.guard_command(command).state == "clean", command
