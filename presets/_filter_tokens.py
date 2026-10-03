#!/usr/bin/env python3








































from __future__ import annotations

import re

import _digits  
import string





POSITIVE_INT = "a positive integer"




POSITIVE_INT_LIST = "a comma-separated list of positive integers"






ISO_INSTANT = ("an ISO-8601 date or instant (2026-08-09, or "
               "2026-08-09T16:07:45+00:00)")




















ISO_INSTANT_OR_TAG = ("an ISO-8601 date or instant (2026-08-09, or "
                      "2026-08-09T16:07:45+00:00), or the name of a tag in "
                      "this clone (v0.34.0) — a tag whose name is itself a "
                      "date is read as the date, so spell that one "
                      "refs/tags/2026-08-09")




_REF_ALLOWED = frozenset(string.ascii_letters + string.digits + "._-+/")


def looks_like_ref(value: object) -> bool:

















    text = str(value or "")
    if not text or text != text.strip():
        return False
    if text.startswith("-"):
        return False


    if len(text) >= 5 and _digits.is_ascii_int(text[:4]) and text[4] == "-":
        return False
    return all(c in _REF_ALLOWED for c in text)


















_ISO_INSTANT = re.compile(
    r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
    r"(?:[Tt ](?P<h>\d{2}):(?P<mi>\d{2})"
    r"(?::(?P<s>\d{2})(?:[.,](?P<f>\d+))?)?"
    r"(?P<tz>[Zz]|[+-]\d{2}:?\d{2}(?::?\d{2})?)?)?"
)


def parse_iso_instant(value: object):















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



        return None
    zone = match.group("tz") or ""
    if zone and zone not in ("Z", "z"):
        digits = zone[1:].replace(":", "")
        offset = timedelta(hours=int(digits[0:2]), minutes=int(digits[2:4]),
                           seconds=int(digits[4:6] or 0))
        try:
            tzinfo = timezone(-offset if zone[0] == "-" else offset)
        except ValueError:

            return None
        parsed = parsed.replace(tzinfo=tzinfo)
    else:



        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_multi(
    arg_str: str,
    filter_keys: set[str] | frozenset[str],
    flag_names: set[str] | frozenset[str],
    list_keys: set[str] | frozenset[str] = frozenset(),
) -> tuple[dict[str, list[str]], set[str], list[str]]:



















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

















                unknown.append(tok)
            else:
                filters.setdefault(key.strip(), []).append(val.strip())



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














    multi, flags, unknown = parse_multi(arg_str, filter_keys, flag_names, list_keys)
    scalar = {
        k: (",".join(v) if k in list_keys else v[-1]) for k, v in multi.items()
    }
    return scalar, flags, unknown


def is_empty_value(tok: str, filter_keys: set[str] | frozenset[str]) -> bool:

    if "=" not in tok:
        return False
    key, _, val = tok.partition("=")
    return key.strip() in filter_keys and not val.strip()


def unknown_error(
    unknown: list[str],
    filter_keys: set[str] | frozenset[str],
    flag_names: set[str] | frozenset[str],
) -> str:











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






    bad: list[tuple[str, str, str]] = []
    for key, allowed in domains.items():
        if key not in filters:
            continue
        val = filters[key]
        if allowed is POSITIVE_INT:






            ok = _digits.is_ascii_int(val) and int(val) >= 1
            expected = POSITIVE_INT
        elif allowed is POSITIVE_INT_LIST:
            members = [m.strip() for m in val.split(",")]
            ok = bool(members) and all(
                _digits.is_ascii_int(m) and int(m) >= 1 for m in members)
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

    parts = [
        f"{key}={val!r} (accepted: {expected})" for key, val, expected in bad
    ]
    return (
        "ERROR: value(s) this op cannot apply: " + "; ".join(parts)
        + ". The request would have been dropped when the query was built and "
          "the default answered in its place, so it is refused instead."
    )
