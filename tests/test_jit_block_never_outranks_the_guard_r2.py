"""A JIT `block` on a command the shipped guard allows (v0.35.0 round-2 audit).

Two enforcement gates run on every Bash call: the plugin's `PreToolUse` guard
(`hooks/hooks.json` -> `hooks/pre-bash-guard.sh`), which tokenises argv and
honours each mapping's `unless_flag`; and these markdown rules, whose whole
matcher is one awk regex. They cover the same two commands.

The regex cannot express "except when it carries `--dry-run`" — one-true-awk has
no negation — so `git-push-has-an-op.md` and `gh-pr-view-merge-have-ops.md`
**hard-block** `git push --dry-run` and `gh pr create --dry-run|--help`, and the
body they inject names `git-push` / `gh-pr-create` as the remedy. That is worse
than the guard defect the round-1 audit classed `misdirects`: the guard only
prints a suggestion, a `block` prevents the call outright, and the named
substitute performs the irreversible action the flag exists to decline.

The repair is not a narrower `match`, which is not writable, and not deleting
the files, which would lose the prose the guard's refusal does not carry (the
tag route, `git worktree add -b` tracking, "a zero exit is not a push"). It is
`mode: block` -> `mode: remind`: enforcement moves to the one gate that can
express the exclusion, and the reference material stays.

Would these pass if the code did nothing? No — every rule below is `block` at
9f07c5e and every command below is refused outright by it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".claude" / "jit-context" / "tools" / "00-manual"
INDEX = TOOLS / "00-index.tsv"
TAB = chr(9)


def _rows():
    """(match, file, mode) for every tools row, as the hook reads them."""
    out = []
    for raw in INDEX.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        fields = raw.split(TAB)
        assert len(fields) >= 4, raw
        out.append((fields[1], fields[2], fields[3]))
    return out


def _frontmatter_mode(name):
    """The `mode:` the .md declares, which is inert but is what a reader trusts."""
    for line in (TOOLS / name).read_text(encoding="utf-8").splitlines():
        if line.startswith("mode:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("no mode: line in {0}".format(name))


# Each command performs nothing: `--dry-run` prints what would happen, `--help`
# and `-h` describe the program. The guard already lets every one through —
# `presets/git.json` since #1427, `presets/github.json` and the help flags in
# this same commit.
PERFORMS_NOTHING = [
    ("git-push-has-an-op.md", "git push --dry-run"),
    ("git-push-has-an-op.md", "git push -n"),
    ("git-push-has-an-op.md", "git push --help"),
    ("gh-pr-view-merge-have-ops.md", "gh pr create --dry-run --title x"),
    ("gh-pr-view-merge-have-ops.md", "gh pr create --help"),
    ("gh-pr-view-merge-have-ops.md", "gh pr view 1 --help"),
]


@pytest.fixture
def shipped_presets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"presets": ["github", "git"]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    return supertool._load_config()


@pytest.mark.parametrize("name,command", PERFORMS_NOTHING,
                         ids=[c[1] for c in PERFORMS_NOTHING])
def test_the_guard_lets_it_through(shipped_presets, name, command):
    # Stated first because it is what makes the next test a defect rather than
    # a preference: the two gates disagree, and the coarser one wins.
    assert supertool.guard_command(command).state != "blocked", command


@pytest.mark.parametrize("name", sorted({c[0] for c in PERFORMS_NOTHING}))
def test_no_markdown_rule_still_covers_those_commands(name):
    """#1428 retired both files rather than demoting them.

    This branch first demoted them to `mode: remind`, which was the right
    repair while they existed. #1428 landed the stronger one — the guard
    supersedes them entirely — so the assertion is now that they are gone from
    the index AND from disk, in that order: a row without a file is a rule that
    cannot run, and a file without a row is a rule that never runs, and both
    read exactly like a rule that runs and never matches.
    """
    assert name not in {f for _m, f, _mode in _rows()}, name
    assert not (TOOLS / name).exists(), name


def test_every_rule_declares_the_mode_the_index_runs_it_at():
    """A `.md` saying `block` under a `remind` row reads as enforced and is not.

    The `.md` frontmatter is inert — `00-index.tsv` is what the hook reads —
    so a disagreement between them is invisible at every surface except this
    assertion, and it is the same "reads as enforced, never fires" shape #1254
    closed for the regex column.
    """
    for _match, name, mode in _rows():
        assert _frontmatter_mode(name) == mode, name
