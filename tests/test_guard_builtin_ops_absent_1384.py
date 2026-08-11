"""The builtin ops are outside the raw-command guard, and that is a decision (#1384).

The census in `tests/test_replaces_census_1384.py` covers the 87 ops that come
from `presets/*.json`. It does not cover the ~41 builtins -- `read`, `grep`,
`glob`, `tree`, `wc`, `ls`, `stat`, `diff`, `map` -- and the obvious next
question is whether `cat` should name `read`, `find` should name `glob` and
`ls -R` should name `tree`. The saving would be real: an agent reading files
one at a time is this tool's founding complaint.

The answer is **no**, and it is not an oversight, so it is pinned here rather
than left to be re-proposed every quarter.

**1. The guard scores every pipeline segment, not the command.**
`_guard_segments` splits on `|`, `&&`, `||`, `;` and `&` and offers each
simple command to the registry. The forge CLIs are whole commands whose output
IS the answer; `cat`, `grep`, `head`, `wc` and `ls` are overwhelmingly
*stages* of a larger computation -- `... | grep x`, `wc -l < f`,
`cat f | python -`. A mapping on those words fires on a stage of nearly every
composed command anyone writes, and the op cannot answer the composition.

**2. There is no per-command escape hatch, and the opt-out is repo-global.**
`raw_command_guard: false` is the only way past a wrong block. So the first
time somebody needs `grep` inside a pipeline they turn the guard off for the
repository -- which disarms every `gh`, `glab` and `git` mapping with it. A
gate that makes the common path more expensive is a gate that gets switched
off, and this one takes the other 22 mappings down when it is.

**3. The payoff is a different kind from the one the guard is for.**
Every mapping that exists prevents a *wrong answer*: a hand-summed check tally
(#454), a green read off whichever workflow started last (`gh-branch`), a
squash merge missed by an ancestry test (#1229). `cat file` returns the file.
Its cost is round-trips, which is a budget problem, and a budget problem
argued at a PreToolUse deny is an argument the reader wins by disabling the
arguer.

**4. Mechanically they are not in the population at all.** `_op_registry`
reads `config["ops"]`, which `_merge_presets` builds; builtins are dispatched
from `_BUILTIN_OPS` and never appear there. They also carry no `description`
or `syntax` entry, and the refusal text is quoted from the registry -- so a
builtin mapping would deny with an empty description, which is the one thing
`guard_refusal` was written to make impossible.

What this does NOT close: `find -name` -> `glob` and `ls -R` -> `tree` are
whole commands rather than pipeline stages, so points 1 and 3 are weaker for
them than for `cat` and `grep`. Anyone reopening that should reopen it for
those two only, and points 2 and 4 still apply.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def shipped_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Every shipped preset loaded, as a plugin user gets them."""
    ops = {}
    for path in sorted((_ROOT / "presets").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        ops.update(data.get("ops") or {})
    (tmp_path / ".supertool.json").write_text(
        json.dumps({"ops": ops}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    supertool._load_config()
    return tmp_path


def test_no_builtin_op_reaches_the_guard_population(shipped_registry):
    """The mechanical half of the decision, asserted rather than assumed.

    `_guard_replacements` walks `_op_registry`, which walks `config["ops"]`.
    A builtin that appeared there would inherit no description, so its
    refusal would deny with an empty body.
    """
    replacements, notes = supertool._guard_replacements()
    assert replacements, "no mapping was loaded, so nothing was checked"
    named = {r.op for r in replacements}
    overlap = named & set(supertool._BUILTIN_OPS)
    assert not overlap, sorted(overlap)
    assert not notes, notes


@pytest.mark.parametrize("command,builtin", [
    ("cat _supertool.py", "read"),
    ("cat _supertool.py | head -40", "read"),
    ("head -40 _supertool.py", "read"),
    ("tail -40 _supertool.py", "read"),
    ("grep -rn guard_command _supertool.py", "grep"),
    ("rg guard_command", "grep"),
    ("find . -name '*.py'", "glob"),
    ("ls -R presets", "tree"),
    ("ls presets", "ls"),
    ("wc -l _supertool.py", "wc"),
    ("stat _supertool.py", "stat"),
    ("diff a.txt b.txt", "diff"),
    ("sed -n '1,20p' _supertool.py", "read"),
])
def test_a_shell_utility_a_builtin_op_answers_still_runs(
        shipped_registry, command, builtin):
    """Each of these has an op that would answer the same question better.

    None is blocked, and the docstring above is why. `builtin` is carried in
    the parameter list so the pair is visible in the test id rather than
    inferred: this is a list of deliberate misses, not a list of gaps.
    """
    assert builtin in supertool._BUILTIN_OPS, builtin
    assert supertool.guard_command(command).state == "clean", (
        command, builtin)


def test_a_pipeline_stage_is_scored_so_a_builtin_mapping_would_fire_there(
        shipped_registry):
    """Point 1, demonstrated rather than asserted in prose.

    The guard reaches the second stage of a pipeline -- so a mapping on
    `grep` would refuse `gh-pr:1424:diff | grep x` as surely as a bare
    `grep`. Proved with a command that IS mapped, sitting in stage two.
    """
    verdict = supertool.guard_command("echo hi | gh issue view 1384")
    assert verdict.state == "blocked", verdict
    assert [m.op for m in verdict.matches] == ["gh-issue"], verdict.matches
