"""The guard rules `replaces` cannot express, shipped to every repo (#1698).

`.claude/jit-context/tools/00-manual/` holds five hand-written rules, each
there because the `replaces` registry cannot reach what it forbids. They are
read by `claude-jit-context`'s hooks out of `$CLAUDE_PROJECT_DIR`, so they
guard sessions run inside the supertool checkout and **nowhere else** — a
plugin that ships ops to other repositories shipped none of the guardrails that
make those ops safe to use there. Measured in `Digital-Process-Tools/claude-oss`,
whose whole workflow is ops: `./supertool 'git-push' 2>&1 | tail -6` ran
unblocked and the cut removed the `Repo:` and `Upstream:` lines, the two that
say which repository was written to.

The channel that does reach every user is the plugin's own `hooks/hooks.json`.
This module is read by `hooks/pre_bash_guard.py`, which that file registers on
`Bash|PowerShell` regardless of what the target repository contains.

**The rule files are not copied.** `$CLAUDE_PLUGIN_ROOT/.claude/jit-context/`
is part of every plugin install, so the markdown and the index this reads are
the same bytes the jit hooks read here. A second copy would be a second thing
to keep true, and `tests/test_jit_index_round_trips_1579.py` already exists
because a derived file drifted from the frontmatter it was derived from.

**Four of the five do not travel, and that is a recorded absence rather than an
omission** — see `NOT_SHIPPED`. A rule that encodes this repository's merge
strategy, or that names an op the caller's presets may not load, or that fires
on a tool no shipped matcher covers, arrives in someone else's repository as a
wrong block whose only escape is `raw_command_guard: false` for the whole
repository. `inventory()` enumerates both halves, and `hooks/guard-selftest.py`
prints it: a repo that does not get a rule is told which and why, rather than
being left with the silence #1698 was filed about.

**Layers are the ownership boundary.** A project carrying its own copy of a
rule file owns that rule, and the shipped one stands down for it — per rule,
not per repository. Without that, every supertool worktree would refuse the
same command twice with two different messages, which is #1376's option 3 and
was killed rather than accepted for one release.
"""
from __future__ import annotations

import os
import re
from collections import namedtuple

#: rule file -> the wire verb it is enforced with. `deny` only: the wrapper
#: has no session memory, so a `remind` would be re-injected on every matching
#: call rather than once, and a note under every call anyone writes is one
#: nobody reads — the reasoning `_may_be_replaced` already applies.
SHIPPED = {
    "supertool-no-cut.md": "deny",
}

#: rule file -> why it stays this repository's own. Each of these would be a
#: wrong block somewhere else, and a wrong block's only escape is repo-global.
NOT_SHIPPED = {
    "harness-tools-blocked.md":
        "its tool matcher is Edit|Write|Read|Grep|Glob|MultiEdit|"
        "NotebookEdit and the shipped hooks.json registers Bash|PowerShell "
        "only, so no shipped matcher can reach it. plugin.json already states "
        "that blocking the harness's own file tools is the operator's "
        "setting, not the plugin's; README's `Hard-block native tools` is the "
        "recipe for opting in.",
    "merged-is-not-ancestry.md":
        "it encodes this repository's merge strategy, not supertool's "
        "behaviour. `git branch --merged` is an ancestry test that under-"
        "reports only where pull requests are squashed; in a repo that merges "
        "with a merge commit the command is correct and blocking it would be "
        "a wrong block about somebody else's workflow.",
    "git-C-has-cwd.md":
        "its remedies name git-diff, git-diverge and git-worktrees, which "
        "exist only where the `git` preset is loaded. A refusal naming an op "
        "the caller does not have is a dead end, and the rule is already "
        "carved (#1438) around exactly which `-C` shapes the shipped registry "
        "claims in this checkout.",
    "op-defaults-that-narrow.md":
        "its mode is `once,remind`, and once-per-session needs state a "
        "PreToolUse hook does not carry. Shipped as a plain note it would be "
        "re-injected on every gh-prs / gl-mrs / radar call, which is the "
        "silence-with-a-token-cost #1413 declined to add.",
}

#: Where both layers keep their rules, relative to a tree root.
_RULE_DIR = (".claude", "jit-context", "tools", "00-manual")

#: The file the matcher actually reads. A rule with no row here is a rule that
#: never runs, which reads exactly like one that runs and never matches.
_INDEX = "00-index.tsv"

#: POSIX bracket classes this translator knows, and the whole of what it
#: knows. The index is compiled by **awk** (`claude-jit-context`'s
#: `pre-tool-hook.sh:80`), where `[[:space:]]` is a class; Python's `re` has no
#: such syntax and would read it as a set of literal characters — `[`, `:`,
#: `s`, `p`, `a`, `c`, `e` — which is a different pattern that still compiles.
#: So an unknown class is declined rather than approximated.
_POSIX_CLASSES = {
    "[:space:]": " " + chr(92) + "t" + chr(92) + "n" + chr(92) + "r"
                 + chr(92) + "f" + chr(92) + "v",
}

Rule = namedtuple("Rule", "name verb regex path")

TAB = chr(9)


def rule_directory(root: str) -> str:
    """Where *root* keeps its `00-manual` rules."""
    return os.path.join(root, *_RULE_DIR)


def translate(pattern: str):
    """*pattern* as Python `re`, or None if it cannot be honoured.

    Three states, and the third is the point: a pattern this cannot read is
    reported by `load` rather than dropped. Silently dropping it would leave a
    rule that is indexed, on disk, and enforced nowhere — the shape #1254 grew
    a write-time validator for after two `block` rules turned out to have been
    dead since the day they were written.
    """
    out = pattern
    for name, expansion in _POSIX_CLASSES.items():
        out = out.replace(name, expansion)
    if "[:" in out:
        return None
    try:
        re.compile(out)
    except re.error:
        return None
    return out


def _skip(name: str, why: str) -> str:
    return name + ": " + why


def load(root: str):
    """(rules, skipped). Never an empty rule list with nothing said about it."""
    directory = rule_directory(root)
    index = os.path.join(directory, _INDEX)
    try:
        with open(index, encoding="utf-8") as handle:
            raw = handle.read()
    except (OSError, ValueError) as exc:
        # `ValueError` and not `OSError` alone: an index that is not UTF-8
        # raises `UnicodeDecodeError`, which is a `ValueError`. A narrower
        # clause never fires on it, the exception leaves this function, and
        # the caller turns a corrupt index into "nothing matched" — the
        # guard's own defect class, inside the guard.
        return [], [_skip(
            _INDEX,
            "could not be read at " + directory + " (" + str(exc)
            + "), so no shipped rule is enforced from this install")]

    rules = []
    skipped = []
    seen = set()
    for line in raw.splitlines():
        fields = line.split(TAB)
        if len(fields) < 4:
            continue
        pattern, name, mode = fields[1], fields[2], fields[3]
        if name not in SHIPPED:
            continue
        seen.add(name)
        verb = SHIPPED[name]
        if verb == "deny" and "block" not in mode:
            skipped.append(_skip(
                name, "this module ships it as a deny and its index row says "
                      "mode " + repr(mode) + " - the two layers disagree, so "
                      "nothing is enforced until they are reconciled"))
            continue
        if not pattern.startswith("~"):
            skipped.append(_skip(
                name, "its index row is a literal match, not a regex, and "
                      "only regex rows are shipped"))
            continue
        translated = translate(pattern[1:])
        if translated is None:
            skipped.append(_skip(
                name, "its awk pattern uses a POSIX bracket class this "
                      "translator does not know, so it is declined rather "
                      "than approximated into a different pattern"))
            continue
        body = os.path.join(directory, name)
        if not os.path.isfile(body):
            skipped.append(_skip(
                name, "it has an index row and no file on disk, so there is "
                      "no body to refuse with"))
            continue
        rules.append(Rule(name, verb, re.compile(translated), body))

    for name in sorted(set(SHIPPED) - seen):
        skipped.append(_skip(
            name, "no row in " + _INDEX + ", so nothing carries its pattern"))
    return rules, skipped


def owned_by_project(name: str, project_dir: str) -> bool:
    """Does the project carry its own copy of this rule file?

    Per rule rather than per repository: a repo that wrote one rule of its own
    has not taken over the other four.
    """
    if not project_dir:
        return False
    return os.path.isfile(os.path.join(rule_directory(project_dir), name))


_TRAILER = (
    "This rule ships with supertool and is enforced by the plugin's "
    "PreToolUse hook in every repository, not only in the supertool checkout "
    "(#1698). To own it here instead, put your own copy at "
    ".claude/jit-context/tools/00-manual/" + "{name}" + " and the shipped one "
    "stands down. Four sibling rules do NOT ship, each for a stated reason: "
    "run `python3 <plugin-root>/hooks/guard-selftest.py` for the list.")


def _body(rule: Rule):
    """The rule's prose plus the trailer, or None if it could not be read."""
    try:
        with open(rule.path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, ValueError):
        return None
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for offset in range(1, len(lines)):
            if lines[offset].strip() == "---":
                lines = lines[offset + 1:]
                break
    prose = chr(10).join(lines).strip()
    return prose + chr(10) + chr(10) + _TRAILER.format(name=rule.name)


def match(command: str, plugin_root: str, project_dir: str):
    """(verb, text) for the first shipped rule that claims *command*.

    The subject is lowercased before matching, exactly as awk's
    `match(tolower(cmd), pat)` does in the hook these rules were written for:
    a pattern tuned against that matcher must not become case-sensitive by
    changing which engine reads it.
    """
    rules, skipped = load(plugin_root)
    lowered = command.lower()
    for rule in rules:
        if not rule.regex.search(lowered):
            continue
        if owned_by_project(rule.name, project_dir):
            continue
        text = _body(rule)
        if text is None:
            # A deny with no remedy in it is a wall the caller cannot read
            # their way out of. Disclose and allow, the same third state the
            # rest of this hook uses.
            return "note", (
                "supertool's shipped rule " + rule.name + " matched this "
                "command and its body could not be read from " + rule.path
                + ". The command was allowed - this is a statement about the "
                "rule, not about the command.")
        return rule.verb, text

    if skipped and os.path.isfile(
            os.path.join(rule_directory(plugin_root), _INDEX)):
        # **Three states, and this is the third.** A rule that is indexed,
        # named in `SHIPPED` and impossible to honour used to return the same
        # `None` as a command nothing claimed — a rule that looks shipped and
        # is not, which is the whole shape #1698 was filed about, reproduced
        # inside its own fix.
        #
        # Bounded to an install whose index is *present*. A plugin root with
        # no rule directory carries no layer rather than a broken one, and a
        # note there would ride on every Bash call anyone ever makes;
        # `hooks/guard-selftest.py` reports that half, which is the division
        # #1378 already settled for the hook as a whole.
        return "note", (
            "supertool ships guard rules the `replaces` registry cannot "
            "express, and this install could not honour "
            + str(len(skipped)) + " of them: " + "; ".join(skipped)
            + ". The command was allowed - this is a statement about the "
            "rule layer, not about the command.")
    return None


def inventory(plugin_root: str, project_dir: str):
    """One line per rule, both halves, so an absence is never silent."""
    rules, skipped = load(plugin_root)
    by_name = {rule.name: rule for rule in rules}
    lines = ["  shipped rules the `replaces` registry cannot express:"]
    for name in sorted(SHIPPED):
        if name not in by_name:
            state = "not loaded"
        elif owned_by_project(name, project_dir):
            state = ("stands down - this project owns its own copy at "
                     + os.path.join(*(_RULE_DIR + (name,))))
        else:
            state = "enforcing as " + SHIPPED[name]
        lines.append("    " + name + " : " + state)
    for note in skipped:
        lines.append("    skipped     : " + note)
    lines.append("  rules that stay local to the supertool checkout:")
    for name in sorted(NOT_SHIPPED):
        lines.append("    " + name + " : " + NOT_SHIPPED[name])
    return lines
