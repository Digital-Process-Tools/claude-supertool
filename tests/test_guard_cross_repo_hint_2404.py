"""Cross-repo `gh pr diff -R`/`gh issue view -R` gets a cross-repo remedy (#2404).

`gh pr diff N -R owner/repo` matches the same shipped `replaces` entry as the
same-repo form, so the un-prefixed `use` that entry declares
(`gh-pr:NUMBER:diff`) is what the refusal showed -- silently dropping the
`-R` target and pointing at a form that resolves against the CALLER'S OWN
repo instead of the one the command named. A working cross-repo route
already exists (`repo:OWNER/NAME` chained ahead of the same op --
`presets/_repo_target.py`'s leading-op convention, verified live against
`Digital-Process-Tools/claude-remember#623` while investigating this issue)
and the refusal simply never pointed at it. Fixed by prepending a
`repo:OWNER/NAME ` prefix to the `use` hint whenever the blocked command
carries `-R`/`--repo` and the matched op actually reads `SUPERTOOL_REPO`.

Red before the fix: `verdict.matches[0].use` was the bare
`"gh-pr:NUMBER:diff"` for a command that named a different repository
entirely -- a remedy that silently discards the `-R` target if followed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent
_GITHUB_OPS = json.loads(
    (_ROOT / "presets" / "github.json").read_text(encoding="utf-8"))["ops"]


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": _GITHUB_OPS}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    supertool._load_config()


@pytest.fixture
def shipped_github(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The real github preset as the effective registry."""
    _load(tmp_path, monkeypatch)
    return tmp_path


def test_cross_repo_pr_diff_points_at_the_repo_op(shipped_github):
    verdict = supertool.guard_command(
        "gh pr diff 265 -R Digital-Process-Tools/claude-remember")
    assert verdict.state == "blocked", verdict
    uses = [m.use for m in verdict.matches]
    assert uses == [
        "repo:Digital-Process-Tools/claude-remember gh-pr:NUMBER:diff"
    ], uses


def test_cross_repo_pr_diff_long_flag(shipped_github):
    verdict = supertool.guard_command(
        "gh pr diff 265 --repo Digital-Process-Tools/claude-remember")
    uses = [m.use for m in verdict.matches]
    assert uses == [
        "repo:Digital-Process-Tools/claude-remember gh-pr:NUMBER:diff"
    ], uses


def test_cross_repo_issue_view(shipped_github):
    verdict = supertool.guard_command(
        "gh issue view 623 -R Digital-Process-Tools/claude-remember")
    uses = [m.use for m in verdict.matches]
    assert uses == [
        "repo:Digital-Process-Tools/claude-remember gh-issue:NUMBER"
    ], uses


def test_same_repo_form_is_unchanged(shipped_github):
    """The common case keeps its plain `use` -- no `repo:` prefix appears
    when nothing in the command names one. This must not become a lane that
    widens the guard for everyone while narrowing what it actually catches.
    """
    verdict = supertool.guard_command("gh pr diff 265")
    assert verdict.state == "blocked", verdict
    uses = [m.use for m in verdict.matches]
    assert uses == ["gh-pr:NUMBER:diff"], uses
    assert "repo:" not in uses[0]


def test_a_malformed_repo_flag_is_not_echoed_back(shipped_github):
    """A `-R` value that fails the same shape check the `repo:` op's own
    dispatch applies is silently omitted rather than offered as a working
    alternative -- see `_repo_shape_error`. Guessing at a bad value would be
    worse than saying nothing: it would look like a working remedy."""
    verdict = supertool.guard_command("gh pr diff 265 -R not-a-repo-shape")
    uses = [m.use for m in verdict.matches]
    assert uses == ["gh-pr:NUMBER:diff"], uses


def test_an_op_with_no_repo_dimension_gets_no_hint(shipped_github):
    """`gh-pr-create` is `payload`-mode, not `op`-mode -- it does not read
    `SUPERTOOL_REPO` directly (`_repo_target_ops()` excludes it), so no
    `repo:` prefix is offered even though the command carries `-R`."""
    verdict = supertool.guard_command(
        "gh pr create -t x -b y -R Digital-Process-Tools/claude-remember")
    for m in verdict.matches:
        assert not m.use.startswith("repo:"), m.use
