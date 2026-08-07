#!/usr/bin/env python3
"""The one tokenizer the `gh-*` / `gl-*` boards share, and the one refusal.

`gh-issues`, `gh-prs` and `gl-mrs` all take their filters as one comma-separated
segment — `author=@me,state=open,failed` — so the single supertool arg never
collides with the `:` op tokenizer. Each of them used to parse that segment with
its own loop, and each loop ended the same way:

    elif tok in _FLAGS:
        flags.add(tok)
    # nothing else

A token that was neither a known flag nor a `key=value` the op forwards fell off
the end, and the board was built as though nobody had asked for anything. That
is this repo's defect class with the sign flipped: not an absence produced by
the tool read as an absence in the world, but a *failure to narrow* read as a
property of the world — and it is the worse direction, because an empty board
invites suspicion and a full, plausible one does not
([#864](https://github.com/Digital-Process-Tools/claude-supertool/issues/864),
#939).

#864 fixed one of the three. Two independently written refusal paths in one
preset family is how they disagree again in three months — which is exactly the
asymmetry #939 was filed about, `gh-issues` refusing a typo while `gh-prs`
answered it with the whole board — so the tokenizer, the vocabulary check and
the wording of the refusal live here once.

Three kinds of "not applied" are distinguished, because they want different
sentences:

* **Never heard of it.** A bare token that is not a flag, or a `key=` this op
  has no mapping for. Refused: nothing downstream would ever have seen it.
* **Known key, unmappable value.** `state=mergd` — the key is forwarded but the
  value has no flag, so the argv is built without it and the *default* board
  renders as the filtered one. Refused, and the accepted values are named.
* **Known key, value the backend rejects or matches.** `label=nosuchlabel` is
  forwarded verbatim; whether it matches is GitHub's or GitLab's answer, not
  this module's. Not refused — an empty board there is the truth, and
  pre-judging it here would invent a client-side vocabulary that drifts from
  the server's.
"""
from __future__ import annotations

# Sentinel domain: the value must parse as an integer >= 1. Used for `per=`,
# where a non-numeric value used to fall back to the default page size in
# silence — a caller who asked for a different window and was not told they did
# not get one.
POSITIVE_INT = "a positive integer"


def parse_multi(
    arg_str: str,
    filter_keys: set[str] | frozenset[str],
    flag_names: set[str] | frozenset[str],
) -> tuple[dict[str, list[str]], set[str], list[str]]:
    """Tokenise a comma-separated arg string, keeping every value of a key.

    A repeated key accumulates rather than overwriting, which is how a caller
    asks for more than one author when the list endpoint takes only one per
    query. The third return value is every token that was placed nowhere.
    """
    filters: dict[str, list[str]] = {}
    flags: set[str] = set()
    unknown: list[str] = []
    for tok in (t.strip() for t in arg_str.split(",")):
        if not tok:
            continue
        if "=" in tok:
            key, _, val = tok.partition("=")
            if key.strip() in filter_keys:
                filters.setdefault(key.strip(), []).append(val.strip())
            else:
                unknown.append(tok)
        elif tok in flag_names:
            flags.add(tok)
        else:
            unknown.append(tok)
    return filters, flags, unknown


def parse(
    arg_str: str,
    filter_keys: set[str] | frozenset[str],
    flag_names: set[str] | frozenset[str],
) -> tuple[dict[str, str], set[str], list[str]]:
    """The scalar view of `parse_multi` — a repeated key keeps its last value.

    One tokenizer behind both readings, so a board and the radar tier that
    shares its vocabulary can never disagree about what an arg string said.
    """
    multi, flags, unknown = parse_multi(arg_str, filter_keys, flag_names)
    return {k: v[-1] for k, v in multi.items()}, flags, unknown


def unknown_error(
    unknown: list[str],
    filter_keys: set[str] | frozenset[str],
    flag_names: set[str] | frozenset[str],
) -> str:
    """Name every token that was not applied, and what would have been.

    An error that says what is wrong but not what to do is its own filing, so
    the accepted filters and flags are listed rather than alluded to.
    """
    return (
        "ERROR: unrecognised token(s): " + ", ".join(repr(t) for t in unknown)
        + ". Nothing was filtered by them, so the board is NOT the answer to "
          "the question you asked — refusing rather than printing it. "
          "Filters: " + ", ".join(sorted(filter_keys))
        + ". Flags: " + ", ".join(sorted(flag_names)) + "."
    )


def bad_values(
    filters: dict[str, str],
    domains: dict[str, object],
) -> list[tuple[str, str, str]]:
    """Known keys whose value this op has no mapping for.

    Returns `(key, value, what would have been accepted)` per offender. Only
    keys listed in `domains` are checked — everything else is forwarded, and
    whether the backend likes it is the backend's answer to give.
    """
    bad: list[tuple[str, str, str]] = []
    for key, allowed in domains.items():
        if key not in filters:
            continue
        val = filters[key]
        if allowed is POSITIVE_INT:
            ok = val.isdigit() and int(val) >= 1
            expected = POSITIVE_INT
        else:
            assert isinstance(allowed, (set, frozenset))
            ok = val in allowed
            expected = ", ".join(sorted(str(a) for a in allowed))
        if not ok:
            bad.append((key, val, expected))
    return bad


def extra_segments_error(argv: list[str], op: str) -> str | None:
    """Refuse when the `:` tokenizer split an arg the board only reads once.

    A board op's whole grammar is ONE comma-separated segment, so `main()` reads
    `sys.argv[1]` and nothing else. But supertool hands every `:`-segment to the
    preset as its own argv entry, so `gh-issues:state=open:GARBAGE` arrives as
    `['state=open', 'GARBAGE']` and the second half is dropped in silence — the
    full board printed as the answer to a question it does not answer.

    That is #864 one layer up. #864 taught the *tokenizer* to refuse a token it
    could not place; nothing guarded the argv the tokenizer is handed, so the
    refusal is not bypassed by a mangled token, it is bypassed by never being
    shown the token at all (#964).

    The colon-in-a-value case is one instance: `label=lane:tracker-ops` splits
    into a perfectly valid `label=lane` plus an orphan, so every value-level
    check passes and the wrong label is queried. There is no escape (`\\:`
    splits identically, and #806 declined to promote it), so this refuses and
    names the one form that survives the tokenizer.

    Returns None for the ordinary single-segment call. Ops with a genuinely
    positional grammar — `gh-job:ID:raw:START:END` — must NOT call this.
    """
    extra = [a for a in argv[2:]]
    if not extra:
        return None
    return (
        "ERROR: extra ':' segment(s) that were never applied: "
        + ", ".join(repr(t) for t in extra)
        + f". `{op}` takes its filters as ONE comma-separated segment, and "
          "supertool splits the op argument on ':' — so everything after the "
          "first ':' was dropped before any filter was parsed. The board would "
          "have been built from a partly-applied filter and is NOT the answer "
          "to the question you asked, so it is refused rather than printed. "
          f"Write it as one segment: {op}:key=value,key=value. A value that "
          "itself contains ':' cannot be expressed here at all — there is no "
          "escape — so query it with the backend CLI directly and file the "
          "gap."
    )


def value_error(bad: list[tuple[str, str, str]]) -> str:
    """The refusal for a key that is known and a value that is not."""
    parts = [
        f"{key}={val!r} (accepted: {expected})" for key, val, expected in bad
    ]
    return (
        "ERROR: value(s) this op cannot apply: " + "; ".join(parts)
        + ". The request would have been dropped when the query was built and "
          "the default answered in its place, so it is refused instead."
    )
