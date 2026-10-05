









































from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_config.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )






















_NOISE_EXCLUDE_PATHS: Tuple[str, ...] = (
    ".git/", "node_modules/", ".svn/", ".hg/", ".idea/", ".vscode/",
    "__pycache__/", ".venv/", "venv/", "dist/", "build/",
    "phpstan-result-cache/", ".phpunit.cache/", ".rector/",
)



























_CREDENTIAL_STEMS_SPELLED_APART_2734: Tuple[Tuple[str, str, str], ...] = (

    (".", "max", "/"), (".", "ssh", "/"), (".", "aws", "/"),
    (".", "gnupg", "/"), (".", "kube", "/"), (".", "docker", "/"),
    (".", "terraform", "/"), (".", "chef", "/"), (".", "npm", "/"),
    ("", "secrets", "/"), ("", "credentials", "/"),

    ("", ".env", "/"), ("", ".env", ".*"),
    ("!", ".env", ".example"), ("!", ".env", ".sample"),
    ("!", ".env", ".template"), ("!", ".env", ".dist"),
    ("!", ".env", ".defaults"), ("!", ".env", ".schema"),

    (".", "netrc", "/"), ("_", "netrc", "/"), (".", "npmrc", "/"),
    (".", "pypirc", "/"), (".", "git-credentials", "/"),
    (".", "pgpass", "/"), (".", "my.cnf", "/"), (".", "htpasswd", "/"),
    (".", "dockercfg", "/"),

    ("id_", "rsa", "*"), ("id_", "dsa", "*"), ("id_", "ecdsa", "*"),
    ("id_", "ed25519", "*"),
    ("*.", "pem", ""), ("*.", "key", ""), ("*.", "p12", ""), ("*.", "pfx", ""),
    ("*.", "jks", ""), ("*.", "keystore", ""), ("*.", "ppk", ""),


    (".", "hashnode-token", "/"), (".", "devto-token", "/"),
    (".", "bluesky-app-password", "/"),
)

_SECRET_EXCLUDE_PATHS: Tuple[str, ...] = tuple(
    prefix + stem + suffix
    for prefix, stem, suffix in _CREDENTIAL_STEMS_SPELLED_APART_2734)


_DEFAULT_EXCLUDE_PATHS: Tuple[str, ...] = (
    _NOISE_EXCLUDE_PATHS + _SECRET_EXCLUDE_PATHS
)
_NOISE_EXCLUDE_SET = frozenset(_NOISE_EXCLUDE_PATHS)
WILDCARD_CHARS = re.compile(r"[*?\[]")

_COMPACT_SKIP = re.compile(
    r"^\s*$"           
    r"|^\s*//"         
    r"|^\s*#"          
    r"|^\s*\*"         
    r"|^\s*/\*"        
    r"|^\s*\*/"        
    r"|^\s*<!--"       
    r"|^\s*--!?>"      
)


_CONFIG: Dict[str, Any] | None = None
_CONFIG_CHECKED = False





_CONFIG_WARNINGS: List[str] = []






_CONFIG_PATH: str | None = None


_mcp_specs: Dict[str, dict] = {}






_INSTALL_DIR = os.path.dirname(os.path.realpath(__file__)).replace(os.sep, "/")



_SHIPPED_PRESET_OPS: Dict[str, str] | None = None












_SHIPPED_PRESET_SYNTAX: Dict[str, str] = {}


def _shipped_preset_ops() -> Dict[str, str]:















    global _SHIPPED_PRESET_OPS
    if _SHIPPED_PRESET_OPS is not None:
        return _SHIPPED_PRESET_OPS
    index: Dict[str, str] = {}
    _SHIPPED_PRESET_SYNTAX.clear()
    preset_dir = os.path.join(_INSTALL_DIR, "presets")
    try:
        entries = sorted(os.listdir(preset_dir))
    except OSError:
        entries = []
    for fname in entries:
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(preset_dir, fname), encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):



            continue
        if not isinstance(data, dict):
            continue
        preset_ops = {}
        for section in ("ops", "builtin-ops"):
            found = data.get(section)
            if isinstance(found, dict):
                preset_ops.update(found)
        if not preset_ops:
            continue
        for op_name, entry in preset_ops.items():







            if not isinstance(op_name, str):
                continue
            if op_name in index:
                continue
            index[op_name] = fname[:-5]
            syntax = entry.get("syntax") if isinstance(entry, dict) else None
            if isinstance(syntax, str) and syntax.strip():
                _SHIPPED_PRESET_SYNTAX[op_name] = syntax.strip()
    _SHIPPED_PRESET_OPS = index
    return index


def _registry_syntax(name: str) -> str | None:













    entry = (_load_config().get("ops") or {}).get(name)
    if isinstance(entry, dict):
        syntax = entry.get("syntax")
        if isinstance(syntax, str) and syntax.strip():
            return syntax.strip()
    _shipped_preset_ops()  
    return _SHIPPED_PRESET_SYNTAX.get(name)





_REPO_TARGET_MODES: Dict[str, str] | None = None


def _repo_target_modes() -> Dict[str, str]:











    global _REPO_TARGET_MODES
    if _REPO_TARGET_MODES is not None:
        return _REPO_TARGET_MODES
    modes: Dict[str, str] = {}
    preset_dir = os.path.join(_INSTALL_DIR, "presets")
    try:
        entries = sorted(os.listdir(preset_dir))
    except OSError:
        entries = []
    for fname in entries:
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(preset_dir, fname), encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        preset_ops = data.get("ops")
        if not isinstance(preset_ops, dict):
            continue
        for op_name, op_def in preset_ops.items():
            if not isinstance(op_name, str) or not isinstance(op_def, dict):
                continue
            declared = op_def.get("repo_target")
            if declared is True:
                modes.setdefault(op_name, "op")
            elif isinstance(declared, str) and declared:
                modes.setdefault(op_name, declared)
    _REPO_TARGET_MODES = modes
    return modes


def _repo_target_ops() -> set[str]:

    return {op for op, mode in _repo_target_modes().items() if mode == "op"}


def _repo_reachable_ops() -> set[str]:










    return set(_repo_target_modes())














_REPO_SEGMENT_RE = re.compile(r"\A[A-Za-z0-9._][A-Za-z0-9._-]*\Z")


def _repo_target_platform(ops: List[str]) -> str | None:














    presets = _shipped_preset_ops()
    modes = _repo_target_modes()






    found = {presets.get(a.split(":", 1)[0], "")
             for a in ops if a.split(":", 1)[0] in modes}
    if not found:
        return None
    if len(found) > 1:
        return "mixed"










    sole = next(iter(found))
    return sole if sole else "unknown"


def _repo_shape_error(value: str, platform: str | None) -> str | None:





    segments = value.split("/")
    if (len(segments) < 2
            or any(s in (".", "..") or not _REPO_SEGMENT_RE.match(s)
                   for s in segments)):
        return (
            f"repo: expected OWNER/NAME (GitHub) or GROUP[/SUBGROUP]/PROJECT "
            f"(GitLab), got {value!r} — segments may hold only letters, "
            "digits, '.', '_' and '-' "
            "(e.g. repo:Digital-Process-Tools/claude-remember)\n"
        )
    if platform == "github" and len(segments) != 2:
        return (
            f"repo: {value!r} is a GitLab project path, and this call's "
            "repo-targetable ops are GitHub's — gh takes exactly OWNER/NAME "
            "(e.g. repo:Digital-Process-Tools/claude-remember)\n"
        )
    return None


def _repo_refusal(op: str) -> str:











    return (
        f"repo: {op!r} cannot be pointed at a repo, so a repo: op in this call "
        f"would apply to some ops and be silently ignored by this one. Drop "
        f"the repo: op, or give the repo-scoped ops a call of their own.\n"
    )


def _presets_not_loaded_here() -> List[str]:

    config = _load_config()
    enabled = {p for p in (config.get("presets") or []) if isinstance(p, str)}
    return [p for p in sorted(set(_shipped_preset_ops().values()))
            if p not in enabled]







_CONFLICT_MARKER_PREFIXES = ("<<<<<<<", "|||||||", "=======", ">>>>>>>")


def _skipped_config() -> Optional[Tuple[str, str]]:
























































    _load_config()
    if _CONFIG_PATH:
        return None
    d = os.path.abspath(os.getcwd())
    while True:
        candidate = os.path.join(d, ".supertool.json")
        if not os.path.isfile(candidate):







            if os.path.isdir(candidate):
                return (candidate, "is a directory, not a file")
            if os.path.lexists(candidate):
                return (candidate, "is not a regular file")
        else:
            try:



                with open(candidate, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError as exc:
                return (candidate,
                        f"could not be read ({exc.__class__.__name__})")
            marked = [n for n, line in enumerate(text.splitlines(), 1)
                      if line.startswith(_CONFLICT_MARKER_PREFIXES)]
            if marked:
                where = ", ".join(str(n) for n in marked[:8])
                more = "" if len(marked) <= 8 else f", +{len(marked) - 8} more"
                return (candidate,
                        f"holds git conflict markers (lines {where}{more}), "
                        f"so it does not parse")
            try:
                json.loads(text)
            except ValueError as exc:
                return (candidate, f"does not parse ({exc})")






            return (candidate, "was found and could not be loaded")
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _preset_disclosure() -> str:









    missing = _presets_not_loaded_here()
    if not missing:
        return ""
    missing_set = set(missing)
    n_ops = sum(1 for p in _shipped_preset_ops().values() if p in missing_set)
    names = ", ".join(missing)
    if _CONFIG_PATH:
        return (f"> {len(missing)} shipped presets ({names}) — {n_ops} ops — are not "
                f"loaded here: {_CONFIG_PATH} does not list them under "
                f'"presets". Add one there, or make the first op '
                f"'cwd:<project-path>'.")
    skipped = _skipped_config()
    if skipped:



        path, why = skipped
        return (f"> Built-in ops only. {path} {why}, so {len(missing)} shipped "
                f"presets ({names}) — {n_ops} ops — are not loaded here. Repair "
                f"that file and they come back.")
    return (f"> Built-in ops only. No .supertool.json was found from {os.getcwd()}, "
            f"so {len(missing)} shipped presets ({names}) — {n_ops} ops — are not "
            f"loaded here. Run from a project that enables them, or make the first "
            f"op 'cwd:<project-path>'.")













_DIRECTORY_BUILD_EXCLUDED_PRESETS = {"bluesky", "devto", "hashnode", "slack", "watch", "youtube"}


def _find_preset_file(name: str, project_dir: str) -> str | None:







    candidates = [
        os.path.join(project_dir, "presets", f"{name}.json"),
        os.path.join(os.path.expanduser("~"), ".config", "supertool", "presets", f"{name}.json"),
        os.path.join(_INSTALL_DIR, "presets", f"{name}.json"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


def _substitute_placeholders(template: str, values: Dict[str, str]) -> str:










    return _PLACEHOLDER_RE.sub(
        lambda m: values[m.group(1)] if m.group(1) in values else m.group(0),
        template,
    )


def _resolve_preset_cmd(cmd: str, preset_dir: str) -> str:









    path_prefix = preset_dir.replace(os.sep, "/").rstrip("/") + "/"
    return cmd.replace("{path}", path_prefix)



_UNSET = object()


def _merge_op_def(base: Any, override: Any) -> Any:


















    if _merges_key_by_key(base, override):
        merged = dict(base)
        merged.update(override)
        return merged
    return override


def _merges_key_by_key(base: Any, override: Any) -> bool:










    return isinstance(base, dict) and isinstance(override, dict)


def _record_op_sources(config: Dict[str, Any],
                       op_presets: Dict[str, str],
                       preset_bases: Dict[str, Any],
                       project_ops: Dict[str, Any],
                       merged_ops: Dict[str, Any]) -> None:







    sources: Dict[str, Dict[str, Any]] = {}
    for name in merged_ops:
        preset = op_presets.get(name)
        override = project_ops.get(name, _UNSET)
        if override is _UNSET:
            sources[name] = {"preset": preset, "project": False,
                             "overridden": []}
            continue
        if preset is None:
            overridden: Any = []
        elif _merges_key_by_key(preset_bases.get(name), override):
            overridden = sorted(override)
        else:




            overridden = None
        sources[name] = {"preset": preset, "project": True,
                         "overridden": overridden}
    config["_op_sources"] = sources
















_OP_CONFIG_RESERVED_KEYS = {
    "cmd", "timeout", "description", "syntax", "example", "status",
    "restartMcp", "replaces", "paths", "exitStatus", "form", "hint",
    "safety", "repo_target",
}













_RELATIVE_SEARCH_PATH_KEYS = {"watch_sources_path"}


def _anchor_relative_search_path(raw: str, config_path: str) -> str:



























    anchor = os.path.dirname(os.path.realpath(config_path))
    parts = raw.split(os.pathsep)
    resolved = []
    for part in parts:
        entry = part.strip()
        if entry and not os.path.isabs(entry):
            entry = os.path.normpath(os.path.join(anchor, entry))
        resolved.append(entry)
    return os.pathsep.join(resolved)


def _op_config_key_collisions(project_ops: Dict[str, Any]
                              ) -> Dict[str, List[str]]:














































    by_env: Dict[str, List[Tuple[str, Any]]] = {}
    for op_name, entry in project_ops.items():
        if not isinstance(entry, dict):
            continue
        for key, value in entry.items():
            if not isinstance(key, str) or key in _OP_CONFIG_RESERVED_KEYS:
                continue
            by_env.setdefault(f"SUPERTOOL_{key.upper()}", []).append(
                (op_name, value))
    collisions: Dict[str, List[str]] = {}
    for var_name, pairs in by_env.items():
        ops_here = sorted({op for op, _ in pairs})
        if len(ops_here) < 2:
            continue
        fingerprints = set()
        for _, value in pairs:
            try:
                fingerprints.add(json.dumps(value, sort_keys=True))
            except TypeError:
                fingerprints.add(repr(value))
        if len(fingerprints) > 1:
            collisions[var_name] = ops_here
    return collisions


def _merge_presets(config: Dict[str, Any], project_dir: str) -> None:

    presets = config.get("presets")
    project_ops = config.get("ops", {})
    if not isinstance(project_ops, dict):
        project_ops = {}



    config["_op_config_collisions"] = _op_config_key_collisions(project_ops)
    if presets is not None and not isinstance(presets, list):



        config.setdefault("_preset_warnings", []).append(
            f'"presets" must be a list of preset names, got '
            f"{type(presets).__name__} — no preset ops were merged")
        _record_op_sources(config, {}, {}, project_ops, project_ops)
        return
    if not presets:



        _record_op_sources(config, {}, {}, project_ops, project_ops)
        return

    merged_ops: Dict[str, Any] = {}
    op_presets: Dict[str, str] = {}

    for name in presets:
        if not isinstance(name, str):
            continue
        preset_path = _find_preset_file(name, project_dir)
        if preset_path is None:





            if name in _DIRECTORY_BUILD_EXCLUDED_PRESETS:
                config.setdefault("_preset_warnings", []).append(
                    f"preset {name!r} is not in this build (directory "
                    f"install); install supertool-cli@dpt-plugins for it"
                )
            else:
                config.setdefault("_preset_warnings", []).append(
                    f"preset {name!r} not found"
                )
            continue
        try:
            with open(preset_path, encoding="utf-8") as f:
                preset_data = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:






            config.setdefault("_preset_warnings", []).append(
                f"preset {name!r}: failed to load {preset_path} "
                f"({exc.__class__.__name__}: {exc})"
            )
            continue

        preset_dir = os.path.dirname(preset_path)


















        preset_builtin_docs = preset_data.get("builtin-ops")
        if preset_builtin_docs is not None and not isinstance(
                preset_builtin_docs, dict):







            config.setdefault("_preset_warnings", []).append(
                f"preset {name!r}: builtin-ops is "
                f"{type(preset_builtin_docs).__name__}, not a table "
                f"— the whole section was dropped"
            )
        if isinstance(preset_builtin_docs, dict):
            merged_builtin_docs = dict(config.get("builtin-ops") or {})
            for doc_name, doc_def in preset_builtin_docs.items():
                if not isinstance(doc_name, str):
                    continue







                project_entry = (config.get("builtin-ops") or {}).get(doc_name)
                merged_entry = (
                    _merge_op_def(doc_def, project_entry)
                    if project_entry is not None else doc_def)
                merged_builtin_docs[doc_name] = merged_entry
                if not isinstance(merged_entry, dict):









                    config.setdefault("_preset_warnings", []).append(
                        f"preset {name!r}: builtin-ops.{doc_name} is "
                        f"{type(merged_entry).__name__}, not a table "
                        f"— every runtime reader will fall back to its "
                        f"default as if this key were never set"
                    )







                doc_sources = config.setdefault("_preset_doc_contributions", {})
                doc_sources.setdefault(name, []).append(doc_name)
            config["builtin-ops"] = merged_builtin_docs

        preset_ops = preset_data.get("ops", {})
        for op_name, op_def in preset_ops.items():

            if isinstance(op_def, dict) and "cmd" in op_def:
                op_def = dict(op_def)  
                op_def["cmd"] = _resolve_preset_cmd(op_def["cmd"], preset_dir)
            elif isinstance(op_def, str):
                op_def = _resolve_preset_cmd(op_def, preset_dir)
            merged_ops[op_name] = op_def


            op_presets[op_name] = name




    preset_bases = dict(merged_ops)
    for op_name, op_def in project_ops.items():
        merged_ops[op_name] = _merge_op_def(merged_ops.get(op_name), op_def)
    config["ops"] = merged_ops
    _record_op_sources(config, op_presets, preset_bases, project_ops,
                       merged_ops)


def _config_trust_violation(candidate: str) -> Optional[str]:























    if os.name != "posix":
        return None
    try:
        st = os.stat(candidate)
    except OSError as exc:
        return f"cannot stat: {exc}"
    caller_uid = os.getuid()
    if st.st_uid not in (caller_uid, 0) and caller_uid != 0:
        return (
            f"not owned by the current user (owner uid {st.st_uid}, "
            f"running as uid {caller_uid})"
        )
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"group/world-writable (mode {stat.S_IMODE(st.st_mode):o})"
    return None


def _unread_variable_blocks(config: Dict[str, Any]) -> List[str]:







    found = []
    for section in ("validators", "formatters", "mcp"):
        block = config.get(section)
        if not isinstance(block, dict):
            continue
        for name, spec in block.items():
            if not isinstance(spec, dict) or "variables" in spec:
                continue
            for key, value in spec.items():
                if not isinstance(value, dict) or not value:
                    continue
                if all(isinstance(k, str) and re.fullmatch(r"[A-Z_][A-Z0-9_]*", k)
                       and isinstance(v, (str, int, float)) for k, v in value.items()):
                    found.append(
                        f"{section}.{name}: key {key!r} holds NAME=value pairs that "
                        f"nothing reads -- a spawned tool's variables go under "
                        f'"variables" (renamed in 0.66.0, #2734)')
    return found


def _load_config() -> Dict[str, Any]:



























    global _CONFIG, _CONFIG_CHECKED, _CONFIG_PATH, _mcp_specs
    if _CONFIG_CHECKED:
        return _CONFIG or {}
    _CONFIG_CHECKED = True
    d = os.path.abspath(os.getcwd())
    project_dir = d
    while True:
        candidate = os.path.join(d, ".supertool.json")
        if os.path.isfile(candidate):
            violation = _config_trust_violation(candidate)
            if violation is not None:
                _CONFIG_WARNINGS.append(f"skipped {candidate}: {violation}")
                if os.path.exists(os.path.join(d, ".git")):
                    break
                parent = os.path.dirname(d)
                if parent == d:
                    break
                d = parent
                continue
            try:
                with open(candidate, encoding="utf-8") as f:
                    _CONFIG = json.load(f)



                    if not isinstance(_CONFIG, dict):









                        _CONFIG_WARNINGS.append(
                            f"{candidate} does not hold a JSON object (got "
                            f"{type(_CONFIG).__name__}) — ignoring it"
                        )
                        _CONFIG = {}
                    project_dir = d
                    _CONFIG_PATH = candidate
                    _merge_presets(_CONFIG, project_dir)

                    mcp_block = _CONFIG.get("mcp")
                    if isinstance(mcp_block, dict):
                        for srv_name, spec in mcp_block.items():
                            if isinstance(spec, dict) and "cmd" in spec:
                                _mcp_specs[srv_name] = spec
                    _CONFIG_WARNINGS.extend(_unread_variable_blocks(_CONFIG))
                    return _CONFIG
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:








                _CONFIG_WARNINGS.append(
                    f"skipped {candidate}: {exc.__class__.__name__}: {exc}"
                )
        if os.path.exists(os.path.join(d, ".git")):
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    _CONFIG = {}
    return _CONFIG


_MIXED_TREE_ENV = "SUPERTOOL_ALLOW_MIXED_TREE"


def _mixed_tree_pair() -> Optional[Tuple[str, str]]:





























    _load_config()
    if not _CONFIG_PATH:
        return None
    project_dir = os.path.dirname(os.path.realpath(_CONFIG_PATH))
    peer = os.path.join(project_dir, "supertool.py")
    try:
        if not os.path.isfile(peer):
            return None
        if os.path.dirname(os.path.realpath(peer)).replace(os.sep, "/") == _INSTALL_DIR:
            return None
    except OSError:
        return None
    return (_INSTALL_DIR, project_dir)


def _mixed_tree_allowed() -> bool:



    return (os.environ.get("SUPERTOOL_ALLOW_MIXED_TREE") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _mixed_tree_note(pair: Tuple[str, str]) -> str:

    core, other = pair
    return f"mixed supertool trees: core={core}/supertool.py presets={other}"


def _mixed_tree_decline(op: str, pair: Tuple[str, str]) -> str:


























    core, other = pair
    if _op_safety_class(op) == "read-only":
        fix = (
            f"Fix: run from {core}, or make the first op 'cwd:{core}'. To mix on "
            f"purpose, set {_MIXED_TREE_ENV}=1 — the receipt then carries the "
            f"pairing instead of a bare PASS.\n"
        )
    else:
        fix = (
            f"Fix: '{op}' is not read-only -- run it from INSIDE {other} with "
            f"'python3 supertool.py {op}...' instead. Do NOT use 'cwd:{core}' "
            f"here: that would silently retarget the write at {core}'s own "
            f"checked-out branch, not {other}'s (#2432). To mix on purpose "
            f"anyway, set {_MIXED_TREE_ENV}=1 — the receipt then carries the "
            f"pairing instead of a bare PASS.\n"
        )
    return (
        f"SKIPPED: '{op}' comes from a different supertool tree than the core "
        f"that is running.\n"
        f"  core:    {core}/supertool.py (the file you invoked)\n"
        f"  presets: {other} (resolved from this cwd — its .supertool.json and "
        f"presets/ would answer)\n"
        f"Declined rather than PASSing for a build the tool cannot name: the "
        f"code you meant to exercise would not have run, and the answer would "
        f"have looked exactly like a correct one (#678).\n"
        + fix
    )


def _is_compact() -> bool:

    return bool(_load_config().get("compact", False))


def _notifier_debug_enabled() -> bool:

    override = os.environ.get("SUPERTOOL_NOTIFIER_DEBUG")
    if override is not None:
        return override.strip().lower() in ("1", "true", "yes", "on")
    return bool(_load_config().get("notifier_debug", False))


def _notifier_debug_log_path() -> str:

    return os.environ.get("SUPERTOOL_NOTIFIER_DEBUG_LOG") or "/tmp/supertool-notifier-debug.log"


def plain_mode() -> bool:











    return os.environ.get("SUPERTOOL_PLAIN", "").strip().lower() in (
        "1", "true", "yes", "on"
    )





_PLAIN_MARKERS = {
    "⚠": "[WARN]",  
    "✓": "[OK]",    
    "✗": "[FAIL]",  
    "ℹ": "[INFO]",  
    "↳": "->",      
}


def mark(glyph: str) -> str:





    if plain_mode():
        return _PLAIN_MARKERS.get(glyph, glyph)
    return glyph


def _reconfigure_stdout_utf8() -> None:








    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass











_REPLACEMENT_CHAR = "\ufffd"


def _undecodable_at(text: str) -> int:







    return text.find(_REPLACEMENT_CHAR)


def _display_safe(text: str) -> str:















    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        pass
    try:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:


        return text.encode("utf-8", "backslashreplace").decode("utf-8")












_LINE_BREAK_PATTERN = r"\r\n|\r|\n"
_LINE_BREAK_RE_STR = re.compile(_LINE_BREAK_PATTERN)
_LINE_BREAK_RE_BYTES = re.compile(_LINE_BREAK_PATTERN.encode("ascii"))







_AMBIGUOUS_LINE_BREAKS = tuple(chr(c) for c in (
    0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029))


def _split_lines_keepends(data: Any) -> Any:













    if isinstance(data, bytes):
        return data.splitlines(keepends=True)
    rx = _LINE_BREAK_RE_STR
    out = []
    pos = 0
    for m in rx.finditer(data):
        out.append(data[pos:m.end()])
        pos = m.end()
    if pos < len(data):
        out.append(data[pos:])
    return out


def _split_lines(data: Any) -> Any:

    keep = _split_lines_keepends(data)
    if isinstance(data, bytes):
        return [ln.rstrip(b"\r\n") for ln in keep]
    return [ln.rstrip("\r\n") for ln in keep]


def _line_break_ambiguity_note(data: Any) -> str:







    if isinstance(data, bytes):
        present = [c for c in _AMBIGUOUS_LINE_BREAKS
                   if c.encode("utf-8") in data]
    else:
        present = [c for c in _AMBIGUOUS_LINE_BREAKS if c in data]
    if not present:
        return ""
    names = ", ".join(f"U+{ord(c):04X}" for c in present)
    return (f"note: contains {names} — supertool numbers lines by LF / CRLF / "
            f"CR only, so a tool that also breaks on these (Python's "
            f"str.splitlines, some editors) numbers this file differently. "
            f"supertool's reads and its line-addressed edits agree with each "
            f"other.\n")


def _notifier_log(msg: str) -> None:

    if not _notifier_debug_enabled():
        return
    try:
        with open(_notifier_debug_log_path(), "a", encoding="utf-8") as f:
            ts = datetime.now().isoformat(timespec="milliseconds")
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass


def _parallel_workers() -> int:







    override = os.environ.get("SUPERTOOL_PARALLEL")
    raw: object = override if override is not None else _load_config().get("parallel", 0)
    if isinstance(raw, bool):
        return 4 if raw else 0
    if isinstance(raw, int):
        return max(0, raw)
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("true", "yes", "on"):
            return 4
        if s in ("false", "no", "off", ""):
            return 0





        try:
            n = int(s)
        except ValueError:
            if override is not None:
                _env_notice(f"note: SUPERTOOL_PARALLEL={raw!r} is not a whole number "
                            f"or true/false - ignoring it and using 0 (sequential).")
            return 0
        if n < 0:
            if override is not None:
                _env_notice(f"note: SUPERTOOL_PARALLEL={raw!r} is below the minimum of 0 "
                            f"- ignoring it and using 0 (sequential).")
            return 0
        return n
    return 0






_ENV_ANNOUNCED: "set[str]" = set()


def _env_notice(text: str) -> None:






    if text in _ENV_ANNOUNCED:
        return
    _ENV_ANNOUNCED.add(text)
    print(text)
    sys.stdout.flush()


def _env_int(raw: "Optional[str]", name: str, default: int, *,
             minimum: "Optional[int]" = None) -> int:





















    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _env_notice(f"note: {name}={raw!r} is not a whole number "
                    f"- ignoring it and using {default}.")
        return default
    if minimum is not None and value < minimum:
        _env_notice(f"note: {name}={raw!r} is below the minimum of {minimum} "
                    f"- ignoring it and using {default}.")
        return default
    return value


def _env_float(raw: "Optional[str]", name: str, default: float, *,
               minimum: "Optional[float]" = None) -> float:


    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _env_notice(f"note: {name}={raw!r} is not a number "
                    f"- ignoring it and using {default}.")
        return default
    if value != value:  
        _env_notice(f"note: {name}={raw!r} is not a usable number "
                    f"- ignoring it and using {default}.")
        return default
    if minimum is not None and value < minimum:
        _env_notice(f"note: {name}={raw!r} is below the minimum of {minimum} "
                    f"- ignoring it and using {default}.")
        return default
    return value












_OP_ENV_OVERRIDES = {
    ("around", "max_bytes"): lambda: os.environ.get("SUPERTOOL_AROUND_MAX_BYTES"),
    ("batch", "max_ops"): lambda: os.environ.get("SUPERTOOL_BATCH_MAX_OPS"),
    ("glob", "max_results"): lambda: os.environ.get("SUPERTOOL_GLOB_MAX_RESULTS"),
    ("grep", "count_ceiling"): lambda: os.environ.get("SUPERTOOL_GREP_COUNT_CEILING"),
    ("grep", "count_truncated"): lambda: os.environ.get("SUPERTOOL_GREP_COUNT_TRUNCATED"),
    ("grep", "max_line_chars"): lambda: os.environ.get("SUPERTOOL_GREP_MAX_LINE_CHARS"),
    ("grep", "max_results"): lambda: os.environ.get("SUPERTOOL_GREP_MAX_RESULTS"),
    ("grep_around", "max_bytes"): lambda: os.environ.get("SUPERTOOL_GREP_AROUND_MAX_BYTES"),
    ("head", "char_window"): lambda: os.environ.get("SUPERTOOL_HEAD_CHAR_WINDOW"),
    ("read", "abstract"): lambda: os.environ.get("SUPERTOOL_READ_ABSTRACT"),
    ("read", "abstract_threshold_bytes"): lambda: os.environ.get("SUPERTOOL_READ_ABSTRACT_THRESHOLD_BYTES"),
    ("read", "elide"): lambda: os.environ.get("SUPERTOOL_READ_ELIDE"),
    ("read", "elide_window_seconds"): lambda: os.environ.get("SUPERTOOL_READ_ELIDE_WINDOW_SECONDS"),
    ("read", "git_timeout_seconds"): lambda: os.environ.get("SUPERTOOL_READ_GIT_TIMEOUT_SECONDS"),
    ("read", "max_autoread_lines"): lambda: os.environ.get("SUPERTOOL_READ_MAX_AUTOREAD_LINES"),
    ("read", "max_bytes"): lambda: os.environ.get("SUPERTOOL_READ_MAX_BYTES"),
    ("read", "max_lines"): lambda: os.environ.get("SUPERTOOL_READ_MAX_LINES"),
    ("read", "php_abstract"): lambda: os.environ.get("SUPERTOOL_READ_PHP_ABSTRACT"),
    ("tail", "char_window"): lambda: os.environ.get("SUPERTOOL_TAIL_CHAR_WINDOW"),
}


def _op_env_override(op_name: str, key: str) -> "tuple[str, Optional[str]]":





    env_key = f"SUPERTOOL_{op_name.upper()}_{key.upper()}"
    reader = _OP_ENV_OVERRIDES.get((op_name, key))
    if reader is None:
        raise KeyError(
            f"no literal reader for {env_key} -- add ({op_name!r}, {key!r}) "
            f"to _OP_ENV_OVERRIDES (#2734)")
    return env_key, reader()


def _get_op_int(op_name: str, key: str, default: int) -> int:



















    env_key, env_val = _op_env_override(op_name, key)
    cfg = _load_config()
    op_cfg = cfg.get("builtin-ops", {}).get(op_name, {})








    val = op_cfg.get(key) if isinstance(op_cfg, dict) else None
    fallback = default
    if val is not None:














        if isinstance(val, int) and not isinstance(val, bool) and val > 0:
            fallback = val
        else:
            _env_notice(
                f"note: builtin-ops.{op_name}.{key}={val!r} is not a positive "
                f"whole number - ignoring it and using {default}. "
                f"({op_name}.{key} is a threshold; 0 is not an off switch.)")
    if env_val:
        try:
            n = int(env_val)
        except ValueError:
            _env_notice(f"note: {env_key}={env_val!r} is not a whole number "
                        f"- ignoring it and using {fallback}.")
            return fallback
        if n > 0:
            return n
        _env_notice(f"note: {env_key}={env_val!r} is below the minimum of 1 "
                    f"- ignoring it and using {fallback}.")
    return fallback




_BOOL_TRUE = ("1", "true", "yes", "on")
_BOOL_FALSE = ("0", "false", "no", "off")


def _coerce_bool(raw: Any) -> "Optional[bool]":






    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return raw != 0
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in _BOOL_TRUE:
            return True
        if s in _BOOL_FALSE:
            return False
    return None


def _get_op_bool(op_name: str, key: str, default: bool) -> bool:














    env_key, env_val = _op_env_override(op_name, key)
    cfg = _load_config()
    op_cfg = cfg.get("builtin-ops", {}).get(op_name, {})
    val = op_cfg.get(key) if isinstance(op_cfg, dict) else None
    fallback = default
    if val is not None:
        coerced = _coerce_bool(val)
        if coerced is None:
            _env_notice(f"note: builtin-ops.{op_name}.{key}={val!r} is not a "
                        f"true/false value - ignoring it and using {default}.")
        else:
            fallback = coerced
    if env_val:
        coerced = _coerce_bool(env_val)
        if coerced is None:
            _env_notice(f"note: {env_key}={env_val!r} is not a true/false value "
                        f"- ignoring it and using {fallback}.")
        else:
            return coerced
    return fallback


def _grep_file_includes() -> Tuple[str, ...] | None:






    global _GREP_EXTENSIONS_EFFECTIVE
    if _GREP_EXTENSIONS_EFFECTIVE is not None:
        return _GREP_EXTENSIONS_EFFECTIVE if _GREP_EXTENSIONS_EFFECTIVE != ("*",) else None
    cfg = _load_config()
    builtin_ops = cfg.get("builtin-ops", {})
    op_cfg = builtin_ops.get("grep", {})



    exts = op_cfg.get("extensions", []) if isinstance(op_cfg, dict) else []
    if exts and isinstance(exts, list):
        valid = tuple(sorted(e for e in exts if isinstance(e, str) and e.startswith("*.")))
        if valid:
            _GREP_EXTENSIONS_EFFECTIVE = valid
            return valid

    _GREP_EXTENSIONS_EFFECTIVE = ("*",)  
    return None


def _get_exclude_paths(op_name: str, no_exclude: bool = False) -> Tuple[str, ...]:






    if no_exclude:
        return ()
    defaults = set(_DEFAULT_EXCLUDE_PATHS)
    cfg = _load_config()
    project_paths = cfg.get("ops", {}).get(op_name, {})
    if isinstance(project_paths, dict):
        extra = project_paths.get("exclude-paths", [])
        if isinstance(extra, list):
            for p in extra:
                if isinstance(p, str) and p:
                    defaults.add(_normalise_exclude_entry(p))
    return tuple(sorted(defaults))


def _normalise_exclude_entry(entry: str) -> str:








    if entry.startswith("!") or WILDCARD_CHARS.search(entry):
        return entry
    return entry if entry.endswith("/") else entry + "/"


def _is_excluded(rel_path: str, exclude_paths: Tuple[str, ...]) -> bool:




























    if not exclude_paths:
        return False
    import fnmatch

    normalised = rel_path.replace(os.sep, "/")

    if normalised.startswith("./"):
        normalised = normalised[2:]
    bare_path = normalised.rstrip("/")
    if not normalised.endswith("/"):
        normalised += "/"
    basename = bare_path.rsplit("/", 1)[-1]

    components = {c for c in bare_path.split("/") if c}

    def _glob_hit(pattern: str) -> bool:
        return (fnmatch.fnmatch(basename, pattern)
                or fnmatch.fnmatch(bare_path, pattern))




    for entry in exclude_paths:
        if not entry.startswith("!"):
            continue
        pattern = entry[1:].rstrip("/")
        if not pattern:
            continue
        if WILDCARD_CHARS.search(pattern):
            if _glob_hit(pattern):
                return False
        elif pattern == basename or normalised.startswith(pattern + "/"):
            return False

    for entry in exclude_paths:
        if entry.startswith("!"):
            continue
        if WILDCARD_CHARS.search(entry):
            if _glob_hit(entry.rstrip("/")):
                return True
            continue
        if normalised.startswith(entry):
            return True

        bare = entry.rstrip("/")
        if "/" not in bare and bare in components:
            return True
    return False


def _is_disclosable_exclusion(
    rel_path: str, exclude_paths: Tuple[str, ...]
) -> bool:























    signal = tuple(p for p in exclude_paths if p not in _NOISE_EXCLUDE_SET)  
    return bool(signal) and _is_excluded(rel_path, signal)


def _split_exclude_prefixes(
    exclude_paths: Tuple[str, ...],
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:









    singles: List[str] = []
    multis: List[str] = []
    for p in exclude_paths:
        trimmed = p.rstrip("/")
        if "/" in trimmed:
            multis.append(trimmed)
        else:
            singles.append(trimmed)
    return tuple(singles), tuple(multis)


def _grep_exclude_flags(exclude_paths: Tuple[str, ...]) -> List[str]:








































    has_negation = any(p.startswith("!") for p in exclude_paths)
    negated = {p[1:].rstrip("/") for p in exclude_paths if p.startswith("!")}
    flags: List[str] = []
    for entry in exclude_paths:
        if entry.startswith("!"):
            continue
        bare = entry.rstrip("/")
        if not bare or "/" in bare or bare in negated:
            continue
        if WILDCARD_CHARS.search(bare) and has_negation:
            continue
        flags.append(f"--exclude-dir={bare}")
        if entry in _NOISE_EXCLUDE_SET:
            flags.append(f"--exclude={bare}")
    return flags


def _rtk_drop_excluded(
    rtk_out: str, exclude_paths: Tuple[str, ...]
) -> Tuple[str, int]:










    if not exclude_paths:
        return rtk_out, 0
    cwd = os.getcwd()
    kept: List[str] = []
    dropped: set = set()
    for line in rtk_out.splitlines():
        m = re.match(r"^(.+?):\d+:", line)
        if m and _is_excluded(_safe_relpath(m.group(1), cwd), exclude_paths):
            dropped.add(m.group(1))
            continue
        kept.append(line)
    return "\n".join(kept) + ("\n" if kept else ""), len(dropped)





_GIT_IGNORED_CACHE: Dict[Tuple[str, str], Any] = {}
_GIT_IGNORE_TIMEOUT = 10


class _GitIgnoreView(NamedTuple):







    dirs: frozenset
    files: frozenset
    unavailable: str


_GIT_IGNORE_NONE = _GitIgnoreView(frozenset(), frozenset(), "")


def _gitignore_enabled() -> bool:






    if os.environ.get("SUPERTOOL_NO_GITIGNORE") == "1":
        return False
    return bool(_load_config().get("gitignore", True))


def _git_ignore_view(root: str) -> _GitIgnoreView:
























    if not _gitignore_enabled() or not os.path.isdir(root):
        return _GIT_IGNORE_NONE
    cwd = os.getcwd()
    key = (cwd, os.path.normpath(root))
    cached = _GIT_IGNORED_CACHE.get(key)
    if cached is None:
        cached = _compute_git_ignore_view(root, cwd)
        _GIT_IGNORED_CACHE[key] = cached
    return cached


def _git_ignored_dirs(root: str) -> frozenset:

    return _git_ignore_view(root).dirs


def _run_git_ignore_query(root: str, args: List[str]) -> Any:

    try:
        return subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True, timeout=_GIT_IGNORE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _compute_git_ignore_view(root: str, cwd: str) -> _GitIgnoreView:




    probe = _run_git_ignore_query(root, ["check-ignore", "-q", "--", os.path.abspath(root)])
    if probe is None:
        return _GitIgnoreView(frozenset(), frozenset(), "git could not be run")
    if probe.returncode == 0:
        return _GIT_IGNORE_NONE
    if probe.returncode != 1:
        err = (probe.stderr or b"").decode("utf-8", "replace").lower()
        why = ("not a git repository" if "not a git repository" in err
               else f"git check-ignore exited {probe.returncode}")
        return _GitIgnoreView(frozenset(), frozenset(), why)
    listing = _run_git_ignore_query(root, [
        "ls-files", "-z", "--others", "--ignored", "--exclude-standard",
        "--directory", "--no-empty-directory",
    ])
    if listing is None or listing.returncode != 0:
        return _GitIgnoreView(frozenset(), frozenset(), "git ls-files failed")
    dirs = set()
    files = set()
    for entry in listing.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not entry:
            continue


        rel = _strip_dot_slash(
            _safe_relpath(os.path.normpath(os.path.join(root, entry)), cwd)
        )
        if not rel or rel == "." or rel.startswith(".."):
            continue
        (dirs if entry.endswith("/") else files).add(rel)
    return _GitIgnoreView(frozenset(dirs), frozenset(files), "")


class _GitIgnoreTally:



    def __init__(self) -> None:
        self.hidden: List[str] = []
        self.unavailable: List[str] = []

    def saw(self, view: _GitIgnoreView) -> None:
        if view.unavailable and view.unavailable not in self.unavailable:
            self.unavailable.append(view.unavailable)

    def clause(self) -> str:



        parts = []
        n = len(self.hidden)
        if n:


            parts.append(f", {n} gitignored files hidden")
        if self.unavailable:
            parts.append(", gitignore filter not applied ("
                         + "; ".join(self.unavailable) + ")")
        return "".join(parts)


def _is_git_ignored_file(rel_path: str, view: _GitIgnoreView) -> bool:

    if not view.files:
        return False
    return _strip_dot_slash(rel_path) in view.files


def _strip_dot_slash(path: str) -> str:

    rel = path.replace(os.sep, "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _is_git_ignored(rel_root: str, name: str, ignored: frozenset) -> bool:

    if not ignored:
        return False
    return _strip_dot_slash(os.path.join(rel_root, name)) in ignored


def _under_git_ignored(rel_path: str, ignored: frozenset) -> bool:




    if not ignored:
        return False
    rel = _strip_dot_slash(rel_path)
    return any(rel == d or rel.startswith(d + "/") for d in ignored)


def _gitignore_residual(path: str, exclude_paths: Tuple[str, ...]) -> bool:










    if not exclude_paths:
        return False
    view = _git_ignore_view(path)







    if view.unavailable and view.unavailable != "not a git repository":
        return True
    return any(
        not _is_excluded(rel, exclude_paths)
        for rel in (*view.dirs, *view.files)
    )


def _rtk_enabled() -> bool:

    return bool(_load_config().get("rtk", True))



_RTK_PATH: str | None = None
_RTK_CHECKED = False


def _has_rtk() -> str | None:






    global _RTK_PATH, _RTK_CHECKED
    if not _RTK_CHECKED:
        _RTK_CHECKED = True
        if os.environ.get("SUPERTOOL_NO_RTK") == "1":
            _RTK_PATH = None
        else:






            _RTK_PATH = _which_excluding_cwd("rtk")
    return _RTK_PATH


def _rtk_run(args: List[str], timeout: int = 30) -> str | None:

    rtk = _has_rtk()
    if not rtk:
        return None
    try:
        result = subprocess.run(
            [rtk] + args, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace"
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None






































_RTK_ELISION_MARKER_RE = re.compile(
    r"(?m)^\s*\d+\s*" + "│"
    + r"\s*//\s*\.\.\.\s*\d+\s+(?:lines omitted|more lines(?:\s*\(total:\s*\d+\))?)\s*$")


def _rtk_output_looks_malformed(text: str, *, aggressive: bool = False) -> bool:



















    if aggressive:
        return False
    return len(_RTK_ELISION_MARKER_RE.findall(text)) >= 2
