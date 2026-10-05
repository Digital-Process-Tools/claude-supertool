#!/usr/bin/env python





















































from __future__ import annotations

import json
import os
import re
import sys







try:
    import shipped_rules
except Exception:  
    shipped_rules = None


def _names_word(command: str, word: str) -> bool:





    return bool(re.search(r"(?<![\w.-])" + re.escape(word)
                          + r"(?:\.(?:exe|cmd|bat))?(?![\w.-])",
                          command, re.IGNORECASE))





_EXPANSION = re.compile("[$`]")


def _preset_directories(root: str):













    return [(os.path.join(root, "presets"), True),
            (os.path.join(os.path.expanduser("~"), ".config", "supertool",
                          "presets"), False)]


def _replaced_words(root: str):















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

    try:
        return sorted(name for name in os.listdir(directory)
                      if name.endswith(".json"))
    except OSError:
        return None


def _collect_words(directory: str, names, words):

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













    if _EXPANSION.search(command):
        return True
    return any(_names_word(command, word) for word in words)








WIRE_PREFIX = "supertool-guard-v1 "








































WIRE_VERBS = ("note", "deny", "clean")


def _say(verb: str, text: str = "") -> None:


































    payload = (WIRE_PREFIX + verb + "\n" + text).encode("utf-8", "replace")
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:  
        sys.stdout.write(payload.decode("utf-8", "replace"))
        return
    stream.write(payload)
    stream.flush()


def _nothing_to_say() -> None:




























    _say("clean")


def _undecided(reason: str) -> None:
    _say("note",
         "supertool raw-command guard did not run on this command: "
         + reason
         + ". The command proceeded - this is a statement about the "
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











    shipped = None
    if shipped_rules is not None:
        try:
            shipped = shipped_rules.match(
                command, root,
                os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
        except Exception as exc:  



            shipped = ("note",
                       "supertool's shipped rule layer raised "
                       + type(exc).__name__ + ": " + str(exc)
                       + ". The command proceeded - this is a statement "
                         "about the rule layer, not about the command.")




    words = _replaced_words(root)
    if (shipped is None
            and words is not None
            and not _may_be_replaced(command, words)
            and not _project_declares_replaces(os.getcwd(), root)):
        _nothing_to_say()
        return 0

    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import _supertool
    except Exception as exc:  
        _undecided(f"supertool could not be imported from {root} ({exc})")
        return 0








    if shipped is not None:
        try:
            if _supertool._load_config().get("raw_command_guard") is False:
                shipped = None
        except Exception:  
            shipped = None

    if tool_name not in (None, "Bash"):
        try:
            words = _supertool.guard_command_words()
        except Exception as exc:  
            _undecided(f"the registry could not be read ({exc})")
            return 0




        named = [word for word in words if _names_word(command, word)]
        if shipped is not None:



            _say(*shipped)
        elif not named:
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
    except Exception as exc:  
        _undecided(f"the guard raised {type(exc).__name__}: {exc}")
        return 0

    if verdict.state == "blocked":
        _say("deny", _supertool.guard_refusal(verdict))
    elif shipped is not None:





        _say(*shipped)
    elif verdict.state == "uncovered":





        _say("note", _supertool.guard_uncovered_note(verdict))
    elif verdict.state == "undecided":



        _undecided(_supertool.guard_notes_text(verdict.notes))
    else:
        _nothing_to_say()
    return 0


if __name__ == "__main__":
    sys.exit(main())
