"""gh-pr-create never guesses a base, and never renders zero checks as pending (#950)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

MOD_PATH = Path(__file__).parent.parent / "presets" / "github" / "pr_create.py"
_spec = importlib.util.spec_from_file_location("github_pr_create", MOD_PATH)
assert _spec is not None and _spec.loader is not None
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _text(lines) -> str:
    return "\n".join(lines)


# ===========================================================================
# validate — the base is never guessed
# ===========================================================================

def test_missing_base_is_refused_and_says_why():
    err = m.validate({"repo": "o/r", "title": "t", "body": "b"})
    assert err is not None
    assert "base" in err
    low = err.lower()
    assert "guess" in low or "never" in low, err


def test_missing_title_is_refused():
    assert m.validate({"repo": "o/r", "base": "master", "body": "b"}) is not None


def test_missing_repo_is_refused():
    assert m.validate({"title": "t", "base": "master", "body": "b"}) is not None


def test_body_and_body_file_together_is_refused():
    err = m.validate({"repo": "o/r", "title": "t", "base": "master",
                      "body": "b", "body_file": "f"})
    assert err is not None and "body_file" in err


def test_a_complete_payload_validates():
    assert m.validate({"repo": "o/r", "title": "t", "base": "master",
                       "body": "b"}) is None


def test_base_equal_to_head_is_refused():
    err = m.validate({"repo": "o/r", "title": "t", "base": "master",
                      "head": "master", "body": "b"})
    assert err is not None
    assert "master" in err


# ===========================================================================
# head — defaulted from repo state, refused when the state is not readable
# ===========================================================================

def test_head_comes_from_the_payload_when_given():
    head, source, err = m.resolve_head({"head": "fix/950"}, "other", "")
    assert head == "fix/950"
    assert err == ""
    assert "payload" in source


def test_head_defaults_to_the_current_branch_and_says_so():
    head, source, err = m.resolve_head({}, "fix/950", "")
    assert head == "fix/950"
    assert err == ""
    assert "current branch" in source


def test_detached_head_is_refused_not_defaulted():
    head, source, err = m.resolve_head({}, "", "detached HEAD")
    assert err != ""
    assert "head" in err.lower()


# ===========================================================================
# checks — zero is not pending
# ===========================================================================

def test_zero_checks_says_nothing_was_created_not_that_it_is_pending():
    lines, state = m.checks_section([], 30, "a" * 40)
    assert state == m.NO_CHECKS_YET
    body = _text(lines)
    assert "zero" in body.lower(), body
    assert "pending" not in body.lower() or "not" in body.lower(), body


def test_zero_checks_inside_the_creation_window_says_a_run_is_still_expected():
    lines, state = m.checks_section([], 30, "a" * 40)
    body = _text(lines)
    assert "expected" in body.lower() or "still" in body.lower(), body


def test_zero_checks_past_the_window_says_no_workflow_may_cover_this_ref():
    lines, state = m.checks_section([], m.CHECK_CREATION_GRACE + 60, "a" * 40)
    assert state == m.NO_CHECKS_YET
    body = _text(lines)
    assert "UNKNOWN" in body, body


def test_checks_read_render_the_shared_tally():
    rollup = [{"name": "tests", "conclusion": "SUCCESS"},
              {"name": "e2e", "conclusion": "CANCELLED"}]
    lines, state = m.checks_section(rollup, 30, "a" * 40)
    assert state == m.CHECKS_READ
    body = _text(lines)
    assert "2 total" in body and "1 cancelled" in body, body


def test_a_rollup_that_could_not_be_read_is_unknown_not_zero():
    lines, state = m.checks_section(None, 30, "a" * 40, read_error="gh timed out")
    assert state == m.CHECKS_UNKNOWN
    body = _text(lines)
    assert "gh timed out" in body
    assert "UNKNOWN" in body


def test_unestablished_age_is_not_read_as_fresh():
    lines, state = m.checks_section([], None, "a" * 40)
    assert state == m.NO_CHECKS_YET
    assert "UNKNOWN" in _text(lines)


# ===========================================================================
# the verdict line
# ===========================================================================

def test_result_line_names_the_number_and_the_check_state():
    line = m.result_line("951", m.NO_CHECKS_YET, "links #950")
    assert "951" in line
    assert line.startswith("[result]")
    assert "\n" not in line


def test_result_line_carries_an_unknown_check_state():
    line = m.result_line("951", m.CHECKS_UNKNOWN, "")
    assert "unknown" in line.lower()
