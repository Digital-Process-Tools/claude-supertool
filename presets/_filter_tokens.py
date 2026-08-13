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

import re
import string

# Sentinel domain: the value must parse as an integer >= 1. Used for `per=`,
# where a non-numeric value used to fall back to the default page size in
# silence — a caller who asked for a different window and was not told they did
# not get one.
POSITIVE_INT = "a positive integer"

# Sentinel domain: the value must be a comma-separated list of integers >= 1.
# The list arrives through `list_keys` below, which is what makes
# `iids=1233,1240,1251` one value rather than a value and two orphans (#1323).
POSITIVE_INT_LIST = "a comma-separated list of positive integers"

# Sentinel domain: the value must parse as an ISO-8601 date or instant. Used by
# `gh-prs:merged-since=` (#1411). The parser is `parse_iso_instant` below and it
# is the same one the row-level check uses, which is the point: #1209 was two
# clocks and one comparison, so a boundary filter with a private parser beside
# the one that places the rows would be that defect wearing a filter's hat.
ISO_INSTANT = ("an ISO-8601 date or instant (2026-08-09, or "
               "2026-08-09T16:07:45+00:00)")

# Sentinel domain: the same, plus the name of a tag in the local clone. Used by
# `gh-prs:merged-since=` since #1405, and it is not a convenience.
#
# supertool splits an op argument on ':', so `merged-since=2026-08-11T20:57:19Z`
# arrives as three segments and the value is gone before any filter is parsed —
# `extra_segments_error` refuses it and says in as many words that there is no
# escape. That left a bare date as the only typable spelling, and a bare date is
# midnight UTC: against v0.35.0's real boundary, `merged:>2026-08-11T00:00:00Z`
# returns 75 PRs where `merged:>2026-08-11T18:57:19Z` returns 20.
#
# A tag name contains no ':'. It is the only colon-free spelling of a
# second-precision boundary, which is why the release gate is expressed as one.
#
# A tag whose name is itself date-shaped — `2026-08-09`, a common release
# convention — is read as the date, and `looks_like_ref` refuses it as a ref on
# purpose (`2026-13-45` is a typo, not a tag to go hunting for). That left such
# a repository with no spelling at all for its own tag while this sentence named
# one, so `refs/tags/NAME` is accepted and named here: git's own spelling, free
# of ':', and never parseable as a date (#1526).
ISO_INSTANT_OR_TAG = ("an ISO-8601 date or instant (2026-08-09, or "
                      "2026-08-09T16:07:45+00:00), or the name of a tag in "
                      "this clone (v0.34.0) — a tag whose name is itself a "
                      "date is read as the date, so spell that one "
                      "refs/tags/2026-08-09")

# An allowlist rather than a list of forbidden characters, because this value
# is handed to `git` as an argument. Every spelling in this repository's own
# history fits it: `v0.31.0`, `0.3.2`, `v1.0.0-rc1`, `release/2026-08`.
_REF_ALLOWED = frozenset(string.ascii_letters + string.digits + "._-+/")


def looks_like_ref(value: object) -> bool:
    """Could this value be a tag name? **Shape only — never existence.**

    Existence has three outcomes and a filter value has two, which is why the
    two questions are asked in different places: a filter may refuse a value,
    but it may not pick between two defensible boundaries. Resolution and its
    three states live in `_release_gate.resolve_boundary`.

    A date-shaped value is deliberately NOT a candidate ref. `2026-13-45` is a
    typo in a date, and "no tag by that name" is the wrong sentence to hand
    somebody who typed one — they would go looking for a tag they never meant.

    `refs/tags/2026-08-09` is how a repository that tags releases by date names
    its own tag anyway (#1526). It passes here for free — the four-digit test
    reads the start of the string — and `select_tag` matches it against
    `for-each-ref`'s short names. Until that pair existed the refusal named a
    remedy ("spell it as a date, or as a ref") that this input had neither of.
    """
    text = str(value or "")
    if not text or text != text.strip():
        return False
    if text.startswith("-"):
        return False
    # Four digits then a dash: the caller meant a date. Send them back to the
    # date refusal rather than off hunting for a tag.
    if len(text) >= 5 and text[:4].isdigit() and text[4] == "-":
        return False
    return all(c in _REF_ALLOWED for c in text)


#: The one accepted spelling of an instant — extended ISO-8601, and nothing
#: else. Written out here rather than delegated to `datetime.fromisoformat`,
#: whose grammar WIDENED in 3.11. Measured 2026-08-13, CPython 3.9.6 (this
#: project's floor) against 3.11.11:
#:
#:     20260809      ValueError        2026-08-09 00:00:00
#:     2026-W32-1    ValueError        2026-08-03 00:00:00
#:
#: `gh-prs:merged-since=` routes on `parse_iso_instant(value) is None`, so on
#: 3.11+ `merged-since=20260809` was a date boundary at midnight UTC and on the
#: floor it was a tag name hunted in the local clone — the same call, two
#: answers, neither disclosed (#1526). Which way it resolves matters less than
#: that it resolves once, in this repository's code; the floor's grammar is the
#: one kept, because it is the one every green CI leg has already run against.
#: Basic format (`20260809`) reaches `looks_like_ref` and is a tag name on every
#: interpreter now.
_ISO_INSTANT = re.compile(
    r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
    r"(?:[Tt ](?P<h>\d{2}):(?P<mi>\d{2})"
    r"(?::(?P<s>\d{2})(?:[.,](?P<f>\d+))?)?"
    r"(?P<tz>[Zz]|[+-]\d{2}:?\d{2}(?::?\d{2})?)?)?"
)


def parse_iso_instant(value: object):
    """The one clock, as a UTC `datetime`, or ``None``.

    `gh` emits a `Z` suffix and `git` emits a numeric offset. Both are read
    here rather than compared as text — comparing the two spellings as strings
    is #1209, where `"16:07:45Z" > "17:13:43+02:00"` is False at the second
    character and a release was silently delayed.

    The grammar is `_ISO_INSTANT` above and it is deliberately this module's
    own rather than `datetime.fromisoformat`'s: that one accepts different
    values on 3.9 and on 3.11+, and this function is what decides whether a
    filter value is a date or a tag name (#1526).

    A value that will not parse returns ``None``. Every caller treats that as
    "could not place", never as "old" and never as "now".
    """
    from datetime import datetime, timedelta, timezone

    text = str(value or "").strip()
    match = _ISO_INSTANT.fullmatch(text)
    if match is None:
        return None
    fraction = (match.group("f") or "")[:6].ljust(6, "0")
    try:
        parsed = datetime(
            int(match.group("y")), int(match.group("mo")), int(match.group("d")),
            int(match.group("h") or 0), int(match.group("mi") or 0),
            int(match.group("s") or 0), int(fraction))
    except ValueError:
        # Shaped like a date and not one: `2026-13-45`. The shape check is the
        # regex and the calendar check is the constructor's; neither is the
        # other's, and a February 30th must not become a boundary.
        return None
    zone = match.group("tz") or ""
    if zone and zone not in ("Z", "z"):
        digits = zone[1:].replace(":", "")
        offset = timedelta(hours=int(digits[0:2]), minutes=int(digits[2:4]),
                           seconds=int(digits[4:6] or 0))
        try:
            tzinfo = timezone(-offset if zone[0] == "-" else offset)
        except ValueError:
            # `+25:00`. A zone that cannot exist is not a zone to guess at.
            return None
        parsed = parsed.replace(tzinfo=tzinfo)
    else:
        # A naive stamp is not a UTC stamp, but the alternative is to drop the
        # value entirely. Both git and gh always carry a zone, so this branch
        # exists for the bare `2026-08-09` a caller types by hand.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_multi(
    arg_str: str,
    filter_keys: set[str] | frozenset[str],
    flag_names: set[str] | frozenset[str],
    list_keys: set[str] | frozenset[str] = frozenset(),
) -> tuple[dict[str, list[str]], set[str], list[str]]:
    """Tokenise a comma-separated arg string, keeping every value of a key.

    A repeated key accumulates rather than overwriting, which is how a caller
    asks for more than one author when the list endpoint takes only one per
    query. The third return value is every token that was placed nowhere.

    **`list_keys` names the keys whose value is itself a comma-separated
    list.** The whole grammar is one comma-separated segment, so a caller
    writing `iids=1233,1240,1251` hands this function a value and two bare
    tokens — and the two orphans were refused while `iids=1233` applied, which
    is the narrowest possible answer to a question about three numbers. A bare
    token immediately following a list key rejoins that key's value.

    The continuation stops at the first token that is *placed* some other way:
    a `key=value` or a known flag. So `iids=1,nopipe,2` refuses `2` rather than
    quietly reading it as an iid across an intervening flag — the ordering is
    the caller's claim about what belongs to what, and guessing past it is the
    silent-drop defect with the sign flipped again.
    """
    filters: dict[str, list[str]] = {}
    flags: set[str] = set()
    unknown: list[str] = []
    open_list: str | None = None
    for tok in (t.strip() for t in arg_str.split(",")):
        if not tok:
            continue
        if "=" in tok:
            key, _, val = tok.partition("=")
            open_list = None
            if key.strip() not in filter_keys:
                unknown.append(tok)
            elif not val.strip():
                # A known key with no value (#974). It is NOT a filter: every
                # `_build_list_cmd` opens with `if not val: continue`, so the
                # flag is dropped and the *default* board answers a narrowing
                # question. On `gh-prs` it is worse than a drop — `has_role`
                # tests key membership, so `author=` both suppresses the
                # `--author @me` default and emits no `--author`, widening the
                # board from "mine" to everyone's while the scope line still
                # claims a filter.
                #
                # It goes down the `unknown` channel rather than a fourth
                # return value on purpose: every consumer of this tokenizer
                # already guards on `unknown` — the boards refuse, the radar
                # tiers raise, the mr-feed poller returns None — and a new
                # channel would leave each of those blind again until it was
                # taught about it, which is the shape of this bug. `unknown`
                # keeps the token verbatim, so `unknown_error` can still tell
                # the two kinds apart and word them differently.
                unknown.append(tok)
            else:
                filters.setdefault(key.strip(), []).append(val.strip())
                # Only a value this function actually stored may be continued.
                # Opening the list on the token's *shape* would let a refused
                # `iids=` swallow the numbers after it and report nothing.
                if key.strip() in list_keys:
                    open_list = key.strip()
        elif tok in flag_names:
            open_list = None
            flags.add(tok)
        elif open_list is not None:
            filters[open_list][-1] += "," + tok
        else:
            unknown.append(tok)
    return filters, flags, unknown


def parse(
    arg_str: str,
    filter_keys: set[str] | frozenset[str],
    flag_names: set[str] | frozenset[str],
    list_keys: set[str] | frozenset[str] = frozenset(),
) -> tuple[dict[str, str], set[str], list[str]]:
    """The scalar view of `parse_multi` — a repeated key keeps its last value.

    One tokenizer behind both readings, so a board and the radar tier that
    shares its vocabulary can never disagree about what an arg string said.

    **A repeated LIST key concatenates instead.** `v[-1]` is right for a scalar
    — `author=a,author=b` can only forward one `--author` — and wrong for a
    list, where every group the caller wrote is part of the one population they
    asked about. `iids=1,2,nopipe,iids=3,4` is a spelling anyone reaches by
    editing an existing call, and under the scalar rule it looked up 3 and 4
    only: no unknown token, no refusal, no disclosure, because the numbers were
    gone before the op could count them. A silently shorter list is the exact
    defect `iids=` exists to refuse, so the tokenizer must not manufacture one.
    """
    multi, flags, unknown = parse_multi(arg_str, filter_keys, flag_names, list_keys)
    scalar = {
        k: (",".join(v) if k in list_keys else v[-1]) for k, v in multi.items()
    }
    return scalar, flags, unknown


def is_empty_value(tok: str, filter_keys: set[str] | frozenset[str]) -> bool:
    """`author=` — a key this op knows, carrying no value at all (#974)."""
    if "=" not in tok:
        return False
    key, _, val = tok.partition("=")
    return key.strip() in filter_keys and not val.strip()


def unknown_error(
    unknown: list[str],
    filter_keys: set[str] | frozenset[str],
    flag_names: set[str] | frozenset[str],
) -> str:
    """Name every token that was not applied, and what would have been.

    An error that says what is wrong but not what to do is its own filing, so
    the accepted filters and flags are listed rather than alluded to.

    Two kinds share this channel and they need different sentences (#974).
    Telling a caller who typed `author=` that the token is *unrecognised*
    sends them hunting for a typo in a spelling that is correct, and never
    names the consequence that made it worth refusing — the board got WIDER,
    not narrower.
    """
    empty = [t for t in unknown if is_empty_value(t, filter_keys)]
    never = [t for t in unknown if not is_empty_value(t, filter_keys)]
    parts: list[str] = []
    if never:
        parts.append(
            "unrecognised token(s): " + ", ".join(repr(t) for t in never))
    if empty:
        parts.append(
            "filter key(s) given with no value: "
            + ", ".join(repr(t) for t in empty))
    widening = ""
    if empty:
        # The role clause is emitted only for the role keys THIS op actually
        # has and that were actually left empty. Naming `reviewer` at
        # `gh-issues`, which has no such filter, would send the reader looking
        # for a token that does not exist — a refusal that misdescribes the
        # vocabulary is its own small version of this bug.
        roles = sorted(
            k for k in ("author", "assignee", "reviewer")
            if k in filter_keys
            and any(t.partition("=")[0].strip() == k for t in empty)
        )
        role_clause = ""
        if roles:
            role_clause = (
                " " + ", ".join(f"`{r}`" for r in roles)
                + (" is a role key" if len(roles) == 1 else " are role keys")
                + ": leaving one empty also suppresses the `author=@me`"
                  " default, so the board answers with everyone's rather than"
                  " yours — wider, not narrower.")
        widening = (
            " The key is known and spelled right — the value is missing. An "
            "empty value is dropped when the query is built, so the board "
            "comes back WIDER than the one asked for rather than narrower."
            + role_clause
            + " Write `key=VALUE`, or drop the key entirely.")
    return (
        "ERROR: " + "; ".join(parts)
        + ". Nothing was filtered by them, so the board is NOT the answer to "
          "the question you asked — refusing rather than printing it."
        + widening
        + " Filters: " + ", ".join(sorted(filter_keys))
        + (". Flags: " + ", ".join(sorted(flag_names)) + "."
           if flag_names else ". This op accepts no flags at all.")
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
        elif allowed is POSITIVE_INT_LIST:
            members = [m.strip() for m in val.split(",")]
            ok = bool(members) and all(m.isdigit() and int(m) >= 1 for m in members)
            expected = POSITIVE_INT_LIST
        elif allowed is ISO_INSTANT:
            ok = parse_iso_instant(val) is not None
            expected = ISO_INSTANT
        elif allowed is ISO_INSTANT_OR_TAG:
            ok = parse_iso_instant(val) is not None or looks_like_ref(val)
            expected = ISO_INSTANT_OR_TAG
        else:
            assert isinstance(allowed, (set, frozenset))
            ok = val in allowed
            expected = ", ".join(sorted(str(a) for a in allowed))
        if not ok:
            bad.append((key, val, expected))
    return bad


def extra_segments_error(argv: list[str], op: str, hint: str = "") -> str | None:
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

    `hint` is appended when the op has a value that legitimately wants a ':'
    and a colon-free spelling for it. The closing advice — query the backend CLI
    directly and file the gap — is right when nothing here can express the
    value, and wrong when something can: `gh-prs:merged-since=` takes a bare
    date as well as an instant (#1411), so without the hint the refusal sends a
    caller away from an op that would have answered them.

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
        + (" " + hint if hint else "")
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
