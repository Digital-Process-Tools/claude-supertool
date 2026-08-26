"""`gh-pr-edit` — update a published PR body from the payload that wrote it (#1739).

Three properties, each of which the raw route gets wrong:

* **The closing-reference check survives the update.** `gh-pr-create` parses
  `Closes #N` at creation; a body replaced by hand bypasses it entirely, and
  replacing a body is exactly when a `Closes` line goes missing — the new text
  is usually pasted from somewhere else. The three states matter more than the
  refusal: a check that could not read the old body must not report the same
  thing as one that read it and found nothing lost.
* **The receipt proves what landed.** `gh api -X PATCH` answered a bare
  timestamp on the tick that filed this, which says a write happened and not
  which bytes are on the server. The PATCH response carries the stored body,
  so a byte comparison is available without a second call — and the op is only
  useful if it makes it.
* **Whatever it did not apply, it names.** The payload is `gh-pr-create`'s, so
  it carries `base`, `head`, `draft`, `labels`. An update silently ignoring
  half its own input is the absence-read-as-a-clean-result defect with a
  publishing consequence.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent
MOD_PATH = _ROOT / "presets" / "github" / "pr_edit.py"
_spec = importlib.util.spec_from_file_location("github_pr_edit", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

_GH_OPS = json.loads(
    (_ROOT / "presets" / "github.json").read_text(encoding="utf-8"))["ops"]


# ===========================================================================
# argument parsing — a number this op cannot use is refused, never coerced
# ===========================================================================

def test_number_and_payload_are_read_off_the_colon_form():
    number, path, unlink, err = m.parse_args(["1737", "@.max/pr.toml"])
    assert err == ""
    assert number == "1737"
    assert path == "@.max/pr.toml"
    assert unlink is False


def test_the_unlink_token_is_read_and_is_not_the_default():
    _n, _p, unlink, err = m.parse_args(["1737", "@-", "unlink"])
    assert err == ""
    assert unlink is True


def test_a_token_this_op_does_not_have_is_refused_and_names_the_one_it_has():
    _n, _p, _u, err = m.parse_args(["1737", "@-", "force"])
    assert err != ""
    assert "unlink" in err, err


def test_a_missing_payload_is_refused():
    _n, _p, _u, err = m.parse_args(["1737"])
    assert err != ""
    assert "payload" in err.lower(), err


def test_a_non_ascii_digit_number_is_refused_not_coerced():
    # U+0661 ARABIC-INDIC ONE. `int()` accepts it; GitHub does not.
    _n, _p, _u, err = m.parse_args(["١737", "@-"])
    assert err != ""


def test_a_missing_number_is_refused():
    _n, _p, _u, err = m.parse_args([])
    assert err != ""


def test_a_windows_drive_letter_is_reassembled_not_read_as_a_stray_token():
    """supertool splits the op argument on ':', so a drive-lettered payload
    path arrives in two pieces. Refusing the second as an unknown trailing
    token would name `unlink` at a caller whose only mistake was standing on
    Windows."""
    number, path, unlink, err = m.parse_args(
        ["1737", "@C", "\\repo\\pr.toml"])
    assert err == "", err
    assert number == "1737"
    assert path == "@C:\\repo\\pr.toml"
    assert unlink is False


def test_a_drive_letter_path_still_reads_the_unlink_token():
    _n, path, unlink, err = m.parse_args(
        ["1737", "@C", "\\repo\\pr.toml", "unlink"])
    assert err == ""
    assert path == "@C:\\repo\\pr.toml"
    assert unlink is True


def test_an_empty_body_string_is_still_a_body_and_is_not_refused():
    """`body = ""` clears the pull request body on purpose. An absent `body`
    key does not, and the two must not be read as the same request."""
    assert m.validate({"repo": "o/r", "body": ""}) is None


def test_an_empty_body_beside_a_body_file_is_still_two_sources():
    err = m.validate({"repo": "o/r", "body": "", "body_file": "f"})
    assert err is not None and "body_file" in err


# ===========================================================================
# payload — the create payload is accepted whole, and what is dropped is said
# ===========================================================================

def test_a_payload_with_no_body_at_all_is_refused():
    err = m.validate({"repo": "o/r", "title": "t"})
    assert err is not None
    assert "body" in err


def test_body_and_body_file_together_is_refused():
    err = m.validate({"repo": "o/r", "body": "b", "body_file": "f"})
    assert err is not None and "body_file" in err


def test_a_create_payload_validates_unchanged():
    assert m.validate({"repo": "o/r", "title": "t", "base": "master",
                       "body": "Closes #1739."}) is None


def test_the_create_only_fields_are_named_as_ignored_not_silently_dropped():
    ignored = m.ignored_fields({"repo": "o/r", "body": "b", "base": "master",
                                "head": "fix/1739", "draft": True,
                                "labels": ["x"]})
    assert set(ignored) == {"base", "head", "draft", "labels"}


def test_a_payload_with_nothing_to_ignore_reports_nothing():
    assert m.ignored_fields({"repo": "o/r", "body": "b", "title": "t"}) == []


# ===========================================================================
# the closing-reference gate — three states, and `unknown` is not `ok`
# ===========================================================================

def test_a_reference_carried_through_the_update_is_ok():
    state, lost, _msg = m.closing_ref_verdict(
        "Closes #1739.\n\nold prose", "Closes #1739.\n\nnew prose", "")
    assert state == m.REF_OK
    assert lost == []


def test_a_reference_the_new_body_drops_is_the_finding():
    state, lost, msg = m.closing_ref_verdict(
        "Closes #1739.\n\nold prose", "new prose with no reference", "")
    assert state == m.REF_DROPPED
    assert lost == ["#1739"]
    assert "#1739" in msg


def test_an_old_body_that_could_not_be_read_is_unknown_and_never_ok():
    state, lost, msg = m.closing_ref_verdict(None, "Closes #1739.", "gh timed out")
    assert state == m.REF_UNKNOWN
    assert lost == []
    assert "gh timed out" in msg


def test_a_published_body_that_is_null_is_a_body_with_nothing_to_lose():
    """A pull request opened with no body at all. `None` reaches the gate as
    `""` from `main`, and there is genuinely nothing to drop — this is the
    positive control for the `UNKNOWN` case above, which must not render the
    same way."""
    state, lost, _msg = m.closing_ref_verdict("", "Closes #1739.", "")
    assert state == m.REF_OK
    assert lost == []


def test_an_old_body_that_had_no_reference_to_lose_is_ok_not_dropped():
    state, lost, _msg = m.closing_ref_verdict("no reference here", "still none", "")
    assert state == m.REF_OK
    assert lost == []


def test_a_reference_only_the_new_body_has_is_not_a_loss():
    state, lost, _msg = m.closing_ref_verdict("no reference", "Closes #1739.", "")
    assert state == m.REF_OK
    assert lost == []


def test_a_reference_moved_into_a_code_fence_counts_as_dropped():
    """GitHub does not read a fenced `Closes`, so neither does the gate.

    The positive control for the negative assertion above: `_checks` strips
    fences, so a body that "still says Closes #1739" inside one has in fact
    unlinked the issue, and a gate that matched raw text would call it ok.
    """
    new = "prose\n\n```\nCloses #1739.\n```\n"
    state, lost, _msg = m.closing_ref_verdict("Closes #1739.", new, "")
    assert state == m.REF_DROPPED
    assert lost == ["#1739"]


@pytest.mark.parametrize("state,unlink,expected", [
    (m.REF_OK, False, True),
    (m.REF_OK, True, True),
    (m.REF_DROPPED, False, False),
    (m.REF_DROPPED, True, True),
    (m.REF_UNKNOWN, False, False),
    (m.REF_UNKNOWN, True, True),
])
def test_only_an_explicit_unlink_lets_a_dropped_or_unverified_reference_through(
        state, unlink, expected):
    assert m.may_write(state, unlink) is expected


# ===========================================================================
# the receipt — a write that landed something else does not render as success
# ===========================================================================

def test_a_byte_identical_body_is_the_exact_verdict():
    state, _msg = m.landed_verdict("Closes #1739.\n\nbody", "Closes #1739.\n\nbody")
    assert state == m.LANDED_EXACT


def test_a_body_that_came_back_different_is_a_mismatch_naming_both_lengths():
    state, msg = m.landed_verdict("what was sent", "what the server stored")
    assert state == m.LANDED_MISMATCH
    assert "13" in msg and "22" in msg, msg


def test_line_endings_normalised_by_the_server_are_their_own_state():
    """Not `exact`, because the bytes differ; not `mismatch`, because nothing
    was lost. A CRLF payload is the ordinary case on a Windows checkout."""
    state, msg = m.landed_verdict("a\r\nb\r\n", "a\nb\n")
    assert state == m.LANDED_NORMALISED
    assert "line ending" in msg.lower(), msg


def test_a_response_carrying_no_body_field_is_unknown_never_exact():
    state, msg = m.landed_verdict("sent", None)
    assert state == m.LANDED_UNKNOWN
    assert "unknown" in msg.lower(), msg


def test_the_result_line_never_reads_as_a_success_when_nothing_was_verified():
    line = m.result_line("1737", m.REF_OK, m.LANDED_UNKNOWN, "")
    assert line.startswith("[result]")
    assert "UNVERIFIED" in line, line


def test_the_result_line_says_verified_only_on_an_exact_match():
    line = m.result_line("1737", m.REF_OK, m.LANDED_EXACT, "")
    assert "verified" in line.lower(), line
    assert "UNVERIFIED" not in line, line


def test_a_mismatch_result_line_does_not_say_verified():
    line = m.result_line("1737", m.REF_OK, m.LANDED_MISMATCH, "")
    assert "verified" not in line.lower().replace("unverified", ""), line


def test_an_unlinked_update_says_so_on_the_one_line_that_survives_a_tail():
    line = m.result_line("1737", m.REF_DROPPED, m.LANDED_EXACT, "unlink")
    assert "unlink" in line.lower(), line


# ===========================================================================
# the title — applied when the payload carries one, and always reported
# ===========================================================================

def test_an_unchanged_title_is_reported_as_unchanged_and_not_sent():
    fields, lines = m.title_change({"title": "same"}, "same")
    assert "title" not in fields
    assert "unchanged" in " ".join(lines).lower()


def test_a_changed_title_is_sent_and_both_sides_are_shown():
    fields, lines = m.title_change({"title": "new"}, "old")
    assert fields["title"] == "new"
    body = " ".join(lines)
    assert "old" in body and "new" in body


# ---------------------------------------------------------------------------
# the title is verified on its own axis, because a title that did not land is
# not a body that did not land (found by the review of the first commit)
# ---------------------------------------------------------------------------

def test_no_title_sent_is_its_own_state_and_is_not_a_verification():
    state, msg = m.title_verdict(None, "whatever is published")
    assert state == m.TITLE_NOT_SENT
    assert msg == ""


def test_a_title_that_came_back_as_sent_is_verified():
    state, msg = m.title_verdict("new", "new")
    assert state == m.TITLE_EXACT
    assert "verified" in msg.lower()


def test_a_title_the_server_did_not_store_is_a_mismatch():
    state, msg = m.title_verdict("new", "something else")
    assert state == m.TITLE_MISMATCH
    assert "something else" in msg


def test_a_response_with_no_title_field_is_a_mismatch_not_a_pass():
    state, _msg = m.title_verdict("new", None)
    assert state == m.TITLE_MISMATCH


def test_a_failed_title_never_renders_as_a_failed_body():
    """The receipt has two axes and the result line must not merge them: a
    byte-perfect body under a title that did not land used to print `body on
    the server is NOT what was sent`, which is false about the body."""
    line = m.result_line("1737", m.REF_OK, m.LANDED_EXACT, "", m.TITLE_MISMATCH)
    assert "TITLE" in line, line
    assert "body verified byte-identical" in line, line


def test_a_verified_title_is_said_and_an_unsent_one_is_not_claimed():
    sent = m.result_line("1737", m.REF_OK, m.LANDED_EXACT, "", m.TITLE_EXACT)
    assert "title verified" in sent
    quiet = m.result_line("1737", m.REF_OK, m.LANDED_EXACT, "", m.TITLE_NOT_SENT)
    assert "title" not in quiet.lower(), quiet


def test_a_payload_with_no_title_leaves_the_published_one_alone():
    fields, lines = m.title_change({}, "old")
    assert fields == {}
    assert "not touched" in " ".join(lines).lower()


# ===========================================================================
# the registry — the op ships, and the guard names it for the working route
# ===========================================================================

def test_the_op_is_registered_with_a_safety_class_and_a_grammar():
    entry = _GH_OPS["gh-pr-edit"]
    assert entry["safety"] == "acts"
    assert "gh-pr-edit:" in entry["syntax"]
    assert "#" not in entry["syntax"], "syntax is grammar, not provenance"


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
    "gh pr edit 1737 --body-file body.md",
    "gh pr edit 1737 --body text",
    "gh pr edit 1737 --title new-title",
])
def test_the_body_and_title_spellings_of_gh_pr_edit_are_refused(
        shipped_github, command):
    verdict = supertool.guard_command(command)
    assert verdict.state == "blocked", (command, verdict)
    assert any("gh-pr-edit" in match.use for match in verdict.matches), verdict


@pytest.mark.parametrize("command", [
    # Everything else `gh pr edit` does, this op does not do. A block naming an
    # op that cannot relabel a PR is a dead end with no per-command way past.
    "gh pr edit 1737 --add-label priority-high",
    "gh pr edit 1737 --add-reviewer someone",
    "gh pr edit 1737 --milestone v0.46.0",
    "gh pr edit 1737 --base master",
    # A browser is not a thing any op opens.
    "gh pr edit 1737 --body-file body.md --web",
    # And a command that only describes itself performs nothing.
    "gh pr edit --help",
])
def test_the_shapes_this_op_does_not_answer_stay_clean(shipped_github, command):
    assert supertool.guard_command(command).state == "clean", command


# ===========================================================================
# #1909 — repo: reconciled against the payload's own repo field
# ===========================================================================

def _pr_payload(tmp_path: Path, data: dict) -> str:
    p = tmp_path / "pr_edit.json"
    p.write_text(json.dumps(data))
    return str(p)


def _install_edit_harness(monkeypatch, current: dict, response: dict):
    monkeypatch.setattr(
        m, "_gh_json",
        lambda args, timeout=30, **kw: (
            (current, "") if args[:2] == ["api"] and "-X" not in args
            else (response, "")))


def test_repo_op_supplies_a_silent_payload(monkeypatch, capsys, tmp_path):
    """target set, payload silent -> the target wins, stated with its source
    in the receipt."""
    payload_file = _pr_payload(tmp_path, {"body": "Closes #1739."})
    monkeypatch.setattr(sys, "argv", ["pr_edit.py", "1737", payload_file])
    monkeypatch.setenv("SUPERTOOL_REPO", "owner/from-repo-op")
    _install_edit_harness(
        monkeypatch, {"title": "t", "state": "OPEN", "body": "old"},
        {"body": "Closes #1739.", "title": "t", "html_url": "u"})

    assert m.main() == 0
    out = capsys.readouterr().out
    assert "owner/from-repo-op" in out
    assert "repo from repo: op" in out


def test_repo_op_and_agreeing_payload_proceed(monkeypatch, capsys, tmp_path):
    """Both present and agreeing is not ambiguous -- it proceeds."""
    payload_file = _pr_payload(
        tmp_path, {"repo": "o/r", "body": "Closes #1739."})
    monkeypatch.setattr(sys, "argv", ["pr_edit.py", "1737", payload_file])
    monkeypatch.setenv("SUPERTOOL_REPO", "o/r")
    _install_edit_harness(
        monkeypatch, {"title": "t", "state": "OPEN", "body": "old"},
        {"body": "Closes #1739.", "title": "t", "html_url": "u"})

    assert m.main() == 0
    assert "o/r" in capsys.readouterr().out


def test_repo_op_and_disagreeing_payload_refuse(monkeypatch, capsys, tmp_path):
    """Both present and disagreeing must refuse, naming both values and never
    guessing which wins -- the one arm that proves the check is for real.
    Would still pass if the code did nothing UNLESS paired with the agreeing
    case above, which proceeds."""
    payload_file = _pr_payload(
        tmp_path, {"repo": "o/r", "body": "Closes #1739."})
    monkeypatch.setattr(sys, "argv", ["pr_edit.py", "1737", payload_file])
    monkeypatch.setenv("SUPERTOOL_REPO", "owner/somewhere-else")

    def _must_not_be_called(*a, **kw):
        raise AssertionError("_gh_json was called despite the disagreement")
    monkeypatch.setattr(m, "_gh_json", _must_not_be_called)

    assert m.main() == 1
    out = capsys.readouterr().out
    assert "o/r" in out
    assert "owner/somewhere-else" in out
    assert "gh-pr-edit" in out
