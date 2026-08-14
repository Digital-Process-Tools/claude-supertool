#!/usr/bin/env python
"""PreToolUse(Bash|PowerShell) — refuse a raw command the registry replaces (#1347).

The enforcement ships **with supertool**, so every plugin user gets the same
gate rather than each repo hand-writing regexes for the conventions it wants.
The mapping itself is not here: it is `replaces` on the op's own registry
entry, and this file only asks `guard_command` and renders the answer.

Three outcomes, deliberately not two:

* a match  -> `permissionDecision: deny`, with the op's own description.
* no match, and the guard could see everything -> an envelope with no
  decision in it, which is silence to the caller.
* the guard could **not** answer -> the command runs, and the transcript says
  so in `additionalContext`. Failing closed makes an unreadable config a wall
  the caller cannot even edit their way out of; failing open *silently* is the
  exact defect #1347 opens with, where a gate that intermittently did not run
  was indistinguishable from a command that complied. Allow, and disclose.

**Every path writes an envelope, including the clean one** (#1390). Writing
nothing was the same bytes an interpreter that never started writes, so the
wrapper above could not tell "checked, nothing replaced" from "did not run" —
the third state existing in this file and not surviving the layer below it.
The clean envelope carries `hookEventName` and no decision, so what the caller
sees is unchanged; what the *wrapper* sees is the difference between an answer
and an absence.

**A shell that is not Bash gets the third state, never a decision** (#1413).
`hooks.json` used to match `Bash` alone, so wherever the PowerShell tool is
enabled — Claude then treats PowerShell as the primary shell and routes shell
commands through it — this hook never ran, and a hook that never runs is
indistinguishable at the call site from one that ran and approved. The matcher
is now `Bash|PowerShell`, and the answer for PowerShell is `undecided`: the
tokeniser below is POSIX, PowerShell quoting, escaping and its backtick
continuation are not, and a mis-tokenised command produces a false DENY whose
only escape is `raw_command_guard: false` for the whole repository. Reading
that shell properly needs a second tokeniser and a `replaces` schema that says
which shell each `argv` is written in; that is a larger change than disclosing
the gap, and it stays unbuilt on purpose.

The disclosure is **narrowed to commands that name a replaced binary**, taken
from the registry rather than from a second hardcoded list. An
`additionalContext` line under every PowerShell call anyone writes is one
nobody reads, which is the same silence with a token cost — the reasoning
`_guard_segments` already applies to a `$` in an argument.

**Most calls do not reach `_supertool` at all** (#1377). Importing it was
~142 ms of the wrapper's measured 301 ms per Bash call, and it was paid for
`ls -la /tmp` exactly as for `git push`. `_may_be_replaced` below is a
necessary condition read straight off the shipped presets, so a command that
names no replaced binary — and cannot expand into one — is answered without
the import. What that trades away is named in `_may_be_replaced`.
"""
from __future__ import annotations

import json
import os
import re
import sys


def _names_word(command: str, word: str) -> bool:
    """Is *word* a whole command word here, `.exe` and friends included?

    The same two spellings `_guard_command_word` folds together, written once
    and used by both readers below.
    """
    return bool(re.search(r"(?<![\w.-])" + re.escape(word)
                          + r"(?:\.(?:exe|cmd|bat))?(?![\w.-])",
                          command, re.IGNORECASE))


#: `$` and a backtick. A command word the shell expands is one this file
#: cannot read, so the fast path hands it to the real guard rather than
#: deciding it: `$GH_BIN pr view` names no `gh` in its own text.
_EXPANSION = re.compile("[$`]")


def _preset_directories(root: str):
    """(directory, required) for every preset tier that is not per-project.

    `_find_preset_file` resolves three: `{project}/presets`, then
    `~/.config/supertool/presets`, then the install's own. The project tier is
    the caller's, because it moves with cwd and is handled by
    `_project_declares_replaces`; the other two are scanned here.

    Missing the user tier was a live hole (#1377, found in review): a project
    whose `.supertool.json` enables a user-level preset has no literal
    `"replaces"` anywhere in its own tree, so nothing bailed, and the word the
    preset declared was not in the list either. The command took the fast path
    and ran unguarded.
    """
    return [(os.path.join(root, "presets"), True),
            (os.path.join(os.path.expanduser("~"), ".config", "supertool",
                          "presets"), False)]


def _replaced_words(root: str):
    """Every command word those presets declare, or None if unknown.

    A flat scan of `presets/*.json`, which is emphatically **not** how the
    guard itself reads the registry — `_guard_replacements` goes through
    `_op_registry` because a partial project override merges key by key and a
    hand-rolled walk turns such an op into a stub (#1356). That hazard is one
    of *under*-collecting, and this list is only ever used to decide whether
    to go and ask the real one. Over-collecting costs a wasted import;
    under-collecting is a command that runs unguarded, so anything unexpected
    returns None and the caller takes the slow path.

    `tests/test_guard_hook_cost_1377.py` pins the containment against
    `guard_command_words` preset by preset, which is what keeps two readers of
    one registry from drifting apart silently.
    """
    words = set()
    for directory, required in _preset_directories(root):
        names = _preset_names(directory)
        if names is None:
            if required:
                return None
            continue
        if _collect_words(directory, names, words) is None:
            return None
    return frozenset(words)


def _preset_names(directory: str):
    """The `.json` files in *directory*, or None if it could not be listed."""
    try:
        return sorted(name for name in os.listdir(directory)
                      if name.endswith(".json"))
    except OSError:
        return None


def _collect_words(directory: str, names, words):
    """Add every declared command word, or None on anything unexpected."""
    for name in names:
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        ops = data.get("ops")
        if ops is None:
            continue
        if not isinstance(ops, dict):
            return None
        for definition in ops.values():
            if not isinstance(definition, dict):
                continue
            raw = definition.get("replaces")
            if raw is None:
                continue
            if not isinstance(raw, list):
                return None
            for item in raw:
                if not isinstance(item, dict):
                    return None
                argv = str(item.get("argv") or "").split()
                if not argv:
                    return None
                words.add(argv[0])
    return words


def _project_declares_replaces(start: str, root: str) -> bool:
    """Does any config on the way up declare a `replaces` of its own?

    True also means "could not tell". The file changes under the user's hands
    between one Bash call and the next, so nothing about it is cached and
    nothing about it is parsed: reproducing which words a project override
    contributes is `_op_registry`'s job, and a second implementation of that
    is the drift #1356 names. Noticing the file has an opinion is cheap and
    correct; acting on the opinion stays the real guard's.

    Every `.supertool.json` on the path is read, not merely the nearest.
    `_load_config` keeps walking past one it cannot parse, so the nearest file
    is not always the effective one — and a project's own `presets/` directory
    is searched before the plugin's, so it can introduce a word of its own.

    That last check skips a `presets/` that **is** the plugin's own, compared
    by device and inode the way `session-start.sh` compares the two trees.
    Without it this repository's own checkout — where the project root and the
    plugin root are one directory — took the slow path on every command,
    which is the one host where the saving was measured.
    """
    needle = chr(34) + "replaces" + chr(34)
    shipped = os.path.join(root, "presets")

    def declares(path: str) -> bool:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return needle in fh.read()
        except OSError:
            return True

    def is_shipped(path: str) -> bool:
        try:
            return os.path.samefile(path, shipped)
        except OSError:
            return False

    directory = os.path.abspath(start)
    while True:
        config = os.path.join(directory, ".supertool.json")
        if os.path.isfile(config):
            if declares(config):
                return True
            presets = os.path.join(directory, "presets")
            try:
                names = [] if is_shipped(presets) else sorted(
                    os.listdir(presets))
            except OSError:
                names = []
            for name in names:
                if name.endswith(".json") and declares(
                        os.path.join(presets, name)):
                    return True
        parent = os.path.dirname(directory)
        if parent == directory:
            return False
        directory = parent


def _may_be_replaced(command: str, words) -> bool:
    """Could any registry entry claim this command? A necessary condition.

    Wrong in one direction only: a True that did not need to be costs the
    import this exists to skip, where a False that should have been True is a
    command running unguarded with nothing said about it. So an expansion is
    True, not analysed.

    What the False branch gives up is the `undecided` note on a command that
    names nothing replaced — `. ./script.sh` no longer says its argument was
    not read. That is the narrowing #1413 already applied to the PowerShell
    disclosure, for the reason it gives there: a line under every command
    anyone writes is one nobody reads. The DENY direction loses nothing.
    """
    if _EXPANSION.search(command):
        return True
    return any(_names_word(command, word) for word in words)


#: The first bytes of every answer this file writes, and the whole of what
#: `hooks/pre-bash-guard.sh` uses to recognise one (#1625). It replaces the
#: envelope prefix that served the same purpose for #1377: the identification
#: property is unchanged — only this file writes this, so a candidate that
#: produces it both is a Python 3 and ran — but the answer is no longer a
#: document the wrapper can forward.
WIRE_PREFIX = "supertool-guard-v1 "

#: The whole vocabulary. Three words, and the wrapper knows all three by
#: exact match; anything else is refused there rather than relayed.
WIRE_VERBS = ("silent", "note", "deny")


def _say(verb: str, text: str = "") -> None:
    """Answer the wrapper: a verb line, then the text, and nothing structural.

    **This file does not write the envelope any more** (#1625). It used to
    write the whole `hookSpecificOutput` document and the wrapper forwarded it
    verbatim, so an interpreter that printed a well-formed document and exited
    0 had authored the harness's own `permissionDecision` — well-formed JSON,
    nothing for #1613's escaper to catch, and no way for the wrapper to tell
    that document from this one. The two were the same bytes.

    Authenticating the channel cannot fix that: whatever secret would prove
    "my rung wrote this" is handed to the rung, and the rung is the forger.
    So the shape changes instead. The wrapper authors every structural byte,
    `permissionDecision` is a literal it writes itself and `deny` is the only
    value it can hold, and what crosses is a verb from `WIRE_VERBS` plus one
    run of text that is data all the way to `_json_string`.

    Not JSON on this channel on purpose. A JSON answer would need the wrapper
    to parse it in order to re-serialise it, and the wrapper is bash — reached,
    on the decline path, precisely when no interpreter is available to parse
    anything.

    **Bytes, not text, and both halves of that are Windows-only.** Graded
    reasoned, not observed (the #627 convention): nobody here has a Windows
    box. A text-mode `sys.stdout` translates a line feed into CR LF there,
    which would put a carriage return on the end of every verb and leave the
    wrapper reading `deny` plus a control character — a dialect it does not
    know, so every deny on Windows would become a disclosed allow. A text-mode
    write also encodes with the console code page, so a non-ASCII path inside
    a refusal could raise `UnicodeEncodeError` on the one platform where
    `$VIRTUAL_ENV` is most often the only rung there is. Neither was reachable
    before: `json.dumps` escapes both a line feed and a non-ASCII character
    into ASCII by construction, and that is exactly the property given up by
    not writing JSON here.
    """
    payload = (WIRE_PREFIX + verb + "\n" + text).encode("utf-8", "replace")
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:  # a stdout with no byte layer - a test double, say
        sys.stdout.write(payload.decode("utf-8", "replace"))
        return
    stream.write(payload)
    stream.flush()


def _nothing_to_say() -> None:
    """An answer that decides nothing — not the same bytes as no answer."""
    _say("silent")


def _undecided(reason: str) -> None:
    _say("note",
         "supertool raw-command guard did not run on this command: "
         + reason
         + ". The command was allowed - this is a statement about the "
           "guard, not about the command.")


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError as exc:
        _undecided(f"the hook input did not parse ({exc})")
        return 0

    tool_name = event.get("tool_name")
    command = (event.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not command.strip():
        _nothing_to_say()
        return 0

    root = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))

    # Before the import, because the import is the cost (#1377). Ordered
    # cheapest-first: the two regex screens decide most commands without ever
    # stat-ing the ancestor chain.
    words = _replaced_words(root)
    if (words is not None
            and not _may_be_replaced(command, words)
            and not _project_declares_replaces(os.getcwd(), root)):
        _nothing_to_say()
        return 0

    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import _supertool
    except Exception as exc:  # pragma: no cover - a broken install
        _undecided(f"supertool could not be imported from {root} ({exc})")
        return 0

    if tool_name not in (None, "Bash"):
        try:
            words = _supertool.guard_command_words()
        except Exception as exc:  # pragma: no cover - defensive
            _undecided(f"the registry could not be read ({exc})")
            return 0
        # Any dot other than an executable suffix ends the match, so
        # `cat gh.log` does not read as `gh`: this line only decides whether
        # to print a disclosure, and one that fires on a filename is one
        # nobody reads.
        named = [word for word in words if _names_word(command, word)]
        if not named:
            _nothing_to_say()
        else:
            _undecided(
                f"it was routed through the {tool_name} tool, whose quoting "
                f"this POSIX tokeniser does not read, and it names "
                + ", ".join(named)
                + " - binaries some op supersedes. Ask supertool "
                  "'guard:COMMAND' for the verdict")
        return 0

    try:
        verdict = _supertool.guard_command(command)
    except Exception as exc:  # pragma: no cover - defensive
        _undecided(f"the guard raised {type(exc).__name__}: {exc}")
        return 0

    if verdict.state == "blocked":
        _say("deny", _supertool.guard_refusal(verdict))
    elif verdict.state == "undecided":
        # Through supertool's bounded formatter, not a second join: this line
        # rendered 27,632 characters from 200 chained segments, on a path
        # where nothing is blocked and nothing runs that should not (#1454).
        _undecided(_supertool.guard_notes_text(verdict.notes))
    else:
        _nothing_to_say()
    return 0


if __name__ == "__main__":
    sys.exit(main())
