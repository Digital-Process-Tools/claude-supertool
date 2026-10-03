


























from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_grep.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )




_REGEX_METACHARS = re.compile(r"[()\[\]{}|.*+?^$\\]")


def _is_regexy(pattern: str) -> bool:



    return bool(_REGEX_METACHARS.search(pattern))


def _literal_note(pattern: str, count: int) -> str:



    return (f"(no regex match; showing {count} literal "
            f"match(es) for {pattern!r})\n")







_PATTERN_QUOTE_CHARS = ("'", chr(34), "`")


def _unwrapped_pattern(pattern: str) -> str:







    if len(pattern) < 3:
        return ""
    quote = pattern[0]
    if quote not in _PATTERN_QUOTE_CHARS or pattern[-1] != quote:
        return ""
    inner = pattern[1:-1]
    if quote in inner:
        return ""
    return inner


def _quoted_pattern_note(pattern: str, inner: str,
                         inner_matches: bool) -> str:














    quote = pattern[0]
    lead = (f"(the pattern {pattern!r} begins and ends with `{quote}`, and "
            f"both were searched as literal text rather than read as "
            f"quoting. ")
    if inner_matches:
        return lead + (f"Without them, {inner!r} DOES match here — this zero "
                       f"is about the quotes, not about what was scanned.)"
                       + chr(10))
    return lead + (f"Without them, {inner!r} matches nothing here either — "
                   f"the quotes are not why this is zero.)" + chr(10))


def _compile_lenient(pattern: str) -> "re.Pattern[str]":





    try:
        return re.compile(pattern)
    except re.error:
        return re.compile(re.escape(pattern))


def _quote_pair_note(pattern: str, probe: Callable[[str], object]) -> str:








    inner = _unwrapped_pattern(pattern)
    if not inner:
        return ""
    try:
        matches = bool(probe(inner))
    except (re.error, OSError, UnicodeError):





        return ""
    return _quoted_pattern_note(pattern, inner, matches)


def _case_insensitive_note(pattern: str, probe: Callable[[str], object]) -> str:

























    if not any(c.isalpha() for c in pattern):
        return ""
    try:
        matches = bool(probe("(?i)" + pattern))
    except (re.error, OSError, UnicodeError):



        return ""
    if not matches:
        return ""





    return (f"(grep is case-sensitive; {pattern!r} DOES match here when "
            f"searched case-insensitively -- this zero may be about "
            f"capitalisation, not absence. Re-run as "
            f"grep:(?i){pattern}:PATH for a case-insensitive search -- no "
            f"quotes around the pattern, which grep searches literally.)"
            + chr(10))













_ERE_UNSAFE = re.compile(r"\\[^.^$*+?()\[\]{}|\\/-]|\\$|\(\?|[*+?}]\?|\[[:.=]")








_BRE_ALT = "\\|"


def _bre_alternation_rewrite(pattern: str) -> Tuple[str, bool]:

    if _BRE_ALT not in pattern:
        return pattern, False
    return pattern.replace(_BRE_ALT, "|"), True


def _top_level_branches(pattern: str) -> List[str]:






    branches: List[str] = []
    depth = 0
    in_class = False
    start = 0
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif c == "|" and depth == 0:
            branches.append(pattern[start:i])
            start = i + 1
        i += 1
    branches.append(pattern[start:])
    return branches
















_SATURATION_PROBES = ("", "x", "supertool 42", "  ")


def _branch_matches_everything(branch: str) -> bool:

    if branch == "":
        return True
    try:
        regex = re.compile(branch)
    except re.error:


        return False
    return all(regex.search(probe) is not None for probe in _SATURATION_PROBES)


def _saturating_branch(pattern: str) -> Optional[str]:






    if "|" not in pattern:
        return None
    branches = _top_level_branches(pattern)
    if len(branches) < 2:
        return None
    for branch in branches:
        if _branch_matches_everything(branch):
            return branch
    return None


def _saturates(pattern: str) -> bool:


    return _saturating_branch(pattern) is not None


def _saturating_pattern_refusal(written: str, effective: str,
                                rewritten: bool) -> str:







    branch = _saturating_branch(effective)
    if branch is None:
        return ""
    if branch == "":
        why = ("has an empty alternation branch, so it matches every line of "
               "every file scanned")
    else:
        why = (f"has an alternation branch `{branch}` that matches the empty "
               f"string, so the whole pattern matches every line of every "
               f"file scanned")



    lines = [
        f"ERROR: pattern `{effective}` {why}. That is a saturated pattern, not "
        f"a search, and its result count is indistinguishable from a search "
        f"that genuinely found a lot.",
    ]
    if rewritten:
        became = ("a bare `|` with nothing to its left" if branch == ""
                  else "a top-level `|` that split the pattern into branches")
        lines.append(
            f"  written as `{written}` — supertool rewrites bash-grep BRE "
            f"alternation, so `{_BRE_ALT}` became {became}.")
        lines.append(
            f"  for a literal `|`, use a character class: "
            f"`{written.replace(_BRE_ALT, '[|]')}`")
    else:
        lines.append(
            "  for a literal `|`, use a character class: `[|]`")
    return chr(10).join(lines) + chr(10)


def _bre_rewrite_note(written: str, effective: str, rewritten: bool) -> str:






    if not rewritten:
        return ""
    return (f"(pattern rewritten to `{effective}` — `{_BRE_ALT}` is bash-grep "
            f"BRE alternation and became a plain `|`. For a literal `|`, use "
            f"`[|]`.)" + chr(10))


def _pattern_gate(
    pattern: str, check_saturation: bool = True
) -> Tuple[str, str, str]:
























































    if len(pattern) > 1000:
        return pattern, (
            f"ERROR: pattern too long ({len(pattern)} > 1000 chars)\n"
        ), ""
    if _has_outer_wrapped_unbounded_group(pattern):
        return pattern, (
            "ERROR: pattern contains nested unbounded quantifiers "
            f"({pattern!r}) — would risk catastrophic backtracking. "
            "Rewrite without `(...+)+`-style nesting.\n"
        ), ""
    effective, rewritten = _bre_alternation_rewrite(pattern)
    if check_saturation:
        refusal = _saturating_pattern_refusal(pattern, effective, rewritten)
        if refusal:
            return effective, refusal, ""
    return effective, "", _bre_rewrite_note(pattern, effective, rewritten)


def _pattern_read_as_note(pattern: str, path: str, op: str = "grep") -> str:





























    if ":" not in pattern:
        return ""
    note = (f"(pattern read as {pattern!r}, path as {path!r} — the ':' is part "
            f"of the regex, not a separator. Use {op}:@- with a `pattern` key "
            "if the split was meant to fall elsewhere.)" + chr(10))
    if pattern.startswith("re:"):
        note += (f"({op} has no `re:` prefix — every {op} pattern is already a "
                 "regex, so `re:` is literal text and forms part of the first "
                 "alternation branch. `between:re:START:END:PATH` is the op "
                 "that has one.)" + chr(10))
    return note


def _count_lines(path: str, on_error: int = 0) -> int:









    try:
        count = 0
        last = b""
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                count += chunk.count(b"\n")
                last = chunk
        if last == b"":
            return 0  

        if not last.endswith(b"\n"):
            count += 1
        return count
    except OSError:
        return on_error







_GREP_ALL_OUTSIDE_LIMIT_SLOT = (
    "ERROR: grep read `all` outside the LIMIT slot (grep:PATTERN:PATH:LIMIT:"
    "CONTEXT). `all` is a LIMIT — it removes the result cap so a sweep is "
    "complete; CONTEXT is a number of lines around each match and has no "
    "`all`, and nothing follows CONTEXT. Did you mean "
    "grep:PATTERN:PATH:all:CONTEXT?" + chr(10)
)




_GREP_AROUND_ALL_IN_N_SLOT = (
    "ERROR: grep_around takes PATTERN:PATH:N:LIMIT — context first, the "
    "opposite order to grep's LIMIT:CONTEXT — so `all` landed in the N slot. "
    "Did you mean grep_around:PATTERN:PATH:N:all (e.g. "
    "grep_around:PATTERN:PATH:3:all), or grep:PATTERN:PATH:all:N?" + chr(10)
)














def _grep_zero_limit() -> str:
    default = _get_op_int("grep", "max_results", MAX_GREP_RESULTS)
    return (
        'ERROR: grep LIMIT 0 is not "unlimited" here, and supertool will not '
        "guess which of the two it meant. Uncapped output would land in the "
        "caller's context before it could be declined, and silently "
        "substituting the default would mean the LIMIT that ran was never the "
        "LIMIT that was typed. Both readings have a spelling: "
        f"grep:PATTERN:PATH:{_GREP_ALL_TOKEN} for every match, or omit LIMIT "
        f"for the default of {default} (grep:PATTERN:PATH:200 for any other "
        "cap)." + chr(10)
    )


def op_grep(pattern: str, path: str = ".", limit: int = 0,
            context: int = 0, count_only: bool = False,
            no_exclude: bool = False, no_auto_read: bool = False,
            via_payload: bool = False, full: bool = False) -> str:












    _effective, refusal, note = _pattern_gate(pattern)
    if refusal:
        return refusal
    prefix = "" if via_payload else _pattern_read_as_note(pattern, path, "grep")
    return (prefix
            + note
            + _op_grep(pattern, path, limit, context, count_only,
                       no_exclude, no_auto_read, full))


def _grep_limit_and_label(limit: int) -> Tuple[int, str]:







    if limit == GREP_LIMIT_ALL:
        return sys.maxsize, _GREP_ALL_TOKEN
    if limit <= 0:
        limit = _get_op_int("grep", "max_results", MAX_GREP_RESULTS)
    return limit, str(limit)


def _has_outer_wrapped_unbounded_group(pattern: str) -> bool:






































    n = len(pattern)

    def _skip_class(k: int) -> int:

        if k < n and pattern[k] == "^":
            k += 1
        if k < n and pattern[k] == "]":
            k += 1
        while k < n and pattern[k] != "]":
            if pattern[k] == "\\":
                k += 1
            k += 1
        return k + 1

    i = 0
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            i = _skip_class(i + 1)
            continue
        if c == "(":
            depth = 1
            j = i + 1
            has_unbounded = False
            while j < n and depth:
                cj = pattern[j]
                if cj == "\\":
                    j += 2
                    continue
                if cj == "[":
                    j = _skip_class(j + 1)
                    continue
                if cj == "(":
                    depth += 1
                elif cj == ")":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                elif cj in "+*":
                    has_unbounded = True
                j += 1
            if has_unbounded and j < n and pattern[j] in "+*":
                return True
            i += 1
            continue
        i += 1
    return False


def _op_grep(pattern: str, path: str = ".", limit: int = 0,
             context: int = 0, count_only: bool = False,
             no_exclude: bool = False, no_auto_read: bool = False,
             full: bool = False) -> str:

















    unlimited = limit == GREP_LIMIT_ALL
    limit, limit_label = _grep_limit_and_label(limit)
    if not pattern:
        return "ERROR: empty pattern\n"




    if len(pattern) > 1000:
        return f"ERROR: pattern too long ({len(pattern)} > 1000 chars)\n"















    if _has_outer_wrapped_unbounded_group(pattern):
        return (
            "ERROR: pattern contains nested unbounded quantifiers "
            f"({pattern!r}) — would risk catastrophic backtracking. "
            "Rewrite without `(...+)+`-style nesting.\n"
        )





    pattern, _ = _bre_alternation_rewrite(pattern)


    if path != "." and not os.path.isfile(path) and not os.path.isdir(path):

        from glob import glob as _glob
        if not _glob(path, recursive=True):




            _swap = _swap_suggest(
                "grep", "PATTERN:PATH", "pattern", pattern, path,
                f"grep:{path}:{pattern}")
            return _path_not_found(path, op="grep", suggest=_swap,
                                   call_prefix=f"grep:{pattern}")

    excl = _get_exclude_paths("grep", no_exclude)



















    if (not count_only and context == 0 and not unlimited
            and _rtk_enabled() and _has_rtk()
            and not _ERE_UNSAFE.search(pattern)):
        _, multi = _split_exclude_prefixes(excl)
        if not multi and not _gitignore_residual(path, excl):


            rtk_args = ["grep", "-rn", "-E", "-m", str(limit + 1)]
            rtk_args.extend(_grep_exclude_flags(excl))
            rtk_args.extend([pattern, path])
            rtk_out = _rtk_run(rtk_args)
            if rtk_out is not None and rtk_out.strip():
                rtk_out, rtk_dropped = _rtk_drop_excluded(rtk_out, excl)
                if not rtk_dropped:



                    return _rtk_grep_report(
                        rtk_out, limit,
                        census=lambda: _rtk_grep_census(pattern, path, excl))



















    hidden_files: List[str] = []
    git_tally = _GitIgnoreTally()
    candidates = _grep_candidates(path, excl, hidden_files, git_tally)
    scanned = len(candidates)
    hidden = _hidden_suffix(len(hidden_files)) + git_tally.clause()

    if count_only:
        counts = _grep_count(pattern, path, limit, excl, candidates=candidates)
        literal_note = ""
        if not counts and _is_regexy(pattern):
            counts = _grep_count(re.escape(pattern), path, limit, excl, candidates=candidates)
            if counts:
                literal_note = _literal_note(pattern, sum(counts.values()))
        total = sum(counts.values())
        file_count = len(counts)
        out = [literal_note,
               f"({total} total matches across {file_count} files{_scanned_suffix(scanned)}{hidden})\n"]
        if total == 0:
            out.append(_shim_facade_note(path))





            out.append(_quote_pair_note(pattern, lambda inner: _grep_recursive(
                inner, path, 1, excl, candidates=candidates)))
            out.append(_case_insensitive_note(pattern, lambda p: _grep_recursive(
                p, path, 1, excl, candidates=candidates)))




        for fp, cnt in sorted(counts.items()):
            out.append(f"{_fwd(fp)}: {cnt} match{'' if cnt == 1 else 'es'}" + chr(10))
        out.append("\n")
        return "".join(out)

    if context > 0:
        ceiling = _grep_count_ceiling(limit)
        groups = _grep_recursive_context(
            pattern, path, ceiling + 1, context, excl, candidates=candidates)
        literal = False
        if not groups and _is_regexy(pattern):
            groups = _grep_recursive_context(
                re.escape(pattern), path, ceiling + 1, context, excl, candidates=candidates)
            literal = bool(groups)
        total, capped = _grep_total(
            sum(1 for g in groups for line in g if line[2] == "match"), ceiling)
        groups, truncated = _trim_context_groups(groups, limit)
        count = sum(
            1 for g in groups for line in g if line[2] == "match"
        )
        literal_note = _literal_note(pattern, count) if literal else ""
        file_count = len({g[0][0] for g in groups if g})
        out = [literal_note, f"({count} results in {file_count} files{_scanned_suffix(scanned)}{hidden}, "
               f"limit {limit_label}, context {context}"
               f"{_truncation_suffix(truncated, total, capped)})\n"]
        if count == 0:
            out.append(_shim_facade_note(path))
            out.append(_quote_pair_note(
                pattern, lambda inner: _grep_recursive_context(
                    inner, path, 1, context, excl, candidates=candidates)))
            out.append(_case_insensitive_note(
                pattern, lambda p: _grep_recursive_context(
                    p, path, 1, context, excl, candidates=candidates)))
        current_file: str = ""
        first_group = True
        cut: List[Tuple[str, int]] = []
        for group in groups:
            group_file = group[0][0] if group else ""
            if group_file != current_file:
                current_file = group_file
                out.append(f"{current_file}\n")
                first_group = True  
            if not first_group:
                out.append("  --\n")
            first_group = False
            for _fp, lineno, kind, content in group:
                if len(content) > _grep_line_cap():
                    cut.append((_fp, lineno))
                capped = _cap_grep_line(content, full)
                if kind == "match":
                    out.append(f"  {lineno}:{capped}\n")
                else:
                    out.append(f"  {lineno}-{capped}\n")
        _note = _grep_full_note(cut) if full else _grep_cut_note(cut)
        if _note:
            out.insert(2, _note)
        out.append("\n")
        return _cap_context_window("".join(out), "grep_around")















    ceiling = _grep_count_ceiling(limit)
    hits = _grep_recursive(pattern, path, ceiling + 1, excl, candidates=candidates)
    literal = False
    if not hits and _is_regexy(pattern):
        hits = _grep_recursive(re.escape(pattern), path, ceiling + 1, excl, candidates=candidates)
        literal = bool(hits)
    truncated = len(hits) > limit
    total, capped = _grep_total(len(hits), ceiling)
    hits = hits[:limit]
    count = len(hits)
    literal_note = _literal_note(pattern, count) if literal else ""
    file_count = len({fp for fp, _, _ in hits})

    out = [literal_note,
           f"({count} results in {file_count} files{_scanned_suffix(scanned)}{hidden}, "
           f"limit {limit_label}{_truncation_suffix(truncated, total, capped)})\n"]
    if count == 0:
        out.append(_shim_facade_note(path))
        out.append(_quote_pair_note(pattern, lambda inner: _grep_recursive(
            inner, path, 1, excl, candidates=candidates)))
        out.append(_case_insensitive_note(pattern, lambda p: _grep_recursive(
            p, path, 1, excl, candidates=candidates)))
    current_file = ""
    cut: List[Tuple[str, int]] = []
    for fp, lineno, content in hits:
        if fp != current_file:
            current_file = fp
            out.append(f"{fp}\n")
        if len(content) > _grep_line_cap():
            cut.append((fp, lineno))
        out.append(f"  {lineno}:{_cap_grep_line(content, full)}\n")
    _note = _grep_full_note(cut) if full else _grep_cut_note(cut)
    if _note:
        out.insert(2, _note)
    out.append("\n")




    if (not no_auto_read
            and count > 0
            and os.path.isfile(path)
            and os.path.getsize(path) < _get_op_int("read", "max_bytes", MAX_READ_BYTES)):
        line_cap = _get_op_int("read", "max_autoread_lines", MAX_AUTOREAD_LINES)
        if _count_lines(path, on_error=MAX_AUTOREAD_LINES + 1) > line_cap:
            out.append(f"[auto-read skipped: > {line_cap} lines — "
                       f"read:{path}:full to see it]\n")
        else:
            out.append(f"[auto-read: single file < {_get_op_int('read', 'max_bytes', MAX_READ_BYTES)} bytes, "
                       "match found]\n")



            out.append(render_file(path, 0, _get_op_int("read", "max_lines", MAX_READ_LINES),
                                   limit_defaulted=True))

    return "".join(out)


_AROUND_DIR_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", "vendor"}
_AROUND_DIR_MAX_FILES = 20


def _grep_line_cap() -> int:
    return _get_op_int("grep", "max_line_chars", MAX_GREP_LINE_CHARS)


def _cap_grep_line(content: str, full: bool = False) -> str:














    if full:
        return content
    cap = _grep_line_cap()
    if len(content) <= cap:
        return content
    return f"{content[:cap]}… (+{len(content) - cap} chars)"


def _grep_cut_note(cut: List[Tuple[str, int]]) -> str:












    if not cut:
        return ""
    fp, lineno = cut[0]
    plural = "" if len(cut) == 1 else "s"
    return (f"note: {len(cut)} line{plural} cut at {_grep_line_cap()} chars — "
            f"read:PATH:LINE-LINE returns a cut line byte-exactly, e.g. "
            f"read:{fp}:{lineno}-{lineno}\n")


def _grep_full_note(cut: List[Tuple[str, int]]) -> str:















    if not cut:
        return ""
    plural = "" if len(cut) == 1 else "s"
    return (f"note: full: {len(cut)} line{plural} would exceed "
            f"{_grep_line_cap()} chars and {'was' if len(cut) == 1 else 'were'} "
            f"returned in full, uncut, because full was requested\n")


def _cap_context_window(text: str, op_name: str) -> str:








    cap = _get_op_int(op_name, "max_bytes", MAX_AROUND_BYTES)
    encoded = text.encode("utf-8", errors="surrogateescape")
    if len(encoded) <= cap:
        return text
    clipped = encoded[:cap].decode("utf-8", errors="ignore")



    nl = clipped.rfind("\n")
    if nl >= 0:
        clipped = clipped[:nl + 1]
    dropped = len(encoded) - len(clipped.encode("utf-8", errors="ignore"))
    return (clipped +
            f"… truncated (~{dropped} more bytes) — narrow context (:N) "
            f"or use between: for the whole symbol\n")


def _around_one_file(regex: "re.Pattern[str]", path: str, n: int) -> str:





    try:
        with open(path, "rb") as f:
            raw_lines = f.read().splitlines(keepends=True)
    except OSError as e:
        return f"ERROR: could not read {path}: {e}\n"

    lines = []
    for raw in raw_lines:
        try:
            lines.append(raw.decode("utf-8", errors="replace"))
        except Exception:
            lines.append("<binary line>\n")

    match_lineno = None
    for i, line in enumerate(lines):
        if regex.search(line):
            match_lineno = i
            break

    if match_lineno is None:
        return ""

    total = len(lines)
    start = max(0, match_lineno - n)
    end = min(total, match_lineno + n + 1)

    out = [f"(match at line {match_lineno + 1}, showing lines {start + 1}–{end}, "
           f"{total} lines total)\n"]
    for i in range(start, end):
        marker = "→" if i == match_lineno else " "
        out.append(f"{i + 1:>6}{marker}{lines[i]}")
    out.append("\n")
    return "".join(out)


def op_around(pattern: str, path: str, n: int = 10,
               via_payload: bool = False) -> str:



















    _effective, refusal, note = _pattern_gate(pattern)
    if refusal:
        return refusal
    prefix = "" if via_payload else _pattern_read_as_note(pattern, path, "around")
    return (prefix
            + note
            + _op_around(pattern, path, n))


def _op_around(pattern: str, path: str, n: int = 10) -> str:







    if not pattern:
        return "ERROR: empty pattern\n"



    pattern, _ = _bre_alternation_rewrite(pattern)
    if not path:
        return "ERROR: empty path\n"

    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(re.escape(pattern))

    if not os.path.isdir(path) and not os.path.isfile(path):






        suggest = None















        if _is_ascii_int(path) and _containment_error([pattern]) is None:
            suggest = (
                "`around` takes PATTERN:PATH[:N] — "
                f"'{path}' was read as the path. Did you mean: "
                f"around_line:{pattern}:{path}[:N]"
            )
        if suggest is None:




            suggest = _swap_suggest(
                "around", "PATTERN:PATH[:N]", "pattern", pattern, path,
                f"around:{path}:{pattern}[:N]")
        return _path_not_found(path, label="file", suggest=suggest,
                               op="around")

    def _render(rx: "re.Pattern[str]") -> Tuple[str, bool]:


        if os.path.isdir(path):
            hits: List[str] = []
            scanned = 0
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs
                           if not d.startswith(".") and d not in _AROUND_DIR_SKIP]
                for name in sorted(files):
                    if name.startswith("."):
                        continue
                    fpath = os.path.join(root, name)
                    scanned += 1
                    rendered = _around_one_file(rx, fpath, n)
                    if rendered and rendered.startswith("ERROR:"):
                        continue
                    if not rendered:
                        continue
                    rel = _fwd(_safe_relpath(fpath, path))
                    hits.append(f"=== {rel} ===\n{rendered}")
                    if len(hits) >= _AROUND_DIR_MAX_FILES:
                        break
                if len(hits) >= _AROUND_DIR_MAX_FILES:
                    break
            if not hits:
                return (f"(no match for {pattern!r} in {path}, "
                        f"scanned {scanned} file(s))\n\n", False)
            header = f"(matched {len(hits)} file(s) under {path}"
            if len(hits) >= _AROUND_DIR_MAX_FILES:
                header += f", capped at {_AROUND_DIR_MAX_FILES}"
            header += f", scanned {scanned})\n"
            return _cap_context_window(header + "".join(hits), "around"), True

        rendered = _around_one_file(rx, path, n)
        if not rendered:
            return (f"(no match for {pattern!r} in {path})\n"
                    + _shim_facade_note(path) + "\n"), False
        return _cap_context_window(rendered, "around"), True

    out_text, matched = _render(regex)
    if not matched and _is_regexy(pattern):
        lit_text, lit_matched = _render(re.compile(re.escape(pattern)))
        if lit_matched:
            return _literal_note(pattern, lit_text.count("=== ") or 1) + lit_text
    if not matched:


        quote_note = _quote_pair_note(
            pattern, lambda inner: _render(_compile_lenient(inner))[1])
        if quote_note:
            return quote_note + out_text
    return out_text


def op_between_symbol(symbol: str, path: str) -> str:





    if not symbol:
        return "ERROR: empty symbol\n"
    if not path:
        return "ERROR: empty path\n"
    if os.path.isdir(path):
        return (f"ERROR: between only works on single files, not "
                f"directories: {path}\n")
    if not os.path.isfile(path):



        _swap = _swap_suggest(
            "between", "SYMBOL:PATH", "symbol", symbol, path,
            f"between:{path}:{symbol}")
        if _swap:
            return f"ERROR: file not found: {path}\n  {_swap}\n"
        return f"ERROR: file not found: {path}\n"

    if not _has_tree_sitter():
        return ("ERROR: between symbol mode requires tree-sitter "
                "(install tree-sitter-language-pack). "
                "Use 'between:re:START:END:PATH' for regex line slicing.\n")

    ext = os.path.splitext(path)[1].lower()
    lang_name = _TS_LANG_MAP.get(ext)
    if not lang_name:
        return (f"ERROR: tree-sitter does not support extension {ext!r}. "
                "Use 'between:re:START:END:PATH' for regex line slicing.\n")

    found = _ts_find_node(path, lang_name, symbol)
    if found is None and lang_name in _TS_GRAMMAR_FAILED:
        return (f"ERROR: tree-sitter grammar for {ext!r} failed to load "
                f"({_TS_GRAMMAR_FAILED[lang_name]}) - cannot search for "
                f"symbols. Use 'between:re:START:END:PATH' for regex line "
                f"slicing.\n")


    normalized = _normalize_symbol_query(symbol)
    if found is None and normalized != symbol:
        found = _ts_find_node(path, lang_name, normalized)
        if found is not None:
            symbol = normalized
    if found is None:
        extra = "" if normalized == symbol else f" (also tried {normalized!r})"
        return (f"ERROR: symbol {symbol!r} not found in {path}{extra}\n"
                + _shim_facade_note(path))
    node, kind, total = found

    start_line = node.start_point[0]
    end_line = node.end_point[0]

    try:
        with open(path, "rb") as f:
            raw_lines = f.read().splitlines(keepends=True)
    except OSError as e:
        return f"ERROR: could not read {path}: {e}\n"

    total_lines = len(raw_lines)
    end_line = min(end_line, total_lines - 1)

    suffix = f", {total} matches (first shown)" if total > 1 else ""
    out = [f"({kind} {symbol!r}, lines {start_line + 1}–{end_line + 1}, "
           f"{end_line - start_line + 1} lines{suffix})\n"]
    for i in range(start_line, end_line + 1):
        try:
            line = raw_lines[i].decode("utf-8", errors="replace")
        except Exception:
            line = "<binary line>\n"
        marker = "→" if i == start_line else " "
        out.append(f"{i + 1:>6}{marker}{line}")
    out.append("\n")
    return "".join(out)


def op_between_pattern(start: str, end: str, path: str) -> str:



    if not start:
        return "ERROR: empty start pattern\n"
    if not end:
        return "ERROR: empty end pattern\n"
    if not path:
        return "ERROR: empty path\n"
    if os.path.isdir(path):
        return (f"ERROR: between only works on single files, not "
                f"directories: {path}\n")
    if not os.path.isfile(path):
        return f"ERROR: file not found: {path}\n"

    start, start_refusal, start_note = _pattern_gate(start)
    if start_refusal:
        return start_refusal
    end, end_refusal, end_note = _pattern_gate(end)
    if end_refusal:
        return end_refusal




    gate_notes = start_note + end_note

    try:
        start_re = re.compile(start)
    except re.error:
        start_re = re.compile(re.escape(start))
    try:
        end_re = re.compile(end)
    except re.error:
        end_re = re.compile(re.escape(end))

    try:
        with open(path, "rb") as f:
            raw_lines = f.read().splitlines(keepends=True)
    except OSError as e:
        return gate_notes + f"ERROR: could not read {path}: {e}\n"

    lines: List[str] = []
    for raw in raw_lines:
        try:
            lines.append(raw.decode("utf-8", errors="replace"))
        except Exception:
            lines.append("<binary line>\n")

    start_idx: int | None = None
    for i, line in enumerate(lines):
        if start_re.search(line):
            start_idx = i
            break
    if start_idx is None:
        return (gate_notes
                + f"ERROR: start pattern {start!r} not matched in {path}\n"
                + _shim_facade_note(path))

    end_idx: int | None = None
    for i in range(start_idx + 1, len(lines)):
        if end_re.search(lines[i]):
            end_idx = i
            break
    if end_idx is None:
        return (gate_notes
                + f"ERROR: end pattern {end!r} not matched after line "
                f"{start_idx + 1} in {path}\n")

    out = [gate_notes,
           f"(slice lines {start_idx + 1}–{end_idx + 1}, "
           f"{end_idx - start_idx + 1} lines)\n"]
    for i in range(start_idx, end_idx + 1):
        marker = "→" if i in (start_idx, end_idx) else " "
        out.append(f"{i + 1:>6}{marker}{lines[i]}")
    out.append("\n")
    return "".join(out)






def _rtk_grep_census(pattern: str, path: str,
                     exclude_paths: Tuple[str, ...]) -> Optional[Tuple[int, int]]:

































    if not _get_op_bool("grep", "count_truncated", True):
        return None



    args = ["grep", "-rc", "-H", "-E"]
    args.extend(_grep_exclude_flags(exclude_paths))
    args.extend([pattern, path])
    out = _rtk_run(args)
    if out is None:
        return None
    cwd = os.getcwd()
    total = 0
    scanned = 0
    for line in out.splitlines():
        if not line.strip():
            continue
        fpath, sep, num = line.rpartition(":")
        num = num.strip()




        if not sep or not fpath or not _is_ascii_int(num):
            return None
        if exclude_paths and _is_excluded(_safe_relpath(fpath, cwd), exclude_paths):
            continue
        scanned += 1
        total += int(num)
    if scanned == 0:
        return None
    return total, scanned


def _rtk_grep_report(rtk_out: str, limit: int,
                     census: Optional[Callable[[], Optional[Tuple[int, int]]]] = None
                     ) -> str:





































    lines = [ln for ln in rtk_out.splitlines() if ln.strip()]
    truncated = len(lines) > limit
    lines = lines[:limit]
    files = set()
    for ln in lines:
        m = re.match(r"^(.+?):\d+:", ln)
        if m:
            files.add(m.group(1))
    total: Optional[int] = None
    reason: Optional[str] = None
    scanned_clause = ", scanned ? files"
    if truncated and census is not None:
        counted = census()
        if counted is None:
            pass
        elif counted[0] <= len(lines):



            reason = (f"the delegated count came back at {counted[0]}, "
                      f"at or below the {len(lines)} rows shown, and was "
                      "refused as incoherent")
        else:
            total, scanned = counted
            scanned_clause = _scanned_suffix(scanned)
    header = (f"({len(lines)} results in {len(files)} files"
              f"{scanned_clause} — delegated to rtk"
              f", limit {limit}"
              f"{_truncation_suffix(truncated, total, reason=reason)})\n")
    return header + "".join(ln + "\n" for ln in lines) + "\n"


def _truncation_suffix(truncated: bool, total: Optional[int] = None,
                       capped: bool = False,
                       reason: Optional[str] = None) -> str:




































    if not truncated:
        return ""
    if total is None:
        return (" — TRUNCATED, more matches exist, total unknown ("
                + (reason or "the delegated count pass returned no total")
                + ")")
    if capped:
        return (f" — TRUNCATED, {total}+ matches total "
                f"(count capped at {total})")
    return f" — TRUNCATED, {total} matches total"


def _trim_context_groups(
    groups: List[List[Tuple[str, int, str, str]]], limit: int
) -> Tuple[List[List[Tuple[str, int, str, str]]], bool]:








    kept: List[List[Tuple[str, int, str, str]]] = []
    seen = 0
    truncated = False
    for group in groups:
        lines: List[Tuple[str, int, str, str]] = []
        stopped = False
        for entry in group:
            if entry[2] == "match":
                if seen >= limit:
                    stopped = True
                    break
                seen += 1
            lines.append(entry)
        if any(e[2] == "match" for e in lines):
            kept.append(lines)
        if stopped:
            truncated = True
            break
    return kept, truncated


def _hidden_suffix(hidden: int) -> str:














    if hidden <= 0:
        return ""
    return f", {hidden} files hidden by exclude-paths"


def _grep_count_ceiling(limit: int) -> int:






    return max(_get_op_int("grep", "count_ceiling", MAX_GREP_COUNT_CEILING),
               limit)


def _grep_total(seen: int, ceiling: int) -> Tuple[int, bool]:






    return min(seen, ceiling), seen > ceiling


def _scanned_suffix(scanned: int) -> str:





    if scanned == 0:
        return ", scanned 0 files — nothing matched the path/glob"
    return f", scanned {scanned} files"


def _grep_count(
    pattern: str, path: str, limit: int,
    exclude_paths: Tuple[str, ...] = (),
    candidates: Optional[List[str]] = None,
) -> Dict[str, int]:

    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(re.escape(pattern))

    counts: Dict[str, int] = {}
    if candidates is None:
        candidates = _grep_candidates(path, exclude_paths)

    for file_path in candidates:
        cnt = 0
        try:
            with open(file_path, "rb") as f:
                for raw in f:
                    try:
                        line = raw.decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    if regex.search(line):
                        cnt += 1
        except OSError:
            continue
        if cnt > 0:
            counts[file_path] = cnt
    return counts


def _grep_candidates(
    path: str, exclude_paths: Tuple[str, ...] = (),
    hidden: Optional[List[str]] = None,
    git_tally: Optional["_GitIgnoreTally"] = None,
) -> List[str]:



























    candidates: List[str] = []
    if os.path.isfile(path):
        candidates.append(path)
    elif os.path.isdir(path):
        exts = _grep_file_includes()  
        cwd = os.getcwd()
        view = _git_ignore_view(path) if exclude_paths else _GIT_IGNORE_NONE
        if git_tally is not None:
            git_tally.saw(view)
        ignored = view.dirs
        for root, dirs, files in os.walk(path):
            rel_root = _safe_relpath(root, cwd) if exclude_paths else ""
            if exclude_paths:
                dirs[:] = [
                    d for d in dirs
                    if not _is_excluded(os.path.join(rel_root, d), exclude_paths)
                    and not _is_git_ignored(rel_root, d, ignored)
                ]
            for name in files:
                if exts is not None and not any(
                        name.endswith(ext.lstrip("*")) for ext in exts):
                    continue
                rel_name = os.path.join(rel_root, name)
                if exclude_paths and _is_excluded(rel_name, exclude_paths):
                    if hidden is not None and _is_disclosable_exclusion(
                            rel_name, exclude_paths):
                        hidden.append(os.path.join(root, name))
                    continue
                if _is_git_ignored_file(rel_name, view):
                    if git_tally is not None:
                        git_tally.hidden.append(os.path.join(root, name))
                    continue
                candidates.append(os.path.join(root, name))
    return candidates


def _grep_recursive(
    pattern: str, path: str, limit: int,
    exclude_paths: Tuple[str, ...] = (),
    candidates: Optional[List[str]] = None,
) -> List[Tuple[str, int, str]]:







    try:
        regex = re.compile(pattern)
    except re.error:

        regex = re.compile(re.escape(pattern))

    results: List[Tuple[str, int, str]] = []
    if candidates is None:
        candidates = _grep_candidates(path, exclude_paths)

    for file_path in candidates:
        if len(results) >= limit:
            break
        try:
            with open(file_path, "rb") as f:
                for lineno, raw in enumerate(f, start=1):
                    try:
                        line = raw.decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    if regex.search(line):
                        results.append((_fwd(file_path), lineno, line.rstrip()))
                        if len(results) >= limit:
                            break
        except OSError:
            continue
    return results


def _grep_recursive_context(
    pattern: str, path: str, limit: int, context: int,
    exclude_paths: Tuple[str, ...] = (),
    candidates: Optional[List[str]] = None,
) -> List[List[Tuple[str, int, str, str]]]:








    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(re.escape(pattern))

    if candidates is None:
        candidates = _grep_candidates(path, exclude_paths)
    groups: List[List[Tuple[str, int, str, str]]] = []
    match_count = 0

    for file_path in candidates:
        if match_count >= limit:
            break
        try:
            with open(file_path, "rb") as f:
                raw_lines = f.read().splitlines(keepends=True)
        except OSError:
            continue

        lines = []
        for raw in raw_lines:
            try:
                lines.append(raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r"))
            except Exception:
                lines.append("<binary line>")


        match_indices = [
            i for i, line in enumerate(lines) if regex.search(line)
        ]
        if not match_indices:
            continue



        windows: List[Tuple[int, int]] = []  
        for mi in match_indices:
            w_start = max(0, mi - context)
            w_end = min(len(lines) - 1, mi + context)
            if windows and w_start <= windows[-1][1] + 1:

                windows[-1] = (windows[-1][0], max(windows[-1][1], w_end))
            else:
                windows.append((w_start, w_end))


        match_set = set(match_indices)
        for w_start, w_end in windows:
            if match_count >= limit:
                break
            group: List[Tuple[str, int, str, str]] = []
            for i in range(w_start, w_end + 1):
                kind = "match" if i in match_set else "context"
                group.append((_fwd(file_path), i + 1, kind, lines[i]))
                if kind == "match":
                    match_count += 1
            groups.append(group)

    return groups
