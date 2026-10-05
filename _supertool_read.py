


































from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_read.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )








def _read_narrowing_hint(path: str) -> str:

















    return (f"    ↳ that is the head of the file — for a region, "
            f"read:{path}:START-END; to find one, "
            f"read:{path}:::grep=PATTERN" + "\n")


def render_file(path: str, offset: int = 0, limit: int = 0,
                grep_filter: str = "", force_full: bool = False,
                range_form: bool = False, *,
                limit_defaulted: bool | None = None) -> str:












    try:
        _safe_path(path)
    except SecurityError as e:
        return f"ERROR: {e}\n"




    path = _expand_home(path)



    filter_scan_all = bool(grep_filter) and limit <= 0



















    if limit_defaulted is None and limit <= 0 and not filter_scan_all:
        limit_defaulted = True
    if limit <= 0:
        limit = _get_op_int("read", "max_lines", MAX_READ_LINES)
    if not path or not os.path.isfile(path):
        return _path_not_found(path, label="file", op_name="read",
                               call_prefix="read")





    rtk_malformed = False

    if not grep_filter and offset == 0 and limit == _get_op_int("read", "max_lines", MAX_READ_LINES) and _rtk_enabled() and _has_rtk():
        rtk_args = ["read", "-n", "--max-lines", str(_get_op_int("read", "max_lines", MAX_READ_LINES))]
        rtk_used_aggressive = _is_compact()
        if rtk_used_aggressive:
            rtk_args += ["--level", "aggressive"]
        rtk_args.append(path)
        rtk_out = _rtk_run(rtk_args)











        rtk_malformed = rtk_out is not None and _rtk_output_looks_malformed(
            rtk_out, aggressive=rtk_used_aggressive)
        if rtk_out is not None and not rtk_malformed:




            _more = ""
            try:
                with open(path, "rb") as _probe:
                    _raw = _probe.read()
                _ambiguity = _line_break_ambiguity_note(_raw)














                if (len(_split_lines_keepends(_raw))
                        > _get_op_int("read", "max_lines", MAX_READ_LINES)
                        or len(_raw)
                        > _get_op_int("read", "max_bytes", MAX_READ_BYTES)):
                    _more = _read_narrowing_hint(path)
            except OSError:
                _ambiguity = ""
            return _ambiguity + rtk_out + _more + "\n"

    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return f"ERROR: could not read {path}: {e}\n"



    raw_lines = _split_lines_keepends(data)

    line_count = len(raw_lines)
    if filter_scan_all:






        limit = max(0, line_count - offset)
    out = [f"({line_count} lines, {size} bytes{_read_freshness_note(path)})"
           f"{_path_meta_suffix(path, b''.join(raw_lines[:64]))}\n"]
    if rtk_malformed:



        out.append(
            "(rtk's compressed read looked malformed for this file -- "
            "falling back to the built-in renderer; #1786)\n")
    _ambiguity = _line_break_ambiguity_note(data)
    if _ambiguity:
        out.append(_ambiguity)
    bytes_emitted = 0
    printed = 0
    end = min(offset + limit, line_count)


    in_claude = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "") != ""
    byte_cap = _get_op_int("read", "max_bytes", MAX_READ_BYTES)
    apply_byte_cap = in_claude or not force_full









    lift_line_cap = not apply_byte_cap and not range_form
    if lift_line_cap:
        end = line_count  

    filter_regex = None
    filter_literal_why = ""
    if grep_filter:
        try:
            filter_regex = re.compile(grep_filter)
        except re.error as e:




            filter_regex = re.compile(re.escape(grep_filter))
            filter_literal_why = str(e)

    compact = not filter_regex and _is_compact()
    matched_any = False







    last_scanned = offset
    capped = False
    for i in range(offset, end):
        last_scanned = i + 1
        try:
            line = raw_lines[i].decode("utf-8", errors="replace")
        except Exception:
            line = "<binary line>\n"
        if filter_regex and not filter_regex.search(line):
            continue
        if compact and _COMPACT_SKIP.match(line):
            continue
        matched_any = True
        numbered = f"{i + 1:>6}→{line}"
        out.append(numbered)
        bytes_emitted += len(numbered)
        printed += 1
        if apply_byte_cap and bytes_emitted >= byte_cap:
            capped = True
            break






    cap_cut = capped and last_scanned < end
    unsearched = 0



    filter_notes: List[str] = []
    if filter_regex:
        unsearched = line_count - (last_scanned - offset)
        if filter_literal_why:
            filter_notes.append(
                f"(the grep= pattern is not a usable regex — "
                f"{filter_literal_why}; searched for it as a literal "
                f"string instead)\n")
    if filter_regex and not matched_any:
        if unsearched > 0 and last_scanned <= offset:





            filter_notes.append(
                f"(no lines matching {grep_filter!r} -- offset "
                f"{offset + 1} is past the end of this {line_count}-line "
                f"file, so no line was searched and this is not an answer "
                f"about the file)\n")
        elif unsearched > 0:


            plural = "s" if unsearched != 1 else ""
            verb = "were" if unsearched != 1 else "was"
            filter_notes.append(
                f"(no lines matching {grep_filter!r} in lines "
                f"{offset + 1}-{last_scanned} of {line_count} — the other "
                f"{unsearched} line{plural} {verb} NOT searched, so this is "
                f"not an answer about the whole file)\n")
        else:
            filter_notes.append(
                f"(no lines matching {grep_filter!r} in any of "
                f"{line_count} lines)\n")




        if last_scanned > offset:
            def _filter_probe(inner: str) -> bool:
                rx = _compile_lenient(inner)
                for raw in raw_lines[offset:last_scanned]:
                    try:
                        candidate = raw.decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    if rx.search(candidate):
                        return True
                return False

            filter_notes.append(_quote_pair_note(grep_filter, _filter_probe))
    elif filter_regex and cap_cut:















        plural = "s" if unsearched != 1 else ""
        verb = "were" if unsearched != 1 else "was"
        if unsearched > 0:
            filter_notes.append(
                f"(the grep= filter searched lines {offset + 1}-"
                f"{last_scanned} of {line_count} and stopped there — the "
                f"output reached the {byte_cap}-byte cap, so the other "
                f"{unsearched} line{plural} {verb} NOT searched and this is "
                f"not an answer about the whole file — continue with "
                f"read:PATH:{last_scanned}:LIMIT:grep=PATTERN)\n")
        else:
            filter_notes.append(
                f"(the grep= filter searched all {line_count} lines; the "
                f"output reached the {byte_cap}-byte cap on line "
                f"{last_scanned}, the last line of the file)\n")
    elif filter_regex and unsearched > 0:
        plural = "s" if unsearched != 1 else ""
        verb = "were" if unsearched != 1 else "was"
        filter_notes.append(
            f"(the grep= filter searched lines {offset + 1}-{last_scanned} "
            f"of {line_count} — {unsearched} line{plural} outside that range "
            f"{verb} NOT searched)\n")
    elif cap_cut:
        remaining = line_count - last_scanned
        out.append(
            f"... (truncated at {_get_op_int('read', 'max_bytes', MAX_READ_BYTES)} bytes "
            f"— showed lines {offset + 1}-{last_scanned} of {line_count} "
            f"({remaining} more line{'s' if remaining != 1 else ''}) — "
            f"use read:PATH:OFFSET:LIMIT to get more)\n"
        )
        if offset == 0 and limit_defaulted is True and not force_full:
            out.append(_read_narrowing_hint(path))
    elif not filter_regex and last_scanned < line_count:









        why = ""
        if limit_defaulted is True:
            why = (f" — the read.max_lines default of {limit} stopped the "
                   f"read here, not the file")
        elif limit_defaulted is False and last_scanned >= offset + limit:






            intact = "" if compact else ", nothing was cut"
            why = (f" — lines {offset + 1}-{last_scanned} are the whole window "
                   f"asked for{intact}; those {line_count - last_scanned} "
                   f"are simply below it")
        out.append(f"... ({line_count - last_scanned} more lines{why})\n")







        if offset == 0 and limit_defaulted is True and not force_full:
            out.append(_read_narrowing_hint(path))
    elif not filter_regex and offset > 0:





        if printed:
            out.append(f"[end of file — lines 1-{offset} not shown]\n")
    elif not filter_regex:
        out.append("[complete file — no more lines]\n")
    if filter_notes:









        for note in reversed(filter_notes):
            out.insert(1, note)
    if offset > 0:




        window_note = _read_window_note(
            path, limit if not lift_line_cap else max(0, line_count - offset),
            offset, line_count, printed,
            last_scanned=last_scanned, capped=cap_cut, cap_reached=capped,
            byte_cap=byte_cap if apply_byte_cap else 0,
            limit_synthetic=lift_line_cap,
            limit_defaulted=limit_defaulted,
            range_form=range_form,
            skipped_by=("the grep= filter" if filter_regex
                        else "compact mode" if compact else ""))
        out.insert(1, window_note)














        if printed > FOOTER_ECHO_MIN_LINES:
            out.append(window_note)
    out.append("\n")
    return "".join(out)


def _read_window_note(path: str, limit: int, offset: int,
                      line_count: int, shown: int, last_scanned: int = 0,
                      capped: bool = False, cap_reached: bool = False,
                      byte_cap: int = 0,
                      limit_synthetic: bool = False,
                      limit_defaulted: bool | None = None,
                      range_form: bool = False,
                      skipped_by: str = "") -> str:
























































    req_start = offset + 1
    req_end = offset + limit
    if last_scanned < offset:
        last_scanned = offset + shown








    if range_form:
        asked = f"range {req_start}-{req_end} (START-END form)"
    else:
        asked = (f"offset {offset} + limit {limit} (OFFSET:LIMIT form) "
                 f"= lines {req_start}-{req_end}")
    if shown <= 0:
        if last_scanned > offset:
            skipped = last_scanned - offset
            by = f"{skipped_by} suppressed" if skipped_by else "nothing matched"
            return (f"window: {asked}; scanned lines {req_start}-"
                    f"{last_scanned} of {line_count} and emitted none — "
                    f"{by} all {skipped}\n")
        return (f"window: {asked}; returning nothing — the file has "
                f"{line_count} lines\n")
    hint = ""






    got_end = last_scanned if last_scanned > offset else req_end






    if not range_form and offset > 0 and limit > 0:











        skipped_word = "line was" if offset == 1 else "lines were"
        hint = (f"; OFFSET is a skip count, so {offset} {skipped_word} skipped: "
                f"this window is read:{path}:{req_start}-{got_end}")














        if limit_synthetic:
            pass
        elif _range_note_fires(offset, limit, line_count):
            span = limit - offset + 1



            hint += (f" — this asked for {limit} lines from offset {offset}, "
                     f"which is OFFSET:LIMIT, not START:END; for lines "
                     f"{offset}-{limit} ({span} lines) use "
                     f"read:{path}:{offset}-{limit}")
        else:
            hint += (f" — for lines {offset}-{offset + limit - 1} use "
                     f"read:{path}:{offset}-{offset + limit - 1}")
    suppressed = (last_scanned - offset) - shown
    held = ""
    if suppressed > 0:
        held = (f", {shown} of those {last_scanned - offset} lines emitted "
                f"({skipped_by or 'a filter'} skipped {suppressed})")
    reasons = []
    if capped:
        reasons.append(f"cut short by the {byte_cap}-byte cap")
    if last_scanned >= line_count:
        reasons.append("the end of the file")
    limit_ended = (not limit_synthetic and limit > 0
                   and last_scanned >= req_end)
    if limit_ended:







        if limit_defaulted is True:
            reasons.append(f"the read.max_lines default of {limit} lines was "
                           f"reached — you named no LIMIT, so this bound is "
                           f"the op's and {line_count - last_scanned} lines of "
                           f"the file are below it")
        else:
            reasons.append("the limit was reached")
    _EOF = "the end of the file"
    if not reasons:
        stops = f", stopping at line {last_scanned} for no reason this op can name"
    elif len(reasons) == 1:
        stops = f", stopping at line {last_scanned}: {reasons[0]}"
    elif _EOF in reasons:












        others = [r for r in reasons if r != _EOF]
        stops = (f", stopping at line {last_scanned}: {_EOF}, and "
                 + " and ".join(others)
                 + f" at the same line — the file ending settles it, "
                 f"nothing follows line {last_scanned}")
    else:







        stops = (f", stopping at line {last_scanned}: "
                 + " and ".join(reasons)
                 + " coincide here — which one ended the window cannot be told apart")
    if (limit_ended and limit_defaulted is False and not capped
            and _EOF not in reasons):

















        stops += (" — the window ends here because it was asked to, "
                  "nothing was cut")
    if cap_reached and not capped:









        stops += (f" — the {byte_cap}-byte cap was reached on that line and "
                  f"dropped nothing; it stops whole lines and never truncates "
                  f"one, so these bytes are complete")
    return (f"window: {asked}; returning lines {req_start}-{last_scanned} "
            f"of {line_count}{held}{stops}{hint}\n")


_READ_RANGE_RE = re.compile(r"\d+-\d+")


def _range_note_fires(offset: int, limit: int, total: int) -> bool:





















    if offset <= 0 or limit <= 0 or limit <= offset or total <= 0:
        return False
    return not (offset + limit <= total and limit >= 2 * offset)


def _abstract_lang(path: str) -> str:






    lang = _TS_LANG_MAP.get(os.path.splitext(path)[1].lower(), "")
    return "" if lang in _ABSTRACT_READ_SKIP_LANGS else lang






_ABSTRACT_READ_SKIP_LANGS: FrozenSet[str] = frozenset({"markdown"})


def _abstract_map(path: str, lang: str, size_bytes: int) -> Tuple[str, str]:














    body = op_map(path)
    if _UNREADABLE_MARKER in body:




        why = _map_unreadable_reason(path) or "the reason is no longer visible"
        return "", f"{_UNREADABLE_MARKER} {path}: {why} — no tier looked at it"
    if (body.startswith("ERROR:") or "(no symbols)" in body
            or _NO_PARSER_MARKER in body
            or "no supported files" in body):
        reason = f"no symbols found in {path} ({lang})"
        if not _has_tree_sitter():
            reason += " — tree-sitter is not installed, so only the regex tier ran"
        return "", reason
    map_bytes = len(body.encode("utf-8", errors="replace"))
    budget = min(size_bytes, _get_op_int("read", "max_bytes", MAX_READ_BYTES))
    if map_bytes >= budget:
        return "", (f"symbol map for {path} ({lang}) is {map_bytes} bytes, "
                    f"not smaller than the {budget} bytes this read emits")
    return body, ""

























_READ_ELIDE_KIND = "read-elide"
_READ_ELIDE_WINDOW_SECONDS = 900


def _read_elide_enabled() -> bool:















    off = os.environ.get("SUPERTOOL_READ_NO_ELIDE", "")
    if off.strip() and off.strip() != "0":
        return False
    return _get_op_bool("read", "elide", True)


def _read_elide_window() -> float:
    return float(_get_op_int("read", "elide_window_seconds",
                             _READ_ELIDE_WINDOW_SECONDS))


def _read_elide_session_key() -> str:













    user = os.environ.get("USER") or os.environ.get("USERNAME") or "?"
    return "|".join((user, str(os.getppid()),
                     os.path.realpath(os.getcwd())))


def _read_elide_state_path(file_path: str) -> str:






    key = f"{_read_elide_session_key()}|{os.path.realpath(file_path)}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return os.path.join(str(_cache_root() / _READ_ELIDE_KIND), digest)


def _read_elide_load(file_path: str) -> "Optional[Tuple[str, float, int]]":






    with open(_read_elide_state_path(file_path), "r", encoding="utf-8") as fh:
        raw = fh.read().strip()
    parts = raw.split()
    if len(parts) != 3:
        return None
    try:
        return parts[0], float(parts[1]), int(parts[2])
    except ValueError:
        return None


def _read_elide_record(file_path: str, digest: str, size: int,
                       now: float) -> None:
    target = _read_elide_state_path(file_path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = f"{target}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(f"{digest} {now:.3f} {size}\n")
    os.replace(tmp, target)


def _read_elide_line(file_path: str, digest: str, size: int,
                     when: float) -> str:





    stamp = datetime.fromtimestamp(when).strftime("%H:%M:%S")
    return (f"[read elided — {file_path} is byte-identical to your read at "
            f"{stamp} (sha256 {digest[:12]}, {size:,} bytes on disk), so this "
            f"would return what you already have. If you no longer have it: "
            f"read:{file_path}:full]\n")


def _read_elide(path: str, offset: int, limit: int, grep_filter: str,
                force_full: bool, range_form: bool) -> str:





    if offset or limit or grep_filter or range_form:


        return ""
    if not _read_elide_enabled():
        return ""
    hasher = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                hasher.update(chunk)
                size += len(chunk)
    except OSError:




        return ""
    digest = hasher.hexdigest()
    now = time.time()
    try:
        prior = _read_elide_load(path)
    except OSError:
        prior = None
    if (prior is not None and not force_full and prior[0] == digest
            and 0 <= now - prior[1] <= _read_elide_window()):
        return _read_elide_line(path, digest, prior[2], prior[1])
    try:
        _read_elide_record(path, digest, size, now)
    except OSError:
        pass  
    return ""


def op_read(path: str, offset: int = 0, limit: int = 0,
            grep_filter: str = "", force_full: bool = False,
            range_form: bool = False) -> str:







    filter_note = ""
    if grep_filter:
        grep_filter, refusal, filter_note = _pattern_gate(grep_filter)
        if refusal:
            return refusal










    skip_note = ""
    if (offset == 0 and limit == 0 and not grep_filter and not force_full
            and (_get_op_bool("read", "abstract", False)
                 or _get_op_bool("read", "php_abstract", False))):
        lang = _abstract_lang(path)
        threshold = _get_op_int("read", "abstract_threshold_bytes",
                                _get_op_int("read", "max_bytes", MAX_READ_BYTES))
        try:
            size_bytes = os.path.getsize(path)
        except OSError:
            size_bytes = 0
        if lang and size_bytes > threshold:
            body, reason = _abstract_map(path, lang, size_bytes)
            if body:
                line_count = 0
                try:
                    with open(path, "rb") as f:


                        for line_count, _ in enumerate(f, 1):  
                            pass
                except OSError:
                    pass
                return (body
                        + f"\n[abstract read — {lang}, {line_count} lines, "
                          f"{size_bytes} bytes raw — "
                          f"use read:{path}:full for content "
                          f"or read:{path}:::grep=PATTERN to filter]\n")
            skip_note = (f"[abstract read skipped — {reason}; "
                         f"showing raw source]\n")





    elision = _read_elide(path, offset, limit, grep_filter, force_full,
                          range_form)
    if elision:
        return elision





    limit_defaulted = limit <= 0 and not grep_filter
    if limit <= 0 and not grep_filter:
        limit = _get_op_int("read", "max_lines", MAX_READ_LINES)
    body = render_file(path, offset, limit, grep_filter, force_full,
                       range_form, limit_defaulted=limit_defaulted)



    return skip_note + filter_note + body + _read_edit_hint(path, body)


def _modify_hint_install_dir() -> str:







    return os.path.dirname(os.path.realpath(__file__))


def _modify_hint_wrapper_is_runnable(path: str) -> bool:








    if os.name == "nt":
        try:
            with open(path, "rb") as fh:
                return fh.read(2) == b"#!"
        except OSError:
            return False
    return os.access(path, os.X_OK)


def _modify_hint_quoted_interpreter() -> str:







    exe = sys.executable
    if " " not in exe:
        return exe
    return chr(34) + exe + chr(34) if os.name == "nt" else shlex.quote(exe)


def _modify_hint(op: str) -> str:






















    root = _modify_hint_install_dir()
    quoted = chr(39) + op + chr(39)
    wrapper = os.path.join(root, "supertool")
    if (os.path.isfile(wrapper)
            and _modify_hint_wrapper_is_runnable(wrapper)):
        return "./supertool " + quoted
    if os.path.isfile(os.path.join(root, "supertool.py")):
        return _modify_hint_quoted_interpreter() + " supertool.py " + quoted
    return "(no runnable supertool found in " + root + " -- the op is " + quoted + ")"


def _read_edit_hint(path: str, body: str) -> str:





    if body.startswith("ERROR:"):
        return ""








    op = f"edit:::OLD:::NEW:::{_flat_field(path, disclose_newline=True)}"
    return (f"{mark(chr(0x21B3))} to modify: {_modify_hint(op)}"
            f"  (or edit:@- ; no harness Read needed)\n")


def op_glob(pattern: str, no_exclude: bool = False, no_auto_read: bool = False) -> str:

    if not pattern:
        return "ERROR: empty pattern\n"













    if pattern.startswith("~"):
        _eg = _fwd(os.path.join(os.path.expanduser("~"), "*.txt"))
        return ("ERROR: unsupported path form: glob does not expand `~` — "
                f"pass an absolute path (e.g. glob:{_eg}).\n")







    _pattern_err = _glob_pattern_containment_error(pattern)
    if _pattern_err:
        return _pattern_err


    if not WILDCARD_CHARS.search(pattern) and os.path.isfile(pattern):
        if no_auto_read:
            return f"{pattern}\n"
        return ("[auto-read: concrete path, no wildcards]\n"
                + render_file(pattern, 0,
                              _get_op_int("read", "max_lines", MAX_READ_LINES),
                              limit_defaulted=True))

    excl = _get_exclude_paths("glob", no_exclude)



    cap = _get_op_int("glob", "max_results", MAX_GLOB_RESULTS)
    hidden_files: List[str] = []
    git_tally = _GitIgnoreTally()
    files = _glob_files(pattern, excl, over_fetch=1, hidden=hidden_files,
                        git_tally=git_tally)








    midpath_note = ""



    if (not files and "/" in pattern
            and not pattern.startswith(("/", "**", "./", "../"))):
        retry = "**/" + pattern
        hidden_files = []
        retry_tally = _GitIgnoreTally()
        files = _glob_files(retry, excl, over_fetch=1, hidden=hidden_files,
                            git_tally=retry_tally)
        git_tally = retry_tally
        if files:
            midpath_note = (f"[mid-path retry: no match under cwd for "
                            f"{pattern!r} — matched {retry!r}]\n")





















    if _glob_results_escape(files + hidden_files):
        return ("ERROR: path escapes cwd: " + repr(pattern) + " — at least "
                "one match resolves outside cwd, reached through a symlink "
                "the pattern does not name. Refusing the whole call: a list "
                "with those entries dropped would report a population it had "
                "silently narrowed, and how many there were is itself an "
                "answer about a directory outside the boundary. "
                + _ALLOW_OUTSIDE_HINT + chr(10))
    glob_truncated = len(files) > cap
    files = files[:cap]

    prefix = ""
    if len(files) >= 2:
        prefix = os.path.commonpath(files)
        if prefix and not prefix.endswith(os.sep):
            prefix += os.sep

        if len(prefix) <= 10:
            prefix = ""
    truncation = " — TRUNCATED, more files match" if glob_truncated else ""
    out = [midpath_note,
           f"({len(files)} files{_hidden_suffix(len(hidden_files))}"
           f"{git_tally.clause()}{truncation})\n"]
    if prefix:
        fwd_prefix = _fwd(prefix)
        out.append(f"{fwd_prefix}\n")
        for f in files:
            out.append(f"  {_fwd(f[len(prefix):])}\n")
    else:
        for f in files:
            out.append(_fwd(f) + "\n")
    out.append("\n")



    if not no_auto_read and len(files) == 1 and os.path.getsize(files[0]) < _get_op_int("read", "max_bytes", MAX_READ_BYTES):
        line_cap = _get_op_int("read", "max_autoread_lines", MAX_AUTOREAD_LINES)
        if _count_lines(files[0], on_error=MAX_AUTOREAD_LINES + 1) > line_cap:
            out.append(f"[auto-read skipped: > {line_cap} lines — "
                       f"read:{files[0]}:full to see it]\n")
        else:
            out.append("[auto-read: glob returned 1 file]\n")
            out.append(render_file(files[0], 0,
                                   _get_op_int("read", "max_lines", MAX_READ_LINES),
                                   limit_defaulted=True))

    return "".join(out)


def op_ls(path: str = ".") -> str:
    if not os.path.isdir(path):
        return f"ERROR: not a directory: {path}\n"
    try:
        items = sorted(os.listdir(path))
    except OSError as e:
        return f"ERROR: could not list {path}: {e}\n"
    out = [f"({len(items)} items)\n"]
    for item in items:
        full = os.path.join(path, item)
        marker = "/" if os.path.isdir(full) else ""
        out.append(f"{item}{marker}\n")
    out.append("\n")
    return "".join(out)


def _probe_minified(probe: str) -> bool:






    if len(probe) <= MAX_READ_BYTES:
        return False
    longest = max((len(seg) for seg in probe.split("\n")), default=0)
    return longest >= MINIFIED_LINE_CHARS


def _looks_minified(path: str) -> bool:

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            probe = f.read(MAX_READ_BYTES + 1)
    except OSError:
        return False
    return _probe_minified(probe)


def _char_window(path: str, n_chars: int, from_end: bool = False) -> str:




    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = f.read()
    total = len(data)
    if from_end:
        window = data[-n_chars:]
        clipped = total - len(window)
        return (f"({total} chars, minified; showing last {len(window)}, "
                f"{clipped} clipped)\n… ({clipped} earlier chars)\n{window}\n")
    window = data[:n_chars]
    clipped = total - len(window)
    return (f"({total} chars, minified; showing first {len(window)})\n"
            f"{window}\n… ({clipped} more chars truncated)\n")


def op_tail(path: str, n: int = 20) -> str:
    if not path or not os.path.isfile(path):
        return f"ERROR: file not found: {path}\n"
    if _looks_minified(path):
        return _char_window(
            path, _get_op_int("tail", "char_window", CHAR_WINDOW_CHARS),
            from_end=True)
    with open(path, "rb") as f:
        raw_lines = f.read().splitlines(keepends=True)
    total = len(raw_lines)
    start = max(0, total - n)
    out = [f"({total} lines total, showing last {n})\n"]
    for i in range(start, total):
        try:
            line = raw_lines[i].decode("utf-8", errors="replace")
        except Exception:
            line = "<binary line>\n"
        out.append(f"{i + 1:>6}→{line}")
    out.append("\n")
    return "".join(out)


def op_head(path: str, n: int = 20) -> str:
    if not path or not os.path.isfile(path):
        return f"ERROR: file not found: {path}\n"
    if _looks_minified(path):
        return _char_window(
            path, _get_op_int("head", "char_window", CHAR_WINDOW_CHARS),
            from_end=False)
    with open(path, "rb") as f:
        raw_lines = f.read().splitlines(keepends=True)
    total = len(raw_lines)
    limit = min(n, total)
    out = [f"({total} lines total, showing first {limit})\n"]
    for i in range(limit):
        try:
            line = raw_lines[i].decode("utf-8", errors="replace")
        except Exception:
            line = "<binary line>\n"
        out.append(f"{i + 1:>6}→{line}")
    out.append("\n")
    return "".join(out)


def op_wc(path: str) -> str:
    if not path or not os.path.isfile(path):
        return f"ERROR: file not found: {path}\n"


    if _rtk_enabled() and _has_rtk():
        rtk_out = _rtk_run(["wc", path])
        if rtk_out is not None:
            return rtk_out + "\n"

    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return f"ERROR: could not read {path}: {e}\n"
    text = data.decode("utf-8", errors="replace")
    lines = text.count("\n")
    words = len(text.split())
    chars = len(text)
    result = f"{lines} {words} {chars} {path}"
    if _probe_minified(text):
        result += f"  [minified — {chars} chars, {len(data)} bytes]"
    return result + "\n"






_NOT_A_REPO = "not a git repository"















PATH_META_UNKNOWN = "git?"

















PATH_META_NOT_CONSULTED = "no-git"




































_PATH_META_BULK: Dict[str, Any] = {}


def _path_meta_bulk_drop() -> None:










    for key in [k for k, v in _PATH_META_BULK.items() if v != "declined"]:
        del _PATH_META_BULK[key]





_PATH_META_GIT_TIMEOUT_DEFAULT = 2


def _path_meta_git_timeout() -> int:


























    return _get_op_int(
        "read", "git_timeout_seconds", _PATH_META_GIT_TIMEOUT_DEFAULT)


def _path_meta_bulk_fill(root: str) -> Optional[Dict[str, Any]]:











    taken_ns = time.time_ns()
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain", "-z", "--ignored=matching"],
            capture_output=True, timeout=_path_meta_git_timeout(), cwd=root,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    codes: Dict[str, str] = {}
    fields = r.stdout.split(b"\x00")
    i = 0
    while i < len(fields):
        record = fields[i]
        i += 1
        if len(record) < 4:
            continue
        xy = record[:2].decode("ascii", errors="replace")
        name = record[3:].decode("utf-8", errors="surrogateescape")
        if xy[:1] in ("R", "C"):

            i += 1
        codes[name.rstrip("/")] = xy
    return {"codes": codes, "taken_ns": taken_ns}


_PATH_META_ROOT_CACHE: Dict[str, str] = {}


def _path_meta_repo_root(path: str) -> str:













    start = os.path.dirname(os.path.abspath(path)) or os.sep
    cached = _PATH_META_ROOT_CACHE.get(start)
    if cached is not None:
        return cached
    current = start
    root = ""
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            root = current
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    _PATH_META_ROOT_CACHE[start] = root
    return root


def _path_meta_bulk_code(codes: Dict[str, str], rel: str) -> str:









    code = codes.get(rel)
    if code:
        return code
    parts = rel.split("/")
    for depth in range(len(parts) - 1, 0, -1):
        code = codes.get("/".join(parts[:depth]))
        if code in ("!!", "??"):
            return code
    return ""


def _short_age(seconds: float) -> str:










    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _read_freshness_note(path: str) -> str:
















    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return ""
    return f", modified {_short_age(time.time() - mtime)} ago"


def _path_meta_suffix(path: str, sample: bytes = b"") -> str:



















    parts = []
    if sample:
        head = sample[:8192]
        if b"\x00" in head:
            parts.append("bin")
        else:
            try:
                head.decode("utf-8")
            except UnicodeDecodeError:
                parts.append("non-utf8")
            if b"\r\n" in head:
                parts.append("crlf")
            if b"<<<<<<< " in head or b"\n=======\n" in head:
                parts.append("cf!")
    if os.path.islink(path):
        try:
            target = os.readlink(path)
        except OSError:
            target = "?"
        broken = " broken" if not os.path.exists(path) else ""
        parts.append(f"->{target}{broken}")
    mtime_ns = None
    try:
        st = os.lstat(path)
        mtime_ns = st.st_mtime_ns
        if st.st_mode & 0o111 and not os.path.isdir(path):
            parts.append("x")




        if os.path.islink(path):
            age_sec = max(0, int(time.time() - st.st_mtime))
            SEVEN_DAYS = 7 * 86400
            if age_sec > SEVEN_DAYS:
                days = age_sec // 86400
                if days < 30:
                    parts.append(f"{days}d")
                elif days < 365:
                    parts.append(f"{days // 7}w")
                else:
                    parts.append(f"{days // 30}mo")
    except OSError:
        pass





    code = None
    absolute = os.path.abspath(path)
    root = _path_meta_repo_root(path)
    if not root:







        parts.append(PATH_META_NOT_CONSULTED)
        return (" " + " ".join(parts)) if parts else ""






    through_a_link = os.path.realpath(path) != absolute
    if root and not through_a_link:
        entry = _PATH_META_BULK.get(root)
        if entry is None:
            _PATH_META_BULK[root] = "primed"
        elif entry == "primed":
            filled = _path_meta_bulk_fill(root)
            entry = filled if filled is not None else "declined"
            _PATH_META_BULK[root] = entry
        servable = (
            isinstance(entry, dict)
            and mtime_ns is not None
            and mtime_ns < entry["taken_ns"]
        )
        if servable:
            try:
                rel = os.path.relpath(absolute, root)
            except ValueError:



                rel = ""
            if rel and not rel.startswith(os.pardir):
                code = _path_meta_bulk_code(entry["codes"], rel.replace(os.sep, "/"))

    if code is None:
        try:



















            r = subprocess.run(
                ["git", "status", "--porcelain", "--ignored=matching", "--",
                 ":(literal)" + os.path.basename(absolute)],
                capture_output=True, text=True, timeout=_path_meta_git_timeout(),
                cwd=os.path.dirname(absolute) or ".", encoding="utf-8", errors="replace",
            )
            if r.returncode == 0:
                code = r.stdout[:2]
            elif _NOT_A_REPO not in r.stderr.lower():
                parts.append(PATH_META_UNKNOWN)
        except subprocess.TimeoutExpired:
            parts.append(PATH_META_UNKNOWN)
        except OSError:




            pass

    if code == "??":
        parts.append("?")
    elif code == "!!":
        parts.append("!")
    elif code and ("M" in code or "A" in code):
        parts.append("m")
    return (" " + " ".join(parts)) if parts else ""


def op_stat(path: str) -> str:

    if not path:
        return "ERROR: empty path\n"
    if not os.path.lexists(path):
        return f"ERROR: not found: {path}\n"

    try:
        st = os.lstat(path)
    except OSError as e:
        return f"ERROR: could not stat {path}: {e}\n"

    size = st.st_size
    modified = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    if os.path.islink(path):
        try:
            target = os.readlink(path)
        except OSError:
            target = "?"
        broken = " (broken)" if not os.path.exists(path) else ""
        return f"{size} {modified} symlink {path} -> {target}{broken}\n"
    kind = "dir" if os.path.isdir(path) else "file"
    return f"{size} {modified} {kind} {path}\n"


def op_around_line(path: str, line: int, n: int = 10) -> str:

    if not path or not os.path.isfile(path):
        return _path_not_found(path, label="file", op_name="around_line")
    if line < 1:
        return f"ERROR: line number must be >= 1, got {line}\n"

    try:
        with open(path, "r", errors="replace", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        return f"ERROR: could not read {path}: {e}\n"

    total = len(lines)
    if line > total:
        return f"ERROR: line {line} exceeds file length ({total} lines)\n"

    start = max(0, line - 1 - n)
    end = min(total, line + n)
    out = [f"({total} lines total, showing lines {start + 1}–{end})\n"]
    for i in range(start, end):
        marker = "→" if i == line - 1 else " "
        out.append(f"{i + 1:>6}{marker}{lines[i]}")
    if not lines[end - 1].endswith("\n"):
        out.append("\n")
    return "".join(out)


def op_tree(path: str, depth: int = 3,
            exclude_paths: Tuple[str, ...] = ()) -> str:

    if not path:
        path = "."
    if not os.path.isdir(path):
        return f"ERROR: not a directory: {path}\n"
    if depth < 1:
        return f"ERROR: depth must be >= 1, got {depth}\n"

    out: List[str] = []
    hidden: List[str] = []
    base = os.path.abspath(path)
    cwd = os.getcwd()




    git_tally = _GitIgnoreTally()
    view = _git_ignore_view(path) if exclude_paths else _GIT_IGNORE_NONE
    git_tally.saw(view)

    def _walk(dir_path: str, prefix: str, current_depth: int) -> None:
        if current_depth > depth:
            return
        try:
            entries = sorted(os.listdir(dir_path))
        except OSError:
            return

        entries = [e for e in entries if not e.startswith(".")]
        dirs = [e for e in entries if os.path.isdir(os.path.join(dir_path, e))]
        files = [e for e in entries if not os.path.isdir(os.path.join(dir_path, e))]

        for f in files:
            if exclude_paths:
                rel_f = _safe_relpath(os.path.join(dir_path, f), cwd)
                if _is_excluded(rel_f, exclude_paths):
                    if _is_disclosable_exclusion(rel_f, exclude_paths):
                        hidden.append(f)
                    continue
                if _is_git_ignored_file(rel_f, view):
                    git_tally.hidden.append(f)
                    continue
            out.append(f"{prefix}{f}\n")
        for d in dirs:
            if exclude_paths:
                rel = _safe_relpath(os.path.join(dir_path, d), cwd)
                if _is_excluded(rel, exclude_paths):
                    continue
                if _strip_dot_slash(rel) in view.dirs:
                    continue
            out.append(f"{prefix}{d}/\n")
            if current_depth < depth:
                _walk(os.path.join(dir_path, d), prefix + "  ", current_depth + 1)

    out.append(f"{os.path.basename(base)}/\n")
    _walk(base, "  ", 1)
    if hidden:
        out.append(f"({len(hidden)} files hidden by exclude-paths)\n")
    git_clause = git_tally.clause()
    if git_clause:
        out.append(f"({git_clause[2:]})\n")
    return "".join(out)










_TS_CHECKED = False
_TS_AVAILABLE = False
_TS_PACKAGE: str = ""  


def _has_tree_sitter() -> bool:

    global _TS_CHECKED, _TS_AVAILABLE, _TS_PACKAGE
    if not _TS_CHECKED:
        _TS_CHECKED = True
        try:
            from tree_sitter_language_pack import get_parser  
            _TS_AVAILABLE = True
            _TS_PACKAGE = "pack"
        except ImportError:
            try:
                from tree_sitter_languages import get_parser  
                _TS_AVAILABLE = True
                _TS_PACKAGE = "languages"
            except ImportError:
                _TS_AVAILABLE = False
    return _TS_AVAILABLE



_CTAGS_PATH: str | None = None
_CTAGS_CHECKED = False


def _has_ctags() -> str | None:

    global _CTAGS_PATH, _CTAGS_CHECKED
    if not _CTAGS_CHECKED:
        _CTAGS_CHECKED = True




        _CTAGS_PATH = _which_excluding_cwd("ctags")
    return _CTAGS_PATH



_TS_LANG_MAP: Dict[str, str] = {
    ".php": "php", ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "javascript", ".go": "go", ".rs": "rust",
    ".java": "java", ".rb": "ruby", ".c": "c", ".cpp": "cpp", ".h": "c",
    ".hpp": "cpp", ".cs": "c_sharp", ".swift": "swift", ".kt": "kotlin",
    ".scala": "scala", ".lua": "lua", ".sh": "bash", ".bash": "bash",
    ".md": "markdown", ".markdown": "markdown",
}






_TS_LANG_ALIASES: Dict[str, str] = {
    "c_sharp": "csharp",
}





_TS_GRAMMAR_FAILED: Dict[str, str] = {}


def _ts_get_parser(lang_name: str) -> Any:










    if lang_name in _TS_GRAMMAR_FAILED:
        raise LookupError(_TS_GRAMMAR_FAILED[lang_name])

    if _TS_PACKAGE == "pack":
        from tree_sitter_language_pack import get_parser
    else:
        from tree_sitter_languages import get_parser

    try:
        return get_parser(lang_name)
    except LookupError as first_err:
        alt = _TS_LANG_ALIASES.get(lang_name)
        if alt is None:
            alt = next(
                (k for k, v in _TS_LANG_ALIASES.items() if v == lang_name), None)
        if alt is not None:
            try:
                return get_parser(alt)
            except LookupError as second_err:
                reason = (f"neither {lang_name!r} nor {alt!r} recognised by "
                          f"the installed tree-sitter package "
                          f"({first_err}; {second_err})")
                _TS_GRAMMAR_FAILED[lang_name] = reason
                raise LookupError(reason) from second_err
        reason = f"{lang_name!r} not recognised by the installed tree-sitter package ({first_err})"
        _TS_GRAMMAR_FAILED[lang_name] = reason
        raise LookupError(reason) from first_err


_TS_DEF_NODES: Dict[str, Dict[str, str]] = {
    "php": {
        "class_declaration": "class", "interface_declaration": "interface",
        "trait_declaration": "trait", "enum_declaration": "enum",
        "method_declaration": "method", "function_definition": "function",
        "const_element": "const", "property_declaration": "property",
        "use_declaration": "use",
    },
    "python": {
        "class_definition": "class", "function_definition": "def",
    },
    "javascript": {
        "class_declaration": "class", "function_declaration": "function",
        "method_definition": "method", "arrow_function": "function",
    },
    "typescript": {
        "class_declaration": "class", "function_declaration": "function",
        "method_definition": "method", "interface_declaration": "interface",
        "type_alias_declaration": "type", "enum_declaration": "enum",
    },
    "go": {
        "type_declaration": "type", "function_declaration": "func",
        "method_declaration": "method",
    },
    "rust": {
        "struct_item": "struct", "enum_item": "enum", "trait_item": "trait",
        "function_item": "fn", "impl_item": "impl",
    },
    "java": {
        "class_declaration": "class", "interface_declaration": "interface",
        "method_declaration": "method", "enum_declaration": "enum",
    },
    "ruby": {
        "class": "class", "module": "module", "method": "def",
    },
}


_TS_DEF_NODES_DEFAULT: Dict[str, str] = {
    "class_declaration": "class", "class_definition": "class",
    "function_declaration": "function", "function_definition": "function",
    "method_declaration": "method", "method_definition": "method",
    "interface_declaration": "interface",
}







_HIERARCHY_MAX_NAMES = 4







_TS_HERITAGE_CONTAINER_TYPES = frozenset({
    "base_clause", "class_interface_clause",  
    "class_heritage",                          
    "superclass", "super_interfaces",          
    "extends_interfaces",                      
    "extends_type_clause",                     
})



_TS_HIERARCHY_LEAF_TYPES = frozenset({
    "identifier", "type_identifier", "constant", "name",
    "scoped_identifier", "qualified_name",
})


def _ts_hierarchy_names(container: Any) -> List[str]:







    names: List[str] = []

    def _walk(node: Any) -> None:
        if node.type == "keyword_argument":
            return
        if node.type in _TS_HIERARCHY_LEAF_TYPES:
            text = node.text.decode("utf-8", errors="replace").strip()
            if text:
                names.append(text)
            return
        for child in node.named_children:
            _walk(child)

    for child in container.named_children:
        _walk(child)
    return names


def _ts_class_hierarchy(node: Any) -> List[str]:




    names: List[str] = []
    for child in node.children:
        if child.type in _TS_HERITAGE_CONTAINER_TYPES:
            names.extend(_ts_hierarchy_names(child))
        elif child.type == "argument_list" and node.type == "class_definition":


            names.extend(_ts_hierarchy_names(child))
    return names


def _format_hierarchy_suffix(names: List[str]) -> str:

    if not names:
        return ""
    shown = names[:_HIERARCHY_MAX_NAMES]
    rest = len(names) - len(shown)
    suffix = ", ".join(shown)
    if rest > 0:
        suffix += f", +{rest} more"
    return f" < {suffix}"






_SYMBOL_MODIFIER_WORDS = frozenset({
    "async", "function", "func", "fn", "def", "class", "interface", "trait",
    "enum", "struct", "type", "method", "public", "private", "protected",
    "static", "final", "abstract", "readonly", "export", "default", "const",
    "let", "var", "impl", "sub", "proc",
})


def _normalize_symbol_query(symbol: str) -> str:









    s = symbol.strip()
    if "(" in s:
        s = s.split("(", 1)[0].strip()
    tokens = [t for t in s.split() if t]
    if not tokens:
        return symbol.strip()
    while len(tokens) > 1 and tokens[0].lower() in _SYMBOL_MODIFIER_WORDS:
        tokens.pop(0)
    return tokens[0]


def _ts_parse(parser: Any, source_bytes: bytes) -> Any:

    try:
        return parser.parse(source_bytes)
    except TypeError:
        return parser.parse(source_bytes.decode("utf-8", errors="replace"))


def _ts_extract(path: str, lang_name: str) -> List[Tuple[str, str, int, int]]:





    try:
        parser = _ts_get_parser(lang_name)
    except LookupError:





        return []

    try:
        with open(path, "rb") as f:
            source = f.read()
        tree = _ts_parse(parser, source)
    except (OSError, UnicodeDecodeError) as e:
        if os.environ.get("SUPERTOOL_DEBUG"):
            print(f"[supertool debug] _ts_extract failed for {path}: {e}",
                  file=__import__("sys").stderr)
        return []

    if lang_name == "markdown":
        return _ts_extract_markdown(source, tree)

    def_nodes = _TS_DEF_NODES.get(lang_name, _TS_DEF_NODES_DEFAULT)
    symbols: List[Tuple[str, str, int, int, int]] = []

    def _walk(node: Any, depth: int = 0) -> None:
        node_type = node.type
        if node_type in def_nodes:
            kind = def_nodes[node_type]
            name = _ts_node_name(node, lang_name)
            if kind in ("class", "interface"):
                hierarchy = _ts_class_hierarchy(node)
                if hierarchy:
                    name += _format_hierarchy_suffix(hierarchy)
            line = node.start_point[0] + 1  
            end_line = node.end_point[0] + 1
            symbols.append((kind, name, line, end_line, depth))

            for child in node.children:
                _walk(child, depth + 1)
        else:
            for child in node.children:
                _walk(child, depth)

    _walk(tree.root_node)
    return symbols


_MD_HEADING_NODES = frozenset({"atx_heading", "setext_heading"})


def _ts_extract_markdown(source: bytes, tree: Any) -> List[Tuple[str, str, int, int, int]]:










    symbols: List[Tuple[str, str, int, int, int]] = []

    def _level(node: Any) -> int:
        for child in node.children:
            ctype = child.type
            if ctype.startswith("atx_h") and ctype.endswith("_marker"):
                return int(ctype[5:-7])
            if ctype.startswith("setext_h") and ctype.endswith("_underline"):
                return int(ctype[8:-10])
        return 1

    def _title(node: Any) -> str:
        for child in node.children:
            if child.type in ("inline", "paragraph", "heading_content"):
                return source[child.start_byte:child.end_byte].decode(
                    "utf-8", errors="replace").strip()
        raw = source[node.start_byte:node.end_byte].decode(
            "utf-8", errors="replace")
        return raw.splitlines()[0].lstrip("#").strip(" #").strip()

    def _walk(node: Any) -> None:
        if node.type in _MD_HEADING_NODES:
            level = _level(node)
            name = _title(node)
            if name:
                line = node.start_point[0] + 1
                symbols.append((f"h{level}", name, line, line, level - 1))
            return
        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    symbols.sort(key=lambda s: s[2])
    return symbols


def _ts_node_name(node: Any, lang_name: str) -> str:






    name_node = node.child_by_field_name("name")
    if name_node:
        return name_node.text.decode("utf-8", errors="replace")


    if node.type == "const_element" and node.children:
        return node.children[0].text.decode("utf-8", errors="replace")



    if node.type == "property_declaration":
        for child in node.children:
            if child.type == "property_element":
                for grandchild in child.children:
                    if grandchild.type == "variable_name":

                        text = grandchild.text.decode("utf-8", errors="replace")
                        return text.lstrip("$")


    for child in node.children:
        if child.type in ("identifier", "name", "type_identifier",
                          "property_identifier"):
            return child.text.decode("utf-8", errors="replace")

    return "<anonymous>"


def _ts_find_node(
    path: str, lang_name: str, name: str
) -> Tuple[Any, str, int] | None:




    try:
        parser = _ts_get_parser(lang_name)
    except LookupError:



        return None

    try:
        with open(path, "rb") as f:
            source = f.read()
        tree = _ts_parse(parser, source)
    except (OSError, UnicodeDecodeError) as e:
        if os.environ.get("SUPERTOOL_DEBUG"):
            print(f"[supertool debug] _ts_find_node failed for {path}: {e}",
                  file=__import__("sys").stderr)
        return None

    def_nodes = _TS_DEF_NODES.get(lang_name, _TS_DEF_NODES_DEFAULT)
    matches: List[Tuple[Any, str]] = []

    def _walk(node: Any) -> None:
        if node.type in def_nodes:
            if _ts_node_name(node, lang_name) == name:
                matches.append((node, def_nodes[node.type]))
        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    if not matches:
        return None
    node, kind = matches[0]
    return node, kind, len(matches)


def _ctags_extract(path: str) -> List[Tuple[str, str, int, str]]:





    ctags = _has_ctags()
    if not ctags:
        return []

    try:
        result = subprocess.run(
            [ctags, "--output-format=json", "--fields=+nKS", "-f", "-", path],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace"
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    symbols: List[Tuple[str, str, int, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            tag = json.loads(line)
        except json.JSONDecodeError:
            continue
        if tag.get("_type") != "tag":
            continue
        name = tag.get("name", "")
        kind = tag.get("kind", tag.get("kindFull", ""))
        lineno = tag.get("line", 0)
        scope = tag.get("scope", "")
        symbols.append((kind, name, lineno, scope))

    return symbols



_REGEX_PATTERNS: Dict[str, List[Tuple[str, re.Pattern[str]]]] = {
    ".php": [
        ("class", re.compile(
            r"^\s*(?:abstract\s+|final\s+)?class\s+(\w+)", re.MULTILINE)),
        ("interface", re.compile(
            r"^\s*interface\s+(\w+)", re.MULTILINE)),
        ("trait", re.compile(
            r"^\s*trait\s+(\w+)", re.MULTILINE)),
        ("enum", re.compile(
            r"^\s*enum\s+(\w+)", re.MULTILINE)),
        ("function", re.compile(
            r"^\s*(?:abstract\s+)?(?:public|protected|private|static|\s)*\s*function\s+(\w+)",
            re.MULTILINE)),
        ("const", re.compile(
            r"^\s*(?:public|protected|private)?\s*const\s+(\w+)",
            re.MULTILINE)),
    ],
    ".py": [
        ("class", re.compile(r"^class\s+(\w+)", re.MULTILINE)),
        ("def", re.compile(r"^(\s*)def\s+(\w+)", re.MULTILINE)),
    ],
    ".js": [
        ("class", re.compile(r"^\s*(?:export\s+)?class\s+(\w+)", re.MULTILINE)),
        ("function", re.compile(
            r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", re.MULTILINE)),
    ],
    ".ts": [
        ("class", re.compile(r"^\s*(?:export\s+)?class\s+(\w+)", re.MULTILINE)),
        ("interface", re.compile(
            r"^\s*(?:export\s+)?interface\s+(\w+)", re.MULTILINE)),
        ("type", re.compile(
            r"^\s*(?:export\s+)?type\s+(\w+)", re.MULTILINE)),
        ("function", re.compile(
            r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", re.MULTILINE)),
        ("enum", re.compile(
            r"^\s*(?:export\s+)?enum\s+(\w+)", re.MULTILINE)),
    ],
    ".go": [
        ("type", re.compile(r"^type\s+(\w+)", re.MULTILINE)),
        ("func", re.compile(r"^func\s+(?:\([^)]+\)\s+)?(\w+)", re.MULTILINE)),
    ],
    ".rs": [
        ("struct", re.compile(
            r"^\s*(?:pub\s+)?struct\s+(\w+)", re.MULTILINE)),
        ("enum", re.compile(r"^\s*(?:pub\s+)?enum\s+(\w+)", re.MULTILINE)),
        ("trait", re.compile(r"^\s*(?:pub\s+)?trait\s+(\w+)", re.MULTILINE)),
        ("fn", re.compile(
            r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)", re.MULTILINE)),
        ("impl", re.compile(r"^\s*impl(?:<[^>]+>)?\s+(\w+)", re.MULTILINE)),
    ],
    ".java": [
        ("class", re.compile(
            r"^\s*(?:public|protected|private)?\s*(?:abstract\s+|final\s+)?class\s+(\w+)",
            re.MULTILINE)),
        ("interface", re.compile(
            r"^\s*(?:public|protected|private)?\s*interface\s+(\w+)",
            re.MULTILINE)),
        ("enum", re.compile(
            r"^\s*(?:public|protected|private)?\s*enum\s+(\w+)",
            re.MULTILINE)),
    ],
    ".rb": [
        ("class", re.compile(r"^\s*class\s+(\w+)", re.MULTILINE)),
        ("module", re.compile(r"^\s*module\s+(\w+)", re.MULTILINE)),
        ("def", re.compile(r"^\s*def\s+(\w+)", re.MULTILINE)),
    ],
}

_REGEX_PATTERNS[".tsx"] = _REGEX_PATTERNS[".ts"]
_REGEX_PATTERNS[".jsx"] = _REGEX_PATTERNS[".js"]









_HEADER_HERITAGE_RE: Dict[str, Tuple[Optional[re.Pattern[str]], Optional[re.Pattern[str]]]] = {
    ".php": (re.compile(r"extends\s+([\w\\]+)"),
             re.compile(r"implements\s+(.+)", re.DOTALL)),
    ".java": (re.compile(r"extends\s+([\w.<>]+)"),
              re.compile(r"implements\s+(.+)", re.DOTALL)),
    ".js": (re.compile(r"extends\s+([\w.$]+)"), None),
    ".ts": (re.compile(r"extends\s+([\w.$]+)"),
            re.compile(r"implements\s+(.+)", re.DOTALL)),
}
_HEADER_HERITAGE_RE[".tsx"] = _HEADER_HERITAGE_RE[".ts"]
_HEADER_HERITAGE_RE[".jsx"] = _HEADER_HERITAGE_RE[".js"]







_CLASS_HEADER_WINDOW = 2000


def _class_header_text(content: str, start: int) -> Tuple[str, bool]:









    chunk = content[start:start + _CLASS_HEADER_WINDOW]
    brace = chunk.find("{")
    if brace == -1:
        return chunk, True
    return chunk[:brace], False


def _split_top_level_commas(s: str) -> List[str]:








    parts: List[str] = []
    depth = 0
    current: List[str] = []
    opens = "([{<"
    closes = ")]}>"
    for ch in s:
        if ch in opens:
            depth += 1
        elif ch in closes:
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _leading_balanced_parens(header: str) -> Optional[str]:









    i = 0
    n = len(header)
    while i < n and header[i].isspace():
        i += 1
    if i >= n or header[i] != "(":
        return None
    depth = 0
    start = i
    for j in range(i, n):
        if header[j] == "(":
            depth += 1
        elif header[j] == ")":
            depth -= 1
            if depth == 0:
                return header[start + 1:j]
    return None  


def _header_hierarchy(ext: str, header: str) -> List[str]:





    if ext == ".py":
        inner = _leading_balanced_parens(header)
        if inner is None:
            return []
        names = []
        for part in _split_top_level_commas(inner):
            part = part.strip()
            if part and "=" not in part and not part.startswith("*"):
                names.append(part)
        return names
    if ext == ".rb":
        m = re.match(r"\s*<\s*([\w:]+)", header)
        return [m.group(1)] if m else []
    spec = _HEADER_HERITAGE_RE.get(ext)
    if not spec:
        return []
    names = []
    extends_re, implements_re = spec
    if extends_re:
        m = extends_re.search(header)
        if m:
            names.append(m.group(1).strip())
    if implements_re:
        m = implements_re.search(header)
        if m:
            names.extend(
                part.strip()
                for part in _split_top_level_commas(m.group(1))
                if part.strip()
            )
    return names


def _regex_extract(path: str) -> List[Tuple[str, str, int, int, int]]:






    ext = os.path.splitext(path)[1].lower()
    patterns = _REGEX_PATTERNS.get(ext)
    if not patterns:
        return []

    try:
        with open(path, "r", errors="replace", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return []

    symbols: List[Tuple[str, str, int, int, int]] = []

    for kind, regex in patterns:
        for m in regex.finditer(content):
            line_num = content[:m.start()].count("\n") + 1
            if ext == ".py" and kind == "def":

                indent = m.group(1)
                name = m.group(2)
                depth = 1 if len(indent) > 0 else 0
            else:
                name = m.group(1)
                depth = 0
            if kind in ("class", "interface"):
                header, truncated = _class_header_text(content, m.end())






                if truncated and ext not in (".py", ".rb"):






                    name += " < ?"
                else:
                    hierarchy = _header_hierarchy(ext, header)
                    if hierarchy:
                        name += _format_hierarchy_suffix(hierarchy)
            symbols.append((kind, name, line_num, line_num, depth))


    symbols.sort(key=lambda s: s[2])
    return symbols


def _format_map_symbols(
    symbols: List[Tuple[str, str, int, int, int]], path: str, line_count: int
) -> str:

    out = [f"{_fwd(path)} ({line_count} lines)\n"]
    for kind, name, line, end_line, depth in symbols:
        indent = "  " * (depth + 1)
        label = f"[{line}]" if line == end_line else f"[{line}-{end_line}]"
        out.append(f"{indent}{kind} {name}  {label}\n")
    return "".join(out)


def _format_ctags_symbols(
    symbols: List[Tuple[str, str, int, str]], path: str, line_count: int
) -> str:




    out = [f"{_fwd(path)} ({line_count} lines)\n"]
    for kind, name, line, scope in symbols:
        depth = 1 if scope else 0
        indent = "  " * (depth + 1)
        out.append(f"{indent}{kind} {name}  [{line}]\n")
    return "".join(out)



_MAP_EXTENSIONS = frozenset(
    list(_TS_LANG_MAP.keys()) + list(_REGEX_PATTERNS.keys())
)


def _collect_files(
    path: str, exclude_paths: Tuple[str, ...],
    hidden: Optional[List[str]] = None,
    git_tally: Optional["_GitIgnoreTally"] = None,
) -> List[str]:










    skip_dirs = {"vendor", "Generated", ".claude", ".max"}

    if os.path.isfile(path):
        return [path]

    if not os.path.isdir(path):
        return []

    cwd = os.getcwd()
    files: List[str] = []


    view = _git_ignore_view(path) if exclude_paths else _GIT_IGNORE_NONE
    if git_tally is not None:
        git_tally.saw(view)
    for root, dirs, filenames in os.walk(path):
        rel_root = _safe_relpath(root, cwd)
        dirs[:] = sorted(
            d for d in dirs
            if d not in skip_dirs
            and not d.startswith(".")
            and not (exclude_paths and _is_excluded(os.path.join(rel_root, d), exclude_paths))
            and not _is_git_ignored(rel_root, d, view.dirs)
        )
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in _MAP_EXTENSIONS:
                continue
            rel_fn = os.path.join(rel_root, fn)
            if exclude_paths and _is_excluded(rel_fn, exclude_paths):
                if hidden is not None and _is_disclosable_exclusion(
                        rel_fn, exclude_paths):
                    hidden.append(os.path.join(root, fn))
                continue
            if _is_git_ignored_file(rel_fn, view):
                if git_tally is not None:
                    git_tally.hidden.append(os.path.join(root, fn))
                continue
            files.append(os.path.join(root, fn))
    return files


MAX_MAP_FILES = 100  




_NO_PARSER_MARKER = "no symbol parser for "




_UNREADABLE_MARKER = "could not read"


def _map_unreadable_reason(path: str) -> str:




















    try:
        with open(path, "rb") as fh:
            fh.read(1)
    except OSError as exc:
        return exc.strerror or str(exc)
    return ""


def _map_no_parser_reason(ext: str, use_ts: bool, ctags_ran: bool) -> str:





















    ts_lang = _TS_LANG_MAP.get(ext) if use_ts else None
    if ts_lang and ts_lang not in _TS_GRAMMAR_FAILED:
        return ""
    if ext in _REGEX_PATTERNS:
        return ""

    if not ext:
        ext = "(no extension)"
    if use_ts:
        detail = f"tree-sitter and the regex tier have no {ext} grammar"
    else:
        detail = (f"tree-sitter is not installed and the regex tier has no "
                  f"{ext} patterns")
    if ctags_ran:
        detail += "; ctags found nothing"
    return f"{_NO_PARSER_MARKER}{ext} - {detail}"


def _ts_tier_is_blind(ext: str, use_ts: bool) -> bool:
















    if not use_ts:
        return True
    lang_name = _TS_LANG_MAP.get(ext)
    return not lang_name or lang_name in _TS_GRAMMAR_FAILED


def op_map(path: str, no_exclude: bool = False) -> str:












    if not path:
        return "ERROR: empty path\n"
    if not os.path.exists(path):
        return _path_not_found(path, op_name="map", call_prefix="map")

    hidden_files: List[str] = []
    git_tally = _GitIgnoreTally()
    files = _collect_files(
        path, _get_exclude_paths("map", no_exclude), hidden_files, git_tally)
    if not files:
        return (f"(no supported files found in {path}"
                f"{_hidden_suffix(len(hidden_files))}{git_tally.clause()})\n")

    truncated = len(files) > MAX_MAP_FILES
    files = files[:MAX_MAP_FILES]





    use_ts = _has_tree_sitter()


    actual_tier: str = "regex"
    unparsed = 0
    unreadable = 0

    out_files: List[str] = []

    for fpath in files:
        ext = os.path.splitext(fpath)[1].lower()



        unreadable_why = _map_unreadable_reason(fpath)
        if unreadable_why:
            out_files.append(
                f"{_fwd(fpath)} (unreadable)\n"
                f"  ({_UNREADABLE_MARKER}: {unreadable_why} — no tier looked "
                f"at this file, so nothing here is a claim about its "
                f"contents)\n")
            unreadable += 1
            continue
        line_count = _count_lines(fpath)

        symbols_found = False

        if use_ts:
            lang_name = _TS_LANG_MAP.get(ext)
            if lang_name:
                symbols = _ts_extract(fpath, lang_name)
                if symbols:
                    out_files.append(_format_map_symbols(symbols, fpath, line_count))
                    symbols_found = True
                    actual_tier = "tree-sitter"

        ctags_ran = False
        if (not symbols_found and _ts_tier_is_blind(ext, use_ts)
                and _has_ctags()):
            ctags_ran = True
            symbols_ct = _ctags_extract(fpath)
            if symbols_ct:
                out_files.append(_format_ctags_symbols(
                    symbols_ct, fpath, line_count))
                symbols_found = True
                actual_tier = "ctags"

        if not symbols_found:
            symbols_rx = _regex_extract(fpath)
            if symbols_rx:
                out_files.append(_format_map_symbols(
                    symbols_rx, fpath, line_count))
                symbols_found = True

        if not symbols_found:
            no_parser = _map_no_parser_reason(ext, use_ts, ctags_ran)
            ts_lang = _TS_LANG_MAP.get(ext) if use_ts else None
            if no_parser:



                out_files.append(
                    f"{_fwd(fpath)} ({line_count} lines)\n  ({no_parser})\n")
                unparsed += 1
            elif ts_lang and ts_lang in _TS_GRAMMAR_FAILED:




                out_files.append(
                    f"{_fwd(fpath)} ({line_count} lines)\n"
                    f"  (tree-sitter grammar unavailable for {ext}: "
                    f"{_TS_GRAMMAR_FAILED[ts_lang]} - no symbols from any tier)\n")
            else:

                out_files.append(f"{_fwd(fpath)} ({line_count} lines)\n  (no symbols)\n")

    if unparsed + unreadable == len(files):



        actual_tier = "none"
    out = [f"({len(files)} files{_hidden_suffix(len(hidden_files))}"
           f"{git_tally.clause()}, tier: {actual_tier})\n"] + out_files




    out.append(_shim_facade_surface_note(path))
    if truncated:
        out.append(f"\n... (truncated at {MAX_MAP_FILES} files)\n")
    out.append("\n")
    return "".join(out)


def _glob_files(
    pattern: str, exclude_paths: Tuple[str, ...] = (), over_fetch: int = 0,
    hidden: Optional[List[str]] = None,
    git_tally: Optional["_GitIgnoreTally"] = None,
) -> List[str]:




















    max_results = _get_op_int("glob", "max_results", MAX_GLOB_RESULTS) + over_fetch


    expanded = _expand_braces(pattern)
    if expanded != [pattern]:
        seen: set = set()
        results: List[str] = []
        for sub_pattern in expanded:
            for f in _glob_files(sub_pattern, exclude_paths, over_fetch, hidden,
                                 git_tally):
                if f not in seen:
                    seen.add(f)
                    results.append(f)
                    if len(results) >= max_results:
                        return results
        return results

    if exclude_paths and "**" in pattern and pattern.count("**") == 1:





        import fnmatch
        star_idx = pattern.index("**")
        root_part = pattern[:star_idx].rstrip("/").rstrip(os.sep) or "."
        tail = pattern[star_idx + 2:].lstrip("/").lstrip(os.sep)
        if not os.path.isdir(root_part):
            root_part = "."
            tail = pattern.lstrip("/").lstrip(os.sep)

        cwd = os.getcwd()
        view = _git_ignore_view(root_part)
        if git_tally is not None:
            git_tally.saw(view)
        ignored = view.dirs
        files: List[str] = []
        for root, dirs, filenames in os.walk(root_part):
            rel_root = _safe_relpath(root, cwd)
            dirs[:] = sorted(
                d for d in dirs
                if not _is_excluded(os.path.join(rel_root, d), exclude_paths)
                and not _is_git_ignored(rel_root, d, ignored)
            )
            for name in sorted(filenames):
                full = os.path.join(root, name)

                rel_from_root = _safe_relpath(full, root_part)
                if not tail or fnmatch.fnmatch(name, tail) or fnmatch.fnmatch(rel_from_root, tail):
                    if os.path.isfile(full):
                        rel_full = _safe_relpath(full, cwd)
                        if _is_excluded(rel_full, exclude_paths):
                            if hidden is not None and (
                                    _is_disclosable_exclusion(
                                        rel_full, exclude_paths)):
                                hidden.append(full)
                            continue
                        if _is_git_ignored_file(rel_full, view):
                            if git_tally is not None:
                                git_tally.hidden.append(full)
                            continue
                        files.append(full)
                        if len(files) >= max_results:
                            return files
        return files

    from glob import glob




    glob_kwargs: Dict[str, Any] = {"recursive": True}
    if not exclude_paths and sys.version_info >= (3, 11):
        glob_kwargs["include_hidden"] = True
    matches = sorted(glob(pattern, **glob_kwargs))
    files_out = [m for m in matches if os.path.isfile(m)]
    if exclude_paths:
        cwd = os.getcwd()


        view = _git_ignore_view(_glob_ignore_root(pattern))
        if git_tally is not None:
            git_tally.saw(view)
        ignored = view.dirs
        if hidden is not None:
            hidden.extend(
                m for m in files_out
                if _is_disclosable_exclusion(
                    _safe_relpath(m, cwd), exclude_paths)
            )
        kept = []
        for m in files_out:
            rel_m = _safe_relpath(m, cwd)
            if (_is_excluded(rel_m, exclude_paths)
                    or _under_git_ignored(rel_m, ignored)):
                continue
            if _is_git_ignored_file(rel_m, view):
                if git_tally is not None:
                    git_tally.hidden.append(m)
                continue
            kept.append(m)
        files_out = kept
    return files_out[:max_results]


def _glob_ignore_root(pattern: str) -> str:







    head = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
    directory = head if head.endswith(("/", os.sep)) else os.path.dirname(head)
    return directory if directory and os.path.isdir(directory) else "."
