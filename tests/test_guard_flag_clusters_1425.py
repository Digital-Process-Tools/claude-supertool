"""#1425 - two holes in `unless_flag`, found by the v0.35.0 security audit.

**A refusal whose remedy does the thing the blocked command declined to do.**
`git push --dry-run` and `git push -n` were blocked, and the refusal named
`git-push`, which performs the real push. Every other defect this guard has
had costs a *missed block* - a raw read an op could have answered. This one
costs the opposite, on the one op that can destroy someone else's commits, and
it is worse on a refspec: `git push --dry-run origin feature:refs/heads/other`
previews pushing a named branch to a named ref, and a caller obeying the
refusal pushes *the current branch to its own upstream* - a ref nobody named.

`git commit --dry-run` is the same shape and was found while fixing it.

**Clustered short flags defeated every exclusion.** `_guard_excluded` compared
`token.split("=", 1)[0]` against the list, so `-sb` was not `-s` even though
`-sb` is the common spelling of the intent `-s` was excluded for. That gap
existed for every short flag on every entry, present and future, so it is
fixed in the matcher rather than enumerated per entry.

Widening an exclusion makes the guard block *less*, which is the direction it
is allowed to be wrong in (`_guard_excluded`'s own docstring): a wrong block
has no per-command escape, a missed block costs a raw call. That is the whole
argument for doing it in the mechanism, and the reason the same treatment is
NOT given to the positive `flag` matcher, where it would block more.

The cost is real and named: a single-dash token is read as a cluster of
single-letter flags, so a short flag carrying a clustered *value* whose text
happens to contain an excluded letter is excluded too - `git push -oci.skip`
now reads as carrying `-f`. Telling that from `-sb` needs per-flag arity the
guard does not have, and it errs toward allowing. A double-dash token is never
expanded, which is what keeps `--foo` from matching an excluded `-f`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent
_GIT_OPS = json.loads(
    (_ROOT / "presets" / "git.json").read_text(encoding="utf-8"))["ops"]


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
          config: Dict[str, Any]) -> None:
    (tmp_path / ".supertool.json").write_text(
        json.dumps(config), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    supertool._load_config()


@pytest.fixture
def shipped_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The real git preset as the effective registry, per #1384's fixture."""
    _load(tmp_path, monkeypatch, {"ops": _GIT_OPS})
    return tmp_path


def _probe_op(**entry: Any) -> Dict[str, Any]:
    return {"ops": {"probe-op": {
        "safety": "read-only",
        "cmd": "true",
        "description": "a probe",
        "syntax": "probe-op:X",
        "replaces": [entry],
    }}}


# --------------------------------------------------------------------------
# The dry run: a preview must not be answered by an op that pushes
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command,why", [
    ("git push --dry-run", "git-push has no dry-run route and pushes"),
    ("git push -n", "the short spelling of the same flag"),
    ("git push --dry-run origin master", "with an explicit remote and branch"),
    ("git push --dry-run origin feature:refs/heads/other",
     "the worst shape: obeying the refusal pushes the current branch to its "
     "own upstream, a ref the caller never named"),
    ("git push -n origin master", "same, short spelling"),
    ("git commit --dry-run", "git-commit commits; there is no preview route"),
    ("git commit --dry-run --short", "the value-ish companion flags too"),
])
def test_a_dry_run_is_not_answered_by_an_op_that_does_the_thing(
        shipped_git, command, why):
    assert supertool.guard_command(command).state == "clean", (command, why)


def test_commit_short_n_is_not_dragged_along_by_the_dry_run_exclusion(
        shipped_git):
    """`-n` is `--dry-run` on push and `--no-verify` on commit.

    Excluding it on commit because the long spelling was excluded there would
    un-claim a real commit, which is the mapping doing its job.
    """
    verdict = supertool.guard_command("git commit -n -m x")
    assert verdict.state == "blocked", verdict
    assert [m.use for m in verdict.matches] == [
        _GIT_OPS["git-commit"]["syntax"]], verdict


# --------------------------------------------------------------------------
# Clustered short flags reach the exclusion list
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command,why", [
    ("git status -sb", "the common spelling of the intent -s was excluded for"),
    ("git status -bs", "order in the cluster does not matter"),
    ("git status -sbz", "three of them, two excluded"),
    ("git worktree list -zv", "same list, different entry"),
    ("git push -fq", "-f is excluded; clustering it does not un-exclude it"),
    ("git push -nq", "the new -n, clustered"),
])
def test_a_clustered_short_flag_is_read_as_its_letters(
        shipped_git, command, why):
    assert supertool.guard_command(command).state == "clean", (command, why)


@pytest.mark.parametrize("command,use", [
    # Nothing that was blocked before this stops being blocked, unless it
    # carries an excluded letter. -u, -a, -m are on no exclusion list.
    ("git status -uall", "git-status"),
    ("git status -b", "git-status"),
    ("git commit -am x", "COMMIT"),
    ("git push origin master", "git-push"),
])
def test_a_cluster_of_unexcluded_letters_is_still_claimed(
        shipped_git, command, use):
    expected = _GIT_OPS["git-commit"]["syntax"] if use == "COMMIT" else use
    verdict = supertool.guard_command(command)
    assert verdict.state == "blocked", (command, verdict)
    assert [m.use for m in verdict.matches] == [expected], command


def test_a_long_flag_is_never_read_as_a_cluster(tmp_path, monkeypatch):
    """The bound on the widening, and the one that would be silent.

    `--foo` contains an `f`. If a double-dash token were expanded the way a
    single-dash one is, every long flag would un-claim any entry excluding a
    short flag whose letter it happens to spell - and an exclusion is invisible
    at the call site, so the guard would simply stop firing.
    """
    _load(tmp_path, monkeypatch, _probe_op(
        argv="probe run", unless_flag=["-f", "-d"], use="probe-op:X"))
    for command in ("probe run --foo", "probe run --dry", "probe run --fd"):
        assert supertool.guard_command(command).state == "blocked", command
    for command in ("probe run -f", "probe run -fx", "probe run -xd"):
        assert supertool.guard_command(command).state == "clean", command


def test_a_double_dash_still_ends_the_option_list_for_a_cluster(
        tmp_path, monkeypatch):
    _load(tmp_path, monkeypatch, _probe_op(
        argv="probe run", unless_flag=["-s"], use="probe-op:X"))
    assert supertool.guard_command("probe run -sb").state == "clean"
    assert supertool.guard_command("probe run -- -sb").state == "blocked"


def test_a_bare_dash_is_not_a_cluster_of_nothing(tmp_path, monkeypatch):
    """`-` is stdin, a positional. It has no letters, and must not become one."""
    _load(tmp_path, monkeypatch, _probe_op(
        argv="probe run", unless_flag=["-s"], use="probe-op:X"))
    assert supertool.guard_command("probe run -").state == "blocked"
    assert supertool.guard_command("probe run --").state == "blocked"


def test_the_positive_flag_matcher_is_not_widened(shipped_git):
    """Deliberately asymmetric: widening an exclusion blocks less, widening the
    matcher blocks more, and only one of those is the safe direction here.

    `git push -uq` carries `-u` in a cluster. It is not routed to
    `git-push:set-upstream`; the bare entry claims it, and the caller is told
    about an op that sets upstream when missing anyway.
    """
    assert [m.use for m in supertool.guard_command(
        "git push -u origin HEAD").matches] == ["git-push:set-upstream"]
    assert [m.use for m in supertool.guard_command(
        "git push -uq origin HEAD").matches] == ["git-push"]
