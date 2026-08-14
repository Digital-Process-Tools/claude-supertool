"""#1384 step 1 -- the git ops declare which raw `git` invocation they supersede.

#1347 shipped the registry-driven guard with four mappings, all of them in
`presets/github.json`; #1393 added the GitLab side. `presets/git.json` had
none, so the three commands #1384's own measurement table calls out --
`git push origin master`, `git commit -m x`, `git -C /tmp/x status` -- were
allowed **silently**, while two `block` rules in
`.claude/jit-context/tools/00-manual/` existed to stop the first and the third
by hand. Nothing anywhere stopped `git commit`.

Four ops declare a mapping. The other nine deliberately do not, and the
absences are asserted as hard as the presences: in this schema an absent entry
is the only escape hatch, and the opt-out (`raw_command_guard: false`) is
repo-global, so one over-broad git mapping disarms GitHub's and GitLab's
mappings with it.

Two limits are pinned here rather than left to be rediscovered:

* **`git push origin <tag>` is BLOCKED, not missed.** It is discriminated by
  the *value of a positional* -- `origin master` and `origin v0.34.0` are the
  same argv shape, arity and token classes -- so no `unless_flag` can express
  it and the bare entry claims both. The route out is flag-shaped and is
  excluded on purpose: `git push --tags` and `--follow-tags` push tags on any
  forge and are left alone.
* **`git -C <path> <sub>` matches nothing here.** `argv` is a contiguous token
  prefix and `-C`'s value sits between the command word and the subcommand, so
  `{"argv": "git status"}` cannot see `git -C /tmp/x status`. That is a matcher
  gap, not a preset one, and `presets/git.json` must not paper over it with a
  `{"argv": "git -C"}` entry -- that would block `git -C W tag` and
  `git -C W push origin v1.2.3`, neither of which any op answers.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

import _guard_wire

import supertool

_ROOT = Path(__file__).resolve().parent.parent
_GIT_OPS = json.loads(
    (_ROOT / "presets" / "git.json").read_text(encoding="utf-8"))["ops"]
# git-commit has exactly one form, so its `use` is its `syntax`. Derived here
# rather than retyped, and asserted equal: `use` is a hand-written string
# sitting beside the entry and is the one part of a refusal that CAN go stale
# (test_every_use_string_names_an_op_that_exists, test_raw_command_guard_1347).
_COMMIT_USE = _GIT_OPS["git-commit"]["syntax"]
assert _GIT_OPS["git-commit"]["replaces"][0]["use"] == _COMMIT_USE


@pytest.fixture
def shipped_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The real git preset, as the effective registry.

    Fed through `ops` rather than `presets` so every assertion below is a
    statement about the file in this commit, not about preset resolution order.
    """
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": _GIT_OPS}), encoding="utf-8")
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
    # One of the three lines #1384 measured as "allowed, silent". Of the other
    # two, `git -C /tmp/x status` is not reachable from a `replaces` argv at
    # all and is asserted among the absences below, and
    # `git push origin master` names a refspec `git-push` does not take, so
    # since #1684 it is `uncovered` rather than blocked — see
    # `test_a_push_that_names_a_ref_is_uncovered`.
    ("git commit -m x", _COMMIT_USE),
    ("git status", "git-status"),
    ("git push", "git-push"),
    ("git push origin", "git-push"),
    ("git status --branch", "git-status"),
    ("git status -uall", "git-status"),
    ("git commit", _COMMIT_USE),
    ("git commit -a -m 'feat: x'", _COMMIT_USE),
    ("git worktree list", "git-worktrees"),
])
def test_a_raw_git_call_names_the_op_that_answers_it(shipped_git, command, use):
    assert _uses(command) == [use], command


@pytest.mark.parametrize("command,use", [
    # A flag selects WHICH spelling of git-push is named, the discrimination
    # #1347 built and `gh pr view --json state` is the worked example of.
    # Each carries at most the remote positionally: a refspec un-claims every
    # one of these entries on arity (#1684), whatever flag it also carries.
    ("git push --force-with-lease", "git-push:force-with-lease"),
    ("git push --force-with-lease origin", "git-push:force-with-lease"),
    ("git push --no-verify", "git-push:no-verify"),
    ("git push -u origin", "git-push:set-upstream"),
    ("git push --set-upstream origin", "git-push:set-upstream"),
])
def test_a_flag_selects_the_git_push_spelling(shipped_git, command, use):
    assert _uses(command) == [use], command


@pytest.mark.parametrize("command", [
    "git push origin v0.34.0",
    "git push origin master",
    "git push origin HEAD:master",
    "git push --force-with-lease origin master",
    "git push -u origin HEAD",
])
def test_a_push_that_names_a_ref_is_uncovered(shipped_git, command):
    """The shape this mapping used to get wrong, and how it stopped (#1684).

    `git push origin v0.34.0` was BLOCKED with `git-push` named, and `git-push`
    pushes the **current branch**: obeying that refusal published a ref the
    caller never typed while the tag stayed uncreated, and the command reported
    success. The discrimination this test used to call impossible — asking the
    repository whether a ref is a tag — is not the one needed. `master` and
    `v0.34.0` are both explicit refspecs, `git-push` takes neither, and arity
    says so from the argv alone.

    The cost is stated rather than hidden: `git push origin master` is no
    longer refused, which is a missed block on a command an op does answer
    when you happen to be on `master`. That is the direction this guard is
    allowed to be wrong in — a wrong block has no per-command escape.
    """
    verdict = supertool.guard_command(command)
    assert verdict.state == "uncovered", (command, verdict)
    assert verdict.matches == (), command
    line = " ".join(verdict.uncovered)
    assert "no op covers this form" in line, line
    # It still names the op, as an alternative rather than as a substitute.
    assert "git-push" in line, line


def test_the_tag_push_route_is_the_command_itself(shipped_git):
    """Anti-vacuity: `uncovered` has to be an ALLOW, not a quieter refusal."""
    verdict = supertool.guard_command("git push origin v0.34.0")
    assert verdict.state != "blocked", verdict
    assert supertool.op_guard("git push origin v0.34.0").count("BLOCKED") == 0


def test_the_refusal_carries_the_git_op_own_words(shipped_git):
    verdict = supertool.guard_command("git push")
    text = supertool.guard_refusal(verdict)
    assert "git-push" in text
    # Read off the registry at match time, so it cannot describe a flag the op
    # no longer has -- the failure that made #1221's hand-written rule teach a
    # wrong fact for an unknown number of sessions.
    assert "Push current branch" in text


def test_the_refusal_footer_does_not_promise_tagging_is_never_blocked(
        shipped_git):
    """Adjacent to this change, and falsified by it.

    `guard_refusal` closed every refusal with "so tagging, releasing, deleting
    a ref and re-running a workflow are not blocked". Once `presets/git.json`
    claims a bare `git push`, that sentence is printed at the bottom of the
    very refusal that just blocked a tag push -- the reader's only signal,
    saying the opposite of what happened. The enumeration is a claim about
    which *commands* no op maps, and a preset can falsify it, so the footer no
    longer makes it and points at the op that answers per command instead.
    """
    # `git push origin v0.34.0` until #1684, which stopped blocking it at all;
    # the footer claim is a property of every refusal, so the bare push is the
    # same test with one that still is one.
    verdict = supertool.guard_command("git push")
    # `guard_refusal` renders its footer for any verdict handed to it, so
    # without this the whole test passes with `git-push`'s mapping deleted:
    # nothing would be blocked, no match would render, and an absent claim
    # about tags reads exactly like a corrected one.
    assert verdict.state == "blocked", verdict
    text = supertool.guard_refusal(verdict)
    assert "git-push" in text, text
    assert "tagging" not in text, text
    # ...while still carrying the two facts it exists for.
    assert "raw_command_guard: false" in text, text
    assert "guard:" in text, text


# --------------------------------------------------------------------------
# The exclusions: shapes of a mapped command that no op answers
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command,why", [
    ("git push --tags", "no tag op; this is the flag-shaped route out"),
    ("git push origin --tags", "same, with an explicit remote"),
    ("git push --follow-tags", "same for annotated tags reachable from HEAD"),
    ("git push --delete origin old-branch", "ref delete has no op"),
    ("git push -d origin old-branch", "same, short spelling"),
    ("git push --mirror", "git-push pushes one branch"),
    ("git push --all", "same"),
    ("git push --prune", "deletes remote refs; no op"),
    ("git push --force", "git-push offers only :force-with-lease, which "
                         "REFUSES when the remote moved -- a different "
                         "operation, so naming it would be a dead end"),
    ("git push -f", "same, short spelling"),
    ("git commit --amend", "the op REFUSES amend (#962) and its own refusal "
                           "names `git commit --amend` as the route"),
    ("git commit --amend --no-edit", "same"),
    ("git commit --fixup HEAD~1", "no fixup route"),
    ("git commit --squash HEAD~1", "no squash route"),
    ("git commit --allow-empty -m x", "no empty-commit route"),
    ("git status --porcelain", "git-status renders prose; :full uncaps the "
                               "lists, it does not emit porcelain"),
    ("git status --porcelain=v2", "the value is not read, the flag is"),
    ("git status -s", "same, short format"),
    ("git status --short", "same"),
    ("git status -z", "same, NUL-separated"),
    ("git worktree list --porcelain", "git-worktrees renders a board"),
])
def test_an_excluded_shape_of_a_mapped_command_stays_usable(
        shipped_git, command, why):
    assert supertool.guard_command(command).state == "clean", (command, why)


# --------------------------------------------------------------------------
# The absences, which are load-bearing: the opt-out is repo-global
# --------------------------------------------------------------------------

# The four global-option rows that used to open this list are gone. They were
# recorded here as chosen absences -- "argv is a contiguous token prefix and
# -C's value sits inside it" -- and #1421 established that a chosen absence is
# not what they were: `git status` blocked and `git -C P status` clean is one
# subcommand with two verdicts, and the second rendered byte-identically to a
# command nothing replaces. They are now pinned as BLOCKED in
# tests/test_guard_narrower_than_the_command_1421.py.
@pytest.mark.parametrize("command,why", [
    ("git diff",
     "raw git diff spans revision ranges, machine formats and pathspecs "
     "git-diff has no spelling for, and a range carries no flag to exclude"),
    ("git diff --name-only HEAD~1", "same"),
    ("git diff --staged", "same: :staged answers it, the rest of git diff "
                          "does not narrow with it"),
    ("git checkout master",
     "git checkout <arg> is two operations sharing one name and the op "
     "refuses the pathspec one (#756) -- the same positional-value "
     "discrimination git push origin <tag> cannot express"),
    ("git checkout -b feat/x master", "no branch-create op"),
    ("git merge master",
     "git merge --abort and --continue are what git-conflicts itself prints "
     "as its hint, and --no-ff / --squash / -X have no op spelling"),
    ("git merge --abort", "same"),
    ("git log --oneline -5", "git-investigate and git-trail answer narrower "
                             "questions than git log"),
    ("git log --all -S guard_command", "same, for the pickaxe: a clustered "
                                       "-Sfoo is not the flag -S, so the "
                                       "mapping would fire on one spelling "
                                       "and not the other"),
    ("git blame _supertool.py", "git-blame needs a LINE; whole-file blame has "
                                "no replacement"),
    ("git worktree add /tmp/wt -b feat/x", "inspection only; nothing adds"),
    ("git worktree remove /tmp/wt", "same"),
    ("git tag -a v0.34.0 -m x", "no tag op"),
    ("git rebase --continue", "no rebase op"),
    ("git stash", "no stash op"),
    ("git fetch --all --prune", "no fetch op"),
    ("git pull --ff-only", "no pull op"),
    ("git add -A", "git-commit stages the paths you name; add alone has no op"),
])
def test_a_git_shape_with_no_supertool_answer_stays_usable(
        shipped_git, command, why):
    assert supertool.guard_command(command).state == "clean", (command, why)


@pytest.mark.parametrize("command", [
    "gh pr view 1321",
    "pytest tests/test_git_replaces_1384.py -q",
    "grep -rn 'git status' docs",
    "echo 'git push origin master'",
    "gitk --all",
    "python3 supertool.py 'git-status'",
])
def test_nothing_added_here_fires_on_an_unrelated_command(
        shipped_git, command):
    assert supertool.guard_command(command).state == "clean", command


def test_exactly_these_git_ops_declare_a_mapping():
    """The record of which absences were chosen, next to the ones that were not.

    Nine of thirteen ops declare nothing. Each is a decision with a reason in
    the test above, so moving one should not be possible without touching this
    line.
    """
    declared = {name for name, definition in _GIT_OPS.items()
                if "replaces" in definition}
    assert declared == {"git-status", "git-commit", "git-push",
                        "git-worktrees"}, sorted(declared)


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
    return _guard_wire.envelope(proc.stdout)


def test_the_hook_denies_the_commands_1384_measured(tmp_path):
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": _GIT_OPS}), encoding="utf-8")
    for command, op in (("git push", "git-push"),
                        ("git commit -m x", "git-commit")):
        hook = _run_hook(command, tmp_path)["hookSpecificOutput"]
        assert hook["hookEventName"] == "PreToolUse"
        assert hook["permissionDecision"] == "deny", (command, hook)
        assert op in hook["permissionDecisionReason"], command
    # The third was allowed when #1384 shipped, and that was the defect rather
    # than the design (#1421): `git -C <path> status` now reaches the same
    # entry `git status` does, through the hook and not only in the matcher.
    hook = _run_hook("git -C /tmp/x status", tmp_path)["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny", hook
    assert "git-status" in hook["permissionDecisionReason"], hook


def test_the_hook_lets_a_tag_flag_push_through(tmp_path):
    """The one that would cost most if it were wrong: it ships default-on."""
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": _GIT_OPS}), encoding="utf-8")
    for command in ("git push --tags", "git push --follow-tags",
                    "git commit --amend --no-edit"):
        out = _run_hook(command, tmp_path)
        assert out.get("hookSpecificOutput", {}).get(
            "permissionDecision") != "deny", (command, out)


# --------------------------------------------------------------------------
# Through the preset, which is how a plugin user actually gets these
# --------------------------------------------------------------------------

def test_the_mapping_arrives_through_the_preset_not_only_through_ops(tmp_path):
    """Everything above injects `ops`, which would also pass if presets were unread.

    A repo enables these with `"presets": ["git"]` and never copies the op
    bodies into its own config, so that is the only route the mapping actually
    travels in production.
    """
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"presets": ["git"]}), encoding="utf-8")
    hook = _run_hook("git push", tmp_path)["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny", hook
    assert "git-push" in hook["permissionDecisionReason"]

    # And the #1684 shape travels the same route: allowed, with the reason in
    # `additionalContext` rather than a decision.
    hook = _run_hook("git push origin v1.2.3", tmp_path)["hookSpecificOutput"]
    assert "permissionDecision" not in hook, hook
    assert "no op covers this form" in hook["additionalContext"], hook

    out = _run_hook("git push --tags", tmp_path)
    assert out.get("hookSpecificOutput", {}).get(
        "permissionDecision") != "deny", out
