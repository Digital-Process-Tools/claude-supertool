"""The copy-pasteable `gh api` hints must name the targeted repo (#673).

`gh api repos/{owner}/{repo}/…` expands those two literal placeholders from the
**cwd's** git remote. Two of `job.py`'s hints are printed as text for a human to
copy, directly beneath data that was fetched from the repo target — so under a
target they hand the reader a command pointing at a different repository than
the output above it.

That is the precise failure #673 exists to close, arriving in the one place a
test could not see it: with no target set, `_api_repo_path` returns the
placeholder string byte-for-byte, so both spellings look identical until
someone actually uses a target.

The harm test that ranks this above cosmetics: can a reader acting reasonably
on this output conclude the opposite of the truth? Yes — they run the suggested
command against their cwd's repo, get a 404 or, worse, a *different job with
the same ID*, and conclude the op lied about the job.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PRESET = Path(__file__).parent.parent / "presets" / "github" / "job.py"
_spec = importlib.util.spec_from_file_location("github_job_673_hints", PRESET)
assert _spec is not None and _spec.loader is not None
job = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(job)

TARGET = "Digital-Process-Tools/claude-remember"


def test_missing_log_hint_names_the_target(monkeypatch) -> None:
    monkeypatch.setenv("SUPERTOOL_REPO", TARGET)

    msg = job._missing_log_message("999", None, False, "boom")

    assert f"gh api repos/{TARGET}/actions/jobs/999" in msg
    assert "{owner}" not in msg, (
        "the hint still carries gh's cwd placeholders, so it points at "
        f"whatever repo the caller is standing in rather than {TARGET}:\n{msg}"
    )


def test_missing_log_hint_names_the_cwd_repo_without_a_target(
        monkeypatch) -> None:
    """No target: the hint names the cwd's repository rather than gh's placeholders.

    This assertion is inverted from the one #673 shipped, on purpose (#1679).
    It used to demand the string stay byte-identical without a target, which
    was right while the only consumer of the difference was a `repo:` call —
    but the *placeholder* form is itself the defect for a line a reader pastes:
    `gh api` resolves `{owner}`/`{repo}` from whatever cwd it lands in, so the
    same command names a different repository in every checkout and says
    nothing about having changed meaning. #1670 drew that distinction in
    `pr_merge`; this is the same one, arriving here.
    """
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    monkeypatch.setattr(job._repo_target, "cwd_slug",
                        lambda timeout=15: "o/r")

    msg = job._missing_log_message("999", None, False, "boom")

    assert "gh api repos/o/r/actions/jobs/999" in msg, msg
    assert "{owner}" not in msg, msg


def test_missing_log_hint_declines_to_the_placeholders_when_the_slug_is_unread(
        monkeypatch) -> None:
    """The third state: gh could not say which repository this is.

    The placeholders are still a *correct* command in the reader's own
    checkout, so this loses the improvement and nothing else — never a path
    built out of half an answer.
    """
    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    monkeypatch.setattr(job._repo_target, "cwd_slug", lambda timeout=15: "")

    msg = job._missing_log_message("999", None, False, "boom")

    assert "gh api repos/{owner}/{repo}/actions/jobs/999" in msg, msg


@pytest.mark.parametrize("suffix", ["", "/logs"])
def test_api_path_helper_substitutes_the_target(monkeypatch, suffix) -> None:
    """The helper both hints now route through, pinned on its own."""
    monkeypatch.setenv("SUPERTOOL_REPO", TARGET)
    assert job._api_repo_path(f"actions/jobs/999{suffix}") == (
        f"repos/{TARGET}/actions/jobs/999{suffix}")

    monkeypatch.delenv("SUPERTOOL_REPO", raising=False)
    assert job._api_repo_path(f"actions/jobs/999{suffix}") == (
        f"repos/{{owner}}/{{repo}}/actions/jobs/999{suffix}")
