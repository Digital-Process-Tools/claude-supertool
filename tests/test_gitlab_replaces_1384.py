"""#1384 — the GitLab ops declare which raw `glab` invocation they supersede.

#1347 shipped the registry-driven guard with four mappings, every one of them
in `presets/github.json`. `presets/gitlab.json` had none, and the markdown
layer that guard replaces never had a rule mentioning `glab` at all — zero
hits across the nine files in `.claude/jit-context/tools/00-manual/`. So the
GitLab ops have never had enforcement of any kind, before or after #1347.

The mappings are asserted against the **shipped** `presets/gitlab.json`, loaded
as the effective registry, so a test here goes red if a `replaces` block is
absent, renamed, or points at an op that cannot answer the command it claims.

What is deliberately *not* mapped is asserted just as hard. In this schema the
absence of an entry is the only escape hatch, and the opt-out
(`raw_command_guard: false`) is repo-global — so one over-broad GitLab mapping
disarms the whole gate, GitHub's four included. A command supertool cannot
answer must therefore stay silently allowed.

Two ops ship no mapping on purpose:

* `gl-api` — `_guard_score` has no negative term, so `{"argv": "glab api"}`
  would also block `glab api -X POST` / `-F` / `--input`, the write shapes
  gl-api itself names glab for ("GET-only by design"). supertool has no route
  for a GitLab write, so that block has no way past except turning the guard
  off everywhere. Expressing the GET-only mapping needs an exclusion term in
  the guard core, which is #1347's schema and not this preset's.
* `gl-runners` — `glab` has no runner command at all (`glab --help`, 1.86.0).
  The only raw route to the fleet is `glab api runners/...`, which is the
  `glab api` prefix above.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent
_GITLAB_OPS = json.loads(
    (_ROOT / "presets" / "gitlab.json").read_text(encoding="utf-8"))["ops"]


@pytest.fixture
def shipped_gitlab(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The real gitlab preset, as the effective registry.

    Fed through `ops` rather than `presets` so every assertion below is a
    statement about the file in this commit — not about preset resolution
    order, and not about whether `glab` happens to exist on the runner.
    """
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": _GITLAB_OPS}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    supertool._load_config()
    return tmp_path


def _uses(command: str):
    verdict = supertool.guard_command(command)
    assert verdict.state == "blocked", (command, verdict)
    return sorted(match.use for match in verdict.matches)


# --------------------------------------------------------------------------
# The mappings
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command,use", [
    ("glab mr view 5", "gl-mr:NUMBER_OR_BRANCH"),
    ("glab mr view feat/1384", "gl-mr:NUMBER_OR_BRANCH"),
    ("glab mr list --per-page 50", "gl-mrs"),
    ("glab issue view 42", "gl-issue:NUMBER"),
    ("glab issue create --title x", "gl-issue-create:@FILE"),
    ("glab ci trace 224356863", "gl-job:NUMBER"),
])
def test_a_raw_glab_call_names_the_op_that_answers_it(
        shipped_gitlab, command, use):
    assert _uses(command) == [use], command


@pytest.mark.parametrize("command,use", [
    # A flag selects WHICH op is named, the discrimination #1347 built and the
    # `gh pr view --json state` / `--json files` pair is the worked example of.
    ("glab mr view 5 --comments", "gl-mr:NUMBER_OR_BRANCH:full"),
    ("glab mr view 5 -c", "gl-mr:NUMBER_OR_BRANCH:full"),
    ("glab mr view -c 5", "gl-mr:NUMBER_OR_BRANCH:full"),
    ("glab issue view 42 --comments", "gl-issue:NUMBER:full"),
    ("glab issue view 42 -c", "gl-issue:NUMBER:full"),
    # `glab ci get` has no bare entry: without a pipeline id it is the
    # discovery call, and gl-pipeline needs the id it would be discovering.
    ("glab ci get --pipeline-id 12345", "gl-pipeline:NUMBER"),
    ("glab ci get -p 12345", "gl-pipeline:NUMBER"),
])
def test_a_flag_selects_the_more_specific_op(shipped_gitlab, command, use):
    assert _uses(command) == [use], command


@pytest.mark.parametrize("command,use", [
    # `gl-job` takes a numeric id only (#1145 refuses a name before fetching),
    # and the bare form is glab own interactive picker.
    ("glab ci trace lint", "gl-job:NUMBER"),
    ("glab ci trace", "gl-job:NUMBER"),
    # `gl-mrs` has no --search, no --draft, no date range.
    ("glab mr list --search widget", "gl-mrs"),
])
def test_a_shape_the_op_answers_differently_is_still_refused(
        shipped_gitlab, command, use):
    """Deliberately broader than the op argument surface, and why that is right.

    The bar for declaring a mapping is that the op answers the same
    *question*, not that it accepts the same arguments. `gl-pipeline:N:failed`
    hands you the job id `glab ci trace lint` would have resolved, and
    `gh issue list` -> `gh-issues:per=100` already makes the identical trade on
    the GitHub side. Contrast `glab api -X POST`, where no spelling of any op
    answers the question at all — that one declares nothing.

    Listed here rather than left to the bare prefix so that narrowing one of
    these later is a visible decision instead of a silent hole.
    """
    assert _uses(command) == [use], command


def test_the_refusal_carries_the_gitlab_op_own_words(shipped_gitlab):
    verdict = supertool.guard_command("glab ci trace 224356863")
    text = supertool.guard_refusal(verdict)
    assert "gl-job" in text
    # The description is read off the registry at match time, so it cannot
    # describe a mode the op no longer has.
    assert "Why job failed" in text


# --------------------------------------------------------------------------
# The absences, which are load-bearing: the opt-out is repo-global
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command,why", [
    ("glab api -X POST projects/:id/issues",
     "supertool has no route for a GitLab API write at all"),
    ("glab api -F title=x projects/:id/issues",
     "-F makes glab POST; same absence as -X"),
    ("glab api projects/:id/members/all",
     "gl-api ships unmapped — the schema cannot exclude the write shapes"),
    ("glab issue list --per-page 50",
     "there is no gl-issues board op on the GitLab side"),
    ("glab ci status",
     "branch-scoped discovery; gl-pipeline requires a pipeline id"),
    ("glab ci get",
     "same call without the id, so nothing to point at"),
    ("glab ci list",
     "no op lists pipelines"),
    ("glab mr diff 5",
     "gl-mr renders name-status per file, never patch hunks"),
    ("glab mr merge 123 --squash",
     "no GitLab merge op exists"),
    ("glab mr create --fill",
     "no GitLab MR-create op exists"),
    ("glab release create v0.34.0",
     "tagging and releasing are the documented raw route"),
    ("glab ci retry 4242",
     "re-running a job has no op"),
])
def test_a_glab_shape_with_no_supertool_answer_stays_usable(
        shipped_gitlab, command, why):
    assert supertool.guard_command(command).state == "clean", (command, why)


@pytest.mark.parametrize("command", [
    "git log --oneline -5",
    "pytest tests/test_gitlab_replaces_1384.py -q",
    "grep -rn 'glab mr view' docs",
    "echo 'glab issue view 42'",
    "glabber --not-the-cli mr view 5",
])
def test_nothing_added_here_fires_on_an_unrelated_command(
        shipped_gitlab, command):
    assert supertool.guard_command(command).state == "clean", command


def test_exactly_these_gitlab_ops_declare_a_mapping():
    """The record of which absences were chosen, next to the ones that were not.

    Adding `gl-api` or `gl-runners` here is a real decision — see the module
    docstring for why neither is expressible today — so it should not be
    possible to make it without touching this line.
    """
    declared = {name for name, definition in _GITLAB_OPS.items()
                if "replaces" in definition}
    assert declared == {"gl-mr", "gl-mrs", "gl-issue", "gl-issue-create",
                        "gl-job", "gl-pipeline"}, sorted(declared)


# --------------------------------------------------------------------------
# End to end, through the hook the plugin actually installs
# --------------------------------------------------------------------------

def _run_hook(command: str, cwd: Path) -> Dict[str, Any]:
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": command}})
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(_ROOT)
    proc = subprocess.run(
        [sys.executable, str(_ROOT / "hooks" / "pre_bash_guard.py")],
        input=payload, capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=str(cwd), env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def test_the_hook_denies_a_raw_glab_mr_view(tmp_path):
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": _GITLAB_OPS}), encoding="utf-8")
    hook = _run_hook("glab mr view 5", tmp_path)["hookSpecificOutput"]
    assert hook["hookEventName"] == "PreToolUse"
    assert hook["permissionDecision"] == "deny"
    assert "gl-mr" in hook["permissionDecisionReason"]


def test_the_hook_lets_a_gitlab_api_write_through(tmp_path):
    """The one that would cost most if it were wrong: it ships default-on."""
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": _GITLAB_OPS}), encoding="utf-8")
    out = _run_hook("glab api -X POST projects/:id/issues", tmp_path)
    decision = out.get("hookSpecificOutput", {}).get("permissionDecision")
    assert decision != "deny", out


# --------------------------------------------------------------------------
# Through the preset, which is how a plugin user actually gets these
# --------------------------------------------------------------------------

def test_the_mapping_arrives_through_the_preset_not_only_through_ops(tmp_path):
    """Everything above injects `ops`, which would also pass if presets were unread.

    A repo enables the GitLab ops with `"presets": ["gitlab"]` and never
    copies the op bodies into its own config, so that is the only route the
    mapping actually travels in production. This repo does not enable the
    gitlab preset — `glab mr view` is allowed *here* and denied in a repo that
    does — which is exactly why the distinction has to be pinned rather than
    inferred from a local probe.

    It also pins that registration does not depend on the `glab` binary being
    installed: the preset manifest declares `requires: glab`, no CI runner has
    it, and a guard that silently stopped enforcing on those runners would be
    an absence produced by the tool read as an absence in the world.
    """
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"presets": ["gitlab"]}), encoding="utf-8")
    hook = _run_hook("glab mr view 5", tmp_path)["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny", hook
    assert "gl-mr" in hook["permissionDecisionReason"]

    out = _run_hook("glab api -X POST projects/:id/issues", tmp_path)
    assert out.get("hookSpecificOutput", {}).get(
        "permissionDecision") != "deny", out
