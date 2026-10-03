"""_supertool_config -- config/presets-merge/env-knobs/exclude/gitignore/rtk/display, split out of _supertool.py (#2706).

Loaded by `_load_part("_supertool_config")` from inside `_supertool.py`, at the
exact source position this code used to occupy: a plain `exec(code,
globals())` via `_load_part`, not a real `import`. Every function defined
below therefore has `__globals__ is _supertool.__dict__` once loaded, so
every existing `monkeypatch.setattr(supertool, "_load_config", ...)` (and the
rest of this file's names -- at least 46 monkeypatch sites across 18 test
files, unlike `_supertool_guard.py`'s pilot measurement of 0) keeps reaching
the code it patches. A real `import` would give this code its own module
globals() and silently stop every one of those sites from patching anything.

Not importable on its own. `_load_part` is the only legitimate loader: it
puts `_load_part` itself into the globals this file executes against before
running it, which is exactly the marker the guard below checks for. A bare
`import _supertool_config` or `python3 _supertool_config.py` gets this
module's own fresh globals(), which has no such name, and refuses with a
clear ImportError rather than failing later with a NameError on the first
name this file assumes `_supertool.py` already defined (Dict, Any, os, re,
subprocess, time, fnmatch, ...).

Covers: `.supertool.json` discovery/load/merge (`_load_config`,
`_merge_presets`, preset-cmd resolution), the op registry's source-of-truth
helpers (`_shipped_preset_ops`, `_registry_syntax`, `_repo_target_*`), every
env/config knob reader (`_env_int`, `_env_float`, `_get_op_int`,
`_get_op_bool`, `_coerce_bool`), the mixed-tree guard (#678), the display
region (`plain_mode`, `mark`, `_display_safe`, the shared line-break
helpers), the exclude-paths list and matcher (`_get_exclude_paths`,
`_is_excluded`), gitignore integration (`_gitignore_enabled`,
`_git_ignored_dirs`), and rtk (Rust Token Killer) integration
(`_rtk_enabled`, `_has_rtk`, `_rtk_run`, `_rtk_output_looks_malformed`).

`_which_excluding_cwd` (used by this file's `_has_rtk`) is NOT part of this
split: it stays in `_supertool.py` core (not yet split into any part as of
this commit) because it has a second caller, `_has_ctags`, far outside this
region, and is one of three duplicated copies of the same cwd-excluding
algorithm (the others in `validators/common/spawnable.py` and
`presets/_spawnable.py`) kept in sync by
`tests/test_bare_spawn_cwd_gate_2596.py`. Both calls resolve fine at runtime
regardless of which part/core file defines which name, since every part
shares `_supertool.py`'s own globals() once loaded.
"""
from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_config.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )

# Default exclude-paths applied to all traversal ops (glob, grep, tree, map).
# Directories are pruned at the walk boundary — they are never opened. Files
# are dropped from the result, and dropping one *silently* is the thing this
# list must not do — see `_hidden_suffix` and `_is_disclosable_exclusion`.
#
# Three entry shapes, all honoured by `_is_excluded`:
#   "name/"   literal — a DIR or a FILE of that name. A single segment matches
#             at any depth; a multi-segment path is anchored to cwd.
#   "*.pem"   glob — fnmatched against the basename. Needed for shapes that are
#             not a fixed name (`id_rsa*`, `*.pem`).
#   "!name"   negation — un-excludes what it matches and wins over every other
#             entry, whatever the order.
#
# Split in two because the *disclosure count* distinguishes them, not because
# matching does: `_is_excluded` is handed the concatenation and cannot tell
# them apart. Noise is skipped in silence; a credential file is skipped and
# counted (#691).

# Build output, caches and VCS metadata. Deliberately NOT counted: nobody
# searching a repo meant these, they are documented, and a counter that fires
# on them is a number that is never zero — which is noise, not disclosure.
_NOISE_EXCLUDE_PATHS: Tuple[str, ...] = (
    ".git/", "node_modules/", ".svn/", ".hg/", ".idea/", ".vscode/",
    "__pycache__/", ".venv/", "venv/", "dist/", "build/",
    "phpstan-result-cache/", ".phpunit.cache/", ".rector/",
)

_SECRET_EXCLUDE_PATHS: Tuple[str, ...] = (
    # #146 / #691: credential dirs and files, kept out of glob/grep/tree/map so
    # a token cannot land in an LLM context as a side effect of a search nobody
    # aimed at it. #146 added the file entries below and documented that the
    # trailing slash covered files; for two years nothing called `_is_excluded`
    # on a file, so it did not. #691 wired it up.
    #
    # The boundary is deliberately narrow. A file earns a place here only when
    # holding a credential is its entire purpose: an exact name (`.netrc`) or an
    # unambiguous key-file shape (`*.pem`). No name-fragment heuristics —
    # `*secret*`, `*token*`, `*password*` hit source and test files constantly,
    # and a search that silently skips your own code is a worse failure than
    # the one this list exists to prevent.
    #
    # Directories.
    ".max/", ".ssh/", ".aws/", ".gnupg/", ".kube/", ".docker/",
    ".terraform/", ".chef/", ".npm/", "secrets/", "credentials/",
    # Environment files. `.env.*` covers `.local`, `.production`, `.staging`
    # and whatever a project invents next. The negations keep the committed
    # placeholders greppable — people read those to learn which keys exist,
    # and hiding them is the over-broad direction of this same defect.
    ".env/", ".env.*",
    "!.env.example", "!.env.sample", "!.env.template", "!.env.dist",
    "!.env.defaults", "!.env.schema",
    # Tool credential files.
    ".netrc/", "_netrc/", ".npmrc/", ".pypirc/", ".git-credentials/",
    ".pgpass/", ".my.cnf/", ".htpasswd/", ".dockercfg/",
    # Private keys and keystores.
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.ppk",
    # Supertool's own documented cwd token files (see presets/*/_auth.py). The
    # `.bluesky-handle` and `.hashnode-publication-id` siblings are public
    # identifiers, not credentials, and stay visible.
    ".hashnode-token/", ".devto-token/", ".bluesky-app-password/",
)

# Matching sees one flat list; only the disclosure count reads the split.
_DEFAULT_EXCLUDE_PATHS: Tuple[str, ...] = (
    _NOISE_EXCLUDE_PATHS + _SECRET_EXCLUDE_PATHS
)
_NOISE_EXCLUDE_SET = frozenset(_NOISE_EXCLUDE_PATHS)
WILDCARD_CHARS = re.compile(r"[*?\[]")
# Patterns for lines that are "blank or comment-only" across common languages
_COMPACT_SKIP = re.compile(
    r"^\s*$"           # blank lines
    r"|^\s*//"         # PHP/JS/TS single-line comments
    r"|^\s*#"          # Python/shell comments
    r"|^\s*\*"         # Javadoc/PHPDoc continuation lines
    r"|^\s*/\*"        # block comment open
    r"|^\s*\*/"        # block comment close
    r"|^\s*<!--"       # XML/HTML comment open
    r"|^\s*--!?>"      # XML/HTML comment close (--> and the spec's --!>)
)

# Config file — .supertool.json in project root (or parent dirs)
_CONFIG: Dict[str, Any] | None = None
_CONFIG_CHECKED = False

# Files the loader had to skip, reported once on stderr by main(). A config it
# cannot read is skipped rather than fatal — but skipping in silence means the
# user's ops are simply absent with nothing on screen to connect that to a
# file, so the reason is kept and surfaced (#418).
_CONFIG_WARNINGS: List[str] = []

# Absolute path of the .supertool.json the loader actually used, or None when
# the walk up from cwd found nothing. The loader always knew this and threw it
# away, which left the dispatcher unable to tell "your config does not enable
# that" from "you are not in a project at all" — two different problems for the
# person reading the error (#614).
_CONFIG_PATH: str | None = None

# MCP server specs parsed from _CONFIG["mcp"] — populated by _load_config()
_mcp_specs: Dict[str, dict] = {}

# Supertool install directory (where supertool.py actually lives, following symlinks).
# Normalised to forward slashes so the directory survives `shlex.split(posix=True)`
# which would otherwise eat Windows backslashes as escape sequences when
# `{supertool_dir}` is substituted into validator / formatter / notifier cmd
# templates. Windows accepts forward-slash paths everywhere; POSIX is unaffected.
_INSTALL_DIR = os.path.dirname(os.path.realpath(__file__)).replace(os.sep, "/")


# Shipped presets, indexed op name -> preset name. Populated lazily.
_SHIPPED_PRESET_OPS: Dict[str, str] | None = None

# The same pass's `syntax` strings, op name -> syntax line, for names that
# declare one. Filled by `_shipped_preset_ops()` because it already has the
# parsed JSON open and throwing it away cost a second read of every preset
# (#1524). Absent key means the entry has no `syntax`, which is a real answer.
#
# Not a second cache with its own lifetime: it is cleared and refilled inside
# the `_SHIPPED_PRESET_OPS` rebuild, so it is exactly as fresh as its sibling
# and cannot outlive it. That matters because #1322 is precisely the defect of
# a lazily-built index whose claimed lifetime was not the one it had — and a
# separate dict at module scope, absent from `conftest.RESET_GLOBALS`, would
# survive the per-test reset that returns `_SHIPPED_PRESET_OPS` to None.
_SHIPPED_PRESET_SYNTAX: Dict[str, str] = {}


def _shipped_preset_ops() -> Dict[str, str]:
    """Map every op declared by a shipped preset to the preset that declares it.

    Read from ``presets/*.json`` next to supertool.py, so it describes the
    *installed build* rather than whatever the cwd happens to enable. That is
    what makes it usable as evidence: when this index holds ``gl-mr``, the op
    demonstrably exists in this binary and its absence from the current call is
    a fact about where the caller is standing, not about the tool (#614).

    Deliberately not a registry of every op that exists anywhere. A custom op in
    another project's .supertool.json is genuinely unknowable from here and is
    never guessed at.

    Cached, and only consulted on the unknown-op path and by ``ops`` — a normal
    call never opens these files.
    """
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
            # A preset we cannot read contributes nothing. Same rule as the
            # config loader: an unreadable file is an absence, never a fatal —
            # and an index missing one preset still beats no index at all.
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
            # Deliberately NOT filtered against _BUILTIN_OPS. That filter stood
            # in for "a preset must not shadow a built-in", which `dispatch`
            # already guarantees structurally — `_resolve_custom_op` is reached
            # only on the fallthrough, after every built-in branch has
            # declined. Meanwhile it made a built-in documented in a preset
            # (`presets/lsp.json`, #2025) invisible to this index, so the #614
            # hint answered "no such op" for an op this binary dispatches.
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
    """The op's own documented invocation form, or None when there is not one.

    Three states rather than two, per ``docs/validators.md`` "Declining instead
    of guessing": a syntax line, no registry entry at all (every built-in —
    `registry:paste` says so in as many words), and an entry that declares no
    `syntax` key. The last two both return None, because an empty string would
    render as a syntax line that teaches nothing, which is the shape of a clean
    answer standing in for an absent one.

    The loaded config first, then the shipped presets, because an override in
    `.supertool.json` is the form the caller will actually type here. Only
    consulted on the unknown-op path.
    """
    entry = (_load_config().get("ops") or {}).get(name)
    if isinstance(entry, dict):
        syntax = entry.get("syntax")
        if isinstance(syntax, str) and syntax.strip():
            return syntax.strip()
    _shipped_preset_ops()  # populates _SHIPPED_PRESET_SYNTAX
    return _SHIPPED_PRESET_SYNTAX.get(name)


# Ops that accept a repo target, op name -> how it is named. Populated lazily.
#   "op"      — honours a leading `repo:OWNER/NAME` op (#673)
#   "payload" — takes the target in its own payload instead
_REPO_TARGET_MODES: Dict[str, str] | None = None


def _repo_target_modes() -> Dict[str, str]:
    """Map every op that can be pointed at a repo to how it is pointed there.

    Read from the same shipped ``presets/*.json`` as ``_shipped_preset_ops``,
    for the same reason: it describes the installed build, so it is usable as
    evidence about what this binary can do rather than about what the cwd
    happens to enable.

    An op absent from this map cannot be repo-targeted at all. That absence is
    load-bearing — it is what lets a `repo:` op refuse a call it could only
    have affected half of, instead of being quietly dropped for the other ops.
    """
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
    """Ops that honour a leading ``repo:OWNER/NAME`` directly."""
    return {op for op, mode in _repo_target_modes().items() if mode == "op"}


def _repo_reachable_ops() -> set[str]:
    """Every op a ``repo:`` op may sit beside without refusing the call (#1909).

    Broader than :func:`_repo_target_ops`: a ``payload``-mode op does not read
    ``SUPERTOOL_REPO`` itself, but it is still repo-scoped, so ``repo:`` may
    now supply its payload's own repo field when the payload is silent, and
    the op refuses on its own when the two are both set and disagree — see
    ``presets/_repo_target.py``'s ``resolve_or_conflict``. Only an op absent
    from ``_repo_target_modes()`` altogether has no repo dimension at all,
    and that is the one case still refused outright, by :func:`_repo_refusal`.
    """
    return set(_repo_target_modes())


#: A repo target is a path made of project-path segments and nothing else.
#: Both forges draw from the same character set, so one pattern serves both.
#: This is checked here rather than in the preset because the value is
#: substituted into an API path (`projects/<target>`) and handed to a CLI that
#: attaches a live token — `gl-api` already refuses a path that names a host
#: for that reason (#1035), and a target is one more way to write a path.
# A leading '-' is refused (#1040, #1487): a value validated here can reach a
# shipped op as a POSITIONAL argv element to `gh`/`glab`, in the slot where a
# `-`-prefixed token is read as a flag rather than a value. The callee
# rejecting an unknown flag today is not this validator's guarantee to borrow
# -- a call site moving, or a flag name colliding, is all it takes for that to
# stop holding.
_REPO_SEGMENT_RE = re.compile(r"\A[A-Za-z0-9._][A-Za-z0-9._-]*\Z")


def _repo_target_platform(ops: List[str]) -> str | None:
    """Which forge the repo-targetable ops in *ops* belong to.

    ``"github"``, ``"gitlab"``, ``"mixed"``, ``"unknown"`` (a repo-targetable
    op whose declaring preset is not one of the shipped forges, e.g. a
    project-defined op -- #1487), or None when the call names no targetable
    op at all. Derived from the shipped preset each op is declared in, not
    from its name prefix — the manifest is the registry, and a prefix is a
    convention that can be broken without anything failing.

    The pre-pass needs this because the two forges do not accept the same
    target shape (#676): GitHub is exactly ``OWNER/NAME``, GitLab allows
    ``GROUP/SUBGROUP/PROJECT``. Loosening for both would weaken the GitHub
    check, which is the alternative #676 names and rejects.
    """
    presets = _shipped_preset_ops()
    modes = _repo_target_modes()
    # Every op that has a repo dimension at all, not only the ones that take
    # it from this op. A `payload`-mode write op reconciles `repo:` against
    # its own payload field rather than being refused here (#1909), but it is
    # still evidence of which forge the call is about — reading only
    # `op`-mode ops let a GitLab project path through a GitHub-only call with
    # the shape never mentioned, and the reader sent to the wrong fix.
    found = {presets.get(a.split(":", 1)[0], "")
             for a in ops if a.split(":", 1)[0] in modes}
    if not found:
        return None
    if len(found) > 1:
        return "mixed"
    # `""` here is a project-defined op: repo-targetable (present in `modes`,
    # scanned from the SAME shipped presets/*.json), but not one
    # `_shipped_preset_ops()` has an entry for. That used to collapse into the
    # same `None` this function returns for "no repo-targetable op in this
    # call at all" (`next(iter(found)) or None`) -- one checker, two different
    # causes, one silent answer. Named `"unknown"` instead (#1487): the shape
    # check still falls back to the generic >=2-segments rule either way --
    # there is no way to know which forge's stricter rule a project op wants
    # -- but the "could not classify" state is now its own value rather than
    # an accidental falsy fallthrough.
    sole = next(iter(found))
    return sole if sole else "unknown"


def _repo_shape_error(value: str, platform: str | None) -> str | None:
    """Why *value* is not a usable repo target for *platform*, or None.

    Three states rather than two: a shape this cannot vouch for is refused
    with the reason, never accepted with a shrug and encoded on the way out.
    """
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
    """Why this call cannot carry a ``repo:`` op alongside *op*.

    Only reached for an op absent from ``_repo_target_modes()`` altogether —
    no repo dimension at all. A ``payload``-mode op used to be refused here
    too, on the theory that "one place the target comes from" meant one
    *route*; #1909 narrowed that to one *resolved value* — the pre-pass now
    lets ``repo:`` sit beside a payload-mode op, exports it the same as for an
    ``op``-mode read, and the op itself reconciles the two (see
    ``presets/_repo_target.py``'s ``resolve_or_conflict``), refusing on its
    own only when both are set and disagree.
    """
    return (
        f"repo: {op!r} cannot be pointed at a repo, so a repo: op in this call "
        f"would apply to some ops and be silently ignored by this one. Drop "
        f"the repo: op, or give the repo-scoped ops a call of their own.\n"
    )


def _presets_not_loaded_here() -> List[str]:
    """Shipped preset names the active config does not enable, sorted."""
    config = _load_config()
    enabled = {p for p in (config.get("presets") or []) if isinstance(p, str)}
    return [p for p in sorted(set(_shipped_preset_ops().values()))
            if p not in enabled]


#: Line prefixes git writes into a file it could not merge. `|||||||` is the
#: diff3/zdiff3 style's base section and is here for the *line list*, not for
#: detection: every style emits `<<<<<<<` and `>>>>>>>`, so the conflict is
#: found either way. What it buys is that the list of lines to go and fix is
#: complete — one missing entry sends the reader back to the file.
_CONFLICT_MARKER_PREFIXES = ("<<<<<<<", "|||||||", "=======", ">>>>>>>")


def _skipped_config() -> Optional[Tuple[str, str]]:
    """(path, why) for a `.supertool.json` that is present and was not used.

    `_load_config` walks up from the cwd and *skips* any config it cannot
    parse, so when nothing above parses either, `_CONFIG_PATH` stays None. Both
    renders below then said "No .supertool.json was found" — one line under a
    stderr warning naming the file the same run had just skipped. Two lines of
    one render disagreeing about whether the config exists, and a reader who
    sees only the ERROR goes looking for a missing file that is present
    (#1162).

    The conflicted case is called out by name because it is circular: git
    writes markers into the file, the file stops parsing, and the ops that
    disappear are `git-conflicts`, `git-resolve` and `git-status` — precisely
    the ones for the situation that removed them. Naming the marker lines is
    the one fact `git-conflicts` would have supplied, from the code that can
    still supply it.

    Re-derived from disk rather than parsed back out of `_CONFIG_WARNINGS`:
    the warning is a formatted sentence, not a record, and this way the note
    describes the file as it is at message time and adds no module state.

    Returns None when there genuinely is no config — inventing a presence is
    the same defect mirrored.

    **Uncached, and repeated per unresolved op.** `_load_config` is guarded by
    `_CONFIG_CHECKED` and this is not, so `dispatch` calling
    `_unknown_op_message` once per unresolved op — and `batch` recursing per
    sub-op — means N walks for N unresolvable shipped-preset names in one call,
    where before there was a static string. That is a deliberate trade, not an
    oversight:

    * The walk cannot start unless `_CONFIG_PATH` is falsy, so every call from
      inside a project returns above on a cached `_load_config`. The cost is
      confined to an already-erroring path in a tree with no usable config, and
      `test_no_walk_happens_when_a_config_actually_loaded` is what keeps it
      there.
    * Caching it would need invalidating on `cwd:`, which retargets the working
      directory mid-call and has no hook to hang that on. A cached answer read
      after a chdir is a claim about the wrong tree — #678's shape, and a worse
      defect than the walk it saves.
    * The walk is bounded by the parent-chain depth (an `isfile` per level) and
      reads one file at most, at the moment the process is about to print an
      error and stop.

    If the repetition ever matters, the fix is to make `_unknown_op_message`
    take the note as an argument and have `dispatch` derive it once — not to
    cache this.

    The load is forced here rather than assumed of the caller. `_CONFIG_PATH`
    is None both when no config loaded *and* when none has been attempted yet,
    and `_unknown_op_message` reads it without triggering one — in a dispatch
    the config is always loaded first, by `_resolve_custom_op`, but a direct
    call is not a dispatch. Without this, a perfectly good `.supertool.json`
    was reported as "found and could not be loaded": the absence-produced-by-
    the-tool defect, inside the fix for it.
    """
    _load_config()
    if _CONFIG_PATH:
        return None
    d = os.path.abspath(os.getcwd())
    while True:
        candidate = os.path.join(d, ".supertool.json")
        if not os.path.isfile(candidate):
            # The name exists and is not a regular file. `_load_config` gates
            # on `isfile` too, so it walks straight past — the right behaviour
            # for a loader and the wrong sentence for a reader: "no
            # .supertool.json was found" about a name sitting in the cwd is
            # this issue's own defect, one `stat` over. `lexists` rather than
            # `exists`, so a dangling symlink is reported instead of resolving
            # to nothing and reading as absent.
            if os.path.isdir(candidate):
                return (candidate, "is a directory, not a file")
            if os.path.lexists(candidate):
                return (candidate, "is not a regular file")
        else:
            try:
                # errors="replace" on purpose: a config that is not UTF-8 is
                # one more way to be unusable, and raising here would take
                # down the message written to explain the failure.
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
            # Parsed on a second look, and the reason is deliberately NOT
            # named. Two different histories land here: the loader failed
            # downstream of the parse (a preset file, the mcp block), or its
            # strict-UTF-8 read raised `UnicodeDecodeError` on bytes this
            # re-read replaced. Naming either one would be a guess, and the
            # displayed string is the only claim that holds for both.
            return (candidate, "was found and could not be loaded")
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _preset_disclosure() -> str:
    """One line naming the presets that are not loaded here — never their ops.

    ``ops`` from a non-project directory listed the file ops and stopped, and a
    reader takes that as the tool's whole capability (#614 — its filer did).
    Enumerating the hidden ops would roughly double the listing and get eaten
    from the tail by the SessionStart cap, so this names presets and a count and
    leaves ``cwd:`` as the way through. Empty string when nothing is hidden: the
    absence of the line is itself the signal that the listing is complete.
    """
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
        # Found and unusable is not absent (#1162). Naming `cwd:` here would
        # be worse than saying nothing: the config that is broken is the one
        # in the tree the caller is standing in.
        path, why = skipped
        return (f"> Built-in ops only. {path} {why}, so {len(missing)} shipped "
                f"presets ({names}) — {n_ops} ops — are not loaded here. Repair "
                f"that file and they come back.")
    return (f"> Built-in ops only. No .supertool.json was found from {os.getcwd()}, "
            f"so {len(missing)} shipped presets ({names}) — {n_ops} ops — are not "
            f"loaded here. Run from a project that enables them, or make the first "
            f"op 'cwd:<project-path>'.")


#: Presets `.github/release-branch.json`'s `deny` list strips out of the
#: directory build (#2734): each one reads a credential and sends it to its
#: own vendor's API, the shape the Anthropic directory's `MCP_FORWARDS_CREDENTIAL_ENV`
#: hold fires on (#2732). `dpt-plugins` keeps shipping all of them from
#: `master`; only the directory tree omits the files. A name landing here
#: must also appear in `.github/release-branch.json`'s `deny` list (and, for
#: a watch source rather than a preset proper, under `presets/watch/sources/`)
#: or this message would describe a build that does not actually ship this
#: way. Checked against that file by `tests/test_release_branch_build_2705.py`
#: rather than duplicated there -- a second copy of this list is the defect
#: this comment exists to prevent.
_DIRECTORY_BUILD_EXCLUDED_PRESETS = {"bluesky", "devto", "hashnode", "slack", "youtube"}


def _find_preset_file(name: str, project_dir: str) -> str | None:
    """Find a preset JSON file by name, checking three locations in order.

    Resolution order:
    1. {project_dir}/presets/{name}.json   — project-level
    2. ~/.config/supertool/presets/{name}.json — user-level
    3. {supertool install dir}/presets/{name}.json — shipped
    """
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
    """Substitute {name} placeholders in a single left-to-right pass.

    Text inserted by a substitution is never rescanned, so an argument VALUE
    that happens to contain a placeholder token stays literal. Chained
    str.replace calls did rescan, which let a value expand a later
    placeholder inside itself and break the command's shell quoting.

    Unknown names are left untouched (e.g. {path}, resolved earlier by
    _resolve_preset_cmd).
    """
    return _PLACEHOLDER_RE.sub(
        lambda m: values[m.group(1)] if m.group(1) in values else m.group(0),
        template,
    )


def _resolve_preset_cmd(cmd: str, preset_dir: str) -> str:
    """Replace {path} placeholder with the preset's directory (trailing slash).

    Example: 'python3 {path}gitlab/issue.py {arg}'
    becomes: 'python3 /home/user/.local/supertool/presets/gitlab/issue.py {arg}'

    Normalises preset_dir to forward slashes — the cmd template flows through
    `shlex.split(posix=True)` which would otherwise eat Windows backslashes
    as escape sequences. Forward slashes work on every platform.
    """
    path_prefix = preset_dir.replace(os.sep, "/").rstrip("/") + "/"
    return cmd.replace("{path}", path_prefix)


#: Distinguishes "the project supplied no override" from "it supplied None".
_UNSET = object()


def _merge_op_def(base: Any, override: Any) -> Any:
    """The one rule for a project op-def landing on a preset op-def (#1356).

    Dict over dict merges key-by-key, so a project override can add or replace
    individual keys without restating the preset's `cmd`. Anything else
    replaces wholesale.

    Extracted from `_merge_presets` because the rule had escaped it. Three ops
    in this repo's own `.supertool.json` — `dashboard`, `radar`, `git-diff` —
    are partial overrides carrying one config key and no `cmd` or `syntax` at
    all, and a hand-rolled walk writing the obvious `ops[name] = entry`
    replaced the shipped definition with the stub. The op count is unchanged
    either way, so nothing looks wrong; the entry simply stops matching every
    filter downstream. #1350's containment audit lost `git-diff` — the op it
    was written about — out of its own population and printed a pass.

    So: one function, and `_op_registry` renders what it produced rather than
    recomputing it.
    """
    if _merges_key_by_key(base, override):
        merged = dict(base)
        merged.update(override)
        return merged
    return override


def _merges_key_by_key(base: Any, override: Any) -> bool:
    """Will this override merge into the base, or replace it outright?

    A predicate rather than an inlined `isinstance` pair because two callers
    ask it and they must never disagree: `_merge_op_def` decides what lands in
    the registry, `_record_op_sources` decides what the registry says happened.
    The first version asked only whether the *override* was a dict, so a dict
    landing on a preset that ships its op as a bare cmd string was recorded as
    a key-by-key merge — attributing surviving keys to a definition that had
    been replaced entirely.
    """
    return isinstance(base, dict) and isinstance(override, dict)


def _record_op_sources(config: Dict[str, Any],
                       op_presets: Dict[str, str],
                       preset_bases: Dict[str, Any],
                       project_ops: Dict[str, Any],
                       merged_ops: Dict[str, Any]) -> None:
    """Stamp where each merged op came from, for `_op_registry` to read.

    Written by the loader during the one walk that does the merging, never
    recomputed: a second walk is a second copy of the rule, which is the whole
    defect (#1356). Its presence is also the signal that provenance is *known* —
    a config that never passed through here reports "unknown", not "project".
    """
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
            # The shipped definition was replaced outright — either side being
            # a non-dict does it — so there is no per-key answer to give.
            # None, never [] — an empty list means "merged, changed nothing",
            # which is a different and true thing about a `{}` override.
            overridden = None
        sources[name] = {"preset": preset, "project": True,
                         "overridden": overridden}
    config["_op_sources"] = sources


#: Keys `_resolve_custom_op`'s launcher never exports as `SUPERTOOL_<KEY>` —
#: its own control fields, not config for the subprocess to read (#692,
#: #1347, #1357, #1672, #1675, #1757). `safety` and `repo_target` join the
#: list for #1757: both are metadata the CORE reads off the merged registry
#: entry to decide how to treat the op before ever spawning it (safety
#: classification; whether a leading `repo:OWNER/NAME` token is honoured) —
#: never something the subprocess itself consults. A tree-wide grep for
#: either env var name (`SUPERTOOL_SAFETY`, `SUPERTOOL_REPO_TARGET`) inside
#: a preset returns zero, the same signature `replaces`/`paths`/`exitStatus`
#: each had before they were added here. Module-level and shared with
#: `_op_config_key_collisions` below so the two can never disagree about
#: which keys actually become an env var: a set redeclared at each site would
#: let one drift and the other not, and the drift would be silent — exactly
#: the defect class this whole file exists to remove.
_OP_CONFIG_RESERVED_KEYS = {
    "cmd", "timeout", "description", "syntax", "example", "status",
    "restartMcp", "replaces", "paths", "exitStatus", "form", "hint",
    "safety", "repo_target",
}

#: Extra config keys whose value is a search path -- one or more directories
#: joined by `os.pathsep` -- resolved against the directory of the
#: `.supertool.json` that declared them, rather than exported verbatim (#2164).
#:
#: `presets/watch/sourcepath.CONFIG_KEY` is the one entry today. Anchoring
#: happens HERE, once, before the value is exported as an env var -- never
#: inside the preset that reads it back, which may be a detached, re-exec'd
#: poller with no reliable notion of "the directory this was typed in" (the
#: CWD argument `sourcepath.resolve()`'s own docstring makes, and still makes,
#: about a relative entry resolved late). The config file does not move, so
#: resolving once here and exporting an absolute path is what the poller
#: inherits and re-derives after any number of re-execs, unchanged.
_RELATIVE_SEARCH_PATH_KEYS = {"watch_sources_path"}


def _anchor_relative_search_path(raw: str, config_path: str) -> str:
    """Resolve each relative entry of a pathsep-joined search path `raw`
    against the REAL directory of `config_path`.

    An already-absolute entry is left exactly as declared, minus surrounding
    whitespace -- this only adds information, never removes it, so a value
    that was already fully usable is what it was before #2164. An entry that
    is empty (an operator's stray separator) is left alone too, for the same
    reason `sourcepath.resolve()` treats it as nothing declared rather than a
    refusal: there is nothing here to anchor.

    Each entry is stripped before its `isabs()` check, matching
    `sourcepath.resolve()`'s own `raw_entry.strip()` -- checking the
    UNSTRIPPED text would call a whitespace-padded absolute entry relative
    and join the anchor directory onto it, producing a path that exists
    nowhere and silently dropping the operator's real directory (review
    finding on #2164's own PR).

    The anchor is `os.path.dirname(os.path.realpath(config_path))`, not a
    plain `os.path.dirname` -- the same normalisation the three other sites
    in this file that derive "the directory containing the declaring config"
    already apply (`_mixed_tree_pair`, and two more; all key off
    `os.path.realpath(_CONFIG_PATH)`). `_CONFIG_PATH` is built from
    `os.path.abspath(os.getcwd())`, not `os.path.realpath`, so a symlink
    component in the path a future caller sets it from would otherwise let
    this function's anchor diverge from every other reader of the same
    config path in this file (review finding on #2164's own PR).
    """
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
    """Env var names two DIFFERENT project-declared ops would both export (#1009).

    `ops.<op>.<key>` reaches that op's own subprocess as `SUPERTOOL_<KEY>` —
    the key alone, never namespaced by the op that declared it. Nothing stops
    a second op from picking the same key name for an unrelated setting, and
    the failure when that happens was silent: neither op errors, neither
    warns, and whichever op runs reads whatever value its OWN project entry
    happens to carry for that key. A live instance shipped as
    `hashnode_react.auto_force` / `hashnode_comment.auto_force`, two separate
    documented opt-ins, both landing on `SUPERTOOL_AUTO_FORCE` (#1009,
    2026-08-13 comment).

    Scoped to `project_ops` — the project's OWN "ops" section in
    `.supertool.json` — never to a preset's shipped defaults. A preset
    deliberately sharing one key across many ops on purpose (`REPO_TARGET`
    across every `gh-*`/`gl-*` op that targets a repo, `DEFAULT_LIMIT` across
    every list op) is the same shape as `SUPERTOOL_GIT_TIMEOUT`: one name,
    several readers, by design. Flagging that would refuse most of this
    repo's own shipped presets for behaviour that has always been correct —
    measured: 10 of the 18 non-reserved keys shipped presets declare are
    already read by 2+ ops on purpose.

    Same name is not enough even within `project_ops` — a first version of
    this checked only that, and `supertool 'ops:roster'` against this very
    repository's own `.supertool.json` immediately named `SUPERTOOL_WATCH_NAME`
    a collision: `channel`, `radar`, `unwatch`, `watch` and `watches` all
    declare `watch_name: "oss-supertool"`, one identifier repeated on purpose
    so the whole `watch` family points at the same channel. Refusing that
    would have broken every `watch` op in this repository's own config the
    moment this landed — exactly the breaking change this fix exists to
    avoid causing. So the signal is not "two ops named it the same"; it is
    "two ops named it the same AND disagree about the value" — the hashnode
    case (`auto_force: true` vs `auto_force: false`) disagrees, `watch_name`
    repeated verbatim does not. Values are compared via `json.dumps` with
    sorted keys so a dict or list config value compares by content, not
    identity; a value that cannot be JSON-encoded compares by `repr` rather
    than silently dropping out of the check.

    Returns `{env_var_name: [op_name, ...]}` for every name where two or more
    ops declare GENUINELY DIFFERING values, each list sorted and
    deduplicated and naming every op that touches the name — including one
    that happens to agree with another, so an operator fixing the disagreement
    sees the whole group rather than only the pair that disagrees. Empty when
    nothing collides, which covers both "only one op uses this key" and
    "several agree on one value".
    """
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
    for env, pairs in by_env.items():
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
            collisions[env] = ops_here
    return collisions


def _merge_presets(config: Dict[str, Any], project_dir: str) -> None:
    """Load and merge preset ops into config. Project ops win on conflict."""
    presets = config.get("presets")
    project_ops = config.get("ops", {})
    if not isinstance(project_ops, dict):
        project_ops = {}
    # Computed here, once, from `project_ops` alone — before any preset is
    # merged in — and stashed on every return path below (#1009). Anything
    # a preset contributes is deliberately out of scope; see the docstring.
    config["_op_config_collisions"] = _op_config_key_collisions(project_ops)
    if presets is not None and not isinstance(presets, list):
        # Declared and unusable is not the same as not declared. Nothing gets
        # merged either way, but here the config asked for ops that are now
        # absent — so the registry must not go on to report a complete set.
        config.setdefault("_preset_warnings", []).append(
            f'"presets" must be a list of preset names, got '
            f"{type(presets).__name__} — no preset ops were merged")
        _record_op_sources(config, {}, {}, project_ops, project_ops)
        return
    if not presets:
        # No presets to merge, so every op is the project's own — provenance
        # is fully known and must be stamped, or `_op_registry` cannot tell
        # this from a config that never reached the loader at all.
        _record_op_sources(config, {}, {}, project_ops, project_ops)
        return

    merged_ops: Dict[str, Any] = {}
    op_presets: Dict[str, str] = {}

    for name in presets:
        if not isinstance(name, str):
            continue
        preset_path = _find_preset_file(name, project_dir)
        if preset_path is None:
            # Store warning in a list so callers can report it. A name on
            # `_DIRECTORY_BUILD_EXCLUDED_PRESETS` is not a typo -- it is
            # genuinely absent from a directory install on purpose (#2734),
            # and a flat "not found" reads like the config is broken rather
            # than like the install is deliberately smaller than `master`.
            if name in _DIRECTORY_BUILD_EXCLUDED_PRESETS:
                config.setdefault("_preset_warnings", []).append(
                    f"preset {name!r} is not in this build (directory "
                    f"install); install supertool@dpt-plugins for it"
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
            # UnicodeDecodeError is a ValueError, not an OSError, so it used to
            # slip this clause entirely: presets/git.json holds — and ✓, which
            # an ASCII locale cannot decode, and supertool died at startup with
            # a traceback over a file we ship (#418). Named rather than widened
            # to Exception — the merge below can raise TypeError or KeyError on
            # a malformed op-def, and that is a bug here that must stay loud.
            config.setdefault("_preset_warnings", []).append(
                f"preset {name!r}: failed to load {preset_path} "
                f"({exc.__class__.__name__}: {exc})"
            )
            continue

        preset_dir = os.path.dirname(preset_path)

        # A preset may also document ops it does NOT define: built-ins, whose
        # code is in this file and whose behaviour no manifest can change
        # (#2025). Those go in the manifest's own `builtin-ops` section and
        # merge into the config's, never into `ops`.
        #
        # The section matters, it is not bookkeeping. Four independent guards
        # sweep `config["ops"]` on the premise that a preset op is one the
        # preset *defines* — every entry resolves to a script (#1269), declares
        # a safety class (#1231), declares a path-containment boundary (#1287,
        # #1350), and is accounted for by the `replaces` census (#1384). A
        # doc-only entry satisfies none of those and must not: its script is
        # this module, its class is `_OP_SAFETY_BUILTIN`, its paths go through
        # the built-in chokepoint. Putting it in `ops` would mean teaching four
        # sweeps an exception apiece; putting it here means none of them ever
        # sees it, and the doc surfaces that read `builtin-ops` — `op_ops`,
        # `_help_entry`, `_configured_op_names`, `_build_at_file_registry` —
        # all reach it with no change at all.
        preset_builtin_docs = preset_data.get("builtin-ops")
        if preset_builtin_docs is not None and not isinstance(
                preset_builtin_docs, dict):
            # The whole `builtin-ops` SECTION being a non-table is the same
            # #2308 defect one level further up than the per-entry check
            # below: a preset manifest declaring `"builtin-ops": "oops"`
            # used to skip this whole branch silently, dropping the section
            # exactly as if the preset had never declared one at all
            # (oss:auditor's re-spawned self-review finding, since neither
            # the per-entry check nor `_preset_warnings` ever saw it).
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
                # The project's own config wins, by the same rule `ops` uses
                # — and key-for-key, through the same helper, for the same
                # reason: `builtin-ops` entries carry behaviour overrides
                # (`read.max_lines`, `grep.extensions`) as well as prose, so a
                # project setting one key would otherwise delete the preset's
                # whole entry and leave the op undocumented. That is #1356's
                # stub, one section over.
                project_entry = (config.get("builtin-ops") or {}).get(doc_name)
                merged_entry = (
                    _merge_op_def(doc_def, project_entry)
                    if project_entry is not None else doc_def)
                merged_builtin_docs[doc_name] = merged_entry
                if not isinstance(merged_entry, dict):
                    # The same defect `_get_op_int`/`_get_op_bool` fixed at the
                    # reader (#1332/#654) and `registry:OP` fixed at the
                    # disclosure surface (#2079), one layer further upstream:
                    # a `builtin-ops.<op>` entry that is not a table used to be
                    # stored here unchecked, with nothing recorded anywhere.
                    # Every runtime reader then fell back to its own default,
                    # identically to the key never having been set at all —
                    # this repository's own signature defect, an absence the
                    # tool produced read as an absence in the world.
                    config.setdefault("_preset_warnings", []).append(
                        f"preset {name!r}: builtin-ops.{doc_name} is "
                        f"{type(merged_entry).__name__}, not a table "
                        f"— every runtime reader will fall back to its "
                        f"default as if this key were never set"
                    )
                # Stamped by the loader, for the same reason `_op_sources` is:
                # "did this preset contribute anything?" is asked against the
                # provenance, never by re-walking the manifests, and a preset
                # whose whole contribution is documentation contributed. Kept
                # out of `_op_sources` itself, which answers about ops that
                # exist in `config["ops"]` — a row there for a name the
                # registry does not hold would be provenance for nothing.
                doc_sources = config.setdefault("_preset_doc_contributions", {})
                doc_sources.setdefault(name, []).append(doc_name)
            config["builtin-ops"] = merged_builtin_docs

        preset_ops = preset_data.get("ops", {})
        for op_name, op_def in preset_ops.items():
            # Resolve script paths relative to where the preset JSON lives
            if isinstance(op_def, dict) and "cmd" in op_def:
                op_def = dict(op_def)  # don't mutate original
                op_def["cmd"] = _resolve_preset_cmd(op_def["cmd"], preset_dir)
            elif isinstance(op_def, str):
                op_def = _resolve_preset_cmd(op_def, preset_dir)
            merged_ops[op_name] = op_def
            # Last preset wins the definition, so it wins the attribution too.
            # Preset-vs-preset is a wholesale replace, not a key merge.
            op_presets[op_name] = name

    # Project-level ops override preset ops, by the one shared rule. The
    # pre-override state is kept: whether an override merged or replaced is a
    # fact about the base it landed on, and after the loop that base is gone.
    preset_bases = dict(merged_ops)
    for op_name, op_def in project_ops.items():
        merged_ops[op_name] = _merge_op_def(merged_ops.get(op_name), op_def)
    config["ops"] = merged_ops
    _record_op_sources(config, op_presets, preset_bases, project_ops,
                       merged_ops)


def _config_trust_violation(candidate: str) -> Optional[str]:
    """Refuse a config file that is not the caller's own to control (#695).

    `.supertool.json` is executable data — `ops.<name>.cmd` runs a shell
    command the moment that op is invoked (see the "no execution at load
    time" contract in ``test_security_config.py``, which is the *only*
    thing that contract ever promised: it never claimed the file itself is
    safe to be writable by anyone else). A group- or world-writable config,
    or one owned by a different user than the one running supertool, can be
    rewritten by another local account between the moment it was reviewed
    and the moment an op it declares actually runs — the exact TOCTOU shape
    `ssh` refuses on `~/.ssh/config` and git refuses (via `safe.directory`)
    on a repo owned by someone else.

    POSIX-only: `st_uid` and the group/other write bits are meaningless on
    Windows (`os.stat().st_uid` is always 0 there), so this returns `None`
    — trusted — unconditionally on that platform rather than fabricate a
    check with no signal behind it.

    Returns the reason to skip, or `None` when the file is safe to read.
    Root (`st_uid` of the file owned by root, or the caller running as
    root) is treated as trusted, matching git's own `safe.directory` carve
    out for a root-owned tree.
    """
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


def _load_config() -> Dict[str, Any]:
    """Load .supertool.json from cwd or parents, stopping at the nearest
    ``.git`` ancestor. Cached.

    After loading, merges any preset ops declared in "presets" key and
    parses the optional "mcp" block into the module-level _mcp_specs dict.

    Trust boundary (#695): a config in this project is exactly as trusted
    as the project's own code — cloning a hostile repo and running any op
    already executes its validators, ops and presets, so a project-local
    `.supertool.json` adds no new attack surface *for the project that owns
    it*. What it must not do is reach OUTSIDE that project: before #695 the
    walk went all the way to `/` with no ownership check, so opening a
    subdirectory of an otherwise-untrusted tree (a shared `/tmp` extraction,
    a CI checkout dir with a stray ancestor config) could silently pick up
    a config that governs nothing the user actually opened. Two independent
    limits now apply, either one enough to stop a candidate that only the
    first check used to gate:

    * the walk itself stops once it reaches a directory containing `.git`
      — the repo root — rather than continuing past it; a caller not
      inside a git repo at all keeps the old to-`/` behaviour, since there
      is no repo boundary to stop at;
    * each candidate is checked with `_config_trust_violation` before it is
      opened — a file that is group/world-writable, or not owned by the
      caller (or root), is skipped exactly like a malformed one, with the
      reason recorded in `_CONFIG_WARNINGS` rather than silently ignored.
    """
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
                    # JSON `null` parses to None; bare scalars / lists parse
                    # to non-dict. _merge_presets needs a dict — coerce to
                    # empty to keep the rest of the loader honest.
                    if not isinstance(_CONFIG, dict):
                        # Adjacent to #2308/#2306, same signature defect one
                        # layer higher: this used to coerce to `{}` with
                        # nothing recorded, rendering a `.supertool.json`
                        # that parses but is a JSON array/string/number
                        # byte-identical to no config file existing at all.
                        # `presets/_publish_safety._supertool_config` fixed
                        # the identical shape for its own loader in this
                        # same commit (#2306); this is the sibling fix for
                        # the loader every other op goes through.
                        _CONFIG_WARNINGS.append(
                            f"{candidate} does not hold a JSON object (got "
                            f"{type(_CONFIG).__name__}) — ignoring it"
                        )
                        _CONFIG = {}
                    project_dir = d
                    _CONFIG_PATH = candidate
                    _merge_presets(_CONFIG, project_dir)
                    # Parse MCP server specs from the optional "mcp" block.
                    mcp_block = _CONFIG.get("mcp")
                    if isinstance(mcp_block, dict):
                        for srv_name, spec in mcp_block.items():
                            if isinstance(spec, dict) and "cmd" in spec:
                                _mcp_specs[srv_name] = spec
                    return _CONFIG
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
                # Skip and keep walking up, which is what this loop has always
                # done with a config it cannot use — but record why. A config
                # that is not UTF-8 raises UnicodeDecodeError, a ValueError,
                # which escaped the old clause and took startup down for every
                # op including the ones that never needed the config (#418).
                # Not `except Exception`: this try also covers _merge_presets
                # and the mcp block, and a TypeError out of either is a bug
                # that must not be swallowed as "bad config file".
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
    """(core dir, other checkout) when two supertool trees answer one call (#678).

    `_load_config()` walks up from **cwd**, and `_find_preset_file` looks in
    `{project_dir}/presets/` first. So the config, the preset JSONs and the
    scripts they point at all come from wherever the caller is standing, while
    the core that parsed the ops came from the file that was invoked. Run a
    branch worktree's `supertool.py` from a master checkout and you get branch
    core + master presets in one process, with nothing on the receipt saying so
    — the code under test never executes and the answer still says `PASS`.

    The signal is deliberately narrow: the resolved project root is *itself a
    different supertool checkout*. The cheaper "the invoked supertool.py is not
    under the project root" was considered and rejected — that is the documented
    install (a clone symlinked onto `$PATH`, used from arbitrary project roots),
    so it would fire on essentially every legitimate invocation and teach
    everyone to ignore it. A project root that merely ships its own `presets/`
    is not a mix either: overriding a shipped preset is a documented feature.

    Uncached — two `stat` calls, and a cached verdict is one more thing to go
    stale in a reused daemon process (#680).

    The peer is compared by *directory*, not by file identity. Since #931 the
    invoked entry point is `supertool.py` while this code lives in
    `_supertool.py`, so `realpath(peer) == realpath(__file__)` can never hold
    and the check would report a mix on every single invocation made from
    inside any supertool checkout — including this one's own test suite.
    Same-install and same-directory are the same fact here, and the directory
    is the one that survives the split.
    """
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
    """True when the caller has declared the mix deliberate via env."""
    return (os.environ.get(_MIXED_TREE_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on")


def _mixed_tree_note(pair: Tuple[str, str]) -> str:
    """One line naming both trees — used on stderr and on the stamped receipt."""
    core, other = pair
    return f"mixed supertool trees: core={core}/supertool.py presets={other}"


def _mixed_tree_decline(op: str, pair: Tuple[str, str]) -> str:
    """The third state for an op whose provenance is unknown (#678, #1942).

    Not a finding — nothing was found wrong with the op. An absence: the tool
    cannot say which version would answer, so it says that instead of printing a
    `PASS` indistinguishable from one the invoked build produced. Same contract
    as `docs/validators.md` §"Declining instead of guessing".

    Reused for two different callers: a config/preset op reached through
    `_resolve_custom_op` (which shells out — the wrong tree's *script* would
    have run), and, since #1942, a write-class builtin reached from
    `_dispatch_impl`'s own chokepoint (no subprocess — the wrong tree's
    *validators, formatters and hooks* would have run instead). `op` and
    `pair` say which call and which two trees either way; nothing below
    names which caller it was, on purpose, since the remedy is identical.

    The remedy branches on `op`'s own safety class (#2432). `cwd:{core}` is
    genuinely safe advice for a read-only op -- it only needs `core`'s
    presets/config to answer correctly, and nothing in `core` is touched. It
    is dangerous advice for anything else: `cwd:PATH` really does
    `os.chdir(PATH)` for the rest of the call (mirrors `cd PATH && ...`), so
    a write that follows `cwd:{core}` runs against `core`'s own checked-out
    branch -- not `other`'s -- with no further decline, because the mismatch
    that triggered THIS decline is exactly what `cwd:` just erased. A caller
    who wanted to push a worktree's branch and instead pushed the main
    clone's currently-checked-out branch is that bug, observed (#2432).
    """
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
    """Check if compact mode is enabled in .supertool.json."""
    return bool(_load_config().get("compact", False))


def _notifier_debug_enabled() -> bool:
    """Env SUPERTOOL_NOTIFIER_DEBUG=1 wins over JSON `notifier_debug: true`."""
    env = os.environ.get("SUPERTOOL_NOTIFIER_DEBUG")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    return bool(_load_config().get("notifier_debug", False))


def _notifier_debug_log_path() -> str:
    """Override via SUPERTOOL_NOTIFIER_DEBUG_LOG; default /tmp/supertool-notifier-debug.log."""
    return os.environ.get("SUPERTOOL_NOTIFIER_DEBUG_LOG") or "/tmp/supertool-notifier-debug.log"


def plain_mode() -> bool:
    """True when ASCII-only output is requested via --plain or SUPERTOOL_PLAIN.

    Hooks, ``grep``, and CI parse op output with no UTF-8 / locale guarantees.
    The ``⚠``/``✓`` glyphs are nice UX for the model but a liability downstream:
    a C/POSIX-locale ``grep`` won't reliably match a multibyte glyph, and a
    cp1252 console crashes on it. Plain mode swaps every glyph for an ASCII
    marker (``[WARN]``/``[OK]``/``[FAIL]``) so machine consumers parse reliably.

    Set by the ``--plain`` CLI flag (which exports ``SUPERTOOL_PLAIN=1`` so it
    reaches preset subprocesses too) or directly via ``SUPERTOOL_PLAIN=1``.
    """
    return os.environ.get("SUPERTOOL_PLAIN", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


# Glyph → ASCII marker map. Keys are the rich-mode glyphs; values the ASCII
# fallback emitted in plain mode. Centralised so every call site routes through
# mark() rather than hard-coding a glyph and a separate plain branch.
_PLAIN_MARKERS = {
    "⚠": "[WARN]",  # ⚠
    "✓": "[OK]",    # ✓
    "✗": "[FAIL]",  # ✗
    "ℹ": "[INFO]",  # ℹ
    "↳": "->",      # ↳ — sub-line continuation
}


def mark(glyph: str) -> str:
    """Return ``glyph`` in rich mode, or its stable ASCII marker in plain mode.

    Unknown glyphs pass through unchanged so callers can't silently emit a
    non-ASCII character that plain mode was supposed to strip.
    """
    if plain_mode():
        return _PLAIN_MARKERS.get(glyph, glyph)
    return glyph


def _reconfigure_stdout_utf8() -> None:
    """Force stdout/stderr to UTF-8 so ops never crash on a non-UTF-8 console.

    Windows defaults stdout to cp1252, which can't encode the glyphs ops print
    and raises ``UnicodeEncodeError`` (returncode 1) — caught in CI on git-diff.
    This is cheap insurance even when plain mode is on (a stray glyph in user
    content shouldn't crash the process). No-op on Pythons / streams without
    ``reconfigure`` (< 3.7 or wrapped streams).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


# `errors="replace"` (#501) leaves U+FFFD wherever a child process's output was
# not valid UTF-8. Where supertool only *displays* that output, mojibake is the
# right trade against a traceback that lands after half the answer is already
# on screen — that is #498's lesson and it is why the sweep is otherwise
# uniform. Where the decoded text becomes something else, it is not: bytes
# written back into the user's file, or a path handed to the filesystem, turn a
# crash into a wrong answer, which this repository has rated the worse failure
# every time it has come up (#414, #445, #454, #459, #477, #482, #345, #487,
# #263). Those seams call this and name what happened instead of proceeding.
_REPLACEMENT_CHAR = "\ufffd"


def _undecodable_at(text: str) -> int:
    """Offset of the first U+FFFD in ``text``, or ``-1`` when it decoded clean.

    A command whose output genuinely contains U+FFFD is indistinguishable from
    one whose output was mangled, and is refused too. That direction is the
    safe one: the caller declines and says why, rather than writing bytes it
    cannot vouch for.
    """
    return text.find(_REPLACEMENT_CHAR)


def _display_safe(text: str) -> str:
    """``text`` with lone surrogates rendered as U+FFFD — for output only.

    The edit ops read with ``errors="surrogateescape"`` so that bytes which are
    not valid UTF-8 round-trip through ``_atomic_write`` untouched (#1049,
    #1059). That puts lone surrogates in the buffer, and those receipts echo
    the buffer back — context lines, a diff hunk. A lone surrogate cannot be
    encoded to a UTF-8 stream, so the op wrote the right bytes and then died
    with ``UnicodeEncodeError`` on the way to saying so: a traceback, no
    receipt, over a file that did change.

    Display only. The bytes are already on disk and nothing here touches them,
    and mojibake in something only being *shown* is the trade this file makes
    everywhere else (see ``_REPLACEMENT_CHAR``). Ops that turn decoded text
    back into bytes or into a path still refuse instead of guessing.
    """
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        pass
    try:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:
        # A surrogate outside DC80..DCFF never came from a surrogateescape read
        # and has no original byte to go back to. Name it rather than drop it.
        return text.encode("utf-8", "backslashreplace").decode("utf-8")


# The one definition of a line boundary supertool uses, for every op that
# numbers lines (#1060): LF, CR and CRLF — what a caller counting lines in an
# editor, in `wc -l`, or in any line-oriented CLI will have counted.
#
# `str.splitlines()` additionally breaks on the eight characters listed below;
# `bytes.splitlines()` does not. `read` used the bytes version and
# `replace_lines` the str version, so a file holding any of them had two
# numberings: the read showed the target at line N, the write landed on a
# different line, and nothing reported a problem. Two independent splits is how
# that happened, so there is one function and both call sites use it.
_LINE_BREAK_PATTERN = r"\r\n|\r|\n"
_LINE_BREAK_RE_STR = re.compile(_LINE_BREAK_PATTERN)
_LINE_BREAK_RE_BYTES = re.compile(_LINE_BREAK_PATTERN.encode("ascii"))

# The characters `str.splitlines()` treats as boundaries and this definition
# does not. Present in a file, they mean some other tool numbers its lines
# differently from supertool — which is disclosed, not resolved.
# Spelled with `chr()` rather than as literals: four of the eight are invisible
# and two of them (U+2028, U+2029) are line breaks to half the tools that would
# ever display this file, which is the property being described.
_AMBIGUOUS_LINE_BREAKS = tuple(chr(c) for c in (
    0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029))


def _split_lines_keepends(data: Any) -> Any:
    """Split `data` (str or bytes) into lines, keeping the line endings.

    Matches `bytes.splitlines(keepends=True)` for both types — the conservative
    definition above. Returns a list of the same type it was given.

    The bytes branch delegates to that builtin rather than re-implementing it:
    `bytes.splitlines` *is* this definition, it is ~7x faster than the regex on
    a megabyte, and `read` runs it on every call. The two branches are one
    contract with two implementations, which is only safe because
    `test_line_split_helper_is_the_conservative_definition` pins them against
    the same expectation — including the eight characters they must both
    refuse to split on.
    """
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
    """`_split_lines_keepends` with the endings stripped."""
    keep = _split_lines_keepends(data)
    if isinstance(data, bytes):
        return [ln.rstrip(b"\r\n") for ln in keep]
    return [ln.rstrip("\r\n") for ln in keep]


def _line_break_ambiguity_note(data: Any) -> str:
    """A line naming the characters in `data` another tool would split on.

    Empty for the overwhelming majority of files. When it is not, the caller
    may be holding a line number counted under the other definition, and saying
    so is the point: the alternative is picking one in silence and letting a
    line-addressed edit land somewhere the reader never saw (#1060).
    """
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
    """Append a timestamped line to the notifier debug log when enabled. Silent otherwise."""
    if not _notifier_debug_enabled():
        return
    try:
        with open(_notifier_debug_log_path(), "a", encoding="utf-8") as f:
            ts = datetime.now().isoformat(timespec="milliseconds")
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass


def _parallel_workers() -> int:
    """Max worker count for parallel batched ops. 0 = sequential.

    Env `SUPERTOOL_PARALLEL` wins over JSON. Accepts:
      int N      → up to N workers (0 disables)
      true/false → 4 / 0 (back-compat with bool config)
    Default: 0 (off).
    """
    env = os.environ.get("SUPERTOOL_PARALLEL")
    raw: object = env if env is not None else _load_config().get("parallel", 0)
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
        # `SUPERTOOL_PARALLEL=x` used to return 0 — indistinguishable from not
        # setting it at all, so a caller who asked for parallelism silently got
        # none. `=-4` did the same through `max(0, ...)`. Both now say so (#654).
        # Only an *env* value is reported: the int branch above is reachable only
        # from JSON config, which is not what this message would be naming.
        try:
            n = int(s)
        except ValueError:
            if env is not None:
                _env_notice(f"note: SUPERTOOL_PARALLEL={raw!r} is not a whole number "
                            f"or true/false - ignoring it and using 0 (sequential).")
            return 0
        if n < 0:
            if env is not None:
                _env_notice(f"note: SUPERTOOL_PARALLEL={raw!r} is below the minimum of 0 "
                            f"- ignoring it and using 0 (sequential).")
            return 0
        return n
    return 0


#: Messages already emitted this process — see `presets/_env.py`. `_get_op_int`
#: is consulted several times for a single `read`, so without this one bad
#: `SUPERTOOL_READ_MAX_LINES` would print the same line six times above the
#: output it is warning about.
_ENV_ANNOUNCED: "set[str]" = set()


def _env_notice(text: str) -> None:
    """One line, on stdout, flushed, at most once per distinct message.

    Not stderr: `_run_custom_op` returns a successful subprocess's stdout and
    drops its stderr, and falling back to a default *is* success — so a notice
    on stderr is a notice nobody receives (#654).
    """
    if text in _ENV_ANNOUNCED:
        return
    _ENV_ANNOUNCED.add(text)
    print(text)
    sys.stdout.flush()


def _env_int(name: str, default: int, *, minimum: "Optional[int]" = None) -> int:
    """Read `name` as an int, or say why it could not be and what is in force.

    Deliberately duplicated from `presets/_env.py` rather than imported.
    `supertool.py` is a single self-contained file — importing a preset helper
    would make core dispatch fail wherever `presets/` was not shipped alongside,
    which is a larger blast radius than the fifteen lines it saves. The two
    copies are kept in step by `tests/test_env_knob_parsing_654.py`, which
    asserts the same contract against both.

    Unset is silent. Set-but-unusable is announced and falls back to `default`.
    `minimum` is a validated floor, not a clamp — see `presets/_env.py` for why
    a negative is refused rather than quietly rounded up.
    """
    raw = os.environ.get(name)
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


def _env_float(name: str, default: float, *, minimum: "Optional[float]" = None) -> float:
    """`_env_int` for the knobs measured in seconds. Same contract."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _env_notice(f"note: {name}={raw!r} is not a number "
                    f"- ignoring it and using {default}.")
        return default
    if value != value:  # NaN is below every bound and equal to none, including itself
        _env_notice(f"note: {name}={raw!r} is not a usable number "
                    f"- ignoring it and using {default}.")
        return default
    if minimum is not None and value < minimum:
        _env_notice(f"note: {name}={raw!r} is below the minimum of {minimum} "
                    f"- ignoring it and using {default}.")
        return default
    return value


def _get_op_int(op_name: str, key: str, default: int) -> int:
    """Read an integer setting from builtin-ops.<op_name>.<key>, with fallback.

    Env var SUPERTOOL_<OP>_<KEY> takes precedence over JSON config.
    Example: SUPERTOOL_READ_ABSTRACT_THRESHOLD_BYTES=12000

    The env override used to fail closed in silence: a non-numeric or
    non-positive `SUPERTOOL_READ_MAX_LINES` fell through to config and read
    exactly like no override at all, so a caller who had set a cap could not
    tell it had been discarded (#654). It now names the variable, the value, and
    the limit actually in force — resolved first, so the number printed is the
    one that will be used rather than a guess at it.

    The *config* side did the same thing and said nothing at all, for another
    thirty-odd releases (#1332). A `builtin-ops.<op>.<key>` that is not a
    positive whole number is still refused — a `"max_lines": 0` honoured as
    "return no lines" would be a silent empty read, which is worse than the
    misconfiguration — but it is now announced through the same notice. A
    switch belongs on `_get_op_bool`, where `0` means off, not here.
    """
    env_key = f"SUPERTOOL_{op_name.upper()}_{key.upper()}"
    env_val = os.environ.get(env_key)
    cfg = _load_config()
    op_cfg = cfg.get("builtin-ops", {}).get(op_name, {})
    # A `builtin-ops.<op>` entry that is not a table is `_merge_presets`'s
    # own #2308 finding -- it now records a `_preset_warnings` entry naming
    # exactly this shape, but the unconditional `.get(key)` this guard
    # replaces crashed with `AttributeError` on the string/list/etc it
    # merged in unchecked, which is worse than the "fell back to its
    # default" this function's own docstring already promised (caught in
    # self-review, both spawned reviewers independently found the same
    # crash; `_get_op_bool` right below already carried this same guard).
    val = op_cfg.get(key) if isinstance(op_cfg, dict) else None
    fallback = default
    if val is not None:
        # `isinstance(True, int)` is True, so a JSON `true` used to read as the
        # threshold 1 — a one-line `read`, which is not what anyone writing it
        # meant. And a configured `0` was indistinguishable from an absent key,
        # so the helper's own default won in silence (#1332).
        #
        # It still wins: honouring `"max_lines": 0` would turn a loud
        # misconfiguration into a silent empty read, which is the worse of the
        # two failures. What changed is that the substitution is now SAID. The
        # helper already named a bad env value and said nothing about a bad
        # config one; a caller who wrote a deliberate `0` in `.supertool.json`
        # could not tell from any output that it had been discarded.
        #
        # A switch does not belong here at all — `_get_op_bool` is the helper
        # where `0` means off.
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


#: The spellings a hand-written `.supertool.json` or an env var may use for a
#: switch. `_get_op_int` accepts neither, which is the whole of #1332.
_BOOL_TRUE = ("1", "true", "yes", "on")
_BOOL_FALSE = ("0", "false", "no", "off")


def _coerce_bool(raw: Any) -> "Optional[bool]":
    """`raw` as a bool, or None when it is not a boolean value at all.

    None is the third state: "this was set, and it is not readable as on or
    off" is different from "this was not set", and only the caller knows which
    default that falls back to.
    """
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
    """Read a switch from builtin-ops.<op_name>.<key>. `0` means off.

    The sibling of `_get_op_int`, and the reason it exists: that helper is a
    positive-threshold reader (`val if isinstance(val, int) and val > 0`), so a
    configured `0` reads as an absent key and the default wins. For `max_lines`
    or `count_ceiling` that is right — a zero there is meaningless. For a flag
    whose default is ON it makes the documented off-switch inert, which is how
    `read.elide: 0` shipped into three documentation sites and did nothing
    (#1332).

    Env `SUPERTOOL_<OP>_<KEY>` takes precedence, same as `_get_op_int`. A value
    that is set but unreadable is announced with the state actually in force,
    rather than silently becoming the default.
    """
    env_key = f"SUPERTOOL_{op_name.upper()}_{key.upper()}"
    env_val = os.environ.get(env_key)
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
    """Return effective grep file extensions. Cached.

    Reads builtin-ops.grep.extensions from .supertool.json.
    - No config / empty list → None (search all files)
    - Config with extensions → only those patterns
    """
    global _GREP_EXTENSIONS_EFFECTIVE
    if _GREP_EXTENSIONS_EFFECTIVE is not None:
        return _GREP_EXTENSIONS_EFFECTIVE if _GREP_EXTENSIONS_EFFECTIVE != ("*",) else None
    cfg = _load_config()
    builtin_ops = cfg.get("builtin-ops", {})
    op_cfg = builtin_ops.get("grep", {})
    # Same #2308 guard as `_get_op_int` just above, and for the identical
    # reason: a `builtin-ops.grep` entry that is not a table used to crash
    # `.get("extensions", ...)` with `AttributeError` rather than fall back.
    exts = op_cfg.get("extensions", []) if isinstance(op_cfg, dict) else []
    if exts and isinstance(exts, list):
        valid = tuple(sorted(e for e in exts if isinstance(e, str) and e.startswith("*.")))
        if valid:
            _GREP_EXTENSIONS_EFFECTIVE = valid
            return valid
    # Default: search all files
    _GREP_EXTENSIONS_EFFECTIVE = ("*",)  # sentinel for "no filter"
    return None


def _get_exclude_paths(op_name: str, no_exclude: bool = False) -> Tuple[str, ...]:
    """Return the effective set of exclude-path prefixes for a traversal op.

    Merges _DEFAULT_EXCLUDE_PATHS with any project-level exclude-paths defined
    under ops.<op_name>.exclude-paths in .supertool.json (additive union).
    Returns an empty tuple when no_exclude=True (per-call escape hatch).
    """
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
    """Normalise one `exclude-paths` entry to the shape `_is_excluded` expects.

    A literal gets a trailing slash so it prefix-matches. A glob and a negation
    are returned untouched: appending `/` to `*.key` produced `*.key/`, which
    fnmatches nothing and no longer looks like a glob either — so a wildcard in
    a project config was a silent no-op, failing in exactly the direction this
    setting exists to prevent (#691).
    """
    if entry.startswith("!") or WILDCARD_CHARS.search(entry):
        return entry
    return entry if entry.endswith("/") else entry + "/"


def _is_excluded(rel_path: str, exclude_paths: Tuple[str, ...]) -> bool:
    """Return True if rel_path matches any of the exclude entries.

    Answers for **files as well as directories**. Callers that walk must ask
    about both: pruning `dirs[:]` alone is how `.env/` sat on the default list
    from #146 to #691 while `grep` printed the contents of every `.env` in the
    tree. There is nothing wrong with the matching here — it was simply never
    asked about a file.

    Four entry shapes (`.gitignore` semantics):
      1. **Prefix match** — `rel_path` literally starts with the entry (catches
         a `node_modules/` at the project root).
      2. **Component match** — a single-segment entry (`__pycache__/`, `.git/`)
         matches that name appearing ANYWHERE in the path (catches nested
         `presets/devto/__pycache__/foo.pyc`). The trailing `/` is not a
         directory assertion: `rel_path` gets one appended before comparison,
         so `.env/` matches a FILE named `.env` and a DIR named `.env/` alike.
      3. **Glob** — an entry containing `*`, `?` or `[` is fnmatched against the
         basename and against the whole relative path (`*.pem`, `id_rsa*`).
      4. **Negation** — an entry starting with `!` un-excludes what it matches
         and wins over every other entry regardless of order, so `.env.*` can
         be listed without hiding the committed `.env.example`.

    Multi-segment prefixes (`Dvsi/dvsi-private/libs/`) keep prefix-only
    semantics — anchoring to repo root is the whole point of them.

    rel_path should be relative to cwd and use os.sep. Comparison normalises
    separators and strips a leading './'.
    """
    if not exclude_paths:
        return False
    import fnmatch
    # Normalise to forward-slashes for consistent prefix matching
    normalised = rel_path.replace(os.sep, "/")
    # Strip leading "./" produced by os.path.join(".", name) or relpath at cwd
    if normalised.startswith("./"):
        normalised = normalised[2:]
    bare_path = normalised.rstrip("/")
    if not normalised.endswith("/"):
        normalised += "/"
    basename = bare_path.rsplit("/", 1)[-1]
    # Component set for the "matches anywhere" check (skip empties).
    components = {c for c in bare_path.split("/") if c}

    def _glob_hit(pattern: str) -> bool:
        return (fnmatch.fnmatch(basename, pattern)
                or fnmatch.fnmatch(bare_path, pattern))

    # Negations first, and they are final — an entry cannot be re-excluded by a
    # later pattern, so the answer never depends on tuple order (the defaults
    # are `sorted()` before they get here).
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
        # Single-segment prefixes also match anywhere in the path.
        bare = entry.rstrip("/")
        if "/" not in bare and bare in components:
            return True
    return False


def _is_disclosable_exclusion(
    rel_path: str, exclude_paths: Tuple[str, ...]
) -> bool:
    """Does this file's exclusion belong in the report's hidden count? (#691)

    The count is the entire justification for hiding a file at all: a `*.pem`
    sitting in a fixtures directory is survivable *because* the header says
    something was dropped. That holds only while the number discriminates, so
    entries that fire constantly and mean nothing have to stay out of it.

    `_hidden_suffix` already made this argument — for directories. Files
    versus directories was a *proxy* for the real line, which is noise versus
    credential, and the proxy holds only because almost every noise entry
    happens to be a directory. In a git **worktree** `.git` is a gitfile, not
    a directory, so the proxy broke precisely where the agent work happens:
    the counter read `1` on every call in the tree, about a pointer file
    nobody searched for. A reader learns to skip a number that is never zero,
    and then the call that says `2` because a real `.env` was hidden looks
    like all the others.

    Built-in noise entries are kept out of the count and still kept out of
    the result — nothing is hidden any less than before. A project's own
    `exclude-paths` entries always count: we cannot know whether one is noise
    or a credential, over-disclosure is the safe direction, and whoever added
    the pattern is the person most likely to want to know that it fired.
    """
    signal = tuple(p for p in exclude_paths if p not in _NOISE_EXCLUDE_SET)  # noqa: F811 -- shadows the `signal` import, whose only other use moved to _supertool_mcp.py's part (#2706)
    return bool(signal) and _is_excluded(rel_path, signal)


def _split_exclude_prefixes(
    exclude_paths: Tuple[str, ...],
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Split exclude prefixes into single-segment names and multi-segment paths.

    Single-segment ("node_modules/", ".git/") can be passed to grep's
    --exclude-dir.  Multi-segment ("Dvsi/dvsi-private/libs/") cannot — callers
    that delegate to grep should fall back to native walking when any
    multi-segment prefixes are present.

    Returns (singles, multis), each tuple of trimmed names without trailing "/".
    """
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
    """Build the `--exclude` / `--exclude-dir` argv for the delegated grep.

    Two things the old `--exclude-dir`-only argv got wrong (#691):

    - **`--exclude-dir` cannot skip a file.** `.env/` means "a dir or a file
      named `.env`" everywhere else in supertool, so every literal entry now
      emits both flags. This alone is what stopped the delegated engine reading
      `.env` off disk at all.
    - **System grep has no negation.** `--exclude=.env.*` would hide
      `.env.example`, which the native walker shows — and which backend ran must
      never change the answer. So when the effective list carries any negation,
      wildcard entries are withheld from the argv entirely and left to the
      post-filter in `op_grep`. Literal entries still go through, which is where
      the traversal win lives (`node_modules`, `.git`).

    And one thing this argv got wrong in turn (#764): a file grep never opens
    is a file `_rtk_drop_excluded` never sees, so `rtk_dropped` stayed 0 and
    `_rtk_grep_report` printed no hidden clause. The disclosure #691 added was
    therefore inverted against usefulness — honest whenever the flags failed,
    silent whenever they worked, which is the fast path and the common one.

    So `--exclude=NAME` is emitted only for entries the report would not have
    counted anyway. A **disclosable** entry (anything off the built-in noise
    list — the same test `_is_disclosable_exclusion` applies) sends only
    `--exclude-dir=NAME`: the file comes back, the post-filter drops it,
    `dropped > 0`, and `op_grep` redoes the walk natively for a full and honest
    report. The cost is that second walk, paid only when a credential-shaped
    file actually matched the pattern — and already the status quo for the
    wildcard half, which the negation rule above withholds for its own reasons.
    A silent search is the worse surprise: the count is the entire
    justification for hiding a file without asking.

    `--exclude-dir` is kept in both cases. A pruned directory is not counted by
    the native walker either — it is never opened, so there are no files to
    count — and withholding it would cost the traversal win and buy no
    disclosure at all.

    Multi-segment entries are never expressible as a bare name; `op_grep`
    already refuses to delegate at all when one is present.
    """
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
    """Filter excluded paths out of a delegated grep's `path:lineno:content`.

    The authoritative guard on the delegated path, and deliberately not the
    only one: the argv flags are an optimisation, this is the guarantee. It
    runs the same `_is_excluded` the native walker runs, so an rtk release that
    rewrites the argv, or a system grep that ignores `--exclude`, still cannot
    put a credential in the output.

    Returns (kept_text, dropped_file_count).
    """
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


# Directories git ignores, keyed on (cwd, search root). One entry per walk
# root per process — a batch call runs many ops and must not re-shell per op.
_GIT_IGNORED_CACHE: Dict[Tuple[str, str], frozenset] = {}
_GIT_IGNORE_TIMEOUT = 10


def _gitignore_enabled() -> bool:
    """Whether walks prune gitignored directories. Default: true (#449).

    Off via `"gitignore": false` in .supertool.json, or SUPERTOOL_NO_GITIGNORE=1
    for one invocation. `no-exclude` on the op turns it off too, since that flag
    already means "show me everything".
    """
    if os.environ.get("SUPERTOOL_NO_GITIGNORE") == "1":
        return False
    return bool(_load_config().get("gitignore", True))


def _git_ignored_dirs(root: str) -> frozenset:
    """Directories under `root` that git ignores, as cwd-relative posix paths.

    Asks git rather than parsing `.gitignore` (#449). Negations (`!keep/`),
    nested ignore files, `.git/info/exclude` and the user's global excludes are
    all semantics we would otherwise have to reimplement, and getting any of
    them wrong hides files — the failure direction this repository has spent a
    week removing. `git ls-files --directory` also collapses an ignored tree to
    its top directory instead of listing it, so the answer costs one subprocess
    and never descends into what it is telling us to skip.

    **Only directories are collected.** Ignored *files* are left in the walk:
    the win here is pruning at the directory boundary, per-file filtering would
    buy little, and `_DEFAULT_EXCLUDE_PATHS` already covers the secret-file
    case (#146).

    Returns an empty set — meaning "no opinion", not "nothing to skip" —
    outside a repo, without git, on timeout, and, deliberately, when `root`
    itself is ignored. That last case is the whole guarantee: a caller who
    names `.claude/worktrees/foo` as the search root gets results, because
    every path under an ignored root is ignored and pruning there would return
    silence.
    """
    if not _gitignore_enabled() or not os.path.isdir(root):
        return frozenset()
    cwd = os.getcwd()
    key = (cwd, os.path.normpath(root))
    cached = _GIT_IGNORED_CACHE.get(key)
    if cached is None:
        cached = _compute_git_ignored_dirs(root, cwd)
        _GIT_IGNORED_CACHE[key] = cached
    return cached


def _run_git_ignore_query(root: str, args: List[str]) -> Any:
    """Run one git query under `root`; None when git is absent or misbehaves."""
    try:
        return subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True, timeout=_GIT_IGNORE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _compute_git_ignored_dirs(root: str, cwd: str) -> frozenset:
    """Uncached body of `_git_ignored_dirs`."""
    # check-ignore exits 1 for "not ignored", 0 for "ignored", 128 for "not a
    # repo" / any other failure. Only 1 authorises pruning: 0 means the caller
    # deliberately searched inside an ignored tree, 128 means we do not know.
    probe = _run_git_ignore_query(root, ["check-ignore", "-q", "--", os.path.abspath(root)])
    if probe is None or probe.returncode != 1:
        return frozenset()
    listing = _run_git_ignore_query(root, [
        "ls-files", "-z", "--others", "--ignored", "--exclude-standard",
        "--directory", "--no-empty-directory",
    ])
    if listing is None or listing.returncode != 0:
        return frozenset()
    dirs = set()
    for entry in listing.stdout.decode("utf-8", "surrogateescape").split("\0"):
        # Trailing slash is git's marker for "this whole directory is ignored".
        # Entries without one are individual files, which we leave alone.
        if not entry.endswith("/"):
            continue
        rel = _strip_dot_slash(
            _safe_relpath(os.path.normpath(os.path.join(root, entry)), cwd)
        )
        if rel and rel != "." and not rel.startswith(".."):
            dirs.add(rel)
    return frozenset(dirs)


def _strip_dot_slash(path: str) -> str:
    """Normalise a relative path to forward slashes with no leading './'."""
    rel = path.replace(os.sep, "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _is_git_ignored(rel_root: str, name: str, ignored: frozenset) -> bool:
    """Whether `rel_root/name` is one of the directories git told us to skip."""
    if not ignored:
        return False
    return _strip_dot_slash(os.path.join(rel_root, name)) in ignored


def _under_git_ignored(rel_path: str, ignored: frozenset) -> bool:
    """Whether a file path sits inside any ignored directory.

    Used on the post-filter glob path, which has no walk boundary to prune at.
    """
    if not ignored:
        return False
    rel = _strip_dot_slash(rel_path)
    return any(rel == d or rel.startswith(d + "/") for d in ignored)


def _gitignore_residual(path: str, exclude_paths: Tuple[str, ...]) -> bool:
    """Whether git ignores a directory the built-in excludes would still walk.

    Gates rtk delegation (#449). rtk shells out to the system grep, whose
    `--exclude-dir` takes bare names and cannot express a nested path like
    `.claude/worktrees/`, so a delegated grep would return the very copies the
    native walker prunes — and which backend ran must never change the answer.
    The test is *residual*, not "is anything ignored": a repo whose ignore set
    is `node_modules/` alone is already fully covered by
    `_DEFAULT_EXCLUDE_PATHS`, and nobody should lose delegation over it.
    """
    if not exclude_paths:
        return False
    return any(
        not _is_excluded(rel, exclude_paths) for rel in _git_ignored_dirs(path)
    )


def _rtk_enabled() -> bool:
    """Check if RTK delegation is enabled in .supertool.json. Default: true."""
    return bool(_load_config().get("rtk", True))


# RTK integration — when rtk is installed, delegate read/grep/wc for compressed output
_RTK_PATH: str | None = None
_RTK_CHECKED = False


def _has_rtk() -> str | None:
    """Return rtk binary path if available, None otherwise. Cached.

    Honours ``SUPERTOOL_NO_RTK=1`` — used by tests that spawn supertool in a
    subprocess and need the unwrapped output format regardless of whether the
    user has rtk on their PATH.
    """
    global _RTK_PATH, _RTK_CHECKED
    if not _RTK_CHECKED:
        _RTK_CHECKED = True
        if os.environ.get("SUPERTOOL_NO_RTK") == "1":
            _RTK_PATH = None
        else:
            # #2611: a raw which() searches the current directory ahead of
            # every real PATH entry on Windows, so a repo-planted "rtk.exe"
            # would be resolved -- and then spawned by _rtk_run() below,
            # which passes no cwd= -- ahead of the real tool. Same class as
            # #2596/#2575, closed the same way: this file's own
            # _which_excluding_cwd() instead of a bare which().
            _RTK_PATH = _which_excluding_cwd("rtk")
    return _RTK_PATH


def _rtk_run(args: List[str], timeout: int = 30) -> str | None:
    """Run rtk command, return stdout or None on failure."""
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


# #1786: a full-file `read:PATH` delegated to `rtk read` on a >150-line file
# came back with the file's real last line (`export = 1`) sandwiched between
# TWO elision-style footers -- `// ... 362 lines omitted` above it and
# `// ... 361 more lines (total: 512)` below it -- whose counts do not even
# sum to the file's own line count. Reproduced directly against `rtk` (not
# through supertool at all): `rtk read -n --max-lines 300 FILE` on any file
# over its window reliably renders this head-window-plus-tail-line-preview
# collision. That is a bug in `rtk`'s own compression
# (github.com/rtk-ai/rtk), not in anything this file computes --
# grep for "lines omitted" in this module finds nothing, because supertool
# never emits that phrase itself.
#
# What is this file's own bug: `render_file`'s RTK delegation trusted that
# render wholesale and returned it to the caller unchanged, so a third
# party's malformed output became supertool's own answer with nothing to
# tell the two apart. A caller who does not already know the file cannot
# tell a garbled compression from a correct one (the failure mode the issue
# itself named as the worst part). Two occurrences of the elision-marker
# shape bracketing real content is not a pattern any well-formed `rtk read`
# truncation produces -- a truncation cuts once -- so it is the signature
# checked for here.
#
# Anchored to rtk's own footer-line SHAPE, not a bare substring (self-review,
# #1786): a plain substring search matches this exact text sitting inside
# ordinary prose that merely discusses this bug -- confirmed against the
# real rtk 0.35.0 binary reading this very test file and the changelog
# fragment for #1786, both of which quote the marker text in their own
# docstrings/prose and neither of which rtk actually truncates. Every line
# `rtk -n` emits, including its own elision markers, is prefixed with
# `N <U+2502>` (the box-drawing vertical bar rtk uses as its column
# separator, never a plain `|`); a source line merely talking about the
# shape does not carry that exact prefix immediately before the marker,
# because it is prose or code, not rtk's own render. Requiring the whole
# line (after that prefix and optional indentation) to be nothing but the
# marker is what a genuine footer looks like and ordinary quoted text does
# not.
_RTK_ELISION_MARKER_RE = re.compile(
    r"(?m)^\s*\d+\s*" + "│"
    + r"\s*//\s*\.\.\.\s*\d+\s+(?:lines omitted|more lines(?:\s*\(total:\s*\d+\))?)\s*$")


def _rtk_output_looks_malformed(text: str, *, aggressive: bool = False) -> bool:
    """True if `text` carries the double-elision-marker corruption (#1786).

    A single elision marker is `rtk`'s ordinary "I truncated here" footer and
    is left alone. Two or more, in the same render, means real content sits
    between a head-truncation notice and a tail-preview notice that
    contradict each other's counts -- the shape reproduced in #1786 at
    `rtk`'s default and `minimal` levels, not a guess at what a well-formed
    render could also look like.

    `aggressive=True` disables the check entirely (#1786 self-review). Under
    `--level aggressive`, `rtk` legitimately compacts EACH function body it
    elides with its own marker -- reproduced directly: a six-function file
    at `--max-lines 10 --level aggressive` renders three `// ... N lines
    omitted` markers plus a final `// ... N more lines (total: M)` summary,
    four matches in one entirely correct render, because aggressive mode
    truncates per block rather than once. Counting markers cannot tell that
    apart from the corruption this function exists to catch, so the
    corruption check is scoped to the levels it was actually reproduced at.
    """
    if aggressive:
        return False
    return len(_RTK_ELISION_MARKER_RE.findall(text)) >= 2
