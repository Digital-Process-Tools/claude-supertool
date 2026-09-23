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
    number, path, edit_id, err = m.parse_args(["2078", "@.max/comment.toml"])
    assert err == ""
    assert number == "2078"
    assert path == "@.max/comment.toml"
    assert edit_id == ""


def test_a_missing_payload_is_refused():
    _n, _p, _e, err = m.parse_args(["2078"])
    assert err != ""
    assert "payload" in err.lower(), err


def test_a_missing_number_is_refused():
    _n, _p, _e, err = m.parse_args([])
    assert err != ""


def test_a_non_ascii_digit_number_is_refused_not_coerced():
    # U+0661 ARABIC-INDIC ONE. `int()` accepts it; GitHub does not.
    _n, _p, _e, err = m.parse_args(["١737", "@-"])
    assert err != ""


def test_a_windows_drive_letter_is_reassembled_not_read_as_a_stray_token():
    number, path, edit_id, err = m.parse_args(["2078", "@C", "\\repo\\comment.toml"])
    assert err == "", err
    assert number == "2078"
    assert path == "@C:\\repo\\comment.toml"
    assert edit_id == ""


def test_a_trailing_token_this_op_does_not_have_is_refused():
    _n, _p, _e, err = m.parse_args(["2078", "@-", "unlink"])
    assert err != ""


# ---------------------------------------------------------------------------
# edit=COMMENT_ID (#2643)
# ---------------------------------------------------------------------------

def test_a_trailing_edit_token_is_read_off_and_stripped_from_the_payload_slot():
    number, path, edit_id, err = m.parse_args(["2078", "@-", "edit=555"])
    assert err == "", err
    assert number == "2078"
    assert path == "@-"
    assert edit_id == "555"


def test_a_non_numeric_edit_id_is_refused():
    _n, _p, _e, err = m.parse_args(["2078", "@-", "edit=abc"])
    assert err != ""
    assert "comment id" in err.lower(), err


def test_edit_still_refuses_an_unrelated_second_trailing_token():
    _n, _p, _e, err = m.parse_args(["2078", "@-", "unlink", "edit=555"])
    assert err != "", "edit=555 must not silently swallow an earlier stray token"


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


def test_a_toml_body_table_array_is_refused_not_stringified_2322():
    # #2322: a `[[body]]` TOML table-array parses as a list of
    # `{"value": ...}` dicts. `_body_text` used to do
    # `str(payload.get("body") or "")`, which happily stringifies a list --
    # publishing the literal Python repr (`[{'value': '...'}]`) as the
    # comment body. Confirmed against a real posted comment on issue #2310.
    # #2315 made the same call for gh-issue-create's sibling case: refuse
    # instead of guessing at a join. Applied here too.
    payload = {"repo": "o/r", "body": [{"value": "hello"}]}
    err = m.validate(payload)
    assert err is not None, "a list body must be refused by validate()"
    assert "string" in err.lower(), err
    assert "list" in err, err


def test_a_toml_body_table_array_never_reaches_the_publish_step_2322():
    # Even if validate() were bypassed, _body_text itself must refuse a
    # non-string body rather than stringify it -- never a Python repr as
    # the content to publish.
    content, err = m._body_text({"body": [{"value": "hello"}]})
    assert err != "", "a list body must be refused by _body_text() too"
    assert "{'value'" not in content, (
        f"a Python repr leaked into the published body: {content!r}")


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


def test_main_create_path_posts_to_the_collection_endpoint_not_patch(
        monkeypatch, capsys, tmp_path):
    # Positive control for the create/POST branch, now that main() branches
    # on edit_id between two transports (#2643) -- without this, a
    # regression that made the create path also emit PATCH would pass every
    # other test in this file, since none of them inspect the args _gh_json
    # was actually called with.
    payload_file = _payload(tmp_path, {"repo": "o/r", "body": "scoping note"})
    monkeypatch.setattr(sys, "argv", ["issue_comment.py", "2078", payload_file])

    seen = {}

    def fake_gh_json(args, stdin=None, timeout=30):
        seen["args"] = args
        return ({"id": 555, "body": "scoping note", "html_url": "https://x/555"}, "")
    monkeypatch.setattr(m, "_gh_json", fake_gh_json)

    assert m.main() == 0
    assert "-X" in seen["args"] and seen["args"][seen["args"].index("-X") + 1] == "POST"
    assert "repos/o/r/issues/2078/comments" in seen["args"]
    assert "repos/o/r/issues/comments/" not in " ".join(seen["args"])


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
# end-to-end through main() -- edit=COMMENT_ID (#2643)
# ===========================================================================

def test_main_edit_patches_the_existing_comment_not_the_create_endpoint(
        monkeypatch, capsys, tmp_path):
    payload_file = _payload(tmp_path, {"repo": "o/r", "body": "corrected note"})
    monkeypatch.setattr(
        sys, "argv", ["issue_comment.py", "2078", payload_file, "edit=555"])

    calls = []

    def fake_gh_json(args, stdin=None, timeout=30):
        calls.append(args)
        if "-X" not in args:
            # the ownership GET (#2665) -- comment 555 really is on #2078
            return ({"id": 555,
                      "issue_url": "https://api.github.com/repos/o/r/issues/2078"}, "")
        return ({"id": 555, "body": "corrected note", "html_url": "https://x/555"}, "")
    monkeypatch.setattr(m, "_gh_json", fake_gh_json)

    assert m.main() == 0
    assert len(calls) == 2
    assert "repos/o/r/issues/comments/555" in calls[0]
    patch_call = calls[1]
    assert "-X" in patch_call and patch_call[patch_call.index("-X") + 1] == "PATCH"
    assert "repos/o/r/issues/comments/555" in patch_call
    out = capsys.readouterr().out
    assert "edited" in out
    assert "byte-identical" in out


def test_main_edit_reports_not_edited_when_the_patch_itself_fails(
        monkeypatch, capsys, tmp_path):
    payload_file = _payload(tmp_path, {"repo": "o/r", "body": "corrected note"})
    monkeypatch.setattr(
        sys, "argv", ["issue_comment.py", "2078", payload_file, "edit=555"])

    def fake_gh_json(args, stdin=None, timeout=30):
        if "-X" not in args:
            return ({"id": 555,
                      "issue_url": "https://api.github.com/repos/o/r/issues/2078"}, "")
        return (None, "404 Not Found")
    monkeypatch.setattr(m, "_gh_json", fake_gh_json)

    assert m.main() == 1
    out = capsys.readouterr().out
    assert "NOT edited" in out


def test_main_edit_refuses_when_the_comment_belongs_to_a_different_issue(
        monkeypatch, capsys, tmp_path):
    # #2665: a mistyped or stale COMMENT_ID must never PATCH silently. The
    # GET here returns a real comment, but one that belongs to #9999, not
    # the #2078 the caller named -- must refuse before any PATCH runs.
    payload_file = _payload(tmp_path, {"repo": "o/r", "body": "corrected note"})
    monkeypatch.setattr(
        sys, "argv", ["issue_comment.py", "2078", payload_file, "edit=555"])

    calls = []

    def fake_gh_json(args, stdin=None, timeout=30):
        calls.append(args)
        return ({"id": 555,
                  "issue_url": "https://api.github.com/repos/o/r/issues/9999"}, "")
    monkeypatch.setattr(m, "_gh_json", fake_gh_json)

    assert m.main() == 1
    assert len(calls) == 1, "the PATCH must not run once ownership fails"
    out = capsys.readouterr().out
    assert "NOT edited" in out
    assert "9999" in out
    assert "2078" in out


def test_main_edit_refuses_when_the_ownership_check_itself_fails(
        monkeypatch, capsys, tmp_path):
    # The GET that should confirm ownership errors outright (404, network,
    # bad JSON) -- refuse rather than proceeding to PATCH on no evidence.
    #
    # The PATCH branch is stubbed to SUCCEED (unlike the GET) so this test
    # cannot pass by accident if the ownership GET were removed entirely:
    # without the #2665 check, main() would go straight to a successful
    # PATCH and report "edited", not "NOT edited".
    payload_file = _payload(tmp_path, {"repo": "o/r", "body": "corrected note"})
    monkeypatch.setattr(
        sys, "argv", ["issue_comment.py", "2078", payload_file, "edit=555"])

    calls = []

    def fake_gh_json(args, stdin=None, timeout=30):
        calls.append(args)
        if "-X" not in args:
            return (None, "404 Not Found")
        return ({"id": 555, "body": "corrected note", "html_url": "https://x/555"}, "")
    monkeypatch.setattr(m, "_gh_json", fake_gh_json)

    assert m.main() == 1
    assert len(calls) == 1, "the PATCH must not run once the ownership GET fails"
    out = capsys.readouterr().out
    assert "NOT edited" in out
    assert "edited" not in out.replace("NOT edited", "")


def test_main_edit_refuses_when_the_ownership_get_returns_a_non_dict(
        monkeypatch, capsys, tmp_path):
    # A malformed or unexpected gh response (e.g. a JSON array) is not a
    # dict, and must be treated the same as a GET failure -- refuse rather
    # than reading .get() off something that is not the comment object.
    payload_file = _payload(tmp_path, {"repo": "o/r", "body": "corrected note"})
    monkeypatch.setattr(
        sys, "argv", ["issue_comment.py", "2078", payload_file, "edit=555"])

    calls = []

    def fake_gh_json(args, stdin=None, timeout=30):
        calls.append(args)
        return ([1, 2, 3], "")
    monkeypatch.setattr(m, "_gh_json", fake_gh_json)

    assert m.main() == 1
    assert len(calls) == 1, "the PATCH must not run once the ownership GET is malformed"
    out = capsys.readouterr().out
    assert "NOT edited" in out


# ---------------------------------------------------------------------------
# edit_target_error -- unit-level, no gh call (#2665)
# ---------------------------------------------------------------------------

def test_edit_target_error_is_empty_when_issue_url_matches():
    assert m.edit_target_error(
        {"id": 555, "issue_url": "https://api.github.com/repos/o/r/issues/2078"},
        "", "2078") == ""


def test_edit_target_error_names_the_real_issue_on_mismatch():
    err = m.edit_target_error(
        {"id": 555, "issue_url": "https://api.github.com/repos/o/r/issues/9999"},
        "", "2078")
    assert err != ""
    assert "9999" in err
    assert "2078" in err


def test_edit_target_error_fires_when_issue_url_is_missing_entirely():
    # A malformed or unexpected response must not read as a match: an
    # absent issue_url is not a positive-control silence for "same issue".
    err = m.edit_target_error({"id": 555}, "", "2078")
    assert err != ""


def test_edit_target_error_reports_the_get_failure_when_the_get_itself_failed():
    err = m.edit_target_error(None, "404 Not Found", "2078")
    assert err != ""
    assert "404" in err


def test_main_edit_with_a_non_numeric_id_is_refused_before_any_gh_call(
        monkeypatch, capsys, tmp_path):
    payload_file = _payload(tmp_path, {"repo": "o/r", "body": "x"})
    monkeypatch.setattr(
        sys, "argv", ["issue_comment.py", "2078", payload_file, "edit=abc"])

    def _must_not_be_called(*a, **k):
        raise AssertionError("gh must not be called when edit_id is invalid")
    monkeypatch.setattr(m, "_gh_json", _must_not_be_called)

    assert m.main() == 1
    out = capsys.readouterr().out
    assert "comment id" in out.lower()


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
