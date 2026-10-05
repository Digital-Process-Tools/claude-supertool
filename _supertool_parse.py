"""Op names, synonyms, safety classification and arg-parsing (#2706).

Loaded by `_load_part("_supertool_parse")` from inside `_supertool.py`, at the
exact source position this code used to occupy: a plain `exec(code,
globals())` via `_load_part`, not a real `import`. Every function defined
below therefore has `__globals__ is _supertool.__dict__` once loaded, so every
existing `monkeypatch.setattr(supertool, "_batch_prevalidation_refusal", ...)`
(the only four patch sites measured against this span, all against that one
name) keeps reaching the code it patches.

Not importable on its own. `_load_part` is the only legitimate loader: it
puts `_load_part` itself into the globals this file executes against before
running it, which is exactly the marker the guard below checks for. A bare
`import _supertool_parse` or `python3 _supertool_parse.py` gets this module's
own fresh globals(), which has no such name, and refuses with a clear
ImportError rather than failing later with a NameError on the first name this
file assumes `_supertool.py` already defined (re, os, sys, glob, Dict, List,
Optional, Tuple, Iterable, ...).

Two contiguous spans of the pre-split `_supertool.py`, concatenated here
rather than loaded with two separate `_load_part` calls: op names/synonyms/
near-miss suggestions/safety classification (what used to run first) and
arg-splitting/colon-and-extra-token refusals (what used to run several
thousand lines later, just before the old "Dispatch" section header, which
stays in `_supertool.py` because it also titles code that has not moved yet).
No module-level statement in either span evaluates a name the other span
defines in a default argument, a decorator, or a class body — every
cross-span dependency here is a plain function call deferred until the
function actually runs, by which point both spans are loaded — so merging
them into one part file, loaded once at the position the first span used to
occupy, is equivalent to the two original positions: nothing between the two
original spans (unmoved here, still in `_supertool.py`) refers to a name from
the second span at module-evaluation time, which is only possible because
that code already ran, unmodified, before the second span's original
position, at every point in `_supertool.py`'s history.
"""
from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_parse.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )

# Built-in op names — custom ops/aliases with these names are ignored
_BUILTIN_OPS = {"read", "grep", "grep_around", "glob", "ls", "tail", "head", "wc", "check", "around", "map", "diff", "stat", "around_line", "tree", "replace", "replace_dry", "edit", "replace_lines", "paste", "append", "vi", "validate", "format", "validate_staged", "format_staged", "workspace", "resolve", "diag", "hover", "rename", "payload-lint"}

# Ops the dispatcher handles but that are absent from _BUILTIN_OPS, which is a
# shadowing blocklist ("custom ops with these names are ignored") and not a
# capability list. Kept separate so the blocklist's semantics are untouched.
_DISPATCH_ONLY_OPS = {
    "between", "vim", "batch", "gc", "help", "version",
    "ops", "ops-compact", "introduction", "output-format", "registry",
    "guard", "doctor", "init", "json-set",
}

# Valid from the CLI but never reaching dispatch(): main() honours and strips
# them before the op loop. They belong in any list a caller reads.
_MAIN_LEVEL_OPS = {"cwd", "repo"}


def _valid_op_names() -> List[str]:
    """Every op name this binary accepts regardless of config, sorted.

    The unknown-op error used to carry a hand-written list of 18 names while the
    dispatcher accepted 40+ — the tool under-reporting its own capability, which
    is exactly the defect #614 is about, one layer in. Derived from the sets
    dispatch really uses so it cannot rot again. ``vi`` is dropped: it lingers in
    _BUILTIN_OPS from before the op was renamed ``vim`` and no branch handles it.
    """
    return sorted((_BUILTIN_OPS | _DISPATCH_ONLY_OPS | _MAIN_LEVEL_OPS) - {"vi"})


# Ops whose intended target is a documented fact rather than a guess (#1303).
# Two entries, and both carry their evidence:
#
# `write` — the harness tool is called `Write`, and
# `.claude/jit-context/tools/00-manual/harness-tools-blocked.md`, which fires on
# every `Write` attempt in this repo, maps `Write` to `paste`. The tool taught
# the mapping in one place and forgot it in the other, so an agent reaching for
# `write:@-` got the whole builtin roster in alphabetical order, explaining none
# of it.
# `vi` — lingers in `_BUILTIN_OPS` from before the op was renamed `vim`, and no
# branch handles it; `_valid_op_names()` drops it for that reason.
# `gh-since-tag` — deleted in #1405, folded into `gh-prs` as the `merged-since=`
# filter. The strongest receipt available: the name did not drift, the op was
# removed into this one. It is the first entry whose target is a PRESET op
# rather than a builtin, which `_near_miss_ops` already supported — its
# candidate set is `_valid_op_names()` PLUS the loaded config's ops — while the
# table's pinning test measured targets against builtins alone. That test now
# admits shipped preset ops, from `_shipped_preset_ops()`, so it keeps exactly
# the guarantee it had: a target that is renamed or dropped still fails it.
#
# Additions belong here only with that kind of receipt. Anything inferable is
# inferred below instead, where the rule can be stated and argued with.
#
# **A synonym carries a NAME, not an invocation.** `write -> paste` is a whole
# answer because `paste` alone is what the caller wanted. `gh-since-tag ->
# gh-prs` is not: the board is the right op and `merged-since=TAG,state=merged`
# is the rest of the sentence, and this table has nowhere to put it.
#
# What closes that gap is `_synonym_invocation_note`, which prints the target's
# registry `syntax` under the suggestion. Until #1524 this comment claimed the
# gap was closed by the target's *description*, "which the unknown-op message
# already prints for a preset op" — it did not, and never had: `_near_miss_ops`
# pairs a candidate with a provenance label and nothing else, so the message
# read `Did you mean: gh-prs (preset 'github')`, and bare `gh-prs` is every open
# PR on the repo — a different board from the release gate. A false claim in a
# change's own prose, used as the argument for relaxing the rule stated one
# paragraph above it; the rule was fine, the receipt was invented.
_OP_SYNONYMS = {"write": "paste", "vi": "vim", "gh-since-tag": "gh-prs"}

# The ops that bring a file into existence, in the order a refusal should offer
# them (#1334, #1372). `paste` writes a whole file and its parent dirs; `append`
# creates too, and extends when the file is already there.
#
# Hand-written, and there is no derived alternative to reach for. `replaces` —
# the registry key the raw-command guard quotes so its advice cannot go stale
# (#1376) — maps a raw *shell command* onto an op, and only preset and project
# ops carry one. Every name here is a built-in, and a built-in has no registry
# entry at all: `registry:paste` answers "'paste' is a built-in, not a preset or
# project op, so it has no registry entry". `help:paste` reads `.supertool.json`
# and returns nothing outside a project. So there is no shipped source for these
# descriptions, and a hand-written tuple is the honest answer rather than the
# lazy one.
#
# What stops it rotting is the same thing that pins `_OP_SYNONYMS`: a test
# enumerating both tables against `_valid_op_names()`, which fails the day a
# named target is renamed or dropped.
_CREATING_OPS = ("paste", "append")


def _create_instead_hint() -> str:
    """The "there is nothing here to edit" clause, naming both causes.

    Deliberately does NOT choose between them. `file not found` from a write op
    has two causes and only one wants a creating op: a caller who meant to
    create, and a caller who typo'd a path to a file that does exist. For the
    second, taking the suggestion writes a new file at the wrong path and leaves
    the real one unedited — strictly worse than the refusal, which is #1424's
    `misdirects` class. So both readings are stated and the `tried:` line above
    is what the caller decides on.
    """
    return (
        f"to create it instead: {_CREATING_OPS[0]}:::PATH:::CONTENT writes a "
        f"whole file and its parent dirs, {_CREATING_OPS[1]} creates or "
        f"extends. If the path is a typo, either would write a second file and "
        f"leave the real one unedited — check `tried:` above first."
    )

# Below four characters, one edit is not a typo signal. `lsx` is one edit from
# `ls`, `dif` from `diff`, `hea` from `head` and `heap`; suggesting on that is
# how a refusal sends its reader one round-trip FURTHER from the answer than
# silence would. Whole-segment matches (`worktrees` -> `git-worktrees`) have no
# length floor, because a shared segment is not a coincidence.
_NEAR_MISS_MIN_LEN = 4

# Three at most. A ranked list long enough to need reading is the wall of names
# this exists to replace.
_NEAR_MISS_MAX = 3


def _within_one_edit(a: str, b: str) -> bool:
    """True when `a` becomes `b` in one insert, delete, substitute or swap.

    Optimal string alignment distance <= 1, decided directly rather than by
    building a matrix. The transposition arm is not decoration: `raed` for
    `read` is two substitutions to Levenshtein and one typo to a human, and
    adjacent-key swaps are the commonest miss there is.
    """
    if a == b:
        return False
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = [i for i in range(la) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return (len(diff) == 2 and diff[1] == diff[0] + 1
                and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
    short, long_ = (a, b) if la < lb else (b, a)
    i = j = 0
    skipped = False
    while i < len(short) and j < len(long_):
        if short[i] == long_[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            j += 1
    return True


def _near_miss_ops(op: str) -> List[Tuple[str, str]]:
    """Names worth putting in front of a caller who typed `op`, and why each.

    Empty is a real answer and the common one. The bar is not "suggest
    something" — a wrong name costs the reader a round-trip they would not have
    spent on silence, and #1424's `misdirects` class is what that looks like
    when the named remedy is worse than the refusal. So every rule here is one
    that cannot fire on a coincidence, and a miss under all of them prints
    nothing rather than the least-bad candidate.

    The candidate set is what is loaded in THIS process — builtins plus the
    project's and presets' ops — because that is the set the caller may
    immediately use, and it is already in memory. Shipped-but-not-enabled
    presets are a second tier and are labelled as such: naming one without
    saying it is not loaded here would answer a question the caller did not ask.
    """
    typed = op.strip().lower()
    if not typed:
        return []
    loaded: Dict[str, str] = {name: "builtin" for name in _valid_op_names()}
    config = _load_config()
    sources = config.get("_op_sources") or {}
    for name in (config.get("ops") or {}):
        preset = (sources.get(name) or {}).get("preset")
        loaded[name] = f"preset '{preset}'" if preset else "project op"

    hits: List[Tuple[str, str]] = []
    seen = set()

    def _add(name: str, why: str) -> None:
        if name == op or name in seen:
            return
        seen.add(name)
        hits.append((name, why))

    # Case first, and alone. `Read:x` differs from `read` by something that is
    # certainly not a typo of a third op, and the distance rule cannot see that:
    # lowercased it is distance 0, which the "not a suggestion for itself" guard
    # discards, leaving `head` at distance 1 as the top candidate. Returning here
    # rather than falling through — once the name is known, a runner-up is noise.
    by_lower = {name.lower(): name for name in loaded}
    if typed in by_lower:
        _add(by_lower[typed], loaded[by_lower[typed]])
        return hits[:_NEAR_MISS_MAX]

    # A synonym is a documented mapping, not a guess, so it fires whether or not
    # the target is loaded here. It used to require `target in loaded`, which
    # made the table silently conditional on the caller's cwd: outside any
    # project `gh-since-tag` produced NO suggestion at all — the deletion #1405
    # went to some lengths not to leave as a hole, reopened by an `in` (#1524).
    target = _OP_SYNONYMS.get(typed)
    if target in loaded:
        _add(target, loaded[target])
    elif target in _shipped_preset_ops():
        _add(target, f"preset '{_shipped_preset_ops()[target]}', not loaded here")
    for name in sorted(loaded):
        if name.lower().endswith("-" + typed):
            _add(name, loaded[name])
    if len(typed) >= _NEAR_MISS_MIN_LEN:
        for name in sorted(loaded):
            if _within_one_edit(typed, name.lower()):
                _add(name, loaded[name])
    if not hits:
        for name, preset in sorted(_shipped_preset_ops().items()):
            if name in loaded:
                continue
            if name.lower().endswith("-" + typed) or (
                    len(typed) >= _NEAR_MISS_MIN_LEN
                    and _within_one_edit(typed, name.lower())):
                _add(name, f"preset '{preset}', not loaded here")
    return hits[:_NEAR_MISS_MAX]


def _cwd_retargets_note(op: str) -> str:
    """`cwd:` moves what the op ACTS on, not only where its config is read.

    The half of #1554 that is worse than the one filed. The guard blocks a raw
    `git commit` and names `git-commit`; the op is unavailable in a directory
    with no `.supertool.json` above it, and this message then offers
    `cwd:<project-path>`. Obeyed literally by someone standing in a throwaway
    repository under a scratchpad path — the #1536 agent's situation, three
    times in one run — that runs the op against a *different repository*.
    Measured: `cwd:<project>` followed by `git-status` reported the project's
    branch and working tree, not the caller's.

    So the escape hatch is kept and its scope is stated. A remedy that merely
    fails again costs a round-trip; one that succeeds somewhere else is the
    `misdirects` class proper, and this is the one line that separates them.
    """
    return (
        f"       'cwd:' moves the directory the op ACTS on as well as the one "
        f"its config is read from, so it is\n"
        f"       the fix only when the work is in that project — never a way "
        f"to run '{op}' against THIS\n"
        f"       directory. For a repository no project covers, this op has no "
        f"route to it (#1554).\n"
    )


def _synonym_invocation_note(op: str, named: List[str]) -> str:
    """The rest of the sentence, for the one candidate that is not a guess.

    `_OP_SYNONYMS` maps a NAME onto a NAME. `gh-since-tag -> gh-prs` needs more
    than that — bare `gh-prs` is every open PR on the repo, while the name the
    caller typed was the release gate, `gh-prs:merged-since=TAG,state=merged`.
    Answering with the bare name hands over a different board, which is #1424's
    `misdirects` class: a remedy that succeeds at the wrong question costs more
    than the refusal did (#1524).

    **Only the synonym route, never a distance guess.** The table above is ops
    "whose intended target is a documented fact rather than a guess", and that
    is exactly what pays for the extra line. `gh-prr` yields two candidates from
    edit distance; a syntax line each would put ~500 characters of filter
    grammar above a roster #1222 already fought to keep readable, to elaborate
    on names the tool is only guessing at.

    Empty when the target has no registry `syntax` — every built-in, so
    `write -> paste` and `vi -> vim` are unchanged and stay the whole answers
    the table argues they are.

    **The `syntax` field is printed WHOLE, so what is in it is what a reader
    pays for.** For `gh-prs` that used to be 243 characters — the filter
    grammar, then a sentence, so the invocation the caller needed began around
    character 255 of the rendered line. The obvious repair was to split on the
    `  --  ` separating the two halves, and it was refused as an inference from
    **one** field: 86 ops declared a `syntax`, exactly 1 contained that
    separator, and the distribution was min 7 / median 29 / p90 66 / max 243.

    #1590 fixed the field instead of the render. The narration left for
    `description`, which already carried it word for word; the invocation
    stayed, as the second of two ` | `-separated forms — the shape 4 other
    fields already use for an alternate invocation. So this function is
    unchanged and its output is 63 characters shorter, and the convention it
    relies on is pinned across the whole corpus by
    `tests/test_syntax_is_grammar_not_prose_1590.py`: no `syntax` carries an
    issue reference, a `  --  ` clause, or more than 200 characters.
    """
    target = _OP_SYNONYMS.get(op.strip().lower())
    if not target or target not in named:
        return ""
    syntax = _registry_syntax(target)
    if not syntax:
        return ""
    return (f"       '{op}' was an invocation; '{target}' is only a name. "
            f"Syntax: {syntax}\n")


def _batch_op_head(arg: str) -> str:
    """The op NAME `dispatch()` would resolve `arg` to, without running it.

    Mirrors the two ways `_dispatch_impl` splits an arg before it looks the
    name up: a `:::` opener splits there, everything else splits on the
    first `:` via `_split_arg`. The `:::no-exclude` suffix and the `@payload`
    routing that come after in `_dispatch_impl` never change which NAME is
    being asked for, so neither is reproduced here -- this only answers the
    one question #2122's pre-validation needs.
    """
    if arg.endswith(_NO_EXCLUDE_SUFFIX):
        arg = arg[: -len(_NO_EXCLUDE_SUFFIX)]
    if re.match(r"^([a-zA-Z_][a-zA-Z0-9_-]*):::", arg):
        parts = arg.split(":::")
    else:
        parts = _split_arg(arg)
    return parts[0] if parts else ""


def _op_is_unroutable(op: str) -> bool:
    """True only for a NAME `_dispatch_impl` cannot resolve to any handler.

    Deliberately narrower than "will succeed" (#2122's own must-not-fire
    case): a shipped preset op this config does not enable still routes
    here, because dispatch answers it with its own explanatory ERROR
    ("unavailable here, not unknown") rather than the plain unknown-op one --
    the op's EXISTENCE is a documented fact about this binary even when its
    availability here is not. A custom op whose command later fails to
    spawn, or a builtin given the wrong number of arguments, is a RUNTIME
    failure and is not knowable before anything runs; only the naming
    question is, which is the only question pre-validation is allowed to
    ask (`_unknown_op_message`'s own docstring: a typo and a custom op we
    never saw stay unknown, on purpose -- the same rule applies here).
    """
    if op in _BUILTIN_OPS - {"vi"} or op in _DISPATCH_ONLY_OPS or op in _MAIN_LEVEL_OPS:
        return False
    config = _load_config()
    # `isinstance(..., dict)`, not a truthiness check: a project config whose
    # `"ops"` is a bare string survives `_merge_presets` uncoerced on both of
    # its early-return paths, and `op in "some-string"` is a Python SUBSTRING
    # test -- `foo` would read as routable against `"xyzfooabc"` and be waved
    # through the very guard this function exists to be. A malformed config
    # must degrade this check to "not a custom op", never to "yes, routable".
    ops = config.get("ops")
    if isinstance(ops, dict) and op in ops:
        return False
    aliases = config.get("aliases")
    if isinstance(aliases, dict) and op in aliases:
        return False
    if op in _shipped_preset_ops():
        return False
    return True


def _batch_prevalidation_refusal(argv: List[str]) -> "Optional[str]":
    """Refuse a whole multi-op call before any member runs, if one member's
    op name is unroutable (#2122).

    Every positional argument is an op, so a mistyped option (`--offset`)
    becomes a batch member of its own. Before this existed, the refusal was
    already correct -- an ERROR and exit 1 for every such member -- but it
    arrived only after every EARLIER member had already run to completion,
    so a one-character slip after an unbounded `read:` bought the largest
    possible read and explained, afterwards, that it should not have.

    Mirrors the raw-command guard's own rule for a call it refuses: a
    refusal covers the whole call, never only the part that named it. A
    batch is one call; running a prefix of it that the caller did not mean
    is the worst available outcome -- work is done, output is spent, and
    the caller has to reconstruct which half happened.

    Returns `None` when every member routes (the ordinary case, and the
    only one #1234's own batch -- a real `grep` op that fails at RUNTIME on
    its argument -- must still take, unchanged).
    """
    if len(argv) < 2:
        return None
    bad = [(i, a, _batch_op_head(a)) for i, a in enumerate(argv)
           if _op_is_unroutable(_batch_op_head(a))]
    if not bad:
        return None
    msg = (
        f"ERROR: batch refused before running: {len(bad)} of {len(argv)} "
        "members are not a routable op name.\n"
        "A batch validates every member's op name before any of them runs "
        "-- a member nobody can route means the caller did not get the "
        "batch they meant, and running the ops ahead of it is the worst "
        "available outcome (#2122).\n"
    )
    for i, raw, op in bad:
        if op.startswith("-"):
            # The op this flag was meant as an argument TO is the nearest
            # earlier member that actually routes — not simply `argv[i-1]`.
            # In this issue's own headline example the member before
            # `--limit` is `900`, itself unroutable, and naming it produced
            # `900:PATH:...` one line under "unknown operation: 900": a
            # remedy contradicting the diagnosis directly above it.
            prev_op = ""
            for j in range(i - 1, -1, -1):
                candidate = _batch_op_head(argv[j])
                if not _op_is_unroutable(candidate):
                    prev_op = candidate
                    break
            hint = (f"; ops take their arguments positionally, e.g. "
                    f"{prev_op}:PATH:...\n" if prev_op else
                    "; ops take their arguments positionally, not as "
                    "flags\n")
            msg += f"  [{i}] {raw!r} looks like a flag" + hint
        else:
            msg += f"  [{i}] {raw!r} -- unknown operation: {op}\n"
            # The per-member half of the help a single-op call gets (#614's
            # `Did you mean` and its synonym note). Refusing the batch
            # earlier must not buy the caller a THINNER diagnosis than the
            # same typo would have got on its own.
            for line in _unknown_op_message(op).splitlines():
                if line.startswith(("Did you mean:", "       ")):
                    msg += f"      {line.strip()}\n"
    # The roster once, not once per member: it is the same 40+ names every
    # time, and a batch of four typos would otherwise print it four times —
    # spending on a refusal exactly the output this issue exists to save.
    for line in _unknown_op_message(bad[0][2]).splitlines():
        if line.startswith(("Valid operations:", "Plus ")):
            msg += line + "\n"
    return msg


def _unknown_op_message(op: str) -> str:
    """Answer "can I do this?" in three states, not two (#614).

    ``docs/validators.md``'s "Declining instead of guessing" one layer out: a
    checker that cannot act must distinguish *no such thing* from *not from
    here*, because emitting the first when it means the second is an absence the
    tool produced being read as an absence in the world. It cost this repo's
    heaviest user two debugging detours in one evening — ``unknown operation:
    gh-job`` read as "the installed build predates that op", and several turns
    went into hand-rolled ``gh api`` calls that worked, so nothing looked broken.

    The only absence namable honestly is a shipped preset op: it ships beside
    supertool.py, so its existence is a fact about this binary. Everything else —
    a typo, a custom op from a config we never saw — stays *unknown*, on purpose.
    Hedging every miss into "maybe you need a project root" would trade a correct
    message for a guess, which is the same bad trade in the other direction.
    """
    preset = _shipped_preset_ops().get(op)
    if preset is not None:
        # Every branch below reads `_CONFIG_PATH`, which is None both when no
        # config loaded and when none has been attempted. A dispatch always
        # loads one first (`_resolve_custom_op`); a direct call to this
        # function does not, and the difference decided which sentence a
        # caller got about a config that was sitting there and fine (#1162).
        _load_config()
        if _CONFIG_PATH:
            return (
                f"ERROR: op '{op}' is unavailable here, not unknown — it is provided "
                f"by the shipped preset '{preset}', which {_CONFIG_PATH} does not "
                f"enable.\n"
                f'       Fix: add "{preset}" to that file\'s "presets" list, or make '
                f"this call's first op 'cwd:<project-path>' pointing at a project "
                f"that already enables it.\n"
                + _cwd_retargets_note(op)
                + "       'ops' lists what is loaded here.\n"
            )
        skipped = _skipped_config()
        if skipped:
            # A config that is present and unusable is not a config that is
            # absent (#1162), and the remedies for absence are both dead here.
            # `cwd:<other-project>` retargets the working directory, so a git
            # op would report *that* repo's conflicts — #678's shape arriving
            # through the remedy line — and there is no other root to move to:
            # the broken file is in the tree the caller is standing in.
            path, why = skipped
            conflicted = "conflict markers" in why
            fix = (f"       Fix: resolve the markers in {path}, then re-run. "
                   f"The ops that would have helped are the ones it removed, "
                   f"so until then this is a raw-git repair.\n"
                   if conflicted else
                   f"       Fix: repair {path}, or make this call's first op "
                   f"'cwd:<project-path>' pointing at a project that enables "
                   f"'{preset}'.\n")
            return (
                f"ERROR: op '{op}' is unavailable here, not unknown — it is "
                f"provided by the shipped preset '{preset}'.\n"
                f"       {path} {why}, so no preset ops and no project ops are "
                f"loaded — only the built-ins.\n"
                + fix
                + ("" if conflicted else _cwd_retargets_note(op))
                + "       'ops' lists what is loaded here.\n"
            )
        return (
            f"ERROR: op '{op}' is unavailable here, not unknown — it is provided by "
            f"the shipped preset '{preset}'.\n"
            f"       No .supertool.json was found from {os.getcwd()} or any parent, "
            f"so no preset ops and no project ops are loaded — only the built-ins.\n"
            f"       Fix: run it from a project that enables the '{preset}' preset, "
            f"or make this call's first op 'cwd:<project-path>'.\n"
            + _cwd_retargets_note(op)
            + "       'ops' lists what is loaded here.\n"
        )
    msg = f"ERROR: unknown operation: {op}\n"
    # Above the roster, not instead of it (#1222). The suggestion can still be
    # wrong; the list cannot, and a reader who has to scroll past 44 names to
    # reach a candidate has been charged for both.
    near = _near_miss_ops(op)
    if near:
        msg += ("Did you mean: "
                + ", ".join(f"{name} ({why})" for name, why in near) + "\n")
        msg += _synonym_invocation_note(op, [name for name, _ in near])
    msg += f"Valid operations: {', '.join(_valid_op_names())}\n"
    loaded = _load_config().get("ops") or {}
    if loaded:
        msg += (f"Plus {len(loaded)} project/preset ops loaded from "
                f"{_CONFIG_PATH or 'config'} — run 'ops' for the full list.\n")
    return msg


# The three safety classes a listing row can carry (#1231).
#
# **Every figure in this comment was wrong, and they were the argument.** It
# read "`ops` is 47,254 bytes and `ops-compact` 9,067 against a ~7,168-byte
# SessionStart cap, so the startup listing is truncated *today*". Measured on
# 2026-08-27: `ops` 3,740, `ops-compact` 16,100, cap 10,000. The `ops` figure
# was 12.6x high — it predates #1774 making the default listing signatures-only,
# and 47,254 is roughly what `ops:full` costs now. So the premise "signatures
# cannot fit" was false, and the conclusion drawn from it — hand every session
# names alone — outlived it by fifteen releases.
#
# #1877 corrected this same pair of numbers where they appear in
# `hooks/session-start.sh` and left this copy, which is the one the reader
# reaches from the code. `tests/test_render_size_claims_1877.py` grades the
# figures in that file; this comment is prose beside a constant and nothing
# graded it. That is the whole lesson: a measurement written into a comment is
# a measurement nothing re-runs, and the second copy is the one that survives.
#
# What stands unchanged is why the class exists at all. A bare signature is
# only safely actionable for an op you may probe: `between:F:747:820` answers
# "'820' was read as the path" and names the op that does take a range, so
# listing → call → the error teaches the form. You cannot probe `gh-pr-merge`,
# which merges. Hence the class, and hence it now rides on `ops` too (#2028)
# rather than only on the roster.
#
# Declared, never inferred. `_PARALLEL_SAFE_OPS` looks like a ready-made
# read-only set and is not one — it is a dispatch-safety set, and until #1244
# it carried `format_staged`, which runs formatters over every staged file and
# writes them. Deriving "read-only" from it here would have rendered a mutating
# op probe-safe: an error in the one direction that costs something.
#
# The two agree as of #1244 and a test now holds them together, so this is no
# longer a live contradiction. The rule survives the repair anyway: a class
# read off another set's membership is only ever as right as that set's last
# edit, and this table is the declaration site.
#
# "acts" is about consequence, not about spawning: nearly every preset op runs
# a python subprocess. `*_status_since` is the case that proves it — it reads a
# feed, which sounds read-only, and writes `~/.config/<service>/last_check` on
# success. One probe to learn the signature advances the watermark, and the
# next real briefing reports an empty window it silently consumed. That is this
# repo's own defect wearing a safety class, so those three are "acts".
_SAFETY_CLASSES = ("read-only", "writes", "acts")

# Marker rendered beside a name. Read-only is unmarked, and that is a positive
# claim rather than an absence: the fallback for an op whose class is not
# declared is "acts", so a missing class renders `!` and is never the quiet one.
_SAFETY_MARKERS = {"read-only": "", "writes": "*", "acts": "!"}

# Safety class of every built-in. A fact about this binary, so it lives beside
# the sets that declare the built-ins exist rather than in a project's
# `.supertool.json` — which may be absent, stale, or another project's.
_OP_SAFETY_BUILTIN: Dict[str, str] = {
    # read-only — call blind; the error message teaches the signature
    "around": "read-only", "around_line": "read-only", "between": "read-only",
    "check": "read-only", "cwd": "read-only", "diag": "read-only",
    "diff": "read-only", "glob": "read-only", "grep": "read-only",
    "grep_around": "read-only", "head": "read-only", "help": "read-only",
    "hover": "read-only", "introduction": "read-only", "ls": "read-only",
    "map": "read-only", "ops": "read-only", "ops-compact": "read-only",
    "output-format": "read-only", "read": "read-only",
    "guard": "read-only",
    "registry": "read-only", "repo": "read-only",
    "replace_dry": "read-only", "resolve": "read-only", "stat": "read-only",
    "tail": "read-only", "tree": "read-only", "validate": "read-only",
    "validate_staged": "read-only", "version": "read-only",
    "wc": "read-only", "workspace": "read-only", "doctor": "read-only",
    "payload-lint": "read-only",
    # writes — changes files in this tree
    "append": "writes", "batch": "writes", "edit": "writes",
    "format": "writes", "format_staged": "writes", "gc": "writes",
    "init": "writes", "json-set": "writes",
    "paste": "writes", "rename": "writes", "replace": "writes",
    "replace_lines": "writes", "vim": "writes",
}


# Caller-declared read-only mode (#1787). `ops:roster` has always classed
# every op unmarked/`*`/`!` -- the only place read and write are distinguished
# at all -- but until this, the classification was unenforceable: a caller who
# IS read-only by design (a review/audit agent whose whole remit is
# annotating a diff) had no way to say so and be held to it. The concrete
# incident, `claude-oss#251`: a review agent ran `radar` (classed `!`)
# against the live watch fleet it was auditing, mid-audit of that fleet's own
# healing logic. Nothing stopped it; it is knowable only because the agent
# reported itself.
#
# An env var, not a `.supertool.json` key: the flag is a property of ONE
# CALLER's intent for one invocation, not of the repo or the tree. The same
# worktree is dispatched into by a read-only audit agent and a normal writing
# session in the same tick, sometimes the same process tree -- a
# project-level toggle would gate every caller or none, and whichever
# session wrote it last would silently win for every sibling.
# `SUPERTOOL_ALLOW_MIXED_TREE` and `SUPERTOOL_ALLOW_OUTSIDE_CWD` beside it are
# the same shape: a per-process opt a config file cannot represent.
#
# And this constrains only the well-behaved (#1787's own third question). The
# raw-command guard hooks `Bash` only, and a caller who wants to write can
# always not call the op -- `Edit`/`Write` reach disk with no op, no
# validator, no rollback (#1671), exactly as the roster's own legend already
# says. Shipped anyway: the incident this answers was not an adversary, it
# was a cooperative agent one call away from meaning to be read-only and
# having no way to say so. A guardrail against your own mistake is not made
# worthless by a determined caller's ability to route around it -- `noclobber`
# and a shell `readonly` variable are the same shape, and neither claims to
# stop an attacker.
_READ_ONLY_ENV = "SUPERTOOL_READ_ONLY"


def _read_only_declared() -> bool:
    """True when the caller has declared this whole invocation read-only.

    Same truthy spellings as `_mixed_tree_allowed()` beside it, so one
    convention answers "is this env var set" everywhere in this file.
    """
    # Literal name, not the module constant (#2734) -- see _READ_ONLY_ENV's
    # own declaration for why.
    return (os.environ.get("SUPERTOOL_READ_ONLY") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _op_is_recognized(op: str) -> bool:
    """True when `op` resolves to something dispatch actually runs -- a
    builtin, or a name this project's config declares under `"ops"` or
    `"aliases"` (#1787's own self-review). The read-only gate must not ask
    `_op_safety_class` about a name that fails this: an unrecognised name is
    not `read-only` either, so a naive gate declined it as a manufactured
    `!`-class op whose printed remedy -- "unset SUPERTOOL_READ_ONLY" -- could
    never fix a name that does not exist. `_op_gated_by_mixed_tree_write_check`
    below carries the identical carve-out for the identical reason (#1878).
    """
    if op in _valid_op_names():
        return True
    config = _load_config()
    for section in ("ops", "aliases"):
        entries = config.get(section)
        if isinstance(entries, dict) and op in entries:
            return True
    return False


def _op_is_preset_op(op: str) -> bool:
    """True when `op` is a project-declared preset/custom op -- present in
    this project's config under `"ops"` -- as opposed to a builtin.

    This is the population the generic `args`-list @payload route (#1165)
    applies to: a preset op's syntax is free-form (`gh-job:ID:grep:PATTERN`),
    so it cannot get a named-field `:::` registry entry the way `edit` or
    `git-commit` can, and had no @payload route of any kind. A builtin with
    no route has one for a specific, considered reason (it is read-only and
    reached through `_READ_OP_AT_FIELDS` instead, or its syntax legitimately
    has no ':::' fields) and must not silently gain one here.

    Deliberately not `_op_is_recognized`'s `"aliases"` branch: an alias
    expands to other ops before any of them run, so a payload aimed at the
    alias name itself would have nothing to receive it.
    """
    _ops_cfg = _load_config().get("ops")
    return isinstance(_ops_cfg, dict) and op in _ops_cfg


def _op_safety_class(op: str) -> str:
    """Safety class for ONE op name, without building the whole roster.

    Same precedence `_roster_classes()` applies across every name, extracted
    so the dispatch-time read-only gate below doesn't pay for classifying
    every loaded op to ask about the one about to run (#1787). Builtins are
    `_OP_SAFETY_BUILTIN` -- a project config cannot downgrade one, matching
    `_roster_classes()`'s own rule that class is a property of this binary.
    A preset/project op reads its declared `"safety"` off `.supertool.json`
    ("ops" then "aliases"); undeclared or a string-form cmd shorthand (no
    `"safety"` key to read) falls back to `"acts"`, the loudest class.

    Call `_op_is_recognized(op)` first: an op that fails it is not covered by
    either rule above, and this function still answers `"acts"` for it --
    correct for `_roster_classes()`, which only ever asks about names it is
    already enumerating, and wrong for a caller asking about an arbitrary
    string, which is exactly what an unrecognised name is.

    Deliberately ignores `"status"` (listing suppression): a builtin or
    preset op still dispatches when hidden from `ops`/`ops:roster`, and this
    gate has to answer about what actually runs, not about what is shown.
    """
    if op in _valid_op_names():
        return _OP_SAFETY_BUILTIN.get(op, "acts")
    config = _load_config()
    for section in ("ops", "aliases"):
        entries = config.get(section)
        if isinstance(entries, dict):
            info = entries.get(op)
            if isinstance(info, dict):
                declared = info.get("safety")
                return declared if declared in _SAFETY_CLASSES else "acts"
    return "acts"


def _read_only_decline(op: str, cls: str) -> str:
    """The refusal for a `*`/`!`-class op under SUPERTOOL_READ_ONLY=1 (#1787).

    Names the op and its class rather than a bare "denied" -- sub-question 2
    of the issue this answers, and the same third-state argument
    `_mixed_tree_decline` beside it makes: a caller reading this receipt must
    be able to tell "declined because I asked for read-only" from "failed for
    an unrelated reason" without cross-referencing anything else.

    `op` is flattened through `_flat_field(..., disclose_newline=True)`
    before it is printed -- the same treatment `_dispatch_impl`'s own header
    already gives it, and for the same reason: a colon-CLI op name can never
    carry a literal newline, but a `.supertool.json` `"ops"`/`"aliases"` KEY
    can (a JSON string permits one), and an unflattened name there could
    forge a `[result] …` line at column 0 (caught in self-review, #1787).
    """
    flat_op = _flat_field(op, disclose_newline=True)
    marker = _SAFETY_MARKERS.get(cls, "!")
    shown = marker or cls
    what = ("writes files in this tree" if cls == "writes"
            else "reaches outside this tree, or outlives the call")
    return (
        f"SKIPPED: '{flat_op}' is class `{shown}` ({cls}) -- it {what} -- and "
        f"{_READ_ONLY_ENV}=1 is set.\n"
        f"Declined rather than run: the caller asked to be held to "
        f"read-only, and acting anyway would be exactly the silent gap this "
        f"declaration exists to close (#1787).\n"
        f"Every op's class: `ops:roster`. To act anyway for this call, unset "
        f"{_READ_ONLY_ENV}.\n"
    )


# Read-only built-in ops. Two consumers, one predicate, because they ask the
# same question: `_main`'s ThreadPool gate, and the `_path_meta_bulk_drop()` in
# `dispatch`'s `finally` that keeps the repo-wide `git status` snapshot from
# outliving a write. Excludes mutating ops (replace, edit, replace_lines) and
# custom ops (could shell out to anything). `between` is included — pure file
# read.
#
# "Read-only" is the contract, not a description of whoever is currently in the
# set, and the difference cost something. Until #1244 this carried
# `format_staged`, which shells formatters over every staged file and rewrites
# them. Under SUPERTOOL_PARALLEL, `supertool 'format_staged' 'read:f.txt'`
# rendered the *pre*-format bytes — under `[complete file — no more lines]`,
# a positive completeness claim — while the post-format bytes were already on
# disk. The same call sequentially was right, so the answer depended on a
# performance switch.
#
# "Safe to run concurrently" was the tempting weaker reading and it is not
# available: parallel safety is a property of the whole set of ops in one call,
# and a writer is unsafe beside any reader of the same path. Membership is
# read-only or nothing.
#
# `_OP_SAFETY_BUILTIN` above declares the same fact for `ops` output, and
# tests/test_parallel_safe_writes_1244.py asserts the two cannot drift apart
# again — two sources for one truth is what filed this.
#
# It carried `blame` until #1285, three and a half months after b4099a5 moved
# that op to the git preset as `git-blame`: four registries needed the deletion
# and three did not get it. tests/test_registry_names_dispatch_1285.py holds
# every table here against the dispatcher now, because a name in this one with
# no dispatch branch is inert only by coincidence — the day a preset or config
# op is named `blame`, the row applies to it.
_PARALLEL_SAFE_OPS = {
    "read", "grep", "glob", "ls", "head", "tail", "wc", "stat",
    "map", "tree", "around", "around_line", "between", "diff",
    "version", "validate", "validate_staged", "workspace",
    "resolve", "diag", "hover", "help", "doctor", "payload-lint",
}


# #146: dispatch-level path containment. Each op has a known position(s) of its
# path arg(s) in `parts`. _safe_path enforces cwd containment unless
# SUPERTOOL_ALLOW_OUTSIDE_CWD=1 is set. Chokepoint coverage (render_file,
# _atomic_write) catches internal callers that bypass dispatch (alias expansion,
# test code calling op_X directly).
#
# At module scope since #1285, and not for tidiness: read by `dispatch` and by
# the drift guard that holds every op registry against the dispatcher. As a
# local literal it was unreadable from outside the function, so the one table
# whose rows say *which argument is a path* had no check on its membership.
_PATH_ARG_POSITIONS = {
    "read": (1,), "head": (1,), "tail": (1,), "wc": (1,),
    "stat": (1,), "around_line": (1,), "ls": (1,), "tree": (1,),
    "map": (1,), "validate": (1,), "format": (1,),
    "workspace": (1,), "diag": (1,),
    # grep_around:PATTERN:PATH — parts[2] and nothing else. True for
    # three-and-four-token calls: their trailing slots must parse as ints, so
    # a ':' in the pattern fails the call rather than moving the path. FALSE
    # for the two-token shape (#1842): there is no trailing int slot to fail
    # on, so `grep_around:PATTERN:PATH` moves the path exactly as `grep`
    # does, and answers rather than failing.
    "grep_around": (2,),
    # `grep` and `around` are deliberately absent: neither keeps its path
    # in a fixed slot. `_parse_grep_args`/`_parse_around_args` peel the
    # trailing ints and take `path = args[-1]`, so a ':' anywhere in the
    # PATTERN pushes the path past slot 2 and the gate saw a pattern
    # fragment instead — arbitrary read, contents and all (#1166). Read
    # from the other side it also over-contained: with a ':' in the
    # pattern, slot 2 IS a pattern fragment, so searching a local file for
    # an absolute-path string was refused while naming a file the caller
    # never asked to open. Both branches gate the path they computed.
    # `glob` is deliberately absent, and until #1366 the absence had no
    # note beside it — the only unexplained one in a table where `grep`,
    # `around` and `between` each say why. It read as a decision and was
    # an oversight: `glob` reached no gate at all, so `glob:/tmp/x/*.txt`
    # listed what `read:/tmp/x/f.txt` refused. Its slot 1 is a PATTERN,
    # not a path — magic components, `**`, brace groups — so this table
    # cannot gate it, and neither can #1287's `_resolve_custom_op`
    # declaration gate, which only ever sees preset ops. `op_glob` gates
    # the reach it computes and the matches it got, at the points it has
    # them; see `_glob_reach`.
    # hover:SYMBOL:FILE, rename:OLD:NEW:FILE, resolve:SYMBOL[:FROM_FILE]
    "hover": (2,), "rename": (3,), "resolve": (2,),
    # diff:PATH1:PATH2
    "diff": (1, 2),
    # `between` is deliberately absent: neither of its readings keeps its
    # path in a fixed slot. Symbol mode takes parts[-1] (a ':' in the
    # symbol pushes the path past slot 2, and nothing gated parts[3] —
    # #1163), `re:` mode joins parts[4:] (a ':' in the path leaves only
    # the first fragment gated). And slot 2 is the START *regex* in the
    # `re:` reading, so gating it refused a legitimate local slice while
    # naming a file the caller never asked for (#1164). Both branches
    # gate the path they computed, at the point they computed it.
    # check:PRESET:PATH — runs a custom op, path forwarded as {file}.
    "check": (2,),
    # mutating ops (also covered by _atomic_write chokepoint):
    "edit": (3,), "replace": (3,), "replace_dry": (3,),
    "replace_lines": (1,), "paste": (1,), "append": (1,), "vim": (1,),
}


def _is_parallel_safe(arg: str) -> bool:
    """Return True if the op name is in the read-only safe set.

    Detects op name from `op:...` or `op:::...` prefix. Anything else —
    custom ops, mutating ops, malformed args — is treated as unsafe.

    Callers use it for two different-looking questions — may these ops share a
    thread pool, and may the status snapshot survive this op — which are the
    same question about whether the op writes.
    """
    m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_-]*)(:::|:|$)", arg)
    if not m:
        return False
    return m.group(1) in _PARALLEL_SAFE_OPS


_DRIVE_LETTER = re.compile(r"^@?[A-Za-z]\Z")  # \Z, not $ — #1188
_URL_SCHEMES = ("http", "https", "ftp", "ftps", "ssh", "git", "file", "ws", "wss")
# Numeric port, optionally followed by '/path' or '?query' or end — used to
# absorb 'https://host' + ':8080/path' fragments that arose from `:`-splitting.
_URL_PORT = re.compile(r"^\d+(?:[/?#].*)?\Z")  # \Z, not $ — #1188


# NUL-bracketed marker for the two-pass `\\` protection in _decode_escapes.
# NUL is extremely unlikely in CLI args, and pass 3 replaces it with a literal
# backslash so it never reaches output.
_DECODE_ESCAPES_SENTINEL = "\x00BS\x00"


def _decode_escapes(s: str) -> str:
    r"""Decode shell-style escape sequences in mutating-op arguments.

    Used by `replace`, `replace_dry`, `edit`, `replace_lines`, `vi` so callers
    can pass multi-line OLD/NEW/CONTENT via CLI without literal `\n` polluting
    file contents.

    Decoded sequences:
      `\n` `\t` `\r`           → newline / tab / CR
      `\\`                     → literal `\`
      `\<punctuation>`         → drop `\` (shell-style defensive escape:
                                  `\)`, `\(`, `\$`, `\"`, `\'`, `\ `, `\!`...
                                  Kevin's defensive `\)` becomes `)`).
      `\<letter|digit>`        → preserved as-is. PHP namespace `\Foo`,
                                  regex char classes `\d`/`\w`/`\s`, and
                                  `:s` backrefs `\1`..`\9` all survive.

    A two-pass sentinel keeps `\\` (literal backslash) intact across the
    other substitutions.
    """
    if "\\" not in s:
        return s
    out = s.replace("\\\\", _DECODE_ESCAPES_SENTINEL)
    out = out.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
    # `\xHH` (two hex digits) → single char. Useful inside TEXT to embed
    # bytes like `\x27` (single quote) without bash single-quote nesting.
    out = re.sub(
        r"\\x([0-9A-Fa-f]{2})",
        lambda m: chr(int(m.group(1), 16)),
        out,
    )
    # Drop `\` before punctuation/symbols (defensive shell escapes).
    out = re.sub(r"\\([^A-Za-z0-9])", r"\1", out)
    out = out.replace(_DECODE_ESCAPES_SENTINEL, "\\")
    return out


def _split_arg(arg: str) -> List[str]:
    """Split 'op:arg1:arg2:arg3' by ':' but reassemble drive letters and URLs.

    Splits on every ':' (no limit) then merges:
      - Single-letter pieces followed by slash/backslash → Windows drive letter
      - URL-scheme pieces (http, https, ftp, ssh, git, file, ws...) followed
        by '//...' → URL. Scheme detection looks at the LAST '|'-separated
        segment of the piece, so URLs work when embedded as one of several
        '|'-separated args (e.g. publish ops with TITLE|FILE|URL|TAGS|COVER).
      - URLs with a host already absorbed, followed by a numeric port
        (optionally with a path) → URL with port.

    Examples:
        'read:foo.py'                          → ['read', 'foo.py']
        'read:C:\\Users\\file.py'              → ['read', 'C:\\Users\\file.py']
        'grep:pat:C:/src:20'                   → ['grep', 'pat', 'C:/src', '20']
        'op:T|F|https://x.com/a|tag'           → ['op', 'T|F|https://x.com/a|tag']
        'op:T|F|https://a.com|t|https://b'     → ['op', 'T|F|https://a.com|t|https://b']
        'op:T|https://example.com:8080/path|x' → ['op', 'T|https://example.com:8080/path|x']
    """
    raw = arg.split(":")  # Full split — drive letters and URLs rejoined below
    tokens: List[str] = []
    i = 0
    while i < len(raw):
        piece = raw[i]
        # Greedily absorb next pieces if current looks like a drive letter or URL scheme
        while i + 1 < len(raw):
            next_piece = raw[i + 1]
            last_seg = piece.rsplit("|", 1)[-1]
            # Drive-letter detection also splits on ',' so a comma-joined
            # multi-path keeps reassembling each member's drive letter
            # (e.g. 'C:\a.php,C' + '\b.php' → 'C:\a.php,C:\b.php' for the
            # validate list form). The drive letter is the last ','/'|'-segment.
            drive_seg = last_seg.rsplit(",", 1)[-1]
            # #1972 (CI, windows-latest): a `KEY=` prefix (#1690's own
            # `path=`/`file=` keyword) puts the drive letter after the '='
            # rather than at the start of the piece -- 'path=C' fails the
            # single-letter check below whole, so the reassembly this
            # function already promises for a bare drive letter (and that
            # .supertool.json's own `validate` description documents: "Drive
            # letters are reassembled") never fired for it. One shared
            # tokenizer, extended once, rather than a second reassembly
            # pass downstream in `_extract_path_kw` re-deriving what this
            # function already knows how to do.
            _kw_match = re.match(r"^[A-Za-z_][A-Za-z0-9_]*=(.*)\Z", drive_seg)
            if _kw_match:
                drive_seg = _kw_match.group(1)
            is_drive = (
                _DRIVE_LETTER.match(drive_seg) is not None
                and next_piece
                and next_piece[0] in ("/", "\\")
            )
            is_url = (
                last_seg.lower() in _URL_SCHEMES
                and next_piece.startswith("//")
            )
            # Port absorption: piece already has '://' (URL host absorbed last
            # iteration), and next piece is purely numeric or numeric+path.
            # 'https://example.com' + '8080/path' → 'https://example.com:8080/path'.
            is_url_port = (
                "://" in last_seg
                and bool(_URL_PORT.match(next_piece))
            )
            if not (is_drive or is_url or is_url_port):
                break
            piece = f"{piece}:{next_piece}"
            i += 1
        tokens.append(piece)
        i += 1
    return tokens


# The last colon slot each op actually reads. A non-empty token past it is
# refused rather than dropped (#1582, #1345).
#
# Measured on 2c8eaf9, twelve read ops probed with one junk token appended:
# twelve dropped it and answered. `read:CLAUDE.md:::lines=66-76` returned all
# 102 lines — 13549 bytes where ~1500 were asked for — because the unknown key
# landed past every slot the branch indexes. That is the worst shape the class
# has: **an ignored narrowing returns MORE than was asked for, and more always
# reads as a superset of correct**, so nothing in the render distinguishes it
# from a call that did what was typed.
#
# `read` is deliberately absent and handled by `_extra_colon_tokens` directly:
# its tail is variadic by design (`read:PATH:::grep=PATTERN` is the documented
# spelling, and the `:::` yields two empty parts before it), so a slot number
# cannot express what it accepts.
#
# `grep`, `around` and `between` are absent for the reason they are absent from
# `_PATH_ARG_POSITIONS`: none of them keeps its arguments in fixed slots — a
# ':' anywhere in the PATTERN moves everything right. grep's own trailing slack
# is closed at its parser instead (`_grep_peeled_extras`); `around` and
# `between` already fail loudly, absorbing the extra token into the PATH slot
# and reporting `path not found`.
#
# Only NON-EMPTY tokens count. `ls:C:` tokenizes to ['ls', 'C', ''] on the
# Windows drive-letter path that _split_arg declines to rejoin (no slash
# follows), and refusing it would be a platform-only behaviour change bought
# for nothing.
_MAX_COLON_SLOTS: Dict[str, int] = {
    "head": 2, "tail": 2, "tree": 2, "diff": 2, "glob": 2,
    "wc": 1, "ls": 1, "stat": 1, "map": 1,
    # #2238: dispatched via `body = op_payload_lint(parts[1] if len(parts) > 1
    # else "")` -- only parts[1] (the @file/@- reference) is ever read, same
    # single-slot shape as wc/ls/stat/map. Missing from this table meant
    # `payload-lint:@somefile:extra:more` silently dropped `extra`/`more`
    # instead of refusing, exactly what #1582 named for every other op.
    "payload-lint": 1,
    "around_line": 3,
    "grep_around": 4,
}


def _op_syntax(op: str) -> str:
    """The op's own syntax line from the registry, or "" when it has none.

    Quoted rather than restated, so a refusal cannot drift from the op the way
    a hand-copied grammar does. Returns "" rather than raising: a refusal that
    cannot find the registry is still a refusal, and one line shorter.
    """
    try:
        config = _load_config()
    except Exception:
        return ""
    for section in ("builtin-ops", "ops", "aliases"):
        entry = config.get(section, {})
        if isinstance(entry, dict) and isinstance(entry.get(op), dict):
            return str(entry[op].get("syntax", "") or "")
    return ""


def _extra_colon_tokens(op: str, parts: List[str]) -> List[str]:
    """Non-empty colon tokens this op will peel and never read (#1582).

    `read` is scanned rather than indexed: the filter can land in any trailing
    slot (parts[4] for `read:PATH:::grep=`, parts[3] when a range consumed only
    one), so the rule is "one grep=, empties, nothing else" rather than a slot
    count. A SECOND `grep=` is an extra too — the scan takes the first and
    breaks, so the second was silently the one that did not apply.
    """
    if op == "read":
        extra: List[str] = []
        # parts[3] is a slot the read branch reads for itself — `full`/`raw`, a
        # LIMIT, or the FIRST `grep=` when a range or a bare offset consumed
        # only one slot — so the scan starts at parts[4]. It still has to know
        # whether parts[3] took the grep=: without that, a second `grep=` one
        # slot along was read as the first and allowed through, which is the
        # exact drop this function exists to close.
        seen_grep = len(parts) > 3 and parts[3].startswith("grep=")
        for tok in parts[4:]:
            if not tok:
                continue
            if tok.startswith("grep=") and not seen_grep:
                seen_grep = True
                continue
            extra.append(tok)
        return extra
    last = _MAX_COLON_SLOTS.get(op)
    if last is None:
        return []
    return [tok for tok in parts[last + 1:] if tok]


def _extra_colon_tokens_refusal(op: str, extra: List[str],
                                remedy: str = "") -> str:
    """Refuse the tokens rather than run the call they were removed from.

    A refusal, not a note beside the answer: the answer is to a question the
    caller did not ask, and printing it under a warning is how #1582 was read
    as "conditional, not unimplemented" by the agent that hit it twice.
    """
    nl = chr(10)
    them = "them" if len(extra) > 1 else "it"
    lines = [
        f"ERROR: {op}: {len(extra)} argument"
        f"{'s' if len(extra) > 1 else ''} past the last slot {op} reads — "
        f"{', '.join(repr(t) for t in extra)}.",
        f"  Dropped rather than refused before #1582, so the call ran without "
        f"{them}: a narrowing that was ignored returns MORE than was asked "
        f"for, and nothing in the result says so.",
    ]
    syntax = _op_syntax(op)
    if syntax:
        lines.append(f"  Syntax: {syntax}")
    if remedy:
        lines.append(f"  {remedy}")
    fields = _READ_OP_AT_FIELDS.get(op)
    if fields:
        lines.append(
            f"  A value that contains ':' cannot be spelled on the colon CLI "
            f"— use {op}:@- (fields: {', '.join(fields)}).")
    return nl.join(lines) + nl


def _extra_token_remedy(op: str, parts: List[str], extra: List[str]) -> str:
    """The one-line repair, when the tokens say what was meant (#1582).

    `lines=` is the spelling a reader reaches for after seeing `START-END` in
    the syntax string, so it gets the call spelled out rather than a bare
    rejection. Everything else gets nothing: guessing at a token nobody
    recognises is how the drop happened in the first place.
    """
    if op != "read" or len(parts) < 2:
        return ""
    for tok in extra:
        if tok.startswith("lines=") and _READ_RANGE_RE.fullmatch(tok[6:]):
            # `_flat_field`, not the raw token: this is the one place the
            # refusal echoes a caller-supplied string UNQUOTED, so that the
            # repair can be copied and run. A path is whatever the caller
            # typed, `str.splitlines()` breaks on ten separators (#886), and a
            # forged `[result]` line at column 0 inside a refusal is the same
            # defect the header already guards against.
            return (f"For a line range use the syntax form: "
                    f"read:{_flat_field(parts[1], disclose_newline=True)}"
                    f":{tok[6:]}")
    return ""


def _grep_peel_trailing(parts: List[str]) -> Tuple[List[str], List[str],
                                                   bool, bool, bool]:
    """The two right-to-left peels grep's colon form does, in one place.

    Shared so that "how many trailing tokens were there" and "which two did the
    op read" are answered by the same code. They were not, and #1345 is the gap:
    the parser knew it had peeled three and read two, and nothing compared the
    numbers.

    Returns (args, trailing, count_only, no_auto_read, full) — `args` being
    what is left for the PATTERN and PATH slots. `full` (#1712) joins the
    same word-flag peel as `count` and `no-auto-read`: it is order-independent
    with them and with LIMIT/CONTEXT, which sit in the still-numeric `trailing`
    slots peeled afterwards.
    """
    args = parts[1:]
    count_only = False
    no_auto_read = False
    full = False
    while args and args[-1] in ("count", "no-auto-read", "full"):
        if args[-1] == "count":
            count_only = True
        elif args[-1] == "no-auto-read":
            no_auto_read = True
        else:
            full = True
        args = args[:-1]
    trailing: List[str] = []
    while len(args) >= 3 and (_is_ascii_int(args[-1])
                              or args[-1] == _GREP_ALL_TOKEN):
        trailing.insert(0, args[-1])
        args = args[:-1]
    return args, trailing, count_only, no_auto_read, full


def _grep_peeled_extras(parts: List[str]) -> List[str]:
    """Trailing tokens grep peeled off and never read (#1345).

    `grep:PAT:PATH:5:3:2` peels three and reads two, so the `2` vanished with
    no refusal and no note: a well-formed answer to a slightly different
    question, which is the house defect in its quietest form.

    Returns [] when `all` sits outside the LIMIT slot, because #1328's
    `_GREP_ALL_OUTSIDE_LIMIT_SLOT` says strictly more about that same call —
    which token it is, and where it belongs.
    """
    if not parts[1:]:
        return []
    _args, trailing, _count, _no_auto, _full = _grep_peel_trailing(parts)
    if _GREP_ALL_TOKEN in trailing[1:]:
        return []
    return trailing[2:]


def _swap_suggest(op: str, sig: str, other_key: str, other_value: str,
                  missing_value: str, call_template: str) -> Optional[str]:
    """Suggest a positional swap when the value in the OTHER slot resolves
    to a real file/dir and the missing one does not (#1711).

    `_colon_split_hint` already answers "your PATTERN has a ':' in it and
    swallowed the PATH slot" — a different mistake, and the one arity that
    reaches it (4+ tokens) is not the one a swap usually lands on. A swap
    needs no ':' anywhere: both values are ordinary tokens, just in the
    wrong slots, and `_colon_split_hint` declines on exactly that shape
    (`":" not in leading and _looks_like_path(path)` is true for a plain
    identifier in the path slot). This closes that gap generically rather
    than per-op: whenever the slot that failed to resolve sits next to a
    slot that resolved to an actual file, that is a positive, checkable
    signal — not a guess from shape alone — and every op in the read family
    can ask it the same way.

    Deliberately does NOT fire on `missing_value == other_value`: pointing
    someone at swapping two identical arguments explains nothing.

    Gates `other_value` through `_gate_paths` before ever stat-ing it
    (self-review caught this): the same "a stat of an outside file is
    itself the existence oracle this gate exists to close" hazard that
    every OTHER call site in this file gates against applies here too —
    `os.path.isfile('/etc/passwd')` on the PATTERN/SYMBOL slot would leak
    whether an arbitrary host path exists through a diagnostic message
    that was never supposed to touch anything outside cwd. A value that
    fails containment is silently treated as "does not resolve" — same
    as a value that is simply missing — rather than surfaced as its own
    kind of refusal, so this stays a hint and never a second gate with its
    own wording to keep in sync.
    """
    if not other_value or other_value == missing_value:
        return None
    _err, (_expanded,) = _gate_paths([other_value])
    if _err:
        return None
    if not (os.path.isfile(_expanded) or os.path.isdir(_expanded)):
        return None
    other_value = _expanded
    return (
        f"`{op}` takes {sig} — '{other_value}' looks like the path and "
        f"'{missing_value}' looks like the {other_key}. Did you mean: "
        f"{call_template}"
    )


def _extract_path_kw(parts: List[str]) -> Tuple[List[str], Optional[str]]:
    """Pull a `path=VALUE`/`file=VALUE` token out of a read op's args (#1690).

    `grep`, `around` and `grep_around` all take PATTERN:PATH, `read` takes
    PATH first -- internally consistent per op, but the four disagree with
    each other, and only a multi-op call surfaces that. Direction 1 from
    #1690: a caller who does not want to remember which slot the path goes
    in can name it instead, rather than at the fixed position each op
    otherwise requires.

    Purely additive. `parts[0]` (the op name) is never a candidate, so it is
    always returned untouched at index 0. Exactly one match is required —
    zero leaves `parts` untouched (ordinary positional parsing runs exactly
    as before), and two or more is left in place rather than picking one:
    an ambiguous call should fail the way it always did, not have this
    silently resolve it.

    `parts[1]` is ALSO never a candidate, and that is deliberate rather than
    an oversight (self-review caught the gap, #1711/#1690's own review): the
    PATTERN slot always sits at `parts[1]` for every op this is wired into,
    and a legitimate search for the literal string `path=` or `file=` —
    plausible in any codebase that assigns those variable names — would
    otherwise have its own pattern silently reinterpreted as the keyword.
    Restricting the scan to `parts[2:]` still closes the gap #1690 exists
    for (which slot PATH goes in past the mandatory leading PATTERN) while
    leaving the one slot every caller unconditionally controls alone.
    """
    matches = [i for i, p in enumerate(parts)
               if i > 1 and (p.startswith("path=") or p.startswith("file="))]
    if len(matches) != 1:
        return parts, None
    i = matches[0]
    value = parts[i].split("=", 1)[1]
    if not value:
        return parts, None
    return parts[:i] + parts[i + 1:], value


def _parse_grep_args(parts: List[str]) -> tuple:
    """Parse grep tokens, handling '::' in patterns (e.g. Class::CONST).

    Format: grep:PATTERN:PATH:LIMIT:CONTEXT:count
    The challenge: PATTERN may contain ':' (PHP ::, URL schemes, etc.).
    Strategy: parse known trailing fields (count, context, limit) from the
    right, then the path, and rejoin everything left as the pattern.

    A third trailing token is not this function's business to refuse — it
    returns a 7-tuple and has no error channel — so dispatch asks
    `_grep_peeled_extras` the same question off the same peel (#1345).

    The 7th field, `full` (#1712), is a word flag peeled alongside `count`
    and `no-auto-read` — order-independent with them and with LIMIT/CONTEXT —
    rather than a new positional slot, so `grep:PAT:PATH:LIMIT:CTX:full` and
    every existing caller's arity both parse unchanged.
    """
    # parts[0] is 'grep', work with parts[1:]
    if not parts[1:]:
        return ("", ".", _get_op_int("grep", "max_results", MAX_GREP_RESULTS), 0, False, False, False)

    # Peel the trailing LIMIT[:CONTEXT] slots: format is ...PATH:LIMIT:CONTEXT.
    # Two trailing tokens = limit + context; one = limit only. `all` (#1328) is
    # accepted in the LIMIT slot alongside the digits — before this it was not a
    # digit, so it fell through to the PATH slot and `grep:PAT:PATH:all` searched
    # a directory called `all`.
    context = 0
    limit = _get_op_int("grep", "max_results", MAX_GREP_RESULTS)
    args, trailing, count_only, no_auto_read, full = _grep_peel_trailing(parts)
    if len(trailing) == 1:
        limit = (GREP_LIMIT_ALL if trailing[0] == _GREP_ALL_TOKEN
                 else int(trailing[0]))
    elif len(trailing) >= 2:
        limit = (GREP_LIMIT_ALL if trailing[0] == _GREP_ALL_TOKEN
                 else int(trailing[0]))
        if _GREP_ALL_TOKEN in trailing[1:]:
            # `all` is a LIMIT. Reading it as one here would run a call nobody
            # typed (limit `all` with the caller's number silently demoted to
            # context), and ignoring it would run the default under a token the
            # caller believes changed something. `trailing[1:]` rather than
            # `trailing[1]` because the peel takes every trailing token and the
            # read takes two: a third one is dropped, and a dropped `all` is
            # exactly the silent-completeness bug this token exists to close.
            limit = GREP_LIMIT_ALL_MISPLACED
        else:
            context = int(trailing[1])

    # Now args should be [pattern_parts..., path]
    # The path is the last element; everything before it is the pattern
    if len(args) >= 2:
        path = args[-1] if args[-1] else "."
        pattern = ":".join(args[:-1])
    else:
        # Single token: pattern only, no path
        pattern = args[0] if args else ""
        path = "."

    return (pattern, path, limit, context, count_only, no_auto_read, full)


def _parse_around_args(parts: List[str]) -> tuple:
    """Parse around tokens, handling '::' in patterns.

    Format: around:PATTERN:PATH:N
    Strategy: peel trailing int (N) from right, then path, rejoin rest as pattern.
    """
    args = parts[1:]
    if not args:
        return ("", "", 10)

    # Peel N (int) from right
    n = 10
    if len(args) >= 3 and _is_ascii_int(args[-1]):
        n = int(args[-1])
        args = args[:-1]

    # Last token is path, everything before is pattern
    if len(args) >= 2:
        path = args[-1] if args[-1] else ""
        pattern = ":".join(args[:-1])
    else:
        # Single token: pattern only, no path
        pattern = args[0] if args else ""
        path = ""

    return (pattern, path, n)


def _around_line_delegation(pattern: str, path: str, n: int) -> str:
    """Answer `around:PATH:LINE[:N]` as `around_line`, and say so (#1086).

    `around` takes PATTERN:PATH[:N] and `around_line` takes PATH:LINE[:N] — the
    same op family, the same output, opposite argument order, and nothing in
    either name says which. Four agents in one session picked wrong; every one
    recovered off the error message, which is why it stayed unfiled and also why
    a better error was never going to be the fix.

    Gated so it only ever converts a call that ALREADY FAILS into an answer: the
    numeric argument must not name a real file (a file called `1160` is a path,
    not a line), the argument in front of it must pass cwd containment, and it
    must resolve. No call that works today changes meaning, and the
    `:`-tokenizer is not touched — this is post-parse recovery inside the op.

    `between:PATH:START:END` is deliberately NOT given the same treatment: its
    redirect target is `read`, a different op, and #983 decided that `between`
    should keep doing exactly one thing rather than grow a fourth spelling of a
    range read. Its error already carries the fully-substituted `read` command.

    #1154 asked whether this should refuse rather than substitute, on the
    grounds the caller's intent is genuinely ambiguous. It is not: the
    `os.path.exists(path)` check above is exactly what keeps this from ever
    guessing between two real readings. When the numeric token names an
    entry `os.path.exists` reports as present, that call falls straight
    through to a literal `around` and this function returns "" —
    delegation only fires on a call whose literal reading was already
    unanswerable, so there is exactly one answer left, not a choice among
    several. (A dangling symlink is the one case `os.path.exists` reports
    as absent though the entry is really there — but a literal `around`
    against a symlink that resolves nowhere cannot answer either, so the
    same "only one reading answers" argument still holds; it is a looser
    proxy for "answerable", not a strict `os.path.exists` synonym.) #1154
    also claimed the substitution was disclosed only after the answer it
    substituted; the `note + chr(10) + op_around_line(...)` order below has
    been disclosure first since this function was written for #1086, so
    that half of the report does not reproduce here.
    `tests/test_around_line_delegation_order_1154.py` pins both — including
    that a genuinely existing numeric-named file must be spelled BARE
    (relative to cwd) to actually exercise this guard, since an absolute
    `tmp_path`-rooted name fails `_is_ascii_int` on its slashes before
    `os.path.exists` is ever reached (self-review caught the first draft's
    version of this test doing exactly that).
    """
    if not _is_ascii_int(path) or os.path.exists(path):
        return ""
    line = int(path)
    if line < 1:
        return ""
    # The promotion happens HERE and nowhere else: past this point `pattern` has
    # stopped being a pattern and is a filename. Dispatch already ran containment
    # on the path the parser computed, which in THIS reading is the numeric
    # token — so parts[1] arrives unchecked, and `around:/etc/hosts:3` read a
    # file that `around:localhost:/etc/hosts:1` — the same file, named in the
    # slot the parser does treat as a path — refuses (#1135).
    #
    # The check is here rather than at the dispatch guard because parts[1] is a
    # PATTERN in every other reading, and a pattern is not a path: gating it
    # unconditionally would refuse `around:/etc/passwd:code.py`, i.e. searching
    # a repo for an absolute path string. The dispatch guard answers "the path
    # this call resolved"; this slot only becomes a path conditionally, so the
    # guard has to be conditional too. (#1166 dropped `around` from
    # `_PATH_ARG_POSITIONS` entirely — the table gated a fixed slot the parser
    # did not necessarily use — but that changed nothing here: the promoted slot
    # was never in it.)
    _contained, (pattern,) = _gate_paths([pattern])
    if _contained:
        return _contained
    if not pattern or not os.path.isfile(pattern):
        return ""
    return (
        f"(read as around_line:{pattern}:{line}:{n} — `around` takes "
        f"PATTERN:PATH[:N], so {path!r} was the path, which is not a file. "
        f"Its sibling `around_line` takes PATH:LINE[:N], which is the only "
        f"reading that answers.)" + chr(10)
        + op_around_line(pattern, line, n)
    )


def _between_numeric_hint(parts: List[str]) -> str:
    """Error text for `between:PATH:START:END` and `between:PATH:LINE` (#983).

    `between` is SYMBOL:PATH, so a trailing line number is tokenized as the
    path and the call fails with `path not found: '20'` — a filesystem
    complaint about a number, which is the tool's own mis-split wearing the
    shape of an absence on disk. Three agents in one evening read that as
    "supertool cannot do this" and fell back to `sed` or the harness Read.

    The redirect names the op that already answers, rather than growing a
    fourth spelling of the same read: `read:PATH:START-END` is an inclusive
    1-based range and has been since 0.19.0, and `around_line:PATH:LINE[:N]`
    covers the single-line case. `between` is left doing exactly one thing.

    Returns "" whenever the shape is not unambiguously a line range — the
    numeric token must not name a real path (a file called `12` is a range
    nobody asked for), and the path in front of it must resolve, or this
    would be guessing at a plain typo.

    That resolve is a filesystem probe on parts[1], and parts[1] is a SYMBOL in
    every other reading of `between` — so `_PATH_ARG_POSITIONS["between"] =
    (2, 4)` does not cover it and dispatch's gate has already passed on a slot
    this call did not use as a path. Unguarded, the two answers below differ by
    whether the file is there, which is an existence oracle for anything the
    process can stat (#1142):

        between:/etc/hosts:3:5   -> the range redirect
        between:/etc/nope:3:5    -> path not found: '5'

    Contained here rather than in the table, the shape #1135 established: the
    slot only becomes a path conditionally, so the guard has to be conditional
    too — widening the table would refuse `between:/etc/passwd:code.py`, a
    perfectly ordinary symbol lookup. And it refuses rather than falling
    silent, because the redirect this hint would print is `read:PATH:START-END`
    on a path `read` itself refuses: advice whose remedy is refused is worse
    than the refusal. `_containment_error` never stats, so the refusal reads
    the same whether or not the file exists.
    """
    if len(parts) not in (3, 4):
        return ""
    nums = parts[2:]
    # `read:` takes both `PATH:OFFSET:LIMIT` and `PATH:START-END`, so both
    # spellings arrive here from the same muscle memory. Only the colon one used
    # to be caught; the dash one fell through to `file not found: '1-30'` — a
    # filesystem complaint about a line range, which is the exact mis-split this
    # hint exists to translate, one spelling later (#1234).
    if len(nums) == 1 and nums[0].count("-") == 1 and not os.path.exists(nums[0]):
        _a, _b = nums[0].split("-")
        if _is_ascii_int(_a) and _is_ascii_int(_b):
            nums = [_a, _b]
    if not all(_is_ascii_int(t) and not os.path.exists(t) for t in nums):
        return ""
    path = parts[1]
    if not path:
        return ""
    _contained, (path,) = _gate_paths([path])
    if _contained:
        return _contained
    if not os.path.isfile(path):
        return ""
    lines = [
        f"ERROR: between does not take line ranges — it is between:SYMBOL:PATH"
        f" (or between:re:START:END:PATH), and {parts[-1]!r} was read as the"
        f" path.",
    ]
    if len(nums) == 2:
        start, end = int(nums[0]), int(nums[1])
        if end < start:
            start, end = end, start
        lines.append(f"  For lines {start}-{end} use: read:{path}:{start}-{end}"
                     f"  (inclusive, 1-based)")
    else:
        line = int(nums[0])
        lines.append(f"  For the lines around {line} use: "
                     f"around_line:{path}:{line}[:N]")
        lines.append(f"  For an explicit span use: read:{path}:START-END"
                     f"  (inclusive, 1-based)")
    return chr(10).join(lines) + chr(10)


def _comma_path_list_suggest(op: str, path: str) -> str:
    """Hint for a `PATH,PATH,...` list handed to an op that takes one path.

    `git-resolve` accepts a comma list, so reaching for it elsewhere is the
    spelling the operator was already taught. Joined into one filename it
    fails, and the generic `wrong CWD?` advice then names the one thing that
    provably did not cause it: every entry resolves from the current
    directory. Following it produces the same error a second time (#921).

    Returns "" unless at least one entry exists — a path with a literal comma
    that is simply absent is an ordinary typo, and inventing a list there
    would trade one misreport for another. The count is stated rather than
    implied, so the caller can tell "all of these exist" from "some do".
    """
    if "," not in path:
        return ""
    entries = [e for e in path.split(",") if e]
    if len(entries) < 2:
        return ""
    # Containment before the stat, not after: the loop below is an existence
    # probe on every entry, and dispatch only ever gated the comma-JOINED
    # string — `a.py,/etc/shadow` resolves under the cwd because its first
    # character does, so the tally leaked whether the second entry existed
    # (#1142, the same oracle #1135 closed for `around`).
    _entry_err, entries = _gate_paths(entries)
    if _entry_err:
        return ""
    found = [e for e in entries if os.path.exists(e)]
    if not found:
        return ""
    tally = (f"all {len(entries)}" if len(found) == len(entries)
             else f"{len(found)} of {len(entries)}")
    return (f"a comma-separated list is not accepted here — {op} takes ONE "
            f"path, and the whole list was read as a single filename "
            f"({tally} of its entries exist, so the cwd is not the problem). "
            f"Pass one path, a directory, or one {op} op per file — several "
            f"ops batch into a single call.")


#: Entry-point shims and the sibling that holds the code they stand for. A
#: named pair, not a general "facade" test. A general one — a small module
#: that re-exports a bigger sibling — would fire on every `__init__.py` in a
#: package and still not be the thing anyone greps by mistake; this pair is
#: the one the rename created (#931) and it fires every time.
_SHIM_CORE = {"supertool.py": "_supertool.py"}


def _shim_core_beside(path: str) -> str:
    """The core file name, if PATH is the entry-point shim with it on disk.

    The gate both facade notes share. Gated on the pair actually being on disk
    together: a lone file named `supertool.py` in someone else's tree is an
    ordinary file, and a note there would be a guess.
    """
    if not path:
        return ""
    core = _SHIM_CORE.get(os.path.basename(path))
    if not core or not os.path.isfile(path):
        return ""
    if not os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(path)),
                                       core)):
        return ""
    return core


def _shim_facade_surface_note(path: str) -> str:
    """Disclose the facade beside a result that is CORRECT but partial (#1272).

    `map:supertool.py` returns the shim's two real symbols. Small, correct,
    positive — and misleading in exactly the way #1259's zero was, because the
    reader concludes they have seen the module's surface. It is the harder half:
    an absence at least looks like nothing, whereas an answer-shaped answer
    gives nobody a reason to make the second call.

    So the wording is not #1259's. That note says the result is evidence about
    the shim rather than about supertool, and sends the caller to re-run —
    right, because there the result was empty. Here the listed symbols really
    are the shim's and the list really is complete for that file, so a note
    that read as a correction would be false about a true result. This one
    affirms the result, then says the surface is next door and offers a second
    call to ADD, never a re-run to replace.
    """
    core = _shim_core_beside(path)
    if not core:
        return ""
    return (f"(note: {os.path.basename(path)} is only the entry point — "
            f"supertool's implementation lives in {core} beside it (#931). "
            f"The symbols above are the shim's own and this map is complete "
            f"for that file; supertool's own surface is next door. Add "
            f"map:{core} to see it.)" + chr(10))


def _shim_facade_note(path: str) -> str:
    """Disclose that an empty result came from an entry-point shim (#1259).

    `grep:SYMBOL:supertool.py` scans a file that by construction holds almost
    nothing and reports `0 results in 0 files, scanned 1 files`. Every clause
    is true and the three-state contract is working — `scanned 1 files` proves
    the op looked. What it cannot say is that the file it looked at is a
    facade, so the zero is byte-identical to a zero from the core, and every
    instinct to grep `supertool.py` has landed here since the split.

    Disclosure, never redirect. Scanning `_supertool.py` for a caller who
    named `supertool.py` would answer a question nobody asked and report it
    as the answer to the one they did — a quieter wrong answer than the one
    it replaced.

    Gated by `_shim_core_beside` on the pair being on disk together. Only ever
    called where the result is already empty, so a *hit* — which IS evidence
    about the shim — is left alone by this one. A hit that claims to be the
    whole file's surface is a third case, and #1272 gives it its own wording in
    `_shim_facade_surface_note`: the note there may not read as a correction.
    """
    core = _shim_core_beside(path)
    if not core:
        return ""
    return (f"(note: {os.path.basename(path)} is only the entry point — "
            f"supertool's implementation lives in {core} beside it (#931). "
            f"An empty result here is evidence about the shim, not about "
            f"supertool. Re-run against {core}.)" + chr(10))


def _multi_path_suggest(op: str, path: str,
                        call_prefix: Optional[str] = None) -> str:
    """Hint for `PATH PATH ...` handed to an op that takes one path (#1261).

    `grep PATTERN a.py b.py` is the shell spelling every caller already has.
    Written into the colon CLI the whole argument list lands in the single
    PATH slot and the call fails as one missing filename. What answered
    before was the `:`-split hint, and its prescribed repair is a payload —
    where `path` is a scalar too, so following it reproduces the failure one
    form further along. The op can positively tell the two apart: the value
    it could not resolve contains whitespace and its parts each exist.

    Returns "" unless EVERY part exists. A partial match is a genuinely
    missing path, and stacking a second diagnosis on the first would trade
    one misreport for another — the three-state rule, `docs/validators.md`.

    Returns "" as well when any part fails containment, and that check runs
    BEFORE the existence loop rather than after it: the loop is a stat on
    each part, and dispatch upstream only ever gated the whitespace-JOINED
    string, which resolves under the cwd whenever its first part does. Left
    unguarded the disclosure is an existence oracle for anything the process
    can reach (#1142).

    No new syntax on purpose. Accepting a whitespace list would make a
    filename containing a space unrepresentable in the one slot that must be
    able to name any file — and the tree already has a delimiter for the ops
    that do take lists (`validate:a.py,b.py`, `git-resolve`), so a second one
    would mean two spellings for one idea. Batching is the tool's premise and
    it already buys the round-trip the caller was reaching for.
    """
    parts = path.split()
    if len(parts) < 2:
        return ""
    _multi_err, parts = _gate_paths(parts)
    if _multi_err:
        return ""
    if not all(os.path.exists(p) for p in parts):
        return ""
    n = len(parts)
    word = {2: "TWO", 3: "THREE", 4: "FOUR"}.get(n, str(n))
    lines = [
        f"this looks like {word} paths — {op} takes ONE path (a file, or a "
        f"directory it walks), and the whole string was read as a single "
        f"filename (all {n} parts exist, so the cwd is not the problem)."
    ]
    if call_prefix:
        calls = " ".join("'" + f"{call_prefix}:{p}" + "'" for p in parts)
        lines.append("    Batch instead — several ops run in ONE call, which "
                     "is the round-trip you were reaching for:")
        lines.append(f"      ./supertool {calls}")
    else:
        lines.append("    Pass one path, or the directory they share — and "
                     "batch several ops into one call.")
    return chr(10).join(lines)


# A drive letter that sits after a SPACE (rather than after ',' or '|', the
# two separators `_split_arg` already rejoins across). `_split_arg` never
# looks across whitespace when deciding whether the piece it is holding ends
# in a bare drive letter, so 'grep:pat:file.py C:\Users\x.py' tokenizes to
# leading='pat:file.py C' + path='\Users\x.py' instead of rejoining the
# drive letter into the path (#1271).
#
# Deliberately NOT fixed in `_split_arg` itself: widening the rejoin to look
# across whitespace would change tokenization for a call that works today --
# a pattern containing a space, immediately followed by an absolute path,
# currently parses as pattern-with-space plus path and would silently become
# one token on POSIX, where nothing is broken. This repair instead runs only
# once a path has already failed to resolve, which is the one place that
# cannot break a call that currently succeeds.
_DRIVE_LETTER_AFTER_SPACE = re.compile(r"\s([A-Za-z])\Z")


def _drive_letter_swap_suggest(op: str, leading: str, path: str) -> str:
    """Recognise a drive letter split off `leading` by a preceding space.

    Fires only when rejoining the trailing letter of `leading` with `path`
    (as LETTER:path) produces a path that actually exists -- the same
    "positively checkable, not a guess from shape alone" bar every sibling
    suggest function in this file uses. Returns "" otherwise, including when
    the candidate fails containment: a value that cannot be gated is treated
    as "does not resolve", never surfaced as its own refusal (`_swap_suggest`
    follows the same rule for the same reason).

    Takes `op` rather than the caller's own `call_prefix`: that prefix is
    built from the UNFIXED `leading` (`f"{op}:{leading}"`, for `_multi_path_
    suggest`'s own repair), and appending to it here would print the broken
    leading twice -- once inside the prefix, once as `fixed_leading`.
    """
    if not path or path[0] not in ("/", "\\"):
        return ""
    m = _DRIVE_LETTER_AFTER_SPACE.search(leading)
    if not m:
        return ""
    letter = m.group(1)
    candidate = f"{letter}:{path}"
    _err, (expanded,) = _gate_paths([candidate])
    if _err or not os.path.exists(expanded):
        return ""
    fixed_leading = leading[:m.start(1) - 1]
    return (
        f"this looks like a Windows absolute path that got split on its own "
        f"drive letter: {leading!r} + {path!r} is really {fixed_leading!r} + "
        f"{candidate!r} (the drive letter followed a space, and the "
        f"tokenizer only rejoins one after ',' or '|', never after "
        f"whitespace -- #1271).\n"
        f"    {op}:{fixed_leading}:{candidate}"
    )


def _looks_like_path(tok: str) -> bool:
    """Could *tok* plausibly be a path the caller typed?

    Only used to decide whether a not-found path is more likely a typo or the
    tail of a `:`-split pattern. Whitespace, quotes, pipes and regex punctuation
    are what a spilled pattern carries; a path the caller meant has none.
    """
    if not tok or tok != tok.strip():
        return False
    return not any(c in tok for c in " \t\"'()|<>*?")


def _absorbed_pattern_segments(leading: str) -> List[str]:
    """The pattern's `:`-segments, tokenized the way dispatch tokenized them.

    `_split_arg` is what produced `leading` in the first place, and it merges a
    Windows drive letter back together (`C:\\src` is ONE token, not `C` plus
    `\\src`). A plain `leading.split(":")` here would re-fragment exactly the
    absorbed path this check exists to recognise, and it would do it only on
    the platform CI runs and the author does not.

    The op name is a throwaway: `_split_arg` splits a whole `op:arg:arg` call
    and the caller only wants the args.
    """
    return _split_arg("x:" + leading)[1:]


def _absorbed_path_hint(op: str, leading: str, path: str,
                        keys: Tuple[str, ...] = ("pattern",)) -> str:
    """Refuse a `:`-split that swallowed the PATH and widened the scan (#1417).

    `grep:_FLAGS|def main:presets/github/issues.py:::40` rejoins to the pattern
    `_FLAGS|def main:presets/github/issues.py:` with path `.`, then scans the
    whole tree (934 files when it was reported) and returns hits. The `|` is
    incidental — `grep:PAT:PATH:` does the same with no alternation in it. The
    trigger is the empty PATH token, which `_parse_grep_args` turns into `.`.

    **Why this refuses where `around_line` auto-corrects.** `around:FILE:232:12`
    is redirected to `around_line` and disclosed, and that is safe for one
    reason only: the reading it replaces is a hard failure (`path not found:
    232`), so there is nothing to lose and one candidate answer to gain. Here
    the reading it would replace is a *successful* whole-tree sweep, so a silent
    redirect swaps one answer nobody typed for another. Two live candidates and
    no way to rank them is the shape that has to decline instead of guess.

    **grep only, deliberately.** `_colon_split_hint` is shared with `around`
    and `between`, and wiring this in there looked free and is not. `around`
    and `between` never *default* a path to `.` — an empty slot becomes `""`
    and fails loudly — so `.` there is always a value the caller typed. Worse,
    `between:re:START:END:PATH` has three caller-supplied arguments before the
    path, so `between:re:START:code.py:.` is an ordinary tree-wide range search
    whose END happens to name a file; treating that as an absorbed path is a
    false refusal, and the repair it printed dropped the `re:` marker that
    selects the mode. One op's parser defect does not generalise to a family.

    Gated hard, because the re-read is normally right: measured over the 165
    distinct `grep:` spellings written down in this repo, 39 hit the `:` rejoin
    and **none** of them is declined here. The refusal needs a segment of the
    rejoined pattern to name an existing file or directory, and needs it to sit
    after the first segment — the PATH slot follows the pattern, so a leading
    segment that happens to name a file is a pattern.

    Containment is checked per segment, and a segment that fails it is skipped
    rather than reported. Dispatch gates the PATH slot before calling this
    (#1166) so the stat below is safe; a segment of the PATTERN arrives
    unchecked, and confirming `/etc/shadow` back to the caller would be exactly
    the existence oracle that gate closes.
    """
    if path != "." or ":" not in leading:
        return ""
    segments = _absorbed_pattern_segments(leading)
    found = -1
    for i, segment in enumerate(segments):
        if i == 0 or not segment or segment == ".":
            continue
        contained, (expanded,) = _gate_paths([segment])
        if contained:
            continue
        if os.path.exists(expanded):
            found = i
    if found < 0:
        return ""
    named = segments[found]
    meant = ":".join(segments[:found])
    q = chr(39) * 3
    fields = chr(10).join(f"    {k} = {q}<{k}, colons and all>{q}" for k in keys)
    nl = chr(10)
    return (
        f"ERROR: {op} would have scanned the whole tree, not the file you "
        f"named (#1417).{nl}"
        f"  Read as {keys[0]}={leading!r} + path='.' — {named!r} stayed inside "
        f"the {keys[0]}, and the PATH slot resolved to the repo root (an empty "
        f"slot and a literal '.' both produce it).{nl}"
        f"  Nothing would have failed: that scan succeeds, over every file, "
        f"for a {keys[0]} you did not write. So it is declined rather than "
        f"corrected — both readings are live and neither is guessable.{nl}"
        f"  If you meant that file:{nl}"
        f"    {op}:{meant}:{named}{nl}"
        f"  If you did mean the whole tree, a payload says where the {keys[0]} "
        f"ends and the colon CLI cannot:{nl}"
        f"    ./supertool '{op}:@-' <<'EOF'{nl}"
        f"{fields}{nl}"
        f'    path = "."{nl}'
        f"    EOF{nl}"
        f"  (or {op}:@file.toml — same shape the mutating ops use.){nl}"
    )


# Which grep_around slot each numeric position is, for the refusal below, and
# whether `all` is a value that slot takes. It is a LIMIT and only a LIMIT
# (#1328), so `grep_around:PAT:PATH:N:all` is a shipped spelling the refusal
# must not swallow — caught by `test_grep_around_takes_all_too` while #1826 was
# being written, which is the same "a guard deleted a guard" failure the
# docstring below argues a right-to-left rejoin would cause. In the N slot
# `all` never reaches here: `_GREP_AROUND_ALL_IN_N_SLOT` returns first with a
# message that names the slot order, which is more specific than this one.
_GREP_AROUND_NUMERIC_SLOTS = ((3, "N", False), (4, "LIMIT", True))


def _grep_around_numeric_refusal(parts: List[str], pattern: str,
                                 path: str) -> str:
    """Refuse a grep_around whose N or LIMIT slot is not a number (#1826).

    `grep_around` is the one op in this family that keeps PATTERN and PATH in
    **fixed** slots, and that is deliberate rather than an oversight in need of
    the `_parse_grep_args` rejoin. Three shipped guards are built on it:
    `_PATH_ARG_POSITIONS["grep_around"] = (2,)` gates the path statically,
    `_GREP_AROUND_ALL_IN_N_SLOT` reads `all` out of slot 3, and #1345's
    fifth-token refusal counts slots. A right-to-left rejoin peels trailing
    integers, so `grep_around:PAT:PATH:all` would take `all` for the PATH and
    the first two guards would be gone — which is why this is a refusal and not
    a parse change.

    What the fixed slots cost is that a `:` in the PATTERN puts its own tail in
    a numeric slot. Until now that surfaced as `ERROR: argument parsing:
    invalid literal for int() with base 10: 'CONST'` — the interpreter talking
    about the caller's search term. It also means #1065's `pattern read as`
    disclosure, extended to `around` in #1821, can never fire here:
    `_colon_split_hint` needs a `:` in the *leading* argument and `parts[1]`
    cannot hold one. So this refusal carries the disclosure itself.

    Returns "" when both slots are absent, empty or numeric — every call that
    worked before still reaches `op_grep` unchanged.
    """
    for index, slot, takes_all in _GREP_AROUND_NUMERIC_SLOTS:
        if len(parts) <= index:
            return ""
        token = parts[index]
        if not token or _is_ascii_int(token):
            continue
        if takes_all and token == _GREP_ALL_TOKEN:
            continue
        q = chr(39) * 3
        nl = chr(10)
        return (
            f"ERROR: grep_around: the {slot} slot takes a number and got "
            f"{token!r}.{nl}"
            f"  Read as pattern={pattern!r} + path={path!r} — grep_around keeps "
            f"both in fixed slots (PATTERN:PATH:N:LIMIT) and does not rejoin a "
            f"':' back into the pattern, unlike grep and around.{nl}"
            f"  If your pattern contains ':', that is the likely cause, and a "
            f"payload says where it ends:{nl}"
            f"    ./supertool 'grep_around:@-' <<'EOF'{nl}"
            f"    pattern = {q}<pattern, colons and all>{q}{nl}"
            f'    path = "<path>"{nl}'
            f"    n = 3{nl}"
            f"    limit = 10{nl}"
            f"    EOF{nl}"
            f"  (or grep_around:@file.toml — same shape the mutating ops "
            f"use.){nl}"
        )
    return ""


def _colon_split_hint(op: str, leading: str, path: str,
                      keys: Tuple[str, ...] = ("pattern",),
                      call_prefix: Optional[str] = None,
                      swap_fallback: bool = True) -> str:
    """Error text for a read op whose PATH does not exist and probably should.

    A read op that mis-tokenizes must never read as an absence in the world.
    grep/around/between already fail loudly on a missing path rather than
    returning a bare zero — this makes the failure NAME the cause and carry
    the escape, so the next caller does not re-derive it from scratch (#625).

    Returns "" when the missing path is an ordinary typo (no ':' in the
    leading argument and the path token still looks like a path) — crying
    wolf on every wrong path would make the advice worthless.

    `swap_fallback` must be False for a caller with no `_swap_suggest`
    fallback of its own downstream (#1972 review caught this): declining
    here is only safe when returning "" hands the call to code that will
    say something else instead. `between:re:START:END:PATH` shares this
    function but calls `op_between_pattern`, which has no swap-suggest
    branch at all -- declining there for `leading` a colon-joined
    `START:END` was a silent zero-diagnostic regression, not a wash, the
    first time this shipped.
    """
    if not path or path == "." or os.path.exists(path):
        # (#1271 follow-up, CI: windows-latest) On a single-drive Windows
        # machine -- virtually every real install, and every windows-latest
        # CI runner observed so far -- a path missing its drive letter
        # (e.g. "\Users\x.py") is NOT a broken path: it resolves against
        # the current drive, and since there is only one drive, that
        # resolution names the EXACT SAME FILE the full "C:\Users\x.py"
        # would have. `os.path.exists(path)` therefore returns True for
        # BOTH a genuinely correct drive-relative call and a mis-tokenized
        # one that dropped its drive letter -- the two are indistinguishable
        # from here, because on such a machine they resolve to the same
        # thing. A version of this function that fired anyway (tried and
        # reverted, see the PR history) turned a working call into a false
        # "path not found" whenever a caller's own pattern coincidentally
        # ended in whitespace plus the drive letter -- worse than the bug it
        # was chasing. This one specific shape is therefore an accepted
        # limitation, not a gap to close here: the diagnosis below still
        # fires whenever the naive path genuinely does not resolve (a
        # multi-drive machine, or a target that does not exist under the
        # current drive either), which is the only case it can safely tell
        # apart from a call that already works.
        return ""
    if ":" not in leading and _looks_like_path(path):
        return ""
    # #1972 (CI, windows-latest): the check above declines only when
    # `leading` has NO colon at all -- true for an ordinary swap on POSIX,
    # false on Windows, where an absolute path always carries one from its
    # own drive letter (`C:\Users\...`). That platform difference must not
    # change which diagnosis fires: `leading` resolving as a real file is
    # the SAME positive, checkable evidence `_swap_suggest` uses downstream
    # (in `op_around`/`op_grep`/`op_between_symbol`'s own fallback, reached
    # once this returns "") -- ONLY for the callers that have that
    # fallback, which is what `swap_fallback` gates. Gated through
    # `_gate_paths` for the same reason `_swap_suggest` is: a bare
    # `os.path.isfile` on caller-controlled text would make this an
    # existence oracle for paths outside the containment boundary.
    if swap_fallback:
        _leading_err, (_leading_expanded,) = _gate_paths([leading])
        if not _leading_err and (os.path.isfile(_leading_expanded)
                                  or os.path.isdir(_leading_expanded)):
            return ""
    # Ahead of the `:` diagnosis, not after it. A value that whitespace-splits
    # into parts which all exist is positively a different mistake, and the
    # colon advice points at a payload whose `path` is a scalar as well — a
    # repair that fails identically one form further along (#1261).
    if call_prefix is None:
        call_prefix = f"{op}:{leading}"
    _drive = _drive_letter_swap_suggest(op, leading, path)
    if _drive:
        return _path_not_found(path, suggest=_drive)
    _multi = _multi_path_suggest(op, path, call_prefix)
    if _multi:
        return _path_not_found(path, suggest=_multi)
    original = f"{leading}:{path}"
    q = chr(39) * 3
    fields = "\n".join(f"    {k} = {q}<{k}, colons and all>{q}" for k in keys)
    return (
        f"ERROR: path not found: {path!r} (cwd: {os.getcwd()})\n"
        f"  Read as {'+'.join(keys)}={leading!r} + path={path!r}"
        f" — i.e. {original!r} split on ':'.\n"
        f"  If your {keys[0]} contains ':', that split is the likely cause. "
        f"The colon CLI cannot tell where the {keys[0]} ends; a payload can:\n"
        f"    ./supertool '{op}:@-' <<'EOF'\n"
        f"{fields}\n"
        f"    path = \"<path>\"\n"
        f"    EOF\n"
        f"  (or {op}:@file.toml — same shape the mutating ops use.)\n"
    )
