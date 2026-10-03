"""_supertool_read -- the read-family ops, split out of _supertool.py (#2706).

Loaded by `_load_part("_supertool_read")` from inside `_supertool.py`, at the
exact source position this code used to occupy: a plain `exec(code,
globals())` via `_load_part`, not a real `import`. Every function defined
below therefore has `__globals__ is _supertool.__dict__` once loaded, so any
`monkeypatch.setattr(supertool, "op_read", ...)` (or any other name defined
here) keeps reaching the code it patches.

Not importable on its own. `_load_part` is the only legitimate loader: it
puts `_load_part` itself into the globals this file executes against before
running it, which is exactly the marker the guard below checks for. A bare
`import _supertool_read` or `python3 _supertool_read.py` gets this module's
own fresh globals(), which has no such name, and refuses with a clear
ImportError rather than failing later with a NameError on the first name this
file assumes `_supertool.py` already defined (Dict, Any, os, re, shlex, ...).

This part holds `render_file`, `op_read`, `op_glob`, `op_ls`, `op_head`,
`op_tail`, `op_wc`, `op_stat`, `op_tree` and `op_map` (the tree-sitter/ctags
symbol map), plus every private helper the #2706 split found had no caller
outside this cluster -- the read-side modify/elision/freshness machinery and
the tree-sitter/ctags extraction internals `op_map` alone uses. A handful of
formatting helpers physically defined in `_supertool_grep.py` (the pattern
gate, `_hidden_suffix`, `_truncation_suffix`, `_scanned_suffix`, `_count_lines`)
are still called from here (`op_read`'s `grep=` filter, `op_glob`, `op_map`'s
own truncation disclosure) -- that is not a layering violation: both parts
share one globals() dict by the time any op actually runs, so a cross-part
call resolves exactly like a same-file one always did.

`op_check` and `op_diff` physically sat inside this same source span but are
unrelated to reading a file (a declared-preset check runner and a two-file
diff) -- #2706 relocated those two small functions to just above the
`_load_part` calls in `_supertool.py` itself, rather than smuggling them into
either part, since neither belongs to read or grep.
"""
from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_read.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )




# ---------------------------------------------------------------------------
# Core operations (pure functions — all return the string to emit)
# ---------------------------------------------------------------------------

def _read_narrowing_hint(path: str) -> str:
    """The two ways out of a whole-file read that did not come back whole (#1811).

    A large `read:PATH` renders the file's **head** and stops. The head is the
    region least likely to be why the file was read, so the preview is often
    paid for and unusable — and until now the stop was disclosed with no way
    out attached: `... (N more lines)` named no remedy at all, and the byte-cap
    footer named only `read:PATH:OFFSET:LIMIT`, the one spelling this repo has
    three issues of evidence that callers read as START:END (#382, #1417,
    #1489). Naming the remedy where the caller is standing is what `between:`'s
    refusal already does: it suggests its own narrowing form at the moment it
    declines, rather than after the reader has paid.

    The preview is kept. Dropping it in favour of a structure block was the
    other candidate and loses more than it saves: the head is what tells the
    caller what kind of file this is, which is the input to writing the
    narrowing call these two lines ask for.
    """
    return (f"    ↳ that is the head of the file — for a region, "
            f"read:{path}:START-END; to find one, "
            f"read:{path}:::grep=PATTERN" + "\n")


def render_file(path: str, offset: int = 0, limit: int = 0,
                grep_filter: str = "", force_full: bool = False,
                range_form: bool = False, *,
                limit_defaulted: bool | None = None) -> str:
    """Emit a file's contents with line numbers, truncated at caps.

    Shared by read: and by grep/glob auto-promote branches.
    When grep_filter is set, only lines matching the regex are shown (with
    original line numbers preserved).
    When rtk is available and no special options are used, delegates to
    rtk read for compressed output.

    Enforces _safe_path containment (closes #146) — the path must resolve
    under cwd unless SUPERTOOL_ALLOW_OUTSIDE_CWD=1 is set. Catches read
    attempts against /etc/passwd, ~/.ssh/*, .max/*token*, etc.
    """
    try:
        _safe_path(path)
    except SecurityError as e:
        return f"ERROR: {e}\n"
    # Behind the check that just cleared it (#1300). Dispatch already hands
    # this an expanded path; the rebind is for the internal callers this
    # chokepoint exists to catch — alias expansion and direct `render_file`
    # calls — so the string opened is the string checked here too.
    path = _expand_home(path)
    # A caller who names no LIMIT alongside `grep=` is asking about the file,
    # not about the file's first `read.max_lines` lines (#1052). Recorded here,
    # before the default lands, because afterwards the two are indistinguishable.
    filter_scan_all = bool(grep_filter) and limit <= 0
    # Which of two bounds the window note will be naming (#1820). "The limit"
    # was one word for two facts: a LIMIT the caller typed, which is a window
    # they closed themselves and needs no action, and the `read.max_lines`
    # default below, which is the op's own cap on output standing in for a
    # bound nobody named — the one that hides the rest of the file. Recorded
    # here, before the default lands, because afterwards they are the same
    # integer (the same reason `filter_scan_all` is recorded on the line above).
    # `filter_scan_all` is excluded: it re-synthesises LIMIT to reach EOF, so
    # the bound that ends that read is the file, not either of these two.
    # Three states, not two, and the default is the third. `False` is a
    # positive claim — "the caller typed this bound" — and a call site that
    # pre-resolves `read.max_lines` into the LIMIT slot cannot be told from one
    # that typed 300, so a `False` default is inherited silently by exactly the
    # callers who know least. All three auto-read sites did that, and the
    # footer then told a `glob:PATH` caller `lines 1-300 are the whole window
    # asked for, nothing was cut` while the default had cut 100 lines and
    # nobody had asked for a window at all. `None` means the caller did not
    # say; neither verdict is printed, which is the honest answer rather than
    # the flattering one.
    if limit_defaulted is None and limit <= 0 and not filter_scan_all:
        limit_defaulted = True
    if limit <= 0:
        limit = _get_op_int("read", "max_lines", MAX_READ_LINES)
    if not path or not os.path.isfile(path):
        return _path_not_found(path, label="file", op="read",
                               call_prefix="read")

    # #1786: set before the branch below runs, so the native path further
    # down can tell "rtk was never tried" from "rtk was tried and its output
    # was discarded as malformed" -- both leave `rtk_out` unset, but only the
    # second is worth disclosing to the caller.
    rtk_malformed = False
    # RTK delegation — simple reads without offset/filter/limit changes
    if not grep_filter and offset == 0 and limit == _get_op_int("read", "max_lines", MAX_READ_LINES) and _rtk_enabled() and _has_rtk():
        rtk_args = ["read", "-n", "--max-lines", str(_get_op_int("read", "max_lines", MAX_READ_LINES))]
        rtk_used_aggressive = _is_compact()
        if rtk_used_aggressive:
            rtk_args += ["--level", "aggressive"]
        rtk_args.append(path)
        rtk_out = _rtk_run(rtk_args)
        # #1786: rtk's own compression can render a real content line
        # sandwiched between two contradicting elision markers -- a bug in
        # `rtk` itself (see `_rtk_output_looks_malformed`'s docstring), not
        # in this module. Trusting that wholesale turned a third party's
        # corruption into a plausible-looking answer about the file; a
        # malformed render is discarded here and the call falls through to
        # the native renderer below instead, which is disclosed rather than
        # silently swapped in. `aggressive=` is threaded through rather than
        # re-derived: `--level aggressive` legitimately renders several
        # elision markers in one correct output (one per compacted function
        # body), which the detector must not mistake for this corruption.
        rtk_malformed = rtk_out is not None and _rtk_output_looks_malformed(
            rtk_out, aggressive=rtk_used_aggressive)
        if rtk_out is not None and not rtk_malformed:
            # rtk renders the body, but the line-numbering disclosure is
            # supertool's own contract and rtk knows nothing about it. A
            # delegated read that stays silent is the same silence #1060 is
            # about, one layer down.
            _more = ""
            try:
                with open(path, "rb") as _probe:
                    _raw = _probe.read()
                _ambiguity = _line_break_ambiguity_note(_raw)
                # The delegated branch is the one that fires by default where
                # #1811 was reported, and it returned rtk's render unchanged —
                # so the narrowing advice added to supertool's own footers
                # below would never have reached that caller. rtk owns the
                # preview and its own "N more lines" footer; it knows nothing
                # about supertool's call forms, which is the same argument the
                # line-numbering disclosure above is here for.
                #
                # The gate is supertool's own bounds, not a parse of rtk's
                # output: a file over either cap could not have come back whole
                # from a whole-file read under any renderer. That under-fires
                # where rtk compresses a file supertool would have returned
                # intact — the safe direction, since a hint printed on a
                # complete read is advice nobody reads.
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
    # One definition of a line, shared with the ops that edit *by* line number
    # (#1060). `bytes.splitlines` already was this definition; going through the
    # helper is what stops the two sides drifting apart a second time.
    raw_lines = _split_lines_keepends(data)

    line_count = len(raw_lines)
    if filter_scan_all:
        # A filter is not a window. `read.max_lines` bounds how much is
        # *emitted*; applying it to how much is *searched* made the inline
        # filter answer `(no lines matching X)` about a file whose only match
        # sat at line 328 of 351 — a confident negative produced by the default
        # LIMIT, not by the file (#1052). The byte cap below still bounds the
        # output, and a filtered read emits only the lines that matched.
        limit = max(0, line_count - offset)
    out = [f"({line_count} lines, {size} bytes{_read_freshness_note(path)})"
           f"{_path_meta_suffix(path, b''.join(raw_lines[:64]))}\n"]
    if rtk_malformed:
        # #1786: disclosed rather than silently swapped in -- the caller
        # gets the native render either way, but only this line says the
        # first attempt was thrown away and why.
        out.append(
            "(rtk's compressed read looked malformed for this file -- "
            "falling back to the built-in renderer; #1786)\n")
    _ambiguity = _line_break_ambiguity_note(data)
    if _ambiguity:
        out.append(_ambiguity)
    bytes_emitted = 0
    printed = 0
    end = min(offset + limit, line_count)
    # When invoked outside Claude Code, the 25KB hook limit doesn't apply —
    # so `:full` from a human shell should return the whole file uncapped.
    in_claude = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "") != ""
    byte_cap = _get_op_int("read", "max_bytes", MAX_READ_BYTES)
    apply_byte_cap = in_claude or not force_full
    # Two separate lifts, and they were one flag. `full` removes the DEFAULT
    # line cap; it does not remove a window the caller typed. Without the
    # `range_form` guard, `read:f:2-4:full` from a human shell returned lines
    # 2..EOF — and the window note, keyed off the same flag, relabelled it
    # `range 2-8 (START-END form)` and then said the stop had no reason it could
    # name. That is this lane's defect one level worse than a drop: the
    # discarded END laundered by the line written to disclose it. The byte cap
    # stays lifted, which is the rest of what `full` means, so
    # `read:BIG:1-5000:full` still comes back whole (#1582).
    lift_line_cap = not apply_byte_cap and not range_form
    if lift_line_cap:
        end = line_count  # ignore line cap too when human asks for full file

    filter_regex = None
    filter_literal_why = ""
    if grep_filter:
        try:
            filter_regex = re.compile(grep_filter)
        except re.error as e:
            # The fallback is right — an unusable regex should not fail a read.
            # The silence was not: the literal search's zero was rendered in the
            # same words as a real absence, so a rejected pattern read as a
            # missing string (#1052).
            filter_regex = re.compile(re.escape(grep_filter))
            filter_literal_why = str(e)

    compact = not filter_regex and _is_compact()
    matched_any = False
    # `printed` counts *emitted* lines; the two `continue`s below advance the
    # read without incrementing it. `offset + printed` is therefore neither
    # where reading stopped nor how much of the file is left, and every count
    # in this render that was derived from it named a line that was not the
    # last one shown (#945). `last_scanned` is the 1-based index of the last
    # line the loop actually looked at, which is the only honest answer to
    # both questions.
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

    # The cap is tested AFTER a whole line has been emitted, so it never
    # truncates a line — it drops the ones that would have come next. When the
    # loop stopped at `end` there were none to drop, and calling that a
    # truncation contradicted `grep`'s byte-exact remedy in the very render it
    # sends the caller to (#1616).
    cap_cut = capped and last_scanned < end
    unsearched = 0
    # Collected rather than appended: every line in here is a note *about* the
    # scan, and it is inserted above the content at the end of this function.
    # See the insert below for why.
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
            # The loop never ran, so `last_scanned` is still `offset` and the
            # range below would read `1001-1000` -- a span whose start is past
            # its end. The count was right and the range naming it was not, and
            # a disclosure that reads as nonsense is not read (PR #1057
            # review).
            filter_notes.append(
                f"(no lines matching {grep_filter!r} -- offset "
                f"{offset + 1} is past the end of this {line_count}-line "
                f"file, so no line was searched and this is not an answer "
                f"about the file)\n")
        elif unsearched > 0:
            # Three states, not two: found, not found, and did-not-look. The
            # old single line said the second when it meant the third (#1052).
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
        # #1435, and only over the lines the loop actually read: the branch
        # above where `last_scanned == offset` has already said nothing was
        # searched, and "matches nothing here either" over an empty window
        # would contradict it with a second, more confident absence.
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
        # `cap_cut`, not `capped`: the cap is only what stopped the scan when
        # the loop broke before the window's own end. A limit that ended the
        # scan on a line that happened to carry the total over the cap left
        # lines unsearched too, and blaming the cap for them attributes the
        # limit's work to the wrong bound — the same misattribution #1616
        # removed one render over, found reviewing that fix.
        #
        # The byte cap breaks the scan loop, not just the emission: a filtered
        # read that matched and *then* hit the cap has stopped looking. This
        # used to fall through to the generic `elif capped:` wording below,
        # which offers `(R more lines)` — a phrase a reader can only take as
        # "R lines that did not match". That is the same absence-read-as-
        # presence #1052 was filed to remove, left standing in the one case
        # where the file is large enough for it to cost something (PR #1057
        # review).
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
        # `... (N more lines)` on its own is #1820 all over again, in the one
        # shape the window note cannot reach: `_read_window_note` fires only
        # when `offset > 0`, and a range starting at line 1 has an offset of 0.
        # So `read:PATH:1-50` — asked for, delivered whole — and a plain
        # `read:PATH` the `read.max_lines` default cut at 50 closed with the
        # SAME bare footer, byte for byte. One caller has everything they
        # asked for and the other is missing 150 lines to a bound they never
        # set. Found by the audit of the first commit of this fix, which is
        # why it is a second hunk rather than part of the first.
        why = ""
        if limit_defaulted is True:
            why = (f" — the read.max_lines default of {limit} stopped the "
                   f"read here, not the file")
        elif limit_defaulted is False and last_scanned >= offset + limit:
            # `nothing was cut` is claimed only when nothing was suppressed.
            # Under compact mode blanks and comments ARE dropped from the
            # output, and this footer — unlike the window note — has no `held`
            # clause beside it to say so, so the bare phrase would be a false
            # claim standing alone. What stays true either way is which bound
            # ended the window, which is the fact #1820 is about.
            intact = "" if compact else ", nothing was cut"
            why = (f" — lines {offset + 1}-{last_scanned} are the whole window "
                   f"asked for{intact}; those {line_count - last_scanned} "
                   f"are simply below it")
        out.append(f"... ({line_count - last_scanned} more lines{why})\n")
        # #1811: this footer disclosed the stop and named no way out at all —
        # the weaker of the two, and the one a plain `read:PATH` on a long file
        # actually lands on. Gated on `limit_defaulted` rather than on
        # `offset == 0`: the caller this advice is for is the one who named no
        # window at all, and a caller who typed `read:PATH:1-50` has already
        # demonstrated they know the forms. Advice printed on every read is
        # advice nobody reads.
        if offset == 0 and limit_defaulted is True and not force_full:
            out.append(_read_narrowing_hint(path))
    elif not filter_regex and offset > 0:
        # `[complete file — no more lines]` after a windowed read was a plain
        # falsehood: lines 1-OFFSET were never emitted, and when OFFSET sits
        # past EOF *nothing* was emitted and the render still said the whole
        # file had been shown (#945). The empty case is left to the window
        # note, which is the only line that can honestly describe it.
        if printed:
            out.append(f"[end of file — lines 1-{offset} not shown]\n")
    elif not filter_regex:
        out.append("[complete file — no more lines]\n")
    if filter_notes:
        # Header position, above the content, for #955's reason rather than by
        # analogy with it: "Construction order is not render order, and a note
        # that arrives after the wrong window has already been paid for is
        # barely a note." That is an argument about what the reader has spent
        # by the time the correction reaches them, and it was made about this
        # same `out` list in this same function. A filtered read that emits
        # 20 KB of matches and only then admits 308 lines were never searched
        # charges for the wrong answer first and corrects it afterwards
        # (PR #1057 review).
        for note in reversed(filter_notes):
            out.insert(1, note)
    if offset > 0:
        # Inserted at index 1 — after the count header, before the first line
        # of content. Construction order is not render order, and a correction
        # the caller reads *after* paying for the wrong window is not a
        # disclosure (#945).
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
        # Repeated at the foot too, but only above FOOTER_ECHO_MIN_LINES
        # printed lines (#1777): the header copy is what a caller reading
        # top-down sees first, but a window long enough to fill the context
        # (`read:PATH:195:300`, a 189-line window in the reported case)
        # pushes it off-screen before the content it corrects is even
        # reached. The foot is what a caller who jumps to the tail of a long
        # read actually lands on, so the same correction has to be there too
        # -- the identical string, not a shortened restatement, so the two
        # copies cannot drift apart. Gated rather than unconditional: #1489
        # and #382 already pin "one disclosure, above the body" for a window
        # that fits on a screen (48 printed lines, neither test's window
        # anywhere near this one's problem size), and an unconditional
        # second copy broke that contract on CI for every offset>0 read,
        # however small (review round found after this PR opened).
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
    """One line naming the window the caller asked for and the window actually
    returned, emitted for every `read` with a non-zero OFFSET (#945).

    OFFSET is a *skip count*, so `read:f:19:1` renders line 20 and line 19 is
    absent from the output entirely. Nothing in the old render distinguished
    "here is line 19" from "here is a line near 19", so a caller quoting the
    result into a brief or an issue quoted the wrong line with full confidence.

    The window is disclosed rather than the semantics changed: `read:PATH:A-B`
    already spells 1-based inclusive addressing, and silently re-basing OFFSET
    would break every caller who had it right — trading a visible wrong answer
    for an invisible one.

    The correction fires for every non-range read with a non-zero OFFSET, and
    names TWO ranges: `read:PATH:{offset+1}-{offset+limit}` is the window that
    actually came back, and `read:PATH:{offset}-{offset+limit-1}` is the one a
    caller who read `:A:B` as START:END was after. Until #1417 only the second
    was offered, and only when LIMIT <= OFFSET, deferring the wider band to
    `_read_range_note` (#382 — deleted in #1489, which folded its clause into
    this line; only its gate survives, as `_range_note_fires`). That deferral
    was to a speaker itself gated — on overshoot, or LIMIT < 2*OFFSET — so
    `read:f:1:40` on a long file
    tripped neither and got no hint at all. That shape is #1138's own archetype
    and, measured over 5,598 real `read:PATH:N:M` calls in this machine's
    transcripts, 1,877 of them (34%) sat in the same hole.

    The parse is NOT changed, and the two candidates #1138 proposed were both
    measured before being declined: refusing `:N:M` outright hits all 5,549
    calls with a non-zero OFFSET, and re-reading it as START-END when N < M
    hits 3,208. Neither number is a rounding error, and a refusal that breaks a
    form callers legitimately use is a regression dressed as a fix.

    A window ends at one of FOUR bounds and the note names which: a LIMIT the
    caller typed, the `read.max_lines` default they did not (#1820 — one word
    was covering both, and they want opposite responses), the file ending, or
    the byte cap cutting it short. Naming the bound is not the same as saying
    whether it cost anything, so a window that ended at a caller-set bound with
    nothing dropped also says `nothing was cut`; EOF is excluded because it
    settles that in its own words below, and a real cap cut is the state the
    clause distinguishes from. The first
    version of this note computed the shortfall against the requested end and
    attributed all of it to EOF, so a 20KB truncation was announced as "the end
    of the file" at the top of a render whose own footer said 146 lines
    remained. When two reasons land on the same line the note says they
    coincide rather than picking the flattering one — an end it cannot
    attribute is declined, not guessed.

    `capped` and `cap_reached` are two different facts and were one flag until
    #1616. The cap is tested after a whole line has been emitted, so it drops
    the lines that would have followed and never truncates one: only when the
    loop stopped short of the window's own end did it cost anything, and that
    is `capped`. `cap_reached` alone — the output sits at the cap but the
    limit or EOF ended the read anyway — is disclosed as a fact and kept out
    of `reasons`, where it had been offered as a rival explanation for an end
    that was not in doubt.
    """
    req_start = offset + 1
    req_end = offset + limit
    if last_scanned < offset:
        last_scanned = offset + shown
    # "offset N + limit M = lines A-B" rather than "requested lines A-B":
    # LIMIT is often the 300-line default the caller never typed, and calling
    # that a request would be its own small untruth.
    # Which of the two grammars ran, named in the receipt rather than left to
    # be inferred from the numbers (#1414). `:A:B` and `:A-B` are one character
    # apart and return different windows, and until #1417 the only line that
    # spoke about the window described BOTH of them in OFFSET/LIMIT terms — so
    # a range call was reported in the vocabulary of the form it did not use.
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
    # The end of the window that actually came back, not the end that was asked
    # for. `req_end` overshoots whenever EOF or the byte cap cut the window
    # short, and offering it as "this window is read:PATH:A-B" would put a
    # claim of fact that is not a fact inside the line written to cure exactly
    # that (review of 58135ef: `read:f:8:5` on a ten-line file returned 9-10
    # and the hint named 9-13).
    got_end = last_scanned if last_scanned > offset else req_end
    # `range_form` says the caller typed `read:PATH:A-B`, whose OFFSET this
    # function only ever sees after the range was converted to one. Telling
    # that caller "OFFSET is a skip count" corrects a form they did not use,
    # and names A-1..B-1 — off by one against the lines they asked for. A
    # correct call being told it was wrong is why the range form reads as
    # non-existent even though it shipped in 0.19.0 (#983).
    if not range_form and offset > 0 and limit > 0:
        # Two spellings, and the receipt names both because they answer two
        # different questions: what came back, and what was probably meant.
        # Until #1417 only the second was offered and only when LIMIT <=
        # OFFSET, on the reasoning that `_read_range_note` (#382, since
        # deleted) owned the other band. It did not: #382 is itself gated on
        # overshoot or
        # LIMIT < 2*OFFSET, so `read:f:1:40` on a long file — #1138's own
        # archetype, and the commonest shape there is — satisfied neither gate
        # and was told nothing. A deferral to a speaker who is silent is the
        # house defect one level up: an absence produced by the tool, read as
        # an absence in the world.
        skipped_word = "line was" if offset == 1 else "lines were"
        hint = (f"; OFFSET is a skip count, so {offset} {skipped_word} skipped: "
                f"this window is read:{path}:{req_start}-{got_end}")
        # The FACT above fires always. The GUESS below — which lines the caller
        # was probably after — has two spellings and exactly one speaker.
        # Until #1489 the second belonged to `_read_range_note` (#382), which
        # printed it as a trailing `note:` UNDER the body: a correct sentence
        # arriving after the reader had already paid for the wrong window,
        # beside a `window:` line above the body that had already said
        # OFFSET:LIMIT. One question, one answer, and it goes where #1432 put
        # the rest of this disclosure.
        #
        # Which spelling is #382's gate, unchanged: an overshoot of EOF, or a
        # LIMIT that lands near its OFFSET rather than independent of it, is
        # the shape of an END misread as a LIMIT (`read:f:52:72` wanting lines
        # 52-72). Anything else is an ordinary skip-then-read, and the lines
        # that caller was after are OFFSET..OFFSET+LIMIT-1.
        if limit_synthetic:
            pass
        elif _range_note_fires(offset, limit, line_count):
            span = limit - offset + 1
            # The span of what was ASKED FOR, not of what came back: the byte
            # cap can cut the window short, and naming the lines returned here
            # would state a number the call did not produce (#1020).
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
        # Two different bounds had been sharing one word (#1820). A LIMIT the
        # caller typed is a window they closed themselves and there is nothing
        # to do about it; the `read.max_lines` default is the op's own cap on
        # output, standing in for a bound they never named, and it is the one
        # that hides the rest of the file. `read:PATH:10` reported the second
        # in the vocabulary of the first — a request the caller never made,
        # named back to them as theirs.
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
        # #1342 — EOF and the limit landing on the same line was declined as
        # indistinguishable, one line under a header that prints the file's
        # total. `last_scanned >= line_count` decides it, and the two callers
        # this separates want different next actions: one wanted everything
        # from line N and has it, the other hit a cap and needs a wider window.
        # A decline emitted where the answer is on hand is the three-state
        # contract used as a shrug, and it erodes the declines that are real.
        #
        # The other reasons are still named — decisive is not the same as
        # silent, and a caller who set the limit is entitled to know it was
        # reached. What changes is that they are no longer offered as rival
        # explanations for a question the op can answer.
        others = [r for r in reasons if r != _EOF]
        stops = (f", stopping at line {last_scanned}: {_EOF}, and "
                 + " and ".join(others)
                 + f" at the same line — the file ending settles it, "
                 f"nothing follows line {last_scanned}")
    else:
        # Reached by nothing today, and said so rather than left to be
        # discovered: since #1616 `capped` means the loop stopped short of the
        # window's own end, which excludes both EOF and the limit, so the only
        # pair that can occur is EOF+limit and that is the branch above. This
        # is the shape a future fourth reason would take, not a live path — a
        # decline where more file remains and nothing on hand says which bound
        # would move first if the other were raised.
        stops = (f", stopping at line {last_scanned}: "
                 + " and ".join(reasons)
                 + " coincide here — which one ended the window cannot be told apart")
    if (limit_ended and limit_defaulted is False and not capped
            and _EOF not in reasons):
        # The third state, and the one this note never had (#1820). Every
        # clause above names which bound closed the window; none of them said
        # whether that bound cost the caller anything. So a window returned
        # WHOLE and a window the cap cut short both opened with `stopping at
        # line N: ...`, and the only way to tell them apart was to read again,
        # wider, and compare — one extra read of the reporter's file, spent
        # purely to learn that the first one had already answered.
        #
        # EOF is excluded because #1342's branch already settles it in its own
        # words (`nothing follows line N`); a second verdict beside it would be
        # two speakers on one question, which is what #1489 removed one clause
        # over. `capped` is excluded because it is the state this distinguishes
        # from.
        #
        # The claim is about the WINDOW, not about the lines: with a `grep=`
        # filter the emitted count is smaller than the scanned count, and the
        # `held` clause above is what speaks to that.
        stops += (" — the window ends here because it was asked to, "
                  "nothing was cut")
    if cap_reached and not capped:
        # The output IS at the cap, so a wider window will lose lines and the
        # caller is entitled to know. What it did NOT do is cost this call
        # anything: the cap is tested after a whole line is emitted, so it
        # drops following lines and never truncates one, and this window ended
        # at its own limit or at EOF regardless. Stated rather than dropped
        # into `reasons`, where it was offered as a rival explanation and made
        # `read:f:2-2` on a 25 KB line report itself truncated while returning
        # every byte — contradicting the byte-exact remedy `grep` prints to
        # send callers here (#1616).
        stops += (f" — the {byte_cap}-byte cap was reached on that line and "
                  f"dropped nothing; it stops whole lines and never truncates "
                  f"one, so these bytes are complete")
    return (f"window: {asked}; returning lines {req_start}-{last_scanned} "
            f"of {line_count}{held}{stops}{hint}\n")


_READ_RANGE_RE = re.compile(r"\d+-\d+")


def _range_note_fires(offset: int, limit: int, total: int) -> bool:
    """Does `read:PATH:A:B` look like a line range misread as OFFSET:LIMIT (#382)?

    `:A:B` is OFFSET:LIMIT, but it reads like START:END to anyone who has used
    `sed -n 'A,Bp'`, and the overshoot is quiet — the output just looks long.
    Requires LIMIT > OFFSET throughout — a real limit is seldom larger than the
    point it starts from — plus one of two independent tells:

    * OFFSET+LIMIT runs past EOF (#382's original gate), or
    * LIMIT < 2*OFFSET (#1020). The filed call was `read:PATH:5370:5460` on a
      19571-line file: it does NOT overrun, so #382's note stayed silent on the
      exact shape it was written for. What gives it away instead is that 5460
      sits just past 5370 — an END line lands NEAR its START, while an
      independent LIMIT does not. The doubling threshold keeps #382's own
      counter-example (`read:PATH:10:20`, a legitimate skip-then-read) quiet.

    Disclosure rather than refusal, because both readings are legitimate here
    and there is no gate that separates them without breaking working calls.
    Until #1489 this gate had a function of its own, `_read_range_note`, which
    printed the same disclosure a second time below the body; it now decides
    only which of `_read_window_note`'s two guesses is offered.
    """
    if offset <= 0 or limit <= 0 or limit <= offset or total <= 0:
        return False
    return not (offset + limit <= total and limit >= 2 * offset)


def _abstract_lang(path: str) -> str:
    """tree-sitter language name for PATH's extension, or "" when the abstract
    read has no language table for it.

    The gate used to be `path.endswith(".php")` while `_TS_LANG_MAP` already
    covered eighteen extensions (#670) — so a TypeScript or Python user got the
    raw file, which is the thing they already had."""
    lang = _TS_LANG_MAP.get(os.path.splitext(path)[1].lower(), "")
    return "" if lang in _ABSTRACT_READ_SKIP_LANGS else lang


# Languages whose symbol map is not a stand-in for their source. A signature
# list substitutes for a function body; a heading list does not substitute for
# the prose underneath it, so `read:` on a markdown file returns the document
# (#887). `map:` still builds the heading tree — this gate is read-only.
_ABSTRACT_READ_SKIP_LANGS: FrozenSet[str] = frozenset({"markdown"})


def _abstract_map(path: str, lang: str, size_bytes: int) -> Tuple[str, str]:
    """Symbol map for PATH, or the reason there isn't a usable one.

    Returns `(map, "")` on success and `("", reason)` when the caller should
    fall back to raw source. Two ways to fail, and the caller states which:

    - **No symbols.** The parser ran and found no definitions (a data-only
      module), or no parser matched at all. Returning that empty map would
      read as "this file has no code" — the absence of an answer wearing the
      shape of one. See docs/validators.md, "Declining instead of guessing".
    - **No saving.** A map that is not smaller than the bytes this read would
      otherwise emit is a worse answer than the source. Measured on 263 real
      files across 16 languages this fires on ~4% of them, so it is rare but
      not theoretical.
    """
    body = op_map(path)
    if _UNREADABLE_MARKER in body:
        # Its own arm, not the one below (#1680). "No symbols found" about a
        # file nothing opened is the defect this state exists to end, one layer
        # up in the caller — and the tree-sitter footnote under it would
        # explain which tier ran for a file no tier received a byte of.
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


# ---------------------------------------------------------------------------
# #1329 — eliding a repeat read of a byte-identical file
# ---------------------------------------------------------------------------
# The op cannot observe whether the caller still holds the first copy. A
# re-read after a context compaction is the NORMAL case, not the edge case:
# the earlier result was evicted and the model is asking again precisely
# because it no longer has it. Nothing in this process can tell that apart
# from a redundant second ask, so the design does not try to. It bounds the
# damage instead, and every bound below is one of this repo's own rules:
#
#   - the elision is always ONE round-trip from the bytes, and the command
#     that returns them is printed in the line itself, not in the docs;
#   - it only fires inside a recency window measured from the last time
#     content was ACTUALLY returned — never bumped by an elision, or a file
#     polled every minute would be elided forever;
#   - a state file that cannot be read or written returns the content. An
#     unanswered cache is `skipped`, not silence;
#   - a file whose bytes changed is never elided, at any age. Repeat reads of
#     files that MOVED are the ones that carry information.
#
# Measured on the supertool corpus (`claude-log:cost`, #1252): unchanged
# re-reads are 0.0% of result bytes, because batching already prevents the
# pattern here. This is built for what it prevents, not for what it recovers.
_READ_ELIDE_KIND = "read-elide"
_READ_ELIDE_WINDOW_SECONDS = 900


def _read_elide_enabled() -> bool:
    """Whether a repeat read may be elided at all.

    Routed through `_get_op_bool`, not `_get_op_int`. That helper exists for
    positive-integer thresholds and reads a configured `0` as "unset",
    substituting its own default (`val if isinstance(val, int) and val > 0`).
    `read.elide` was the first boolean here whose default is ON, so through
    that helper `"elide": 0` was documented in three places and inert — a
    switch that reports the state it was asked for and does not enter it. It
    was read inline here as the narrow fix; #1332 gave the class a helper, so
    the next switch does not have to rediscover this.

    `SUPERTOOL_READ_NO_ELIDE` is kept and checked first: it is the documented
    per-call escape and it is negative-sense, which `_get_op_bool`'s
    `SUPERTOOL_READ_ELIDE` is not.
    """
    off = os.environ.get("SUPERTOOL_READ_NO_ELIDE", "")
    if off.strip() and off.strip() != "0":
        return False
    return _get_op_bool("read", "elide", True)


def _read_elide_window() -> float:
    return float(_get_op_int("read", "elide_window_seconds",
                             _READ_ELIDE_WINDOW_SECONDS))


def _read_elide_session_key() -> str:
    """Identity that two concurrent agents must never share.

    PPID is the session proxy `caller_tag` already uses — Claude Code does not
    expose session_id to Bash tools, only to hook stdin. Nine worktrees were
    live on this machine on 2026-08-11, so the resolved cwd is mixed in too:
    two agents that somehow share a parent still key separately per tree.
    The two failure directions are not symmetric — over-keying costs one file
    returned again, under-keying suppresses content the caller never saw — so
    the key is deliberately the narrower of the two.
    """
    # USER is POSIX; Windows sets USERNAME. Neither is load-bearing on its own
    # — PPID plus the resolved cwd already separate two agents — but a "?" for
    # every Windows caller would silently drop a component of the key.
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "?"
    return "|".join((user, str(os.getppid()),
                     os.path.realpath(os.getcwd())))


def _read_elide_state_path(file_path: str) -> str:
    """One sidecar per (session, file), so concurrent supertool processes never
    read-modify-write a shared index."""
    # sha256, unlike the two path-only cache-name hashes below, because this
    # key carries USER: CodeQL flags a weak hash over identity-bearing input
    # (alert 11 on #1331). The digest is only a filename either way, but a
    # standing alert on a shipped line is a cost paid by every later reader.
    key = f"{_read_elide_session_key()}|{os.path.realpath(file_path)}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return os.path.join(str(_cache_root() / _READ_ELIDE_KIND), digest)


def _read_elide_load(file_path: str) -> "Optional[Tuple[str, float, int]]":
    """(sha256, when content was last RETURNED, bytes) — or None for no record.

    OSError is deliberately not swallowed here: the caller has to be able to
    tell "no record" from "the cache could not answer", and both must end in
    the content being returned rather than in an elision.
    """
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
    # "on disk", not "not returned": a >20KB file is byte-capped on the way
    # out (`apply_byte_cap = in_claude or not force_full`), so the file's size
    # and the bytes the first read actually handed over are not the same
    # number. Naming the file's size as the withheld amount would overstate it
    # on exactly the files where the cap bites.
    stamp = datetime.fromtimestamp(when).strftime("%H:%M:%S")
    return (f"[read elided — {file_path} is byte-identical to your read at "
            f"{stamp} (sha256 {digest[:12]}, {size:,} bytes on disk), so this "
            f"would return what you already have. If you no longer have it: "
            f"read:{file_path}:full]\n")


def _read_elide(path: str, offset: int, limit: int, grep_filter: str,
                force_full: bool, range_form: bool) -> str:
    """The elision line, or "" meaning "return the content".

    Records on every path that returns content, including `full` — after a
    forced read the caller demonstrably holds the bytes again.
    """
    if offset or limit or grep_filter or range_form:
        # A recorded whole-file read says nothing about a slice request, and
        # a slice must not arm an elision of the whole file.
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
        # Missing, a directory, unreadable — render_file owns that message.
        # (Windows raises PermissionError where POSIX raises
        # IsADirectoryError; both are OSError, which is why this catches the
        # base class rather than either name.)
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
        pass  # A cache that cannot be written never suppresses content.
    return ""


def op_read(path: str, offset: int = 0, limit: int = 0,
            grep_filter: str = "", force_full: bool = False,
            range_form: bool = False) -> str:
    # `grep=` is a filter on a window rather than a search across a tree, and
    # until #1344 that difference was expressed by reaching neither the BRE
    # rewrite nor the saturation refusal — so `read:PATH:::grep=^|x` returned
    # the whole file under an ordinary header and the caller could only read
    # that as "every one of these lines matched". The gate decides; the route
    # only has to reach it. First statement in the op because a refused
    # pattern must not open the file.
    filter_note = ""
    if grep_filter:
        grep_filter, refusal, filter_note = _pattern_gate(grep_filter)
        if refusal:
            return refusal
    # Abstract mode — when enabled, read:PATH on a file in a language
    # tree-sitter knows, with no offset/limit/grep, returns the symbol map
    # (measured 3-18% of the source bytes, median ~5%). Skipped when:
    #   - the extension is not in _TS_LANG_MAP (no map to build)
    #   - file size <= threshold (small files fit raw in the cap, abstract
    #     buys nothing)
    #   - caller passes :full / :raw (force_full)
    #   - explicit offset/limit/grep
    #   - the map would be empty or no smaller than the raw read — those two
    #     fall back to source *and say so*, never silently
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
                        # B007: the binding is read *after* the loop, which is
                        # the one shape the rule cannot see.
                        for line_count, _ in enumerate(f, 1):  # noqa: B007
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
    # `grep=` with no LIMIT is left at 0 so render_file can tell "the caller
    # named no window" from "the caller asked for 300 lines". Applying the
    # default here made the two identical, and the filter then searched only
    # the first 300 lines while reporting its zero as a fact about the file
    # (#1052).
    elision = _read_elide(path, offset, limit, grep_filter, force_full,
                          range_form)
    if elision:
        return elision
    # Recorded before the default lands, and passed rather than re-derived:
    # render_file applies the same default itself, but on this route it is
    # handed the resolved 300 and cannot tell it from a LIMIT the caller typed
    # (#1820). That is the identical shape as the `grep=` case the comment
    # above describes, one argument over.
    limit_defaulted = limit <= 0 and not grep_filter
    if limit <= 0 and not grep_filter:
        limit = _get_op_int("read", "max_lines", MAX_READ_LINES)
    body = render_file(path, offset, limit, grep_filter, force_full,
                       range_form, limit_defaulted=limit_defaulted)
    # `filter_note` above the header, for the reason the filter notes inside
    # render_file are: a disclosure the caller reads after paying for the
    # output is not a disclosure.
    return skip_note + filter_note + body + _read_edit_hint(path, body)


def _modify_hint_install_dir() -> str:
    """Directory holding `supertool` / `supertool.py`, for `_modify_hint`.

    Named and exposed on its own -- rather than inlined -- so a test can
    monkeypatch it the way `presets/_st_hint.py`'s own `install_dir` already
    is, and so this file's `__file__` (this module's own real path, not the
    entry point beside it) is resolved in exactly one place.
    """
    return os.path.dirname(os.path.realpath(__file__))


def _modify_hint_wrapper_is_runnable(path: str) -> bool:
    """Best-effort probe: is `path` runnable the way `./supertool` prints it?

    Mirrors `presets/_st_hint.py`'s `_wrapper_is_runnable` (#905/#1919):
    POSIX reads the execute bit directly; Windows has none, so the fallback
    there is a `#!` shebang -- every wrapper this project's own install
    instructions produce (README.md) is a symlink to `supertool.py`, itself
    `#!/usr/bin/env python3`.
    """
    if os.name == "nt":
        try:
            with open(path, "rb") as fh:
                return fh.read(2) == b"#!"
        except OSError:
            return False
    return os.access(path, os.X_OK)


def _modify_hint_quoted_interpreter() -> str:
    """`sys.executable`, quoted for the shell that will receive it if it must be.

    Mirrors `presets/_st_hint.py`'s `_quoted_interpreter` (#1017): an
    interpreter path with a space -- the ordinary Windows install, or any
    POSIX box whose user has one in `$HOME` -- must be quoted or the printed
    remedy asks the shell to run a program named `C:\\Program`.
    """
    exe = sys.executable
    if " " not in exe:
        return exe
    return chr(34) + exe + chr(34) if os.name == "nt" else shlex.quote(exe)


def _modify_hint(op: str) -> str:
    """A runnable supertool invocation for `op`, for `_read_edit_hint`'s footer.

    Duplicates `presets/_st_hint.py`'s `st_hint` (#905/#1012) rather than
    importing it: `presets/` ships no `__init__.py` and is a tree of
    subprocess-invoked scripts, not a package core can reach into, so this
    core module keeps its own copy of the small check the way it already
    does for `parse_remote` elsewhere in this file, rather than splicing
    `presets/` onto its own `sys.path` for one helper.

    Three states, exactly as `st_hint` has (#2120): `./supertool` only when
    a runnable wrapper genuinely sits on disk beside this file -- a
    gitignored symlink present in a clone, absent in a `git worktree`
    because git does not carry a symlink into one, which is exactly where a
    dispatched agent normally stands. `sys.executable supertool.py` when
    only the entry point is there -- the worktree case, and what this
    repo's own CLAUDE.md already tells a worktree caller to run. And an
    explicit "no runnable supertool found" when neither is, rather than
    printing either literal on a guess: a wrong prefix's failure
    (`No such file or directory`) reads as "the tool is not installed", not
    "one character of the suggestion was wrong", which spends the reader's
    trust before it spends their time.
    """
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
    """One-line nudge appended to a single-file read receipt: supertool's edit
    op bypasses the harness must-Read-first gate, so the file can be modified
    without a harness Read tool call (#309). Scoped to successful single-file
    reads only -- render_file errors get no hint, and grep/glob multi-file
    branches don't route through op_read."""
    if body.startswith("ERROR:"):
        return ""
    # `_flat_field`, for the reason #1019 flattened the `--- <op> ---` header
    # two lines above this one and the `edited …` line in `op_edit` — and this
    # footer was left raw, on the same op, in the same receipt (#1569). A
    # filename is whatever the filesystem accepted, `str.splitlines()` breaks
    # on ten separators (#886), and the tail of a forged name lands at column 0
    # inside what reads as a command to run. The name is disclosed rather than
    # censored: the hint is unrunnable either way for such a file, and one
    # naming a path that is not the one read would be worse than a broken one.
    op = f"edit:::OLD:::NEW:::{_flat_field(path, disclose_newline=True)}"
    return (f"{mark(chr(0x21B3))} to modify: {_modify_hint(op)}"
            f"  (or edit:@- ; no harness Read needed)\n")


def op_glob(pattern: str, no_exclude: bool = False, no_auto_read: bool = False) -> str:
    """Find files matching pattern. Auto-reads concrete file paths unless no_auto_read."""
    if not pattern:
        return "ERROR: empty pattern\n"
    # `glob` is the one path-taking op that does NOT expand `~` (#1300), and it
    # says so rather than answering `(0 files)` — a zero the tool manufactured,
    # indistinguishable from a real empty directory, which is this codebase's
    # house defect.
    #
    # Why refuse instead of expanding, when every other op now expands: the
    # expansion elsewhere is written back *behind* `_safe_path`, so it can only
    # name what the gate already cleared. `glob` is not in
    # `_PATH_ARG_POSITIONS` and resolves its own pattern, so the gate it sits
    # behind is the one below rather than dispatch's — and that gate reads the
    # pattern, not a path, so it cannot approve an expansion of one. Expanding
    # here would widen what the op reaches rather than fix what it says. The
    # honest half of #1300, kept deliberately after #1366.
    if pattern.startswith("~"):
        _eg = _fwd(os.path.join(os.path.expanduser("~"), "*.txt"))
        return ("ERROR: unsupported path form: glob does not expand `~` — "
                f"pass an absolute path (e.g. glob:{_eg}).\n")

    # Containment, first half: the pattern's own reach, before disk is touched
    # (#1366). Until this landed `glob` was outside the gate entirely, so
    # `glob:/tmp/x/*.txt` listed what `read:/tmp/x/f.txt` refused — filenames
    # rather than bytes, which is an existence oracle across the boundary
    # (#1135, #1142). It refuses rather than answering `(0 files)`, the zero
    # the tool manufactures.
    _pattern_err = _glob_pattern_containment_error(pattern)
    if _pattern_err:
        return _pattern_err

    # Auto-promote: concrete path with no wildcards that points to a file
    if not WILDCARD_CHARS.search(pattern) and os.path.isfile(pattern):
        if no_auto_read:
            return f"{pattern}\n"
        return ("[auto-read: concrete path, no wildcards]\n"
                + render_file(pattern, 0,
                              _get_op_int("read", "max_lines", MAX_READ_LINES),
                              limit_defaulted=True))

    excl = _get_exclude_paths("glob", no_exclude)
    # over_fetch=1 (#448): `(N files)` implies completeness the same way grep's
    # header did, and one file past the cap is what distinguishes "N matched"
    # from "N shown".
    cap = _get_op_int("glob", "max_results", MAX_GLOB_RESULTS)
    hidden_files: List[str] = []
    git_tally = _GitIgnoreTally()
    files = _glob_files(pattern, excl, over_fetch=1, hidden=hidden_files,
                        git_tally=git_tally)
    # glob is CWD-relative — `glob.glob` and the `os.walk` branch both resolve
    # against `os.getcwd()`, and so does the containment gate above. (This
    # comment and the `reads.md` row said "repo root" until #1366; from a
    # subdirectory that is a different directory and the wrong answer.) So a
    # pattern naming a mid-path segment
    # (`SiBrief/**/*.php` for a dir nested under Dvsi/src2/) returns 0 while the
    # same segment works fine in grep. Retry once with a `**/` prefix so both
    # ops accept the same mental model (#363).
    midpath_note = ""
    # `~` is not in this tuple: a tilde pattern returns above and never
    # reaches the retry, so listing it here would be a guard for a case that
    # cannot arrive (#1300).
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
    # Containment, second half: what the pattern could not predict (#1366). A
    # wildcard component can land on a symlink pointing out of the tree, so the
    # pattern is contained and the match is not. Checked BEFORE the cap, or a
    # cap-length prefix would decide whether the call is refused.
    #
    # And before the exclude-paths filter, which is the ordering half of #1392.
    # `_glob_files` drops excluded entries into `hidden_files`, so a pattern
    # that climbed out into `~/.ssh` had every match filtered away and the
    # containment check then saw an empty list: the call rendered as
    # `(0 files, 6 files hidden by exclude-paths)`, which is a refusal turned
    # into exact cardinality about a directory outside the boundary. The
    # excluded entries are checked too — being excluded is a reason not to
    # *print* a file, never a reason not to *gate* it.
    #
    # Refuses the whole call, names no file, and does not say how many (#1392).
    # Dropping the offending entries would hand back a narrowed list with an
    # honest-looking `(N files)` header — the population-narrowed-in-silence
    # defect this gate exists to prevent — printing them would disclose the
    # outside paths the refusal is about, and the count is the third form of
    # the same disclosure: `51 match(es)` and `2 match(es)` are two different
    # answers about a directory the caller may not read.
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
    # Strip common directory prefix when 2+ files share one
    prefix = ""
    if len(files) >= 2:
        prefix = os.path.commonpath(files)
        if prefix and not prefix.endswith(os.sep):
            prefix += os.sep
        # Only strip if it saves something meaningful (> 10 chars)
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

    # Auto-read: glob returned exactly 1 file — save the follow-up read round-trip.
    # Gated on BOTH byte size and line count (#362): see op_grep for rationale.
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
    """True when text overflows the byte cap AND holds a line long enough that
    line-based head/tail/wc return one giant useless line. Catches both pure
    single-line blobs and minified bodies behind a short leading comment
    (e.g. `/* license */\\n` + 300KB of minified JS), which a plain
    "no newline in the first chunk" test misses (#240).
    """
    if len(probe) <= MAX_READ_BYTES:
        return False
    longest = max((len(seg) for seg in probe.split("\n")), default=0)
    return longest >= MINIFIED_LINE_CHARS


def _looks_minified(path: str) -> bool:
    """Cheap minified-file detector: probes only the first MAX_READ_BYTES."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            probe = f.read(MAX_READ_BYTES + 1)
    except OSError:
        return False
    return _probe_minified(probe)


def _char_window(path: str, n_chars: int, from_end: bool = False) -> str:
    """First/last n_chars of a file as a character window, with a marker
    reporting total size. For minified files where line slicing is
    meaningless (#240).
    """
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

    # RTK delegation
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


# git's way of saying "nothing here is version-controlled" — an answer, and
# the common one for a file outside any checkout. Every other non-zero exit
# (a held index lock, a dubious-ownership refusal, a corrupt object store) is
# a lookup that did not happen, and renders as PATH_META_UNKNOWN.
_NOT_A_REPO = "not a git repository"

# The third state of the working-tree marker (#705). `?`, `!` and `m` are
# answers; their joint absence used to mean both "this file matches the index"
# and "the lookup failed", which inverts the marker's whole job on the failure
# leg — a modified file reading as clean, on every `read`.
#
# It is a token rather than a punctuation mark on purpose. The field looks
# one character wide because its busiest members are, but it is a
# space-separated token list whose members already run to `non-utf8`, `crlf`
# and `->target broken`, so nothing was costing a character. A punctuation
# mark would have had to be one the reader has no meaning for yet — and every
# free character is free precisely because it says nothing, which is the
# wrong property for the token that has to say the most. `git?` names the
# check that declined and cannot be confused with the three answers it
# replaces, none of which mention git.
PATH_META_UNKNOWN = "git?"

# The fourth state, added for #1397: the repo-root walk (`_path_meta_repo_root`)
# already climbed from this path's own directory to the filesystem root and
# found no `.git` anywhere. A per-path `git status` spawn in that state can
# only ever answer "not a git repository" (a spawn that produced nothing) or
# fail (which would render as `PATH_META_UNKNOWN` above) -- so it is skipped
# outright rather than paid on every single-file read outside a repository.
#
# Skipping the spawn must not be confused with the spawn having answered
# "clean" (no marker at all): the walk has known gaps -- an unreadable `.git`
# (permissions), or a `GIT_DIR` that escaped the #714 scrub and points
# somewhere the walk never looks -- where git would actually disagree with
# it. `PATH_META_NOT_CONSULTED` says plainly "the walk found no repository;
# git was never asked", which is a claim about what this process *checked*,
# not a claim about the file's working-tree state. Never conflate it with
# `PATH_META_UNKNOWN` (git *was* asked and failed to answer) or with a clean
# answer (nothing appended, the marker's normal silence).
PATH_META_NOT_CONSULTED = "no-git"


# One `git status` answer per repo root, reused across the paths rendered in a
# single process (#1126). The filed shape was a memo keyed by path; that cannot
# help, because the case it exists for — a batched `read` of seven files — asks
# about seven *different* paths and every lookup would miss. What repeats is the
# spawn, not the question, so this coalesces the query instead of remembering
# the answer: 6 paths cost 2 spawns rather than 6.
#
# Values, three states rather than two, so "we did not look" stays distinct from
# "we looked and it was clean" (docs/validators.md, "Declining instead of
# guessing"):
#
#   "primed"    one per-path query has been paid here; the next path escalates.
#               A lone `read` — the overwhelmingly common call — therefore costs
#               exactly what it always did, with no bulk query bolted on.
#   "declined"  the repo-wide query timed out or failed. Stay on the per-path
#               route forever in this process rather than reading its silence
#               as a clean tree. A repo with a very large ignored subtree is the
#               expected way to land here.
#   dict        {"codes": …, "taken_ns": …} — servable.
#
# Three things invalidate it, and they are the whole answer to "what is the
# correct lifetime":
#
#   1. `_atomic_write` clears it — every mutating op passes through there, so an
#      `edit` between two `read`s cannot be answered from the older snapshot.
#   2. `dispatch` clears it after any op outside `_PARALLEL_SAFE_OPS` — that is
#      what catches an index change from a preset (`git-commit`), which moves no
#      file mtime and so is invisible to (3).
#   3. A path whose mtime is at or after the snapshot instant is not served from
#      it. This is the one that covers a writer outside supertool entirely.
#
# Residual: a file rewritten by another process within the same filesystem mtime
# tick as the snapshot. Accepted knowingly, and it is a marker one call stale,
# not a marker computed against the wrong repository.
_PATH_META_BULK: Dict[str, Any] = {}


def _path_meta_bulk_drop() -> None:
    """Invalidate every snapshot, keeping the `declined` verdicts.

    A snapshot describes a tree at an instant, so a write invalidates it. A
    `declined` does not describe the tree at all — it records that this repo's
    status query does not come back inside the budget, which is a property of
    the repository (a very large ignored subtree, most likely) and is just as
    true after the write as before it. Clearing it wholesale meant the second
    path after every single edit re-paid the full 2s timeout to rediscover the
    same fact, turning a one-off cost into a per-edit one.
    """
    for key in [k for k, v in _PATH_META_BULK.items() if v != "declined"]:
        del _PATH_META_BULK[key]


#: Historical hardcoded value for both `git status` spawns behind
#: `_path_meta_suffix` -- kept as the default so an unconfigured install
#: behaves exactly as before (#1398).
_PATH_META_GIT_TIMEOUT_DEFAULT = 2


def _path_meta_git_timeout() -> int:
    """The `git status` spawn budget for `_path_meta_suffix`, in seconds.

    Was a bare `timeout=2` at both spawn sites, with no config key (#1398).
    Parallel mode spawns several of these against that same fixed budget, so
    raising worker count raised the rate of honest `git?` declines (the token
    meaning "the working-tree lookup declined, state unknown") purely as a
    function of a constant nobody could tune.

    A flat, tunable budget rather than one that scales with worker count: the
    workers here are separate `supertool` invocations (often separate
    processes with no shared coordination point), so there is nothing this
    function could read to learn "how many others are inflight right now"
    without new cross-process machinery. Making the existing constant
    configurable lets a caller who *does* know their own concurrency (a CI
    job setting `-n auto`, a batch script) raise it once; it does not attempt
    to infer that number automatically.

    No retry-once-before-declining here either: it would double the worst
    case latency of every read on a genuinely slow tree, and the issue itself
    asks that be measured before being chosen rather than guessed at. Left
    as a follow-up.

    Routed through `_get_op_int`, so an explicit `0` is refused loudly and
    the default is used instead (#1332's lesson: a threshold's `0` must not
    be silently swallowed into the default with no trace).
    """
    return _get_op_int(
        "read", "git_timeout_seconds", _PATH_META_GIT_TIMEOUT_DEFAULT)


def _path_meta_bulk_fill(root: str) -> Optional[Dict[str, Any]]:
    """One repo-wide `git status`, parsed into {relpath: XY}. None = declined.

    `-z` rather than the quoting `--porcelain` default: with NUL separators git
    emits path bytes verbatim, so a filename with a quote, a newline or a
    non-UTF-8 byte survives instead of arriving backslash-escaped and failing to
    match the path we were asked about.

    `taken_ns` is sampled *before* the spawn, so a file written while git was
    still walking the tree compares as newer than the snapshot and is refused by
    the caller's mtime check rather than answered from it.
    """
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
            # A rename or copy is two fields: the new name, then the original.
            i += 1
        codes[name.rstrip("/")] = xy
    return {"codes": codes, "taken_ns": taken_ns}


_PATH_META_ROOT_CACHE: Dict[str, str] = {}


def _path_meta_repo_root(path: str) -> str:
    """Repo root for `path`, walking the path AS WRITTEN — links unresolved.

    Deliberately not `_dirs_up_to_repo_root`, which calls `os.path.realpath`
    first. That is correct for the formatter/validator machinery it was built
    for, where the question is which config governs the real file. It is the
    wrong question here, and measurably so: a symlink `link.txt` inside repo A
    pointing at a file in repo B resolved to **B's root**, so the marker beside
    a file in A was computed from a completely different repository's status.

    The per-path query this coalesces runs with `cwd=os.path.dirname(
    os.path.abspath(path))`, so climbing from that same directory is what keeps
    the two routes answering about the same repository.
    """
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
    """This path's status letters from a repo-wide snapshot, or "" for clean.

    Ancestors are consulted because a repo-wide `git status` collapses whole
    directories: an ignore rule of `build/` yields one `!! build/` record and
    nothing for the files under it, and an untracked directory collapses the
    same way. Asked per-path, git names the file itself — so without this walk
    a `read` of an ignored file would lose its `!` marker the moment the query
    was coalesced, which is a cheaper answer that is not the same answer.
    """
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
    """Seconds as one short human token.

    Byte-identical to `presets/git/status.py::_age` (itself duplicated from
    `presets/git/worktrees.py::_age`) -- one number is what changes between a
    seconds-old file and an hours-old one, and every render in this tool that
    states an age should say it the same way rather than inventing a fourth
    spelling. Duplicated rather than imported for the same reason those two
    are: this module has no import path to `presets/git/status.py`, and a
    number this small does not earn one.
    """
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _read_freshness_note(path: str) -> str:
    """', modified Xs/Xm/Xh/Xd ago' for a read/workspace header (#1379).

    `(N lines, N bytes)` and the `[complete file -- no more lines]` footer
    beside it are both true statements about the instant this op ran, and
    carry no signal about how long ago that instant was. A reader with no
    memory -- which is every agent in this loop -- cannot tell a fact it read
    a minute ago from one it read just now unless the render says so; without
    it, `[complete file]` reads as a standing fact rather than a claim with an
    expiry, which cost one session ~20 minutes of forensics reconciling a
    17-line read against a file that had grown to 35 lines a minute later.

    One extra `os.stat` -- `_path_meta_suffix` already pays this exact cost
    for the same path a few characters later in the same header line, so this
    is not a second file-system round trip class, only a second read of a
    value the op was already about to fetch.
    """
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return ""
    return f", modified {_short_age(time.time() - mtime)} ago"


def _path_meta_suffix(path: str, sample: bytes = b"") -> str:
    """Compact suffix for read/workspace meta line. Empty when nothing notable.
    Tokens: ->target [broken] | bin | non-utf8 | ? | ! | m | x | crlf | Nd|Nw|Nmo
            | git? (the working-tree lookup declined — state unknown, not clean)
            | no-git (the repo-root walk found no `.git` at all — git was never
              asked, distinct from both a clean answer and git? (#1397))

    The `Nd|Nw|Nmo` token is a symlink-only fact as of #1379: it used to fire
    for any file over a week old, using `os.lstat`'s mtime, on the same line
    `_read_freshness_note` now prints right before this suffix using
    `os.stat`'s mtime (follows a symlink; `os.lstat` does not). For a plain
    file the two stats see the same mtime, so both used to say the identical
    age twice in two unlabeled spellings on one line -- `modified 20d ago)
    20d` -- confusable as two different facts rather than one restated.
    For a symlink they can genuinely disagree (the link's own mtime vs. the
    target's), which is a real fact worth keeping, not a duplicate to drop --
    a link's own age is exactly the kind of thing `->target` beside it is
    already disclosing. Gated on `os.path.islink` so the token now fires only
    where it says something the freshness note does not already say.
    """
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
        # Symlink-only (#1379) — see the docstring above for why. `path` is
        # re-tested rather than reusing the earlier `os.path.islink` result:
        # both are cheap `lstat`-backed checks, and duplicating the syscall
        # is a smaller cost than threading a boolean an extra 20 lines.
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

    # Answer from this process's repo-wide snapshot when there is one and it is
    # old enough to speak for this file (#1126). `code` stays None until
    # something has actually looked, so the per-path spawn below is skipped only
    # on a real answer and never on a missing one.
    code = None
    absolute = os.path.abspath(path)
    root = _path_meta_repo_root(path)
    if not root:
        # The walk climbed from this path's own directory to the filesystem
        # root and found no `.git` anywhere -- the per-path spawn below runs
        # with that exact same directory as its cwd, so it could only ever
        # report "not a git repository" too, or fail. Skip a spawn that
        # cannot produce information (#1397) and say so explicitly rather
        # than rendering as clean, which would silently paper over the
        # walk's own gaps (an unreadable `.git`, an escaped `GIT_DIR`).
        parts.append(PATH_META_NOT_CONSULTED)
        return (" " + " ".join(parts)) if parts else ""
    # A repo-wide status keys every record by the path as it sits in the tree.
    # If any component of this path is a link, the name we would look up is not
    # the name git recorded, the lookup misses, and a miss is indistinguishable
    # from clean — a modified or untracked file rendering as if it were neither.
    # One `realpath` compare is cheaper than being wrong, and the per-path query
    # below handles links correctly today.
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
                # Different drives on Windows. The walk said this path is under
                # `root`, so this should not happen; if it does, re-ask git
                # rather than invent a relative path.
                rel = ""
            if rel and not rel.startswith(os.pardir):
                code = _path_meta_bulk_code(entry["codes"], rel.replace(os.sep, "/"))

    if code is None:
        try:
            # The pathspec is the bare filename, not `path`: this runs with a
            # cwd of the file's own directory, so a path written relative with a
            # directory component in it (`sub/s.txt`, the form the CLI hands
            # over) was resolved a second time against that directory. git
            # warned on stderr, exited 0 with empty stdout, and the marker
            # silently vanished while the bulk arm answered correctly for the
            # same file — #1186. The cwd itself stays, because it is what keeps
            # this route and `_path_meta_repo_root` talking about the same
            # repository when a path crosses a repo boundary.
            #
            # `:(literal)` because a filename is not a pattern: a clean
            # `t[a].txt` globbed onto its modified sibling `ta.txt` and reported
            # that file's ` m` as its own. The bulk arm looks the name up in a
            # dict, so it was already literal — this is the same two-answers
            # divergence, in the direction that invents a marker rather than
            # losing one. The magic prefix and not the `--literal-pathspecs`
            # flag: that one has to precede the subcommand, and the shims the
            # decline tests install match on `$1` being `status` (#705). A flag
            # that silently un-shims a fixture is a test that stops testing.
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
            # No git on this machine, or the file's directory went away under us.
            # Nothing here was ever going to answer, so a decline that can never
            # resolve would be noise on every read (docs/validators.md,
            # "Declining instead of guessing").
            pass

    if code == "??":
        parts.append("?")
    elif code == "!!":
        parts.append("!")
    elif code and ("M" in code or "A" in code):
        parts.append("m")
    return (" " + " ".join(parts)) if parts else ""


def op_stat(path: str) -> str:
    """Show file/dir/symlink metadata: size, mtime, kind. Symlinks show target + broken flag."""
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
    """Show N lines of context around a specific line number."""
    if not path or not os.path.isfile(path):
        return _path_not_found(path, label="file", op="around_line")
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
    """Show directory structure with depth limit."""
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
    # Gitignored files AND directories are skipped (#2738); tree pruned neither
    # before, so it listed `private/p.txt` under an ignored `private/`. One
    # listing for the whole walk, taken from the root the caller named -- an
    # ignored root named directly comes back with an empty view and is shown.
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
        # Filter hidden files/dirs
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



# ---------------------------------------------------------------------------
# map — three-tier symbol extraction (tree-sitter → ctags → regex)
# ---------------------------------------------------------------------------

# Tree-sitter detection (lazy, cached)
# Supports two packages: tree-sitter-language-pack (newer, Python 3.10+)
# and tree-sitter-languages (older, Python 3.8-3.12)
_TS_CHECKED = False
_TS_AVAILABLE = False
_TS_PACKAGE: str = ""  # "pack" or "languages"


def _has_tree_sitter() -> bool:
    """Check if a tree-sitter language package is importable. Cached."""
    global _TS_CHECKED, _TS_AVAILABLE, _TS_PACKAGE
    if not _TS_CHECKED:
        _TS_CHECKED = True
        try:
            from tree_sitter_language_pack import get_parser  # noqa: F401
            _TS_AVAILABLE = True
            _TS_PACKAGE = "pack"
        except ImportError:
            try:
                from tree_sitter_languages import get_parser  # noqa: F401
                _TS_AVAILABLE = True
                _TS_PACKAGE = "languages"
            except ImportError:
                _TS_AVAILABLE = False
    return _TS_AVAILABLE


# ctags detection (lazy, cached)
_CTAGS_PATH: str | None = None
_CTAGS_CHECKED = False


def _has_ctags() -> str | None:
    """Return ctags binary path if available, None otherwise. Cached."""
    global _CTAGS_PATH, _CTAGS_CHECKED
    if not _CTAGS_CHECKED:
        _CTAGS_CHECKED = True
        # #2611: same class as _has_rtk()'s own fix above -- a raw which()
        # would resolve a repo-planted "ctags.exe" ahead of the real tool
        # on Windows, and the result is spawned by the caller below with
        # no cwd=.
        _CTAGS_PATH = _which_excluding_cwd("ctags")
    return _CTAGS_PATH


# Language extension → tree-sitter language name
_TS_LANG_MAP: Dict[str, str] = {
    ".php": "php", ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "javascript", ".go": "go", ".rs": "rust",
    ".java": "java", ".rb": "ruby", ".c": "c", ".cpp": "cpp", ".h": "c",
    ".hpp": "cpp", ".cs": "c_sharp", ".swift": "swift", ".kt": "kotlin",
    ".scala": "scala", ".lua": "lua", ".sh": "bash", ".bash": "bash",
    ".md": "markdown", ".markdown": "markdown",
}

# Names that differ between the two supported packages for the same
# language. tree-sitter-languages (older) used "c_sharp"; the actively
# maintained tree-sitter-language-pack calls it "csharp" (#790). Keyed
# either direction — _ts_get_parser tries both the requested name and,
# if that fails, its counterpart here.
_TS_LANG_ALIASES: Dict[str, str] = {
    "c_sharp": "csharp",
}

# lang_name -> reason, populated the first time a grammar fails to load
# under either spelling. Lets callers report "grammar unavailable"
# distinctly from "parsed fine, file has no definitions" (#790) instead
# of a LookupError silently becoming an empty result.
_TS_GRAMMAR_FAILED: Dict[str, str] = {}


def _ts_get_parser(lang_name: str) -> Any:
    """Resolve a tree-sitter parser for lang_name, trying the other
    package's spelling before giving up (#790).

    Raises LookupError, with both attempted names in the message, when
    neither spelling resolves under the installed package. Failures are
    cached in _TS_GRAMMAR_FAILED so repeated calls for the same language
    (e.g. across every file of a map: run) don't re-attempt both names
    and so callers can distinguish this from a working grammar that
    simply found nothing.
    """
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

# Tree-sitter node types that represent definitions, per language family
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

# Shared fallback for languages not in the map
_TS_DEF_NODES_DEFAULT: Dict[str, str] = {
    "class_declaration": "class", "class_definition": "class",
    "function_declaration": "function", "function_definition": "function",
    "method_declaration": "method", "method_definition": "method",
    "interface_declaration": "interface",
}


# #1954: `map` used to render a class's name and line span and nothing about
# what it inherits from -- a subclass with two methods of its own and a
# parent that supplies the rest read as a small standalone class. Capped
# so a wide interface list cannot blow the line, and the cut is SAID
# ("+N more") rather than a shorter list that silently claims completeness.
_HIERARCHY_MAX_NAMES = 4

# Node types that wrap a class/interface's parent + interface names, across
# the grammars in the issue's own table. One neutral `<` separator for every
# language rather than a language-shaped `extends`/`implements`: cheaper and
# reads consistently on a mixed-language map, at the cost of not
# distinguishing a parent class from an interface on sight -- the trade the
# issue leaves as an open call, decided here in the cheaper direction.
_TS_HERITAGE_CONTAINER_TYPES = frozenset({
    "base_clause", "class_interface_clause",  # php
    "class_heritage",                          # js/ts classes (wraps extends_clause/implements_clause)
    "superclass", "super_interfaces",          # java classes, ruby
    "extends_interfaces",                      # java interfaces: `interface Sub extends A, B`
    "extends_type_clause",                     # ts/js interfaces: `interface Sub extends A, B`
})

# Node types whose text IS a name -- recursion into a heritage clause stops
# here rather than walking into the name's own (empty) children.
_TS_HIERARCHY_LEAF_TYPES = frozenset({
    "identifier", "type_identifier", "constant", "name",
    "scoped_identifier", "qualified_name",
})


def _ts_hierarchy_names(container: Any) -> List[str]:
    """Named leaves under a heritage/interface-list node, source order.

    Recurses through wrapper nodes a grammar interposes (TS/JS nests the
    real names one level down inside `extends_clause`/`implements_clause`;
    Java nests interface names inside a `type_list`) and skips a Python
    `keyword_argument` (`metaclass=Meta` is not a base class) entirely.
    """
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
    """Base class + interfaces named by a class/interface definition node's
    own heritage clauses (#1954). Node-type driven: the same information
    sits under a different child depending on grammar, so this asks each
    known shape rather than assuming one."""
    names: List[str] = []
    for child in node.children:
        if child.type in _TS_HERITAGE_CONTAINER_TYPES:
            names.extend(_ts_hierarchy_names(child))
        elif child.type == "argument_list" and node.type == "class_definition":
            # Python: `class Foo(Base, metaclass=Meta):` -- the base list is
            # the class_definition's own argument_list, not a named clause.
            names.extend(_ts_hierarchy_names(child))
    return names


def _format_hierarchy_suffix(names: List[str]) -> str:
    """`< A, B, C` (or `, +N more`) appended to a class's own map line."""
    if not names:
        return ""
    shown = names[:_HIERARCHY_MAX_NAMES]
    rest = len(names) - len(shown)
    suffix = ", ".join(shown)
    if rest > 0:
        suffix += f", +{rest} more"
    return f" < {suffix}"


# Keywords that appear in front of a symbol when it's copy-pasted out of source
# (`async function foo`, `public static function bar`, `class Baz`). `between:`
# used to reject those verbatim strings, so a caller who typed the signature the
# way it reads in the file fell back to grep+read (#363).
_SYMBOL_MODIFIER_WORDS = frozenset({
    "async", "function", "func", "fn", "def", "class", "interface", "trait",
    "enum", "struct", "type", "method", "public", "private", "protected",
    "static", "final", "abstract", "readonly", "export", "default", "const",
    "let", "var", "impl", "sub", "proc",
})


def _normalize_symbol_query(symbol: str) -> str:
    """Reduce a source-shaped symbol query to the bare definition name.

    `async function fillAndSubmit` -> `fillAndSubmit`
    `public static function getFoo` -> `getFoo`
    `fillAndSubmit(page)` -> `fillAndSubmit`

    A query that is *only* a keyword (`function`) is returned unchanged — it
    may legitimately be the name being looked for.
    """
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
    """Parse source through tree-sitter, handling 0.25's str-only API."""
    try:
        return parser.parse(source_bytes)
    except TypeError:
        return parser.parse(source_bytes.decode("utf-8", errors="replace"))


def _ts_extract(path: str, lang_name: str) -> List[Tuple[str, str, int, int]]:
    """Extract symbols from a file using tree-sitter.

    Returns list of (kind, name, line, depth) tuples.
    depth: 0 = top-level, 1 = inside a class, 2 = nested deeper.
    """
    try:
        parser = _ts_get_parser(lang_name)
    except LookupError:
        # Grammar could not be loaded under either known spelling — recorded
        # in _TS_GRAMMAR_FAILED by _ts_get_parser. Returning [] keeps the
        # existing contract (ctags/regex tiers still get a chance below
        # this call), but the failure is now discoverable rather than
        # silently identical to "parsed fine, no definitions" (#790).
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
            line = node.start_point[0] + 1  # 0-indexed → 1-indexed
            end_line = node.end_point[0] + 1
            symbols.append((kind, name, line, end_line, depth))
            # Recurse into class/struct/impl bodies for methods
            for child in node.children:
                _walk(child, depth + 1)
        else:
            for child in node.children:
                _walk(child, depth)

    _walk(tree.root_node)
    return symbols


_MD_HEADING_NODES = frozenset({"atx_heading", "setext_heading"})


def _ts_extract_markdown(source: bytes, tree: Any) -> List[Tuple[str, str, int, int, int]]:
    """Extract the heading tree from a parsed markdown document (#887).

    Headings are markdown's symbols, but they do not fit the generic walker:
    the level lives in a marker child (`atx_h2_marker`, `setext_h1_underline`)
    rather than in the node type, the name lives in an `inline` child rather
    than a `name` field, and nesting is by level rather than by containment.

    Returned as (kind, name, line, end_line, depth) with kind "h1".."h6" and
    depth = level - 1, so `## Foo` renders one step in from `# Foo`.
    """
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
    """Extract the name from a tree-sitter definition node.

    Tries the 'name' field first, then common field names per language.
    Falls back to the first identifier child.
    """
    # Direct name field (works for most declarations)
    name_node = node.child_by_field_name("name")
    if name_node:
        return name_node.text.decode("utf-8", errors="replace")

    # PHP const_element: name is the first child
    if node.type == "const_element" and node.children:
        return node.children[0].text.decode("utf-8", errors="replace")

    # PHP property_declaration (typed props since 7.4): nested
    # property_element → variable_name → $name. Walk down to find the variable.
    if node.type == "property_declaration":
        for child in node.children:
            if child.type == "property_element":
                for grandchild in child.children:
                    if grandchild.type == "variable_name":
                        # variable_name → "$" + name child; strip the leading $
                        text = grandchild.text.decode("utf-8", errors="replace")
                        return text.lstrip("$")

    # Fallback: first identifier-like child
    for child in node.children:
        if child.type in ("identifier", "name", "type_identifier",
                          "property_identifier"):
            return child.text.decode("utf-8", errors="replace")

    return "<anonymous>"


def _ts_find_node(
    path: str, lang_name: str, name: str
) -> Tuple[Any, str, int] | None:
    """Find first definition node by name. Returns (node, kind, total_matches) or None.

    total_matches lets callers warn when a name resolves to multiple definitions.
    """
    try:
        parser = _ts_get_parser(lang_name)
    except LookupError:
        # See _ts_extract: failure is recorded in _TS_GRAMMAR_FAILED so
        # op_between_symbol can report it instead of a misleading
        # "symbol not found" (#790).
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
    """Extract symbols from a file using universal-ctags.

    Returns list of (kind_label, name, line, scope) tuples.
    scope is the parent class/function name or "" for top-level.
    """
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


# Regex patterns for symbol extraction (fallback when no tools available)
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
# .tsx and .jsx share the TS/JS patterns
_REGEX_PATTERNS[".tsx"] = _REGEX_PATTERNS[".ts"]
_REGEX_PATTERNS[".jsx"] = _REGEX_PATTERNS[".js"]


# #1954, regex tier: same gap as tree-sitter's `_ts_node_name`, and this is
# the tier that actually runs whenever tree-sitter is unavailable -- the
# default in this repo's own test suite. `extends`/`implements` are
# keywords here, not grammar fields, so this is a header-text regex rather
# than a node walk; `re.DOTALL` matters because a wrapped declaration (the
# PHP example the issue was filed from) puts each interface on its own
# line.
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

# 2000 chars comfortably covers any realistic wrapped extends/implements
# clause (the issue's own multi-line PHP example is under 150). Widened
# from 400 after a review measured a false "+N more" undercount on a
# synthetic 12-interface header that ran past the old window (#1954
# follow-up): the count was wrong, not just approximate, because
# `_format_hierarchy_suffix` had already lost the names past the cut.
_CLASS_HEADER_WINDOW = 2000


def _class_header_text(content: str, start: int) -> Tuple[str, bool]:
    """The declaration's own text from just past the class/interface name
    up to its opening `{`, and whether the scan window ran out first.

    Cutting at `{` keeps a later comment or method body that happens to say
    "implements" from being read as part of the header. When no `{` turns
    up inside the window, the header is truncated -- the caller must not
    report a name count computed from it as complete: better to decline
    the whole suffix than assert a number this function cannot back up.
    """
    chunk = content[start:start + _CLASS_HEADER_WINDOW]
    brace = chunk.find("{")
    if brace == -1:
        return chunk, True
    return chunk[:brace], False


def _split_top_level_commas(s: str) -> List[str]:
    """Split on `,` that is not nested inside `()`, `[]`, `{}` or `<>`.

    A Python base list can carry a parenthesized default
    (`class Foo(Base, x=(1, 2)):`) and a Java/TS interface list can carry a
    generic type argument (`implements Comparable<Foo, Bar>`) -- a plain
    `str.split(",")` cuts both in the middle and reports a garbled or
    truncated name instead of the real one.
    """
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
    """Content of the balanced `(...)` at the very start of `header` (after
    optional leading whitespace), or None if it does not start with one.

    A plain `[^)]*` regex stops at the FIRST `)`, which for
    `class Foo(Base, x=(1, 2)):` is the one closing the nested tuple
    literal -- the outer close-paren and anything after it (a later real
    base class) are silently lost. Balanced counting does not have that
    failure mode.
    """
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
    return None  # unbalanced within the scanned window


def _header_hierarchy(ext: str, header: str) -> List[str]:
    """Base class + interfaces named in a class/interface declaration's own
    header (from the name to the opening `{`). Language-shaped, because the
    keywords are: PHP/Java/JS/TS name them with `extends`/`implements`,
    Python's base list is the parens right after the name, Ruby's is `<`.
    """
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
    """Extract symbols from a file using regex patterns.

    Returns list of (kind, name, line, end_line, depth) tuples.
    Regex can't reliably detect span; end_line == line.
    depth is always 0 except indented Python `def` → depth 1.
    """
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
                # Python: indented def → depth 1
                indent = m.group(1)
                name = m.group(2)
                depth = 1 if len(indent) > 0 else 0
            else:
                name = m.group(1)
                depth = 0
            if kind in ("class", "interface"):
                header, truncated = _class_header_text(content, m.end())
                # Python (`:`) and Ruby (no closing token at all) match right
                # at the head of the header regardless of where -- or
                # whether -- the scan window ran out; only the keyword-based
                # languages (`extends`/`implements` reached via .search()
                # over the WHOLE header) can have a real base class or
                # interface sitting past a truncated window.
                if truncated and ext not in (".py", ".rb"):
                    # #2002: a truncated window and a genuinely standalone
                    # class used to render identically -- "no hierarchy" is
                    # what BOTH an absent parent and an unread one look
                    # like once `hierarchy` is simply []. `_class_header_text`
                    # already tells the two apart (`truncated`); the marker
                    # says so instead of discarding that fact here.
                    name += " < ?"
                else:
                    hierarchy = _header_hierarchy(ext, header)
                    if hierarchy:
                        name += _format_hierarchy_suffix(hierarchy)
            symbols.append((kind, name, line_num, line_num, depth))

    # Sort by line number
    symbols.sort(key=lambda s: s[2])
    return symbols


def _format_map_symbols(
    symbols: List[Tuple[str, str, int, int, int]], path: str, line_count: int
) -> str:
    """Format extracted symbols as an indented tree string."""
    out = [f"{_fwd(path)} ({line_count} lines)\n"]
    for kind, name, line, end_line, depth in symbols:
        indent = "  " * (depth + 1)
        label = f"[{line}]" if line == end_line else f"[{line}-{end_line}]"
        out.append(f"{indent}{kind} {name}  {label}\n")
    return "".join(out)


def _format_ctags_symbols(
    symbols: List[Tuple[str, str, int, str]], path: str, line_count: int
) -> str:
    """Format ctags symbols as an indented tree string.

    Uses scope field to infer nesting (symbols with a scope → depth 1).
    """
    out = [f"{_fwd(path)} ({line_count} lines)\n"]
    for kind, name, line, scope in symbols:
        depth = 1 if scope else 0
        indent = "  " * (depth + 1)
        out.append(f"{indent}{kind} {name}  [{line}]\n")
    return "".join(out)


# Supported extensions for map scanning
_MAP_EXTENSIONS = frozenset(
    list(_TS_LANG_MAP.keys()) + list(_REGEX_PATTERNS.keys())
)


def _collect_files(
    path: str, exclude_paths: Tuple[str, ...],
    hidden: Optional[List[str]] = None,
    git_tally: Optional["_GitIgnoreTally"] = None,
) -> List[str]:
    """Collect files to map from a path (file or directory).

    For directories, walks recursively. Skips hidden dirs, vendor/, Generated/,
    .claude/, .max/, and any dirs matching exclude_paths prefixes.

    `exclude_paths` is required (not defaulted) because the universal classics
    (.git/, node_modules/, etc.) live in `_DEFAULT_EXCLUDE_PATHS` and must reach
    this function via `_get_exclude_paths("map", ...)`. A defaulted empty tuple
    here would silently re-walk node_modules.
    """
    skip_dirs = {"vendor", "Generated", ".claude", ".max"}

    if os.path.isfile(path):
        return [path]

    if not os.path.isdir(path):
        return []

    cwd = os.getcwd()
    files: List[str] = []
    # Gitignored files and directories are skipped (#2738) -- map extracted
    # `def leaked` from an ignored `.py` before. One listing for the walk.
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


MAX_MAP_FILES = 100  # Cap to prevent overwhelming output

# Substring every "we could not look" render shares, so callers that key off
# map's output (`_abstract_map`) can recognise the third state without
# re-deriving which extensions have parsers.
_NO_PARSER_MARKER = "no symbol parser for "

# The fourth state (#1680). Same reason as the marker above: a caller keying
# off map's output has to be able to tell "no tier could look" from "no tier
# COULD BE RUN, because the bytes never arrived".
_UNREADABLE_MARKER = "could not read"


def _map_unreadable_reason(path: str) -> str:
    """Why no tier can look at PATH at all, or "" when they can (#1680).

    Established once, ahead of `_count_lines`, `_ts_extract` and
    `_regex_extract` — all three swallow this same `OSError` independently and
    each returns the shape of an empty answer, so `map` rendered `tier: regex`
    for a tier that read no bytes, `(0 lines)`, and `(no symbols)` for a file
    it had never opened.

    Composing the verdict out of the three instead was the other candidate and
    is not available: `_count_lines`'s `on_error` sentinel is contracted to
    differ per caller (#388), so a `0` arriving here cannot be told from a
    genuinely empty file, and the distinction is destroyed one layer down
    rather than merely unreported.

    One byte through `open`, not `os.access` or a `stat`: the question is
    whether this process can obtain the contents, and every other probe answers
    a neighbouring one. A mode that denies read, an ACL, a dangling symlink and
    a file removed between the walk and this call all arrive as `OSError` —
    which is why the reason is reported rather than classified.
    """
    try:
        with open(path, "rb") as fh:
            fh.read(1)
    except OSError as exc:
        return exc.strerror or str(exc)
    return ""


def _map_no_parser_reason(ext: str, use_ts: bool, ctags_ran: bool) -> str:
    """Why no tier could look at EXT, or "" when at least one could (#887).

    `map` had two renders for three facts: symbols, none found, and no parser
    for this file type. The third collapsed into the second, so a markdown
    file dense with headings reported `(no symbols)` — an absence produced by
    the tool, stated as an absence in the document. This computes the third
    state so the render can keep it separate; see docs/validators.md,
    "Declining instead of guessing".

    A tier counts as able to look when it has patterns or a grammar for EXT,
    not merely when it is installed. ctags is deliberately not treated as a
    parser here: the build on PATH may be BSD ctags, which cannot be queried
    for its language list — so a note is appended rather than a capability
    claimed.

    `ctags_ran` is whether ctags was actually invoked on THIS file, not
    whether a binary exists (#913). The note used to be appended from
    availability, which meant it could claim ctags "found nothing" on a run
    where ctags was never asked — the absence-produced-by-the-tool shape this
    repository keeps re-filing.
    """
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
    """True when the tree-sitter tier cannot look at EXT at all (#913).

    Distinguishes "tier 1 has no grammar for this file" — where dropping to
    ctags is the documented cascade and the only way an installed ctags ever
    earns its keep — from "tier 1 parsed the file and it has no definitions",
    where ctags would re-read the same file in a subprocess to reach the same
    answer.

    That distinction is the whole cost argument. Measured on this repo at
    100 mapped files, 14 produced no tree-sitter symbols and all 14 were the
    second kind (changelog fragments, an empty `__init__.py`); a ctags call
    costs ~41ms each on macOS, of which ~33ms is bare process spawn. Falling
    back on emptiness would buy nothing and put a per-file subprocess on a hot
    path — the shape that has reddened Windows legs four times here (#1296,
    #1360, #1461, #1501).
    """
    if not use_ts:
        return True
    lang_name = _TS_LANG_MAP.get(ext)
    return not lang_name or lang_name in _TS_GRAMMAR_FAILED


def op_map(path: str, no_exclude: bool = False) -> str:
    """Generate a symbol map of a file or directory.

    Three tiers, per file:
      1. tree-sitter (if a tree-sitter language package is installed)
      2. ctags (if `ctags` is on PATH) — consulted only for a file tier 1
         cannot parse: no grammar for the extension, or a grammar that failed
         to load. A file tier 1 read and found nothing in does not fall
         through; see `_ts_tier_is_blind` for why (#913).
      3. regex fallback (always available for supported extensions)

    Output: indented tree of classes/functions/methods per file.
    """
    if not path:
        return "ERROR: empty path\n"
    if not os.path.exists(path):
        return _path_not_found(path, op="map", call_prefix="map")

    hidden_files: List[str] = []
    git_tally = _GitIgnoreTally()
    files = _collect_files(
        path, _get_exclude_paths("map", no_exclude), hidden_files, git_tally)
    if not files:
        return (f"(no supported files found in {path}"
                f"{_hidden_suffix(len(hidden_files))}{git_tally.clause()})\n")

    truncated = len(files) > MAX_MAP_FILES
    files = files[:MAX_MAP_FILES]

    # Detect available tier. ctags is probed per file rather than once here:
    # it is reachable whenever tier 1 is blind to a file, which `use_ctags =
    # not use_ts and _has_ctags()` made impossible for the entire run as soon
    # as tree-sitter imported (#913).
    use_ts = _has_tree_sitter()

    # tier label is computed after extraction to reflect what actually produced symbols
    actual_tier: str = "regex"
    unparsed = 0
    unreadable = 0

    out_files: List[str] = []

    for fpath in files:
        ext = os.path.splitext(fpath)[1].lower()
        # Readability is established before anything reports on this file, so
        # a line count and a tier name are only ever printed for a file whose
        # bytes were actually available (#1680).
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
                # No tier has a grammar or a pattern set for this extension.
                # Saying "(no symbols)" here would report the tool's blind
                # spot as a property of the file (#887).
                out_files.append(
                    f"{_fwd(fpath)} ({line_count} lines)\n  ({no_parser})\n")
                unparsed += 1
            elif ts_lang and ts_lang in _TS_GRAMMAR_FAILED:
                # Every tier came up empty AND tree-sitter's grammar never
                # loaded for this language — say so, rather than rendering
                # byte-identical to a file with genuinely zero definitions
                # (#790).
                out_files.append(
                    f"{_fwd(fpath)} ({line_count} lines)\n"
                    f"  (tree-sitter grammar unavailable for {ext}: "
                    f"{_TS_GRAMMAR_FAILED[ts_lang]} - no symbols from any tier)\n")
            else:
                # File exists but no symbols extracted — show it as empty
                out_files.append(f"{_fwd(fpath)} ({line_count} lines)\n  (no symbols)\n")

    if unparsed + unreadable == len(files):
        # Naming a tier that never had a pattern to try is the report line
        # telling the same lie the body used to (#887) — and a tier that never
        # received a byte is the same line telling it again (#1680).
        actual_tier = "none"
    out = [f"({len(files)} files{_hidden_suffix(len(hidden_files))}"
           f"{git_tally.clause()}, tier: {actual_tier})\n"] + out_files
    # A map of the entry-point shim is the module's surface to whoever reads
    # it, and it is not (#1272). Fires on the shim named directly — a map of
    # the *directory* enumerates `_supertool.py` on the next line, so there is
    # nothing left to disclose and the gate's basename test declines it.
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
    """Glob matching files, supports ** recursive. Returns up to MAX_GLOB_RESULTS.

    Gitignored files are dropped on both halves (#2738), from the one git
    listing per walk root; they land in `git_tally.hidden` when a tally is
    passed, and so does a listing that could not be taken.

    `over_fetch` raises the internal cap by that many files without changing
    the cap callers are told about. op_glob passes 1 so it can tell a list that
    happens to be cap-length from one that was cut short (#448).

    When exclude_paths is provided and the pattern contains '**', uses an
    os.walk-based implementation that prunes excluded directories at the walk
    boundary (never opens them).  For non-recursive patterns, falls back to
    glob.glob and filters results post-hoc (no subtree to prune anyway).

    Both halves filter *files* against exclude_paths (#691). Only the glob.glob
    half ever did, so one op gave two answers: `glob:.env*` hid `.env` and
    `glob:**/.env*` listed it. Excluded files land in `hidden` when a list is
    passed, so op_glob can say how many it dropped.
    """
    max_results = _get_op_int("glob", "max_results", MAX_GLOB_RESULTS) + over_fetch

    # Brace expansion: `*.{json,xml}` → fan out + dedupe. Shell/fd semantics.
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
        # Walk-based implementation for recursive globs with exclusions.
        # Only safe when pattern has a single `**` — multi-`**` patterns
        # (`**/X/**/Y`) need full glob semantics on every segment, which
        # fnmatch can't express. Fall through to glob.glob + post-filter.
        # Split on the first '**' to get the root dir and the tail pattern.
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
                # Match the tail pattern against the relative path from root_part
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
    # When exclude_paths is empty, the caller explicitly opted out of
    # exclusions (no_exclude=True) — include dotfiles too so they actually
    # see ".git/" / "node_modules/" etc. Python 3.11+ supports the kwarg;
    # older versions silently skip dotfiles regardless.
    glob_kwargs: Dict[str, Any] = {"recursive": True}
    if not exclude_paths and sys.version_info >= (3, 11):
        glob_kwargs["include_hidden"] = True
    matches = sorted(glob(pattern, **glob_kwargs))
    files_out = [m for m in matches if os.path.isfile(m)]
    if exclude_paths:
        cwd = os.getcwd()
        # No walk boundary to prune at on this path, so gitignored hits are
        # filtered out of the result instead (#449).
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
    """Directory a glob pattern starts from, for the gitignore lookup (#449).

    The literal head of the pattern — everything before the first wildcard —
    is what decides whether the caller deliberately entered an ignored tree.
    `glob:.claude/worktrees/foo/*.php` must keep working; `glob:**/*.php` must
    not drag six worktrees in.
    """
    head = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
    directory = head if head.endswith(("/", os.sep)) else os.path.dirname(head)
    return directory if directory and os.path.isdir(directory) else "."
