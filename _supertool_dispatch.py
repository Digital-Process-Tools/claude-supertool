


























from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_dispatch.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )

import threading as _threading
_DISPATCH_STATE = _threading.local()

























_ACC_FLUSH_LOCK = _threading.Lock()


def _acc_not_checked() -> List[str]:






    buf = getattr(_DISPATCH_STATE, "acc_not_checked", None)
    return _NOT_CHECKED if buf is None else buf


def _acc_validated() -> List[Tuple[str, bool, bool]]:

    buf = getattr(_DISPATCH_STATE, "acc_validated", None)
    return _VALIDATED_FILES if buf is None else buf


def _acc_push() -> Tuple[Optional[List[str]],
                         Optional[List[Tuple[str, bool, bool]]]]:

    prev = (getattr(_DISPATCH_STATE, "acc_not_checked", None),
            getattr(_DISPATCH_STATE, "acc_validated", None))
    _DISPATCH_STATE.acc_not_checked = []
    _DISPATCH_STATE.acc_validated = []
    return prev


def _acc_pop(prev: Tuple[Optional[List[str]],
                         Optional[List[Tuple[str, bool, bool]]]]) -> None:





    mine_not_checked = getattr(_DISPATCH_STATE, "acc_not_checked", None) or []
    mine_validated = getattr(_DISPATCH_STATE, "acc_validated", None) or []
    prev_not_checked, prev_validated = prev
    _DISPATCH_STATE.acc_not_checked = prev_not_checked
    _DISPATCH_STATE.acc_validated = prev_validated









    with _ACC_FLUSH_LOCK:
        (_NOT_CHECKED if prev_not_checked is None
         else prev_not_checked).extend(mine_not_checked)
        (_VALIDATED_FILES if prev_validated is None
         else prev_validated).extend(mine_validated)
























_CNT_FIELDS = (
    "cnt_mutation", "cnt_write", "cnt_skip", "cnt_reapply", "cnt_rollback",
    "cnt_left_on_disk",
)


def _cnt_push() -> Tuple[int, ...]:

    prev = tuple(getattr(_DISPATCH_STATE, f, 0) for f in _CNT_FIELDS)
    for f in _CNT_FIELDS:
        setattr(_DISPATCH_STATE, f, 0)
    return prev


def _cnt_pop(prev: Tuple[int, ...]) -> None:







    for f, p in zip(_CNT_FIELDS, prev):
        setattr(_DISPATCH_STATE, f, p + getattr(_DISPATCH_STATE, f, 0))


def _cnt_frame(field: str) -> int:

    return getattr(_DISPATCH_STATE, field, 0)


def _bump_counter(counter: List[int], field: str, by: int = 1) -> None:
















    if by < 0:
        if counter[0] > 0:
            counter[0] += by
        if getattr(_DISPATCH_STATE, field, 0) > 0:
            setattr(_DISPATCH_STATE, field, getattr(_DISPATCH_STATE, field, 0) + by)
        return
    counter[0] += by
    setattr(_DISPATCH_STATE, field, getattr(_DISPATCH_STATE, field, 0) + by)





_DISPATCH_MAX_DEPTH = _env_int(os.environ.get("SUPERTOOL_DISPATCH_MAX_DEPTH"), "SUPERTOOL_DISPATCH_MAX_DEPTH", 32, minimum=1)


def dispatch(arg: str, pre_parsed: "Optional[Tuple[List[str], bool]]" = None) -> str:




























    depth = getattr(_DISPATCH_STATE, "depth", 0)


    if depth == 0:
        _DISPATCH_STATE.call_failed = False
    if depth >= _DISPATCH_MAX_DEPTH:
        _mark_op_failure()
        return (
            f"ERROR: dispatch recursion limit ({_DISPATCH_MAX_DEPTH}) exceeded "
            f"— check for a self-referencing batch payload\n"
        )
    _DISPATCH_STATE.depth = depth + 1
    _acc_prev = _acc_push()
    _cnt_prev = _cnt_push()
    try:
        out = _dispatch_impl(arg, pre_parsed)





        return _display_safe(out) if depth == 0 else out
    finally:
        _DISPATCH_STATE.depth = depth
        _acc_pop(_acc_prev)
        _cnt_pop(_cnt_prev)





        if not _is_parallel_safe(arg):
            _path_meta_bulk_drop()




        if depth == 0:
            _FORMATTER_SKIPS.clear()


def dispatch_verdict(
    arg: str, pre_parsed: "Optional[Tuple[List[str], bool]]" = None
) -> "Tuple[str, bool]":























    _DISPATCH_STATE.call_failed = False





    out = dispatch(arg) if pre_parsed is None else dispatch(arg, pre_parsed)
    return out, _call_failed()


def _depth1_call_footer(op: str, body: str) -> str:

















    if getattr(_DISPATCH_STATE, "depth", 1) > 1:
        return body





    _not_checked_slice = list(
        getattr(_DISPATCH_STATE, "acc_not_checked", None) or ())
    _validated_slice = list(
        getattr(_DISPATCH_STATE, "acc_validated", None) or ())
    _mutation_delta = _cnt_frame("cnt_mutation")
    if not (op in _OP_TARGETS or _mutation_delta
            or _not_checked_slice or _validated_slice):
        return body
    _result = _result_line(_mutation_delta,
                           _cnt_frame("cnt_write"),
                           _cnt_frame("cnt_skip"),
                           _cnt_frame("cnt_reapply"),
                           _not_checked_slice,
                           _cnt_frame("cnt_rollback"),
                           _validated_slice,
                           _cnt_frame("cnt_left_on_disk"))







    if op == "batch":
        body = _result + body
    body += _result
    body += _branch_line()
    return body


def _dispatch_impl(arg: str, pre_parsed: "Optional[Tuple[List[str], bool]]" = None) -> str:


    no_exclude = arg.endswith(_NO_EXCLUDE_SUFFIX)
    if no_exclude:
        arg = arg[: -len(_NO_EXCLUDE_SUFFIX)]









    header = (f"--- {_flat_field(arg, disclose_newline=True)}"
              f"{_NO_EXCLUDE_SUFFIX if no_exclude else ''} ---\n")





    _at_file_replace_all: bool = False
    _at_file_used: bool = False





    _arg_sep: str = ""
    if pre_parsed is not None:




        parts, _at_file_replace_all = pre_parsed
        _at_file_used = True
        op = parts[0] if parts else ""
    else:




        import re as _re
        triple_match = _re.match(r"^([a-zA-Z_][a-zA-Z0-9_-]*):::", arg)
        if triple_match:
            parts = arg.split(":::")
            _arg_sep = ":::"
        else:
            parts = _split_arg(arg)
            _arg_sep = ":"
        op = parts[0] if parts else ""





















    if _read_only_declared() and _op_is_recognized(op):
        _read_only_cls = _op_safety_class(op)
        if _read_only_cls != "read-only":
            _bump_counter(_SKIP_COUNT, "cnt_skip")
            return _receipt(header, _read_only_decline(op, _read_only_cls))

    def _op_gated_by_mixed_tree_write_check() -> bool:







        if _OP_SAFETY_BUILTIN.get(op) == "writes":
            return True
        _ops_cfg = _load_config().get("ops")
        return isinstance(_ops_cfg, dict) and op in _ops_cfg








    if (
        pre_parsed is None
        and len(parts) >= 2
        and parts[1].startswith("@")
        and op in _READ_OP_AT_FIELDS
        and (
            parts[1] == "@-"
            or os.path.isfile(_resolve_at_path(parts[1][1:]))



            or os.path.isfile(parts[1][1:])





            or (
                len(parts) == 2
                and parts[1][1:].lower().endswith((".toml", ".json"))
            )
        )
    ):
        if len(parts) > 2:
            return _receipt(header, (
                f"ERROR: {op}:@... takes the @reference as the only argument "
                f"(e.g. {op}:@payload.toml or {op}:@-). Put fields in the "
                f"payload, not on the colon CLI.\n"
            ))
        try:
            _read_payload = _load_at_file(parts[1], note=False)
        except ValueError as _e:
            _mark_op_failure()
            return header + _take_payload_warnings() + f"ERROR: {_e}\n"


        _read_warnings = _take_payload_warnings()







        _read_body = _read_op_from_payload(
            op, _read_payload, no_exclude=no_exclude)
        if _op_body_failed(_read_body):
            _mark_op_failure()
        _read_body = _depth1_call_footer(op, _read_body)
        return header + _read_warnings + _read_body









    _at_file_named_fields = _at_file_fields(op)










































    _generic_preset_route = (
        not _at_file_named_fields
        and _op_is_preset_op(op)
        and not _repo_target_modes().get(op, "").startswith("payload")
        and len(parts) >= 2
        and (
            parts[1] == "@-"
            or os.path.isfile(_resolve_at_path(parts[1][1:]))
            or os.path.isfile(parts[1][1:])
            or (
                len(parts) == 2
                and parts[1][1:].lower().endswith((".toml", ".json"))
            )
        )
    )
    if (
        pre_parsed is None
        and len(parts) >= 2
        and parts[1].startswith("@")
        and (_at_file_named_fields or _generic_preset_route)
    ):
        if len(parts) > 2:
            return _receipt(header, (
                f"ERROR: {op}:@... takes the @reference as the only argument "
                f"(e.g. {op}:@payload.json or {op}:@-). Put fields in the "
                f"JSON/TOML payload, not on the colon CLI."
                + _at_file_payload_hint(op) + chr(10)
            ))

















        if _op_gated_by_mixed_tree_write_check():
            _mixed_early = _mixed_tree_pair()
            if _mixed_early is not None and not _mixed_tree_allowed():
                _bump_counter(_SKIP_COUNT, "cnt_skip")
                return _receipt(header, _mixed_tree_decline(op, _mixed_early))
        try:
            payload = _load_at_file(parts[1])
            if _at_file_named_fields:
                parts, _at_file_replace_all = _at_file_to_parts(op, payload)
            else:
                parts, _at_file_replace_all = _at_file_to_parts_generic(
                    op, payload)
            _at_file_used = True


            _arg_sep = ""
        except ValueError as _e:







            return (header + _take_payload_warnings() + f"ERROR: {_e}"
                    + _at_file_payload_hint(op) + chr(10))




    _dec = (lambda s: s) if _at_file_used else _decode_escapes




    if not _at_file_used:
        _stdin_ref = _stdin_ref_in_value_field(op, parts)
        if _stdin_ref:
            return _receipt(header, _stdin_ref)




    _ARG_SEP[0] = _arg_sep





















    _compact_header = ""
    if pre_parsed is None and len(arg) > _HEADER_ARG_MAX:
        _compact_header = _compact_header_arg(op, parts, _arg_sep)



    _custom_op_ok: Optional[bool] = None










    if pre_parsed is None:
        _extra_toks = _extra_colon_tokens(op, parts)
        if _extra_toks:
            return _receipt(header, _extra_colon_tokens_refusal(
                op, _extra_toks, _extra_token_remedy(op, parts, _extra_toks)))




    _path_slots = [_pos for _pos in _PATH_ARG_POSITIONS.get(op, ())
                   if _pos < len(parts)]
    _containment, _gated = _gate_paths(parts[_pos] for _pos in _path_slots)
    if _containment:
        return _receipt(header, _containment)



    if any(_gated[_i] != parts[_pos] for _i, _pos in enumerate(_path_slots)):
        parts = list(parts)
        for _i, _pos in enumerate(_path_slots):
            parts[_pos] = _gated[_i]























    if _op_gated_by_mixed_tree_write_check():
        _mixed = _mixed_tree_pair()
        if _mixed is not None and not _mixed_tree_allowed():
            _bump_counter(_SKIP_COUNT, "cnt_skip")
            return _receipt(header, _mixed_tree_decline(op, _mixed))

    try:
        if op == "read":
            path = parts[1] if len(parts) > 1 else ""
            offset = 0
            limit = 0
            force_full = False
            range_form = False
            if len(parts) > 2 and parts[2]:
                if parts[2] in ("full", "raw"):
                    force_full = True
                elif _READ_RANGE_RE.fullmatch(parts[2]):
                    r_start, r_end = (int(x) for x in parts[2].split("-"))
                    if r_start < 1:
                        return _receipt(
                            header, "ERROR: read range START must be >= 1\n")
                    if r_end < r_start:
                        return _receipt(header, (
                            f"ERROR: read range END ({r_end}) is before "
                            f"START ({r_start})\n"
                        ))
                    offset = r_start - 1
                    limit = r_end - r_start + 1
                    range_form = True
                else:
                    offset = int(parts[2])
            if len(parts) > 3 and parts[3]:
                if parts[3] in ("full", "raw"):
                    force_full = True
                elif parts[3].startswith("grep="):
                    pass  
                elif range_form:
                    return _receipt(header, (
                        f"ERROR: read:PATH:START-END takes no LIMIT "
                        f"(got {parts[3]!r}) — the range already bounds it\n"
                    ))
                else:
                    limit = int(parts[3])




            grep_filter = ""
            for _tok in parts[3:]:
                if _tok.startswith("grep="):
                    grep_filter = _tok[5:]
                    break
            body = op_read(path, offset, limit, grep_filter, force_full,
                           range_form)
        elif op == "grep":



            parts, _kw_path = _extract_path_kw(parts)






            _grep_extra = _grep_peeled_extras(parts)
            if _grep_extra:
                return _receipt(header, _extra_colon_tokens_refusal(
                    "grep", _grep_extra,
                    "Slot order is grep:PATTERN:PATH:LIMIT:CONTEXT — nothing "
                    "follows CONTEXT."))
            pattern, path, limit, context, count_only, no_auto_read, full = \
                _parse_grep_args(parts)
            if _kw_path is not None:
                path = _kw_path



            _contained, (path,) = _gate_paths([path])
            if _contained:
                return _receipt(header, _contained)
            if limit == 0:
                return _receipt(header, _grep_zero_limit())
            if limit == GREP_LIMIT_ALL_MISPLACED:
                return _receipt(header, _GREP_ALL_OUTSIDE_LIMIT_SLOT)




            _widened = _absorbed_path_hint("grep", pattern, path,
                                           keys=("pattern",))
            if _widened:
                return _receipt(header, _widened)
            _hint = _colon_split_hint("grep", pattern, path)
            if _hint:
                return _receipt(header, _hint)
            body = op_grep(pattern, path, limit, context, count_only,
                           no_exclude=no_exclude, no_auto_read=no_auto_read,
                           full=full)
        elif op == "grep_around":





            parts, _kw_path = _extract_path_kw(parts)
            if _kw_path is not None:











                _contained, (_kw_path,) = _gate_paths([_kw_path])
                if _contained:
                    return _receipt(header, _contained)






                parts = parts[:2] + [_kw_path] + parts[2:]
            ga_pattern = parts[1] if len(parts) > 1 else ""
            ga_path = parts[2] if len(parts) > 2 and parts[2] else "."
            if len(parts) > 3 and parts[3] == _GREP_ALL_TOKEN:
                return _receipt(header, _GREP_AROUND_ALL_IN_N_SLOT)





            _ga_bad = _grep_around_numeric_refusal(parts, ga_pattern, ga_path)
            if _ga_bad:
                return _receipt(header, _ga_bad)
            ga_context = int(parts[3]) if len(parts) > 3 and parts[3] else 3
            ga_limit_tok = parts[4] if len(parts) > 4 and parts[4] else ""
            if ga_limit_tok == _GREP_ALL_TOKEN:
                ga_limit = GREP_LIMIT_ALL
            else:
                ga_limit = int(ga_limit_tok) if ga_limit_tok else 10
            if ga_limit == 0:
                return _receipt(header, _grep_zero_limit())
            body = op_grep(ga_pattern, ga_path, ga_limit, ga_context,
                           count_only=False, no_exclude=no_exclude)
        elif op == "wc":
            path = parts[1] if len(parts) > 1 else ""
            body = op_wc(path)
        elif op == "glob":
            pattern = parts[1] if len(parts) > 1 else ""
            no_auto_read = len(parts) > 2 and parts[2] == "no-auto-read"




            if len(parts) > 2 and parts[2] and not no_auto_read:
                return _receipt(header, (
                    f"ERROR: glob: {parts[2]!r} is not a value that slot "
                    f"takes — glob:PATTERN[:no-auto-read], and "
                    f"`no-auto-read` is the only one." + chr(10)
                    + "  Compared against that one literal and discarded when "
                      "it did not match, until #1582: a token that changed "
                      "nothing, on a call that answered anyway." + chr(10)))
            body = op_glob(pattern, no_exclude=no_exclude, no_auto_read=no_auto_read)
        elif op == "ls":
            path = parts[1] if len(parts) > 1 and parts[1] else "."
            body = op_ls(path)
        elif op == "tail":
            path = parts[1] if len(parts) > 1 else ""
            n = int(parts[2]) if len(parts) > 2 and parts[2] else 20
            body = op_tail(path, n)
        elif op == "head":
            path = parts[1] if len(parts) > 1 else ""
            n = int(parts[2]) if len(parts) > 2 and parts[2] else 20
            body = op_head(path, n)
        elif op == "check":
            preset = parts[1] if len(parts) > 1 else ""
            path = parts[2] if len(parts) > 2 and parts[2] else ""
            body = op_check(preset, path)
        elif op == "gc":
            mode = parts[1] if len(parts) > 1 else ""
            kind = parts[2] if len(parts) > 2 else ""
            body = op_gc(mode, kind)
        elif op == "around":


            parts, _kw_path = _extract_path_kw(parts)
            pattern, path, n = _parse_around_args(parts)
            if _kw_path is not None:
                path = _kw_path




            _contained, (path,) = _gate_paths([path])
            if _contained:
                return _receipt(header, _contained)
            _delegated = _around_line_delegation(pattern, path, n)
            if _delegated:
                return _receipt(header, _delegated)
            _hint = _colon_split_hint("around", pattern, path)
            if _hint:
                return _receipt(header, _hint)
            body = op_around(pattern, path, n)
        elif op == "map":
            path = parts[1] if len(parts) > 1 else "."
            body = op_map(path, no_exclude=no_exclude)
        elif op == "diff":
            path1 = parts[1] if len(parts) > 1 else ""
            path2 = parts[2] if len(parts) > 2 else ""
            body = op_diff(path1, path2)
        elif op == "stat":
            path = parts[1] if len(parts) > 1 else ""
            body = op_stat(path)
        elif op == "around_line":
            path = parts[1] if len(parts) > 1 else ""
            line = int(parts[2]) if len(parts) > 2 and parts[2] else 0
            n = int(parts[3]) if len(parts) > 3 and parts[3] else 10
            body = op_around_line(path, line, n)
        elif op == "between":
            if len(parts) >= 2 and parts[1] == "re":







                if len(parts) >= 5:
                    start_pat = parts[2]
                    end_pat = parts[3]
                    path = ":".join(parts[4:])
                    _contained, (path,) = _gate_paths([path])
                    if _contained:
                        return _receipt(header, _contained)




                    _hint = _colon_split_hint(
                        "between", f"{start_pat}:{end_pat}", path,
                        keys=("start", "end"),



                        call_prefix=f"between:re:{start_pat}:{end_pat}",






                        swap_fallback=False,
                    )
                    if _hint:
                        return _receipt(header, _hint)
                    body = op_between_pattern(start_pat, end_pat, path)
                else:
                    body = ("ERROR: between:re: requires START:END:PATH "
                            f"(got {len(parts) - 2} args after 're')\n")
            elif len(parts) >= 3:






                symbol = ":".join(parts[1:-1])
                path = parts[-1]



                _contained, (path,) = _gate_paths([path])
                if _contained:
                    return _receipt(header, _contained)
                _range_hint = _between_numeric_hint(parts)
                if _range_hint:
                    return _receipt(header, _range_hint)
                _hint = _colon_split_hint("between", symbol, path,
                                          keys=("symbol",))
                if _hint:
                    return _receipt(header, _hint)
                body = op_between_symbol(symbol, path)
            else:
                body = ("ERROR: between requires SYMBOL:PATH or "
                        "re:START:END:PATH\n")
        elif op == "tree":
            path = parts[1] if len(parts) > 1 and parts[1] else "."
            d = int(parts[2]) if len(parts) > 2 and parts[2] else 3
            body = op_tree(path, d, exclude_paths=_get_exclude_paths("tree", no_exclude))
        elif op in ("replace", "replace_dry"):
            old_str = _dec(parts[1] if len(parts) > 1 else "")
            new_str = _dec(parts[2] if len(parts) > 2 else "")
            rpath = parts[3] if len(parts) > 3 and parts[3] else "."
            dry = op == "replace_dry"
            body = _run_with_validators(op, parts, lambda: op_replace(old_str, new_str, rpath, dry=dry))
        elif op == "edit":
            old_str = _dec(parts[1] if len(parts) > 1 else "")
            new_str = _dec(parts[2] if len(parts) > 2 else "")
            epath = parts[3] if len(parts) > 3 else ""
            if _at_file_replace_all:
                body = _run_with_validators(op, parts, lambda: op_replace(old_str, new_str, epath or "."))
            else:
                body = _run_with_validators(op, parts, lambda: op_edit(old_str, new_str, epath))
        elif op == "replace_lines":
            rl_path = parts[1] if len(parts) > 1 else ""
            try:
                rl_start = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                rl_end = int(parts[3]) if len(parts) > 3 and parts[3] else 0
            except ValueError:
                body = "ERROR: replace_lines START/END must be integers\n"
            else:

                rl_content = _dec(":".join(parts[4:]) if len(parts) > 4 else "")
                body = _run_with_validators(op, parts, lambda: op_replace_lines(rl_path, rl_start, rl_end, rl_content))
        elif op == "paste":
            p_path = parts[1] if len(parts) > 1 else ""

            p_content = _dec(":".join(parts[2:]) if len(parts) > 2 else "")
            body = _run_with_validators(op, parts, lambda: op_paste(p_path, p_content))
        elif op == "append":
            a_path = parts[1] if len(parts) > 1 else ""

            a_content = _dec(":".join(parts[2:]) if len(parts) > 2 else "")
            body = _run_with_validators(op, parts, lambda: op_append(a_path, a_content))
        elif op == "vim":
            vim_path = parts[1] if len(parts) > 1 else ""
            vim_script = ":".join(parts[2:]) if len(parts) > 2 else ""
            body = _run_with_validators(op, parts, lambda: op_vim(vim_path, vim_script))
        elif op == "json-set":







            js_ref = parts[1] if len(parts) > 1 else ""
            if not js_ref.startswith("@"):
                body = (
                    f"ERROR: json-set takes an @file/@- payload, not colon "
                    f"arguments (got {js_ref!r}) -- write json-set:@- with "
                    f"'path' and 'set' fields in the payload.\n"
                )
            else:
                try:
                    js_payload = _load_at_file(js_ref)
                except ValueError as _je:
                    body = f"ERROR: {_je}\n"
                else:
                    if not isinstance(js_payload, dict):
                        body = (
                            f"ERROR: @file payload for op 'json-set' must be "
                            f"a JSON/TOML object with 'path' and 'set' "
                            f"fields, got {type(js_payload).__name__}\n"
                        )
                    else:
                        js_lower = {str(k).lower(): v for k, v in js_payload.items()}
                        js_unknown = sorted(set(js_lower) - {"path", "set"})
                        if js_unknown:
                            body = (
                                f"ERROR: @file payload for op 'json-set' has "
                                f"unknown field(s) {_flat_keys(js_unknown)} "
                                f"-- accepted: path, set\n"
                            )
                        elif "path" not in js_lower:
                            body = (
                                "ERROR: @file payload for op 'json-set' "
                                "missing required field 'path'\n"
                            )
                        elif "set" not in js_lower:
                            body = (
                                "ERROR: @file payload for op 'json-set' "
                                "missing required field 'set'\n"
                            )
                        elif not isinstance(js_lower["set"], dict):
                            body = (
                                f"ERROR: @file payload field 'set' for op "
                                f"'json-set' must be a table mapping dotted "
                                f"field paths to values, got "
                                f"{type(js_lower['set']).__name__}\n"
                            )
                        else:
                            js_path = str(js_lower["path"])
                            js_fields = js_lower["set"]
                            js_parts = ["json-set", js_path]
                            body = _run_with_validators(
                                "json-set", js_parts,
                                lambda: op_json_set(js_path, js_fields))
        elif op == "batch":




            ref = parts[1] if len(parts) > 1 else ""
            if not ref.startswith("@"):




                body = (
                    "ERROR: batch takes an @file reference, not a bare path"
                    + (f" (got {ref!r})" if ref else "")
                    + " — write `batch:@ops.toml` in the colon form, or "
                    + 'path = "@ops.toml" inside a payload. The leading @ is '
                    + "what marks the value as a file to read the ops from, "
                    + "and it is required in both forms.\n"
                )
            else:
                try:
                    raw_payload = _load_at_file(ref)
                except ValueError as _be:
                    body = f"ERROR: {_be}\n"
                else:

                    if isinstance(raw_payload, list):
                        batch_ops = raw_payload
                        continue_on_error = True
                    elif isinstance(raw_payload, dict):
                        if "ops" not in raw_payload and [
                            k for k in raw_payload if k != "continue_on_error"
                        ]:
                            if isinstance(raw_payload.get("op"), str) and raw_payload["op"]:








                                batch_ops = [raw_payload]
                                continue_on_error = True
                            else:




                                batch_ops = None  
                                body = (
                                    "ERROR: this payload has no 'ops' array — it looks "
                                    "like a single op's fields. Use 'OP:@file' (e.g. "
                                    "'edit:@file') for a single op, or wrap it as "
                                    '{"ops": [...]} for batch.\n'
                                )
                        else:
                            _wrapper_unknown = sorted(
                                k for k in raw_payload
                                if str(k).lower() not in _BATCH_WRAPPER_KEYS)
                            if _wrapper_unknown:
                                batch_ops = None  
                                body = (
                                    "ERROR: unknown key(s) "
                                    + _flat_keys(_wrapper_unknown)
                                    + " at the top level of this batch payload "
                                    + "— accepted: "
                                    + ", ".join(sorted(_BATCH_WRAPPER_KEYS))
                                    + ". Refused rather than dropped (#1551): "
                                    + "a misspelt `continue_on_error` ran the "
                                    + "rest of the batch past a failed op "
                                    + "while the payload said to stop, and "
                                    + "nothing in the receipt disagreed."
                                    + chr(10)
                                )
                            else:
                                batch_ops = raw_payload.get("ops", [])
                                continue_on_error = bool(raw_payload.get("continue_on_error", True))
                    else:
                        batch_ops = []
                        continue_on_error = True
                        body = (
                            "ERROR: batch @file must be a JSON array or object "
                            f"with 'ops' key, got {type(raw_payload).__name__}\n"
                        )
                        batch_ops = None  

                    if batch_ops is not None:
                        if not isinstance(batch_ops, list):
                            body = "ERROR: batch 'ops' must be a JSON array\n"
                        else:
                            _cap = _get_op_int("batch", "max_ops", MAX_BATCH_OPS)
                            _cap_exceeded = len(batch_ops) > _cap
                            if _cap_exceeded:
                                body = (
                                    f"ERROR: batch size {len(batch_ops)} exceeds "
                                    f"max_ops cap ({_cap}). Override via "
                                    f"`ops.batch.max_ops` in .supertool.json or split "
                                    f"into smaller batches.\n"
                                )
                                batch_ops = []
                                _snap_err = ""
                            else:




                                batch_ops, _snap_err = _reorder_batch_for_snapshot(batch_ops)
                                if _snap_err:
                                    body = f"ERROR: {_snap_err}\n"
                                    batch_ops = []
                            results: List[str] = []






                            global _DEFER_FORMATTERS, _FORMAT_QUEUE
                            global _VALIDATOR_DEFER_QUEUE, _VALIDATOR_DEFER_SEEN
                            _batch_owns_defer = not _DEFER_FORMATTERS
                            if _batch_owns_defer:
                                _DEFER_FORMATTERS = True
                                _FORMAT_QUEUE = {}
                                _VALIDATOR_DEFER_QUEUE = []
                                _VALIDATOR_DEFER_SEEN = set()
                            try:
                                for _item in batch_ops:
                                    if not isinstance(_item, dict):
                                        err = f"ERROR: each batch op must be a JSON object, got {type(_item).__name__}\n"
                                        results.append(err)
                                        _mark_op_failure()
                                        if not continue_on_error:
                                            break
                                        continue
                                    _sub_op = _item.get("op", "")
                                    if not _sub_op:
                                        err = "ERROR: batch op missing 'op' field\n"
                                        results.append(err)
                                        _mark_op_failure()
                                        if not continue_on_error:
                                            break
                                        continue



                                    _sub_pre_parsed = None
                                    if _sub_op in _READ_OP_AT_FIELDS:






                                        _read_payload_fields = {
                                            str(_k): _v for _k, _v in _item.items()
                                            if str(_k).lower() != "op"
                                        }
                                        _read_target = str(
                                            _read_payload_fields.get("path", "") or ""
                                        )




                                        _read_body = _read_op_from_payload(
                                            _sub_op, _read_payload_fields
                                        )
                                        if _op_body_failed(_read_body):
                                            _mark_op_failure()
                                        _sub_result = (
                                            "--- "
                                            + _payload_header_arg(_sub_op, _read_target)
                                            + " ---\n"
                                            + _read_body
                                        )
                                        results.append(_sub_result)
                                        if not continue_on_error and _sub_result.split("\n")[1:2] and (
                                            _sub_result.split("\n")[1].startswith("ERROR")
                                        ):
                                            break
                                        continue
                                    if _at_file_fields(_sub_op):
                                        try:
                                            _sub_parts, _sub_replace_all = _at_file_to_parts(_sub_op, _item)
                                        except ValueError as _ve:
                                            err = f"ERROR: {_ve}\n"
                                            results.append(err)
                                            _mark_op_failure()
                                            if not continue_on_error:
                                                break
                                            continue

                                        if _sub_replace_all and _sub_op == "edit":
                                            _sub_parts[0] = "replace"





                                        _sub_pre_parsed = (_sub_parts, _sub_replace_all)





                                        _sub_field_names = _at_file_fields(_sub_op)
                                        _sub_target = ""
                                        if "path" in _sub_field_names:
                                            _sub_path_idx = _sub_field_names.index("path") + 1
                                            if len(_sub_parts) > _sub_path_idx:
                                                _sub_target = _sub_parts[_sub_path_idx]
                                        _sub_arg = _payload_header_arg(
                                            _sub_parts[0], _sub_target
                                        )
                                    else:





                                        _fields, _order_err = _ordered_batch_fields(_sub_op, _item)
                                        if _order_err:
                                            results.append(_order_err)
                                            _mark_op_failure()
                                            if not continue_on_error:
                                                break
                                            continue
                                        _sub_arg = ":".join([_sub_op] + _fields) if _fields else _sub_op
                                    _sub_result = dispatch(_sub_arg, pre_parsed=_sub_pre_parsed)
                                    results.append(_sub_result)













                                    if not continue_on_error and _sub_result.split("\n")[1:2] and (
                                        _sub_result.split("\n")[1].startswith("ERROR")
                                    ):
                                        break
                            finally:
                                if _batch_owns_defer:
                                    _DEFER_FORMATTERS = False


                            if not _snap_err and not _cap_exceeded:
                                body = "".join(results)
                                if _batch_owns_defer:
                                    body += _drain_format_queue()
                                    body += _drain_validator_queue()
        elif op == "payload-lint":
            body = op_payload_lint(parts[1] if len(parts) > 1 else "")
        elif op == "validate":



            v_verbose = "verbose" in parts[1:]
            v_parts = [p for p in parts[1:] if p != "verbose"]
            v_path = v_parts[0] if len(v_parts) > 0 else ""
            v_tools = [t for t in (v_parts[1].split(",") if len(v_parts) > 1 and v_parts[1] else []) if t]
            v_files = [f for f in v_path.split(",") if f]
            if len(v_files) > 1:



                _v_contained, v_files = _gate_paths(v_files)
                if _v_contained:
                    return _receipt(header, _v_contained)
                body = op_validate_multi(v_files, v_tools or None, verbose=v_verbose)
            else:
                body = op_validate(v_path, v_tools or None, verbose=v_verbose)
        elif op == "format":


            f_verbose = "verbose" in parts[1:]
            f_parts = [p for p in parts[1:] if p != "verbose"]
            f_path = f_parts[0] if len(f_parts) > 0 else ""
            f_tools = [t for t in (f_parts[1].split(",") if len(f_parts) > 1 and f_parts[1] else []) if t]
            body = op_format(f_path, f_tools or None, verbose=f_verbose)
        elif op == "validate_staged":


            vs_verbose = "verbose" in parts[1:]
            vs_parts = [p for p in parts[1:] if p != "verbose"]
            vs_tools = [t for t in (vs_parts[0].split(",") if len(vs_parts) > 0 and vs_parts[0] else []) if t]
            body = op_validate_staged(vs_tools or None, verbose=vs_verbose)
        elif op == "format_staged":


            fs_verbose = "verbose" in parts[1:]
            fs_parts = [p for p in parts[1:] if p != "verbose"]
            fs_tools = [t for t in (fs_parts[0].split(",") if len(fs_parts) > 0 and fs_parts[0] else []) if t]
            body = op_format_staged(fs_tools or None, verbose=fs_verbose)
        elif op == "resolve":
            rs_symbol = parts[1] if len(parts) > 1 else ""
            rs_from_file = parts[2] if len(parts) > 2 else None
            body = op_resolve(rs_symbol, rs_from_file)
        elif op == "diag":
            body = op_diag(parts[1] if len(parts) > 1 else "")
        elif op == "hover":
            body = op_hover(parts[1] if len(parts) > 1 else "",
                            parts[2] if len(parts) > 2 else "")
        elif op == "rename":
            body = op_rename(parts[1] if len(parts) > 1 else "",
                             parts[2] if len(parts) > 2 else "",
                             parts[3] if len(parts) > 3 else "")
        elif op == "workspace":
            ws_path = parts[1] if len(parts) > 1 else ""
            body = op_workspace(ws_path)
        elif op == "help":
            body = op_help(parts[1] if len(parts) > 1 else "")
        elif op == "guard":




            header = ""










            body = op_guard(":".join(parts[1:]))  
        elif op == "doctor":


            header = ""
            body = op_doctor(parts[1] if len(parts) > 1 else "")
        elif op == "init":


            header = ""
            body = op_init(parts[1] if len(parts) > 1 else "")
        elif op == "registry":


            header = ""
            body = op_registry(parts[1] if len(parts) > 1 else "")
        elif op in ("introduction", "output-format", "ops", "ops-compact", "version"):

            header = ""
            if op == "introduction":
                body = op_introduction()
            elif op == "output-format":
                body = op_output_format()
            elif op == "version":
                body = op_version()
            else:








                ops_arg = parts[1] if len(parts) > 1 else ""
                if not ops_arg:
                    body = op_ops(compact=(op == "ops-compact"))
                elif ops_arg == "roster" and op == "ops":
                    body = op_ops_roster()
                elif ops_arg == "session" and op == "ops":
                    body = op_ops_session()
                elif ops_arg == "full" and op == "ops":



                    body = op_ops(full=True)
                elif ops_arg.startswith("grep=") and op == "ops":











                    grep_pattern = arg.partition("grep=")[2]
                    if not grep_pattern:
                        body = ("ERROR: `ops:grep=` needs a pattern after "
                                "`grep=` — `ops:grep=PATTERN`.\n")
                    else:
                        body = op_ops_filter(grep_pattern)
                else:
                    body = _ops_argument_refusal(ops_arg, op)
        else:

            custom = _resolve_custom_op(op, parts)
            if custom is not None:
                body = custom
                _custom_op_ok = _CUSTOM_OP_OK[0]
            else:
                alias = _resolve_alias(op, parts)
                if alias is not None:
                    body = alias
                else:
                    body = _unknown_op_message(op)
    except (ValueError, IndexError) as e:
        body = f"ERROR: argument parsing: {e}\n"













    if _custom_op_ok is not None:
        if not _custom_op_ok:
            _mark_op_failure()
    elif _op_body_failed(body):
        _mark_op_failure()


    try:
        _notify_read_op(op, parts)
    except Exception:
        pass  





    if _PAYLOAD_WARNINGS and getattr(_DISPATCH_STATE, "depth", 1) <= 1:
        body = _take_payload_warnings() + body









    if _PAYLOAD_PY_ESCAPE_ADVISORY and getattr(_DISPATCH_STATE, "depth", 1) <= 1:
        _PAYLOAD_PY_ESCAPE_ADVISORY.clear()

    if _WRITE_WARNINGS:
        body += "".join(w[1] for w in _WRITE_WARNINGS)
        _WRITE_WARNINGS.clear()

    if _FORMATTER_SKIPS and getattr(_DISPATCH_STATE, "depth", 1) <= 1:
        body += (
            "[formatters] skipped: " + ", ".join(_FORMATTER_SKIPS)
            + " — no config for it in the edited file's repo (#393)\n"
        )
        _FORMATTER_SKIPS.clear()










    if _compact_header and (_cnt_frame("cnt_write") > 0
                            or _custom_op_ok is True):
        header = (f"--- {_flat_field(_compact_header, disclose_newline=True)}"
                  f"{_NO_EXCLUDE_SUFFIX if no_exclude else ''} ---\n")



























    body = _depth1_call_footer(op, body)

    return header + body






def _read_target_around_line(parts: List[str]) -> Optional[Tuple[str, Optional[int], Optional[int]]]:

    if len(parts) < 3:
        return None
    path = parts[1]
    try:
        line = int(parts[2])
    except (TypeError, ValueError):
        return None
    n = 10
    if len(parts) > 3:
        try: n = int(parts[3])
        except (TypeError, ValueError): pass
    return (path, max(1, line - n), line + n)


def _read_target_read(parts: List[str]) -> Optional[Tuple[str, Optional[int], Optional[int]]]:

















    if len(parts) < 2:
        return None
    path = parts[1]
    if len(parts) == 3 and _READ_RANGE_RE.fullmatch(parts[2]):
        start, end = (int(x) for x in parts[2].split("-"))
        if start < 1 or end < start:
            return (path, None, None)
        return (path, start, end)
    if len(parts) >= 4:
        try:
            offset = int(parts[2]); limit = int(parts[3])
        except (TypeError, ValueError):
            return (path, None, None)
        if offset < 0 or limit <= 0:
            return (path, None, None)
        return (path, offset + 1, offset + limit)
    return (path, None, None)


def _read_target_file_only(parts: List[str]) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    if len(parts) < 2:
        return None
    return (parts[1], None, None)


def _read_target_between(parts: List[str]) -> Optional[Tuple[str, Optional[int], Optional[int]]]:


    if len(parts) < 3:
        return None
    if parts[1] == "re" and len(parts) >= 5:

        return (parts[4], None, None)
    symbol = _normalize_symbol_query(parts[1])
    path = parts[2]
    if not _has_tree_sitter():
        return (path, None, None)
    ext = os.path.splitext(path)[1].lower()  
    lang = _TS_LANG_MAP.get(ext)
    if not lang:
        return (path, None, None)
    found = _ts_find_node(path, lang, symbol)
    if found is None:
        return (path, None, None)
    node, _kind, _total = found
    start_line = node.start_point[0] + 1  
    end_line = node.end_point[0] + 1
    return (path, start_line, end_line)


_READ_OP_TARGETS: Dict[str, Any] = {
    "around_line": _read_target_around_line,
    "read":        _read_target_read,
    "between":     _read_target_between,
    "map":         _read_target_file_only,
    "tail":        _read_target_file_only,
    "head":        _read_target_file_only,
    "wc":          _read_target_file_only,
    "stat":        _read_target_file_only,
}


def _notify_read_op(op: str, parts: List[str]) -> None:

    extractor = _READ_OP_TARGETS.get(op)
    if not extractor:
        return
    target = extractor(parts)
    if target is None:
        return
    path, line_start, line_end = target
    if not path:
        return
    _run_notifiers(op, path, line=line_start, line_end=line_end)


def caller_tag() -> str:







    user = os.environ.get("USER", "?")
    ppid = os.getppid()
    entry = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "?")
    return f"user={user} ppid={ppid} entry={entry}"


def log_call(args: List[str], out_bytes: int) -> None:





    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            meta = f"ops={len(args)} out={out_bytes}b"
            f.write(f"{timestamp} | {caller_tag()} | {meta} | {' '.join(args)}\n")
    except OSError:
        pass  


