"""A guard refusal whose remedy is not the thing refused (v0.35.0 round-2 audit).

`presets/github.json` claimed a bare `gh pr create` with no `unless_flag`, so
`gh pr create --dry-run` — the flag whose entire meaning is *print details
instead of creating the PR* — was refused, and the refusal named
`gh-pr-create:@FILE`, which opens a real pull request. That is the `misdirects`
class the round-1 audit defined: the named substitute performs the irreversible
action the blocked command explicitly declined to perform. #1427 closed the
identical shape for `git push` / `git commit` in `presets/git.json`; this is the
one it did not reach.

Sweeping the class turned up a second, wider instance that no per-op key should
have to carry. **A help flag is on every one of the twenty-eight mappings**, and
`--help` never performs the action either — `gh pr create --help` was blocked
and told to create a PR, `git push --help` was blocked and told to push. #1394
had already met this once and paid for it per-op, with the `*` spelling on
`gl-api` justified in part by `glab api -h` being blocked with no way past. So
the help exclusion is a property of the guard rather than data on each entry: a
mapping added tomorrow gets it without remembering to.

Would these pass if the code did nothing? No. Every `not blocked` case below is
`blocked` at 9f07c5e, which is what the audit measured.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent

# One mapping, no `unless_flag` at all — so anything this file proves about a
# help flag is proved about the guard and not about an entry that opted in.
_BARE: Dict[str, Any] = {
    "ops": {
        "gh-pr-create": {
            "cmd": "true",
            "syntax": "gh-pr-create:@FILE",
            "description": "Open a pull request from a payload file.",
            "replaces": [{"argv": "gh pr create", "use": "gh-pr-create:@FILE"}],
        },
    },
}


@pytest.fixture
def bare_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    (tmp_path / ".supertool.json").write_text(json.dumps(_BARE), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    return supertool._load_config()


class TestHelpIsNeverReplaced:
    """No op supersedes asking a program to describe itself."""

    def test_the_mapping_is_live_without_a_help_flag(self, bare_config):
        # The anti-vacuity case: every assertion below is only a statement
        # about help flags if the same command without one is refused.
        assert supertool.guard_command("gh pr create -t x").state == "blocked"

    @pytest.mark.parametrize("flag", ["--help", "-h"])
    def test_a_help_flag_un_claims_the_argv(self, bare_config, flag):
        verdict = supertool.guard_command("gh pr create " + flag)
        assert verdict.state == "clean", verdict

    def test_a_help_flag_anywhere_in_the_argv_counts(self, bare_config):
        verdict = supertool.guard_command("gh pr create -t x --help")
        assert verdict.state == "clean", verdict

    def test_a_help_flag_after_a_bare_dashdash_is_a_positional(self, bare_config):
        # POSIX ends the option list at `--`, and `_guard_options` already
        # honours that for `flag` and `unless_flag`. A file literally named
        # `--help` is not a request for help.
        verdict = supertool.guard_command("gh pr create -- --help")
        assert verdict.state == "blocked", verdict

    def test_a_short_cluster_is_not_expanded_to_h(self, bare_config):
        # Deliberately narrower than `unless_flag`, which DOES expand clusters.
        # There, expanding blocks less on a flag an op declared; here it would
        # let any cluster containing an `h` un-claim every mapping in the repo.
        verdict = supertool.guard_command("gh pr create -dh")
        assert verdict.state == "blocked", verdict


class TestShippedPresetsCarryNoMisdirect:
    """The shipped mappings, read as data — no guard run, no preset detection."""

    def test_gh_pr_create_excludes_dry_run(self):
        preset = json.loads(
            (_ROOT / "presets" / "github.json").read_text(encoding="utf-8"))
        entries = [
            item
            for op in preset.get("ops", {}).values()
            if isinstance(op, dict)
            for item in op.get("replaces", [])
            if item.get("argv") == "gh pr create"
        ]
        assert entries, "no `gh pr create` mapping in presets/github.json"
        for item in entries:
            assert "--dry-run" in item.get("unless_flag", []), item


@pytest.fixture
def shipped_presets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The real `presets/github.json` and `presets/git.json`, through the loader.

    Not the repository's own `.supertool.json`: `tests/conftest.py` disables
    config discovery for the whole suite, so a guard call from the repo root
    under pytest sees an empty registry and every "not blocked" assertion
    passes vacuously. Naming the presets in a tmp config resolves them from the
    install directory, which is this checkout.
    """
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"presets": ["github", "git"]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    return supertool._load_config()


class TestOverTheShippedPresets:
    """End to end, through the preset files the plugin installs."""

    @pytest.mark.parametrize("cmd", [
        "gh pr create -t x -b y -B master",
        # `git push origin HEAD` until #1684: a refspec un-claims the entry on
        # arity, so it would no longer be an anti-vacuity control.
        "git push origin",
        "gh issue list --state open",
    ])
    def test_a_real_invocation_is_still_refused(self, shipped_presets, cmd):
        # Anti-vacuity: if the presets were not loaded the cases below would
        # pass for the wrong reason, and these fail.
        assert supertool.guard_command(cmd).state == "blocked", cmd

    @pytest.mark.parametrize("cmd", [
        "gh pr create --dry-run -t x -b y",
        "gh pr create --help",
        "git push --help",
        "git commit --help",
        "gh issue list --help",
        "git status -h",
    ])
    def test_a_command_that_performs_nothing_is_not_refused(
            self, shipped_presets, cmd):
        verdict = supertool.guard_command(cmd)
        assert verdict.state != "blocked", verdict
