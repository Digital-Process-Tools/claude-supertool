"""The example justifying the help block does not block (#1452).

One sentence is quoted three times -- in `_guard_help_state`'s docstring, in
`docs/contributing.md` and in the v0.36.0 changelog entry -- as the reason the
ambiguous slot is blocked rather than allowed:

> a wrong block on `git push --force -h` is legible and one flag away from a
> working `git push -h`

Measured on this tree:

    'git push --force -h'  ->  clean []

`--force` is in `git-push`'s `unless_flag`, so every `('git','push')` entry is
un-claimed before the help classifier is reached. The illustration never blocks.
The argument survives and only the example is wrong: `git push origin -o -h
main` -- the other example in the same paragraph -- does block, and is the case
the trade-off is actually about.

The pin is general rather than a string check: every command any of those texts
calls a wrong block is executed against the guard and must actually be blocked.
A prose example that quietly stops matching the code is how #1221's hand-written
rule taught a wrong fact for an unknown number of sessions.

**Second finding, same paragraph.** `_guard_help_state` returns `help` for any
`-h` not preceded by a flag. That is measured-correct for the shipped mappings,
every one of them a `git` or a `gh` invocation -- pinned below, because the
"28 entries" the docs carried was a v0.35.0 count and the registry is 29 today,
so a count states as fact what only a property can keep true. `replaces` is a
key any project may set, and for
`ls -h`, `du -h`, `sort -h` and `grep -h` the token is a real option and the
command runs -- a `clean` verdict on a command that executes, reintroduced by
configuration rather than by code. Not reachable in this tree, so the fix is a
line in the schema docs; the behaviour it warns about is pinned below so the
warning cannot describe a guard that does something else.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent
_SOURCES = ("_supertool.py", "docs/contributing.md", "CHANGELOG.md")
_CLAIM = re.compile(r"wrong block on `([^`]+)`")


@pytest.fixture
def shipped_presets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"presets": ["git", "github"]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    return supertool._load_config()


def _claimed_wrong_blocks():
    for name in _SOURCES:
        text = (_ROOT / name).read_text(encoding="utf-8")
        for command in _CLAIM.findall(text):
            yield name, command


class TestEveryCommandCalledAWrongBlockIsBlocked:

    def test_the_texts_still_make_the_claim(self):
        # Anti-vacuity: a regex that matches nothing passes the test below
        # while the documentation says whatever it likes.
        found = list(_claimed_wrong_blocks())
        assert {name for name, _ in found} == set(_SOURCES), found

    @pytest.mark.parametrize("name,command", list(_claimed_wrong_blocks()))
    def test_it_blocks(self, shipped_presets, name, command):
        verdict = supertool.guard_command(command)
        assert verdict.state == "blocked", (name, command, verdict)


class TestTheOldExampleAndItsReplacement:
    """Why the swap, stated as verdicts rather than as prose."""

    def test_the_old_example_is_un_claimed_before_help_is_reached(
            self, shipped_presets):
        # `--force` is an `unless_flag` of every `git push` entry, so the help
        # classifier never sees this argv.
        assert supertool.guard_command("git push --force -h").state == "clean"
        assert supertool.guard_command("git push --force").state == "clean"

    def test_the_replacement_is_the_case_the_trade_off_is_about(
            self, shipped_presets):
        verdict = supertool.guard_command("git push origin -o -h main")
        assert verdict.state == "blocked", verdict
        assert any("value" in note for note in verdict.notes), verdict


class TestAProjectEntryWhoseDashHIsARealOption:
    """The documented hazard, pinned as behaviour (#1452 finding 2)."""

    @staticmethod
    def _load(tmp_path, monkeypatch, argv):
        (tmp_path / ".supertool.json").write_text(json.dumps({"ops": {
            "probe-op": {"safety": "read-only", "cmd": "true",
                         "syntax": "probe-op", "description": "a probe",
                         "replaces": [{"argv": argv, "use": "probe-op"}]},
        }}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(supertool, "_CONFIG", None)
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
        monkeypatch.setattr(supertool, "_CONFIG_PATH", None)

    def test_the_guard_reads_it_as_help_and_the_command_runs(
            self, tmp_path, monkeypatch):
        self._load(tmp_path, monkeypatch, "du")
        # `du -h` is human-readable sizes, not usage. The guard cannot know
        # that, so it un-claims -- which is why the schema docs have to say so.
        assert supertool.guard_command("du -h /tmp").state == "clean"
        assert supertool.guard_command("du /tmp").state == "blocked"

    def test_every_shipped_entry_is_a_cli_whose_dash_h_is_help(
            self, tmp_path, monkeypatch):
        # What makes the terminal-`-h` rule safe here, stated as a property
        # rather than as the entry count the docs carried: 28 was measured on
        # the v0.35.0 tree and the registry is 29 today, so the number was
        # already wrong when this test was written. `git`, `gh` and `glab` all
        # print usage for `-h`; a `sort`, `ls`, `du` or `grep` entry reddens
        # this and sends its author to the rule in `docs/contributing.md`.
        (tmp_path / ".supertool.json").write_text(
            json.dumps({"presets": ["git", "github", "gitlab"]}),
            encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(supertool, "_CONFIG", None)
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
        monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
        replacements, notes = supertool._guard_replacements(
            supertool._load_config())
        assert replacements and not notes, notes
        words = {entry.argv[0] for entry in replacements}
        assert words == {"git", "gh", "glab"}, sorted(words)

    def test_the_schema_docs_warn_about_it(self):
        text = (_ROOT / "docs" / "contributing.md").read_text(encoding="utf-8")
        assert "`ls -h`" in text or "`du -h`" in text, (
            "docs/contributing.md does not warn that the guard treats "
            "`-h`/`--help` as terminal for every entry, in every registry")
