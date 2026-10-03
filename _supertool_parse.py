



































from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_parse.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )


_BUILTIN_OPS = {"read", "grep", "grep_around", "glob", "ls", "tail", "head", "wc", "check", "around", "map", "diff", "stat", "around_line", "tree", "replace", "replace_dry", "edit", "replace_lines", "paste", "append", "vi", "validate", "format", "validate_staged", "format_staged", "workspace", "resolve", "diag", "hover", "rename", "payload-lint"}




_DISPATCH_ONLY_OPS = {
    "between", "vim", "batch", "gc", "help", "version",
    "ops", "ops-compact", "introduction", "output-format", "registry",
    "guard", "doctor", "init", "json-set",
}



_MAIN_LEVEL_OPS = {"cwd", "repo"}


def _valid_op_names() -> List[str]:








    return sorted((_BUILTIN_OPS | _DISPATCH_ONLY_OPS | _MAIN_LEVEL_OPS) - {"vi"})







































_OP_SYNONYMS = {"write": "paste", "vi": "vim", "gh-since-tag": "gh-prs"}


















_CREATING_OPS = ("paste", "append")


def _create_instead_hint() -> str:










    return (
        f"to create it instead: {_CREATING_OPS[0]}:::PATH:::CONTENT writes a "
        f"whole file and its parent dirs, {_CREATING_OPS[1]} creates or "
        f"extends. If the path is a typo, either would write a second file and "
        f"leave the real one unedited — check `tried:` above first."
    )






_NEAR_MISS_MIN_LEN = 4



_NEAR_MISS_MAX = 3


def _within_one_edit(a: str, b: str) -> bool:







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






    by_lower = {name.lower(): name for name in loaded}
    if typed in by_lower:
        _add(by_lower[typed], loaded[by_lower[typed]])
        return hits[:_NEAR_MISS_MAX]






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















    return (
        f"       'cwd:' moves the directory the op ACTS on as well as the one "
        f"its config is read from, so it is\n"
        f"       the fix only when the work is in that project — never a way "
        f"to run '{op}' against THIS\n"
        f"       directory. For a repository no project covers, this op has no "
        f"route to it (#1554).\n"
    )


def _synonym_invocation_note(op: str, named: List[str]) -> str:





































    target = _OP_SYNONYMS.get(op.strip().lower())
    if not target or target not in named:
        return ""
    syntax = _registry_syntax(target)
    if not syntax:
        return ""
    return (f"       '{op}' was an invocation; '{target}' is only a name. "
            f"Syntax: {syntax}\n")


def _batch_op_head(arg: str) -> str:









    if arg.endswith(_NO_EXCLUDE_SUFFIX):
        arg = arg[: -len(_NO_EXCLUDE_SUFFIX)]
    if re.match(r"^([a-zA-Z_][a-zA-Z0-9_-]*):::", arg):
        parts = arg.split(":::")
    else:
        parts = _split_arg(arg)
    return parts[0] if parts else ""


def _op_is_unroutable(op: str) -> bool:














    if op in _BUILTIN_OPS - {"vi"} or op in _DISPATCH_ONLY_OPS or op in _MAIN_LEVEL_OPS:
        return False
    config = _load_config()






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




            for line in _unknown_op_message(op).splitlines():
                if line.startswith(("Did you mean:", "       ")):
                    msg += f"      {line.strip()}\n"



    for line in _unknown_op_message(bad[0][2]).splitlines():
        if line.startswith(("Valid operations:", "Plus ")):
            msg += line + "\n"
    return msg


def _unknown_op_message(op: str) -> str:
















    preset = _shipped_preset_ops().get(op)
    if preset is not None:





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












































_SAFETY_CLASSES = ("read-only", "writes", "acts")




_SAFETY_MARKERS = {"read-only": "", "writes": "*", "acts": "!"}




_OP_SAFETY_BUILTIN: Dict[str, str] = {

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

    "append": "writes", "batch": "writes", "edit": "writes",
    "format": "writes", "format_staged": "writes", "gc": "writes",
    "init": "writes", "json-set": "writes",
    "paste": "writes", "rename": "writes", "replace": "writes",
    "replace_lines": "writes", "vim": "writes",
}































_READ_ONLY_ENV = "SUPERTOOL_READ_ONLY"


def _read_only_declared() -> bool:





    return (os.environ.get(_READ_ONLY_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on")


def _op_is_recognized(op: str) -> bool:









    if op in _valid_op_names():
        return True
    config = _load_config()
    for section in ("ops", "aliases"):
        entries = config.get(section)
        if isinstance(entries, dict) and op in entries:
            return True
    return False


def _op_is_preset_op(op: str) -> bool:















    _ops_cfg = _load_config().get("ops")
    return isinstance(_ops_cfg, dict) and op in _ops_cfg


def _op_safety_class(op: str) -> str:





















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

































_PARALLEL_SAFE_OPS = {
    "read", "grep", "glob", "ls", "head", "tail", "wc", "stat",
    "map", "tree", "around", "around_line", "between", "diff",
    "version", "validate", "validate_staged", "workspace",
    "resolve", "diag", "hover", "help", "doctor", "payload-lint",
}












_PATH_ARG_POSITIONS = {
    "read": (1,), "head": (1,), "tail": (1,), "wc": (1,),
    "stat": (1,), "around_line": (1,), "ls": (1,), "tree": (1,),
    "map": (1,), "validate": (1,), "format": (1,),
    "workspace": (1,), "diag": (1,),






    "grep_around": (2,),




















    "hover": (2,), "rename": (3,), "resolve": (2,),

    "diff": (1, 2),









    "check": (2,),

    "edit": (3,), "replace": (3,), "replace_dry": (3,),
    "replace_lines": (1,), "paste": (1,), "append": (1,), "vim": (1,),
}


def _is_parallel_safe(arg: str) -> bool:









    m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_-]*)(:::|:|$)", arg)
    if not m:
        return False
    return m.group(1) in _PARALLEL_SAFE_OPS


_DRIVE_LETTER = re.compile(r"^@?[A-Za-z]\Z")  
_URL_SCHEMES = ("http", "https", "ftp", "ftps", "ssh", "git", "file", "ws", "wss")


_URL_PORT = re.compile(r"^\d+(?:[/?#].*)?\Z")  





_DECODE_ESCAPES_SENTINEL = "\x00BS\x00"


def _decode_escapes(s: str) -> str:



















    if "\\" not in s:
        return s
    out = s.replace("\\\\", _DECODE_ESCAPES_SENTINEL)
    out = out.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")


    out = re.sub(
        r"\\x([0-9A-Fa-f]{2})",
        lambda m: chr(int(m.group(1), 16)),
        out,
    )

    out = re.sub(r"\\([^A-Za-z0-9])", r"\1", out)
    out = out.replace(_DECODE_ESCAPES_SENTINEL, "\\")
    return out


def _split_arg(arg: str) -> List[str]:



















    raw = arg.split(":")  
    tokens: List[str] = []
    i = 0
    while i < len(raw):
        piece = raw[i]

        while i + 1 < len(raw):
            next_piece = raw[i + 1]
            last_seg = piece.rsplit("|", 1)[-1]




            drive_seg = last_seg.rsplit(",", 1)[-1]










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





























_MAX_COLON_SLOTS: Dict[str, int] = {
    "head": 2, "tail": 2, "tree": 2, "diff": 2, "glob": 2,
    "wc": 1, "ls": 1, "stat": 1, "map": 1,





    "payload-lint": 1,
    "around_line": 3,
    "grep_around": 4,
}


def _op_syntax(op: str) -> str:






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








    if op == "read":
        extra: List[str] = []






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







    if op != "read" or len(parts) < 2:
        return ""
    for tok in extra:
        if tok.startswith("lines=") and _READ_RANGE_RE.fullmatch(tok[6:]):






            return (f"For a line range use the syntax form: "
                    f"read:{_flat_field(parts[1], disclose_newline=True)}"
                    f":{tok[6:]}")
    return ""


def _grep_peel_trailing(parts: List[str]) -> Tuple[List[str], List[str],
                                                   bool, bool, bool]:













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










    if not parts[1:]:
        return []
    _args, trailing, _count, _no_auto, _full = _grep_peel_trailing(parts)
    if _GREP_ALL_TOKEN in trailing[1:]:
        return []
    return trailing[2:]


def _swap_suggest(op: str, sig: str, other_key: str, other_value: str,
                  missing_value: str, call_template: str) -> Optional[str]:






























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

















    if not parts[1:]:
        return ("", ".", _get_op_int("grep", "max_results", MAX_GREP_RESULTS), 0, False, False, False)






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







            limit = GREP_LIMIT_ALL_MISPLACED
        else:
            context = int(trailing[1])



    if len(args) >= 2:
        path = args[-1] if args[-1] else "."
        pattern = ":".join(args[:-1])
    else:

        pattern = args[0] if args else ""
        path = "."

    return (pattern, path, limit, context, count_only, no_auto_read, full)


def _parse_around_args(parts: List[str]) -> tuple:





    args = parts[1:]
    if not args:
        return ("", "", 10)


    n = 10
    if len(args) >= 3 and _is_ascii_int(args[-1]):
        n = int(args[-1])
        args = args[:-1]


    if len(args) >= 2:
        path = args[-1] if args[-1] else ""
        pattern = ":".join(args[:-1])
    else:

        pattern = args[0] if args else ""
        path = ""

    return (pattern, path, n)


def _around_line_delegation(pattern: str, path: str, n: int) -> str:











































    if not _is_ascii_int(path) or os.path.exists(path):
        return ""
    line = int(path)
    if line < 1:
        return ""
















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





































    if len(parts) not in (3, 4):
        return ""
    nums = parts[2:]





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













    if "," not in path:
        return ""
    entries = [e for e in path.split(",") if e]
    if len(entries) < 2:
        return ""





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







_SHIM_CORE = {"supertool.py": "_supertool.py"}


def _shim_core_beside(path: str) -> str:






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
















    core = _shim_core_beside(path)
    if not core:
        return ""
    return (f"(note: {os.path.basename(path)} is only the entry point — "
            f"supertool's implementation lives in {core} beside it (#931). "
            f"The symbols above are the shim's own and this map is complete "
            f"for that file; supertool's own surface is next door. Add "
            f"map:{core} to see it.)" + chr(10))


def _shim_facade_note(path: str) -> str:




















    core = _shim_core_beside(path)
    if not core:
        return ""
    return (f"(note: {os.path.basename(path)} is only the entry point — "
            f"supertool's implementation lives in {core} beside it (#931). "
            f"An empty result here is evidence about the shim, not about "
            f"supertool. Re-run against {core}.)" + chr(10))


def _multi_path_suggest(op: str, path: str,
                        call_prefix: Optional[str] = None) -> str:




























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
















_DRIVE_LETTER_AFTER_SPACE = re.compile(r"\s([A-Za-z])\Z")


def _drive_letter_swap_suggest(op: str, leading: str, path: str) -> str:















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






    if not tok or tok != tok.strip():
        return False
    return not any(c in tok for c in " \t\"'()|<>*?")


def _absorbed_pattern_segments(leading: str) -> List[str]:











    return _split_arg("x:" + leading)[1:]


def _absorbed_path_hint(op: str, leading: str, path: str,
                        keys: Tuple[str, ...] = ("pattern",)) -> str:







































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










_GREP_AROUND_NUMERIC_SLOTS = ((3, "N", False), (4, "LIMIT", True))


def _grep_around_numeric_refusal(parts: List[str], pattern: str,
                                 path: str) -> str:























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




















    if not path or path == "." or os.path.exists(path):




















        return ""
    if ":" not in leading and _looks_like_path(path):
        return ""












    if swap_fallback:
        _leading_err, (_leading_expanded,) = _gate_paths([leading])
        if not _leading_err and (os.path.isfile(_leading_expanded)
                                  or os.path.isdir(_leading_expanded)):
            return ""




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
