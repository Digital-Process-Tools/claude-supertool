






































from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_catalog.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )

def _onboarding_text(config_key: str, default: str, *,
                     env_value: "Optional[str]") -> str:















    config = _load_config()
    raw = env_value
    if raw is None:
        raw = config.get(config_key)
    if raw is None:
        return default
    val = str(raw).strip()
    if val.lower() in _ONBOARDING_DISABLE_VALUES:
        return ""
    return val


def op_introduction() -> str:


    intro = _onboarding_text(
        "introduction", _DEFAULT_INTRODUCTION,
        env_value=os.environ.get("SUPERTOOL_INTRODUCTION"))
    if not intro:
        return "No introduction configured in .supertool.json\n"
    return str(intro) + "\n\n"


def op_output_format() -> str:


    fmt = _onboarding_text(
        "output-format", _DEFAULT_OUTPUT_FORMAT,
        env_value=os.environ.get("SUPERTOOL_OUTPUT_FORMAT"))
    if not fmt:
        return "No output-format configured in .supertool.json\n"
    return str(fmt) + "\n\n"


def op_version() -> str:

    return f"supertool {VERSION}\n"


_SHIPPED_CONFIG: Optional[Dict[str, Any]] = None





_SHIPPED_CONFIG_STATE: Optional[str] = None





_SHIPPED_CONFIG_DIR: Optional[str] = None


def _shipped_config() -> Dict[str, Any]:















    global _SHIPPED_CONFIG, _SHIPPED_CONFIG_STATE
    if _SHIPPED_CONFIG is None:
        directory = _SHIPPED_CONFIG_DIR or os.path.dirname(
            os.path.abspath(__file__))
        path = os.path.join(directory, ".supertool.json")
        data: Any = None










        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:



            _SHIPPED_CONFIG_STATE = "absent"
        except (OSError, ValueError):







            data = None
            _SHIPPED_CONFIG_STATE = "unreadable"
        else:
            _SHIPPED_CONFIG_STATE = "read" if isinstance(data, dict) else "unreadable"
        if _SHIPPED_CONFIG_STATE == "absent":





















            reference_path = os.path.join(directory, "_shipped_reference.py")
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    "_shipped_reference", reference_path)
                if spec is not None and spec.loader is not None:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    fallback = getattr(module, "BUILTIN_OPS", None)
                    if isinstance(fallback, dict):
                        data = {"builtin-ops": fallback}
                        _SHIPPED_CONFIG_STATE = "read"
                    else:




                        _SHIPPED_CONFIG_STATE = "unreadable"
            except FileNotFoundError:



                pass
            except (OSError, ImportError, SyntaxError, ValueError):








                _SHIPPED_CONFIG_STATE = "unreadable"
        _SHIPPED_CONFIG = data if isinstance(data, dict) else {}
        _fold_shipped_preset_docs(_SHIPPED_CONFIG, directory)
    return _SHIPPED_CONFIG


def _fold_shipped_preset_docs(shipped: Dict[str, Any], directory: str) -> None:


















    preset_dir = os.path.join(directory, "presets")
    try:
        entries = sorted(os.listdir(preset_dir))
    except OSError:
        return
    folded = dict(shipped.get("builtin-ops") or {})
    for fname in entries:
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(preset_dir, fname), encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        docs = data.get("builtin-ops")
        if not isinstance(docs, dict):
            continue
        for name, entry in docs.items():
            if isinstance(name, str) and name not in folded:
                folded[name] = entry
    if folded:
        shipped["builtin-ops"] = folded


def _shipped_reference_path() -> str:






    directory = _SHIPPED_CONFIG_DIR or os.path.dirname(
        os.path.abspath(__file__))
    return os.path.join(directory, ".supertool.json")


def _help_entry(config: Dict[str, Any],
                op_name: str) -> Optional[Dict[str, Any]]:

    for section in ("builtin-ops", "ops", "aliases"):
        entry = config.get(section, {})
        if not isinstance(entry, dict) or op_name not in entry:
            continue
        info = entry[op_name]
        if isinstance(info, dict):
            return info
    return None


def op_help(op_name: str) -> str:












    if not op_name:
        return ("ERROR: help needs an op name — help:OP (e.g. help:vim).\n"
                "Run 'ops' for the full list.\n")
    config = _load_config()
    info = _help_entry(config, op_name)
    from_shipped = False
    if info is None:
        info = _help_entry(_shipped_config(), op_name)
        from_shipped = info is not None
    if info is not None:
        out: List[str] = [str(info.get("syntax", op_name))]
        desc = info.get("description", "")
        if desc:
            out.append("")
            out.append(str(desc))
        form_parent = info.get("form")
        if form_parent:




            out.append("")
            out.append(f"Declared form of `{form_parent}` (#1245) — not an "
                       f"independent op name; `registry:{form_parent}` is "
                       f"the registry entry.")
        ops_list = info.get("ops", [])
        if ops_list:
            out.append("")
            out.append("Ops: " + " ".join(str(o) for o in ops_list))
        example = info.get("example", "")
        if example:
            out.append("")
            out.append(f"Example: {example}")
        route = _help_payload_route(op_name)
        if route:
            out.append(route)
        if from_shipped:
            out.append("")
            out.append(f"(From supertool's shipped reference — nothing in this "
                       f"project's config documents '{op_name}'. A project "
                       f"entry of its own would override this one.)")
        return "\n".join(out) + "\n"
    if op_name in _valid_op_names():






        _shipped_config()
        if _SHIPPED_CONFIG_STATE == "unreadable":
            shipped = (f"  A reference does ship beside this binary and it "
                       f"could NOT be read, so whether it documents "
                       f"'{op_name}' is UNKNOWN — this is not a report that it "
                       f"does not. Check the file's permissions and that it is "
                       f"a JSON object: {_shipped_reference_path()}\n")
        elif _SHIPPED_CONFIG_STATE == "absent":
            shipped = (f"  No reference shipped beside this binary — "
                       f"{_shipped_reference_path()} is not there, which is an "
                       f"incomplete install rather than an undocumented op.\n")
        elif _SHIPPED_CONFIG_STATE == "read":
            shipped = ("  The reference shipped beside this binary was read "
                       "and does not document it either.\n")
        else:







            shipped = (f"  Whether a reference ships beside this binary is "
                       f"UNKNOWN — the internal lookup returned "
                       f"{_SHIPPED_CONFIG_STATE!r}, not one of the states "
                       f"this code expects, so nothing can be asserted about "
                       f"'{op_name}'.\n")
        return (f"ERROR: op '{op_name}' has no documented help in this "
                f"project's config.\n"
                + shipped
                + "  It is a valid operation — `ops:roster` lists every name "
                "loaded here, and the op's own error teaches its "
                "signature.\n")
    return (f"ERROR: no help for op: {op_name}\n"
            f"Run 'ops' for the full list of operations.\n")




















































_HOOK_OUTPUT_CAP_BYTES = 10000


def _over_hook_cap(payload: str) -> bool:







    return len(payload.encode("utf-8")) > _HOOK_OUTPUT_CAP_BYTES


def _configured_op_names(config: Dict[str, Any]) -> set:













    names: set = set()
    for section in ("builtin-ops", "ops", "aliases"):
        entries = config.get(section, {})
        if not isinstance(entries, dict):
            continue
        for name, info in entries.items():
            if isinstance(info, dict) and not info.get("form"):
                names.add(name)
    return names


def op_ops(compact: bool = False, full: bool = False) -> str:






























    config = _load_config()
    builtin_ops = config.get("builtin-ops", {})
    custom_ops = config.get("ops", {})
    alias_defs = config.get("aliases", {})
    lines: List[str] = []

    if not builtin_ops and not custom_ops and not alias_defs:

        lines.append("No descriptions configured in .supertool.json")
        lines.append("")
        lines.append("Built-in operations: " + ", ".join(_valid_op_names()))
        disclosure = _preset_disclosure()
        if disclosure:
            lines.append("")
            lines.append(disclosure)
        lines.append("")
        lines.append("Add a \"builtin-ops\" section to .supertool.json to describe them.")
        return "\n".join(lines) + "\n"

    def _emit_example(info: dict) -> bool:

        if not info.get("example"):
            return False
        if full:
            return True
        if not compact:


            return False
        return bool(info.get("hint"))

    def _emit_desc(info: dict) -> str:







        desc = info.get("description", "")
        if not desc:
            return ""
        if full:
            return desc
        if not compact:



            return ""
        return desc if info.get("hint") else ""











    disclosure = _preset_disclosure()
    if disclosure and not _CONFIG_PATH:
        lines.append(disclosure)
        lines.append("")


    has_ops = False
    if builtin_ops or custom_ops:
        lines.append(_CLASS_LEGEND)
        lines.append("## Operations\n")
        has_ops = True


















        undocumented = sorted(set(_valid_op_names()) - _configured_op_names(config))
        if undocumented:
            lines.append("Also accepted, no reference in .supertool.json: "
                         + ", ".join(undocumented) + "\n")






    marks = _roster_classes()

    def _row(name: str, syntax: str, desc: str) -> str:
        mark_ = _SAFETY_MARKERS.get(marks.get(name, "acts"), "!")
        head_ = f"- `{syntax}`{(' ' + mark_) if mark_ else ''}"
        return f"{head_} — {desc}" if desc else head_

    if builtin_ops:
        for name, info in builtin_ops.items():
            if not isinstance(info, dict):
                continue
            if not info.get("status", 1):
                continue



            lines.append(_row(info.get("form") or name,
                              info.get("syntax", name), _emit_desc(info)))
            if _emit_example(info):
                lines.append(f"  Example: `{info['example']}`")

    active_custom = {k: v for k, v in custom_ops.items()
                     if isinstance(v, dict) and v.get("status", 1)}
    if active_custom:
        for name, info in active_custom.items():
            lines.append(_row(name, info.get("syntax", f"{name}:PATH"),
                              _emit_desc(info)))
            if _emit_example(info):
                lines.append(f"  Example: `{info['example']}`")

    if has_ops:
        lines.append("")


    active_aliases = {k: v for k, v in alias_defs.items()
                      if isinstance(v, dict) and v.get("status", 1)}
    if active_aliases:
        lines.append("## Aliases (multi-op batches)\n")
        for name, info in active_aliases.items():
            desc = _emit_desc(info)
            syntax = info.get("syntax", f"{name}:PATH")
            lines.append(f"- `{syntax}` — {desc}" if desc else f"- `{syntax}`")
            if _emit_example(info):
                lines.append(f"  Example: `{info['example']}`")
        lines.append("")

    if disclosure and _CONFIG_PATH:
        lines.append(disclosure)
        lines.append("")

    body = "\n".join(lines) + "\n"





    if not compact and not full:
        withheld = len(op_ops(full=True).encode("utf-8"))
        body += (
            f"\nSignatures only — every description above is withheld. "
            f"`ops:full` is the same rows carrying them, and costs "
            f"{withheld} bytes in total.\n"
            f"  One op, in full: `help:OP`.  Every description: `ops:full`.  "
            f"Names plus safety class: `ops:roster`.\n"
        )




    if compact and _over_hook_cap(body):
        warning = (
            f"> {mark('⚠')} Output is {len(body.encode('utf-8'))} bytes, exceeds the "
            f"~{_HOOK_OUTPUT_CAP_BYTES}-byte SessionStart hook cap. The tail "
            f"of this listing will be truncated — ops below the cut-off are "
            f"hidden. Run `./supertool 'ops'` to see the full listing.\n\n"
        )
        body = warning + body

    return body


def _roster_classes() -> Dict[str, str]:






















    config = _load_config()
    builtin_entries = config.get("builtin-ops")
    if not isinstance(builtin_entries, dict):
        builtin_entries = {}
    classes: Dict[str, str] = {}
    for name in _valid_op_names():
        entry = builtin_entries.get(name)
        if isinstance(entry, dict) and not entry.get("status", 1):
            continue
        classes[name] = _OP_SAFETY_BUILTIN.get(name, "acts")
    for section in ("ops", "aliases"):
        entries = config.get(section)
        if not isinstance(entries, dict):
            continue
        for name, info in entries.items():
            if not isinstance(info, dict) or not info.get("status", 1):
                continue
            if name in _OP_SAFETY_BUILTIN:
                continue
            declared = info.get("safety")
            classes[name] = declared if declared in _SAFETY_CLASSES else "acts"
    return classes






_CLASS_LEGEND = (
    "Class: unmarked — read-only, call it blind and its own error teaches the "
    "signature.\n`*` writes files here. `!` reaches outside this tree or "
    "outlives the call — look\nthose up, never probe one. An undeclared class "
    "renders `!`, so a gap is never\nthe quiet answer. One op in full: "
    "`help:OP`. Every description: `ops:full`.\n"
)


_ROSTER_LEGEND = (
    "Every op loaded here, and nothing else — the complete list, which the "
    "descriptive\n`ops` listing stops being once a project has enough ops to "
    "pass the ~10KB\nSessionStart cap. Class is declared, never guessed.\n\n"
    "- unmarked — read-only. Call it blind; its own error teaches the "
    "signature.\n"
    "- `*` — writes files in this tree.\n"
    "- `!` — changes something outside this tree, or starts something that "
    "outlives\nthe call. Look these up; never probe one.\n\n"
    "An op whose class is not declared is shown `!`, so a gap is never the "
    "quiet\nanswer. Full entry for one op: `help:OP` — more than the listing "
    "row carries.\nEvery entry: `ops`.\n\n"







    "Ops are one route to disk, not the only one: the raw-command guard "
    "hooks Bash\nonly, so a harness `Edit`/`Write` writes with no op, no "
    "validator and no\nrollback (#1671)."
)


def op_ops_roster(width: int = 78) -> str:








    classes = _roster_classes()
    tokens = [f"{name}{_SAFETY_MARKERS.get(cls, '!')}"
              for name, cls in sorted(classes.items())]






    disclosure = _preset_disclosure()
    body: List[str] = []
    line = ""
    for token in tokens:
        candidate = f"{line} {token}" if line else token
        if line and len(candidate) + 2 > width:
            body.append(f"  {line}")
            line = token
        else:
            line = candidate
    if line:
        body.append(f"  {line}")
    head = "## Ops\n\n" + _ROSTER_LEGEND + "\n"
    if disclosure:
        head += "\n" + disclosure + "\n"
    return head + "\n" + "\n".join(body) + "\n"


def op_ops_session() -> str:























    signatures = op_ops()
    if not _over_hook_cap(signatures):
        return signatures
    size = len(signatures.encode("utf-8"))
    note = (
        f"> Signatures withheld: `ops` renders {size} bytes here, over the "
        f"{_HOOK_OUTPUT_CAP_BYTES}-byte SessionStart hook cap, and a payload "
        f"over it is written to disk with only a preview injected — so the "
        f"listing would arrive looking complete. Names and classes below "
        f"instead; run `ops` for the signatures.\n\n"
    )
    return note + op_ops_roster()


def _ops_argument_refusal(arg: str, op_name: str = "ops") -> str:















    if arg in _roster_classes():
        return (f"ERROR: `{op_name}` takes no filter, and '{arg}' is an op "
                f"name.\n"
                f"  Its full entry: `help:{arg}` — more than the listing row "
                f"carries.\n"
                f"  Every name plus its safety class: `ops:roster`. "
                f"Every signature: `ops`. Every description: `ops:full`.\n")
    return (f"ERROR: unknown argument to `{op_name}`: '{arg}'.\n"
            f"  Accepted: `ops` (every signature), `ops:full` (every "
            f"signature plus its description), `ops:roster` (every name plus "
            f"its safety class), `ops-compact` (the capped listing), "
            f"`ops:grep=PATTERN` (rows whose name, syntax or description "
            f"match PATTERN).\n"
            f"  '{arg}' is also not an op name loaded here — `ops:roster` "
            f"lists the ones that are.\n")


def op_ops_filter(pattern: str) -> str:




































    effective, refusal, note = _pattern_gate(pattern, check_saturation=False)
    if refusal:
        return refusal
    try:
        rx = re.compile(effective)
    except re.error as exc:
        return f"ERROR: invalid pattern `{pattern}`: {exc}\n"

    config = _load_config()
    builtin_ops = config.get("builtin-ops", {})
    custom_ops = config.get("ops", {})
    alias_defs = config.get("aliases", {})

    if not builtin_ops and not custom_ops and not alias_defs:




        all_names = sorted(_valid_op_names())
        names = [n for n in all_names if rx.search(n)]
        lines = [f"## Ops matching `{pattern}`\n"]
        if note:
            lines.append(note)
        lines.append(
            f"{len(names)} of {len(all_names)} ops matched `{pattern}` "
            f"(names only — no .supertool.json found here, so no syntax or "
            f"description to search).\n")
        if names:
            lines.append("  " + ", ".join(names))
        return "\n".join(lines) + "\n"

    marks = _roster_classes()

    def _row(name: str, syntax: str, desc: str) -> str:
        mark_ = _SAFETY_MARKERS.get(marks.get(name, "acts"), "!")
        head_ = f"- `{syntax}`{(' ' + mark_) if mark_ else ''}"
        return f"{head_} — {desc}" if desc else head_

    entries: List[Tuple[str, str, str]] = []
    for name, info in builtin_ops.items():
        if not isinstance(info, dict) or not info.get("status", 1):
            continue
        entries.append((info.get("form") or name,
                        info.get("syntax", name), info.get("description", "")))
    for name, info in custom_ops.items():
        if not isinstance(info, dict) or not info.get("status", 1):
            continue
        entries.append((name, info.get("syntax", f"{name}:PATH"),
                        info.get("description", "")))
    for name, info in alias_defs.items():
        if not isinstance(info, dict) or not info.get("status", 1):
            continue
        entries.append((name, info.get("syntax", f"{name}:PATH"),
                        info.get("description", "")))




    documented = {n for n, _, _ in entries}
    for name in sorted(set(_valid_op_names()) - documented):
        entries.append((name, name, ""))

    total = len(entries)
    matched = [(n, s, d) for n, s, d in entries
              if rx.search(n) or rx.search(s) or rx.search(d)]
    matched.sort(key=lambda t: t[0])

    lines = [f"## Ops matching `{pattern}`\n"]
    if note:
        lines.append(note)
    lines.append(f"{len(matched)} of {total} ops matched `{pattern}`.\n")
    if matched:
        lines.append(_CLASS_LEGEND)
        for name, syntax, desc in matched:
            lines.append(_row(name, syntax, desc))
    return "\n".join(lines) + "\n"


class OpOrigin(NamedTuple):






    name: str
    definition: Any
    preset: str | None
    project: bool
    overridden: Tuple[str, ...] | None


def _op_registry(config: Dict[str, Any] | None = None
                 ) -> Tuple[List[OpOrigin], List[str]]:






















    if config is None:
        config = _load_config()
    ops = config.get("ops")
    if not isinstance(ops, dict):
        ops = {}
    incomplete: List[str] = [
        str(w) for w in (config.get("_preset_warnings") or [])]

    sources = config.get("_op_sources")
    if not isinstance(sources, dict):
        if config.get("presets"):
            incomplete.append(
                "op sources were never recorded — this config declares "
                "presets but did not pass through the loader, so no preset "
                "op was ever merged in: the list holds only what the raw "
                '"ops" section carried, and nothing can be attributed')
            sources = {}
        else:
            sources = {n: {"preset": None, "project": True, "overridden": []}
                       for n in ops}

    entries: List[OpOrigin] = []
    for name in sorted(ops):
        src = sources.get(name)
        if not isinstance(src, dict):
            src = {"preset": None, "project": False, "overridden": []}
        overridden = src.get("overridden")
        entries.append(OpOrigin(
            name=name,
            definition=ops[name],
            preset=src.get("preset"),
            project=bool(src.get("project")),
            overridden=(None if overridden is None
                        else tuple(str(k) for k in overridden)),
        ))
    return entries, incomplete


def _registry_not_enabled_line() -> str:







    missing = _presets_not_loaded_here()
    if not missing:
        return ""
    missing_set = set(missing)
    n_ops = sum(1 for p in _shipped_preset_ops().values() if p in missing_set)
    return (f"Not enabled here: {', '.join(missing)} "
            f"({len(missing)} shipped presets, {n_ops} ops). "
            f'Add one under "presets", or lead with cwd:<project-path>.')


def _registry_unknown_op(op_name: str) -> str:










    for config in (_load_config(), _shipped_config()):
        info = _help_entry(config, op_name)
        if isinstance(info, dict) and info.get("form"):
            parent = info["form"]
            return (f"'{op_name}' is a declared form of `{parent}` "
                    f"(spelling: `{info.get('syntax', op_name)}`), not an op "
                    f"name of its own — it has no registry entry of its own.\n"
                    f"  `registry:{parent}` is the entry; `help:{op_name}` "
                    f"documents the spelling.\n")
    preset = _shipped_preset_ops().get(op_name)
    if preset is not None:
        return (f"ERROR: '{op_name}' is not in this project's registry, but it "
                f"ships with this binary in preset '{preset}' — this config "
                f'does not list it under "presets".\n'
                f"  Every op that is loaded here: `registry`.\n")
    if op_name in _valid_op_names():
        builtin_entry, contributors, malformed = _registry_builtin_ops_entry(
            _load_config(), op_name)
        if builtin_entry is not None:
            return _registry_builtin_op(op_name, builtin_entry, contributors)
        if malformed is not None:
            return (f"ERROR: '{op_name}' is a built-in, and something in this "
                    f"config set `builtin-ops.{op_name}` to a value that is "
                    f"not a table, so nothing could be read out of it: "
                    f"{_flat_field(repr(malformed))}\n"
                    f"  This is NOT the same answer as no override at all - "
                    f"the entry was merged in and then dropped, by whichever "
                    f"preset manifest or project config declared it. A "
                    f"`builtin-ops` entry must be a table of keys.\n"
                    f"  `help:{op_name}` documents the op itself.\n")
        return (f"ERROR: '{op_name}' is a built-in, not a preset or project op, "
                f"so it has no registry entry. `help:{op_name}` documents it; "
                f"`ops:roster` lists every name.\n")
    return (f"ERROR: no op named '{op_name}' here.\n"
            f"  `registry` lists every op this config loads, with its source.\n")


def _registry_builtin_ops_entry(
        config: Dict[str, Any], op_name: str
) -> Tuple[Optional[Dict[str, Any]], Tuple[str, ...], Any]:






























    entry = (config.get("builtin-ops") or {}).get(op_name)
    if not isinstance(entry, dict):


        return None, (), entry
    contributions = config.get("_preset_doc_contributions") or {}
    contributors = tuple(sorted(
        preset for preset, names in contributions.items()
        if isinstance(names, list) and op_name in names))
    return entry, contributors, None








_REGISTRY_BUILTIN_DOC_KEYS = frozenset(
    {"syntax", "description", "example", "status", "hint", "form"})


def _registry_builtin_op(op_name: str, entry: Dict[str, Any],
                         contributors: Tuple[str, ...]) -> str:









    lines = [f"## {op_name} (built-in)"]
    lines.append(
        f"'{op_name}' is a built-in — its code is this module's own "
        f"dispatcher, and no preset or project op replaces it. But "
        f"`builtin-ops.{op_name}` re-tunes its RUNTIME behaviour project-"
        f"wide (#2025), and this project's effective config merges one in:")
    lines.append("")
    override_keys = sorted(
        k for k in entry if k not in _REGISTRY_BUILTIN_DOC_KEYS)
    doc_keys = sorted(k for k in entry if k in _REGISTRY_BUILTIN_DOC_KEYS)
    if override_keys:






        flat = {k: _flat_field(k) for k in override_keys}
        width = max(len(v) for v in flat.values())
        for key in override_keys:
            lines.append(f"- {flat[key].ljust(width)}  {entry[key]!r}")
    else:
        lines.append("(no runtime-affecting keys — only documentation.)")
    if doc_keys:
        lines.append("")
        lines.append("Documentation-only keys, which change nothing about "
                     f"how `{op_name}` runs: " + ", ".join(doc_keys) + ".")
    if contributors:
        lines.append("")
        lines.append("Contributed by preset(s): " + ", ".join(contributors)
                     + " — a project's own top-level `builtin-ops` entry "
                       "wins per key over any of these, the same rule "
                       "`registry:OP` already applies to `ops`.")
    lines.append("")
    lines.append(f"`help:{op_name}` documents the op's ordinary contract; "
                 f"this is only the merged override.")
    return "\n".join(lines) + "\n"


def _registry_incomplete_block(incomplete: List[str]) -> List[str]:






    out = ["INCOMPLETE: this listing may be missing ops."]
    out.extend(f"  - {reason}" for reason in incomplete)
    return out


def _registry_one_op(entry: OpOrigin, incomplete: List[str]) -> str:

    lines: List[str] = [f"## {entry.name}"]
    if entry.preset and entry.project and entry.overridden is None:
        lines.append(f"Preset '{entry.preset}' defines it; the project entry "
                     f"replaced that definition wholesale (non-dict override).")
    elif entry.preset and entry.project:
        n = len(entry.overridden or ())
        lines.append(f"Preset '{entry.preset}' defines it; shadowed by "
                     f"{n} project key{'' if n == 1 else 's'}, merged over it.")
    elif entry.preset:
        lines.append(f"Preset '{entry.preset}'.")
    elif entry.project:
        lines.append("Project config only — no preset ships this op.")
    else:
        lines.append("Source unknown — see the INCOMPLETE note below.")
    lines.append("")
    if isinstance(entry.definition, dict):
        overridden = set(entry.overridden or ())
        width = max((len(k) for k in entry.definition), default=0)
        for key in sorted(entry.definition):
            if key in overridden:
                where = "project"
            elif entry.preset:
                where = f"preset {entry.preset}"
            elif entry.project:
                where = "project"
            else:
                where = "unknown"
            lines.append(f"- {key.ljust(width)}  {where}")
    else:
        lines.append(f"- (not a table) {entry.definition!r}")
    if incomplete:
        lines.append("")
        lines.extend(_registry_incomplete_block(incomplete))
    return "\n".join(lines) + "\n"


def op_registry(op_name: str = "") -> str:






    entries, incomplete = _op_registry()
    if op_name:
        for entry in entries:
            if entry.name == op_name:
                return _registry_one_op(entry, incomplete)
        return _registry_unknown_op(op_name)

    shadowed = [e for e in entries if e.preset and e.project]
    from_preset = [e for e in entries if e.preset and not e.project]
    project_only = [e for e in entries if e.project and not e.preset]
    unattributed = [e for e in entries if not e.preset and not e.project]

    lines: List[str] = [
        f"## Op registry — {len(entries)} ops "
        f"({len(from_preset) + len(shadowed)} from presets, "
        f"{len(project_only)} project-only, {len(shadowed)} shadowed)",




        "Built-ins are not config entries and are not listed — `ops:roster`.",
        "",
    ]
    if incomplete:
        lines.extend(_registry_incomplete_block(incomplete))
        lines.append("")

    def _rows(group: List[OpOrigin], note) -> None:
        width = max((len(e.name) for e in group), default=0)
        for entry in group:
            lines.append(f"- {entry.name.ljust(width)}  {note(entry)}")
        lines.append("")

    if shadowed:
        preset_width = max(len(e.preset or "") for e in shadowed)
        lines.append(f"### Shadowed by project config ({len(shadowed)})")
        lines.append("The preset definition is still in effect; the project "
                     "entry merges these keys over it.")




        def _what_the_project_did(e: OpOrigin) -> str:
            if e.overridden is None:
                return "(replaced wholesale)"
            if not e.overridden:
                return "(merged, no keys)"
            return ", ".join(e.overridden)

        _rows(shadowed, lambda e: (
            f"preset {(e.preset or '').ljust(preset_width)}  + "
            + _what_the_project_did(e)))
    if from_preset:
        lines.append(f"### From presets ({len(from_preset)})")
        _rows(from_preset, lambda e: f"preset {e.preset}")
    if project_only:
        lines.append(f"### Project config only ({len(project_only)})")
        _rows(project_only, lambda e: "project")
    if unattributed:
        lines.append(f"### Source not known ({len(unattributed)})")
        _rows(unattributed, lambda e: "unknown")

    not_enabled = _registry_not_enabled_line()
    if not_enabled:
        lines.append(not_enabled)
        lines.append("")
    lines.append("One op with per-key sources: `registry:NAME`.")
    return "\n".join(lines) + "\n"
