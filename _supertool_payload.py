"""_supertool_payload -- the TOML mini-parser and @file/payload routing, split out of _supertool.py (#2706).

Loaded by `_load_part("_supertool_payload")` from inside `_supertool.py`, at the
exact source position this code used to occupy: a plain `exec(code,
globals())` via `_load_part`, not a real `import`. Every function defined
below therefore has `__globals__ is _supertool.__dict__` once loaded, so
every existing `monkeypatch.setattr(supertool, "<name>", ...)` keeps reaching
the code it patches.

Not importable on its own. `_load_part` is the only legitimate loader: it
puts `_load_part` itself into the globals this file executes against before
running it, which is exactly the marker the guard below checks for. A bare
`import _supertool_payload` or `python3 _supertool_payload.py` gets this
module's own fresh globals(), which has no such name, and refuses with a
clear ImportError rather than failing later with a NameError on the first
name this file assumes `_supertool.py` already defined (Dict, Any, os, re,
json, shlex, ...).
"""
from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_payload.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )


def _detect_payload_format(raw: str) -> str:
    """Return 'json' if first non-whitespace char is { or [, else 'toml'.

    Exception: a leading '[[' is a TOML table-array header (never valid
    JSON), so it is detected as TOML. This lets '[[ops]]' batch payloads
    parse correctly instead of being misread as a JSON array.
    """
    stripped = raw.lstrip(" \t\r\n")
    if stripped.startswith("[["):
        return "toml"
    for c in stripped:
        return "json" if c in "{[" else "toml"
    return "json"


_TOML_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "b": "\b", "f": "\f"}


_TOML_HEXDIGITS = frozenset("0123456789abcdefABCDEF")

_TOML_ESCAPE_ADVICE = (
    "in a basic string a backslash must be escaped as a double backslash, or "
    "use a triple-single-quote literal block, which keeps backslashes as typed"
)


def _toml_decode_escape(s: str, i: int, key_offset: int, multiline: bool) -> Tuple[str, int]:
    r"""Decode the escape starting at s[i] (a backslash); return (text, offset).

    Raises on everything TOML calls invalid, because stdlib `tomllib` raises
    and this parser stands in for it on Python <3.11 (#684). The default it
    replaces — keep the escaped character, drop the backslash — turned
    `path = "C:\Users\dev"` into `C:Usersdev`, and the op then reported a
    missing file at an address the parser had invented: same payload, same
    tool, a parse error on 3.11+ and a manufactured absence below it.

    `\u` / `\U` are decoded rather than rejected. tomllib accepts them, so
    refusing them would trade a silent divergence for a loud one. Agreement
    with tomllib is what matters here, not severity.

    `key_offset` (an int, the key's own start offset in `raw` -- never the key
    TEXT) is diagnostic only: it used to be the key string itself, echoed
    verbatim into every error message below. On Python <3.11, where this
    parser is the ONLY TOML reader (stdlib `tomllib` needs 3.11+), that meant
    a malformed `@file` reference outside the containment boundary (#896 F2 --
    documented as "not a content-disclosure channel" because `tomllib`'s own
    parse errors report only position) could have its own content echoed back
    through THIS parser's error message instead, on exactly the two Python
    versions where `tomllib` is unavailable and this code path is the one
    that runs. A bare TOML key is any run of `[A-Za-z0-9_-]` characters, so
    ordinary file content -- a hostname, a path, a hyphenated word -- reads as
    a "key" whenever it appears before the point parsing fails, and the old
    messages echoed that run in full. Position-only, matching what `tomllib`
    already does, closes the gap without losing anything a human debugging
    their own payload actually uses this message for.
    """
    c = s[i + 1] if i + 1 < len(s) else ""
    if c in _TOML_ESCAPES:
        return _TOML_ESCAPES[c], i + 2
    if c in ("u", "U"):
        width = 4 if c == "u" else 8
        digits = s[i + 2:i + 2 + width]
        if len(digits) == width and all(d in _TOML_HEXDIGITS for d in digits):
            code = int(digits, 16)
            if code < 0x110000 and not 0xD800 <= code <= 0xDFFF:
                return chr(code), i + 2 + width
        raise ValueError(
            f"invalid escape: \\{c} at offset {i} (key starting at offset "
            f"{key_offset}) wants {width} hex digits naming a Unicode scalar "
            f"— {_TOML_ESCAPE_ADVICE}"
        )
    if multiline:
        j = i + 1
        while j < len(s) and s[j] in " \t":
            j += 1
        if j < len(s) and s[j] in "\r\n":
            while j < len(s) and s[j] in " \t\r\n":
                j += 1
            return "", j
    raise ValueError(
        f"invalid escape '\\{c}' at offset {i} (key starting at offset "
        f"{key_offset}) — {_TOML_ESCAPE_ADVICE}"
    )


def _toml_basic_unescape(s: str, key_offset: int = -1, multiline: bool = True) -> str:
    out = []
    i = 0
    while i < len(s):
        if s[i] == "\\":
            text, i = _toml_decode_escape(s, i, key_offset, multiline)
            out.append(text)
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _toml_skip_ws_comments(raw: str, i: int) -> int:
    """Advance past whitespace, newlines and # comments; return the new offset.

    Used inside inline arrays, where TOML allows both to appear between
    elements — a `paths = [\n  "a",  # keep\n  "b",\n]` payload is ordinary.
    """
    n = len(raw)
    while i < n:
        if raw[i] in " \t\r\n":
            i += 1
        elif raw[i] == "#":
            while i < n and raw[i] != "\n":
                i += 1
        else:
            break
    return i


def _toml_parse_array(raw: str, i: int, key_offset: int) -> Tuple[List[Any], int]:
    """Parse an inline array at *i* (on its '['); return (items, next offset).

    Elements are whatever `_toml_parse_value` accepts, so arrays nest. A
    trailing comma is allowed (TOML permits it), whitespace and comments may
    separate elements, and a missing separator is an error rather than a
    silently truncated list.
    """
    n = len(raw)
    i += 1
    items: List[Any] = []
    while True:
        i = _toml_skip_ws_comments(raw, i)
        if i >= n:
            raise ValueError(
                f"unterminated array for the key at offset {key_offset}"
            )
        if raw[i] == "]":
            return items, i + 1
        val, i = _toml_parse_value(raw, i, key_offset)
        items.append(val)
        i = _toml_skip_ws_comments(raw, i)
        if i < n and raw[i] == ",":
            i += 1
            continue
        if i < n and raw[i] == "]":
            return items, i + 1
        raise ValueError(
            f"expected ',' or ']' in array for the key at offset "
            f"{key_offset}, at offset {i}"
        )


def _toml_multiline_close(
    raw: str, i: int, quote: str, escaped: bool
) -> Tuple[int, int, int]:
    """Locate the closing run of a TOML multi-line string opened at *i*.

    Returns `(content_end, run_start, next_index)`, or `(-1, -1, -1)` when the
    block is never closed.

    The subtlety this exists for: a closing run may be **four or five** quotes,
    and the surplus one or two belong to the content. That is not a curiosity —
    it is the only way a multi-line string can end with its own delimiter
    character, and a payload carrying quoted code meets it immediately. The
    fallback parser used to stop at the first three quotes and then choke on the
    leftovers, so that spelling parsed under stdlib `tomllib` (3.11+) and failed
    below it. #834, and the same rule as #684: the escape hatch has to exist on
    every interpreter, or the advice naming it is wrong exactly where it is
    needed most.

    A run of six or more is capped at five, which leaves a stray quote for the
    caller to trip over — the error `tomllib` raises on the same input.
    """
    n = len(raw)
    j = i
    while j < n:
        if escaped and raw[j] == chr(92):
            j += 2
            continue
        if raw[j] != quote:
            j += 1
            continue
        run = j
        while j < n and raw[j] == quote:
            j += 1
        length = j - run
        if length < 3:
            continue
        if length > 5:
            length = 5
            j = run + 5
        return run + (length - 3), run, j
    return -1, -1, -1


def _toml_parse_value(raw: str, i: int, key_offset: int) -> Tuple[Any, int]:
    """Parse one TOML value at *i*; return (value, offset just past it).

    Split out of `_mini_toml_loads` so inline arrays can recurse into it
    rather than reimplementing every scalar form. `key_offset` is the start
    offset of the key this value belongs to, for error messages ONLY -- see
    `_toml_decode_escape`'s docstring for why this is an int and not the key
    text (#896 F2, content-disclosure on Python <3.11).
    """
    n = len(raw)
    if raw[i:i + 3] == '"""':
        i += 3
        end, _run, nxt = _toml_multiline_close(raw, i, '"', True)
        if end < 0:
            raise ValueError(
                f'unterminated """ for the key at offset {key_offset}'
            )
        val: Any = _toml_basic_unescape(raw[i:end], key_offset, True)
        if val.startswith("\r\n"):
            val = val[2:]
        elif val.startswith("\n"):
            val = val[1:]
        return val, nxt
    if raw[i:i + 3] == "'''":
        i += 3
        end, _run, nxt = _toml_multiline_close(raw, i, "'", False)
        if end < 0:
            raise ValueError(
                f"unterminated ''' for the key at offset {key_offset}"
            )
        val = raw[i:end]
        if val.startswith("\r\n"):
            val = val[2:]
        elif val.startswith("\n"):
            val = val[1:]
        return val, nxt
    if raw[i] == '"':
        i += 1
        buf = []
        while i < n and raw[i] != '"':
            if raw[i] == "\\":
                text, i = _toml_decode_escape(raw, i, key_offset, False)
                buf.append(text)
            elif raw[i] == "\n":
                raise ValueError(
                    f"newline in single-line string for the key at offset "
                    f"{key_offset}"
                )
            else:
                buf.append(raw[i])
                i += 1
        if i >= n:
            raise ValueError(
                f"unterminated string for the key at offset {key_offset}"
            )
        return "".join(buf), i + 1
    if raw[i] == "'":
        i += 1
        end = raw.find("'", i)
        if end < 0 or raw.find("\n", i, end) >= 0:
            raise ValueError(
                f"unterminated literal for the key at offset {key_offset}"
            )
        return raw[i:end], end + 1
    if raw[i:i + 4] == "true" and (i + 4 == n or not raw[i + 4].isalnum()):
        return True, i + 4
    if raw[i:i + 5] == "false" and (i + 5 == n or not raw[i + 5].isalnum()):
        return False, i + 5
    if raw[i] == "[":
        return _toml_parse_array(raw, i, key_offset)
    if raw[i] == "-" or _is_ascii_int(raw[i]):
        ns = i
        if raw[i] == "-":
            i += 1
        while i < n and _is_ascii_int(raw[i]):
            i += 1
        try:
            return int(raw[ns:i]), i
        except ValueError as _e:
            raise ValueError(
                f"bad number for the key at offset {key_offset}: {_e}"
            ) from _e
    raise ValueError(
        f"unknown value type for the key at offset {key_offset}, at offset {i}"
    )


def _toml_parse_quoted_key(raw: str, i: int) -> Tuple[str, int]:
    """Parse a quoted key (`"a b" = 1` or `'a b' = 1`) at offset *i*.

    Returns (key, offset just past the closing quote). Quoted keys are
    single-line only -- TOML has no multi-line key form -- so this is the
    same grammar as the single-quote-mark VALUE branches in
    `_toml_parse_value`, not the triple-quote ones (#1595): a basic
    (double-quoted) key decodes escapes via `_toml_decode_escape`, a
    literal (single-quoted) key is used verbatim.
    """
    n = len(raw)
    key_offset = i
    quote = raw[i]
    i += 1
    if quote == chr(34):
        buf = []
        while i < n and raw[i] != chr(34):
            if raw[i] == chr(92):
                text, i = _toml_decode_escape(raw, i, key_offset, False)
                buf.append(text)
            elif raw[i] == chr(10):
                raise ValueError(
                    f"newline in quoted key at offset {key_offset}"
                )
            else:
                buf.append(raw[i])
                i += 1
        if i >= n:
            raise ValueError(f"unterminated quoted key at offset {key_offset}")
        return "".join(buf), i + 1
    end = raw.find(chr(39), i)
    if end < 0 or raw.find(chr(10), i, end) >= 0:
        raise ValueError(f"unterminated quoted key at offset {key_offset}")
    return raw[i:end], end + 1


def _mini_toml_loads(raw: str) -> Dict[str, Any]:
    """Minimal TOML parser for @file payloads.

    Supports: bare keys, integers, true/false, single-line strings
    ("..." with escapes, '...' literal), multi-line strings (\"\"\"...\"\"\"
    with escapes, '''...''' literal), inline arrays (nesting, trailing comma,
    comments between elements), # comments, `[[table]]` array-of-tables
    headers, and a single `[table]` header (#2473) -- same bare-name grammar
    as `[[table]]` (alnum, `_`, `-`; no dots, so this deliberately does not
    add a second, dotted-header convention). No dotted table headers, no
    inline tables (`{ ... }`), no dates — only what
    payloads need. A quoted key (`"my key" = 1`, `'my key' = 1`) is accepted
    (#1595), using the same single-line basic/literal string grammar as
    string VALUES: a basic (double-quoted) key decodes escapes, a literal
    (single-quoted) key keeps them as-is. Dotted keys (`a.b = 1`) remain
    unsupported.

    Inline arrays matter specifically: a variadic payload field is written as
    a list, and `git-commit:@-` with `paths = ["a", "b"]` is the documented
    form. Without them that payload parsed on 3.11+ (stdlib `tomllib`) and
    died below it with `unknown value type for the key at offset N` — the
    op's own documented syntax failing on a third of the supported matrix.

    `[[ops]]` matters specifically: it is the shape a `batch:@-` payload takes,
    and this parser is what runs on Python <3.11, where stdlib `tomllib` is
    absent. Without it a batch payload parses on 3.11+ and dies below it.

    Used as fallback when stdlib `tomllib` is unavailable (Python <3.11).

    Every error message below names an OFFSET into `raw`, never the key or
    value text itself (#896 F2). This parser is the only TOML reader on
    Python <3.11 -- there is no `tomllib` to fall back to -- so it is also
    what runs when an `@file` reference outside the containment boundary is
    malformed, and the fix for #896's TOML-parse-error content-disclosure gap
    had to land here rather than only in `_load_at_file_raw`'s own except
    clause, which never sees the raw text either way.
    """
    result: Dict[str, Any] = {}
    # Key/value pairs land here: the top-level dict, or the most recent
    # [[table]] entry once one has been opened.
    current: Dict[str, Any] = result
    i, n = 0, len(raw)
    while i < n:
        while i < n and raw[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        if raw[i] == "#":
            while i < n and raw[i] != "\n":
                i += 1
            continue
        if raw[i] == "[":
            if raw[i:i + 2] == "[[":
                end = raw.find("]]", i + 2)
                if end < 0:
                    raise ValueError(f"unterminated [[table]] header at offset {i}")
                name_offset = i + 2
                name = raw[name_offset:end].strip()
                if not name or not all(c.isalnum() or c in "_-" for c in name):
                    raise ValueError(
                        f"bad [[table]] name at offset {name_offset}"
                    )
                bucket = result.setdefault(name, [])
                if not isinstance(bucket, list):
                    raise ValueError(
                        f"the [[table]] name at offset {name_offset} is both a "
                        f"value and a [[table]]"
                    )
                current = {}
                bucket.append(current)
                i = end + 2
                continue
            # Single [table] header (#2473). Same bare-name grammar as
            # [[table]] above -- alnum, `_`, `-`, no dots -- so this does not
            # invent a second dotted-header convention; a table opened this
            # way is a dict at top level, reopened in place if seen twice,
            # never a list.
            end = raw.find("]", i + 1)
            if end < 0:
                raise ValueError(f"unterminated [table] header at offset {i}")
            name_offset = i + 1
            name = raw[name_offset:end].strip()
            if not name or not all(c.isalnum() or c in "_-" for c in name):
                raise ValueError(
                    f"bad [table] name at offset {name_offset}"
                )
            table = result.setdefault(name, {})
            if not isinstance(table, dict):
                raise ValueError(
                    f"the [table] name at offset {name_offset} is both a "
                    f"value and a [table]"
                )
            current = table
            i = end + 1
            continue
        ks = i
        if raw[i] in "\"'":
            key, i = _toml_parse_quoted_key(raw, i)
        else:
            while i < n and (raw[i].isalnum() or raw[i] in "_-"):
                i += 1
            if i == ks:
                raise ValueError(f"bad key at offset {i}")
            key = raw[ks:i]
        while i < n and raw[i] in " \t":
            i += 1
        if i >= n or raw[i] != "=":
            raise ValueError(f"expected '=' after the key at offset {ks}")
        i += 1
        while i < n and raw[i] in " \t":
            i += 1
        if i >= n:
            raise ValueError(f"missing value for the key at offset {ks}")
        val, i = _toml_parse_value(raw, i, ks)
        current[key] = val
        while i < n and raw[i] in " \t":
            i += 1
        if i < n and raw[i] == "#":
            while i < n and raw[i] != "\n":
                i += 1
    return result

_TOML_LITERAL_OPENER = re.compile(r"=[ \t]*'''")


# A prose sentence naturally ends a line WITH the delimiter it is describing
# ("...as documented here: '''") rather than following it with more text on
# the same line -- so the garbage that early close actually produces often
# lands on a LATER line, past the closer entirely (#2545). The same-line
# check above never sees it.
#
# A HEURISTIC, not a parser (review finding, #2545): dotted keys
# (`a.b = 1`) are real, legal TOML, so the bare-key alternative repeats
# itself once per `.`-separated segment -- without that, a genuine dotted
# key right after a correctly-closed, unrelated `'''` block failed this
# match and got blamed for a parse error that was actually somewhere else
# entirely (a false positive, worse than the silence this function exists
# to replace, because it speaks with a specific line/column about the wrong
# block). What this still cannot do, by construction, is tell prose that
# merely LOOKS like a key/value pair or a table header from a real one --
# `timeout = 30` or `[not a real table] more words` as more prose reads as
# a legitimate next statement either way, and the early close stays
# unflagged. That is a known, accepted gap in a best-effort hint attached
# to an error that is raised regardless of whether this hint fires.
_TOML_KEY_SEGMENT = r"(?:[A-Za-z0-9_-]+|\"[^\"\n]*\"|'[^'\n]*')"
_TOML_NEXT_STATEMENT = re.compile(
    r"\[\[?[^\n]*\]\]?"
    r"|" + _TOML_KEY_SEGMENT + r"(?:\." + _TOML_KEY_SEGMENT + r")*[ \t]*="
)


def _toml_skip_blank_and_comments(raw: str, i: int) -> int:
    """Advance *i* past whitespace and `#` comment lines, mirroring the main
    parser's own skip (#2545). Used only to look PAST a candidate early close
    for the real next statement -- never to decide whether the overall parse
    is valid.
    """
    n = len(raw)
    while i < n:
        j = i
        while j < n and raw[j] in " \t\r\n":
            j += 1
        if j < n and raw[j] == "#":
            nl = raw.find(chr(10), j)
            j = n if nl < 0 else nl
            i = j
            continue
        return j
    return n


def _toml_delimiter_early_close(raw: str) -> int:
    """Offset of the ''' run that closed a value early, or -1 (#1830, #2545).

    Checkable without a successful parse, which is the whole requirement: this
    runs *because* the parse failed. Walk every `= '''` opener, find the run
    that closes it, and look at what is left. TOML allows only whitespace or a
    `#` comment after a value, so any other text means the run that closed the
    block was carried by the content and the remainder is being read as syntax
    — which is the reported error, `Expected newline or end of document after a
    statement`, pointing at a column nowhere near the cause.

    The trailing garbage is checked on the closer's own line FIRST (#1830),
    then -- if that line was clean -- past any blank/comment lines that follow
    (#2545): a `'''` embedded in prose commonly sits at the END of a line
    ("...closes like this: '''"), pushing everything it truncated onto the
    NEXT line, where the original check never looked, and #2545 was filed on
    exactly that shape reaching the generic near-miss diagnostic silently
    instead of this hint. What follows is flagged only when it does NOT look
    like the start of a real statement (a bare/quoted key `=`, or a `[table]`
    header) -- a legitimate, unrelated `'''` block followed by real content
    is left alone, so the scan keeps walking to the next opener instead of
    misattributing a failure that lives elsewhere in the payload.

    Returns the offset of the closing run rather than a bool so the message can
    name the payload line, the one coordinate the author can act on without
    re-reading their own draft — the same argument as `_dbs_occurrences`.

    An unterminated block is deliberately NOT reported here. It is a different
    failure with its own message (`unterminated ''' for the key at offset N`,
    #896 -- no longer echoing the key text itself), and the parity trigger
    kept below still covers the shape #394 shipped for.
    """
    at = 0
    while True:
        m = _TOML_LITERAL_OPENER.search(raw, at)
        if not m:
            return -1
        _end, run, nxt = _toml_multiline_close(raw, m.end(), chr(39), False)
        if run < 0:
            return -1
        stop = raw.find(chr(10), nxt)
        rest = (raw[nxt:] if stop < 0 else raw[nxt:stop]).strip()
        if rest and not rest.startswith("#"):
            return run
        # Self-review (CI) finding, #2545: the cross-line walk below must NOT
        # fire when the closing run sits ALONE on its own line -- that is the
        # idiomatic way to end a legitimate multi-line block, and firing there
        # means "some later, unrelated content in the payload does not parse"
        # gets blamed on THIS closer with a specific (wrong) line/column.
        # Reproduced against a payload with two entirely well-formed blocks
        # followed by one unrelated bad line: both closers sit alone on their
        # own line, and the real cause is neither of them.
        #
        # A closing run with real content BEFORE it on the same line (prose
        # ending a sentence with the delimiter, `line two ends with a run '''`)
        # has no such legitimate reading -- a literal block's closer is never
        # idiomatically preceded by prose on its own line, so this is the
        # signal that distinguishes "the run embedded in prose closed this
        # block early" from "this block closed exactly where intended, and
        # the payload breaks somewhere else entirely".
        line_start = raw.rfind(chr(10), 0, run) + 1
        before = raw[line_start:run]
        if before.strip():
            beyond = _toml_skip_blank_and_comments(raw, nxt)
            if beyond < len(raw) and not _TOML_NEXT_STATEMENT.match(raw, beyond):
                return run
        at = nxt


def _toml_delimiter_hint(raw: str) -> str:
    """Explain a TOML parse failure caused by ''' inside a ''' block (#394).

    The parse error points at a column in the payload, which is where the
    delimiter closed — not at the ''' in the content that closed it.

    **Parity is not the test.** #394 fired only on an odd number of ''' runs,
    on the reasoning that every block opens and closes, so a stray one means
    the content carried its own. That reasoning is sound and the check built
    from it is not: a run *inside a value* breaks the parse at any count, and
    the even case is the likely one for exactly the payloads most in need of
    the hint — a document that quotes the literal-block syntax, once in `old`
    and once in `new`. Hit twice in one agent run (#1830), and what the caller
    got was a bare column number with no mention of the delimiter at all.

    So the structural check leads and the parity one is kept behind it, because
    the two catch different shapes: an unterminated block is odd and has no
    early close to find.

    Silent when the payload has no ''' at all — then the failure is ordinary
    TOML and a delimiter lecture would be noise. Silent too when no ''' ever
    opens a value: `new = "isn't it''' odd"` carries the run harmlessly inside
    a basic string, and a delimiter lecture there sends the reader after the
    wrong cause, which is worse than saying nothing at all. Silent, likewise,
    on `old = '''x'''  # note`: a trailing comment is valid TOML, so trailing
    text is a finding only when it is not one.
    """
    if not _TOML_LITERAL_OPENER.search(raw):
        return ""
    escapes = (
        "    End the header with FIELD = @rest and put the content after it, "
        "unparsed (#1868) -- the only fix that needs no re-encoding of what "
        "you already wrote.\n"
        '    Or use a \"\"\"basic\"\"\" block instead (escapes apply, so \\ doubles), '
        "or the JSON payload form,\n"
        "    which needs no delimiter: {\"path\": ..., \"old\": ..., \"new\": ...}\n"
    )
    at = _toml_delimiter_early_close(raw)
    if at >= 0:
        line = raw.count(chr(10), 0, at) + 1
        col = at - (raw.rfind(chr(10), 0, at) + 1) + 1
        return (
            f"\n  {mark('↳')} a ''' run inside a value closed the block early — payload "
            f"line {line}, column {col}.\n"
            "    Everything after it on that line is then read as TOML syntax, which is "
            "where the parse error above points.\n"
            "    An EVEN number of ''' runs breaks this way too, so the count settles "
            "nothing (#1830).\n"
            + escapes
        )
    if raw.count("'''") % 2 == 0:
        return ""
    return (
        f"\n  {mark('↳')} the payload has an odd number of ''' runs — content containing "
        "''' closes the block early.\n"
        + escapes
    )


# Where a relative `@payload` reference resolves from, and where the working
# directory was moved to. Both are set by main() before any chdir, and only
# when a chdir actually happens — so dispatch() called on its own (MCP mode,
# tests) keeps resolving against os.getcwd() exactly as it always did.
_INVOCATION_DIR: Optional[str] = None
_CWD_SHIFT: Optional[str] = None       # label of what moved the cwd, e.g. "cwd:"


def _at_root() -> str:
    """Directory a relative `@payload` reference resolves against.

    A `@reference` is an argument the caller typed, so it belongs to the
    directory the call was made from — not to the repo being operated on.
    `path = ` *inside* the payload is the other kind of path and keeps
    following the working directory, which is what makes `cwd:` useful.
    """
    return _INVOCATION_DIR or os.getcwd()


def _resolve_at_path(rel: str) -> str:
    """Absolute path for a relative `@payload` reference. No existence check.

    Accepted risk, documented rather than gated (#896 F2): the reference
    ITSELF -- the string after `@` -- is never passed through
    `_containment_error`/`_safe_path`. `grep:@/tmp/outside.toml` loads and
    parses a file outside the project root. Verified NOT a content-disclosure
    channel, so this is intentionally a documentation fix rather than a code
    fix: every field read out of that file is either re-gated on its own (a
    `path`/`paths` field goes back through `_containment_error` like any
    other op argument) or, if it is not path-shaped, never leaves this
    process as content -- `_load_at_file_raw`'s TOML/JSON parse errors report
    only a line/column position, never the text that failed to parse. What
    an attacker-chosen `@` reference outside the root actually buys is an
    existence-and-parseability oracle for paths outside cwd (does a file
    exist there, is it valid TOML/JSON) -- low value, and gating it would
    mean containment-checking the CLI argument that NAMES a payload before
    the payload's own fields are even read, a second boundary next to the
    one `_containment_error` already owns for op arguments.

    That "position only, never text" claim was FALSE on Python <3.11 until a
    CI run on exactly those two legs (3.9, 3.10) caught it: `_mini_toml_loads`
    (the only TOML reader when stdlib `tomllib` is absent) echoed the parsed
    key -- often the whole malformed file, since a bare TOML key is any run
    of `[A-Za-z0-9_-]` characters -- straight into its own error messages.
    Fixed in `_toml_decode_escape`/`_toml_parse_value`/`_mini_toml_loads`
    themselves (position-only there too now); this paragraph is left in
    place, corrected, as the record that the claim was verified against the
    wrong parser the first time.
    """
    if os.path.isabs(rel):
        return rel
    return os.path.join(_at_root(), rel)


def _at_file_missing_msg(rel: str) -> str:
    """Error for an unresolvable `@payload`, distinguishing absence from a moved root.

    "not found" states an absence in the world. When the working directory has
    moved out from under a relative reference, the absence is one the tool
    produced, and saying so is the difference between a zero-call debug and a
    two-call one. Both roots are named; neither is silently searched — reading
    whichever file happens to exist is how a tool starts opening one the caller
    never meant (#672).
    """
    root = _at_root()
    here = os.getcwd()
    head = f"@file not found: {rel}"
    if os.path.isabs(rel) or os.path.realpath(root) == os.path.realpath(here):
        return head
    label = _CWD_SHIFT or "cwd:"
    alt = os.path.join(here, rel)
    lines = [
        head,
        f"  {mark('↳')} @payload paths resolve against the invocation directory: {root}",
    ]
    if os.path.isfile(alt):
        lines.append(
            f"    It does exist under the {label} target {here}, and is not read from there: "
            "the @reference is an argument you typed, not repo content. Only `path =` inside "
            "the payload follows the working directory."
        )
        lines.append(f"    Pass an absolute path (@{alt}), or write the payload next to the call.")
    else:
        lines.append(
            f"    The {label} target is {here} — moving the working directory does not move "
            "the @reference."
        )
        lines.append("    Present under neither directory: check the path, or pass an absolute one.")
    return chr(10).join(lines)


# How much of the offending block the refusal echoes back, so the suggested
# spelling is recognisably the caller's own line and not a generic example.
_TOML_LITERAL_TAIL_CHARS = 48


def _toml_literal_backslash_message(head: str, run: int) -> str:
    """Name both spellings the refused backslash could have meant (#834)."""
    q = "'"
    tail = head[-_TOML_LITERAL_TAIL_CHARS:]
    lead = "…" if len(head) > len(tail) else ""
    want = run if run > 3 else 4
    quoted = q * 3 + lead + tail + q * want
    basic = chr(34) * 3 + lead + tail + chr(92) * 2 + chr(34) * 3
    arrow = mark("↳")
    return (
        "a " + q * 3 + " literal block ends with a backslash immediately before "
        "its closing quotes. Inside a literal block a backslash is content, "
        "never an escape, so it is written to the file as typed and the line "
        "that reaches the compiler is not the one you meant." + chr(10)
        + "  " + arrow + " to end the content with a quote, drop the backslash "
        "and let the closing run carry it (a literal block may end with 1 or 2 "
        + q + "):" + chr(10) + "      " + quoted + chr(10)
        + "  " + arrow + " to end the content with a backslash, write the block "
        "as a basic one, where it doubles:" + chr(10) + "      " + basic + chr(10)
    )


def _toml_literal_backslash_refusal(raw: str) -> str:
    """Refuse a payload whose literal block ends with an inert backslash (#834).

    The issue was filed as "a string ending in an apostrophe writes broken
    code", and that premise is wrong: a literal block ends with an apostrophe
    perfectly well, by letting the closing run carry it. Refusing on a trailing
    quote would refuse the correct spelling -- the check would fire on its own
    fix.

    What actually broke the reported write is the backslash before the closer.
    The caller typed it out of escape reflex; in a literal block it is inert,
    so the value ended with a stray backslash and the Python written from it
    parsed as something else. The op reported success, the validators agreed,
    and the failure surfaced a language away from its cause.

    This refuses rather than warns for one reason, and it is the reason that
    decides the severity of every guard in this family: **both** readings of
    that backslash have another spelling -- drop it, or move to a basic block
    where it doubles -- so refusing leaves nothing unwritable. A guard whose
    refusal would strand a legitimate intent has to warn instead; see
    `docs/validators.md`, "Declining instead of guessing".
    """
    opener = chr(39) * 3
    i = raw.find(opener)
    while i >= 0:
        _end, run, nxt = _toml_multiline_close(raw, i + 3, chr(39), False)
        if run < 0:
            return ""
        if raw[run - 1:run] == chr(92):
            return _toml_literal_backslash_message(raw[i + 3:run - 1], nxt - run)
        i = raw.find(opener, nxt)
    return ""


# How many fields one double-backslash note names before it stops counting.
# A note that lists nine fields is a wall nobody finishes; the first few locate
# the mistake, and the total is carried in the count.
_PAYLOAD_DBS_MAX_FIELDS = 3

# How many OCCURRENCES inside one field get a located block (#1814). The old
# render stopped at the first, so a field with three offending lines was refused
# three times and each refusal cost a full re-send of the payload -- measured at
# four re-sends in one agent run, one on a 14 KB test file and one on a 6 KB
# pull-request payload. Everything needed to report all of them was already
# parsed at the moment it reported one.
#
# Bounded rather than unbounded because a block is three lines, and a payload
# that legitimately carries forty pairs would otherwise answer with a wall. What
# is beyond the cap is NAMED by payload line, never merely counted -- #1087's
# rule one level down: `and 3 further occurrences` withholds exactly the fact
# that sends the reader back to re-derive it by hand.
_PAYLOAD_DBS_MAX_OCCURRENCES = 4

# Characters of the caller's own line kept either side of the pair. The old
# excerpt was the HEAD of the line, 48 characters, which on the reported 9 KB
# payload did not contain the offending bytes at all: a shell printf format is
# frequently 200 characters into a long line (#1808).
_PAYLOAD_DBS_CONTEXT = 22

# How much of one run is DRAWN, once the rule reaches arbitrary even lengths
# (#1860). The run length is named in words on the same line, so the drawing is
# there to locate it, not to count it: a 400-backslash run drew a 400-character
# excerpt and a 400-caret row under it, twice per occurrence, four occurrences
# per field -- a 2 KB receipt for one finding. Past this many the excerpt is cut
# and carries its own trailing ellipsis.
_PAYLOAD_DBS_MAX_RUN_DRAWN = 20

# A maximal run of backslashes whose length is EVEN. Odd runs are still not
# matched, and that is the whole of the rule.
#
# This matched exactly two until #1860, on the ground that "three or four were
# counted, not produced by escape reflex". Four was, twice, and the mechanism
# the original rationale could not have had is that THIS REFUSAL MANUFACTURES
# IT: four is what a caller writes immediately after reading the two-backslash
# refusal and doubling again to escape the escape -- the moment they are
# thinking hardest about backslashes. The guard's own premise ("each pair would
# reach disk as TWO backslashes, pass every validator, and be wrong only in
# string contents") is true of four exactly as it is of two, so the exemption
# was the guard declining to apply its own stated reason.
#
# Evenness rather than length, because an odd run is not something doubling can
# produce: doubling one gives two, doubling two gives four. A lone backslash,
# and the three-run of a regex, still write unrefused -- without that the
# payload route becomes unusable for the regex and Windows-path cases the
# remedy sanctions.
_EVEN_BACKSLASH_RUN = re.compile(r"(?<!\\)(?:\\\\)+(?!\\)")

# The bare key immediately preceding a value, read backwards off the source.
_PAYLOAD_KEY_BEFORE_VALUE = re.compile(r"([A-Za-z0-9_.\-]+)[ \t]*=[ \t]*$")

# Notes raised while a payload was parsed, drained by dispatch at depth 1.
# A `batch:@file` parses its payload ONCE, in the outer frame, before any
# sub-op runs -- draining per sub-op would file the note inside an unrelated
# op's receipt, which is the wrong place for the one line that says a write
# may not be what its author wrote.
_PAYLOAD_WARNINGS: List[str] = []


# A `[[ops]]` table header, at the start of a line. Counted before a finding's
# offset to name WHICH op a field belongs to (#1087): a batch of six that
# reported a bare `new` cost the reader a hand re-derivation of which op it
# meant -- the one fact the scanner already had.
_TOML_OPS_HEADER = re.compile(r"(?m)^[ \t]*\[\[[ \t]*ops[ \t]*\]\]")


def _dbs_occurrences(raw: str, offset: int,
                     content: str) -> List[Tuple[int, int, str, Optional[int], int]]:
    r"""Locate every EVEN backslash run in one block: `(line, column, excerpt, caret, run)`.

    `line` and `column` are 1-based positions in the SUBMITTED PAYLOAD, not in
    the value -- that is the coordinate the author can act on without re-reading
    their own draft, and the one all three issues asked for (#1808, #1819). They
    are measured on `raw` by counting `chr(10)`, which is what a text editor
    numbers by; the ten-separator question (#886) belongs to the excerpt, below,
    and deliberately not to the numbering, where a U+2028 would produce a line
    number no editor agrees with.

    `excerpt` is centred on the pair rather than taken from the head of the
    line. The old render showed the first 48 characters of whatever line the
    first hit sat on, and on the reported payload the offending bytes were not
    in it (#1808).

    `caret` is the offset into `excerpt` where the pair starts, or **None** when
    the excerpt had to be flattened and the offset can no longer be trusted.
    That third state is the point: `_flat_field` may `repr()` its argument
    (#886), which is not offset-preserving, so a caret computed before
    flattening would point confidently at the wrong character. A caret that
    cannot be placed says so and leaves the exact line and column standing --
    silently omitting it, or drawing it anyway, are the two failures this
    repository is named after.
    """
    out: List[Tuple[int, int, str, Optional[int], int]] = []
    for m in _EVEN_BACKSLASH_RUN.finditer(content):
        at_abs = offset + m.start()
        # The RUN LENGTH, carried rather than assumed to be two (#1860). Once
        # the scan reaches four and six, a caret two characters wide under a
        # run of four points at half of it and the header's `\\` names a pair
        # that is not there -- sending the reader to look for the wrong thing
        # in a receipt whose whole job is to say where to look.
        run = m.end() - m.start()
        line_no = raw.count(chr(10), 0, at_abs) + 1
        col = at_abs - (raw.rfind(chr(10), 0, at_abs) + 1) + 1
        start = content.rfind(chr(10), 0, m.start()) + 1
        stop = content.find(chr(10), m.start())
        line = content[start:] if stop < 0 else content[start:stop]
        at = m.start() - start
        lo = max(0, at - _PAYLOAD_DBS_CONTEXT)
        drawn = min(run, _PAYLOAD_DBS_MAX_RUN_DRAWN)
        # Trailing context only when the WHOLE run was drawn. Where it was cut,
        # the characters after the cut are the rest of the run, so "context"
        # would be 22 more backslashes -- the thing the cap exists to stop.
        after = _PAYLOAD_DBS_CONTEXT if drawn == run else 0
        hi = min(len(line), at + drawn + after)
        # ASCII elision on purpose. `mark()` passes an unmapped glyph straight
        # through, so plain mode is not a guarantee, and a cp1252 console raises
        # UnicodeEncodeError at the print -- killing the process at the refusal
        # rather than at the work the refusal was about.
        lead = "..." if lo > 0 else ""
        excerpt = lead + line[lo:hi] + ("..." if hi < len(line) else "")
        caret: Optional[int] = len(lead) + (at - lo)
        flat = _flat_field(excerpt)
        if flat != excerpt:
            excerpt, caret = flat, None
        out.append((line_no, col, excerpt, caret, run))
    return out


def _dbs_occurrence_block(label: str,
                          occs: List[Tuple[int, int, str, Optional[int], int]],
                          total: int) -> str:
    """Render one field's located occurrences (#1808, #1814, #1819)."""
    arrow = mark(chr(8627))
    shown = occs[:_PAYLOAD_DBS_MAX_OCCURRENCES]
    rest = occs[_PAYLOAD_DBS_MAX_OCCURRENCES:]
    lines = [
        "  " + arrow + " `" + label + "` -- " + str(total)
        + " even backslash run" + ("" if total == 1 else "s")
        + (", first " + str(len(shown)) + " shown:" if rest else ", all shown:")
        + chr(10)
    ]
    for n, (line_no, col, excerpt, caret, run) in enumerate(shown, 1):
        lines.append(
            "      " + str(n) + "/" + str(total) + " at payload line "
            + str(line_no) + ", column " + str(col) + " -- a run of "
            + str(run) + ":" + chr(10)
            + "          " + excerpt + chr(10)
        )
        lines.append(
            # The caret row is as wide as what was DRAWN, never as wide as the
            # run: past _PAYLOAD_DBS_MAX_RUN_DRAWN the excerpt is cut and ends
            # in its own ellipsis, and a caret row wider than the text above it
            # points at nothing. The true length is in words on the line above.
            "          " + " " * caret
            + chr(94) * min(run, _PAYLOAD_DBS_MAX_RUN_DRAWN) + chr(10)
            if caret is not None
            else "          (no caret -- the excerpt carried a line separator or "
            "an unprintable and was flattened, so an offset into it would point "
            "at the wrong character. The payload line and column above are "
            "measured on the raw payload and are exact.)" + chr(10)
        )
    if rest:
        lines.append(
            "      ... and " + str(len(rest)) + " more, at payload line"
            + ("" if len(rest) == 1 else "s") + " "
            + ", ".join(str(o[0]) for o in rest) + chr(10)
        )
    return "".join(lines)


def _toml_literal_double_backslashes(
        raw: str) -> List[Tuple[str, str, str, int,
                          List[Tuple[int, int, str, Optional[int], int]]]]:
    r"""`(key, label, first line, count, occurrences)` per literal block holding
    an EVEN backslash run.

    Provenance is the whole point and it is read off the source, not the parsed
    value: in a basic block `\\` IS one backslash and is the correct spelling,
    so a guard that could not tell the two blocks apart would fire on its own
    remedy. Scanning the literal blocks in `raw` directly answers the question
    exactly rather than heuristically.

    `key` is the bare field name, which is what decides whether the pair is
    write-bound. `label` is what a human is shown -- `ops[2].new` inside a
    batch, and the bare key outside one.
    """
    findings: List[Tuple[str, str, str, int]] = []
    opener = chr(39) * 3
    i = raw.find(opener)
    while i >= 0:
        end, run, nxt = _toml_multiline_close(raw, i + 3, chr(39), False)
        if run < 0:
            break
        content = raw[i + 3:end]
        key_m = _PAYLOAD_KEY_BEFORE_VALUE.search(raw[:i])
        key = key_m.group(1) if key_m else "?"
        hit = _EVEN_BACKSLASH_RUN.search(content)
        if hit and key.lower() != "op":
            total = len(_EVEN_BACKSLASH_RUN.findall(content))
            start = content.rfind(chr(10), 0, hit.start()) + 1
            stop = content.find(chr(10), hit.start())
            line = content[start:] if stop < 0 else content[start:stop]
            seen = len(_TOML_OPS_HEADER.findall(raw[:i]))
            label = key if seen == 0 else "ops[" + str(seen - 1) + "]." + key
            # `_flat_field` on the excerpt, not just the truncation (#1583).
            # `line` was cut on `chr(10)` alone, but this repo's definition of
            # "one line" is `str.splitlines()` — ten separators (#886) — so a
            # U+2028 in the caller's own value survived inside the excerpt and
            # put a line of their choosing at column 0 of the note and of the
            # refusal that render it. Flattened here rather than at the two
            # render sites so the tuple's third element means what its name
            # says everywhere it is read.
            # The fifth element is what #1814 was filed about: the scan had
            # already walked the whole block to produce `total`, and threw
            # every position but the first away. `search` -> `finditer` is the
            # whole of the mechanism; the cost was one re-send of the payload
            # per discarded position.
            findings.append(
                (key, label,
                 _flat_field(line.strip())[:_TOML_LITERAL_TAIL_CHARS], total,
                 _dbs_occurrences(raw, i + 3, content)))
        i = raw.find(opener, nxt)
    return findings


def _payload_double_backslash_note(raw: str) -> str:
    r"""Name a `\\` inside a triple-single-quoted block -- warn, not rewrite (#1027).

    What is left here after #1087 is the half that never reaches disk. A
    doubled `old` cannot match, so the runner reports the skip -- but only
    AFTER the anchor has missed, and only in the language of a failed match.
    This says the same thing one call earlier and in the words that name the
    cause. The half that DID land bytes -- `new`, `content` -- is refused by
    `_payload_double_backslash_refusal` and never reaches this function.

    **It warns, and it does not rewrite.** Collapsing `\\` to `\` would guess at
    intent, and a wrong guess is strictly worse than the bug it replaces: the
    caller loses even the ability to read back what they asked for. The tool's
    job here is to say "you wrote `\\` inside a block that will not process it"
    and leave the decision where it belongs.

    **It warns only for fields that are NOT written** (#1087). The write-bound
    ones -- `_PAYLOAD_DBS_WRITE_KEYS` -- go to
    `_payload_double_backslash_refusal` instead, because the note fired after
    the bytes had landed and every validator passed them.

    The reason this started as a warning was real: #834 and #835 fire at a
    FIXED position, immediately before the closing quotes and at the end of a
    shell line, where every reading has a second spelling, so a refusal strands
    nothing. This pattern has no position, and refusing it with no way out would
    make a payload that legitimately writes a pair unwritable at every offset.
    What changed is that there is now a way out: `literal_backslashes = true`
    (#1096). A suppressible refusal is strictly better than an unsuppressible
    warning -- the suppression is a decision the author records in the payload
    rather than one the tool makes for them.

    What is left here is the half that was always safe: `old` is an anchor, a
    doubled one cannot match, the runner reports the skip, and nothing reaches
    disk. `vim`'s `script` is also left as a note -- it is an instruction
    language, and the tool cannot say from a payload what bytes the file ends
    up holding.

    The line between signal and noise is drawn at two places and nowhere else:

    * **Literal blocks only.** A basic block spells one backslash with two;
      flagging that would flag the fix.
    * **EVEN runs only.** An odd run -- one, three, five -- is not something a
      doubling reflex can produce, and refusing it would make the payload route
      unusable for the regex and Windows-path cases the remedy sanctions. This
      read "runs of exactly two, three or more were counted deliberately" until
      #1860, where four turned out to be what a caller writes right after
      reading the two-backslash refusal.

    It is deliberately NOT narrowed to "escape-looking" sequences. The reported
    cases were `\\d`, `\\302` and `\\n`; `\d` and `\302` are not TOML escapes,
    so the reflex being caught is generic, not TOML-specific, and any escape set
    narrow enough to be a filter would miss the report it was written for.
    Measured instead, and RE-MEASURED when the rule widened from a pair to any
    even run (#1860): 910 of 368801 tracked lines in this repository carry one
    -- 0.25%, of which 104 lines carry a run longer than two, which is the part
    the old rule did not see. This is the pathological corpus, since its densest
    files are tests ABOUT backslash handling. A note at that rate is not one an
    author learns to skip. The earlier figure was 710 of 208854 lines (0.34%)
    for runs of exactly two, against a smaller tree; it is quoted here because a
    rate that moved is worth more than a rate that was replaced.
    """
    findings = [f for f in _toml_literal_double_backslashes(raw)
                if f[0].lower() not in _PAYLOAD_DBS_WRITE_KEYS]
    if not findings:
        return ""
    bs = chr(92)
    arrow = mark("↳")
    lines = [
        mark("⚠") + " payload: a " + chr(39) * 3 + " literal block carries an "
        "EVEN run of backslashes (" + bs * 2 + ", " + bs * 4 + ", ...). A literal "
        "block processes NO escapes, so the run reaches the file at its full "
        "length -- if you meant half of it, write half." + chr(10)
    ]
    for _key, label, line, total, occs in findings[:_PAYLOAD_DBS_MAX_FIELDS]:
        # The payload line number, not the located block. `old` is an anchor:
        # the pair cannot match, the runner reports the skip, and nothing lands,
        # so the reader needs to FIND the pair, not to be argued out of it --
        # the two-directions block belongs to the refusal, where a write was
        # stopped. The line number is the same fact both wanted (#1819).
        at = (", first at payload line " + str(occs[0][0])) if occs else ""
        lines.append(
            "  " + arrow + " `" + label + "` (" + str(total) + " even backslash run"
            + ("" if total == 1 else "s") + at + "): " + line + chr(10)
        )
    rest = findings[_PAYLOAD_DBS_MAX_FIELDS:]
    if rest:
        # Named, not counted (#1087). `and 1 further field` withholds exactly
        # the identifier the reader needs and sends them back to re-derive by
        # hand which of six ops it meant -- a warning that costs a manual
        # reconstruction is close to no warning at all.
        lines.append(
            "  " + arrow + " and " + str(len(rest)) + " more: "
            + ", ".join("`" + f[1] + "`" for f in rest) + chr(10)
        )
    lines.append(
        "  " + arrow + " this is a note, NOT a correction -- nothing was "
        "rewritten, because a pair is sometimes exactly what was meant and "
        "guessing here is worse than the bug. None of the fields above is "
        "written to a file: a doubled `old` cannot match and is reported as a "
        "skip. The write-bound fields (" + ", ".join(sorted(_PAYLOAD_DBS_WRITE_KEYS))
        + ") are refused instead. (#1027, #1087)" + chr(10)
    )
    return "".join(lines)


# Fields whose value lands verbatim, permanently, as bytes someone reads later.
# A doubled backslash here is the only half of #1027 that reaches that state --
# `old` is an anchor that cannot match, and `vim`'s `script` is an instruction
# language where the tool cannot say what the file ends up holding, so neither
# is refused.
#
# `message` joined this set for #1249's item 3, on the same reasoning one step
# removed from a file: `git-commit`'s refusal machinery was write-BOUND, and a
# commit message is not a file, so the doubling warned and the commit
# proceeded -- and the recorded consequence was a commit message about
# backslash misreporting that itself misreported backslashes, fixed afterwards
# with a raw `git` amend outside the payload route entirely. A commit message
# has no anchor reading, so there is no `old`-shaped note-only case to keep for
# it the way there is for `new`/`content`.
#
# Kept as a set rather than derived from the @file registry: the registry knows
# a field's POSITION, not whether its bytes are written, and a rule that
# refused every non-`old` field would refuse `path` -- where a Windows payload
# spells a separator with exactly two backslashes and is correct.
_PAYLOAD_DBS_WRITE_KEYS = frozenset({"new", "content", "message"})

# The one key that says "I meant two characters" (#1096).
_PAYLOAD_LITERAL_BS_KEY = "literal_backslashes"


def _payload_literal_backslashes_scope(parsed: Any) -> Union[bool, FrozenSet[str]]:
    """What `literal_backslashes` exempts from the doubled-backslash refusal (#1096, #1839).

    Top level only, in either of two shapes. `= true` exempts every field in
    the payload. A list of field NAMES -- `= ["new"]` -- exempts only the
    fields it names, everywhere their bare key occurs.

    Neither shape locates an `[[ops]]` boundary, which is what #1096's
    original reasoning turned on: a key placed INSIDE an ops table reads as
    scoped to that op and could not be, because the detector works off the
    raw source, where op boundaries are a line-counting heuristic rather than
    a fact. That argument is about POSITION -- a key whose apparent scope was
    a specific op it could not actually be pinned to. Matching on the bare
    field NAME instead needs no such heuristic: the scanner already carries
    that name (`new`, `content`, ...) on every finding it produces, so
    filtering the findings by name is exact, not a claimed scope that could
    turn out to be a lie. That is what #1839 supersedes; the rest of the
    original reasoning still holds and is why the misplaced-key refusal below
    is unchanged.

    What the list form does NOT solve: two ops that both carry a `new` field
    and disagree with EACH OTHER about that field's own intent still share
    one name, and there is still no honest way to tell them apart from the
    raw source. That payload is still two payloads.

    Returns `True` for the whole-payload form, a lowercase frozenset of field
    names for the list form, or an empty frozenset for anything else --
    unset, a bare list of non-strings, or any other malformed value, all of
    which opt in to nothing rather than guessing at an intent that was not
    stated cleanly.
    """
    if not isinstance(parsed, dict):
        return frozenset()
    value = parsed.get(_PAYLOAD_LITERAL_BS_KEY)
    if value is True:
        return True
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return frozenset(v.lower() for v in value)
    return frozenset()


def _payload_literal_backslashes_misplaced(parsed: Any) -> str:
    """Refuse `literal_backslashes` set inside an `[[ops]]` table (#1096, #1720).

    An author who set it there stated an intent, and honouring it at a scope the
    tool cannot implement is not an option -- but neither is ignoring it. A
    silently dropped flag refuses the write while the payload says it was
    allowed, which is this tracker's own defect class: the receipt and the
    payload disagreeing about what was asked for.

    Names EVERY offending index, not just the first (#1720): a templated batch
    is the likely source of this mistake, and a caller who fixes `ops[0]`,
    re-sends, and is then told about `ops[2]` has paid a round-trip the first
    scan already had the answer to.
    """
    if not isinstance(parsed, dict):
        return ""
    ops = parsed.get("ops")
    if not isinstance(ops, list):
        return ""
    idxs = [idx for idx, entry in enumerate(ops)
            if isinstance(entry, dict) and _PAYLOAD_LITERAL_BS_KEY in entry]
    if not idxs:
        return ""
    where = (
        "`ops[" + str(idxs[0]) + "]`" if len(idxs) == 1
        else ", ".join("`ops[" + str(i) + "]`" for i in idxs[:-1])
        + " and `ops[" + str(idxs[-1]) + "]`"
    )
    return (
        "`" + _PAYLOAD_LITERAL_BS_KEY + "` is set inside " + where
        + ", where it does nothing"
        + ". It is read at the TOP LEVEL of the "
        "payload only, and it applies to every op the payload carries -- "
        "the doubled-backslash scan runs once, over the raw source, "
        "before any op does. Move it to the top level if that is what "
        "you meant; split the payload if the ops differ. (#1096, #1720)"
    )


def _literal_bs_field_list_example(
        findings: List[Tuple[str, str, str, int,
                              List[Tuple[int, int, str, Optional[int], int]]]],
        already: FrozenSet[str] = frozenset()) -> str:
    """A ready-to-paste `literal_backslashes = [...]` naming the fields THIS
    refusal flagged, PLUS any the payload had already exempted, deduplicated
    and in first-seen order (#1839).

    Built from the caller's own payload rather than a generic `["new"]`: the
    caller can paste it rather than adapt it, and it never suggests a name
    that is not actually one of the fields in front of them.

    `already` is the payload's OWN prior list-form scope, when it has one. A
    caller who follows the "paste it as-is" instruction is replacing their
    existing `literal_backslashes = [...]` with this suggestion -- if it were
    built from `findings` alone (the fields STILL refused, which by
    definition excludes anything already exempted), pasting it would silently
    drop every exemption the payload had already recorded, and the very next
    submission would refuse a field the caller had already settled. Union,
    not replacement, is what "paste it as-is" has to mean.
    """
    seen: List[str] = [k.lower() for k in already]
    for f in findings:
        key = f[0].lower()
        if key not in seen:
            seen.append(key)
    return "[" + ", ".join(chr(34) + k + chr(34) for k in seen) + "]"


# The shell single-quote escape idiom for embedding an apostrophe inside a
# single-quoted string: close the quote, emit a literal quote another way,
# reopen. Two common spellings -- double-quote a lone quote (`'"'"'`), or
# backslash-escape it (`'\''`). Either resolves to one apostrophe inside a
# SHELL's own quoting. Neither resolves to anything inside a TOML literal
# block -- which is what a payload's `content =` / `new =` field IS -- so the
# whole sequence lands on disk at its full length, the write reports success,
# and every validator agrees, because the sequence is legal text in nearly
# every language this repo edits. (#2114)
_SHELL_QUOTE_ESCAPE_FORMS = (
    "'" + chr(34) + "'" + chr(34) + "'",   # \'"\'"\'
    "'" + chr(92) + "''",                  # \'\\\'\'
)


def _toml_literal_shell_quote_escape_findings(
        raw: str) -> List[Tuple[str, str, str, int, int]]:
    """`(key, label, first line, count, first payload line number)` per literal
    block whose write-bound field carries the shell single-quote escape idiom
    (#2114).

    The fifth element is the position `#2243` asked for -- the sibling
    doubled-backslash refusal already had one (`_dbs_occurrences`, #1808,
    #1814, #1819), and a count alone ("2 occurrences") sends the reader back
    to re-read the whole field by eye to find either one.

    Scoped to write-bound fields only, the same set `_payload_double_backslash_
    refusal` uses and for the same reason: `old` is an anchor and never lands
    a byte, and `vim`'s `script` is an instruction language this scan cannot
    read the outcome of. A basic `\"\"\"` block is out of scope for a
    different reason -- there the quote and double-quote characters are
    ordinary content that the block's own escaping already governs, not a
    payload author reaching for a shell habit inside a block that has no
    escapes to reach for.
    """
    findings: List[Tuple[str, str, str, int, int]] = []
    opener = chr(39) * 3
    i = raw.find(opener)
    while i >= 0:
        end, run, nxt = _toml_multiline_close(raw, i + 3, chr(39), False)
        if run < 0:
            break
        content = raw[i + 3:end]
        key_m = _PAYLOAD_KEY_BEFORE_VALUE.search(raw[:i])
        key = key_m.group(1) if key_m else "?"
        if key.lower() in _PAYLOAD_DBS_WRITE_KEYS:
            positions = [content.find(form) for form in _SHELL_QUOTE_ESCAPE_FORMS
                         if form in content]
            total = sum(content.count(form) for form in _SHELL_QUOTE_ESCAPE_FORMS)
            if positions:
                hit_at = min(positions)
                start = content.rfind(chr(10), 0, hit_at) + 1
                stop = content.find(chr(10), hit_at)
                line = content[start:] if stop < 0 else content[start:stop]
                seen = len(_TOML_OPS_HEADER.findall(raw[:i]))
                label = key if seen == 0 else "ops[" + str(seen - 1) + "]." + key
                # Measured on the SUBMITTED PAYLOAD (`raw`), not on `content` --
                # the same coordinate #1808 settled on for the sibling refusal,
                # because it is the one the author can act on without re-deriving
                # an offset into their own value.
                at_abs = (i + 3) + hit_at
                line_no = raw.count(chr(10), 0, at_abs) + 1
                findings.append(
                    (key, label, _flat_field(line.strip())[:_TOML_LITERAL_TAIL_CHARS],
                     total, line_no))
        i = raw.find(opener, nxt)
    return findings


def _payload_shell_quote_escape_refusal(parsed: Any, raw: str) -> str:
    """Refuse a payload that would WRITE the shell single-quote escape idiom
    verbatim, unless `literal_backslashes` exempts the field (#2243).

    #2114 made this a WARNING, on a real reason at the time: #834's trailing-
    backslash case works as a refusal because BOTH readings have another
    spelling, so refusing strands nothing, and this idiom had no such second
    spelling to offer -- a payload correctly documenting it (`presets/
    _refname.py`'s own comment on `shlex.quote`'s escaping) writes the exact
    same bytes as a payload that meant a plain apostrophe by mistake, and the
    tool cannot tell those apart from the bytes alone. That reasoning was
    right about the ONLY escape hatch that existed at the time: none. It
    stopped holding the moment `_payload_double_backslash_refusal` (#1087,
    #1096) got one -- `literal_backslashes` is not a claim about backslashes
    specifically, it is "this field's odd-looking punctuation is intentional,
    do not second-guess it", and that sentence is exactly as true of this
    idiom. Reusing the one key rather than adding a second means a payload
    that already declared its intent for the backslash case does not have to
    declare it twice for this one.

    Scoped to the fields whose bytes land -- `_PAYLOAD_DBS_WRITE_KEYS`, the
    same set and the same reason `_payload_double_backslash_refusal` uses:
    `old` is an anchor, a doubled idiom there cannot match, and the runner
    reports the skip, so refusing it would cost a round-trip on a call that
    was already safe.

    Never rewrites, for the same reason `_payload_double_backslash_note` does
    not: collapsing the idiom to a bare apostrophe would guess at intent, and
    a wrong guess is strictly worse than the bug it replaces -- the caller
    loses even the ability to read back what they asked for.

    Fires at parse time, before any op runs, the same guarantee #1087 gives
    the doubled-backslash case: nothing has to be rolled back, because nothing
    was written.
    """
    scope = _payload_literal_backslashes_scope(parsed)
    if scope is True:
        return ""
    findings = [f for f in _toml_literal_shell_quote_escape_findings(raw)
                if f[0].lower() not in scope]
    if not findings:
        return ""
    arrow = mark(chr(8627))
    out = [
        "a " + chr(39) * 3 + " literal block carries the shell single-quote "
        "escape idiom (close the quote, emit one another way, reopen) in a "
        "field that is WRITTEN to the file. A literal block processes NO "
        "escapes, so if this was meant as one apostrophe it lands as the "
        "whole sequence instead -- and if it is documentation OF the idiom, "
        "this refusal is what stops the ambiguous bytes from landing "
        "unrecorded. Each occurrence below names where it was found." + chr(10)
    ]
    for _key, label, line, total, line_no in findings[:_PAYLOAD_DBS_MAX_FIELDS]:
        out.append(
            "  " + arrow + " `" + label + "` -- " + str(total)
            + " occurrence" + ("" if total == 1 else "s")
            + ", first at payload line " + str(line_no) + ": " + chr(96)
            + line + chr(96) + chr(10)
        )
    rest = findings[_PAYLOAD_DBS_MAX_FIELDS:]
    if rest:
        out.append(
            "  " + arrow + " and " + str(len(rest)) + " more: "
            + ", ".join("`" + f[1] + "`" for f in rest) + chr(10)
        )
    field_example = _literal_bs_field_list_example(
        [(f[0], f[1], f[2], f[3], []) for f in findings], scope)
    out.append(
        "  " + arrow + " TWO OPPOSITE fixes, and nothing here can tell which "
        "you meant -- decide per occurrence, then send the payload once:" + chr(10)
        + "      meant ONE apostrophe (an escape reflex carried over from "
        "shell quoting -- a literal block eats nothing, so the sequence "
        "stays whole): replace it with a single `'`." + chr(10)
        + "      meant the idiom AS WRITTEN (documentation of the shell "
        "trick itself): add `" + _PAYLOAD_LITERAL_BS_KEY + " = true` at the "
        "top level of the payload to exempt EVERY field, or name only the "
        "field(s) above -- `" + _PAYLOAD_LITERAL_BS_KEY + " = " + field_example
        + "` -- to leave any other field's own occurrences refused. Either "
        "way, this refusal becomes a decision you recorded." + chr(10)
        + "  (#2114, #1087, #1096, #1839, #2243)"
    )
    return "".join(out)


_PAYLOAD_BS_CODE_EXTS = frozenset((
    ".json", ".py", ".js", ".jsx", ".ts", ".tsx", ".jsonc", ".yaml", ".yml"))
_PAYLOAD_BS_PROSE_EXTS = frozenset((".md", ".txt", ".rst", ".adoc"))


def _payload_target_paths(parsed: Any) -> List[str]:
    """Every `path` string this payload writes to -- the top-level field for
    a single-op payload, or one per `[[ops]]` entry for a batch.
    """
    paths: List[str] = []
    if not isinstance(parsed, dict):
        return paths
    top = parsed.get("path")
    if isinstance(top, str):
        paths.append(top)
    ops = parsed.get("ops")
    if isinstance(ops, list):
        for entry in ops:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                paths.append(entry["path"])
    return paths


def _payload_extension_bs_hint(parsed: Any) -> str:
    """One extra line naming which of the two fixes below is the common case
    for THIS payload's target (#1794).

    Additive only: never changes whether the refusal fires, only nudges the
    reader toward the fix they are more likely to want. The refusal asks the
    same question ("half the run, or the run as written?") of a payload
    writing `.json`/`.py` -- where a doubled backslash is very often a REAL
    one, a JSON string escape or a Python string-literal escape landing
    correctly -- and of a payload writing prose, where it almost never is.
    Silent (returns "") when the targets are mixed or the extension is not
    one this repo has an opinion about -- a wrong nudge costs more than none,
    and this is a hint, not a verdict the refusal itself is not making.
    """
    exts = {os.path.splitext(p)[1].lower() for p in _payload_target_paths(parsed)}
    exts.discard("")
    if not exts:
        return ""
    which = ", ".join(sorted(exts))
    if exts <= _PAYLOAD_BS_CODE_EXTS:
        return (
            "  " + mark(chr(8627)) + " the target here is " + which + " -- a "
            "doubled backslash there is often a REAL one (a JSON string "
            "escape, a Python string-literal escape) rather than an escape "
            "reflex: the SECOND fix below is the common case for this "
            "extension, not the first." + chr(10)
        )
    if exts <= _PAYLOAD_BS_PROSE_EXTS:
        return (
            "  " + mark(chr(8627)) + " the target here is " + which + " -- "
            "prose rarely wants a literal doubled backslash: the FIRST fix "
            "below (write half) is the common case for this extension, not "
            "the second." + chr(10)
        )
    return ""


_PAYLOAD_ADVISORY_OPS_LABEL = re.compile(r"^ops\[(\d+)\]\.")


def _payload_target_path_for_label(parsed: Any, label: str) -> str:
    """The `path` this payload writes TO, for one field's own label (#2493).

    `label` is what `_toml_literal_double_backslashes` already names each
    finding with -- the bare key outside a batch, `ops[N].key` inside one.
    Reused rather than re-derived: a second reading of the same raw source
    could disagree with the first about which `[[ops]]` entry a field
    belongs to, and that disagreement would misattribute the advisory to
    the wrong file.
    """
    if not isinstance(parsed, dict):
        return ""
    m = _PAYLOAD_ADVISORY_OPS_LABEL.match(label)
    if not m:
        top = parsed.get("path")
        return top if isinstance(top, str) else ""
    ops = parsed.get("ops")
    if not isinstance(ops, list):
        return ""
    idx = int(m.group(1))
    if idx >= len(ops) or not isinstance(ops[idx], dict):
        return ""
    entry_path = ops[idx].get("path")
    return entry_path if isinstance(entry_path, str) else ""


def _payload_py_doubled_backslash_advisory(parsed: Any, raw: str) -> Dict[str, str]:
    r"""A `.py`-only, non-blocking advisory for a doubled-backslash escape
    sequence that `literal_backslashes` already exempted from the write
    refusal (#2493).

    `_payload_double_backslash_refusal` blocks this shape unconditionally for
    every write-bound field UNLESS the payload's own author exempted it -- so
    by the time a doubled run reaches `_atomic_write`, the author already
    said "this is intentional". The residual risk `literal_backslashes`
    cannot close: the exemption is FIELD-scoped, not occurrence-scoped, so
    one legitimate `\\d` regex or `\\\\` Windows path in a `content` field
    exempts every OTHER doubled run in that same field too, including a
    genuine `\\n`-for-`\n` typo sitting a few lines away in the same block.
    `py-syntax` structurally cannot see this: `"a\\nb"` and `"a\nb"` are both
    syntactically valid Python with different runtime values, and it only
    ever answers "does this parse" (#2493's own report).

    Scoped exactly as narrowly as the residual risk: only `.py` targets
    (where the ambiguity is a string-literal escape, not prose), and only
    occurrences the payload already exempted -- an occurrence the refusal is
    STILL blocking never reaches disk at all, so there is nothing here to
    advise about. Reuses `_toml_literal_double_backslashes`, the SAME scan
    the refusal itself runs, so this can never disagree with it about what
    counts as a doubled run or where a literal block starts and ends.

    Never a finding, never a rewrite: a decline-to-guess note appended to the
    RECEIPT (via `_PAYLOAD_PY_ESCAPE_ADVISORY`, consumed by `_atomic_write`),
    not to `py-syntax`'s own verdict, which stays `ok` because it is
    correctly answering a different question.
    """
    scope = _payload_literal_backslashes_scope(parsed)
    if not scope:
        return {}
    by_path: Dict[str, List[Tuple[str, int, int, str, int]]] = {}
    for key, label, _line, _total, occs in _toml_literal_double_backslashes(raw):
        lk = key.lower()
        if lk not in _PAYLOAD_DBS_WRITE_KEYS:
            continue
        if not (scope is True or lk in scope):
            continue  # still refused -- never reaches disk
        target = _payload_target_path_for_label(parsed, label)
        if not target or os.path.splitext(target)[1].lower() != ".py":
            continue
        by_path.setdefault(os.path.abspath(target), []).extend(
            (label, line_no, col, excerpt, run)
            for line_no, col, excerpt, caret, run in occs)
    if not by_path:
        return {}
    arrow = mark(chr(8627))
    out: Dict[str, str] = {}
    for abs_path, items in by_path.items():
        lines = [
            mark("ℹ") + " " + str(len(items)) + " doubled-backslash "
            "escape sequence" + ("" if len(items) == 1 else "s") + " in this "
            "write " + ("was" if len(items) == 1 else "were") + " exempted "
            "from the payload refusal via `" + _PAYLOAD_LITERAL_BS_KEY
            + "` -- the exemption covers the WHOLE field, so a genuine "
            "doubling mistake sitting among legitimate ones would look "
            "identical to this. This may be a doubled escape rather than an "
            "intended literal backslash; not corrected automatically. "
            "(#2493)" + chr(10)
        ]
        for label, line_no, col, excerpt, run in items[:_PAYLOAD_DBS_MAX_OCCURRENCES]:
            lines.append(
                "  " + arrow + " `" + label + "` at payload line "
                + str(line_no) + ", column " + str(col) + " (run of "
                + str(run) + "): " + excerpt + chr(10)
            )
        rest = len(items) - _PAYLOAD_DBS_MAX_OCCURRENCES
        if rest > 0:
            lines.append("  " + arrow + " and " + str(rest) + " more" + chr(10))
        out[abs_path] = "".join(lines)
    return out


def _payload_double_backslash_refusal(parsed: Any, raw: str) -> str:
    """Refuse a payload that would WRITE a doubled backslash (#1087).

    #1027 made this a note and gave a good reason: the pattern has no fixed
    position, so refusing it would make a payload that legitimately writes two
    characters unwritable at every offset -- the loud-for-quiet trade this repo
    rules out by name. The reason held only while there was no way to say "I
    meant two". `literal_backslashes` (#1096) is that way, so the refusal is
    suppressible, and a suppressible refusal is strictly better than an
    unsuppressible warning: the suppression is a decision the author records in
    the payload rather than one the tool makes on their behalf.

    Scoped to the fields whose bytes land. `old` keeps the note -- a doubled
    anchor cannot match, the runner reports the skip, and nothing reaches disk,
    so refusing it would cost a round-trip on a call that was already safe.

    Fires at parse time, before any op runs. That is why a `batch` is covered
    by construction and why nothing has to be rolled back: on the reported
    `paste` the file was created, every validator passed (two backslashes are
    legal in every language this repo edits), and the author found out from
    behaviour a CI round later.
    """
    scope = _payload_literal_backslashes_scope(parsed)
    if scope is True:
        return ""
    findings = [f for f in _toml_literal_double_backslashes(raw)
                if f[0].lower() in _PAYLOAD_DBS_WRITE_KEYS
                and f[0].lower() not in scope]
    if not findings:
        return ""
    bs = chr(92)
    arrow = mark(chr(8627))
    out = [
        "a " + chr(39) * 3 + " literal block carries an EVEN run of backslashes "
        "(" + bs * 2 + ", " + bs * 4 + ", ...) in a field that is WRITTEN to the "
        "file, and a literal block processes NO escapes -- the run would reach "
        "disk at its full length, pass every validator, and be wrong only in "
        "string contents. Each occurrence below names the run it found -- if "
        "you already know this run is meant AS WRITTEN (documentation of the "
        "sequence itself, not a doubling mistake), skip straight to `"
        + _PAYLOAD_LITERAL_BS_KEY + "` at the end of this message. (#2314)"
        + chr(10)
    ]
    for _key, label, _line, total, occs in findings[:_PAYLOAD_DBS_MAX_FIELDS]:
        out.append(_dbs_occurrence_block(label, occs, total))
    # Every remaining field is still NAMED (#1087) -- only its located block is
    # dropped. A located block is three lines per occurrence, so a nine-op batch
    # would otherwise answer with a wall; but `and 6 further fields` withholds
    # exactly the identifier that sends the reader back to re-derive by hand
    # which of nine ops was meant, which is close to not reporting them at all.
    for _key, label, _line, total, occs in findings[_PAYLOAD_DBS_MAX_FIELDS:]:
        # The line list is capped by the same constant as a located block, and
        # for the same reason. Uncapped, a field with 93 pairs -- the count in
        # #1808's own report -- put 93 numbers on one line, which is the wall
        # this render was bounded to avoid, reintroduced one branch over.
        shown = ", ".join(str(o[0]) for o in occs[:_PAYLOAD_DBS_MAX_OCCURRENCES])
        over = len(occs) - _PAYLOAD_DBS_MAX_OCCURRENCES
        out.append(
            "  " + arrow + " `" + label + "` -- " + str(total) + " even backslash run"
            + ("" if total == 1 else "s") + (
                ", at payload line" + ("" if len(occs) == 1 else "s") + " "
                + shown + (" and " + str(over) + " more" if over > 0 else "")
                if occs else ""
            ) + chr(10)
        )
    field_example = _literal_bs_field_list_example(findings, scope)
    ext_hint = _payload_extension_bs_hint(parsed)
    if ext_hint:
        out.append(ext_hint)
    out.append(
        "  " + arrow + " or end the header with FIELD = @rest and put the run "
        "after it, unparsed -- the tail is never scanned for this pattern, so "
        "there is nothing to decide (#1868)." + chr(10)
    )
    out.append(
        "  " + arrow + " TWO OPPOSITE fixes, and nothing here can tell which you "
        "meant -- decide per occurrence, then send the payload once:" + chr(10)
        + "      meant HALF the run (escape reflex -- a literal block eats "
        "nothing, so a doubled 1 arrives as 2 and a doubled 2 as 4): write "
        "half of what is caretted above." + chr(10)
        + "      meant the run AS WRITTEN (a shell printf format, a Windows "
        "path, a LaTeX line break): add `" + _PAYLOAD_LITERAL_BS_KEY + " = true` at the top level "
        "of the payload to exempt EVERY field, or name only the field(s) above -- `"
        + _PAYLOAD_LITERAL_BS_KEY + " = " + field_example + "` -- to leave any other "
        "field's own doubled runs refused (#1839). Either way, this refusal becomes "
        "a decision you recorded." + chr(10)
        + "  (#1087, #1096, #1808, #1814, #1819, #1839, #1794)"
    )
    return "".join(out)


def _eol_backslash_pair(text: str) -> Optional[Tuple[str, int]]:
    r"""First line in *text* ending with an even run of backslashes, if any.

    Returns `(line without the run, run length)`. Even is the whole point: bash
    consumes backslashes pairwise from the left, so an even run is all escaped
    backslashes and the line genuinely ends -- an odd run leaves one over and is
    the continuation the caller thought they were writing. A run followed by
    whitespace is skipped here and left to `_sh_backslash_warning`; see
    `_payload_sh_eol_backslash_refusal` for why the two halves part company.
    """
    for m in _TRAILING_BACKSLASH_RUN.finditer(text):
        if m.group(2) or len(m.group(1)) % 2:
            continue
        start = text.rfind(chr(10), 0, m.start()) + 1
        return text[start:m.start()], len(m.group(1))
    return None


def _sh_eol_backslash_message(line: str, run: int) -> str:
    """Name both spellings the refused end-of-line backslashes could have meant."""
    q = chr(39) * 3
    bs = chr(92)
    arrow = mark("↳")
    tail = line[-_TOML_LITERAL_TAIL_CHARS:]
    lead = "…" if len(line) > len(tail) else ""
    return (
        "a " + q + " literal block writes a shell file whose line ends with "
        + str(run) + " backslashes. In a literal block a backslash is content, "
        "never an escape, so all " + str(run) + " reach the file — and in bash "
        "an even run at end of line is an escaped backslash, not a line "
        "continuation. It parses cleanly, `bash -n` and `bash-check` agree, and "
        "the script runs differently." + chr(10)
        + "  " + arrow + " to continue the line, write ONE backslash — a literal "
        "block does not eat it:" + chr(10)
        + "      " + lead + tail + bs + chr(10)
        + "  " + arrow + " to write " + str(run) + " literal backslashes, say so "
        "in a basic block, where each doubles:" + chr(10)
        + "      " + chr(34) * 3 + lead + tail + bs * (run * 2) + chr(34) * 3
        + chr(10)
    )


def _payload_dicts(node: Any) -> List[Dict[str, Any]]:
    """Every mapping in a parsed payload -- the op itself, or each `[[ops]]`."""
    found: List[Dict[str, Any]] = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found.extend(_payload_dicts(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_payload_dicts(value))
    return found


def _payload_sh_eol_backslash_refusal(parsed: Any, raw: str) -> str:
    r"""Refuse a literal block that ends a shell line with `\\` (#835).

    `_sh_backslash_warning` (#380) already reads this pattern out of the bytes
    on their way to disk, and warns. The warning is right, and the write went
    through anyway -- which is the issue: a guard that is certain should stop
    the write, a guard that is heuristic should warn, and mixing the two in one
    channel costs the certain ones their authority.

    The severity is not decided by how certain the pattern is. It is decided by
    #834's rule -- refuse when every intent behind it has another spelling, warn
    when refusing would strand one -- and that rule splits this guard rather
    than promoting it whole:

    * From a triple-single-quoted literal payload block the two backslashes are
      **ambiguous**, and both readings have another spelling. Meant one,
      expecting TOML to eat the other? A literal block eats nothing: write one.
      Meant two? Say it in a basic block, where a wanted pair is spelled with
      four. Refusing leaves nothing unwritable, so it refuses -- at parse time,
      before any op of a batch has run.

    * At the write chokepoint there is no second spelling at all. A block there
      reads whole-file content, so one deliberate `echo \\` on line 400 would
      make every later edit to that script impossible, and no payload field
      fixes that for the colon CLI, which has no fields. That is the
      intent-stranding the rule forbids, so `_sh_backslash_warning` stays a
      warning for every other route into the same bytes.

    The issue proposed `allow_literal_backslash = true`. It is not built: the
    basic block is the opt-out, it already exists, and it says *which* of the
    two intents was meant rather than merely silencing the question.

    Provenance is read off `raw` rather than threaded through the parser. A
    value carrying two backslashes appears in its own source verbatim only if it
    came from a literal block -- a basic block spells the same pair with four,
    so the parsed value is not a substring of the source it was parsed from.

    The backslash-then-whitespace half of #380 is deliberately not refused: a
    basic block writes an escaped space exactly as a literal one does, so that
    reading has no second spelling to be sent to.
    """
    for item in _payload_dicts(parsed):
        path = item.get("path")
        if not isinstance(path, str) or not path.endswith(_SH_SUFFIXES):
            continue
        for key, value in item.items():
            if key in ("path", "op") or not isinstance(value, str):
                continue
            found = _eol_backslash_pair(value)
            if found and value in raw:
                return _sh_eol_backslash_message(*found)
    return ""


def _take_payload_warnings() -> str:
    """Drain the parse-time payload notes, or "" if there are none.

    Every path out of `dispatch` that follows a `_load_at_file` has to call
    this. A note parked in a global and drained only at the bottom of the
    function survives each early `return` in between, and then prints attached
    to whatever op runs next -- a claim about a payload that op never had. That
    is this repository's own defect class, produced by the fix for it, so the
    queue is emptied at the exits rather than at one of them.
    """
    if not _PAYLOAD_WARNINGS:
        return ""
    out = "".join(_PAYLOAD_WARNINGS)
    _PAYLOAD_WARNINGS.clear()
    return out


_AT_FILE_REST_MARKER_RE = re.compile(
    r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*@rest[ \t]*\r?$", re.MULTILINE)

# Fields where the @rest tail is used byte for byte, trailing newline and
# all -- a whole-file/whole-block write, where the payload author's own
# file legitimately ends on a blank line. Every other field (`old`/`new`
# for edit/replace, `pattern` for grep/around/...) strips exactly one
# trailing newline off the tail below: the heredoc form's own closing
# newline is not part of the value, and left in it turns an exact `old`/
# `new` pair into a spurious blank line in the written file and a `pattern`
# into one that matches every line (#2668). Matched case-insensitively
# against the field name the payload author wrote after `=`.
_AT_FILE_REST_RAW_FIELDS = {"content"}


def _rest_tail_value(field: str, tail: str) -> str:
    """The @rest tail as it lands in the parsed payload dict for `field`.

    See `_AT_FILE_REST_RAW_FIELDS` above for which fields are exempt.
    """
    if field.lower() in _AT_FILE_REST_RAW_FIELDS:
        return tail
    if tail.endswith("\r\n"):
        return tail[:-2]
    if tail.endswith("\n"):
        return tail[:-1]
    return tail


def _load_at_file(ref: str, note: bool = True) -> Any:
    """Load JSON or TOML from an @file reference — thin wrapper over
    `_load_at_file_raw`, kept for the many callers that need only the
    parsed value (#1032 is the first caller that also needs the source
    text, for `payload-lint`)."""
    parsed, _raw, _source = _load_at_file_raw(ref, note=note)
    return parsed


def _load_at_file_raw(ref: str, note: bool = True) -> "Tuple[Any, str, str]":
    """Load JSON or TOML from an @file reference, and return the raw source
    alongside the parsed value (#1032).

    Accepts:
      @path/to/file.json   — read from filesystem
      @-                   — read from stdin

    Format detected from first non-whitespace char: { or [ → JSON, else TOML.
    TOML lets you embed code blocks with backslashes/quotes/newlines without
    JSON's double-escaping. Use '''triple-single-quote''' for literal content.

    When the content itself contains ''' — Python source that inspects Python
    source is the common case — that delimiter cannot carry it. Fall back to a
    \"\"\"basic\"\"\" block (escapes apply, so backslashes double) or to the JSON
    payload form, which needs no delimiter at all. #394.

    Returns (parsed value, raw source text, source label) -- the raw text
    and label exist for `payload-lint` (#1032), which needs the source to
    read provenance off it; every other caller uses `_load_at_file`, the
    thin wrapper that discards the last two.
    Raises ValueError with a human-readable message on any error.
    """
    if ref == "@payload" or ref.startswith("@payload "):
        # A batch sub-op that ran from a payload is echoed as
        # `op:@payload → target` (#644). That header is deliberately not
        # re-runnable: the fields it ran from cannot be flattened onto a colon
        # CLI without becoming a different op. Say so, rather than letting it
        # fall through to a bare "@file not found: payload", which reads as a
        # missing file and invites the reader to go looking for one.
        raise ValueError(
            "'@payload' is a header placeholder, not a reference. This op ran "
            "from an @payload whose fields no single-colon header can reproduce "
            "(#644) — re-run it from the original payload file or stdin."
        )
    if ref == "@-":
        raw = sys.stdin.read()
        source = "<stdin>"
    else:
        fpath = ref[1:]  # strip leading @
        resolved = _resolve_at_path(fpath)
        if not os.path.isfile(resolved):
            raise ValueError(_at_file_missing_msg(fpath))
        try:
            with open(resolved, "r", encoding="utf-8") as _f:
                raw = _f.read()
        except OSError as _e:
            raise ValueError(f"@file read error: {fpath}: {_e}") from _e
        source = fpath
    fmt = _detect_payload_format(raw)
    if fmt == "json":
        try:
            return json.loads(raw), raw, source
        except json.JSONDecodeError as _e:
            raise ValueError(f"@file JSON parse error ({source}): {_e}") from _e
    try:
        import tomllib  # stdlib, Python 3.11+
        parser = tomllib.loads
    except ImportError:
        parser = _mini_toml_loads
    # `FIELD = @rest` ends the TOML header there; everything after that
    # line's newline, to end of stream, is FIELD's tail -- never TOML-parsed,
    # so a quoting collision with source code in the content cannot happen
    # (#1868). It is NOT the field's value verbatim for every field: outside
    # `_AT_FILE_REST_RAW_FIELDS`, `_rest_tail_value` (below) strips exactly
    # one trailing newline before it lands in `parsed` (#2668) -- `content`
    # is the one field that still gets the tail byte for byte. `toml_source`
    # is the header only; `raw` keeps the whole original text for the caller
    # that wants provenance (#1032).
    #
    # Residual, named rather than hidden (self-review): the marker regex has
    # no TOML string/table context, so a line that only LOOKS like a marker
    # -- a `foo = @rest` line sitting inside an already-open multi-line
    # string value earlier in the header, documenting this very feature
    # being the likely way one arrives -- is still matched and still ends
    # the header there. The bar this is held to is that the failure stays
    # LOUD: the truncated header then fails to parse (an unterminated
    # string), which is exactly what happens, never a silent misparse. A
    # `[[table]]` array before the marker (a batch payload's [[ops]]) is
    # refused explicitly, below, rather than relying on that same
    # loud-failure argument, because the top level of a batch payload IS
    # still a dict and the injection would otherwise land silently on the
    # wrong table.
    rest_field = None
    toml_source = raw
    rest_marker = _AT_FILE_REST_MARKER_RE.search(raw)
    if rest_marker and re.search(
            r"^[ \t]*\[", raw[:rest_marker.start()], re.MULTILINE):
        # A `[table]` or `[[table]]` header appears before the marker, so
        # the marker line may belong to a NESTED table rather than the
        # top-level dict this pre-split assumes -- a `batch:@-` payload's
        # `[[ops]]` entries, or a plain `[section]`. Treating it as the
        # header-ending marker would inject the tail at the WRONG level
        # (top-level `parsed[rest_field]` rather than the open table) and,
        # for a `[[table]]` array, truncate every later entry too -- both
        # silently (self-review, #1868; the single-bracket case was found by
        # the oss:auditor spawn during the same self-review round). Declining
        # the split keeps the failure loud instead: the literal `@rest`
        # token then reaches the TOML parser as an ordinary invalid value
        # and errors the same way it always did before this feature existed.
        rest_marker = None
    if rest_marker:
        rest_field = rest_marker.group(1)
        toml_source = raw[:rest_marker.start()]
        marker_line = toml_source.count(chr(10)) + 1
        nl_idx = raw.find(chr(10), rest_marker.end())
        rest_tail = raw[nl_idx + 1:] if nl_idx != -1 else ""
        if not rest_tail:
            raise ValueError(
                f"@file payload refused ({source}): the @rest tail is empty "
                f"-- nothing follows line {marker_line} (`{rest_field} = @rest`)"
            )
    try:
        parsed = parser(toml_source)
    except Exception as _e:
        raise ValueError(
            f"@file TOML parse error ({source}): {_e}{_toml_delimiter_hint(toml_source)}"
        ) from _e
    if rest_field is not None:
        if not isinstance(parsed, dict):
            raise ValueError(
                f"@file payload refused ({source}): `{rest_field} = @rest` "
                f"needs a table payload at the top level, got "
                f"{type(parsed).__name__}"
            )
        existing = {str(k).lower(): k for k in parsed}
        if rest_field.lower() in existing:
            orig_key = existing[rest_field.lower()]
            # LAST match, not first: a decoy line that merely looks like
            # "key =" inside an earlier string value in the header would
            # otherwise be reported as the conflicting field instead of the
            # real assignment closer to the marker (self-review, #1868).
            # Still context-blind -- a decoy AFTER the real assignment can
            # still mislead -- the same accepted limitation this file
            # already documents for provenance lookup (_payload_field_provenance:
            # "good enough for a single-op payload, where each key appears once").
            field_ms = list(re.finditer(
                r"^[ \t]*" + re.escape(orig_key) + r"[ \t]*=",
                toml_source, re.MULTILINE))
            field_line = (toml_source.count(chr(10), 0, field_ms[-1].start()) + 1
                          if field_ms else "?")
            raise ValueError(
                f"@file payload refused ({source}): `{rest_field}` is given "
                f"twice -- as a header field (payload line {field_line}) and "
                f"as the @rest tail (line {marker_line}). Use one."
            )
        _rest_value = _rest_tail_value(rest_field, rest_tail)
        if not _rest_value and rest_field.lower() not in _AT_FILE_REST_RAW_FIELDS:
            # `rest_tail` itself was non-empty (the check above already
            # refused a true empty tail), but it was nothing except the
            # marker line's own trailing newline -- e.g. `new = @rest`
            # followed immediately by one blank line and EOF. Stripping
            # that single newline would otherwise silently hand the caller
            # an empty string: `edit`/`replace`'s `new` would delete the
            # matched text and `grep`'s `pattern` would match every line,
            # the exact silent-wrong-answer shape #2668 was filed to
            # eliminate, just reached through a blank-line tail instead of
            # an unstripped one (self-review, oss:developer review round).
            raise ValueError(
                f"@file payload refused ({source}): the @rest tail for "
                f"`{rest_field}` is empty once its trailing newline is "
                f"stripped -- line {marker_line} (`{rest_field} = @rest`) "
                f"is followed by nothing but a blank line"
            )
        parsed[rest_field] = _rest_value
    refusal = _toml_literal_backslash_refusal(toml_source)
    if refusal:
        raise ValueError(f"@file payload refused ({source}): {refusal}")
    refusal = _payload_sh_eol_backslash_refusal(parsed, toml_source)
    if refusal:
        raise ValueError(f"@file payload refused ({source}): {refusal}")
    # `note=False` for the read-op route: the note is about the write path, and
    # a `grep` pattern is a regex rather than file content -- nothing lands, the
    # doubled backslash there is a different question with a different answer,
    # and raising it would be noise on an op that cannot misfile a byte. The
    # refusals below are scoped the same way and for the same reason (#1087).
    # `toml_source` rather than `raw` throughout: the @rest tail was never
    # TOML-parsed, so none of these text-scanning guards can see it, and a
    # doubled backslash written verbatim in the tail is exempt by construction
    # rather than by a special case (#1868).
    if note:
        refusal = _payload_literal_backslashes_misplaced(parsed)
        if refusal:
            raise ValueError(f"@file payload refused ({source}): {refusal}")
        refusal = _payload_double_backslash_refusal(parsed, toml_source)
        if refusal:
            raise ValueError(f"@file payload refused ({source}): {refusal}")
        text = _payload_double_backslash_note(toml_source)
        if text:
            _PAYLOAD_WARNINGS.append(text)
        for _abs_path, _advice in _payload_py_doubled_backslash_advisory(parsed, toml_source).items():
            _PAYLOAD_PY_ESCAPE_ADVISORY[_abs_path] = _advice
        refusal = _payload_shell_quote_escape_refusal(parsed, toml_source)
        if refusal:
            raise ValueError(f"@file payload refused ({source}): {refusal}")
    return parsed, raw, source


_PAYLOAD_LINT_Q3 = chr(39) * 3   # a triple-single-quote delimiter
_PAYLOAD_LINT_QQQ = chr(34) * 3  # a triple-double-quote delimiter


def _payload_field_provenance(raw: str, key: str) -> str:
    """Which delimiter *key* was written with, read off the source (#1032).

    A basic (triple-double-quote) block is indistinguishable from a
    literal (triple-single-quote) one after parsing -- both end up as an
    ordinary Python str -- so this has to look at the TOML source text
    rather than the parsed value. Matches the FIRST `key = DELIM` it
    finds; good enough for a single-op payload, where each key appears
    once.
    """
    rest_m = re.search(
        r"^[ \t]*" + re.escape(key) + r"[ \t]*=[ \t]*@rest[ \t]*\r?$",
        raw, re.MULTILINE)
    if rest_m:
        return "@rest tail (unparsed, no escape processing)"
    delim_pat = (re.escape(_PAYLOAD_LINT_Q3) + "|"
                 + re.escape(_PAYLOAD_LINT_QQQ) + "|'|\\\"")
    m = re.search(
        r"^[ \t]*" + re.escape(key) + r"[ \t]*=[ \t]*(" + delim_pat + ")",
        raw, re.MULTILINE)
    if not m:
        return "could not be determined"
    delim = m.group(1)
    if delim == _PAYLOAD_LINT_Q3:
        return "triple-single-quoted literal block"
    if delim == _PAYLOAD_LINT_QQQ:
        return "triple-double-quoted basic block"
    if delim == "'":
        return "single-line literal"
    return "single-line basic"


def _payload_lint_field_report(raw: str, key: str, value: Any) -> str:
    """One line per field for `payload-lint` -- shape and provenance,
    never a verdict (#1032). This op reports; it does not decide.

    `key!r` rather than `key` bare (#1032 audit finding, class C): a TOML
    basic-string key may itself carry an escaped newline, and an unescaped
    key landing at the front of the line lets the payload's OWN field name
    forge a second, standalone line that impersonates this op's own
    output -- observed forging a fake `anchor check ... matches N time(s)`
    line. `!r` keeps the key on one line regardless of what it contains,
    the same defence every OTHER value in this function already had via
    `!r` (the not-text branch) or via the escaped rendering `len()`/count
    already imply.
    """
    if not isinstance(value, str):
        return f"  {key!r}: {type(value).__name__} = {value!r} (not text)"
    n_lines = value.count(chr(10)) + 1 if value else 0
    prov = _payload_field_provenance(raw, key)
    flags = []
    if "\\" in value:
        flags.append("carries a literal backslash")
    if "\r" in value:
        flags.append("carries a stray \\r")
    # Strip a trailing CR before checking for trailing whitespace, or a
    # CRLF field trips BOTH flags for the same two bytes (#1032 review):
    # `ln.rstrip()` also eats the `\\r` the check above already named,
    # reporting one defect as two.
    _no_cr = [ln[:-1] if ln.endswith(chr(13)) else ln
              for ln in value.split(chr(10))]
    if any(ln != ln.rstrip() for ln in _no_cr):
        flags.append("has a trailing-whitespace line")
    line = f"  {key!r}: {len(value)} chars, {n_lines} line(s), provenance={prov}"
    if flags:
        line += " -- " + ", ".join(flags)
    return line


def _payload_lint_anchor_report(parsed: Dict[str, Any]) -> str:
    """For an edit/replace-shaped payload: does `old` match right now, and
    how many times -- 0, 1, or N, WITHOUT writing (#1032).

    A snapshot of the target file as it is at lint time, not a lock -- the
    real apply can still see a different file. Stated as a snapshot rather
    than hidden, per the issue's own judgment call: "report it, and say it
    is a snapshot".
    """
    old = parsed.get("old")
    path = parsed.get("path")
    if not isinstance(old, str) or not isinstance(path, str) or not path:
        return ""
    if not os.path.isfile(path):
        return (f"anchor check: no file at {path!r} yet -- match count "
                f"could not be determined" + chr(10))
    try:
        with open(path, "r", encoding="utf-8", errors="surrogateescape",
                  newline="") as f:
            content = f.read()
    except OSError as e:
        return f"anchor check: could not read {path!r}: {e}" + chr(10)
    count = content.count(old)
    if count == 0:
        for _nl, cand in _newline_variants(old)[1:]:
            n = content.count(cand)
            if n:
                count = n
                break
    return (f"anchor check (snapshot of {path!r} right now): 'old' "
            f"matches {count} time(s)" + chr(10))


def _payload_lint_path_candidates(parsed: Any) -> "List[str]":
    """Every path `op_payload_lint` is about to open, read or resolve
    (#1032 review, class C follow-up).

    Mirrors the three shapes `op_payload_lint` itself recognizes: a bare
    ops array, `{"ops": [...]}`, and a single-op table. Anything else
    (a malformed item, a missing/non-string `path`) contributes nothing --
    those are reported as their own "could not be determined" states
    further down, not silently treated as contained.
    """
    if isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, dict) and isinstance(parsed.get("ops"), list):
        items = parsed["ops"]
    elif isinstance(parsed, dict):
        items = [parsed]
    else:
        return []
    candidates = []
    for item in items:
        if isinstance(item, dict):
            p = item.get("path")
            if isinstance(p, str) and p:
                candidates.append(p)
    return candidates


def op_payload_lint(ref: str) -> str:
    """Parse an @file/@- payload and report its shape, run nothing (#1032).

    The gap this closes: a payload is parsed, then acted on, and there was
    no way to see the parse. A malformed triple-quoted block was only
    discoverable by applying the payload and reading the target file back
    -- and for `edit`, a malformed payload does not even refuse, it
    matches, writes, and reports `edited`.

    Three states throughout, per the issue's own judgment calls: a field
    whose provenance could not be determined says so rather than guessing,
    and a target file that does not exist yet says the match count could
    not be determined rather than reporting a guessed zero. This op never
    refuses or warns on a suspect payload beyond what loading it already
    does -- the gate is the gate (#834/#835/#1027); this op only reports.
    """
    if not ref.startswith("@"):
        return ("ERROR: payload-lint takes an @reference (@file or @-), "
                "e.g. payload-lint:@edits.toml\n")
    try:
        parsed, raw, source = _load_at_file_raw(ref, note=False)
    except ValueError as exc:
        return f"ERROR: {exc}\n"

    # Every path this op is about to open or resolve -- the anchor
    # snapshot's target, each ops-array item's path -- through the same
    # gate every other path-bearing route passes through, BEFORE any of
    # them is opened, resolved or reported. Without this, `payload-lint`
    # was an existence-and-content oracle for any path on disk: an @file
    # payload naming `/etc/hosts` as `path` returned whether it exists and
    # how many times an arbitrary `old` string occurred in it, unlike every
    # other op (#1032 review, class C follow-up).
    contained = _containment_error(_payload_lint_path_candidates(parsed))
    if contained:
        return contained

    lines = [f"payload-lint: {source}"]

    def _payload_lint_show_path(value: object) -> str:
        """Render an ops-array entry's `path` so it can be copied back out.

        `!r` doubles every backslash, so a Windows path printed here arrived
        with its separators doubled -- a different string from the one the
        payload carries, which is the one thing #672 asks this line to show.
        Four windows legs on #2200 failed on exactly that.

        `!r` is kept for the values where a bare rendering would be worse than
        unreadable: a path carrying a quote, a newline or a tab hides its own
        delimiters or breaks the line it is printed on, and a payload's path
        is untrusted input. Those are shown escaped, and a reader can tell the
        two apart because the escaped form still starts with a quote.
        """
        if not isinstance(value, str):
            return repr(value)
        if any(ch in value for ch in ("'", chr(34), chr(10), chr(13), chr(9))):
            return repr(value)
        return "'" + value + "'"

    is_ops_array = isinstance(parsed, list) or (
        isinstance(parsed, dict) and isinstance(parsed.get("ops"), list))
    if is_ops_array:
        ops_list = parsed if isinstance(parsed, list) else parsed["ops"]
        lines.append(f"shape: ops array, {len(ops_list)} op(s)")
        for i, item in enumerate(ops_list):
            if not isinstance(item, dict):
                lines.append(f"  [{i}] not a table ({type(item).__name__})"
                             f" -- provenance could not be determined")
                continue
            kind = item.get("op", "<missing 'op'>")
            path = item.get("path")
            resolved = ""
            if isinstance(path, str) and path:
                try:
                    resolved = os.path.abspath(_resolve_at_path(path))
                except (OSError, ValueError):
                    resolved = "<could not resolve>"
            entry = f"  [{i}] op={kind!r} path={_payload_lint_show_path(path)}"
            if resolved and resolved != path:
                entry += f" -> {resolved}"
            lines.append(entry)
        return chr(10).join(lines) + chr(10)

    if not isinstance(parsed, dict):
        lines.append(f"shape: {type(parsed).__name__} -- not a table, no "
                     f"fields to report")
        return chr(10).join(lines) + chr(10)

    lines.append(f"shape: single-op payload, {len(parsed)} field(s)")
    for key, value in parsed.items():
        lines.append(_payload_lint_field_report(raw, key, value))
    anchor = _payload_lint_anchor_report(parsed)
    body = chr(10).join(lines) + chr(10)
    if anchor:
        body += anchor
    return body


# Dynamic @file field registry — built lazily from op syntax strings.
# Maps op name → ordered list of JSON field names (positional parts[1..N]).
# Populated on first dispatch call via _build_at_file_registry().
_AT_FILE_REGISTRY: Dict[str, List[Tuple[str, bool, bool]]] = {}
_AT_FILE_REGISTRY_BUILT: bool = False

# Ops whose syntax string contains ':::' (so a @payload route was clearly
# intended) but whose derived field names were discarded by the identifier
# guard in _fields_from_syntax — e.g. inline prose or punctuation that no
# payload key could ever match. Populated alongside _AT_FILE_REGISTRY.
#
# This is NOT the same as an op with no ':::' at all (a read-only op, the
# common and correct case, which never appears here). Conflating the two
# would make this list mostly noise; keeping them apart is what lets a test
# — or a human reading a failure message — ask "was this route dropped on
# purpose, or did a syntax edit just delete it?" (#770). Nothing reads this
# list at runtime; it exists so a test failure can explain itself.
_AT_FILE_DROPPED_ROUTES: List[Tuple[str, str]] = []


def _fields_from_syntax(syntax: str) -> List[Tuple[str, bool, bool]]:
    """Derive field specs from a syntax string using ':::' separator.

    Returns a list of (name, optional, variadic) tuples:
      - name:     lowercased field name, stripped of [ ] ... and whitespace
      - optional: field sits inside a trailing [...] optional group
      - variadic: field token carried '...' — payload value may be a list,
                  expanded into multiple positional parts

    Takes the first alternative (before ' | '), splits on ':::', drops the
    first token (op name). Returns [] if the syntax has no ':::' (read-only
    op — no @file route). Optionality is tracked by '[' / ']' bracket depth,
    so a field is optional whenever an unclosed group is open at its position
    (correct even for a non-trailing optional group).

    Returns [] when any derived field name is not a clean identifier
    ([a-z][a-z0-9_]*). That guards against syntax strings carrying inline
    prose or punctuation a payload key could never match — e.g. git-resolve's
    'PATH[,PATH...][:::BLOCKS]  (SIDE: ...)'. Such ops simply have no @file
    route rather than a falsely-registered, non-functional one.

    Examples:
      'edit:::OLD:::NEW:::PATH'            → [('old',F,F),('new',F,F),('path',F,F)]
      'git-commit:::MESSAGE[:::PATHS...]'  → [('message',F,F),('paths',T,T)]
      'read:PATH'                          → []
    """
    first_alt = re.split(r"\s*\|\s*", syntax)[0]
    if ":::" not in first_alt:
        return []
    specs: List[Tuple[str, bool, bool]] = []
    depth = 0
    for tok in first_alt.split(":::")[1:]:
        variadic = "..." in tok
        optional = depth > 0
        depth += tok.count("[") - tok.count("]")
        name = (tok.replace("[", "").replace("]", "")
                   .replace("...", "").strip().lower())
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            return []
        specs.append((name, optional, variadic))
    return specs


# Read ops that accept `op:@file` / `op:@-`. Deliberately NOT routed through
# _AT_FILE_BUILTIN_DEFAULTS: that registry rebuilds a positional parts list and
# hands it back to the colon parsers, which would re-split the very pattern the
# payload exists to protect. These are dispatched straight to the op (#625).
_READ_OP_AT_FIELDS: Dict[str, Tuple[str, ...]] = {
    "grep":        ("pattern", "path", "limit", "context", "count", "no_auto_read", "full"),
    "around":      ("pattern", "path", "n"),
    "grep_around": ("pattern", "path", "n", "limit"),
    "between":     ("symbol", "start", "end", "path"),
    "read":        ("path", "offset", "limit", "grep", "full"),
    # `validate` is here for the mirror-image reason (#878). Its problem is not
    # a pattern that may contain ':' but a *path list* that may: the colon form
    # `validate:f1,f2,…:FILTER` joins on both ':' and ',', and a filename may
    # legally contain either — as may every absolute path on Windows, whose
    # drive letter is a colon. There is no escape in that form and no amount of
    # sender-side filtering recovers one; only a channel that never re-splits
    # does.
    "validate":    ("path", "paths", "tools", "verbose"),
}


def _payload_strlist(p: Dict[str, Any], key: str) -> List[str]:
    """A payload field that is either one string, or a list of them.

    A comma-separated string is accepted for `tools` because that is what the
    colon form already means there. It is NOT accepted for a path list: commas
    are legal in filenames, and re-splitting on one is the defect the payload
    route exists to avoid.
    """
    value = p.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v)]
    raise ValueError(
        f"field {key!r} must be a string or a list of strings, "
        f"got {type(value).__name__}"
    )


def _validate_from_payload(p: Dict[str, Any]) -> str:
    """Run the `validate` op from a payload, so a path may contain ':' or ','.

    `paths` (a list) is the form that motivates the route; `path` is accepted
    as the singular spelling every other read op uses. Field semantics are the
    colon form's, unchanged: >1 file dispatches the list form and `tools` scopes
    the validator selection.

    **Containment applies to every path, not to the list form.** The first
    version of this function guarded only the `len > 1` branch, on the stated
    ground that this was parity with dispatch. It was not: dispatch applies
    containment twice — generically at `_PATH_ARG_POSITIONS`, which every op
    gets, and *additionally* in the list branch, where position 1 is a
    comma-joined blob the generic gate cannot read. Replicating only the second
    reproduced the special case and skipped the rule (#882). The gate now sits
    one level up, at the top of `_read_op_from_payload`, where the same call
    covers this op's `path` and `paths` *and* the four read ops that had no
    gate at all (#885) — one call for the whole route, so no door into it can
    disagree with another about a path.
    """
    files = _payload_strlist(p, "paths") or _payload_strlist(p, "path")
    if not files:
        return ("ERROR: @payload for op 'validate' missing required field "
                "'path' (or 'paths' for the list form)\n")
    raw_tools = p.get("tools")
    if isinstance(raw_tools, str):
        tools = [t for t in raw_tools.split(",") if t]
    else:
        tools = _payload_strlist(p, "tools")
    verbose = _payload_bool(p, "verbose")
    if len(files) > 1:
        return op_validate_multi(files, tools or None, verbose=verbose)
    return op_validate(files[0], tools or None, verbose=verbose)


def _payload_int(p: Dict[str, Any], key: str, default: int) -> int:
    value = p.get(key)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"field '{key}' must be an integer, got {value!r}") from None


def _payload_grep_limit(p: Dict[str, Any], default: int) -> int:
    """`limit` for the grep family, where the string `all` is a value (#1328).

    The colon CLI and the payload route have to accept the same LIMIT or the
    token is a feature of one spelling — and the payload route is the one that
    exists for the patterns the CLI cannot express, which is exactly where a
    call-site sweep with an alternation ends up.
    """
    value = p.get("limit")
    # Matched exactly, as the colon CLI matches it and as `count` /
    # `no-auto-read` are matched there. A payload that accepted `All` while the
    # CLI read the same token as a path name would make the spelling a property
    # of the route; `_payload_int` refuses it by name instead.
    if value == _GREP_ALL_TOKEN:
        return GREP_LIMIT_ALL
    return _payload_int(p, "limit", default)


def _payload_bool(p: Dict[str, Any], key: str) -> bool:
    value = p.get(key, False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _read_op_from_payload(op: str, payload: Any, no_exclude: bool = False) -> str:
    """Run a read op from an @file/@- payload — the colon-free route (#625).

    The things worth grepping for contain ':' by nature: PHP `Class::CONST`,
    log prefixes, assertion messages, timestamps, and alternations whose last
    branch ends in one. The colon CLI has to guess where the pattern stops; a
    payload never has to. Same @file/@- shape the mutating ops already use, so
    it is one rule applied consistently rather than two to remember.

    `validate` joins the set for the same reason read from the other end: there
    the ambiguous field is the pattern, here it is the file list (#878).
    """
    if isinstance(payload, list) or (
        isinstance(payload, dict) and isinstance(payload.get("ops"), list)
    ):
        return (f"ERROR: this payload is an ops array — use 'batch:@file' "
                f"instead of '{op}:@file'\n")
    if not isinstance(payload, dict):
        return (f"ERROR: @payload for op '{op}' must be a JSON object / TOML "
                f"table, got {type(payload).__name__}\n")
    p = {str(k).lower(): v for k, v in payload.items()}
    allowed = _READ_OP_AT_FIELDS[op]
    unknown = sorted(k for k in p if k not in allowed)
    if unknown:
        return (f"ERROR: unknown field(s) {_flat_keys(unknown)} in {op}:@payload "
                f"— accepted: {', '.join(allowed)}\n")
    try:
        # Containment, before any op sees a path (#885). `op_read` is caught by
        # the `render_file` chokepoint and `validate` gated itself, but
        # `op_grep`, `op_around` and both `op_between_*` have no check of their
        # own: they rely on the `_PATH_ARG_POSITIONS` gate in `_dispatch_impl`,
        # which this route returns before ever reaching. A payload therefore
        # returned the contents of any file on disk, with a regex the caller
        # chose — strictly worse than #882, which was an oracle and an
        # argument. One call here rather than a `_safe_path` beside each op:
        # re-implementing the rule locally is what produced #882, and a fourth
        # copy would drift the same way. Both fields are read with
        # `_payload_strlist`, which accepts a string or a list, because
        # `validate` accepts a list under `path` as well as under `paths` — a
        # `str()` here stringified that list into one nonsense name that
        # resolves under cwd, and the op then used the real one.
        contained = _containment_error(
            [*_payload_strlist(p, "path"), *_payload_strlist(p, "paths")])
        if contained:
            return contained
        # Write the gate's own `~` expansion back, behind the check that just
        # cleared these exact strings (#1300). Applied to the payload fields
        # rather than to a returned list because every branch below re-reads
        # `p["path"]` / `p["paths"]`, and either may be a string or a list.
        for _pk in ("path", "paths"):
            if _pk in p:
                _pv = p[_pk]
                if isinstance(_pv, str):
                    p[_pk] = _expand_home(_pv)
                elif isinstance(_pv, list):
                    p[_pk] = [_expand_home(_x) if isinstance(_x, str) else _x
                              for _x in _pv]
        if op in ("grep", "grep_around", "around"):
            pattern = str(p.get("pattern", "") or "")
            if not pattern:
                return (f"ERROR: @payload for op '{op}' missing required "
                        f"field 'pattern'\n")
            path = str(p.get("path") or ".")
            if op == "grep":
                p_limit = _payload_grep_limit(
                    p, _get_op_int("grep", "max_results", MAX_GREP_RESULTS))
                if p_limit == 0:
                    return _grep_zero_limit()
                return op_grep(
                    pattern, path, p_limit,
                    _payload_int(p, "context", 0),
                    _payload_bool(p, "count"),
                    no_exclude=no_exclude,
                    no_auto_read=_payload_bool(p, "no_auto_read"),
                    via_payload=True,
                    full=_payload_bool(p, "full"),
                )
            if op == "grep_around":
                return op_grep(pattern, path, _payload_grep_limit(p, 10),
                               _payload_int(p, "n", 3), False,
                               no_exclude=no_exclude, via_payload=True)
            return op_around(pattern, path, _payload_int(p, "n", 10),
                              via_payload=True)
        if op == "between":
            path = str(p.get("path", "") or "")
            symbol = str(p.get("symbol", "") or "")
            start = str(p.get("start", "") or "")
            end = str(p.get("end", "") or "")
            if symbol and (start or end):
                return ("ERROR: between:@payload takes EITHER 'symbol' (symbol "
                        "mode) OR 'start' + 'end' (pattern mode), not both\n")
            if symbol:
                return op_between_symbol(symbol, path)
            if start and end:
                return op_between_pattern(start, end, path)
            return ("ERROR: @payload for op 'between' needs 'symbol' (symbol "
                    "mode) or 'start' + 'end' (pattern mode)\n")
        if op == "validate":
            return _validate_from_payload(p)
        path = str(p.get("path", "") or "")
        if not path:
            return "ERROR: @payload for op 'read' missing required field 'path'\n"
        offset = _payload_int(p, "offset", 0)
        limit = _payload_int(p, "limit", 0)
        return op_read(path, offset, limit, str(p.get("grep", "") or ""),
                       _payload_bool(p, "full"))
    except ValueError as exc:
        return f"ERROR: {exc}\n"


_AT_FILE_BUILTIN_DEFAULTS: Dict[str, List[Tuple[str, bool, bool]]] = {
    "edit":          [("old", False, False), ("new", False, False), ("path", False, False)],
    "replace":       [("old", False, False), ("new", False, False), ("path", False, False)],
    "replace_dry":   [("old", False, False), ("new", False, False), ("path", False, False)],
    "replace_lines": [("path", False, False), ("start", False, False), ("end", False, False), ("content", False, False)],
    "paste":         [("path", False, False), ("content", False, False)],
    "append":        [("path", False, False), ("content", False, False)],
    "vim":           [("path", False, False), ("script", False, False)],
}


def _build_at_file_registry() -> None:
    """Populate _AT_FILE_REGISTRY from builtin-ops and custom/preset op syntax.

    Starts from _AT_FILE_BUILTIN_DEFAULTS so the builtins always work even
    when no config file is present (e.g. in tests). Config-derived entries
    overlay the defaults, allowing syntax-driven overrides and automatically
    giving preset ops with ':::' syntax their own @file routes.

    Called once on the first dispatch invocation.
    """
    global _AT_FILE_REGISTRY, _AT_FILE_REGISTRY_BUILT
    if _AT_FILE_REGISTRY_BUILT:
        return
    registry: Dict[str, List[Tuple[str, bool, bool]]] = dict(_AT_FILE_BUILTIN_DEFAULTS)
    dropped: List[Tuple[str, str]] = []
    config = _load_config()
    for section in ("builtin-ops", "ops"):
        for op_name, info in config.get(section, {}).items():
            if not isinstance(info, dict):
                continue
            if info.get("form"):
                # A documented *form* of another op, not an op (#1245). Its
                # syntax is the parent's, so a route keyed on this entry's own
                # name — `read-grep:@-` — is one the dispatcher would refuse.
                continue
            if op_name in _READ_OP_AT_FIELDS:
                # The read family is dispatched straight from
                # `_READ_OP_AT_FIELDS`, never from this registry (#625, see
                # the comment on that dict) — a config `syntax` here can only
                # change what `help:OP` prints, never what dispatch accepts
                # (#2081). Honouring it would make the rendered keys a
                # misreport a manifest can trigger.
                continue
            syntax = info.get("syntax", "")
            if not syntax:
                continue
            fields = _fields_from_syntax(syntax)
            if fields:
                registry[op_name] = fields
            elif ":::" in re.split(r"\s*\|\s*", syntax)[0]:
                # The guard fired, not a read-only op: syntax carries ':::' —
                # a @payload route was intended — but the derived field
                # names weren't clean identifiers, so it was discarded.
                dropped.append((op_name, syntax))
    _AT_FILE_REGISTRY = registry
    _AT_FILE_DROPPED_ROUTES[:] = dropped
    _AT_FILE_REGISTRY_BUILT = True


def _at_file_specs(op: str) -> List[Tuple[str, bool, bool]]:
    """Return (name, optional, variadic) specs for *op*, or [] if no @file route."""
    _build_at_file_registry()
    return _AT_FILE_REGISTRY.get(op, [])


def _at_file_dropped_routes() -> List[Tuple[str, str]]:
    """Ops whose ':::'-bearing syntax had its @payload route discarded by the
    identifier guard in _fields_from_syntax — see _AT_FILE_DROPPED_ROUTES.
    """
    _build_at_file_registry()
    return list(_AT_FILE_DROPPED_ROUTES)


def _at_file_fields(op: str) -> List[str]:
    """Return the field NAMES for *op*, or [] if the op has no @file route.

    Kept name-only for the truthiness/sub-op callers; field semantics
    (optional, variadic) live in _at_file_specs.
    """
    return [name for name, _opt, _var in _at_file_specs(op)]


def _generic_preset_payload_hint(op: str) -> str:
    """Name the `args` key the generic preset route (#1165) accepts, and
    show a call that would work -- the same shape `_at_file_payload_hint`
    gives a named-field op, for the op population that has no named fields
    to name.
    """
    quote = "'" * 3
    lines = [
        f"  {op}:@... has no named payload fields -- it reads its argv "
        f"from '{_GENERIC_PRESET_ARGS_KEY}' instead (a list of strings, "
        f"one per positional argument, sent verbatim: no colon split, no "
        f"mode-rejoin heuristic).",
        f"    ./supertool '{op}:@-' <<'EOF'",
        f"    {_GENERIC_PRESET_ARGS_KEY} = [{quote}...{quote}]",
        "    EOF",
    ]
    return chr(10) + chr(10).join(lines)


_PRESET_NAMED_PAYLOAD_FIELDS: Dict[str, Tuple[str, ...]] = {
    # None of these four take the generic args-list preset route --
    # `repo_target: "payload"` in presets/github.json marks all four as ops
    # that parse their OWN named-field payload, but `_at_file_payload_hint`
    # had no branch for that population and fell through to
    # `_generic_preset_payload_hint`, printing an `args = [...]` example the
    # op's own loader refuses outright (#2444: `help:gh-issue-comment` told a
    # caller to send `args`; the same symptom was confirmed live, in this
    # same self-review round, against the other three -- `help:gh-issue-create`
    # and `help:gh-pr-edit` still printed the wrong hint after #2444's first
    # pass fixed only `gh-issue-comment`, which is what a fix scoped to the
    # filed op alone always risks when the root cause is shared).
    #
    # Every tuple here is pinned by `tests/test_gh_issue_comment_help_text_2444.py`
    # against the op's own `ACCEPTED_KEYS`, read by importing the preset
    # module directly -- a TEST-enforced agreement, not a structural one:
    # nothing stops this dict and that module-level set from being edited
    # independently, the same way #2444 itself happened.
    "gh-issue-comment": ("body", "body_file", "repo"),
    "gh-issue-create": (
        "repo", "title", "body", "body_file", "labels", "assignees",
        "milestone", "dry_run",
    ),
    "gh-pr-create": (
        "repo", "title", "base", "head", "body", "body_file", "draft",
        "labels", "assignees", "reviewers", "milestone",
        "literal_backslashes", "no_close",
    ),
    "gh-pr-edit": (
        "repo", "title", "body", "body_file", "literal_backslashes",
        "no_close", "base", "head", "draft", "labels", "assignees",
        "reviewers", "milestone",
    ),
}


def _preset_named_payload_hint(op: str, fields: Tuple[str, ...]) -> str:
    """Hint text for a preset op that parses its OWN named-field payload
    (`_PRESET_NAMED_PAYLOAD_FIELDS`), never the generic `args` list.

    The worked example picks `body` (or `title` where there is no `body`)
    over `fields[0]` -- `repo` is first in most of these tuples because it
    is shared/optional across the family, and a caller copying the example
    verbatim wants to see the field they are most likely to actually set.
    """
    quote = "'" * 3
    if "body" in fields:
        example_field = "body"
    elif "title" in fields:
        example_field = "title"
    else:
        example_field = fields[0]
    lines = [
        f"  {op}:@... reads its fields from the payload. Keys: "
        f"{', '.join(fields)}",
        f"    ./supertool '{op}:@-' <<'EOF'",
        f"    {example_field} = {quote}...{quote}",
        "    EOF",
    ]
    return chr(10) + chr(10).join(lines)


def _at_file_payload_hint(op: str) -> str:
    """Name the payload keys *op* wants, and show a call that would work.

    An error that names the fault but not the remedy is on this tracker's own
    list. Three agents in one evening met either a bare TOML line/column or
    "takes the @reference as the only argument", and each of them guessed the
    key names from scratch; one mangled its own commit message to get past the
    shell instead, which is permanent in that history (#1003).

    For the `:::`-derived registry (`_AT_FILE_REGISTRY`), the keys come from
    the same source that drives the route itself, so those two can never
    drift apart. `_PRESET_NAMED_PAYLOAD_FIELDS` below is NOT that -- it is a
    hand-maintained dict describing a payload shape each op's own preset
    script parses independently, kept honest only by a test
    (`tests/test_gh_issue_comment_help_text_2444.py`) that imports each
    module and compares its `ACCEPTED_KEYS` against this dict, not by any
    structural guarantee. #2444 itself is what a silent drift there looks
    like; treat that pin as the thing standing between this docstring's
    claim and reality for that branch.
    Returns "" for an op with no @file route, leaving its error untouched.

    A preset op with no registered fields (#1165) falls to
    `_generic_preset_payload_hint` instead of "" -- it DOES have a route,
    the generic `args`-list one, and an op that has a route but no hint text
    is indistinguishable from one that has none at all. A preset op that
    parses its OWN named-field payload (`_PRESET_NAMED_PAYLOAD_FIELDS`) is
    checked before that fallback, so it is never described as an args-list
    op (#2444).
    """
    specs = _at_file_specs(op)
    if not specs:
        named = _PRESET_NAMED_PAYLOAD_FIELDS.get(op)
        if named:
            return _preset_named_payload_hint(op, named)
        if _op_is_preset_op(op):
            return _generic_preset_payload_hint(op)
        return ""
    quote = "'" * 3
    # Every key the route ACCEPTS, not only the positional ones the worked
    # example below can demonstrate. `replace_all` is a boolean rather than a
    # content field, so it has no line in the example — and listing it in the
    # refusal's "accepted:" while omitting it here made the two lines of one
    # message disagree about the same set (#1551).
    names = ", ".join(
        name + ("[]" if variadic else "") + (" (optional)" if optional else "")
        for name, optional, variadic in specs
    )
    for extra in _payload_accepted_fields(op, specs)[len(specs):]:
        names += f", {extra} (optional)"
    lines = [
        f"  {op}:@... reads its fields from the payload. Keys: {names}",
        f"    ./supertool '{op}:@-' <<'EOF'",
    ]
    for name, _optional, variadic in specs:
        if variadic:
            lines.append(f'    {name} = ["path/to/file"]')
        else:
            lines.append(f"    {name} = {quote}...{quote}")
    lines.append("    EOF")
    _rest_field_for_op = {"paste": "content", "append": "content",
                           "edit": "new", "replace": "new"}.get(op)
    if _rest_field_for_op and any(
            name == _rest_field_for_op for name, _o, _v in specs):
        lines.append(
            f"    Or end the header with {_rest_field_for_op} = @rest and put "
            "the content after it, unparsed -- nothing after that line is "
            "TOML-parsed, so no delimiter or backslash guard can collide "
            "with it (#1868)."
        )
    if op == "edit":
        # #1867 — the accepted shape (one edit per payload) is otherwise only
        # inferable from the unknown-field list on an `edits`/`[[edits]]`
        # refusal, by which point the payload is already composed. State it
        # here, where a caller reaching for a batch shape sees it BEFORE
        # writing one, and name the actual multi-edit route.
        lines.append(
            "    One edit per payload — for N edits, send N payloads, or "
            "batch:@file with one {op = \"edit\", ...} entry per edit in "
            "[[ops]]."
        )
    return chr(10) + chr(10).join(lines)


def _stdin_ref_in_value_field(op: str, parts: List[str]) -> str:
    """Refuse `@-` sitting in a field that is content, not a payload reference.

    `@-` means stdin everywhere in this grammar, and the @payload route is
    gated on `parts[1]` alone. So `paste:victim.txt:@-` never read stdin: the
    two characters fell through to `op_paste` as the CONTENT and were written
    over the file, under a receipt that said `rewrote victim.txt (1 lines,
    39 -> 3 bytes)`. Nothing about that receipt is false, and nothing in it
    says the body it wrote is not the body that was piped in — the byte count
    was the only tell, and had the intended body been three bytes long there
    would have been none (#1776).

    The same shape reached `append`, `edit`, `replace` and `replace_lines`:
    one gate, one field position, five ops that write. `vim` refused it by
    accident — its script parser has no verb `@` — which is a loud failure but
    not one that names the route that works.

    Scoped to ops with a MUTATING payload route — `_at_file_fields`, not the
    union `_at_file_route_ops` reports — because that is what makes this a
    refusal rather than a dead end: there is a working call to name, and
    `_at_file_payload_hint` names it. The read ops (`grep`, `around`,
    `grep_around`, `between`, `read`, `validate`) have a payload route of their
    own and are deliberately out: a `@-` in one of their value fields is a
    PATH, so it fails by name — `path not found: @-` — with nothing written and
    nothing lost, and the hint has no keys to print for them, so refusing there
    would trade a loud error for a quieter one. Their message could still be
    better; that is a separate finding, not this guard.

    Both colon forms are covered, `:` and `:::`. The `:::` form never routed a
    payload from a later field either, so it is not where the ambiguity lives —
    but it is the separator this repository tells agents to reach for first, so
    it is where the mistyped sigil arrives most often, and the harm is
    identical. Nothing becomes unwritable: the payload route below writes the
    two characters.

    Not applied to fields that arrived through a payload or a batch sub-op.
    There the caller has already said "these bytes, exactly", nothing was
    tokenized, and that route stays the way to write the two characters
    literally — a guard with no way through is this repository's own defect
    class wearing the costume of a fix.

    Equality, not a substring hunt: a field that merely mentions `@-` in prose
    is ordinary content, and this repository writes that sentence often.
    """
    names = _at_file_fields(op)
    if not names:
        return ""
    for idx, value in enumerate(parts[1:], start=1):
        if value != "@-":
            continue
        field = names[idx - 1] if idx - 1 < len(names) else "field " + str(idx)
        return (
            "ERROR: " + op + ": the " + field + " field is `@-`, the stdin "
            "payload reference — but only `" + op + ":@-` reads stdin, and "
            "there the reference is the whole argument. Here it is a value, "
            "so `@-` would have been taken as the " + field + " itself: the "
            "two characters, not the bytes on stdin. Nothing ran."
            + chr(10)
            + "  Send the fields through the payload instead, which is also "
            "the only way to pass a literal `@-`:"
            + _at_file_payload_hint(op) + chr(10)
        )
    return ""


def _at_file_route_ops() -> List[str]:
    """Every op that has an @payload route, from both registries that grant one.

    There are two reasons an op has the route and they are held in two places:
    a ':::' in its syntax (derived by `_fields_from_syntax`) and membership of
    `_READ_OP_AT_FIELDS` (declared, for read ops whose colon form cannot carry
    a regex or a path with a ':' in it). Asking only one of them answers "no
    route" for half the ops that have one.
    """
    _build_at_file_registry()
    return sorted(set(_AT_FILE_REGISTRY) | set(_READ_OP_AT_FIELDS))


def _help_payload_route(op: str) -> str:
    """The `@-` block `help:OP` prints beneath the colon form (#1400).

    `help:paste` used to print `paste:::PATH:::CONTENT` and stop. That is worse
    than printing nothing about input forms at all, because a reference entry
    listing one of two invocation forms reads as complete — an agent that
    needed the payload route to write a multi-line file guessed `path` and
    `content` from scratch and said so.

    Rendered from the registry that drives the route, never typed here. The
    field names are *derived* from the `syntax` string printed directly above
    this block, so a hand-written copy would keep describing a route at exactly
    the moment a syntax reword deleted it — the silent failure
    `TestPayloadRoutePin` exists to catch (#770).

    Returns "" for an op that has no route. Because every op that has one now
    prints a block, that emptiness is an answer rather than a gap.
    """
    hint = _at_file_payload_hint(op)
    if not hint:
        fields = _READ_OP_AT_FIELDS.get(op)
        if not fields:
            return ""
        hint = chr(10) + chr(10).join([
            f"  {op}:@- (stdin) or {op}:@FILE reads its fields from the "
            f"payload.",
            f"    Keys: {', '.join(fields)}",
        ])
    # #1311 — name `literal_backslashes` (#1096) wherever it can actually
    # fire. It is discoverable today only by tripping the doubled-backslash
    # write refusal (`_payload_double_backslash_refusal`) once, which is the
    # very round-trip this whole block exists to save (#1400's own
    # reasoning, one key later). Scoped to the fields that refusal is scoped
    # to — `_PAYLOAD_DBS_WRITE_KEYS` — so an op whose route carries none of
    # them (a read op, or a preset op taking `body`/`title`) is not told
    # about a key nothing on its route would ever refuse.
    if set(_at_file_fields(op)) & _PAYLOAD_DBS_WRITE_KEYS:
        hint += (
            chr(10) + "  A doubled backslash in a " + chr(39) * 3
            + " literal block reaches disk at its full length (a literal "
            "block processes no escapes) and is refused unless you say it "
            "is meant AS WRITTEN: add `literal_backslashes = true` at the "
            "top level of the payload, or name only the field(s) that need "
            "it, e.g. `literal_backslashes = [\"" + sorted(
                set(_at_file_fields(op)) & _PAYLOAD_DBS_WRITE_KEYS)[0]
            + "\"]` (#1096)."
        )
    return (chr(10) + "Payload route — the colon form is not the only one, and "
            "for an argument holding ':' or a newline it is not the working "
            "one (docs/input-forms.md):" + hint)


def _reorder_batch_for_snapshot(batch_ops: List[Any]) -> Tuple[List[Any], str]:
    """Reorder replace_lines ops within a batch so line numbers refer to the
    original file state (snapshot semantics), not the file as mutated by
    earlier ops in the same batch.

    Strategy: group replace_lines ops by path. For each file appearing in 2+
    replace_lines ops, sort them by start descending and write them back into
    their original slots. Non-replace_lines ops keep their order.

    Bottom-up application means earlier (in batch order, now last-applied)
    line numbers stay valid because mutations happen at lines AFTER the
    next op's range.

    Other op types (edit, replace, paste, vim) are content-matched or
    full-file rewrites, so they're inherently snapshot-safe and need no
    reordering.

    Returns (new_ops, error_message). On overlap detection, returns
    (original_ops, error_message) — caller should surface the error before
    applying anything.
    """
    by_file: Dict[str, List[int]] = {}
    for i, item in enumerate(batch_ops):
        if (
            isinstance(item, dict)
            and item.get("op") == "replace_lines"
            and isinstance(item.get("path"), str)
        ):
            by_file.setdefault(item["path"], []).append(i)

    # Overlap detection — flag conflicts before doing any reorder.
    for path, indices in by_file.items():
        if len(indices) < 2:
            continue
        ranges = []
        for idx in indices:
            item = batch_ops[idx]
            s, e = item.get("start"), item.get("end")
            if not isinstance(s, int) or not isinstance(e, int):
                continue  # let normal dispatch surface type errors
            ranges.append((min(s, e), max(s, e)))
        ranges.sort()
        for i in range(len(ranges) - 1):
            # Treat pure-insert (end < start) as a zero-width range; only
            # error if a true range collides.
            if ranges[i][1] >= ranges[i + 1][0]:
                return batch_ops, (
                    f"batch snapshot: overlapping replace_lines ranges on "
                    f"{path}: [{ranges[i][0]},{ranges[i][1]}] and "
                    f"[{ranges[i + 1][0]},{ranges[i + 1][1]}]"
                )

    # Reorder — for each file with 2+ replace_lines ops, sort descending by
    # start and place back into the same slots.
    if not any(len(v) > 1 for v in by_file.values()):
        return batch_ops, ""
    new_ops = list(batch_ops)
    for _path, indices in by_file.items():
        if len(indices) < 2:
            continue
        items_at = [batch_ops[i] for i in indices]
        items_sorted = sorted(items_at, key=lambda x: -int(x.get("start", 0)))
        for slot, item in zip(indices, items_sorted):
            new_ops[slot] = item
    return new_ops, ""


# Ops whose payload route edits an existing file and cannot create one. The
# roster is the same one `_CREATING_OPS` is the answer to, from the other side.
_PAYLOAD_OPS_THAT_CANNOT_CREATE = ("edit", "replace", "replace_lines", "vim")


def _missing_field_create_clause(op: str, name: str,
                                 lower_payload: Dict[str, Any]) -> str:
    """Say the file is not there, when that is why the field cannot help (#1334).

    `edit:@-` with `path` + `new` and no `old` refuses by naming the field, and
    the field is not the reader's problem: when nothing exists at `path`, no
    spelling of `old` would have worked and `edit` does not create. Filling in a
    `create = true` that `edit` has never had is the shape this was reported in.
    That key used to be dropped in silence one line further on; since #1551 it
    is refused by name, and this clause is appended to that refusal rather than
    replaced by it — "unknown field(s) create" is true and is not the answer.

    Conditioned on the path actually being absent, so a genuine missing-field
    typo on a file that IS there keeps the message it had — naming `paste` there
    would point at whole-file overwrite as the remedy for a forgotten argument,
    which is the `misdirects` trade this must not make.

    **The stat runs behind the containment gate, and that is not optional.**
    This helper is reached from `_at_file_to_parts`, which converts a payload
    into positional parts *before* dispatch applies `_op_path_gate`. Stating
    "there is no file at X" for an X outside the boundary would make the refusal
    text differ by whether an out-of-boundary file exists — an existence oracle
    for exactly the paths every other route refuses to answer about, and one
    this clause would have introduced. Raised in review of the first commit.

    `_containment_error` rather than a second copy of the rule (#882 is the
    filing about what a second copy costs). The tightest boundary is used —
    root=cwd, the default — so an op that dispatch would gate against the repo
    root instead can lose the hint on a path it would have allowed. That is a
    silent hint, not a wrong one, and it is the safe direction.
    """
    if op not in _PAYLOAD_OPS_THAT_CANNOT_CREATE:
        return ""
    path = lower_payload.get("path")
    if not isinstance(path, str) or not path:
        return ""
    if _containment_error([path]):
        return ""
    try:
        if os.path.exists(_expand_home(path)):
            return ""
    except (OSError, ValueError):
        return ""
    return (
        f"\n  and there is no file at {path!r}, so no value of "
        f"'{name}' would have worked — '{op}' edits, it never creates. "
        f"{_create_instead_hint()}"
    )


def _edits_array_clause(op: str, unknown: List[str]) -> str:
    """Say the one-edit-per-payload contract, when the refused key looks
    like a caller reaching for a batch of edits (#1867).

    `edit:@-` refuses an unknown `edits` field correctly, but names only the
    fault — the accepted keys are `old`/`new`/`path`/`replace_all`, none of
    which say "and only one of these tables per payload". The gap is not
    the refusal, it is that the contract behind it is stated nowhere until
    this fires: the batch route (`batch:@file`, one `edit` op per `[[ops]]`
    entry) is the actual answer and was previously left for the caller to
    find by reading `ops` or asking.

    Scoped to `edit` and to a key that plausibly means "more than one edit"
    (`edits`, `edit`, `ops`) rather than firing on every unrelated typo —
    "unknown field(s) foo" already answers a stray key on its own.
    """
    if op != "edit":
        return ""
    if not any(k in ("edits", "edit", "ops") for k in unknown):
        return ""
    return (
        "\n  edit takes ONE edit per payload — for N edits, send N "
        "payloads, or batch:@file with one {op = \"edit\", ...} entry per "
        "edit in [[ops]]."
    )


#: Payload keys that belong to the ROUTE rather than to any op's field list.
#:
#: `op` names a batch sub-item's operation. `literal_backslashes` is the
#: top-level doubled-backslash opt-in (#1096) — and for a single-op payload the
#: top level IS the op's own table, so refusing it here would delete the one
#: spelling that says "I meant two characters". Inside `[[ops]]` it is already
#: refused by `_payload_literal_backslashes_misplaced`, before this runs.
#:
#: Accepted everywhere and advertised nowhere: listing them among an op's
#: fields would read as an invitation to set them per-op, which is what #1096
#: refused to implement.
_PAYLOAD_ROUTE_KEYS = frozenset({"op", _PAYLOAD_LITERAL_BS_KEY})

#: And the same question one level up, at the top of a `batch` payload. The
#: wrapper used to read `ops` and `continue_on_error` and ignore everything
#: else, so a misspelt `continue_on_error` — a flag that decides whether the
#: rest of the batch runs after a failure — was dropped in silence. Found while
#: fixing #1551; the same class, the same route, one level out.
_BATCH_WRAPPER_KEYS = frozenset(
    {"ops", "continue_on_error", _PAYLOAD_LITERAL_BS_KEY})

#: `replace_all` is read at exactly one place in dispatch — the `edit` arm,
#: where true promotes the op to `replace`. On any other op it is inert, so it
#: is an op field of `edit` and an unknown key everywhere else.
_PAYLOAD_REPLACE_ALL_OPS = ("edit",)

#: `no_verify` (#2205) is read at exactly one place — `_at_file_to_parts`
#: below, where `true` appends `git-commit`'s own colon-route sentinel
#: (`--no-verify`) to the positional parts it builds. commit.py's argv has
#: no separate flag channel, so the payload route reaches the same sentinel
#: the colon route already uses rather than inventing a second mechanism.
#: On any other op the key is inert, so it is a field of `git-commit` and an
#: unknown key everywhere else — same shape as `replace_all` above.
_PAYLOAD_NO_VERIFY_OPS = ("git-commit",)
_GIT_COMMIT_NO_VERIFY_TOKEN = "--no-verify"


def _payload_accepted_fields(
        op: str, specs: List[Tuple[str, bool, bool]]) -> List[str]:
    """The op-field names a payload for *op* may carry, in argument order."""
    names = [name for name, _optional, _variadic in specs]
    if op in _PAYLOAD_REPLACE_ALL_OPS:
        names.append("replace_all")
    if op in _PAYLOAD_NO_VERIFY_OPS:
        names.append("no_verify")
    return names


def _payload_unknown_fields(op: str, specs: List[Tuple[str, bool, bool]],
                            lower_payload: Dict[str, Any]) -> List[str]:
    """Keys this op does not implement — refused, never dropped (#1551).

    Every other input surface in this tool already holds this line and says so:
    `_read_op_from_payload` ("unknown field(s) … — accepted: …"),
    `_ordered_batch_fields` for a batch sub-op with no payload route, `ops`'s
    own argument, and the `gh-issues` filter keys, whose contract states it
    outright — "an unrecognised token or filter key is REFUSED, never dropped".
    The mutating payload route was the one that did not, and it is the route
    that WRITES: `edit:@-` carrying `count = 1` ran with no count and answered
    with the ordinary ambiguity refusal, correct about the match and silent
    about the constraint the caller believed they had applied.

    Derived from the registry that drives the route rather than hand-listed, so
    an op whose `syntax` gains a field cannot be refused for using it.
    """
    known = set(_payload_accepted_fields(op, specs)) | _PAYLOAD_ROUTE_KEYS
    return [k for k in lower_payload if k not in known]


def _at_file_to_parts(op: str, payload: Any) -> Tuple[List[str], bool]:
    """Convert a JSON payload dict to (parts, replace_all) for the given op.

    The returned parts list is [op, field1_value, field2_value, ...] matching
    the positional form that the existing dispatch handlers expect.

    All values are coerced to str so the downstream handlers work unchanged.

    replace_all is extracted from the payload and returned separately so the
    dispatch handler can act on it without polluting the parts list.
    """
    if isinstance(payload, list) or (
        isinstance(payload, dict) and isinstance(payload.get("ops"), list)
    ):
        raise ValueError(
            f"this payload is an ops array — use 'batch:@file' instead of "
            f"'{op}:@file' (e.g. batch:@payload.toml)"
        )
    if not isinstance(payload, dict):
        raise ValueError(
            f"@file payload for op '{op}' must be a JSON object, "
            f"got {type(payload).__name__}"
        )
    specs = _at_file_specs(op)
    if not specs:
        raise ValueError(f"@file route not supported for op '{op}'")
    # Case-insensitive key lookup — normalise payload keys once.
    lower_payload = {k.lower(): v for k, v in payload.items()}
    unknown = sorted(_payload_unknown_fields(op, specs, lower_payload))
    if unknown:
        # The create clause rides along rather than being displaced by this
        # refusal. `edit:@-` with `path` + `new` + `create = true` is the shape
        # #1334 was filed about, and "unknown field(s) create" is true but not
        # the answer: what the caller needs is that nothing exists at `path`,
        # that `edit` never creates, and which op does. Naming the key AND the
        # remedy is strictly more than either — dropping the second would trade
        # #1551 for #1334.
        _first_missing = next(
            (name for name, optional, _variadic in specs
             if not optional and name not in lower_payload), "")
        _clause = (_missing_field_create_clause(op, _first_missing,
                                                lower_payload)
                   if _first_missing else "")
        raise ValueError(
            f"@file payload for op '{op}' has unknown field(s) "
            f"{_flat_keys(unknown)} — accepted: "
            f"{', '.join(_payload_accepted_fields(op, specs))}. Refused rather "
            f"than dropped (#1551): an edit performed without the constraint "
            f"the caller wrote reads, in the receipt, exactly like one "
            f"performed with it" + _clause
            + _edits_array_clause(op, unknown)
        )
    parts = [op]
    for name, optional, variadic in specs:
        if name not in lower_payload:
            if optional:
                continue
            raise ValueError(
                f"@file payload for op '{op}' missing required field '{name}'"
                + _missing_field_create_clause(op, name, lower_payload)
            )
        value = lower_payload[name]
        if variadic:
            # Accept a single scalar or a list; each element becomes one
            # positional part (e.g. git-commit paths → PATH PATH ...). A null
            # value or null elements are dropped, so paths:null / paths:[]
            # cleanly omit rather than emitting a literal "None" arg.
            if value is None:
                items: List[Any] = []
            elif isinstance(value, list):
                items = value
            else:
                items = [value]
            parts.extend(str(v) for v in items if v is not None)
        else:
            parts.append(str(value))
    replace_all = bool(lower_payload.get("replace_all", False))
    # #2205 — `no_verify = true` reaches the same sentinel the colon route
    # already parses out of `paths` (`_NO_VERIFY_TOKEN` in commit.py), so
    # commit.py has exactly one place that reads it regardless of route.
    if op in _PAYLOAD_NO_VERIFY_OPS and bool(lower_payload.get("no_verify", False)):
        parts.append(_GIT_COMMIT_NO_VERIFY_TOKEN)
    return parts, replace_all


#: The one payload key the generic preset route (#1165) accepts.
_GENERIC_PRESET_ARGS_KEY = "args"


def _at_file_to_parts_generic(op: str, payload: Any) -> Tuple[List[str], bool]:
    """Generic @file/@- route for a preset op with no named-field registry
    (#1165): payload = `{args = [...]}`. Each element becomes ONE positional
    part, verbatim -- no colon split, no rejoin heuristic, no re-tokenizing
    of any kind.

    This is the escape hatch `_at_file_to_parts` provides for `edit`,
    `git-commit` and the rest of `_AT_FILE_REGISTRY`, extended to every
    preset op that never got a named-field entry because its colon syntax
    is mode-based rather than a flat field list (`gh-job:ID:grep:PATTERN`,
    not `edit:::OLD:::NEW:::PATH`). `gh-job:ID:grep:PATTERN`'s colon CLI
    rejoins everything after the mode (#1145) -- correct for the ordinary
    case, but a pattern that must genuinely END in ':', or one whose
    intended reading disagrees with the rejoin, had no way to be spelled at
    all. `args` sidesteps the question entirely: the caller states the exact
    argv the op receives, and `_resolve_custom_op`'s existing `{args}`
    substitution still `shlex.quote`s each entry, so nothing here needs its
    own quoting logic.

    One key only, and it is required -- no optional/variadic bookkeeping,
    because there are no named fields to be optional or variadic about.
    """
    if isinstance(payload, list) or (
        isinstance(payload, dict) and isinstance(payload.get("ops"), list)
    ):
        raise ValueError(
            f"this payload is an ops array — use 'batch:@file' instead of "
            f"'{op}:@file' (e.g. batch:@payload.toml)"
        )
    if not isinstance(payload, dict):
        raise ValueError(
            f"@file payload for op '{op}' must be a JSON object, "
            f"got {type(payload).__name__}"
        )
    lower_payload = {k.lower(): v for k, v in payload.items()}
    unknown = sorted(k for k in lower_payload if k != _GENERIC_PRESET_ARGS_KEY)
    if unknown:
        raise ValueError(
            f"@file payload for op '{op}' has unknown field(s) "
            f"{_flat_keys(unknown)} — accepted: {_GENERIC_PRESET_ARGS_KEY} "
            f"(a list of strings, one per positional argument). Refused "
            f"rather than dropped (#1551's reasoning applies here too): a "
            f"call run with an argument nobody read reads, in the receipt, "
            f"exactly like one that was."
        )
    if _GENERIC_PRESET_ARGS_KEY not in lower_payload:
        raise ValueError(
            f"@file payload for op '{op}' missing required field "
            f"'{_GENERIC_PRESET_ARGS_KEY}' (a list of strings, one per "
            f"positional argument)"
        )
    value = lower_payload[_GENERIC_PRESET_ARGS_KEY]
    if isinstance(value, str):
        items: List[Any] = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValueError(
            f"@file payload for op '{op}' field '{_GENERIC_PRESET_ARGS_KEY}' "
            f"must be a string or a list of strings, got "
            f"{type(value).__name__}"
        )
    parts = [op] + [str(v) for v in items if v is not None]
    return parts, False
