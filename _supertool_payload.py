

















from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_payload.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )








from typing import Union


def _detect_payload_format(raw: str) -> str:






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




































    result: Dict[str, Any] = {}


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





















_TOML_KEY_SEGMENT = r"(?:[A-Za-z0-9_-]+|\"[^\"\n]*\"|'[^'\n]*')"
_TOML_NEXT_STATEMENT = re.compile(
    r"\[\[?[^\n]*\]\]?"
    r"|" + _TOML_KEY_SEGMENT + r"(?:\." + _TOML_KEY_SEGMENT + r")*[ \t]*="
)


def _toml_skip_blank_and_comments(raw: str, i: int) -> int:





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
















        line_start = raw.rfind(chr(10), 0, run) + 1
        before = raw[line_start:run]
        if before.strip():
            beyond = _toml_skip_blank_and_comments(raw, nxt)
            if beyond < len(raw) and not _TOML_NEXT_STATEMENT.match(raw, beyond):
                return run
        at = nxt


def _toml_delimiter_hint(raw: str) -> str:


























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






_INVOCATION_DIR: Optional[str] = None
_CWD_SHIFT: Optional[str] = None       


def _at_root() -> str:







    return _INVOCATION_DIR or os.getcwd()


def _resolve_at_path(rel: str) -> str:





























    if os.path.isabs(rel):
        return rel
    return os.path.join(_at_root(), rel)


def _at_file_missing_msg(rel: str) -> str:









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




_TOML_LITERAL_TAIL_CHARS = 48


def _toml_literal_backslash_message(head: str, run: int) -> str:

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





_PAYLOAD_DBS_MAX_FIELDS = 3













_PAYLOAD_DBS_MAX_OCCURRENCES = 4





_PAYLOAD_DBS_CONTEXT = 22







_PAYLOAD_DBS_MAX_RUN_DRAWN = 20



















_EVEN_BACKSLASH_RUN = re.compile(r"(?<!\\)(?:\\\\)+(?!\\)")


_PAYLOAD_KEY_BEFORE_VALUE = re.compile(r"([A-Za-z0-9_.\-]+)[ \t]*=[ \t]*$")






_PAYLOAD_WARNINGS: List[str] = []






_TOML_OPS_HEADER = re.compile(r"(?m)^[ \t]*\[\[[ \t]*ops[ \t]*\]\]")


def _dbs_occurrences(raw: str, offset: int,
                     content: str) -> List[Tuple[int, int, str, Optional[int], int]]:
























    out: List[Tuple[int, int, str, Optional[int], int]] = []
    for m in _EVEN_BACKSLASH_RUN.finditer(content):
        at_abs = offset + m.start()





        run = m.end() - m.start()
        line_no = raw.count(chr(10), 0, at_abs) + 1
        col = at_abs - (raw.rfind(chr(10), 0, at_abs) + 1) + 1
        start = content.rfind(chr(10), 0, m.start()) + 1
        stop = content.find(chr(10), m.start())
        line = content[start:] if stop < 0 else content[start:stop]
        at = m.start() - start
        lo = max(0, at - _PAYLOAD_DBS_CONTEXT)
        drawn = min(run, _PAYLOAD_DBS_MAX_RUN_DRAWN)



        after = _PAYLOAD_DBS_CONTEXT if drawn == run else 0
        hi = min(len(line), at + drawn + after)




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













            findings.append(
                (key, label,
                 _flat_field(line.strip())[:_TOML_LITERAL_TAIL_CHARS], total,
                 _dbs_occurrences(raw, i + 3, content)))
        i = raw.find(opener, nxt)
    return findings


def _payload_double_backslash_note(raw: str) -> str:




























































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





        at = (", first at payload line " + str(occs[0][0])) if occs else ""
        lines.append(
            "  " + arrow + " `" + label + "` (" + str(total) + " even backslash run"
            + ("" if total == 1 else "s") + at + "): " + line + chr(10)
        )
    rest = findings[_PAYLOAD_DBS_MAX_FIELDS:]
    if rest:




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





















_PAYLOAD_DBS_WRITE_KEYS = frozenset({"new", "content", "message"})


_PAYLOAD_LITERAL_BS_KEY = "literal_backslashes"


def _payload_literal_backslashes_scope(parsed: Any) -> Union[bool, FrozenSet[str]]:






























    if not isinstance(parsed, dict):
        return frozenset()
    value = parsed.get(_PAYLOAD_LITERAL_BS_KEY)
    if value is True:
        return True
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return frozenset(v.lower() for v in value)
    return frozenset()


def _payload_literal_backslashes_misplaced(parsed: Any) -> str:













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

















    seen: List[str] = [k.lower() for k in already]
    for f in findings:
        key = f[0].lower()
        if key not in seen:
            seen.append(key)
    return "[" + ", ".join(chr(34) + k + chr(34) for k in seen) + "]"











_SHELL_QUOTE_ESCAPE_FORMS = (
    "'" + chr(34) + "'" + chr(34) + "'",   
    "'" + chr(92) + "''",                  
)


def _toml_literal_shell_quote_escape_findings(
        raw: str) -> List[Tuple[str, str, str, int, int]]:


















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




                at_abs = (i + 3) + hit_at
                line_no = raw.count(chr(10), 0, at_abs) + 1
                findings.append(
                    (key, label, _flat_field(line.strip())[:_TOML_LITERAL_TAIL_CHARS],
                     total, line_no))
        i = raw.find(opener, nxt)
    return findings


def _payload_shell_quote_escape_refusal(parsed: Any, raw: str) -> str:


































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





























    scope = _payload_literal_backslashes_scope(parsed)
    if not scope:
        return {}
    by_path: Dict[str, List[Tuple[str, int, int, str, int]]] = {}
    for key, label, _line, _total, occs in _toml_literal_double_backslashes(raw):
        lk = key.lower()
        if lk not in _PAYLOAD_DBS_WRITE_KEYS:
            continue
        if not (scope is True or lk in scope):
            continue  
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





    for _key, label, _line, total, occs in findings[_PAYLOAD_DBS_MAX_FIELDS:]:




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









    for m in _TRAILING_BACKSLASH_RUN.finditer(text):
        if m.group(2) or len(m.group(1)) % 2:
            continue
        start = text.rfind(chr(10), 0, m.start()) + 1
        return text[start:m.start()], len(m.group(1))
    return None


def _sh_eol_backslash_message(line: str, run: int) -> str:

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









    if not _PAYLOAD_WARNINGS:
        return ""
    out = "".join(_PAYLOAD_WARNINGS)
    _PAYLOAD_WARNINGS.clear()
    return out


_AT_FILE_REST_MARKER_RE = re.compile(
    r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*@rest[ \t]*\r?$", re.MULTILINE)










_AT_FILE_REST_RAW_FIELDS = {"content"}


def _rest_tail_value(field: str, tail: str) -> str:




    if field.lower() in _AT_FILE_REST_RAW_FIELDS:
        return tail
    if tail.endswith("\r\n"):
        return tail[:-2]
    if tail.endswith("\n"):
        return tail[:-1]
    return tail


def _load_at_file(ref: str, note: bool = True) -> Any:




    parsed, _raw, _source = _load_at_file_raw(ref, note=note)
    return parsed


def _load_at_file_raw(ref: str, note: bool = True) -> "Tuple[Any, str, str]":






















    if ref == "@payload" or ref.startswith("@payload "):






        raise ValueError(
            "'@payload' is a header placeholder, not a reference. This op ran "
            "from an @payload whose fields no single-colon header can reproduce "
            "(#644) — re-run it from the original payload file or stdin."
        )
    if ref == "@-":
        raw = sys.stdin.read()
        source = "<stdin>"
    else:
        fpath = ref[1:]  
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
        import tomllib  
        parser = tomllib.loads
    except ImportError:
        parser = _mini_toml_loads























    rest_field = None
    toml_source = raw
    rest_marker = _AT_FILE_REST_MARKER_RE.search(raw)
    if rest_marker and re.search(
            r"^[ \t]*\[", raw[:rest_marker.start()], re.MULTILINE):












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


_PAYLOAD_LINT_Q3 = chr(39) * 3   
_PAYLOAD_LINT_QQQ = chr(34) * 3  


def _payload_field_provenance(raw: str, key: str) -> str:









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













    if not isinstance(value, str):
        return f"  {key!r}: {type(value).__name__} = {value!r} (not text)"
    n_lines = value.count(chr(10)) + 1 if value else 0
    prov = _payload_field_provenance(raw, key)
    flags = []
    if "\\" in value:
        flags.append("carries a literal backslash")
    if "\r" in value:
        flags.append("carries a stray \\r")




    _no_cr = [ln[:-1] if ln.endswith(chr(13)) else ln
              for ln in value.split(chr(10))]
    if any(ln != ln.rstrip() for ln in _no_cr):
        flags.append("has a trailing-whitespace line")
    line = f"  {key!r}: {len(value)} chars, {n_lines} line(s), provenance={prov}"
    if flags:
        line += " -- " + ", ".join(flags)
    return line


def _payload_lint_anchor_report(parsed: Dict[str, Any]) -> str:








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















    if not ref.startswith("@"):
        return ("ERROR: payload-lint takes an @reference (@file or @-), "
                "e.g. payload-lint:@edits.toml\n")
    try:
        parsed, raw, source = _load_at_file_raw(ref, note=False)
    except ValueError as exc:
        return f"ERROR: {exc}\n"









    contained = _containment_error(_payload_lint_path_candidates(parsed))
    if contained:
        return contained

    lines = [f"payload-lint: {source}"]

    def _payload_lint_show_path(value: object) -> str:













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





_AT_FILE_REGISTRY: Dict[str, List[Tuple[str, bool, bool]]] = {}
_AT_FILE_REGISTRY_BUILT: bool = False












_AT_FILE_DROPPED_ROUTES: List[Tuple[str, str]] = []


def _fields_from_syntax(syntax: str) -> List[Tuple[str, bool, bool]]:

























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






_READ_OP_AT_FIELDS: Dict[str, Tuple[str, ...]] = {
    "grep":        ("pattern", "path", "limit", "context", "count", "no_auto_read", "full"),
    "around":      ("pattern", "path", "n"),
    "grep_around": ("pattern", "path", "n", "limit"),
    "between":     ("symbol", "start", "end", "path"),
    "read":        ("path", "offset", "limit", "grep", "full"),







    "validate":    ("path", "paths", "tools", "verbose"),
}


def _payload_strlist(p: Dict[str, Any], key: str) -> List[str]:







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







    value = p.get("limit")




    if value == _GREP_ALL_TOKEN:
        return GREP_LIMIT_ALL
    return _payload_int(p, "limit", default)


def _payload_bool(p: Dict[str, Any], key: str) -> bool:
    value = p.get(key, False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _read_op_from_payload(op: str, payload: Any, no_exclude: bool = False) -> str:











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














        contained = _containment_error(
            [*_payload_strlist(p, "path"), *_payload_strlist(p, "paths")])
        if contained:
            return contained




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
            return "ERROR: @payload for the 'read' op is missing required field 'path'\n"
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



                continue
            if op_name in _READ_OP_AT_FIELDS:






                continue
            syntax = info.get("syntax", "")
            if not syntax:
                continue
            fields = _fields_from_syntax(syntax)
            if fields:
                registry[op_name] = fields
            elif ":::" in re.split(r"\s*\|\s*", syntax)[0]:



                dropped.append((op_name, syntax))
    _AT_FILE_REGISTRY = registry
    _AT_FILE_DROPPED_ROUTES[:] = dropped
    _AT_FILE_REGISTRY_BUILT = True


def _at_file_specs(op: str) -> List[Tuple[str, bool, bool]]:

    _build_at_file_registry()
    return _AT_FILE_REGISTRY.get(op, [])


def _at_file_dropped_routes() -> List[Tuple[str, str]]:



    _build_at_file_registry()
    return list(_AT_FILE_DROPPED_ROUTES)


def _at_file_fields(op: str) -> List[str]:





    return [name for name, _opt, _var in _at_file_specs(op)]


def _generic_preset_payload_hint(op: str) -> str:





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




























    specs = _at_file_specs(op)
    if not specs:
        named = _PRESET_NAMED_PAYLOAD_FIELDS.get(op)
        if named:
            return _preset_named_payload_hint(op, named)
        if _op_is_preset_op(op):
            return _generic_preset_payload_hint(op)
        return ""
    quote = "'" * 3





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





        lines.append(
            "    One edit per payload — for N edits, send N payloads, or "
            "batch:@file with one {op = \"edit\", ...} entry per edit in "
            "[[ops]]."
        )
    return chr(10) + chr(10).join(lines)


def _stdin_ref_in_value_field(op: str, parts: List[str]) -> str:











































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








    _build_at_file_registry()
    return sorted(set(_AT_FILE_REGISTRY) | set(_READ_OP_AT_FIELDS))


def _help_payload_route(op: str) -> str:

















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




















    by_file: Dict[str, List[int]] = {}
    for i, item in enumerate(batch_ops):
        if (
            isinstance(item, dict)
            and item.get("op") == "replace_lines"
            and isinstance(item.get("path"), str)
        ):
            by_file.setdefault(item["path"], []).append(i)


    for path, indices in by_file.items():
        if len(indices) < 2:
            continue
        ranges = []
        for idx in indices:
            item = batch_ops[idx]
            s, e = item.get("start"), item.get("end")
            if not isinstance(s, int) or not isinstance(e, int):
                continue  
            ranges.append((min(s, e), max(s, e)))
        ranges.sort()
        for i in range(len(ranges) - 1):


            if ranges[i][1] >= ranges[i + 1][0]:
                return batch_ops, (
                    f"batch snapshot: overlapping replace_lines ranges on "
                    f"{path}: [{ranges[i][0]},{ranges[i][1]}] and "
                    f"[{ranges[i + 1][0]},{ranges[i + 1][1]}]"
                )



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




_PAYLOAD_OPS_THAT_CANNOT_CREATE = ("edit", "replace", "replace_lines", "vim")


def _missing_field_create_clause(op: str, name: str,
                                 lower_payload: Dict[str, Any]) -> str:





























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















    if op != "edit":
        return ""
    if not any(k in ("edits", "edit", "ops") for k in unknown):
        return ""
    return (
        "\n  edit takes ONE edit per payload — for N edits, send N "
        "payloads, or batch:@file with one {op = \"edit\", ...} entry per "
        "edit in [[ops]]."
    )













_PAYLOAD_ROUTE_KEYS = frozenset({"op", _PAYLOAD_LITERAL_BS_KEY})






_BATCH_WRAPPER_KEYS = frozenset(
    {"ops", "continue_on_error", _PAYLOAD_LITERAL_BS_KEY})




_PAYLOAD_REPLACE_ALL_OPS = ("edit",)








_PAYLOAD_NO_VERIFY_OPS = ("git-commit",)
_GIT_COMMIT_NO_VERIFY_TOKEN = "--no-verify"


def _payload_accepted_fields(
        op: str, specs: List[Tuple[str, bool, bool]]) -> List[str]:

    names = [name for name, _optional, _variadic in specs]
    if op in _PAYLOAD_REPLACE_ALL_OPS:
        names.append("replace_all")
    if op in _PAYLOAD_NO_VERIFY_OPS:
        names.append("no_verify")
    return names


def _payload_unknown_fields(op: str, specs: List[Tuple[str, bool, bool]],
                            lower_payload: Dict[str, Any]) -> List[str]:















    known = set(_payload_accepted_fields(op, specs)) | _PAYLOAD_ROUTE_KEYS
    return [k for k in lower_payload if k not in known]


def _at_file_to_parts(op: str, payload: Any) -> Tuple[List[str], bool]:










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

    lower_payload = {k.lower(): v for k, v in payload.items()}
    unknown = sorted(_payload_unknown_fields(op, specs, lower_payload))
    if unknown:







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



    if op in _PAYLOAD_NO_VERIFY_OPS and bool(lower_payload.get("no_verify", False)):
        parts.append(_GIT_COMMIT_NO_VERIFY_TOKEN)
    return parts, replace_all



_GENERIC_PRESET_ARGS_KEY = "args"


def _at_file_to_parts_generic(op: str, payload: Any) -> Tuple[List[str], bool]:





















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
