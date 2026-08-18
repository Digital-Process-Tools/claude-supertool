r"""The rule that blocks the harness file tools does not ship (#1791).

[#1791](https://github.com/Digital-Process-Tools/claude-supertool/issues/1791)
reports that a rule named `supertool-required.md` is enforced by the plugin's
PreToolUse hook in **every** repository, so a contributor who never installed
supertool has `Read`, `Edit`, `Write`, `Glob` and `Grep` all refused and is
pointed at ops they do not have. The filer states plainly that they did not
reproduce it: it was re-filed from another maintainer's report, on the strength
of the rule text they read.

**It does not reproduce.** There is no `supertool-required.md` in this tree at
any path. The rule that matches the description is `harness-tools-blocked.md`,
and it is already a recorded absence in `shipped_rules.NOT_SHIPPED`. This file
is not a fix; it is the pin that keeps the three separate facts standing
between the plugin and that failure from being undone by a one-line change that
looks like an improvement. None of the three was asserted anywhere before it:

1. `harness-tools-blocked.md` is in `NOT_SHIPPED`, and its reason names both
   halves of why - its own tool matcher, and what `hooks/hooks.json` registers.
2. `hooks/hooks.json` registers the hook on `Bash|PowerShell`, so no harness
   file tool is routed to it at all.
3. Fed such an event directly anyway, from a project directory carrying no jit
   layer, the hook allows it.

The blast radius of undoing (1) is wider than #1791 describes: that rule's
index pattern is `~.`, which matches every string, so shipping it would deny
**every Bash command** in somebody else's repository - not only the file tools.
That is what the last test here holds.

Would these pass if the code did nothing? No. Each is paired with the mutation
that reddens it, named in its own docstring, and all four were run red before
this file was committed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from _guard_wire import envelope

_ROOT = Path(__file__).resolve().parents[1]
_HOOKS = _ROOT / "hooks"
_MANUAL = _ROOT / ".claude" / "jit-context" / "tools" / "00-manual"

sys.path.insert(0, str(_HOOKS))

import shipped_rules  # noqa: E402

#: The rule #1791 describes, under the name it actually has here.
_RULE = "harness-tools-blocked.md"

#: The five tools the issue names, plus the two more the rule's own matcher
#: covers. Each with the `tool_input` shape Claude Code really sends - note
#: that none of them carries a `command` key, which is the first thing the
#: hook looks for.
_FILE_TOOLS = [
    ("Read", {"file_path": "README.md"}),
    ("Edit", {"file_path": "README.md", "old_string": "a", "new_string": "b"}),
    ("Write", {"file_path": "README.md", "content": "hello"}),
    ("Glob", {"pattern": "**/*.py"}),
    ("Grep", {"pattern": "def ", "path": "."}),
    ("MultiEdit", {"file_path": "README.md", "edits": []}),
    ("NotebookEdit", {"notebook_path": "n.ipynb", "new_source": "x"}),
]

#: The positive control for the end-to-end rows. A shipped rule really does
#: deny this, in the same fixture, through the same file - so a run where the
#: whole layer was inert cannot pass the allow rows by accident.
_PIPED = "python3 supertool.py 'gh-pr:1424:status' | tail -3"

#: A command no op replaces and no shipped rule is about. In a repository that
#: is not this one, it has to come back with no decision at all.
_NEUTRAL = "make test"


def _foreign_project(tmp_path: Path) -> Path:
    """A repository that installed the plugin and carries no jit layer.

    That is the situation #1791 is about: somebody else's project, whose
    contributors installed nothing.
    """
    project = tmp_path / "someone-elses-repo"
    (project / "src").mkdir(parents=True, exist_ok=True)
    return project


def _hook(tool_name: str, tool_input: dict, project: Path, tmp_path: Path):
    """One PreToolUse event through the file Claude Code actually runs.

    The environment is inherited and two keys overridden, because a hand-built
    `env` would need a portable `PATH` literal and there is none that holds on
    both POSIX and `windows-latest`.
    """
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(_ROOT)
    env["CLAUDE_PROJECT_DIR"] = str(project)
    proc = subprocess.run(
        [sys.executable, str(_HOOKS / "pre_bash_guard.py")],
        input=payload, capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=str(tmp_path), env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return envelope(proc.stdout)["hookSpecificOutput"]


def test_the_rule_the_issue_describes_is_not_in_this_tree() -> None:
    """#1791 names a file. Nothing here is called that, at any path.

    Reddens if a `supertool-required.md` is ever added: at that point the
    issue stops being a non-reproduction and the rest of this file stops
    describing the shipped set.
    """
    stray = sorted(str(path.relative_to(_ROOT))
                   for path in _ROOT.rglob("supertool-required.md"))
    assert stray == [], stray
    assert (_MANUAL / _RULE).is_file(), "the rule it really means moved"


def test_the_harness_tool_rule_stays_a_recorded_absence() -> None:
    """Mutation that reddens it: `SHIPPED[_RULE] = "deny"`.

    The reason is asserted too, not just the membership. A rule demoted to a
    bare name in `NOT_SHIPPED` reads as considered and is not, which is the
    one-of-five silence `test_shipped_guard_rules_1698` was written about.
    """
    assert _RULE not in shipped_rules.SHIPPED
    reason = shipped_rules.NOT_SHIPPED[_RULE]
    assert "Bash|PowerShell" in reason, reason
    assert "operator" in reason, reason


def test_no_shipped_matcher_routes_a_harness_file_tool() -> None:
    """The structural half: the plugin never sees these events.

    Mutation that reddens it: widening the `matcher` in `hooks/hooks.json` to
    include `Read`. The `Bash` assertion at the end is the positive control -
    without it this passes on a `hooks.json` whose PreToolUse block is empty,
    or absent, or misspelled.
    """
    wired = json.loads((_HOOKS / "hooks.json").read_text(encoding="utf-8"))
    matchers = [entry.get("matcher", "")
                for entry in wired["hooks"]["PreToolUse"]]
    for tool, _ in _FILE_TOOLS:
        for matcher in matchers:
            assert tool not in matcher, (tool, matcher)
    assert any("Bash" in matcher for matcher in matchers), matchers


@pytest.mark.parametrize("tool,tool_input", _FILE_TOOLS,
                         ids=[tool for tool, _ in _FILE_TOOLS])
def test_a_harness_file_tool_is_allowed_where_supertool_was_never_installed(
        tmp_path: Path, tool: str, tool_input: dict) -> None:
    """The behavioural half, fed straight to the hook past `hooks.json`.

    A contributor in somebody else's repository must get no decision and no
    disclosure - not a deny, and not a note naming ops they do not have. The
    positive control lives in the next test, in the same fixture: without it
    an allow here would also be produced by a hook that crashed on startup and
    printed an empty envelope.

    **The envelope half of this is weaker than it looks, so it does not stand
    alone.** None of these events carries a `command`, and the hook returns
    before consulting any rule when that key is absent - so the envelope
    assertion passes even with `harness-tools-blocked.md` in `SHIPPED`, and
    what really protects those tools there is the routing test above. The
    second half closes that: it asks the shipped layer directly whether any
    rule claims the strings this event carries, which is the question that
    goes the wrong way the moment a `~.` rule ships.
    """
    project = _foreign_project(tmp_path)
    hook = _hook(tool, tool_input, project, tmp_path)
    assert "permissionDecision" not in hook, hook
    assert "additionalContext" not in hook, hook

    for value in tool_input.values():
        if not isinstance(value, str):
            continue
        claimed = shipped_rules.match(value, str(_ROOT), str(project))
        assert claimed is None, (value, claimed)


def test_the_same_fixture_still_denies_what_the_shipped_layer_is_about(
        tmp_path: Path) -> None:
    """Positive control for the row above, deliberately not merged into it.

    Same project directory, same hook, same call shape. If this does not deny,
    the allow rows above are measuring a dead layer rather than a decision -
    and a dead layer and a correct one are the absence this repository keeps
    filing.
    """
    hook = _hook("Bash", {"command": _PIPED},
                 _foreign_project(tmp_path), tmp_path)
    assert hook.get("permissionDecision") == "deny", hook


def test_a_neutral_command_is_untouched_in_somebody_elses_repository(
        tmp_path: Path) -> None:
    """The catch-all half, and the one with the widest blast radius.

    `harness-tools-blocked.md` is indexed with the pattern `~.`, which matches
    every string. Mutation that reddens it: `SHIPPED[_RULE] = "deny"` - after
    which this ordinary command is denied in every repository the plugin is
    installed in, with a body that opens "This repo is supertool". So the
    membership test above is not the only thing standing here; this one says
    what shipping it would actually cost.
    """
    hook = _hook("Bash", {"command": _NEUTRAL},
                 _foreign_project(tmp_path), tmp_path)
    assert "permissionDecision" not in hook, hook
    assert "additionalContext" not in hook, hook
