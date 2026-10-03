




































from __future__ import annotations

import os
import re
from collections import namedtuple









SHIPPED = {
    "supertool-no-cut.md": "deny",
}



NOT_SHIPPED = {
    "harness-tools-blocked.md":
        "its tool matcher is Edit|Write|Read|Grep|Glob|MultiEdit|"
        "NotebookEdit and the shipped hooks.json registers Bash|PowerShell "
        "only, so no shipped matcher can reach it. plugin.json already states "
        "that blocking the harness's own file tools is the operator's "
        "setting, not the plugin's; README's `Hard-block native tools` is the "
        "recipe for opting in. Re-measured for #1791, which reported this rule "
        "as shipping and blocking every file operation in other people's "
        "repositories: it does not, and its index pattern is `~.`, so "
        "shipping it would deny every Bash command there rather than only the "
        "file tools. Pinned by tests/test_harness_tools_do_not_ship_1791.py.",
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
    "channel-consumers-not-stale.md":
        "its mode is `once,remind`, and once-per-session needs state a "
        "PreToolUse hook does not carry -- the same measurement "
        "`op-defaults-that-narrow.md` records below. Shipped as a plain note "
        "it would be re-injected on every command mentioning channel.ts, "
        "including the ones that are reading the consumer rather than "
        "counting it. The claim itself is about supertool's own consumer "
        "processes and would be true in any repository; it is the delivery "
        "mechanism that does not travel, not the content.",
    "op-defaults-that-narrow.md":
        "its mode is `once,remind`, and once-per-session needs state a "
        "PreToolUse hook does not carry. Shipped as a plain note it would be "
        "re-injected on every gh-prs / gl-mrs / radar call, which is the "
        "silence-with-a-token-cost #1413 declined to add.",
    "json-set-no-append.md":
        "its mode is `once,remind`, and once-per-session needs state a "
        "PreToolUse hook does not carry -- the same measurement "
        "`op-defaults-that-narrow.md` records above. Shipped as a plain note "
        "it would be re-injected on every json-set call in every repository, "
        "most of which have no report-JSON append workflow to warn about.",
    "worktree-reuse-ahead-behind.md":
        "its mode is `once,remind`, same reason. It also names this "
        "repository's own dispatch pattern (a lane reusing a worktree/branch "
        "across a multi-issue bundle) rather than a fact true of supertool "
        "generally -- a repo with no such dispatch loop would get a note "
        "about a workflow it does not run.",
}


_RULE_DIR = (".claude", "jit-context", "tools", "00-manual")



_INDEX = "00-index.tsv"







_POSIX_CLASSES = {
    "[:space:]": " " + chr(92) + "t" + chr(92) + "n" + chr(92) + "r"
                 + chr(92) + "f" + chr(92) + "v",
}

Rule = namedtuple("Rule", "name verb regex path")

TAB = chr(9)


def rule_directory(root: str) -> str:

    return os.path.join(root, *_RULE_DIR)


def translate(pattern: str):








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

    directory = rule_directory(root)
    index = os.path.join(directory, _INDEX)
    try:
        with open(index, encoding="utf-8") as handle:
            raw = handle.read()
    except (OSError, ValueError) as exc:





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












        rules.append(Rule(name, verb, re.compile(translated, re.IGNORECASE),
                          body))

    for name in sorted(set(SHIPPED) - seen):
        skipped.append(_skip(
            name, "no row in " + _INDEX + ", so nothing carries its pattern"))
    return rules, skipped


def owned_by_project(name: str, project_dir: str) -> bool:





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
















_DISCARD_SEPARATORS = ";()<>|&"









_LT = chr(60)
_DISCARD_HEREDOC = re.compile(_LT + _LT + r"-?\s*([\x22\x27]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _strip_heredoc_bodies(prefix: str) -> str:




















    out = []
    lines = prefix.split(chr(10))
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        for m in _DISCARD_HEREDOC.finditer(line):
            delimiter = m.group(2)
            end = i
            while end < len(lines) and lines[end].strip() != delimiter:
                end += 1
            if end >= len(lines):
                continue
            line = line.replace(m.group(0), " ", 1)
            i = end + 1
        out.append(line)
    return chr(10).join(out)


def _discarded_segments(prefix: str):
















    segments = []
    start = 0
    quote = ""
    i = 0
    n = len(prefix)
    while i < n:
        ch = prefix[i]
        if quote:
            if ch == chr(92) and quote == chr(34) and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", chr(34)):
            quote = ch
            i += 1
            continue
        if ch == chr(92) and i + 1 < n:
            i += 2
            continue
        if ch in _DISCARD_SEPARATORS:
            j = i
            while j < n and prefix[j] in _DISCARD_SEPARATORS:
                j += 1
            run = prefix[i:j]
            if "<" not in run and ">" not in run:
                segments.append(prefix[start:i])
                start = j
            i = j
            continue
        i += 1
    segments.append(prefix[start:n])
    return [s.strip() for s in segments if s.strip()]


def _flatten(text: str) -> str:



















    flat = repr(text)[1:-1]
    return flat.replace(chr(96), chr(92) + chr(96))


def _discard_line(command: str, match_start: int) -> str:





    discarded = _discarded_segments(
        _strip_heredoc_bodies(command[:match_start]))
    if not discarded:
        return ""
    named = "; ".join("`" + _flatten(seg) + "`" for seg in discarded)
    return (
        chr(10) + chr(10)
        + "A `PreToolUse` refusal is all-or-nothing across the whole Bash "
        "call, so " + str(len(discarded)) + " earlier command(s) in this "
        "same call would not run either: " + named)


def match(command: str, plugin_root: str, project_dir: str):











    rules, skipped = load(plugin_root)
    for rule in rules:
        found = rule.regex.search(command)
        if not found:
            continue
        if owned_by_project(rule.name, project_dir):
            continue
        text = _body(rule)
        if text is None:



            return "note", (
                "supertool's shipped rule " + rule.name + " matched this "
                "command and its body could not be read from " + rule.path
                + ". The command proceeded - this is a statement about the "
                "rule, not about the command.")
        if rule.verb == "deny":
            text = text + _discard_line(command, found.start())
        return rule.verb, text

    if skipped and os.path.isfile(
            os.path.join(rule_directory(plugin_root), _INDEX)):











        return "note", (
            "supertool ships guard rules the `replaces` registry cannot "
            "express, and this install could not honour "
            + str(len(skipped)) + " of them: " + "; ".join(skipped)
            + ". The command proceeded - this is a statement about the "
            "rule layer, not about the command.")
    return None


def inventory(plugin_root: str, project_dir: str):

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
