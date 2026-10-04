


















from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_presets.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )





class SecurityError(Exception):

    pass






_MAX_SAFE_PATH_LEN = 4096






_ALLOW_OUTSIDE_HINT = (
    "For a one-off call, no config edit and no residue: prefix the call "
    "with `cwd:PATH` to move the boundary there for this call only "
    "(#1784). To allow it for every future call: define "
    "SUPERTOOL_ALLOW_OUTSIDE_CWD=1 as an environment variable, or add "
    '`"allow_outside_cwd": true` to .supertool.json.'
)


def _safe_path(p: str, *, allow_outside_cwd: Optional[bool] = None,
               root: Optional[str] = None, boundary: str = "cwd") -> str:






















































    if allow_outside_cwd is None:
        if os.environ.get("SUPERTOOL_ALLOW_OUTSIDE_CWD") == "1":
            allow_outside_cwd = True
        else:


            try:
                allow_outside_cwd = bool(_load_config().get("allow_outside_cwd"))
            except Exception:
                allow_outside_cwd = False


    if "\x00" in p:
        raise SecurityError(f"path contains NUL byte: {p!r}")






    if len(p) > _MAX_SAFE_PATH_LEN:
        raise SecurityError(
            f"path too long ({len(p)} chars, max {_MAX_SAFE_PATH_LEN})"
        )
    expanded = os.path.expanduser(p)  
    try:
        abs_p = os.path.realpath(expanded)
    except (ValueError, OSError) as e:


        shown = p if len(p) <= 120 else p[:120] + "…"
        raise SecurityError(f"path cannot be resolved: {shown!r} ({e})") from e
    if allow_outside_cwd:
        return abs_p





    abs_p_cmp = os.path.normcase(abs_p)
    base = os.path.realpath(root) if root else os.path.realpath(os.getcwd())
    root_cmp = os.path.normcase(base)
    if abs_p_cmp == root_cmp:
        return abs_p
    if not abs_p_cmp.startswith(root_cmp + os.sep):





        where = "" if root is None else f", root {base}"
        raise SecurityError(
            f"path escapes {boundary}: {p!r} (resolved to {abs_p!r}{where}). "
            + _ALLOW_OUTSIDE_HINT
        )
    return abs_p


def _containment_error(candidates: Iterable[str], *,
                       root: Optional[str] = None,
                       boundary: str = "cwd") -> Optional[str]:











































    for candidate in candidates:
        if not candidate or candidate == ".":
            continue
        try:
            _safe_path(candidate, root=root, boundary=boundary)
        except SecurityError as exc:
            return f"ERROR: {exc}\n"
    return None


def _expand_home(p: str) -> str:




























    if not isinstance(p, str) or not p.startswith("~"):
        return p
    return os.path.expanduser(p)


def _gate_paths(candidates: Iterable[str], *,
                root: Optional[str] = None,
                boundary: str = "cwd") -> Tuple[Optional[str], List[str]]:












    originals = list(candidates)
    err = _containment_error(originals, root=root, boundary=boundary)
    return err, [_expand_home(c) for c in originals]





_GLOB_MAGIC_STANDIN = "__supertool_glob_magic__"


def _glob_split(pattern: str) -> List[str]:

    seps = "/" + (os.sep if os.sep != "/" else "")
    return re.split("[" + re.escape(seps) + "]", pattern)


def _glob_reach(pattern: str) -> str:



















    return "/".join(
        _GLOB_MAGIC_STANDIN if WILDCARD_CHARS.search(comp) else comp
        for comp in _glob_split(pattern)
    )


def _glob_reach_min(pattern: str) -> str:














    kept = [_GLOB_MAGIC_STANDIN if WILDCARD_CHARS.search(comp) else comp
            for comp in _glob_split(pattern) if comp != "**"]
    return "/".join(kept) or "."


def _glob_pattern_containment_error(pattern: str) -> Optional[str]:


















    for sub in _expand_braces(pattern):
        for reach in (_glob_reach(sub), _glob_reach_min(sub)):
            err = _containment_error([reach])
            if err:
                return (err.replace(repr(reach), repr(sub))
                           .replace(_GLOB_MAGIC_STANDIN, "*"))
    return None


def _glob_results_escape(files: Iterable[str]) -> bool:











    for f in files:
        try:
            _safe_path(f)
        except SecurityError:
            return True
    return False





_PATH_BOUNDARIES = ("cwd", "repo")




_PATH_SYNTAX_COMPONENTS = frozenset(("PATH", "PATHS", "FILE", "FILES"))

_SYNTAX_TOKEN_RE = re.compile(r"[^A-Za-z0-9_]+")



























_PATH_CMD_PLACEHOLDERS = ("{file}", "{dir}")


_ALL_ARGS_PLACEHOLDERS = ("{args}", "{argjoin}")



_ONE_ARG_PLACEHOLDERS = ("{file}", "{dir}", "{arg}")


def _unconsumed_arg_tokens(cmd_template: str, parts: List[str]) -> List[str]:


























    if any(p in cmd_template for p in _ALL_ARGS_PLACEHOLDERS):
        return []
    if any(p in cmd_template for p in _ONE_ARG_PLACEHOLDERS):
        extra = list(parts[2:])
        return extra if any(extra) else []
    extra = list(parts[1:])
    return extra if any(extra) else []


def _declared_path_slots(entry: Any) -> List[int]:










    if not isinstance(entry, dict):
        return []
    decl = entry.get("paths")
    if not isinstance(decl, dict) or not isinstance(decl.get("args"), list):
        return []
    return [i for i in decl["args"]
            if isinstance(i, int) and not isinstance(i, bool) and i >= 0]


def _dropped_tokens_refusal(
        op: str, entry: Any, cmd_template: str, dropped: List[str]) -> str:




























    sep = _ARG_SEP[0] or ":"
    used = [p for p in _ONE_ARG_PLACEHOLDERS if p in cmd_template]
    reach = (f"substitutes {', '.join(used)}, which is the FIRST argument token "
             f"and nothing after it"
             if used else
             "substitutes no argument placeholder, so no argument token reaches it")
    syntax = entry.get("syntax") if isinstance(entry, dict) else None
    lines = [
        f"ERROR: op {op!r} was given {len(dropped)} argument token(s) its cmd "
        f"cannot reach: {sep.join(dropped)!r}.",
        f"       The template {reach}.",
        "       Refused rather than dropped (#873): a discarded ':dry' once ran "
        "an op in live mode",
        "       while its receipt read as a dry run.",
    ]
    gated = _declared_path_slots(entry)
    if gated:
        positions = ", ".join(str(i) for i in gated)
        lines += [
            "       Widening the cmd to {args} or {argjoin} is NOT the remedy "
            "here: this op declares",
            f'       "paths": {{"args": [{positions}]}}, so only argument '
            f"position(s) {positions} are containment-checked,",
            "       and a template that consumes the whole tail hands every "
            "later position to the child",
            "       unchecked (#1135, #1560). Extending that `args` list "
            "alongside it is necessary and not",
            "       sufficient — it is a fixed index list, so it holds an "
            "unbounded {args} tail only up to",
            "       the highest index it names. There is no one-line cmd "
            "change that keeps this op contained.",
            "       What IS checked is the first token: carry several fields "
            "inside it, separated by ',' or '|'.",
        ]
    else:
        lines += [
            "       To take every token, write {args} (one shell-quoted argv "
            "word per token) or",
            "       {argjoin} (every token rejoined with ':::' as a single "
            "argument) in the cmd.",
            "       To carry several fields inside the first token, separate "
            "them with ',' or '|'.",
        ]
    if isinstance(syntax, str) and syntax:
        lines.append(f"       This op documents: {syntax}")
    return chr(10).join(lines) + chr(10)











































_UNDECLARED_PATH_OPS = frozenset((
    "bluesky_publish",
    "devto_comment",
    "devto_publish",
    "gh-batch-follow",
    "gh-batch-star",
    "gh-issue-create",
    "gh-pr",
    "gh-pr-create",
    "git-blame",
    "git-commit",
    "git-diff",
    "git-investigate",
    "git-resolve",
    "git-trail",
    "git-worktrees",
    "gl-issue-create",
    "hashnode_comment",
    "hashnode_publish",
    "hashnode_reply",
))


def _syntax_names_a_path(syntax: str) -> bool:

























    if not isinstance(syntax, str):
        return False
    for token in _SYNTAX_TOKEN_RE.split(syntax):
        for component in token.split("_"):
            if component in _PATH_SYNTAX_COMPONENTS:
                return True
    return False


def _cmd_names_a_path(cmd: Any) -> Optional[str]:







    if not isinstance(cmd, str):
        return None
    for placeholder in _PATH_CMD_PLACEHOLDERS:
        if placeholder in cmd:
            return placeholder
    return None


def _entry_names_a_path(entry: Any) -> Optional[str]:

























    if not isinstance(entry, dict):
        return None
    syntax = entry.get("syntax", "")
    if _syntax_names_a_path(syntax):
        return f"names a path in its syntax ({syntax})"
    placeholder = _cmd_names_a_path(entry.get("cmd", ""))
    if placeholder is not None:
        return (f"substitutes the core's {placeholder} path placeholder in "
                f"its cmd")
    return None


def _path_boundary_label(boundary: str) -> str:









    return "the repository root" if boundary == "repo" else "cwd"


def _repo_root_for_containment() -> str:

















    d = os.path.realpath(os.getcwd())
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.path.realpath(os.getcwd())
        d = parent


def _undeclared_path_refusal(op: str, signal: str) -> str:  








    return (
        f"ERROR: op {op!r} {signal} and declares "
        f"no containment boundary.\n"
        f'       Add "paths": {{"args": [1], "root": "cwd"}} to its registry '
        f"entry — \"args\" lists the\n"
        f"       argument positions that are filesystem paths, \"root\" is "
        f'"cwd" (the core\'s boundary)\n'
        f'       or "repo" (the repository root, for an op that resolves '
        f'relative paths against it).\n'
        f'       "args": [] declares that no argument here is a filesystem '
        f"path.\n"
    )


def _preset_path_containment(
        op: str, entry: Any, parts: List[str]) -> Optional[str]:
























    if not isinstance(entry, dict):
        return None
    decl = entry.get("paths")
    if decl is None:
        if op in _UNDECLARED_PATH_OPS:
            return None
        signal = _entry_names_a_path(entry)
        if signal is None:
            return None
        return _undeclared_path_refusal(op, signal)
    if (not isinstance(decl, dict) or not isinstance(decl.get("args"), list)
            or not all(isinstance(i, int) and not isinstance(i, bool) and i >= 0
                       for i in decl["args"])):









        return (
            f'ERROR: op {op!r} has a malformed "paths" declaration — expected '
            f'{{"args": [<non-negative int>, ...], "root": "cwd"|"repo"}}.\n'
        )
    boundary = decl.get("root", "cwd")
    if boundary not in _PATH_BOUNDARIES:
        return (
            f'ERROR: op {op!r} declares an unknown path root {boundary!r} — '
            f'expected one of: {", ".join(_PATH_BOUNDARIES)}.\n'
        )
    slots = [i for i in decl["args"] if 0 <= i < len(parts)]
    err, gated = _gate_paths(
        (parts[i] for i in slots),
        root=_repo_root_for_containment() if boundary == "repo" else None,
        boundary=_path_boundary_label(boundary),
    )
    if err:
        return err
    for _i, _slot in enumerate(slots):
        parts[_slot] = gated[_i]
    return None


def _path_not_found(path: str, *, label: str = "path",
                     suggest: Optional[str] = None,
                     op: Optional[str] = None,
                     call_prefix: Optional[str] = None,
                     creates: bool = False) -> str:









































    if not path:
        return f"ERROR: {label} not found: {path}\n"
    if not suggest and op:
        suggest = (_comma_path_list_suggest(op, path)
                   or _multi_path_suggest(op, path, call_prefix)
                   or None)







    tried = os.path.abspath(path)







    shown, shown_tried = (
        _flat_field(path, disclose_newline=True),
        _flat_field(tried, disclose_newline=True),
    )
    shown_cwd = _flat_field(os.getcwd(), disclose_newline=True)
    if not suggest and path.startswith("~") and os.path.expanduser(path) == path:




        suggest = ("`~` was not expanded: no such user. Pass an absolute "
                   "path, or `~/` for your own home.")







    if os.path.isdir(path):
        return (





            f"ERROR: {shown} is a directory, not a file\n"
            f"  tried: {shown_tried} (cwd: {shown_cwd})\n"
            f"  '{op or label}' takes a single file. `ls:{shown}` or "
            f"`tree:{shown}` lists what is in it.\n"
        )
    lines = [
        f"ERROR: {label} not found: {shown}",
        f"  tried: {shown_tried} (cwd: {shown_cwd})",
    ]
    root = None
    if not os.path.isabs(path):
        try:
            root = _project_root_above_cwd()
        except OSError:
            root = None
    if root and os.path.exists(os.path.join(root, path)):
        lines.append(
            f"  exists at "
            f"{_flat_field(os.path.join(root, path), disclose_newline=True)}"
            f" — prefix the call with "
            f"'cwd:{_flat_field(root, disclose_newline=True)}' to run it "
            f"from the project root"
        )
    elif suggest:
        lines.append(f"  {suggest}")
    else:
        lines.append(
            "  wrong CWD? Prefix the call with cwd:PATH to run it from "
            "elsewhere."
        )
        if creates:
            lines.append(f"  {_create_instead_hint()}")
    return "\n".join(lines) + "\n"


def _extract_env_prefix(cmd: str) -> Tuple[Dict[str, str], str]:











    assignments: Dict[str, str] = {}
    tokens = shlex.split(cmd, posix=True)
    idx = 0










    _kv = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z", re.DOTALL)
    while idx < len(tokens):
        m = _kv.match(tokens[idx])
        if not m:
            break
        assignments[m.group(1)] = m.group(2)
        idx += 1
    if not assignments:
        return {}, cmd



    remaining = " ".join(shlex.quote(t) for t in tokens[idx:])
    return assignments, remaining




def _in_template_single_quotes(s: str, pos: int) -> bool:


















    in_single = False
    in_double = False
    i = 0
    while i < pos:
        c = s[i]
        if in_single:
            if c == "'":
                in_single = False
        elif in_double:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_double = False
        else:
            if c == "\\":
                i += 2
                continue
            if c == "'":
                in_single = True
            elif c == '"':
                in_double = True
        i += 1
    return in_single


def _expand_env(s: str, extras: Dict[str, str]) -> str:



























































































    def _lookup(name: str) -> Optional[str]:
        if name in extras:
            return extras[name]
        probe = "${" + name + "}"
        found = os.path.expandvars(probe)
        return None if found == probe else found

    def _replace(m: "re.Match[str]") -> str:
        name = m.group(1) or m.group(2)
        value = _lookup(name)
        if value is None:
            return m.group(0)
        if _in_template_single_quotes(s, m.start()):









            return value.replace("'", "'\"'\"'")
        return shlex.quote(value)

    return re.sub(
        r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)',
        _replace,
        s,
    )


def _shield_substitute(
    template: str, values: Dict[str, str],
) -> Tuple[str, Dict[str, str]]:




































    nonce = "STPH" + os.urandom(8).hex()
    tokens: Dict[str, str] = {}
    shield: Dict[str, str] = {}
    for i, name in enumerate(sorted(values)):
        token = f".{nonce}x{i}x."
        tokens[name] = token
        shield[token] = values[name]
    return _substitute_placeholders(template, tokens), shield


def _unshield(s: str, shield: Dict[str, str]) -> str:







    for token, value in shield.items():
        s = s.replace(token, value)
    return s


def _unshield_env_value(s: str, shield: Dict[str, str]) -> str:















    restored = _unshield(s, shield)
    if restored == s:
        return restored  
    try:
        parts = shlex.split(restored)
    except ValueError:
        return restored
    return parts[0] if len(parts) == 1 else restored



























GIT_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
)








_LEAKED_GIT_ENV: List[str] = []


def _scrub_process_git_env() -> List[str]:












    removed = []
    if os.environ.pop("GIT_DIR", None) is not None:
        removed.append("GIT_DIR")
    if os.environ.pop("GIT_WORK_TREE", None) is not None:
        removed.append("GIT_WORK_TREE")
    if os.environ.pop("GIT_COMMON_DIR", None) is not None:
        removed.append("GIT_COMMON_DIR")
    if os.environ.pop("GIT_INDEX_FILE", None) is not None:
        removed.append("GIT_INDEX_FILE")
    if os.environ.pop("GIT_OBJECT_DIRECTORY", None) is not None:
        removed.append("GIT_OBJECT_DIRECTORY")
    if os.environ.pop("GIT_ALTERNATE_OBJECT_DIRECTORIES", None) is not None:
        removed.append("GIT_ALTERNATE_OBJECT_DIRECTORIES")
    if os.environ.pop("GIT_NAMESPACE", None) is not None:
        removed.append("GIT_NAMESPACE")
    return removed


def scrub_git_env(target: Optional[MutableMapping[str, str]] = None) -> List[str]:








    if target is None:
        return _scrub_process_git_env()









    removed = []
    if "GIT_DIR" in target:
        removed.append("GIT_DIR")
        del target["GIT_DIR"]
    if "GIT_WORK_TREE" in target:
        removed.append("GIT_WORK_TREE")
        del target["GIT_WORK_TREE"]
    if "GIT_COMMON_DIR" in target:
        removed.append("GIT_COMMON_DIR")
        del target["GIT_COMMON_DIR"]
    if "GIT_INDEX_FILE" in target:
        removed.append("GIT_INDEX_FILE")
        del target["GIT_INDEX_FILE"]
    if "GIT_OBJECT_DIRECTORY" in target:
        removed.append("GIT_OBJECT_DIRECTORY")
        del target["GIT_OBJECT_DIRECTORY"]
    if "GIT_ALTERNATE_OBJECT_DIRECTORIES" in target:
        removed.append("GIT_ALTERNATE_OBJECT_DIRECTORIES")
        del target["GIT_ALTERNATE_OBJECT_DIRECTORIES"]
    if "GIT_NAMESPACE" in target:
        removed.append("GIT_NAMESPACE")
        del target["GIT_NAMESPACE"]
    return removed


def _git_env_notice(removed: List[str]) -> str:
















    if not removed:
        return ""
    return (
        f"scrubbed inherited git variables: {', '.join(removed)} — this call acted "
        f"on the repo at {os.getcwd()}, not the one those variables named "
        f"(#692, #714)\n"
    )


def _declared_value_exits(entry: object) -> FrozenSet[int]:


















    if not isinstance(entry, dict):
        return frozenset()
    decl = entry.get("exitStatus")
    if not isinstance(decl, dict):
        return frozenset()
    values = decl.get("values")
    if not isinstance(values, list):
        return frozenset()

    return frozenset(v for v in values
                     if isinstance(v, int) and not isinstance(v, bool))


def _declared_clean_exits(entry: object) -> frozenset:





















    if not isinstance(entry, dict):
        return frozenset({0})
    decl = entry.get("exitStatus")
    if not isinstance(decl, dict):
        return frozenset({0})
    clean = decl.get("clean")
    if not isinstance(clean, list):
        return frozenset({0})
    return frozenset({0}) | frozenset(
        v for v in clean if isinstance(v, int) and not isinstance(v, bool))


def _preset_declares_error(body: str) -> bool:


















    return any(line.startswith("ERROR: ") for line in body.splitlines())


def _op_config_collision_refusal(op: str, entry: Any,
                                  config: Dict[str, Any]) -> str | None:













    if not isinstance(entry, dict):
        return None
    collisions = config.get("_op_config_collisions")
    if not isinstance(collisions, dict) or not collisions:
        return None
    for key in entry:
        if not isinstance(key, str) or key in _OP_CONFIG_RESERVED_KEYS:
            continue
        env_key = f"SUPERTOOL_{key.upper()}"
        others = collisions.get(env_key)
        if others and op in others:
            rest = ", ".join(o for o in others if o != op)
            return (
                f"ERROR: `ops.{op}.{key}` in .supertool.json reaches this op "
                f"as `{env_key}`, and `ops.{rest}` declares the same key "
                f"name in the same file. Both would read one shared "
                f"variable with no way for either op to tell whose value it "
                f"got (#1009). Rename one — e.g. "
                f"`{op.replace('-', '_')}_{key}` — or drop the duplicate.\n"
            )
    return None


def _resolve_custom_op(op: str, parts: List[str]) -> str | None:











    config = _load_config()
    ops = config.get("ops")
    if not ops or op not in ops:
        return None




    _CUSTOM_OP_OK[0] = None





    _mixed = _mixed_tree_pair()
    if _mixed is not None and not _mixed_tree_allowed():
        _bump_counter(_SKIP_COUNT, "cnt_skip")
        return _mixed_tree_decline(op, _mixed)

    entry = ops[op]
    if isinstance(entry, str):
        cmd_template = entry
        timeout = config.get("timeout", 60)
    elif isinstance(entry, dict):
        cmd_template = entry.get("cmd", "")
        timeout = entry.get("timeout", config.get("timeout", 60))
    else:
        return f"ERROR: invalid config for custom op {op!r}\n"

    if not cmd_template:
        return f"ERROR: empty command for custom op {op!r}\n"







    _collision = _op_config_collision_refusal(op, entry, config)
    if _collision:
        return _collision














    _paths = _preset_path_containment(op, entry, parts)
    if _paths:
        return _paths








    _dropped = _unconsumed_arg_tokens(cmd_template, parts)
    if _dropped:
        return _dropped_tokens_refusal(op, entry, cmd_template, _dropped)







    file_arg = parts[1] if len(parts) > 1 else ""
    dir_arg = os.path.dirname(file_arg) if file_arg else "."



    arg_join = ":::".join(parts[1:]) if len(parts) > 1 else ""





    cmd, _shield = _shield_substitute(cmd_template, {
        "python": _python_token(),
        "file": shlex.quote(file_arg),
        "dir": shlex.quote(dir_arg),
        "arg": shlex.quote(file_arg),
        "args": " ".join(shlex.quote(p) for p in parts[1:]) if len(parts) > 1 else "",
        "argjoin": shlex.quote(arg_join),
    })






















    _RESERVED_KEYS = _OP_CONFIG_RESERVED_KEYS













    extras: Dict[str, str] = {}





    extras["SUPERTOOL_ARG_SEP"] = _ARG_SEP[0]
    if isinstance(entry, dict):
        for k, v in entry.items():
            if k not in _RESERVED_KEYS:









                if (k in _RELATIVE_SEARCH_PATH_KEYS and isinstance(v, str)
                        and _CONFIG_PATH):
                    v = _anchor_relative_search_path(v, _CONFIG_PATH)




                extras[f"SUPERTOOL_{k.upper()}"] = v if isinstance(v, str) else json.dumps(v)

    _prefix_env, cmd = _extract_env_prefix(cmd)


    _prefix_env = {k: _unshield_env_value(v, _shield) for k, v in _prefix_env.items()}
    extras.update(_prefix_env)
    cmd = _unshield(_expand_env(cmd, extras), _shield)

    t0 = time.monotonic()
    try:








        result = subprocess.run(
            shlex.split(cmd), shell=False, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", env={**os.environ, **extras},
        )
        elapsed = _elapsed_since(t0)
        output = result.stdout




        _value_exit = (result.returncode != 0
                       and result.returncode in _declared_value_exits(entry))
        _failed = result.returncode != 0
        if _value_exit:
            _failed = bool(result.stderr.strip()) or _preset_declares_error(output)
        _CUSTOM_OP_OK[0] = not _failed






        _unclean_value = (
            _value_exit and not _failed
            and result.returncode not in _declared_clean_exits(entry))
        if _unclean_value:
            _UNCLEAN_VALUE_EXITS.append(f"{op} exited {result.returncode}")
        if _failed:
            if result.stderr:
                output += result.stderr
            return f"FAIL ({elapsed:.2f}s)\n{output}"


        _stamp = f" [{_mixed_tree_note(_mixed)}]" if _mixed is not None else ""



        _value_note = (f" [exit {result.returncode} is this op's answer, not a "
                       f"verdict — its registry entry declares the exit code a "
                       f"value]") if _value_exit else ""



        if _unclean_value:
            _value_note = (f" [exit {result.returncode} is this op's answer, not "
                           f"a verdict — its registry entry declares the exit "
                           f"code a value, and not one it declares clear to "
                           f"proceed. The op did not fail; supertool exits "
                           f"non-zero so a `&&` guard does not read this answer "
                           f"as permission]")
        return (f"PASS ({elapsed:.2f}s){_stamp}{_value_note}\n"
                f"{output}{_maybe_restart_mcp(entry)}")
    except subprocess.TimeoutExpired as e:


        return (f"{_timeout_verdict_line(t0, timeout)}\n"
                f"{_timeout_partial_output(e)}")
    except OSError as e:
        return f"FAIL: {e}\n"


def _timeout_partial_output(exc: subprocess.TimeoutExpired) -> str:






    chunks = []
    for stream in (exc.stdout, exc.stderr):
        if not stream:
            continue
        text = stream.decode("utf-8", "replace") if isinstance(stream, bytes) else stream
        if text.strip():
            chunks.append(text if text.endswith("\n") else text + "\n")
    if not chunks:
        return ""
    return "--- partial output before timeout ---\n" + "".join(chunks)


def _maybe_restart_mcp(entry: object) -> str:






















    if not isinstance(entry, dict):
        return ""
    spec = entry.get("restartMcp")
    if not spec:
        return ""
    if spec is True:
        names = list(_mcp_specs.keys())
    elif isinstance(spec, list):
        names = [str(n) for n in spec]
    else:
        names = [str(spec)]
    known = [n for n in names if n in _mcp_specs]
    unknown = [n for n in names if n not in _mcp_specs]
    restarted, failed = [], []
    for name in known:
        outcome = _mcp_stop_server(name)
        (restarted if outcome.ok else failed).append(name)










    note = ""
    if restarted:
        note += (f"mcp: restarted {len(restarted)} daemon(s) "
                 f"({_flat_keys(restarted)})\n")
    if failed:
        note += (f"mcp: FAILED to stop {len(failed)} daemon(s) ({_flat_keys(failed)})"
                 f" — they may still answer from a stale index"
                 f" (SUPERTOOL_DEBUG=1 for the reason)\n")
    if unknown:
        note += f"mcp: unknown server(s) ignored ({_flat_keys(unknown)})\n"
    return note


_IN_ALIAS = False  


def _resolve_alias(op: str, parts: List[str]) -> str | None:





    global _IN_ALIAS
    if _IN_ALIAS:
        return None  

    config = _load_config()
    aliases = config.get("aliases")
    if not aliases or op not in aliases:
        return None

    alias_def = aliases[op]
    if not isinstance(alias_def, dict):
        return f"ERROR: alias {op!r} must be an object with 'ops' key\n"

    op_list = alias_def.get("ops", [])
    if not isinstance(op_list, list):
        return f"ERROR: alias {op!r} 'ops' must be a list\n"

    if not op_list:
        return ""


    file_arg = parts[1] if len(parts) > 1 else ""
    dir_arg = os.path.dirname(file_arg) if file_arg else "."
    all_args = " ".join(parts[1:]) if len(parts) > 1 else ""

    _IN_ALIAS = True
    try:
        output_parts: List[str] = []
        alias_values = {
            "file": file_arg, "dir": dir_arg,
            "arg": file_arg, "args": all_args,
        }
        for expanded_op in op_list:


            resolved = _substitute_placeholders(expanded_op, alias_values)
            output_parts.append(dispatch(resolved))
        return "".join(output_parts)
    finally:
        _IN_ALIAS = False


