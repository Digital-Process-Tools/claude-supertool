"""_supertool_catalog -- introduction/output-format/version/help/ops*/registry, split out of _supertool.py (#2706).

Loaded by `_load_part("_supertool_catalog")` from inside `_supertool.py`, at
the exact source position this code used to occupy: a plain `exec(code,
globals())` via `_load_part`, not a real `import`. Every function defined
below therefore has `__globals__ is _supertool.__dict__` once loaded, so
every existing `monkeypatch.setattr(supertool, "_shipped_config", ...)` /
`monkeypatch.setattr(supertool, "_SHIPPED_CONFIG", ...)` (and its
`_SHIPPED_CONFIG_DIR`/`_SHIPPED_CONFIG_STATE` siblings -- 8+ patch sites
measured across tests/test_help_reaches_shipped_docs_1773.py,
tests/test_meta_ops.py, tests/test_shipped_reference_three_states_1781.py,
tests/test_pip_shipped_reference_1783.py and tests/test_vim_preset_2026.py)
keeps reaching the code it patches. This is why this part is loaded the
`_load_part` way rather than the real-module-with-lazy-import way
`_supertool_doctor.py`/`_supertool_gc.py` chose: those two measured far
fewer monkeypatch sites against their own bodies (#2706 "Decision").

Not importable on its own. `_load_part` is the only legitimate loader: it
puts `_load_part` itself into the globals this file executes against before
running it, which is exactly the marker checked for below. A bare `import
_supertool_catalog` or `python3 _supertool_catalog.py` gets this module's
own fresh globals(), which has no such name, and refuses with a clear
ImportError rather than failing later with a NameError on the first name
this file assumes `_supertool.py` already defined (Dict, Any, os, re, ...).

Two spans of the original file, concatenated here in their original order:
`_onboarding_text`/`op_introduction`/`op_output_format`/`op_version` (used
to sit immediately before `op_doctor`/`op_init`), then everything from
`_SHIPPED_CONFIG` through `op_registry` (used to sit immediately after
`op_doctor`/`op_init`). `op_doctor`/`op_init` themselves are NOT part of
this file -- they are #2714's own already-landed stubs, left exactly where
they were in `_supertool.py`, between the two spans this file now holds
concatenated. One `_load_part("_supertool_catalog")` call, at the position
the first span used to occupy, loads both: nothing in either span has an
import-time dependency on `op_doctor`/`op_init` or vice versa -- every
cross-reference among these names is resolved at call time, well after the
whole core module (including both of this file's spans) has finished
loading.
"""
from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_catalog.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )

def _onboarding_text(config_key: str, default: str, *,
                     env_value: "Optional[str]") -> str:
    """`env_value`, else .supertool.json[config_key], else `default`.

    Takes the variable's VALUE, read by the caller under its own literal
    name (#2734): `os.environ.get(env_var)` here was the first read the
    directory's validator cited in this file ("an environment variable
    named at run time"). Keyword-only, so an old positional call fails
    loudly instead of treating a variable's name as its value.

    Same env-over-config-over-built-in convention as `_DEFAULT_COAUTHOR`
    (presets/git/commit.py). Whichever layer wins, a value in
    `_ONBOARDING_DISABLE_VALUES` (case-insensitive, stripped) renders as ""
    so the caller can still print the old "not configured" line -- a
    default that cannot be turned off is worse than none for a project that
    has deliberately kept its session preamble bare (#2342).
    """
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
    """Project introduction text: env override, else .supertool.json's
    `introduction` key, else a shipped default (#2342)."""
    intro = _onboarding_text(
        "introduction", _DEFAULT_INTRODUCTION,
        env_value=os.environ.get("SUPERTOOL_INTRODUCTION"))
    if not intro:
        return "No introduction configured in .supertool.json\n"
    return str(intro) + "\n\n"


def op_output_format() -> str:
    """Output format examples: env override, else .supertool.json's
    `output-format` key, else a shipped default (#2342)."""
    fmt = _onboarding_text(
        "output-format", _DEFAULT_OUTPUT_FORMAT,
        env_value=os.environ.get("SUPERTOOL_OUTPUT_FORMAT"))
    if not fmt:
        return "No output-format configured in .supertool.json\n"
    return str(fmt) + "\n\n"


def op_version() -> str:
    """Output the supertool version."""
    return f"supertool {VERSION}\n"


_SHIPPED_CONFIG: Optional[Dict[str, Any]] = None

#: Which of the three worlds the last `_shipped_config()` call found. `None`
#: until one runs, then exactly one of `read` / `absent` / `unreadable`.
#: Separated from the `{}` it returns because those three states produced one
#: string, and one of the three sentences it produced was false (#1781).
_SHIPPED_CONFIG_STATE: Optional[str] = None

#: The directory the shipped reference is read from. A module-level name rather
#: than a call to `os.path.dirname(__file__)` inline so a test can build the
#: three installs without copying the binary — the audit that found #1781 had
#: to copy `supertool.py` and `_supertool.py` into three temp trees to do it.
_SHIPPED_CONFIG_DIR: Optional[str] = None


def _shipped_config() -> Dict[str, Any]:
    """The `.supertool.json` that ships beside this module (#1773).

    `_load_config()` walks up from **cwd**, so it finds the *project's* config.
    Preset ops carry their documentation wherever the plugin is installed, but
    the builtin ops' `builtin-ops` block lives in this repository's own config
    — not a preset, and merged into nobody else's tree. From a plain consumer
    repo `help:read`, `help:grep`, `help:paste` and `help:edit` therefore all
    answered "has no documented help", while `help:gh-pr` answered in full.

    Read from `__file__`'s directory rather than by any config search: the fact
    being looked up is a property of *this binary*, and the whole failure was a
    lookup that depended on where the caller was standing. Cached, and an
    unreadable or malformed file yields `{}` — a fallback that cannot answer
    must leave the caller with the ordinary refusal, never a traceback.
    """
    global _SHIPPED_CONFIG, _SHIPPED_CONFIG_STATE
    if _SHIPPED_CONFIG is None:
        directory = _SHIPPED_CONFIG_DIR or os.path.dirname(
            os.path.abspath(__file__))
        path = os.path.join(directory, ".supertool.json")
        data: Any = None
        # No `os.path.exists()` pre-check (#1783): it swallows `EACCES` and
        # returns `False` for a directory the process cannot traverse, so a
        # reference sitting inside an unreadable directory reported as
        # "not there" — the exact false sentence #1781 removed one file
        # down, reappearing one level up. `open()` unconditionally instead,
        # and let the exception itself say which of the two happened. This
        # also closes the TOCTOU between the check and the open, and the
        # race was already benign in the safer direction: a file deleted
        # between the two calls used to land in `unreadable` (honest), not
        # in a false "read" (it never does now either).
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            # The install genuinely shipped no reference. Separable from the
            # two below, and the only one of the three where "it does not
            # document this op" is a sentence anybody could act on.
            _SHIPPED_CONFIG_STATE = "absent"
        except (OSError, ValueError):
            # Every other reason `open()` or `json.load()` could fail:
            # permission denied on the file OR the directory containing it,
            # a directory named `.supertool.json`, a symlink loop, malformed
            # JSON. A JSON scalar or list parses without raising and
            # documents nothing, which is not the same fact as a file that
            # documents nothing — one is a reference, the other is not one —
            # so that shape lands here too, in the `else` below.
            data = None
            _SHIPPED_CONFIG_STATE = "unreadable"
        else:
            _SHIPPED_CONFIG_STATE = "read" if isinstance(data, dict) else "unreadable"
        if _SHIPPED_CONFIG_STATE == "absent":
            # The clone and plugin routes ship `.supertool.json` itself and
            # never reach here. The pip route ships neither that file nor
            # `presets/` — every declarative packaging route that could
            # carry a data file there was tried and rejected (#1783's own
            # comment thread: `package-data` globs over declared *packages*
            # and a flat `py-modules` layout has none; `MANIFEST.in` +
            # `include-package-data` reaches the sdist, not the wheel;
            # `data-files` lands in the venv prefix, not site-packages). So
            # that route ships `_shipped_reference.py` instead, a plain
            # module in `py-modules` that survives every route because it
            # IS a module, carrying only the `builtin-ops` block generated
            # from this repo's own `.supertool.json` by
            # `.github/scripts/generate_shipped_reference.py` — never the
            # `ops` section, which documents preset-config overrides for
            # `presets/` this route does not ship either.
            # Loaded from `directory` by path, never a bare `import
            # _shipped_reference` — this call runs from inside this
            # repository's own checkout too, where a bare import would find
            # THIS tree's `_shipped_reference.py` on `sys.path` regardless
            # of `directory`, which is exactly wrong for an install that
            # `_SHIPPED_CONFIG_DIR` is simulating as not having one.
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
                        # The module loaded but does not carry the shape
                        # this fallback expects — present, but not a
                        # reference. Same "cannot tell" bucket as a file
                        # that failed to load at all (#1783 review).
                        _SHIPPED_CONFIG_STATE = "unreadable"
            except FileNotFoundError:
                # Neither `.supertool.json` nor `_shipped_reference.py`
                # exists here — genuinely absent, the state already set
                # above stays correct.
                pass
            except (OSError, ImportError, SyntaxError, ValueError):
                # The module IS there and failed to load — permission
                # denied, a syntax error in a hand-damaged install, or any
                # other reason `exec_module` could raise. Collapsing this
                # back to "absent" would reintroduce, one file over, the
                # exact defect item 2 of this same issue closed for
                # `.supertool.json`: a present-but-broken reference
                # reporting as though nothing shipped at all (#1783 review,
                # Explore/oss:auditor).
                _SHIPPED_CONFIG_STATE = "unreadable"
        _SHIPPED_CONFIG = data if isinstance(data, dict) else {}
        _fold_shipped_preset_docs(_SHIPPED_CONFIG, directory)
    return _SHIPPED_CONFIG


def _fold_shipped_preset_docs(shipped: Dict[str, Any], directory: str) -> None:
    """Fold the shipped presets' `builtin-ops` into the shipped reference.

    A built-in's documentation may live in a preset manifest rather than in
    `.supertool.json` (#2025, #2026), and where it lives is an implementation
    detail of this install — not a fact about the caller's tree. Without this,
    moving `vim`'s entry into `presets/vim.json` made `help:vim` answer "no
    documented help" from any repo that does not list the preset, which is
    exactly the failure #1773 was filed about, reintroduced one file over.

    The listing bytes are still saved: `op_ops` renders the *project's* merged
    config, which only holds what that project's presets contributed. This
    reaches the `help:OP` fallback alone, where the question is what this
    binary can document rather than what this repo loads.

    Entries in `.supertool.json` win — it is the more specific reference — and
    an unreadable manifest contributes nothing, by the same rule as everywhere
    else: a preset we cannot read is an absence, never a fatal.
    """
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
    """Where `_shipped_config()` looks — named in the refusal, never guessed at.

    A remedy that says "check the file" without saying which file sends the
    reader to the project config they are standing in, which is the one place
    the answer is not.
    """
    directory = _SHIPPED_CONFIG_DIR or os.path.dirname(
        os.path.abspath(__file__))
    return os.path.join(directory, ".supertool.json")


def _help_entry(config: Dict[str, Any],
                op_name: str) -> Optional[Dict[str, Any]]:
    """The documentation entry for `op_name` in one config, or None."""
    for section in ("builtin-ops", "ops", "aliases"):
        entry = config.get(section, {})
        if not isinstance(entry, dict) or op_name not in entry:
            continue
        info = entry[op_name]
        if isinstance(info, dict):
            return info
    return None


def op_help(op_name: str) -> str:
    """Output the full reference for a single op from .supertool.json.

    Same metadata `ops` lists, but scoped to one op and never compacted — so
    payload shapes (e.g. vim's macro grammar) are readable without grepping
    source. Looks through builtin-ops, then custom ops, then aliases.

    The project's config answers first and always wins: a project that
    redefines an op documents its own version, and that is the one its caller
    must be shown. Only when no section here has heard of the name does the
    shipped reference answer (#1773), and the answer says so — the entry
    describes the binary, not this tree.
    """
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
            # #1675 — the other half of the registry/help disagreement:
            # `registry:NAME` now says the same thing for a declared form, so
            # neither surface leaves a reader concluding the name is a real,
            # independent op.
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
        # Three states, not two (#1781). `_shipped_config()` returned `{}` for
        # a reference that was absent, one that was unreadable and one that was
        # read and documents nothing — and the single sentence built on it
        # asserted the third about all three. A `chmod 000` install rendered
        # byte-for-byte identically to an install with no file at all, while
        # the file sitting beside the binary documented the op.
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
            # Not one of the three states this lookup is meant to produce —
            # `_SHIPPED_CONFIG_STATE` is `None` (checked before the first
            # lookup ever ran) or some future fourth value. Neither prior
            # sentence is known to be true of it, so this arm must not
            # assert either one (#1783): a catch-all `else` that repeats
            # the "read and does not document" sentence would claim a
            # specific, false thing about a state nobody has produced yet.
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


# Claude Code's hook-stdout cap. Over it, the payload is written to disk and
# only a 2,000-character preview is injected into the model's context, so the
# tail of a listing is hidden behind something that still reads as a listing.
#
# Read out of the harness rather than guessed (#2029). HOW TO RE-DERIVE IT,
# because this is a third-party constant and it can move under us:
#
#     B="$(dirname "$(readlink -f "$(which claude)")")/claude.exe"
#     strings -n 8 "$B" > /tmp/cc-strings.txt
#     grep -oE "originalSizeBytes[^;]{0,120}" /tmp/cc-strings.txt
#
# `bin/claude.exe` is the real executable on every platform despite the name
# (Mach-O arm64 here, ~250MB); the CLI is a compiled bundle, so the JavaScript
# source is not on disk but the string table still carries the function bodies
# verbatim. Search for the emitted message rather than the constant — the
# minifier renames `k0u` on every release, and `Output too large (` does not
# move. From that line, read the enclosing function's default argument.
#
# What that search finds in the shipped bundle
# (`@anthropic-ai/claude-code`, `bin/claude.exe`):
#
#     var CKr=50000, mor=500000, AKr=4, A0u=400000, R0u=200000, i3=50, k0u=1e4;
#
#     async function jKe(e, t, r, n = k0u) {
#         if (e.length <= n) return e;
#         let o = await x2e(e, `hook-${t}-${r}`);
#         if (I2e(o)) return M("tengu_hook_output_persisted", ...
#
# `k0u = 1e4`, and the comparison is `<=`, so exactly the cap passes. The
# 2,000 is its own constant (`hor`) and is what the observed "Preview (first
# 2KB)" line reports. It is the *hook* path specifically: the persisted file is
# named `hook-<id>-<stream>`, the event is `tengu_hook_output_persisted`, and
# tool results are capped separately and far higher by `CKr = 50000`.
#
# This value was 7168 for thirty-odd releases: a midpoint of an empirical
# bracket, which its own comment said out loud — "6.6KB landed full, 11KB+ got
# truncated". Both observations bracket 10,000 and are kept here as
# corroboration; the midpoint drawn between them was 40% low, and it was the
# premise of what every fresh session gets shown (hooks/session-start.sh) and
# of the truncation warning `op_ops(compact=True)` prepends.
#
# UNITS. `e.length` is a JavaScript string length — UTF-16 code units, i.e.
# characters. Supertool measures BYTES, and its listings are full of `—`, `→`,
# `⚠`, `✓`: one character each, three bytes each. So a byte count over-reports
# against this limit and can only refuse or warn EARLIER than the harness
# would, never later. That is the safe direction and it is why this stays a
# byte count rather than being "corrected" to characters — a conservative
# bound survives the harness changing its own unit, and a tight one does not.
# (Claude Code logs the same figure as `originalSizeBytes: e.length`, so the
# confusion is not only ours.)
_HOOK_OUTPUT_CAP_BYTES = 10000


def _over_hook_cap(payload: str) -> bool:
    """Would the harness persist this hook payload instead of injecting it?

    One helper rather than an inline comparison at each site, because the
    boundary is inclusive and `>` versus `>=` is exactly the kind of detail
    that gets flipped by someone reading only the constant. Measured in
    UTF-8 bytes — see the units note above.
    """
    return len(payload.encode("utf-8")) > _HOOK_OUTPUT_CAP_BYTES


def _configured_op_names(config: Dict[str, Any]) -> set:
    """Op names this config has an opinion about — including the ones it hides.

    An entry declaring `form` is skipped: it documents a *spelling* of another
    op (`read-grep` for `read:PATH:::grep=`) and is not a name the dispatcher
    accepts, so counting it here would put a phantom in the one set that
    answers "which op names does this config know about" (#1245).

    ``status: 0`` is a project deliberately suppressing an op from its listing,
    and the disclosure that calls it back out would undo that choice. Same line
    the preset disclosure already draws: the tool names what *it* hid, never
    what the project chose to hide. Only an op with no entry at all is
    undisclosed, which is the case #1124 is about.
    """
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
    """Output the ops reference from .supertool.json (builtin-ops + ops sections).

    Source of truth is the JSON config. If no config exists, falls back to
    listing built-in op names without descriptions.

    **The default is signatures** (#1774). Every op, every name, the shape of
    the call — and nothing else. The descriptive render is `ops:full`, which is
    what this op used to be: tens of KB in this tree against a 10,000-byte
    SessionStart cap (`tests/test_render_size_claims_1877.py` pins the exact,
    checkout-path-dependent figure — not a literal here, per #1813), ~19k
    tokens spent by a caller whose question was which op lists PRs. The cost
    was never spread evenly — across 128 documented ops the median description
    is ~150 characters and the top ten rows are roughly half the corpus —
    because `description` is printed whole by both `ops:full` and `help:OP`
    (never by this listing's default output), and had become the record of how
    each op got here. #1775 put a ratchet under the growth; this changes who
    pays for it by default.

    Nothing is dropped: the row count is identical in both modes, and the
    default footer states the size of what it withheld and the token that
    fetches it. A shorter listing that said nothing would be the defect this
    repo keeps having — an absence produced by the tool, read as an absence in
    the world.

    When compact=True, drops example lines for ops that don't have hint=true,
    and — if the resulting body still exceeds _HOOK_OUTPUT_CAP_BYTES — prepends
    a warning telling the reader that the tail is hidden and to call 'ops' for
    the full listing. Used by the SessionStart hook to maximize information
    density under the harness's hook-output cap.
    """
    config = _load_config()
    builtin_ops = config.get("builtin-ops", {})
    custom_ops = config.get("ops", {})
    alias_defs = config.get("aliases", {})
    lines: List[str] = []

    if not builtin_ops and not custom_ops and not alias_defs:
        # No config — bare fallback listing built-in names
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
        """Whether to print the Example: line for this op given current mode."""
        if not info.get("example"):
            return False
        if full:
            return True
        if not compact:
            # Signature mode. An example is a second line per op and the whole
            # subject here is the shape of the call, which `syntax` already is.
            return False
        return bool(info.get("hint"))

    def _emit_desc(info: dict) -> str:
        """Return description if it should be shown, else empty string.

        In compact mode, descriptions are only kept for ops marked
        ``hint: true`` — the rest are considered self-explanatory from
        their signature alone (read:PATH, grep:PATTERN:PATH, etc.) and
        their description adds no information.
        """
        desc = info.get("description", "")
        if not desc:
            return ""
        if full:
            return desc
        if not compact:
            # #1774 — the default listing is signatures. The prose is one token
            # away (`ops:full`) and one op away (`help:OP`), and the footer
            # names both with the byte count it withheld.
            return ""
        return desc if info.get("hint") else ""

    # Where the disclosure goes depends on which absence it is describing.
    #
    # No config found: the listing actively misleads — it reads as the tool's
    # whole capability (#614's filer read it that way) — so it goes on top,
    # above the SessionStart cap's truncation point, where it is read first.
    #
    # Config found: the missing presets are that project's deliberate choice,
    # not a surprise about where the caller is standing. Same line, but as a
    # footer — a permanent banner on the most-read output would be noise, and
    # being cut by the cap costs nothing when nobody was misled.
    disclosure = _preset_disclosure()
    if disclosure and not _CONFIG_PATH:
        lines.append(disclosure)
        lines.append("")

    # Operations section — built-in and custom merged into one flat list
    has_ops = False
    if builtin_ops or custom_ops:
        lines.append(_CLASS_LEGEND)
        lines.append("## Operations\n")
        has_ops = True
        # Three states, not two (#1124). An op the dispatcher accepts but that
        # no config section describes was omitted outright, so `ops` — the
        # tool's own answer to "what can you do?" — read as a complete
        # capability list while hiding `batch`: the one op that collapses N
        # mutations into a single call, and the only escape from #341's
        # one-payload-per-call cap. Measured over 232 agent transcripts, 70% of
        # supertool calls carried a single op, and every agent that used
        # `batch` had learned it from an out-of-band brief.
        #
        # Derived from the dispatcher's own sets rather than hand-maintained,
        # for the same reason `_valid_op_names` exists (#614): the next op
        # added without a .supertool.json entry discloses itself.
        #
        # Placement follows the rule the preset disclosure already sets — a
        # listing that actively misleads puts its disclosure above the
        # SessionStart truncation point, because compact output is already over
        # the cap and a line at the bottom is a line nobody reads. One line,
        # never a second listing: an op with a real entry never reaches here.
        undocumented = sorted(set(_valid_op_names()) - _configured_op_names(config))
        if undocumented:
            lines.append("Also accepted, no reference in .supertool.json: "
                         + ", ".join(undocumented) + "\n")

    # The safety class, from the one place it is already declared (#2028).
    # `ops` is what a session is handed now, so it has to answer the question
    # the roster was carrying alone: which of these may I call blind to learn
    # its arguments? A signature tells you the shape of a call; it does not
    # tell you that making it opens an issue or merges a pull request.
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
            # A `form` entry documents a spelling of another op and dispatches
            # as nothing (#1245), so it inherits that op's class rather than
            # falling to `acts` and rendering a `!` on `read:PATH:::grep=`.
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

    # Aliases section
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

    # #1774 — say what was withheld, in bytes, and how to get it. Measured
    # rather than described: the number is the full render's own size, so a
    # listing that has been trimmed reports a smaller saving by construction
    # and this line cannot go stale the way a hand-written one would.
    if not compact and not full:
        withheld = len(op_ops(full=True).encode("utf-8"))
        body += (
            f"\nSignatures only — every description above is withheld. "
            f"`ops:full` is the same rows carrying them, and costs "
            f"{withheld} bytes in total.\n"
            f"  One op, in full: `help:OP`.  Every description: `ops:full`.  "
            f"Names plus safety class: `ops:roster`.\n"
        )

    # In compact mode, only warn if the body still won't fit the harness cap.
    # When it fits, no warning — the absence is itself a signal that the listing
    # is complete.
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
    """Every dispatchable op name here, mapped to its safety class (#1231).

    Two sources, each the place the fact is already declared:

    * **Built-ins** come from ``_OP_SAFETY_BUILTIN``, next to the sets that say
      they exist. A project config cannot downgrade one — the class is a
      property of this binary, and ``.supertool.json`` may be absent or belong
      to somebody else's tree.
    * **Preset and project ops** come from a ``"safety"`` key on the op entry,
      beside its ``cmd`` and ``description``. Absent or unrecognised falls back
      to ``acts``, the loudest class, so an undeclared op is over-marked rather
      than quietly under-marked.

    ``status: 0`` suppression is honoured, same as the listing: a project
    hiding an op from ``ops`` meant it, and the roster is not a way around it.
    Built-in *documentation* keys are not a name source — ``.supertool.json``
    carries ``grep-count`` and ``read-grep``, which document forms of ``grep``
    and ``read`` and dispatch as neither. This walk excludes them structurally,
    by iterating ``_valid_op_names()``; since #1245 they also say so, with a
    ``"form"`` key, so an enumeration that cannot do it structurally has
    something to read.
    """
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


# The class key, shared by `ops` and `ops:roster` so the two cannot drift into
# describing the same three markers differently (#2028). Short on purpose: it
# is paid at every session start, and what it has to establish is only that
# unmarked is a *claim* rather than a missing annotation.
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
    # Not a complete account of how files get touched, and it read as one
    # (#1671). The raw-command guard is a PreToolUse hook whose matcher is
    # `Bash|PowerShell`, so `Edit`/`Write` never reach it: the same one-key
    # change was denied through a heredoc and unremarkable through `Edit`,
    # minutes apart. One line, ~150 bytes of a ~10KB session budget, because
    # what it changes is what a reader believes about a boundary they are
    # inside — and a listing of ops is exactly where that belief forms.
    "Ops are one route to disk, not the only one: the raw-command guard "
    "hooks Bash\nonly, so a harness `Edit`/`Write` writes with no op, no "
    "validator and no\nrollback (#1671)."
)


def op_ops_roster(width: int = 78) -> str:
    """Names + safety class for every op, and nothing else (#1231).

    Flat and alphabetical rather than grouped by family: the three misses that
    motivated the issue were all neighbour misses — ``gh-pr-create`` beside
    ``gh-pr``, ``git-worktrees`` beside ``git-status``, ``paste`` beside
    ``write`` — and one alphabetical sweep finds a neighbour where a family
    grouping asks the reader to already know which family it is in.
    """
    classes = _roster_classes()
    tokens = [f"{name}{_SAFETY_MARKERS.get(cls, '!')}"
              for name, cls in sorted(classes.items())]
    # The same disclosure `ops` carries, and for a stronger reason: a roster
    # whose whole subject is completeness must say which shipped presets this
    # directory does not load. Without it the short list from a non-project
    # directory reads as the tool's whole capability — #614's filer read the
    # listing exactly that way. Above the names, because that is where a reader
    # who is about to conclude "no such op" is still looking.
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
    """What a fresh session is handed: signatures if they fit, names if not.

    The choice lives here rather than in `hooks/session-start.sh` because the
    cap constant does, and a shell script measuring a payload it then has to
    re-generate would be a second place for the same decision to go stale —
    which is the failure this op exists downstream of. #2029 found the cap had
    been a guessed midpoint for thirty-odd releases and 40% low.

    **Signatures, not names, by default (#2028).** `ops:roster` prevents "I did
    not know this op existed"; it does not prevent "I did not know this op was
    the answer". An op's own error teaches its signature — true, and the reason
    a roster is defensible at all — but an error only fires after the decision
    to call has been made. A name a reader cannot interpret is a capability
    never reached for, and nothing fails when that happens, so the cost is
    invisible by construction. `between` is a word; `between:SYMBOL:PATH` is a
    call.

    **Three states, not two.** Over the cap the harness writes the payload to
    disk and injects a 2,000-character preview, so a listing that does not fit
    arrives looking like a listing that does. The fallback therefore says what
    it measured, against what, and which listing it withheld — a shorter answer
    with no account of itself is the defect this repo keeps having.
    """
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
    """`ops:gh-labels` printed the whole 47KB listing and said nothing (#1231).

    An argument dropped without a word, in the op whose job is to say which
    arguments exist, in a tool whose rule is that an unrecognised token is
    refused rather than ignored.

    Refused rather than made a filter for the three fixed modes below.
    ``help:OP`` already answers what a filter on an *exact* name would, and
    answers it with strictly more — full contract, semantics and a worked
    example, against the listing's one line. A search across names, syntax
    and descriptions is a different question, and `ops:grep=PATTERN` (#1318)
    answers that one directly, disclosing `N of M matched` so a pattern that
    matches nothing cannot render like an op that does not exist — the
    absence-as-answer defect this function's own history is about.
    """
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
    """`ops:grep=PATTERN` — search the roster instead of piping it (#1318).

    Three independent agents hit the same detour in one week: `ops` prints
    the whole roster, piping it through `grep` is what the shipped
    raw-command guard correctly blocks, and the only route left was a
    redirect-to-a-temp-file workaround — the exact motion the guard exists
    to prevent, reached by obeying it.

    Matches op name, syntax and description — the description because that
    is often the only place the words in "which op creates a file" actually
    appear, even though bare `ops` withholds it by default (#1774). A matched
    row is printed with its description regardless, since the match reason
    would otherwise be invisible. Case-sensitive, like every other pattern
    slot in this file (`grep`, `around`, `between`, `read`'s own `grep=`) —
    this is the one search among them and the one place a silent default
    departure would be least visible.

    Every dispatchable op is a candidate, not only the ones with a
    `.supertool.json` entry: `op_ops()` discloses the undocumented set as a
    footer line (#1124), and a search that skipped them would answer `0 of M`
    for a real, callable op like `introduction` — the exact absence-as-answer
    defect this op exists to remove, arrived at a different way.

    Reuses `_pattern_gate`, the one chokepoint every other pattern-taking op
    goes through (#2574 did the same for `between`'s start/end), so the BRE
    rewrite, the length cap and the ReDoS backtracking guard all apply here
    too. `check_saturation=False`: the saturation refusal exists because a
    pattern matching every line renders identically to an unfiltered read,
    which is exactly the defect this op avoids a different way — by always
    stating `N of M ops matched`, so a pattern matching everything is still an
    honest answer rather than a silent full listing (the same reasoning #2573
    gave `op_vim` for opting out of the same check).

    Always states the count, including `0 of M` — the issue's own requirement
    — so an empty result can never be misread as a short roster.
    """
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
        # `op_ops()` hits this same empty-config state and falls back to
        # every built-in name rather than claiming zero ops exist (#1318
        # review) — a filter over nothing would otherwise say "0 of 0" while
        # dozens of real, dispatchable ops sit unsearched one call away.
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

    # Names the dispatcher accepts but no config section describes — the same
    # set `op_ops()` names in its own footer (#1124) — with no syntax or
    # description to offer, so the name is the only thing to search or show.
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
    """One op in the effective registry, and where its definition came from.

    ``overridden`` is ``None`` — not ``[]`` — when a project entry replaced a
    preset definition wholesale rather than merging keys into it. An empty list
    would read as "the project changed nothing".
    """
    name: str
    definition: Any
    preset: str | None
    project: bool
    overridden: Tuple[str, ...] | None


def _op_registry(config: Dict[str, Any] | None = None
                 ) -> Tuple[List[OpOrigin], List[str]]:
    """The effective op registry, plus the reasons it may be short (#1356).

    **The population comes from the product, never from a second walk.**
    ``config["ops"]`` is what `_merge_presets` produced; this annotates it from
    the provenance the same walk stamped. A caller that re-globbed
    ``presets/*.json`` and wrote ``ops[name] = entry`` would silently reduce
    three of this repo's ops to stubs — see `_merge_op_def`.

    The second element is the point of the function. A registry that could not
    enumerate everything must say so rather than return a smaller set, because
    a short list and a complete one render identically. Three states:

    * provenance stamped by the loader — authoritative,
    * no ``presets`` key at all — every op is the project's own, which is a
      fact derivable without the loader, so still complete,
    * ``presets`` declared but never merged — the population is whatever the
      raw ``ops`` section held, so it is *both* short and unattributable, and
      both are reported.

    Preset load failures recorded in ``_preset_warnings`` are carried through
    too: those ops are genuinely absent from the list.
    """
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
    """Shipped presets this config does not load, by name and op count.

    Same disclosure `ops` carries, rebuilt here to name **only preset names**.
    `_preset_disclosure` embeds `_CONFIG_PATH` / `os.getcwd()`, and a host path
    in this body would make the render differ between Windows and POSIX for no
    gain — the registry's subject is attribution by name.
    """
    missing = _presets_not_loaded_here()
    if not missing:
        return ""
    missing_set = set(missing)
    n_ops = sum(1 for p in _shipped_preset_ops().values() if p in missing_set)
    return (f"Not enabled here: {', '.join(missing)} "
            f"({len(missing)} shipped presets, {n_ops} ops). "
            f'Add one under "presets", or lead with cwd:<project-path>.')


def _registry_unknown_op(op_name: str) -> str:
    """Three states for a name the registry does not hold (#614's rule).

    A fourth, added for #1675: a `builtin-ops` entry declaring `form` names a
    documented *spelling* of another op (`grep-count` for `grep:...:count`,
    `#1245`), not a dispatchable name — `registry` used to say "no op named"
    about it, the identical sentence it gives a name nobody ever declared.
    Checked first, project config before the shipped reference, the same
    order `_help_entry` already resolves in — so a reader who checks
    `registry` and one who checks `help` learn the same fact.
    """
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
    """The merged `builtin-ops.<op_name>` dict, which presets contribute to it,
    and whether what was merged in could not be read at all (#2079).

    Three states, because two of them are absences and only one of them is an
    absence in the world:

    - `(None, (), None)` - nothing merged a `builtin-ops.<op_name>` in at all.
    - `(None, (), <value>)` - something did, and it is not a table. A preset
      manifest carrying `"read": "not-a-dict-oops"` lands here: `_merge_presets`
      stores whatever the JSON held without checking its shape and records no
      preset warning for it, so folding this into the state above rendered a
      malformed config byte-identical to a clean one. That is this repository
      own signature defect wearing a config reader clothes, and the reason the
      caller gets the offending value back rather than a bare `None`.
    - `(<dict>, <contributors>, None)` - a real entry.

    `_merge_presets` writes every preset's `builtin-ops` section — doc-only
    entries a project can also carry runtime overrides in (`read.max_lines`,
    `grep.extensions`) — into `config["builtin-ops"]`, key by key, project
    entries winning over preset ones the same way `ops` merges (#2025). That
    section is read by `_get_op_int`, `_get_op_bool` and `_grep_extensions`
    for a built-in's runtime behaviour, but `registry:OP` used to refuse
    outright for any built-in, so a preset or project re-tuning one project-
    wide had nothing in the tool that disclosed it.

    Contributors come from `_preset_doc_contributions`, which the loader
    stamps per preset during the same walk — never re-derived by re-reading
    `presets/*.json` here, for the reason `_op_registry`'s own docstring
    gives: a second walk drifts from what the loader actually merged.
    """
    entry = (config.get("builtin-ops") or {}).get(op_name)
    if not isinstance(entry, dict):
        # `entry is None` here is the honest absence; anything else was set
        # and is unreadable, and the caller must be able to tell them apart.
        return None, (), entry
    contributions = config.get("_preset_doc_contributions") or {}
    contributors = tuple(sorted(
        preset for preset, names in contributions.items()
        if isinstance(names, list) and op_name in names))
    return entry, contributors, None


#: Keys a `builtin-ops` entry carries purely to document the op (#1675) —
#: never read by `_get_op_int`/`_get_op_bool`/`_grep_extensions`, so a project
#: that sets only these has changed nothing about how the built-in runs. Kept
#: as a set here rather than inferred from "every key `_get_op_int` reads",
#: because that reader is keyed by call site, not by a registry `registry`
#: could enumerate — the same reason `_OP_TARGETS` above is a hand-kept map.
_REGISTRY_BUILTIN_DOC_KEYS = frozenset(
    {"syntax", "description", "example", "status", "hint", "form"})


def _registry_builtin_op(op_name: str, entry: Dict[str, Any],
                         contributors: Tuple[str, ...]) -> str:
    """Render a built-in's merged `builtin-ops` entry (#2079).

    Not the same shape as `_registry_one_op`: a built-in has no `cmd`, no
    `replaces`, and its code is this module's own dispatcher rather than
    anything a preset or project entry can replace. What CAN change is the
    handful of runtime knobs `_get_op_int`/`_get_op_bool`/`_grep_extensions`
    read out of this same merged dict — so that is what this renders, split
    from the keys that are pure documentation and change nothing.
    """
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
        # Both halves are preset- or project-authored: a manifest chooses its
        # own key names as freely as its values, and a key holding a newline
        # would otherwise write a line of its author's choosing at column 0
        # inside this system-authored block - #1391's shape, one function over
        # from `_guard_quote`, which flattens field names for exactly this.
        # `!r` already makes any value one line; `_flat_field` does the key.
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
    """The marker that keeps a short population from reading as a whole one.

    In the body, not on stderr. `main()` already prints `_preset_warnings` to
    stderr, but in a batched call stderr is somewhere else entirely and the
    reader of this op's output sees a list that looks complete (#1356).
    """
    out = ["INCOMPLETE: this listing may be missing ops."]
    out.extend(f"  - {reason}" for reason in incomplete)
    return out


def _registry_one_op(entry: OpOrigin, incomplete: List[str]) -> str:
    """One op's merged definition, with the source of each key."""
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
    """Render the effective op registry, or one op's merged definition.

    Exists so the answer to "what ops are loaded, and where did each come
    from?" comes from the product rather than from each caller's copy of the
    merge rule (#1356).
    """
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
        # Built-ins are a property of the binary, not of a config's registry,
        # so they are not here. Said out loud: a count that reads as the
        # tool's whole capability is #614's defect, and this one is smaller
        # than `ops:roster` by every built-in.
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
        # Three states, not two. `None` is a wholesale replace; `()` is a
        # merge that changed nothing, which leaves the preset definition fully
        # intact — rendering both as "replaced wholesale" asserts the opposite
        # of the truth for the second.
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
