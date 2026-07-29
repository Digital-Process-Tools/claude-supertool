#!/usr/bin/env python3
"""
supertool — Batch file operations for autonomous Claude Code runs.

WHY THIS EXISTS
---------------
Each separate tool round-trip re-pays the cached prefix (system prompt +
rules + tool schemas + prior turns). Anthropic prompt caching is real and
billed at 10% of input price, so re-pay is NOT free but also NOT full
re-pay. Still worth batching.

Per saved round-trip (3 separate reads vs 1 SuperTool call, 50K prefix,
2K per file):
    Cache reads:    156.9K → 50K      (-106.9K raw, -10.7K effective at 10%)
    Output tokens:  900 → 400         (not cached, billed at 5x input rate)
    Round-trips:    3 → 1             (-2-6s wall time)
    Final context:  identical         (same file bytes either way)

Dollars per batch: ~$0.04 Sonnet, ~$0.19 Opus. Compounds across many
batches per autonomous run.

USAGE — BATCH AS MANY OPS AS YOU CAN ANTICIPATE
-----------------------------------------------
There is no limit on ops per call. Pack every read, grep, and glob you
expect to need this turn. Two ops is NOT the cap — six is routine.

Realistic batch (7 ops, 1 round-trip) — ALWAYS quote args to prevent
shell glob expansion:
    supertool \\
        'read:src/SiX/SiXModule.py' \\
        'read:src/SiX/SiXPermissions.py' \\
        'read:src/SiX/SiXOptions.py' \\
        'grep:extends:src/SiX/:20' \\
        'grep:@related:src/SiX/:10' \\
        'glob:src/SiX/Components/**/*.xml' \\
        'glob:src/SiX/EventsManagers/*.py'

OPERATIONS
----------
    read:PATH                  Read file (first 300 lines, 20KB cap)
    read:PATH:START-END        Read an explicit line range, inclusive
    read:PATH:OFFSET:LIMIT     Read with offset and line limit
    grep:PATTERN:PATH          Search pattern (10 results default).
                                Auto-reads full file if PATH is a concrete
                                file < 20KB with a match.
    grep:PATTERN:PATH:no-auto-read
                               Suppress the single-file auto-read — only the
                                matching line(s) are emitted (parity with glob).
    grep:PATTERN:PATH:LIMIT    Search with custom result limit
    grep:PATTERN:PATH:LIMIT:CONTEXT
                               Search with context lines (like grep -C).
                                Match lines: path:lineno:content
                                Context lines: path-lineno-content
                                Groups separated by -- when non-adjacent.
    grep:PATTERN:PATH:LIMIT:CONTEXT:count
                               Return match counts per file instead of content.
                                Output: filepath:COUNT per line.
    read:PATH:OFFSET:LIMIT:grep=PATTERN
                               Read with inline filter — only show lines matching
                                PATTERN (original line numbers preserved).
    glob:PATTERN               Find files matching pattern (** supported).
                                Auto-reads if PATTERN is a concrete file
                                path with no wildcards.
    ls:PATH                    List directory entries
    tail:PATH:N                Last N lines (default 20)
    head:PATH:N                First N lines (default 20)
    wc:PATH                    Line/word/char count (like unix wc)
    check:PRESET:PATH          Run a named validation from ops section in .supertool.json.
                                Config maps preset names to shell commands with {file}.
    around:PATTERN:PATH        Show 10 lines around the first match in FILE
    around:PATTERN:PATH:N      Show N lines around the first match in FILE
    grep_around:PATTERN:PATH   Every match + 3 ctx lines, limit 10 (alias for
                                grep with sane defaults — bulk usage scan)
    grep_around:PATTERN:PATH:N:LIMIT
                               Every match + N ctx lines, custom limit
    map:PATH                   Symbol map of a file or directory. Shows
                                classes, functions, methods, constants as an
                                indented tree with line numbers.
                                Three-tier: tree-sitter → ctags → regex.
    replace_dry:OLD:NEW:PATH   Preview replacements without modifying files.
                                Shows diff-style output (- old / + new) per
                                occurrence with file paths and line numbers.
    replace:OLD:NEW:PATH       Find and replace OLD with NEW across all files
                                in PATH. Returns receipt: files modified and
                                replacement count per file.

Output: structured text with --- separators per operation.
Calls logged to {tempdir}/supertool-calls.log for per-turn analysis
(macOS: /var/folders/.../T/, Linux: /tmp/, Windows: %TEMP%).
"""
from __future__ import annotations

import atexit
import json
import difflib
import hashlib
import os
import stat
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

VERSION = "0.21.0"


def _fwd(p: str) -> str:
    """Normalize path separators to forward slashes for cross-platform output."""
    return p.replace(os.sep, "/")


def _python_token() -> str:
    """Cross-platform shell-quoted Python interpreter for cmd templates.

    Replaces ``{python}`` in custom op / formatter / validator cmd strings.
    Authors used to hard-code ``python3`` (POSIX-only) or ``python`` (Windows)
    in ``.supertool.json`` — neither was portable. ``{python}`` resolves to
    ``sys.executable`` so the same template runs on Linux, macOS, and Windows.

    Backslashes in ``sys.executable`` on Windows would be eaten by
    ``shlex.split``'s POSIX backslash-escape; forward-slash normalisation
    avoids that. Spaces in the install path (``C:/Program Files/...``) are
    handled by ``shlex.quote``.
    """
    return shlex.quote(sys.executable.replace(os.sep, "/"))


def _safe_relpath(path: str, start: str = ".") -> str:
    """os.path.relpath that survives cross-drive Windows paths.

    On Windows, os.path.relpath raises ValueError when `path` and `start`
    live on different drives (e.g. pytest tmp_path under C:\\Temp vs cwd
    under D:\\). Falls back to the absolute path so traversal/exclude
    logic keeps working — the prefix check downstream simply won't match
    a cross-drive path, which is the correct behavior.
    """
    try:
        return os.path.relpath(path, start)
    except ValueError:
        return os.path.abspath(path)


MAX_READ_LINES = 300
# Hard cap on batch:@file op count — prevents DoS via huge payload
# (10k ops sequentially took ~390s on macOS, hung past timeout on Windows).
# Override via ops.batch.max_ops in .supertool.json for one-off bulk runs.
MAX_BATCH_OPS = 1000
MAX_READ_BYTES = 20000  # ~20KB cap — prevents Claude Code "Output too large"
MAX_AUTOREAD_LINES = 60  # glob:/grep: auto-read line cap (#362) — a file under the
# byte cap but with many lines still overshoots context; skip auto-read above this.
MAX_AROUND_BYTES = 16000  # per-op cap for around:/grep_around: context windows (#241)
MAX_GREP_LINE_CHARS = 500  # per-line cap on grep output (#363) — one 25KB single-line
# PHPDoc/@extends annotation used to eat a screenful for a single hit.
CHAR_WINDOW_CHARS = 1000  # head/tail peek window for minified single-line files
MINIFIED_LINE_CHARS = 5000  # a single line this long means line-based view is useless
MAX_GREP_RESULTS = 10
MAX_GLOB_RESULTS = 50
LOG_FILE = os.path.join(tempfile.gettempdir(), "supertool-calls.log")
GREP_FILE_INCLUDES = ("*.php", "*.xml", "*.py", "*.js", "*.ts", "*.md")
_GREP_EXTENSIONS_EFFECTIVE: Tuple[str, ...] | None = None

def _match_glob(path: str, pattern: str) -> bool:
    """fnmatch with brace expansion: `*.{a,b,c}` → match if any of `*.a / *.b / *.c`.

    fnmatch.fnmatch doesn't understand `{a,b,c}` alternatives. This helper
    expands them once (one level, no nesting) and tries each alternative.
    Patterns without braces fall through to plain fnmatch unchanged.
    """
    import fnmatch
    if not pattern:
        return True
    if "{" not in pattern or "}" not in pattern:
        return fnmatch.fnmatch(path, pattern)
    # Expand a single brace group `{a,b,c}` into ["a", "b", "c"]. Multiple
    # brace groups in the same pattern aren't supported (not needed by current
    # consumers) — fall through to plain fnmatch in that case.
    open_i = pattern.index("{")
    close_i = pattern.index("}", open_i)
    if pattern.count("{") > 1 or pattern.count("}") > 1:
        return fnmatch.fnmatch(path, pattern)
    prefix = pattern[:open_i]
    suffix = pattern[close_i + 1:]
    alternatives = pattern[open_i + 1:close_i].split(",")
    for alt in alternatives:
        if fnmatch.fnmatch(path, f"{prefix}{alt.strip()}{suffix}"):
            return True
    return False


def _matches_any_glob(path: str, patterns: Any) -> bool:
    """True if `path` matches any glob in `patterns`.

    `patterns` may be a single glob string or a list of globs (skip if any
    matches). Falsy patterns (None, "", []) match nothing. Used by validator
    and formatter dispatch to honor a per-spec `exclude` glob.
    """
    if not patterns:
        return False
    if isinstance(patterns, str):
        patterns = [patterns]
    return any(_match_glob(path, p) for p in patterns if p)


def _expand_braces(pattern: str) -> List[str]:
    """Expand shell-style brace groups `{a,b,c}` into a list of patterns.

    Supports multiple groups (`*.{a,b}.{x,y}` → 4 patterns) and nesting
    (`{a,b{1,2}}` → `[a, b1, b2]`). Patterns without braces return `[pattern]`.
    Unbalanced braces are returned unchanged (treated as a literal).
    """
    if "{" not in pattern:
        return [pattern]
    open_i = pattern.index("{")
    depth = 0
    close_i = -1
    for i in range(open_i, len(pattern)):
        c = pattern[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                close_i = i
                break
    if close_i == -1:
        return [pattern]
    prefix = pattern[:open_i]
    suffix = pattern[close_i + 1:]
    inner = pattern[open_i + 1:close_i]
    parts: List[str] = []
    depth = 0
    last = 0
    for i, c in enumerate(inner):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(inner[last:i])
            last = i + 1
    parts.append(inner[last:])
    out: List[str] = []
    seen = set()
    for alt in parts:
        for sub in _expand_braces(prefix + alt + suffix):
            if sub not in seen:
                seen.add(sub)
                out.append(sub)
    return out


# Default exclude-paths applied to all traversal ops (glob, grep, tree, map).
# These are pruned at the directory-walk boundary — the dirs are never opened.
# Match is prefix-relative-to-cwd; trailing slash is normalised in _get_exclude_paths.
_DEFAULT_EXCLUDE_PATHS: Tuple[str, ...] = (
    ".git/", "node_modules/", ".svn/", ".hg/", ".idea/", ".vscode/",
    "__pycache__/", ".venv/", "venv/", "dist/", "build/",
    "phpstan-result-cache/", ".phpunit.cache/", ".rector/",
    # #146: credential/secret dirs and files. Pruned so grep/glob/tree/map
    # don't accidentally surface tokens in their output (which then lands in
    # an LLM's context). Override per-project via .supertool.json exclude-paths.
    # Note: trailing slash matches dirs AND files of the same name —
    # `_is_excluded` appends `/` to rel_path before prefix-matching, so `.env/`
    # catches a FILE named `.env` and a DIR named `.env/`. Distinct entries
    # are needed for `.env.local`, `.env.production`, etc. (each is its own name).
    ".env/", ".env.local/", ".env.production/", ".env.development/", ".env.test/",
    ".max/", ".ssh/", ".aws/", ".gnupg/", ".kube/", ".docker/",
    ".terraform/", ".chef/", ".npm/", "secrets/", "credentials/",
)
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
    r"|^\s*-->"        # XML/HTML comment close
)

# Config file — .supertool.json in project root (or parent dirs)
_CONFIG: Dict[str, Any] | None = None
_CONFIG_CHECKED = False

# Files the loader had to skip, reported once on stderr by main(). A config it
# cannot read is skipped rather than fatal — but skipping in silence means the
# user's ops are simply absent with nothing on screen to connect that to a
# file, so the reason is kept and surfaced (#418).
_CONFIG_WARNINGS: List[str] = []

# MCP server specs parsed from _CONFIG["mcp"] — populated by _load_config()
_mcp_specs: Dict[str, dict] = {}

# Supertool install directory (where supertool.py actually lives, following symlinks).
# Normalised to forward slashes so the directory survives `shlex.split(posix=True)`
# which would otherwise eat Windows backslashes as escape sequences when
# `{supertool_dir}` is substituted into validator / formatter / notifier cmd
# templates. Windows accepts forward-slash paths everywhere; POSIX is unaffected.
_INSTALL_DIR = os.path.dirname(os.path.realpath(__file__)).replace(os.sep, "/")


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


def _merge_presets(config: Dict[str, Any], project_dir: str) -> None:
    """Load and merge preset ops into config. Project ops win on conflict."""
    presets = config.get("presets")
    if not presets or not isinstance(presets, list):
        return

    project_ops = config.get("ops", {})
    merged_ops: Dict[str, Any] = {}

    for name in presets:
        if not isinstance(name, str):
            continue
        preset_path = _find_preset_file(name, project_dir)
        if preset_path is None:
            # Store warning in a list so callers can report it
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
        preset_ops = preset_data.get("ops", {})
        for op_name, op_def in preset_ops.items():
            # Resolve script paths relative to where the preset JSON lives
            if isinstance(op_def, dict) and "cmd" in op_def:
                op_def = dict(op_def)  # don't mutate original
                op_def["cmd"] = _resolve_preset_cmd(op_def["cmd"], preset_dir)
            elif isinstance(op_def, str):
                op_def = _resolve_preset_cmd(op_def, preset_dir)
            merged_ops[op_name] = op_def

    # Project-level ops override preset ops. Dict op-defs deep-merge key-by-key
    # so a project override can add/replace individual keys (e.g. job_patterns)
    # without restating the preset's cmd; non-dicts replace wholesale.
    for op_name, op_def in project_ops.items():
        base = merged_ops.get(op_name)
        if isinstance(base, dict) and isinstance(op_def, dict):
            merged = dict(base)
            merged.update(op_def)
            merged_ops[op_name] = merged
        else:
            merged_ops[op_name] = op_def
    config["ops"] = merged_ops


def _load_config() -> Dict[str, Any]:
    """Load .supertool.json from cwd or parents. Cached.

    After loading, merges any preset ops declared in "presets" key and
    parses the optional "mcp" block into the module-level _mcp_specs dict.
    """
    global _CONFIG, _CONFIG_CHECKED, _mcp_specs
    if _CONFIG_CHECKED:
        return _CONFIG or {}
    _CONFIG_CHECKED = True
    d = os.path.abspath(os.getcwd())
    project_dir = d
    while True:
        candidate = os.path.join(d, ".supertool.json")
        if os.path.isfile(candidate):
            try:
                with open(candidate, encoding="utf-8") as f:
                    _CONFIG = json.load(f)
                    # JSON `null` parses to None; bare scalars / lists parse
                    # to non-dict. _merge_presets needs a dict — coerce to
                    # empty to keep the rest of the loader honest.
                    if not isinstance(_CONFIG, dict):
                        _CONFIG = {}
                    project_dir = d
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
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    _CONFIG = {}
    return _CONFIG


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
        try:
            return max(0, int(s))
        except ValueError:
            return 0
    return 0


def _get_op_int(op_name: str, key: str, default: int) -> int:
    """Read an integer setting from builtin-ops.<op_name>.<key>, with fallback.

    Env var SUPERTOOL_<OP>_<KEY> takes precedence over JSON config.
    Example: SUPERTOOL_READ_ABSTRACT_THRESHOLD_BYTES=12000
    """
    env_key = f"SUPERTOOL_{op_name.upper()}_{key.upper()}"
    env_val = os.environ.get(env_key)
    if env_val:
        try:
            n = int(env_val)
            if n > 0:
                return n
        except ValueError:
            pass
    cfg = _load_config()
    op_cfg = cfg.get("builtin-ops", {}).get(op_name, {})
    val = op_cfg.get(key)
    if isinstance(val, int) and val > 0:
        return val
    return default


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
    exts = op_cfg.get("extensions", [])
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
                if isinstance(p, str):
                    # Normalise: ensure trailing slash for directory prefix matching
                    defaults.add(p if p.endswith("/") else p + "/")
    return tuple(sorted(defaults))


def _is_excluded(rel_path: str, exclude_paths: Tuple[str, ...]) -> bool:
    """Return True if rel_path matches any of the exclude prefixes.

    Two match modes (matches `.gitignore` semantics):
      1. **Prefix match** — `rel_path` literally starts with a prefix (catches
         a `node_modules/` at the project root).
      2. **Component match** — any single-segment prefix (`__pycache__/`,
         `.git/`, `node_modules/`) matches that name appearing ANYWHERE in
         the path (catches nested `presets/devto/__pycache__/foo.pyc`,
         which the old prefix-only logic missed).

    Multi-segment prefixes (`Dvsi/dvsi-private/libs/`) keep prefix-only
    semantics — anchoring to repo root is the whole point of them.

    rel_path should be relative to cwd and use os.sep. Comparison normalises
    separators and strips a leading './'.
    """
    if not exclude_paths:
        return False
    # Normalise to forward-slashes for consistent prefix matching
    normalised = rel_path.replace(os.sep, "/")
    # Strip leading "./" produced by os.path.join(".", name) or relpath at cwd
    if normalised.startswith("./"):
        normalised = normalised[2:]
    if not normalised.endswith("/"):
        normalised += "/"
    # Component set for the "matches anywhere" check (skip empties).
    components = {c for c in normalised.rstrip("/").split("/") if c}
    for prefix in exclude_paths:
        if normalised.startswith(prefix):
            return True
        # Single-segment prefixes also match anywhere in the path.
        bare = prefix.rstrip("/")
        if "/" not in bare and bare in components:
            return True
    return False


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
            from shutil import which
            _RTK_PATH = which("rtk")
    return _RTK_PATH


def _rtk_run(args: List[str], timeout: int = 30) -> str | None:
    """Run rtk command, return stdout or None on failure."""
    rtk = _has_rtk()
    if not rtk:
        return None
    try:
        result = subprocess.run(
            [rtk] + args, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None

# Built-in op names — custom ops/aliases with these names are ignored
_BUILTIN_OPS = {"read", "grep", "grep_around", "glob", "ls", "tail", "head", "wc", "check", "around", "map", "diff", "stat", "around_line", "tree", "replace", "replace_dry", "edit", "replace_lines", "paste", "append", "vi", "validate", "format", "validate_staged", "format_staged", "workspace", "resolve", "diag", "hover", "rename"}

# Read-only built-in ops — safe to run in parallel across a batch.
# Excludes mutating ops (replace, edit, replace_lines) and custom ops
# (could shell out to anything). `between` is included — pure file read.
_PARALLEL_SAFE_OPS = {
    "read", "grep", "glob", "ls", "head", "tail", "wc", "stat",
    "map", "tree", "around", "around_line", "between", "diff", "blame",
    "version", "validate", "validate_staged", "format_staged", "workspace",
    "resolve", "diag", "hover", "help",
}


def _is_parallel_safe(arg: str) -> bool:
    """Return True if the op name is in the read-only safe set.

    Detects op name from `op:...` or `op:::...` prefix. Anything else —
    custom ops, mutating ops, malformed args — is treated as unsafe.
    """
    m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_-]*)(:::|:|$)", arg)
    if not m:
        return False
    return m.group(1) in _PARALLEL_SAFE_OPS


# ---------------------------------------------------------------------------
# Custom ops and aliases — config-driven dispatch extensions
# ---------------------------------------------------------------------------

class SecurityError(Exception):
    """Raised when a path arg violates the cwd containment policy."""
    pass


# Hard cap on path length passed to _safe_path. Sized well above MAX_PATH (260)
# and the extended-length namespace (32767) is too permissive for an op arg —
# 4096 catches obvious abuse (1MB args from fuzz tests) while leaving every
# legitimate path well under the limit.
_MAX_SAFE_PATH_LEN = 4096


def _safe_path(p: str, *, allow_outside_cwd: Optional[bool] = None) -> str:
    """Resolve `p` and enforce repo-root containment (closes #146).

    Strict mode (default): the realpath of `p` must equal cwd or live under
    cwd. Symlinks crossing the boundary are rejected. `..` traversal that
    escapes cwd is rejected. Returns the resolved absolute path.

    Opt-out (any one is enough):
      1. `allow_outside_cwd=True` per-call kwarg
      2. `SUPERTOOL_ALLOW_OUTSIDE_CWD=1` env var (CI / one-off)
      3. `"allow_outside_cwd": true` in `.supertool.json` (project-pinned)

    Test suites set the env var via conftest.py so tmp_path-based fixtures
    keep working; production deployments leave it unset.

    Trust model note: lookup (3) reads from the project's `.supertool.json`,
    same trust level as the other knobs there (validators, custom ops,
    presets). A user cloning a hostile repo is already running its code via
    supertool validators / ops; in-config opt-out adds no new attack
    surface. The env var takes precedence for one-off overrides.

    `~` and env-var expansion happen via os.path.expanduser / expandvars —
    a user-supplied `~/.ssh/id_rsa` is resolved to the real path BEFORE
    the cwd check, which is what catches the threat.
    """
    if allow_outside_cwd is None:
        if os.environ.get("SUPERTOOL_ALLOW_OUTSIDE_CWD") == "1":
            allow_outside_cwd = True
        else:
            # Project config opt-in. Wrapped in try/except so a broken /
            # missing config never raises out of a path check.
            try:
                allow_outside_cwd = bool(_load_config().get("allow_outside_cwd"))
            except Exception:
                allow_outside_cwd = False
    # NUL byte rejection — os.path.* raises ValueError on embedded NULs which
    # would leak as an uncaught traceback. Reject early with a clean message.
    if "\x00" in p:
        raise SecurityError(f"path contains NUL byte: {p!r}")
    # Windows: paths longer than MAX_PATH (260) make _getfinalpathname raise
    # ValueError("path too long for Windows") from inside os.path.realpath.
    # Reject oversized paths up front with a clean SecurityError so dispatch
    # returns a clean "ERROR: ..." instead of an uncaught traceback. 4096 is
    # well above MAX_PATH (260) and extended-length (32767) workable values
    # — any real op path stays well under it.
    if len(p) > _MAX_SAFE_PATH_LEN:
        raise SecurityError(
            f"path too long ({len(p)} chars, max {_MAX_SAFE_PATH_LEN})"
        )
    expanded = os.path.expanduser(os.path.expandvars(p))
    try:
        abs_p = os.path.realpath(expanded)
    except (ValueError, OSError) as e:
        # Truncate path in the error message — matches existing SecurityError
        # style of not echoing arbitrarily-large user input back verbatim.
        shown = p if len(p) <= 120 else p[:120] + "…"
        raise SecurityError(f"path cannot be resolved: {shown!r} ({e})")
    if allow_outside_cwd:
        return abs_p
    # Windows: NTFS is case-insensitive (`C:\Users` == `c:\users`) and uses
    # backslash separators. `os.path.normcase` lowercases + normalises
    # separators on Windows; on POSIX it's a no-op so the check stays exact.
    # This also handles drive-letter case (`c:\` vs `C:\`) and forward-slash
    # variants (`C:/Users` vs `C:\Users`).
    abs_p_cmp = os.path.normcase(abs_p)
    root_cmp = os.path.normcase(os.path.realpath(os.getcwd()))
    if abs_p_cmp == root_cmp:
        return abs_p
    if not abs_p_cmp.startswith(root_cmp + os.sep):
        raise SecurityError(
            f"path escapes cwd: {p!r} (resolved to {abs_p!r}). "
            f"To allow: set SUPERTOOL_ALLOW_OUTSIDE_CWD=1 (env), or add "
            f'`\"allow_outside_cwd\": true` to .supertool.json.'
        )
    return abs_p


def _extract_env_prefix(cmd: str) -> Tuple[Dict[str, str], str]:
    """Split a leading `KEY=VAL KEY2=VAL2 ...` shell env-prefix off `cmd`.

    Returns ({KEY:VAL,...}, remaining_cmd_without_prefix). Mirrors POSIX shell
    semantics: a `KEY=VAL` token before the command sets KEY in the child's
    env. Stops at the first non-assignment token. Tokens are parsed via
    shlex.split, so quoted values (`KEY='one two'`) work.

    Needed because the argv-form fix (#145) broke shipped cmd templates that
    set env this way — `subprocess.run(shlex.split(cmd), shell=False)` treats
    the assignment as a literal argv[0], yielding ENOENT.
    """
    env: Dict[str, str] = {}
    tokens = shlex.split(cmd, posix=True)
    idx = 0
    _kv = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
    while idx < len(tokens):
        m = _kv.match(tokens[idx])
        if not m:
            break
        env[m.group(1)] = m.group(2)
        idx += 1
    if not env:
        return {}, cmd
    # Rebuild remaining cmd as shell-safe string so callers can keep using
    # shlex.split on it (the placeholder-substituted file path already
    # passed through shlex.quote upstream, so it survives a second pass).
    remaining = " ".join(shlex.quote(t) for t in tokens[idx:])
    return env, remaining


def _check_vim_shell_allowed() -> Optional[str]:
    """Gate vim's `:!cmd`, `:%!cmd`, `:r !cmd` behind explicit opt-in (closes #147).

    Returns None when allowed, else a clean ERROR string the caller returns
    up the stack. Shell verbs in a vim macro are full RCE by design — a
    prompt-injected vim payload like `:!rm -rf ~` runs verbatim. Default-off
    keeps editor verbs (i/a/o/d/s/etc.) working unconditionally.

    Opt-in (any one is enough):
      1. `SUPERTOOL_ALLOW_VIM_SHELL=1` env var (one-off / CI)
      2. `"allow_vim_shell": true` in `.supertool.json` (project-pinned)
    """
    if os.environ.get("SUPERTOOL_ALLOW_VIM_SHELL") == "1":
        return None
    try:
        if bool(_load_config().get("allow_vim_shell")):
            return None
    except Exception:
        pass
    return (
        "ERROR: vim shell verbs (:!, :%!, :r !) are disabled by default. "
        'To allow: set SUPERTOOL_ALLOW_VIM_SHELL=1 (env), or add '
        '`"allow_vim_shell": true` to .supertool.json. '
        "For one-off shell logic, prefer a wrapper script + custom op.\n"
    )


def _expand_env(s: str, env: Dict[str, str]) -> str:
    """Safe $VAR / ${VAR} expansion from env (no shell).

    Replaces $NAME and ${NAME} with values from env. Unknown vars are left
    literal (vs shell which silently empties them). Used at all argv-form
    dispatch sites (custom ops, validators, formatters, resolve) so users
    can keep cmd templates that rely on env-var expansion without invoking
    a shell.
    """
    return re.sub(
        r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)',
        lambda m: env.get(m.group(1) or m.group(2), m.group(0)),
        s,
    )


def _resolve_custom_op(op: str, parts: List[str]) -> str | None:
    """Try to run op as a custom command from config["ops"].

    Runs argv-form (shell=False) — shell metachars in the cmd template are
    literal tokens, not shell operators. `$VAR` / `${VAR}` expansion from
    the op's env is performed by supertool (see _expand_env below), no shell.

    Returns formatted output string on match, None if op is not a custom op.
    """
    config = _load_config()
    ops = config.get("ops")
    if not ops or op not in ops:
        return None

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

    # Build the command — substitute {file}, {dir}, {arg}, {args}, {argjoin},
    # {python} in ONE pass. Chained str.replace calls would rescan the text a
    # previous pass just inserted, so an ARGUMENT VALUE containing a later
    # placeholder token (a commit message mentioning {argjoin}) got expanded
    # inside its own shlex.quote'd value — shattering the quoting and leaking
    # the value's words into argv. One pass never looks at inserted text.
    file_arg = parts[1] if len(parts) > 1 else ""
    dir_arg = os.path.dirname(file_arg) if file_arg else "."
    # {argjoin}: parts[1:] rejoined with ':::' as a single shell-quoted arg.
    # Lets the receiving script split fields itself when they contain colons
    # (e.g. XPath like .//ns:tag or [position()=1]).
    arg_join = ":::".join(parts[1:]) if len(parts) > 1 else ""
    cmd = _substitute_placeholders(cmd_template, {
        "python": _python_token(),
        "file": shlex.quote(file_arg),
        "dir": shlex.quote(dir_arg),
        "arg": shlex.quote(file_arg),
        "args": " ".join(shlex.quote(p) for p in parts[1:]) if len(parts) > 1 else "",
        "argjoin": shlex.quote(arg_join),
    })

    # Pass extra config keys as SUPERTOOL_ env vars
    _RESERVED_KEYS = {"cmd", "timeout", "description", "syntax", "example", "status", "restartMcp"}
    env = dict(os.environ)
    if isinstance(entry, dict):
        for k, v in entry.items():
            if k not in _RESERVED_KEYS:
                # Strings pass through verbatim (e.g. CSV "error_patterns").
                # Non-scalars (lists/dicts, e.g. "job_patterns") are JSON-encoded
                # so the receiving preset can json.loads them back — str() would
                # emit a Python repr that json.loads can't parse.
                env[f"SUPERTOOL_{k.upper()}"] = v if isinstance(v, str) else json.dumps(v)

    _prefix_env, cmd = _extract_env_prefix(cmd)
    env.update(_prefix_env)
    cmd = _expand_env(cmd, env)

    t0 = time.monotonic()
    try:
        # argv-form (shell=False) — shell metachars in the template become
        # literal tokens, not shell operators. Placeholder values are still
        # shlex.quote'd above so values containing spaces survive shlex.split.
        # encoding is pinned rather than left to the locale: presets print
        # ✓/✗/⚠ as UTF-8, and a cp1252 default decodes those three bytes into
        # three wrong characters, so the receipt renders mojibake for an op
        # that worked. errors="replace" keeps a preset emitting genuinely
        # undecodable bytes from taking the whole run down with it.
        result = subprocess.run(
            shlex.split(cmd), shell=False, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", env=env,
        )
        elapsed = time.monotonic() - t0
        output = result.stdout
        if result.returncode != 0:
            if result.stderr:
                output += result.stderr
            return f"FAIL ({elapsed:.2f}s)\n{output}"
        return f"PASS ({elapsed:.2f}s)\n{output}{_maybe_restart_mcp(entry)}"
    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - t0
        return (f"FAIL (timeout {elapsed:.1f}s > {timeout}s)\n"
                f"{_timeout_partial_output(e)}")
    except OSError as e:
        return f"FAIL: {e}\n"


def _timeout_partial_output(exc: subprocess.TimeoutExpired) -> str:
    """Whatever the killed command printed before the clock ran out.

    Dropping it costs the caller the only evidence of how far the op got — for
    an op that mutates remote state (a push), the difference between "retry" and
    "already landed" was sitting in that discarded buffer (#399).
    """
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
    """SIGTERM the warm MCP daemons a custom op asks to restart via `restartMcp`.

    A custom op that invalidates state the warm LSP daemons cache (autoload map,
    PHPStan result cache, etc.) can declare `restartMcp` so the daemons are
    stopped after the cmd succeeds; the next op that touches each server
    cold-starts a fresh daemon that re-reads the cleared state. Accepts:
      true       -> every server in the config "mcp" block
      ["a","b"]  -> only those servers
      "name"     -> a single server
    Names not present in the config "mcp" block are reported separately instead
    of being counted as restarted, so the status line never claims to have
    stopped a daemon that was never configured. Returns a one-line status suffix
    (empty when nothing to restart). Best-effort like the new-file path — a stop
    failure never fails the op.
    """
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
    for name in known:
        _mcp_stop_server(name)
    note = ""
    if known:
        note += f"mcp: restarted {len(known)} daemon(s) ({', '.join(known)})\n"
    if unknown:
        note += f"mcp: unknown server(s) ignored ({', '.join(unknown)})\n"
    return note


_IN_ALIAS = False  # recursion guard — prevents alias-from-alias expansion


def _resolve_alias(op: str, parts: List[str]) -> str | None:
    """Try to expand op as an alias from config["aliases"].

    Returns concatenated output of all expanded ops, None if not an alias.
    Aliases expand to ops (built-in or custom) but NOT to other aliases.
    """
    global _IN_ALIAS
    if _IN_ALIAS:
        return None  # block recursive alias expansion

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

    # Replace {file}, {dir}, {arg}, and {args} placeholders in each expanded op
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
            # Single pass — same reason as _resolve_custom_op: a path or
            # argument containing '{args}' must not expand itself.
            resolved = _substitute_placeholders(expanded_op, alias_values)
            output_parts.append(dispatch(resolved))
        return "".join(output_parts)
    finally:
        _IN_ALIAS = False


# ---------------------------------------------------------------------------
# Core operations (pure functions — all return the string to emit)
# ---------------------------------------------------------------------------

def render_file(path: str, offset: int = 0, limit: int = 0,
                grep_filter: str = "", force_full: bool = False) -> str:
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
    if limit <= 0:
        limit = _get_op_int("read", "max_lines", MAX_READ_LINES)
    if not path or not os.path.isfile(path):
        return f"ERROR: file not found: {path}\n"

    # RTK delegation — simple reads without offset/filter/limit changes
    if not grep_filter and offset == 0 and limit == _get_op_int("read", "max_lines", MAX_READ_LINES) and _rtk_enabled() and _has_rtk():
        rtk_args = ["read", "-n", "--max-lines", str(_get_op_int("read", "max_lines", MAX_READ_LINES))]
        if _is_compact():
            rtk_args += ["--level", "aggressive"]
        rtk_args.append(path)
        rtk_out = _rtk_run(rtk_args)
        if rtk_out is not None:
            return rtk_out + "\n"

    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            raw_lines = f.read().splitlines(keepends=True)
    except OSError as e:
        return f"ERROR: could not read {path}: {e}\n"

    line_count = len(raw_lines)
    out = [f"({line_count} lines, {size} bytes){_path_meta_suffix(path, b''.join(raw_lines[:64]))}\n"]
    bytes_emitted = 0
    printed = 0
    end = min(offset + limit, line_count)
    # When invoked outside Claude Code, the 25KB hook limit doesn't apply —
    # so `:full` from a human shell should return the whole file uncapped.
    in_claude = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "") != ""
    byte_cap = _get_op_int("read", "max_bytes", MAX_READ_BYTES)
    apply_byte_cap = in_claude or not force_full
    if not apply_byte_cap:
        end = line_count  # ignore line cap too when human asks for full file

    filter_regex = None
    if grep_filter:
        try:
            filter_regex = re.compile(grep_filter)
        except re.error:
            filter_regex = re.compile(re.escape(grep_filter))

    compact = not filter_regex and _is_compact()
    matched_any = False
    for i in range(offset, end):
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
            break

    if filter_regex and not matched_any:
        out.append(f"(no lines matching {grep_filter!r})\n")
    elif apply_byte_cap and bytes_emitted >= byte_cap:
        last_line = offset + printed
        remaining = line_count - last_line
        out.append(
            f"... (truncated at {_get_op_int('read', 'max_bytes', MAX_READ_BYTES)} bytes "
            f"— showed lines {offset + 1}-{last_line} of {line_count} "
            f"({remaining} more line{'s' if remaining != 1 else ''}) — "
            f"use read:PATH:OFFSET:LIMIT to get more)\n"
        )
    elif not filter_regex and offset + printed < line_count:
        out.append(f"... ({line_count - offset - printed} more lines)\n")
    elif not filter_regex:
        out.append("[complete file — no more lines]\n")
    out.append("\n")
    return "".join(out)


_READ_RANGE_RE = re.compile(r"\d+-\d+")


def _read_range_note(path: str, offset: int, limit: int, body: str) -> str:
    """One-line nudge when `read:PATH:A:B` looks like a misread line range (#382).

    `:A:B` is OFFSET:LIMIT, but it reads like START:END to anyone who has used
    `sed -n 'A,Bp'`, and the overshoot is quiet — the output just looks long.
    Fires only on the combination that is near-certain in the misread case and
    rare otherwise: LIMIT > OFFSET (a real limit is seldom larger than the point
    it starts from) *and* OFFSET+LIMIT running past EOF.
    """
    if offset <= 0 or limit <= 0 or limit <= offset:
        return ""
    if body.startswith("ERROR:"):
        return ""
    total = _count_lines(path)
    if total <= 0 or offset + limit <= total:
        return ""
    emitted = max(0, total - offset)
    return (
        f"note: read {emitted} lines (offset {offset}, limit {limit}) — those "
        f"args are OFFSET:LIMIT, not START:END. For lines {offset}-{limit}, "
        f"use read:{path}:{offset}-{limit}\n"
    )


def op_read(path: str, offset: int = 0, limit: int = 0,
            grep_filter: str = "", force_full: bool = False) -> str:
    # PHP abstract mode — when enabled, read:PATH on a PHP file with no
    # offset/limit/grep returns the symbol map (~10x smaller). Skipped when:
    #   - file size <= threshold (small files fit raw in the cap, abstract
    #     buys nothing)
    #   - caller passes :full / :raw (force_full)
    #   - explicit offset/limit/grep
    if (offset == 0 and limit == 0 and not grep_filter
            and not force_full
            and path.endswith(".php")
            and _get_op_int("read", "php_abstract", 0)):
        threshold = _get_op_int("read", "abstract_threshold_bytes",
                                _get_op_int("read", "max_bytes", MAX_READ_BYTES))
        try:
            size_bytes = os.path.getsize(path)
        except OSError:
            size_bytes = 0
        if size_bytes > threshold:
            line_count = 0
            try:
                with open(path, "rb") as f:
                    for line_count, _ in enumerate(f, 1):
                        pass
            except OSError:
                pass
            return (op_map(path)
                    + f"\n[php abstract — {line_count} lines, {size_bytes} bytes raw — "
                      f"use read:{path}:full for content "
                      f"or read:{path}:::grep=PATTERN to filter]\n")
    if limit <= 0:
        limit = _get_op_int("read", "max_lines", MAX_READ_LINES)
    body = render_file(path, offset, limit, grep_filter, force_full)
    return body + _read_edit_hint(path, body)


def _read_edit_hint(path: str, body: str) -> str:
    """One-line nudge appended to a single-file read receipt: supertool's edit
    op bypasses the harness must-Read-first gate, so the file can be modified
    without a harness Read tool call (#309). Scoped to successful single-file
    reads only — render_file errors get no hint, and grep/glob multi-file
    branches don't route through op_read."""
    if body.startswith("ERROR:"):
        return ""
    return (f"{mark('↳')} to modify: ./supertool 'edit:::OLD:::NEW:::{path}'"
            f"  (or edit:@- ; no harness Read needed)\n")


_REGEX_METACHARS = re.compile(r"[()\[\]{}|.*+?^$\\]")


def _is_regexy(pattern: str) -> bool:
    """True if pattern holds regex metacharacters that could make a literal
    code fragment match the wrong thing — or nothing. Gates the zero-hit
    literal fallback so plain-word searches never pay for a second pass."""
    return bool(_REGEX_METACHARS.search(pattern))


def _literal_note(pattern: str, count: int) -> str:
    """One-line banner shown when a pattern found 0 regex hits but matched
    literally once metacharacters were escaped — tells the caller the search
    auto-corrected so they learn the pattern was regex-ambiguous."""
    return (f"(no regex match; showing {count} literal "
            f"match(es) for {pattern!r})\n")


def _count_lines(path: str, on_error: int = 0) -> int:
    """Count lines in a file cheaply, streaming in binary (#362).

    `on_error` is what an unreadable file counts as, because the callers want
    opposite things and one hardcoded answer is wrong for the other (#388). The
    glob:/grep: auto-read line cap passes `MAX_AUTOREAD_LINES + 1` so a file it
    cannot measure is treated as over-cap and left unread — failing closed.
    `map` and the workspace summary keep the default 0, since a sentinel would
    render as a wildly wrong line count in a listing.
    """
    try:
        count = 0
        last = b""
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                count += chunk.count(b"\n")
                last = chunk
        if last == b"":
            return 0  # empty file
        # trailing line without a final newline
        if not last.endswith(b"\n"):
            count += 1
        return count
    except OSError:
        return on_error


def op_grep(pattern: str, path: str = ".", limit: int = 0,
            context: int = 0, count_only: bool = False,
            no_exclude: bool = False, no_auto_read: bool = False) -> str:
    """Search pattern recursively. Auto-reads small single file on match.

    When context > 0, emits N lines before/after each match in grep -C style:
      match lines:   path:lineno:content  (colon separator)
      context lines: path-lineno-content  (dash separator)
    Non-adjacent groups are separated by --.
    Auto-read is skipped when context > 0 (output already contains context).

    When count_only=True, returns match counts per file instead of content.

    When no_auto_read=True, suppresses the single-small-file auto-read so only
    the matching line(s) are emitted (parity with glob's :no-auto-read flag).
    """
    if limit <= 0:
        limit = _get_op_int("grep", "max_results", MAX_GREP_RESULTS)
    if not pattern:
        return "ERROR: empty pattern\n"

    # #150 ReDoS guards. Python's stdlib `re` has no execution timeout, so
    # we reject patterns that are obvious candidates for catastrophic
    # backtracking before they touch any file content.
    if len(pattern) > 1000:
        return f"ERROR: pattern too long ({len(pattern)} > 1000 chars)\n"
    # Nested unbounded quantifiers like `(a+)+`, `(a*)*`, `(.+)*` — the
    # classic ReDoS shape. The check is intentionally loose; users with a
    # legitimate need can split into simpler greps.
    if re.search(r"\([^)]*[+*][^)]*\)[+*?]", pattern):
        return (
            "ERROR: pattern contains nested unbounded quantifiers "
            f"({pattern!r}) — would risk catastrophic backtracking. "
            "Rewrite without `(...+)+`-style nesting.\n"
        )

    # Auto-convert bash grep BRE alternation (\|) to Python regex (|)
    if "\\|" in pattern:
        pattern = pattern.replace("\\|", "|")

    # Early exit if path doesn't exist (don't silently return 0 results)
    if path != "." and not os.path.isfile(path) and not os.path.isdir(path):
        # Could be a glob pattern — check if it expands to anything
        from glob import glob as _glob
        if not _glob(path, recursive=True):
            return (f"ERROR: path not found: {path} (cwd: {os.getcwd()}) — wrong CWD? "
                    f"Prefix the call with cwd:PATH to run it from elsewhere.\n")

    excl = _get_exclude_paths("grep", no_exclude)

    # RTK delegation — basic grep (no context, no count). Thread excludes through
    # via grep's --exclude-dir for single-segment prefixes (.git/, node_modules/,
    # etc.). Multi-segment prefixes (e.g. "Dvsi/dvsi-private/libs/") can't be
    # expressed as --exclude-dir; fall through to the native walker in that case.
    if not count_only and context == 0 and _rtk_enabled() and _has_rtk():
        single, multi = _split_exclude_prefixes(excl)
        if not multi and not _gitignore_residual(path, excl):
            # limit + 1 so the report can tell "exactly N" from "stopped at N"
            # (#448). The extra line is trimmed off before output.
            rtk_args = ["grep", "-rn", "-m", str(limit + 1)]
            for d in single:
                rtk_args.append(f"--exclude-dir={d}")
            rtk_args.extend([pattern, path])
            rtk_out = _rtk_run(rtk_args)
            if rtk_out is not None and rtk_out.strip():
                return _rtk_grep_report(rtk_out, limit)
            # No RTK output — rtk failed, or it ran and matched nothing. Fall
            # through to the native walker either way (#414). A zero result is
            # the ambiguous case #407 exists for, so it must reach the walker
            # and come back with a real scanned count rather than the `?` the
            # delegated report carries. This also lets the zero-hit literal
            # fallback below run.

    # Resolved once and threaded through so a zero-result report can state how
    # many files were actually scanned (#407) — without it, "0 results" is
    # ambiguous between "searched everything, found nothing" and "path/glob
    # resolved to nothing, so nothing was searched".
    candidates = _grep_candidates(path, excl)
    scanned = len(candidates)

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
               f"({total} total matches across {file_count} files{_scanned_suffix(scanned)})\n"]
        for fp, cnt in sorted(counts.items()):
            out.append(f"{_fwd(fp)}:{cnt}\n")
        out.append("\n")
        return "".join(out)

    if context > 0:
        groups = _grep_recursive_context(
            pattern, path, limit + 1, context, excl, candidates=candidates)
        literal = False
        if not groups and _is_regexy(pattern):
            groups = _grep_recursive_context(
                re.escape(pattern), path, limit + 1, context, excl, candidates=candidates)
            literal = bool(groups)
        groups, truncated = _trim_context_groups(groups, limit)
        count = sum(
            1 for g in groups for line in g if line[2] == "match"
        )
        literal_note = _literal_note(pattern, count) if literal else ""
        file_count = len({g[0][0] for g in groups if g})
        out = [literal_note, f"({count} results in {file_count} files{_scanned_suffix(scanned)}, "
               f"limit {limit}, context {context}{_truncation_suffix(truncated)})\n"]
        current_file: str = ""
        first_group = True
        for group in groups:
            group_file = group[0][0] if group else ""
            if group_file != current_file:
                current_file = group_file
                out.append(f"{current_file}\n")
                first_group = True  # reset separator for new file
            if not first_group:
                out.append("  --\n")
            first_group = False
            for _fp, lineno, kind, content in group:
                capped = _cap_grep_line(content)
                if kind == "match":
                    out.append(f"  {lineno}:{capped}\n")
                else:
                    out.append(f"  {lineno}-{capped}\n")
        out.append("\n")
        return _cap_context_window("".join(out), "grep_around")

    # limit + 1 (#448): a count that equals the limit is ambiguous between
    # "exactly N matches" and "stopped at N", and only looking one past the cap
    # settles it. The walk itself is already paid for — `candidates` above
    # traversed the whole tree to produce `scanned` — so the extra cost is
    # reading file *contents* until one more match turns up, not another walk.
    hits = _grep_recursive(pattern, path, limit + 1, excl, candidates=candidates)
    literal = False
    if not hits and _is_regexy(pattern):
        hits = _grep_recursive(re.escape(pattern), path, limit + 1, excl, candidates=candidates)
        literal = bool(hits)
    truncated = len(hits) > limit
    hits = hits[:limit]
    count = len(hits)
    literal_note = _literal_note(pattern, count) if literal else ""
    file_count = len({fp for fp, _, _ in hits})

    out = [literal_note,
           f"({count} results in {file_count} files{_scanned_suffix(scanned)}, "
           f"limit {limit}{_truncation_suffix(truncated)})\n"]
    current_file = ""
    for fp, lineno, content in hits:
        if fp != current_file:
            current_file = fp
            out.append(f"{fp}\n")
        out.append(f"  {lineno}:{_cap_grep_line(content)}\n")
    out.append("\n")

    # Auto-read: single small file + at least one match → emit full file.
    # Gated on BOTH byte size and line count (#362): a file under the byte cap
    # but with many lines still overshoots context, so cap both dimensions.
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
            out.append(render_file(path, 0, _get_op_int("read", "max_lines", MAX_READ_LINES)))

    return "".join(out)


_AROUND_DIR_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", "vendor"}
_AROUND_DIR_MAX_FILES = 20


def _cap_grep_line(content: str) -> str:
    """Truncate one grep output line to a char budget (#363).

    Files with pathological single lines (minified JS, a 25KB one-line
    `@extends` PHPDoc) turn a single hit into a screenful. Cap the line and
    say how much was dropped so the reader knows to widen deliberately.
    Configurable via builtin-ops.grep.max_line_chars or
    SUPERTOOL_GREP_MAX_LINE_CHARS.
    """
    cap = _get_op_int("grep", "max_line_chars", MAX_GREP_LINE_CHARS)
    if len(content) <= cap:
        return content
    return f"{content[:cap]}… (+{len(content) - cap} chars)"


def _cap_context_window(text: str, op_name: str) -> str:
    """Cap a context-window op's output at a byte budget (#241).

    around:/grep_around: with a large :N on a file of long (e.g. minified)
    lines can emit hundreds of KB in one op, blowing the caller's context.
    Truncate at the last line boundary within the cap and append a footer
    that points at the narrower tools. Configurable via
    builtin-ops.<op>.max_bytes or SUPERTOOL_<OP>_MAX_BYTES.
    """
    cap = _get_op_int(op_name, "max_bytes", MAX_AROUND_BYTES)
    encoded = text.encode("utf-8", errors="surrogateescape")
    if len(encoded) <= cap:
        return text
    clipped = encoded[:cap].decode("utf-8", errors="ignore")
    # Truncate at the last line boundary so we never cut mid-line. nl == -1
    # means a single line longer than the cap — nothing to trim to, pass the
    # partial through (the footer still flags it).
    nl = clipped.rfind("\n")
    if nl >= 0:
        clipped = clipped[:nl + 1]
    dropped = len(encoded) - len(clipped.encode("utf-8", errors="ignore"))
    return (clipped +
            f"… truncated (~{dropped} more bytes) — narrow context (:N) "
            f"or use between: for the whole symbol\n")


def _around_one_file(regex: "re.Pattern[str]", path: str, n: int) -> str:
    """Render the first match of regex in file at path with n lines context.

    Returns an empty string when the file has no match (caller filters these
    out so dir fan-out only shows hits).
    """
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


def op_around(pattern: str, path: str, n: int = 10) -> str:
    """Show N lines before and after the first match of PATTERN in PATH.

    PATH can be a file (first match in that file) or a directory (first
    match per file, skipping files with no match, capped at
    _AROUND_DIR_MAX_FILES). Hidden and heavy dirs (.git, node_modules,
    vendor, …) are skipped during dir walk.
    """
    if not pattern:
        return "ERROR: empty pattern\n"
    if "\\|" in pattern:
        pattern = pattern.replace("\\|", "|")
    if not path:
        return "ERROR: empty path\n"

    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(re.escape(pattern))

    if not os.path.isdir(path) and not os.path.isfile(path):
        return f"ERROR: file not found: {path}\n"

    def _render(rx: "re.Pattern[str]") -> Tuple[str, bool]:
        """Render the around-window for `rx`. Returns (output, matched) so the
        caller can decide whether to retry with an escaped literal pattern."""
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
            return f"(no match for {pattern!r} in {path})\n\n", False
        return _cap_context_window(rendered, "around"), True

    out_text, matched = _render(regex)
    if not matched and _is_regexy(pattern):
        lit_text, lit_matched = _render(re.compile(re.escape(pattern)))
        if lit_matched:
            return _literal_note(pattern, lit_text.count("=== ") or 1) + lit_text
    return out_text


def op_between_symbol(symbol: str, path: str) -> str:
    """Return the body of a named function/method/class via tree-sitter.

    SYMBOL is matched against definition node names. First match wins; the
    info line reports total match count when the name is ambiguous.
    """
    if not symbol:
        return "ERROR: empty symbol\n"
    if not path:
        return "ERROR: empty path\n"
    if os.path.isdir(path):
        return (f"ERROR: between only works on single files, not "
                f"directories: {path}\n")
    if not os.path.isfile(path):
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
    # Retry with modifiers/parens stripped so a signature pasted from source
    # resolves like the bare name would (#363). Exact match still wins.
    normalized = _normalize_symbol_query(symbol)
    if found is None and normalized != symbol:
        found = _ts_find_node(path, lang_name, normalized)
        if found is not None:
            symbol = normalized
    if found is None:
        extra = "" if normalized == symbol else f" (also tried {normalized!r})"
        return f"ERROR: symbol {symbol!r} not found in {path}{extra}\n"
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
    """Return inclusive line slice from first line matching START to first
    subsequent line matching END (regex, language-agnostic).
    """
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
        return f"ERROR: could not read {path}: {e}\n"

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
        return f"ERROR: start pattern {start!r} not matched in {path}\n"

    end_idx: int | None = None
    for i in range(start_idx + 1, len(lines)):
        if end_re.search(lines[i]):
            end_idx = i
            break
    if end_idx is None:
        return (f"ERROR: end pattern {end!r} not matched after line "
                f"{start_idx + 1} in {path}\n")

    out = [f"(slice lines {start_idx + 1}–{end_idx + 1}, "
           f"{end_idx - start_idx + 1} lines)\n"]
    for i in range(start_idx, end_idx + 1):
        marker = "→" if i in (start_idx, end_idx) else " "
        out.append(f"{i + 1:>6}{marker}{lines[i]}")
    out.append("\n")
    return "".join(out)


def op_glob(pattern: str, no_exclude: bool = False, no_auto_read: bool = False) -> str:
    """Find files matching pattern. Auto-reads concrete file paths unless no_auto_read."""
    if not pattern:
        return "ERROR: empty pattern\n"

    # Auto-promote: concrete path with no wildcards that points to a file
    if not WILDCARD_CHARS.search(pattern) and os.path.isfile(pattern):
        if no_auto_read:
            return f"{pattern}\n"
        return ("[auto-read: concrete path, no wildcards]\n"
                + render_file(pattern, 0, _get_op_int("read", "max_lines", MAX_READ_LINES)))

    excl = _get_exclude_paths("glob", no_exclude)
    # over_fetch=1 (#448): `(N files)` implies completeness the same way grep's
    # header did, and one file past the cap is what distinguishes "N matched"
    # from "N shown".
    cap = _get_op_int("glob", "max_results", MAX_GLOB_RESULTS)
    files = _glob_files(pattern, excl, over_fetch=1)
    # glob is repo-root relative, so a pattern naming a mid-path segment
    # (`SiBrief/**/*.php` for a dir nested under Dvsi/src2/) returns 0 while the
    # same segment works fine in grep. Retry once with a `**/` prefix so both
    # ops accept the same mental model (#363).
    midpath_note = ""
    if (not files and "/" in pattern
            and not pattern.startswith(("/", "~", "**", "./", "../"))):
        retry = "**/" + pattern
        files = _glob_files(retry, excl, over_fetch=1)
        if files:
            midpath_note = (f"[mid-path retry: no match at repo root for "
                            f"{pattern!r} — matched {retry!r}]\n")
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
    out = [midpath_note, f"({len(files)} files{truncation})\n"]
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
            out.append(f"[auto-read: glob returned 1 file]\n")
            out.append(render_file(files[0], 0, _get_op_int("read", "max_lines", MAX_READ_LINES)))

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


def op_check(preset: str, path: str) -> str:
    """Run a named validation check from the ops section of .supertool.json."""
    if not preset:
        return "ERROR: empty preset name\n"

    main_config = _load_config()
    ops = main_config.get("ops", {})
    if preset in ops:
        result = _resolve_custom_op(preset, ["check", path])
        if result is not None:
            return result

    if not ops:
        return "ERROR: no ops defined in .supertool.json\n"
    available = ", ".join(sorted(ops.keys()))
    return f"ERROR: unknown check {preset!r}. Available: {available}\n"


def op_diff(path1: str, path2: str) -> str:
    """Show unified diff between two files."""
    for p in (path1, path2):
        if not p:
            return "ERROR: diff requires two file paths\n"
        if not os.path.isfile(p):
            return f"ERROR: file not found: {p}\n"

    try:
        with open(path1, "r", errors="replace", encoding="utf-8") as f:
            lines1 = f.readlines()
        with open(path2, "r", errors="replace", encoding="utf-8") as f:
            lines2 = f.readlines()
    except OSError as e:
        return f"ERROR: could not read file: {e}\n"

    diff = list(difflib.unified_diff(
        lines1, lines2, fromfile=path1, tofile=path2, lineterm=""
    ))
    if not diff:
        return "files are identical\n"
    return "\n".join(diff) + "\n"


def _path_meta_suffix(path: str, sample: bytes = b"") -> str:
    """Compact suffix for read/workspace meta line. Empty when nothing notable.
    Tokens: ->target [broken] | bin | non-utf8 | ? | ! | m | x | crlf | Nd|Nw|Nmo
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
    try:
        st = os.lstat(path)
        if st.st_mode & 0o111 and not os.path.isdir(path):
            parts.append("x")
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
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain", "--ignored=matching", "--", path],
            capture_output=True, text=True, timeout=2,
            cwd=os.path.dirname(os.path.abspath(path)) or ".",
        )
        if r.returncode == 0 and r.stdout:
            code = r.stdout[:2]
            if code == "??":
                parts.append("?")
            elif code == "!!":
                parts.append("!")
            elif "M" in code or "A" in code:
                parts.append("m")
    except (OSError, subprocess.TimeoutExpired):
        pass
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
        return f"ERROR: file not found: {path}\n"
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
    base = os.path.abspath(path)
    cwd = os.getcwd()

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
            out.append(f"{prefix}{f}\n")
        for d in dirs:
            if exclude_paths:
                rel = _safe_relpath(os.path.join(dir_path, d), cwd)
                if _is_excluded(rel, exclude_paths):
                    continue
            out.append(f"{prefix}{d}/\n")
            if current_depth < depth:
                _walk(os.path.join(dir_path, d), prefix + "  ", current_depth + 1)

    out.append(f"{os.path.basename(base)}/\n")
    _walk(base, "  ", 1)
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
        from shutil import which
        _CTAGS_PATH = which("ctags")
    return _CTAGS_PATH


# Language extension → tree-sitter language name
_TS_LANG_MAP: Dict[str, str] = {
    ".php": "php", ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "javascript", ".go": "go", ".rs": "rust",
    ".java": "java", ".rb": "ruby", ".c": "c", ".cpp": "cpp", ".h": "c",
    ".hpp": "cpp", ".cs": "c_sharp", ".swift": "swift", ".kt": "kotlin",
    ".scala": "scala", ".lua": "lua", ".sh": "bash", ".bash": "bash",
}

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
    if _TS_PACKAGE == "pack":
        from tree_sitter_language_pack import get_parser
    else:
        from tree_sitter_languages import get_parser
    try:
        parser = get_parser(lang_name)
    except (ImportError, Exception):
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

    def_nodes = _TS_DEF_NODES.get(lang_name, _TS_DEF_NODES_DEFAULT)
    symbols: List[Tuple[str, str, int, int, int]] = []

    def _walk(node: Any, depth: int = 0) -> None:
        node_type = node.type
        if node_type in def_nodes:
            kind = def_nodes[node_type]
            name = _ts_node_name(node, lang_name)
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
    if _TS_PACKAGE == "pack":
        from tree_sitter_language_pack import get_parser
    else:
        from tree_sitter_languages import get_parser
    try:
        parser = get_parser(lang_name)
    except (ImportError, Exception):
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
            capture_output=True, text=True, timeout=15
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
    lines = content.split("\n")

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
    path: str, exclude_paths: Tuple[str, ...]
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
    for root, dirs, filenames in os.walk(path):
        rel_root = _safe_relpath(root, cwd)
        dirs[:] = sorted(
            d for d in dirs
            if d not in skip_dirs
            and not d.startswith(".")
            and not (exclude_paths and _is_excluded(os.path.join(rel_root, d), exclude_paths))
        )
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext in _MAP_EXTENSIONS:
                files.append(os.path.join(root, fn))
    return files


MAX_MAP_FILES = 100  # Cap to prevent overwhelming output


def op_map(path: str, no_exclude: bool = False) -> str:
    """Generate a symbol map of a file or directory.

    Three-tier extraction:
      1. tree-sitter (if tree_sitter_languages is installed)
      2. ctags (if universal-ctags is on PATH)
      3. regex fallback (always available for supported extensions)

    Output: indented tree of classes/functions/methods per file.
    """
    if not path:
        return "ERROR: empty path\n"
    if not os.path.exists(path):
        return f"ERROR: path not found: {path}\n"

    files = _collect_files(path, _get_exclude_paths("map", no_exclude))
    if not files:
        return f"(no supported files found in {path})\n"

    truncated = len(files) > MAX_MAP_FILES
    files = files[:MAX_MAP_FILES]

    # Detect available tier
    use_ts = _has_tree_sitter()
    use_ctags = not use_ts and _has_ctags()

    # tier label is computed after extraction to reflect what actually produced symbols
    actual_tier: str = "regex"

    out_files: List[str] = []

    for fpath in files:
        ext = os.path.splitext(fpath)[1].lower()
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

        if not symbols_found and use_ctags:
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
            # File exists but no symbols extracted — show it as empty
            out_files.append(f"{_fwd(fpath)} ({line_count} lines)\n  (no symbols)\n")

    out = [f"({len(files)} files, tier: {actual_tier})\n"] + out_files
    if truncated:
        out.append(f"\n... (truncated at {MAX_MAP_FILES} files)\n")
    out.append("\n")
    return "".join(out)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rtk_grep_report(rtk_out: str, limit: int) -> str:
    """Wrap rtk's delegated grep output in supertool's report line (#414).

    rtk emits bare ``path:lineno:content`` lines and no report line at all, so
    a delegated grep — `grep:PATTERN:PATH`, the plainest invocation there is —
    silently dropped the result count, the limit disclosure, and #407's
    scanned-file denominator together.

    The denominator is reported as ``?`` rather than computed. rtk (0.35) shells
    out to the system grep and exposes no scanned-file count, and walking the
    tree to produce one is exactly the traversal delegation exists to avoid.
    Per #407's own principle, a gap the caller can see beats one they cannot.

    The ``?`` is never load-bearing. rtk exits non-zero when it matches nothing,
    and op_grep now falls through on empty output too, so a delegated report
    always carries at least one result — which is itself proof that files were
    scanned. The ambiguous case, zero results, always reaches the native walker
    and a real count.

    The body is passed through verbatim: re-rendering it into supertool's
    grouped layout would mean re-parsing content that may itself contain
    colons, for a cosmetic gain.

    op_grep asks rtk for `limit + 1` matches, so this sees one line past the
    cap whenever more exist (#448). The extra line is dropped and the report
    says so, which keeps the delegated path's completeness disclosure identical
    to the native walker's — the same reason the `?` denominator is printed
    rather than omitted.
    """
    lines = [ln for ln in rtk_out.splitlines() if ln.strip()]
    truncated = len(lines) > limit
    lines = lines[:limit]
    files = set()
    for ln in lines:
        m = re.match(r"^(.+?):\d+:", ln)
        if m:
            files.add(m.group(1))
    header = (f"({len(lines)} results in {len(files)} files"
              ", scanned ? files — delegated to rtk"
              f", limit {limit}{_truncation_suffix(truncated)})\n")
    return header + "".join(ln + "\n" for ln in lines) + "\n"


def _truncation_suffix(truncated: bool) -> str:
    """Report format's truncation disclosure (#448).

    `(1 results in 1 files, scanned 118353 files, limit 1)` reads as an
    exhaustive answer and is not one, which is how a coverage audit concluded a
    class had no test when the test was sitting one match past the cap. The
    marker is only ever emitted when a match past the limit was actually seen,
    so its absence is a positive statement: this count is exact.
    """
    return " — TRUNCATED, more matches exist" if truncated else ""


def _trim_context_groups(
    groups: List[List[Tuple[str, int, str, str]]], limit: int
) -> Tuple[List[List[Tuple[str, int, str, str]]], bool]:
    """Cut context-mode groups back to `limit` matches; report whether any were cut.

    The over-fetched match may share a group with the last kept one (matches
    within 2*context+1 lines merge into a single window), so the cut has to
    happen inside the group rather than by dropping whole groups — dropping the
    group would take the limit-th match with it. A group left holding only
    context lines is dropped: context without its match is noise.
    """
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


def _scanned_suffix(scanned: int) -> str:
    """Report format's ", scanned N files" clause (#407).

    A zero scanned count means the path/glob resolved to nothing, so the
    zero-result report above it must not read like a completed search.
    """
    if scanned == 0:
        return ", scanned 0 files — nothing matched the path/glob"
    return f", scanned {scanned} files"


def _grep_count(
    pattern: str, path: str, limit: int,
    exclude_paths: Tuple[str, ...] = (),
    candidates: Optional[List[str]] = None,
) -> Dict[str, int]:
    """Return match counts per file as {filepath: count}."""
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
    path: str, exclude_paths: Tuple[str, ...] = ()
) -> List[str]:
    """Return list of file paths to search for a given path argument.

    When exclude_paths is provided, directories whose path-relative-to-cwd
    starts with one of the prefixes are pruned at the walk boundary (dirs[:]
    mutation) so their subtrees are never opened. Gitignored directories are
    pruned at the same boundary (#449) — and because the pruning happens
    before the files are collected, the returned length is what op_grep reports
    as `scanned N`, so #407's denominator shrinks with the walk instead of
    counting agent worktrees six times over.
    """
    candidates: List[str] = []
    if os.path.isfile(path):
        candidates.append(path)
    elif os.path.isdir(path):
        exts = _grep_file_includes()  # None = all files
        cwd = os.getcwd()
        ignored = _git_ignored_dirs(path) if exclude_paths else frozenset()
        for root, dirs, files in os.walk(path):
            if exclude_paths:
                rel_root = _safe_relpath(root, cwd)
                dirs[:] = [
                    d for d in dirs
                    if not _is_excluded(os.path.join(rel_root, d), exclude_paths)
                    and not _is_git_ignored(rel_root, d, ignored)
                ]
            for name in files:
                if exts is None or any(name.endswith(ext.lstrip("*")) for ext in exts):
                    candidates.append(os.path.join(root, name))
    return candidates


def _grep_recursive(
    pattern: str, path: str, limit: int,
    exclude_paths: Tuple[str, ...] = (),
    candidates: Optional[List[str]] = None,
) -> List[Tuple[str, int, str]]:
    """Return up to `limit` matches as (file_path, lineno, content) tuples.

    Filters by common code/doc extensions when walking directories.
    Always searches when `path` is a single file. file_path is normalised
    to forward slashes; the colon in a Windows drive letter (`C:/...`)
    would break a string-joined return shape on path:lineno:content split.
    """
    try:
        regex = re.compile(pattern)
    except re.error:
        # Fall back to literal substring
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
    """Return match groups with surrounding context lines.

    Each group is a list of (file_path, lineno, kind, content) tuples where
    kind is 'match' or 'context'. Groups represent adjacent/overlapping windows
    of lines. Non-adjacent groups are separated in output by --.

    Stops collecting new match groups once `limit` matches have been found.
    """
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

        # Collect match indices
        match_indices = [
            i for i, line in enumerate(lines) if regex.search(line)
        ]
        if not match_indices:
            continue

        # Merge overlapping windows into groups
        # A window is [match - context, match + context]
        windows: List[Tuple[int, int]] = []  # (start_idx, end_idx) inclusive
        for mi in match_indices:
            w_start = max(0, mi - context)
            w_end = min(len(lines) - 1, mi + context)
            if windows and w_start <= windows[-1][1] + 1:
                # Overlapping or adjacent — extend
                windows[-1] = (windows[-1][0], max(windows[-1][1], w_end))
            else:
                windows.append((w_start, w_end))

        # Build groups from windows
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


def _glob_files(
    pattern: str, exclude_paths: Tuple[str, ...] = (), over_fetch: int = 0
) -> List[str]:
    """Glob matching files, supports ** recursive. Returns up to MAX_GLOB_RESULTS.

    `over_fetch` raises the internal cap by that many files without changing
    the cap callers are told about. op_glob passes 1 so it can tell a list that
    happens to be cap-length from one that was cut short (#448).

    When exclude_paths is provided and the pattern contains '**', uses an
    os.walk-based implementation that prunes excluded directories at the walk
    boundary (never opens them).  For non-recursive patterns, falls back to
    glob.glob and filters results post-hoc (no subtree to prune anyway).
    """
    max_results = _get_op_int("glob", "max_results", MAX_GLOB_RESULTS) + over_fetch

    # Brace expansion: `*.{json,xml}` → fan out + dedupe. Shell/fd semantics.
    expanded = _expand_braces(pattern)
    if expanded != [pattern]:
        seen: set = set()
        results: List[str] = []
        for sub_pattern in expanded:
            for f in _glob_files(sub_pattern, exclude_paths, over_fetch):
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
        ignored = _git_ignored_dirs(root_part)
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
        ignored = _git_ignored_dirs(_glob_ignore_root(pattern))
        files_out = [
            m for m in files_out
            if not _is_excluded(_safe_relpath(m, cwd), exclude_paths)
            and not _under_git_ignored(_safe_relpath(m, cwd), ignored)
        ]
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


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_DRIVE_LETTER = re.compile(r"^@?[A-Za-z]$")
_URL_SCHEMES = ("http", "https", "ftp", "ftps", "ssh", "git", "file", "ws", "wss")
# Numeric port, optionally followed by '/path' or '?query' or end — used to
# absorb 'https://host' + ':8080/path' fragments that arose from `:`-splitting.
_URL_PORT = re.compile(r"^\d+(?:[/?#].*)?$")


# NUL-bracketed marker for the two-pass `\\` protection in _decode_escapes.
# NUL is extremely unlikely in CLI args, and pass 3 replaces it with a literal
# backslash so it never reaches output.
_DECODE_ESCAPES_SENTINEL = "\x00BS\x00"


def _decode_escapes(s: str) -> str:
    r"""Decode shell-style escape sequences in mutating-op arguments.

    Used by `replace`, `replace_dry`, `edit`, `replace_lines`, `vi` so callers
    can pass multi-line OLD/NEW/CONTENT via CLI without literal `\n` polluting
    file contents.

    Decoded sequences:
      `\n` `\t` `\r`           → newline / tab / CR
      `\\`                     → literal `\`
      `\<punctuation>`         → drop `\` (shell-style defensive escape:
                                  `\)`, `\(`, `\$`, `\"`, `\'`, `\ `, `\!`...
                                  Kevin's defensive `\)` becomes `)`).
      `\<letter|digit>`        → preserved as-is. PHP namespace `\Foo`,
                                  regex char classes `\d`/`\w`/`\s`, and
                                  `:s` backrefs `\1`..`\9` all survive.

    A two-pass sentinel keeps `\\` (literal backslash) intact across the
    other substitutions.
    """
    if "\\" not in s:
        return s
    out = s.replace("\\\\", _DECODE_ESCAPES_SENTINEL)
    out = out.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
    # `\xHH` (two hex digits) → single char. Useful inside TEXT to embed
    # bytes like `\x27` (single quote) without bash single-quote nesting.
    out = re.sub(
        r"\\x([0-9A-Fa-f]{2})",
        lambda m: chr(int(m.group(1), 16)),
        out,
    )
    # Drop `\` before punctuation/symbols (defensive shell escapes).
    out = re.sub(r"\\([^A-Za-z0-9])", r"\1", out)
    out = out.replace(_DECODE_ESCAPES_SENTINEL, "\\")
    return out


def _split_arg(arg: str) -> List[str]:
    """Split 'op:arg1:arg2:arg3' by ':' but reassemble drive letters and URLs.

    Splits on every ':' (no limit) then merges:
      - Single-letter pieces followed by slash/backslash → Windows drive letter
      - URL-scheme pieces (http, https, ftp, ssh, git, file, ws...) followed
        by '//...' → URL. Scheme detection looks at the LAST '|'-separated
        segment of the piece, so URLs work when embedded as one of several
        '|'-separated args (e.g. publish ops with TITLE|FILE|URL|TAGS|COVER).
      - URLs with a host already absorbed, followed by a numeric port
        (optionally with a path) → URL with port.

    Examples:
        'read:foo.py'                          → ['read', 'foo.py']
        'read:C:\\Users\\file.py'              → ['read', 'C:\\Users\\file.py']
        'grep:pat:C:/src:20'                   → ['grep', 'pat', 'C:/src', '20']
        'op:T|F|https://x.com/a|tag'           → ['op', 'T|F|https://x.com/a|tag']
        'op:T|F|https://a.com|t|https://b'     → ['op', 'T|F|https://a.com|t|https://b']
        'op:T|https://example.com:8080/path|x' → ['op', 'T|https://example.com:8080/path|x']
    """
    raw = arg.split(":")  # Full split — drive letters and URLs rejoined below
    tokens: List[str] = []
    i = 0
    while i < len(raw):
        piece = raw[i]
        # Greedily absorb next pieces if current looks like a drive letter or URL scheme
        while i + 1 < len(raw):
            next_piece = raw[i + 1]
            last_seg = piece.rsplit("|", 1)[-1]
            # Drive-letter detection also splits on ',' so a comma-joined
            # multi-path keeps reassembling each member's drive letter
            # (e.g. 'C:\a.php,C' + '\b.php' → 'C:\a.php,C:\b.php' for the
            # validate list form). The drive letter is the last ','/'|'-segment.
            drive_seg = last_seg.rsplit(",", 1)[-1]
            is_drive = (
                _DRIVE_LETTER.match(drive_seg) is not None
                and next_piece
                and next_piece[0] in ("/", "\\")
            )
            is_url = (
                last_seg.lower() in _URL_SCHEMES
                and next_piece.startswith("//")
            )
            # Port absorption: piece already has '://' (URL host absorbed last
            # iteration), and next piece is purely numeric or numeric+path.
            # 'https://example.com' + '8080/path' → 'https://example.com:8080/path'.
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


def _parse_grep_args(parts: List[str]) -> tuple:
    """Parse grep tokens, handling '::' in patterns (e.g. Class::CONST).

    Format: grep:PATTERN:PATH:LIMIT:CONTEXT:count
    The challenge: PATTERN may contain ':' (PHP ::, URL schemes, etc.).
    Strategy: parse known trailing fields (count, context, limit) from the
    right, then the path, and rejoin everything left as the pattern.
    """
    # parts[0] is 'grep', work with parts[1:]
    args = parts[1:]
    if not args:
        return ("", ".", _get_op_int("grep", "max_results", MAX_GREP_RESULTS), 0, False, False)

    # Peel known trailing string flags from the right (order-independent)
    count_only = False
    no_auto_read = False
    while args and args[-1] in ("count", "no-auto-read"):
        if args[-1] == "count":
            count_only = True
        else:
            no_auto_read = True
        args = args[:-1]

    # Peel trailing ints: format is ...PATH:LIMIT:CONTEXT
    # Two trailing ints = limit + context; one trailing int = limit only
    context = 0
    limit = _get_op_int("grep", "max_results", MAX_GREP_RESULTS)
    trailing_ints = []
    while len(args) >= 3 and args[-1].isdigit():
        trailing_ints.insert(0, int(args[-1]))
        args = args[:-1]
    if len(trailing_ints) == 1:
        limit = trailing_ints[0]
    elif len(trailing_ints) >= 2:
        limit = trailing_ints[0]
        context = trailing_ints[1]

    # Now args should be [pattern_parts..., path]
    # The path is the last element; everything before it is the pattern
    if len(args) >= 2:
        path = args[-1] if args[-1] else "."
        pattern = ":".join(args[:-1])
    else:
        # Single token: pattern only, no path
        pattern = args[0] if args else ""
        path = "."

    return (pattern, path, limit, context, count_only, no_auto_read)


def _parse_around_args(parts: List[str]) -> tuple:
    """Parse around tokens, handling '::' in patterns.

    Format: around:PATTERN:PATH:N
    Strategy: peel trailing int (N) from right, then path, rejoin rest as pattern.
    """
    args = parts[1:]
    if not args:
        return ("", "", 10)

    # Peel N (int) from right
    n = 10
    if len(args) >= 3 and args[-1].isdigit():
        n = int(args[-1])
        args = args[:-1]

    # Last token is path, everything before is pattern
    if len(args) >= 2:
        path = args[-1] if args[-1] else ""
        pattern = ":".join(args[:-1])
    else:
        # Single token: pattern only, no path
        pattern = args[0] if args else ""
        path = ""

    return (pattern, path, n)


def op_replace(old: str, new: str, path: str = ".", dry: bool = False) -> str:
    """Find and replace text across files. Supports dry-run preview.

    Searches recursively through `path` (respecting grep file includes),
    finds all occurrences of `old`, and either previews (dry=True) or
    executes (dry=False) the replacement.

    Output format:
      - Dry mode: diff-style preview (- old / + new) per occurrence
      - Execute mode: compact receipt (files modified, counts)
    """
    if not old:
        return "ERROR: empty search pattern\n"
    if old == new:
        return "ERROR: old and new strings are identical\n"
    if not path:
        return "ERROR: empty path\n"

    # Validate path exists
    if path != "." and not os.path.isfile(path) and not os.path.isdir(path):
        return f"ERROR: path not found: {path}\n"

    candidates = _grep_candidates(path, _get_exclude_paths("replace"))
    if not candidates:
        return "(0 files to search)\n"

    # Collect matches via whole-file scan so multi-line `old` patterns work.
    # Line-by-line matching would silently miss any pattern containing '\n'.
    file_matches: List[Tuple[str, List[int]]] = []  # (filepath, [match_start_offsets])
    total_count = 0
    for file_path in candidates:
        try:
            with open(file_path, "rb") as f_bin:
                head = f_bin.read(4096)
            if b"\x00" in head:
                # Binary file — skip. Avoids regex-sub corrupting git internals,
                # images, compiled blobs, etc. (recovered cost: a `.git/index`
                # walked into by a stray relative path arg.)
                continue
            with open(file_path, "r", encoding="utf-8", errors="surrogateescape") as f:
                content = f.read()
        except OSError:
            continue
        positions: List[int] = []
        start = 0
        while True:
            idx = content.find(old, start)
            if idx == -1:
                break
            positions.append(idx)
            start = idx + len(old)
        if positions:
            file_matches.append((file_path, positions))
            total_count += len(positions)

    if total_count == 0:
        return f"(0 occurrences of '{old}' found)\n"

    if dry:
        out: List[str] = [f"({total_count} occurrences in {len(file_matches)} files)\n"]
        old_lines = old.split("\n")
        new_lines = new.split("\n")
        for filepath, positions in file_matches:
            out.append(f"\n{_fwd(filepath)}\n")
            try:
                with open(filepath, "r", encoding="utf-8", errors="surrogateescape") as f:
                    content = f.read()
            except OSError:
                continue
            for pos in positions:
                start_line = content.count("\n", 0, pos) + 1
                end_line = start_line + len(old_lines) - 1
                label = f"L{start_line}" if start_line == end_line else f"L{start_line}-L{end_line}"
                out.append(f"  {label}:\n")
                for ol in old_lines:
                    out.append(f"    - {ol}\n")
                for nl in new_lines:
                    out.append(f"    + {nl}\n")
        out.append(f"\nSummary: {total_count} replacements in {len(file_matches)} files (DRY RUN — no files modified)\n")
        return "".join(out)

    # Execute mode
    files_modified: Dict[str, int] = {}
    for file_path, positions in file_matches:
        try:
            with open(file_path, "r", encoding="utf-8", errors="surrogateescape") as f:
                content = f.read()
        except OSError:
            continue
        new_content = content.replace(old, new)
        try:
            _atomic_write(file_path, new_content)
            files_modified[file_path] = len(positions)
        except OSError as e:
            return f"ERROR: failed to write {file_path}: {e}\n"

    total = sum(files_modified.values())
    out = [f"({total} replacements in {len(files_modified)} files)\n"]
    for fp, cnt in sorted(files_modified.items()):
        out.append(f"  {_fwd(fp)} ({cnt})\n")
    out.append(f"\nDone: '{old}' → '{new}'\n")
    return "".join(out)


def _atomic_write(path: str, content: str) -> None:
    """Write content to path atomically — temp file + os.replace.

    Enforces _safe_path containment (closes #146) — the resolved path must
    live under cwd unless SUPERTOOL_ALLOW_OUTSIDE_CWD=1 is set. Single
    chokepoint for all mutation ops (paste/edit/replace_lines/vim/replace).

    If `path` is a symlink, follow it to the real target — otherwise
    os.replace would clobber the symlink with a regular file, leaving the
    real file untouched (silent data divergence). The symlink target itself
    is checked against cwd containment — a symlink in the repo pointing at
    /etc/hosts is rejected.

    Crash-safe: if interrupted mid-write, the original file is preserved
    (the temp file is incomplete but the target path still has old data).

    Uses surrogateescape encoding so any bytes that round-tripped through
    read(errors='surrogateescape') survive the write unchanged — protects
    files containing illegal UTF-8 sequences (binary blobs, partial encodes).
    """
    import tempfile
    # Containment check happens against the symlink target (real path) so a
    # symlinked write doesn't escape cwd via the symlink itself.
    _safe_path(path)
    # Byte-pattern warnings a syntax validator structurally cannot catch, raised
    # at the one place every mutating op passes through (#380).
    _warn = _sh_backslash_warning(path, content)
    _key = os.path.abspath(path)
    real_path = os.path.realpath(path) if os.path.islink(path) else path
    target_dir = os.path.dirname(os.path.abspath(real_path)) or "."
    # Preserve the original file's mode (#259). mkstemp creates the temp file
    # with 0600; os.replace would then clobber the target's mode, silently
    # dropping the executable bit on scripts/git hooks. Capture the existing
    # mode and re-apply it to the temp file before the rename. A brand-new
    # file keeps mkstemp's default — no spurious +x.
    try:
        orig_mode = stat.S_IMODE(os.stat(real_path).st_mode)
    except OSError:
        orig_mode = None
    fd, tmp_path = tempfile.mkstemp(
        prefix=".supertool-", suffix=".tmp", dir=target_dir
    )
    try:
        # Open in binary mode + encode manually so Windows text-mode doesn't
        # translate `\n` → `\r\n` (which corrupts mixed-line-ending content,
        # binary blobs, and any caller that round-tripped through
        # `read(errors='surrogateescape')`).
        with os.fdopen(fd, "wb") as f:
            f.write(content.encode("utf-8", errors="surrogateescape"))
        if orig_mode is not None:
            os.chmod(tmp_path, orig_mode)
        os.replace(tmp_path, real_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    # Only past the rename: the counter says "bytes reached disk", and the
    # warning queue must describe what is on disk. A write that raised did
    # neither, and counting it would let dispatch treat a failed op as a
    # successful one.
    _WRITE_COUNT[0] += 1
    # A rollback rewrites the same path, so drop any stale entry for it first.
    _drop_write_warnings(path)
    if _warn:
        _WRITE_WARNINGS.append((_key, _warn))


_BRANCH_CACHE: List[Optional[str]] = [None]

# Above this, the nearest-line scan is skipped — the diagnostic is a courtesy
# on a failure path and must never become the slow part of a failed edit.
_EDIT_DIAG_MAX_LINES = 20000


def _current_branch() -> str:
    """Current git branch, or "" when there isn't one to report.

    Cached for the process: a single supertool invocation cannot change branch
    mid-call, and a batch of edits would otherwise pay a subprocess each.
    Returns "" for a non-repo, a missing git, or any error — the branch is a
    convenience on the receipt and must never be the thing that fails an op.
    """
    if _BRANCH_CACHE[0] is None:
        branch = ""
        try:
            # symbolic-ref, not rev-parse: it resolves an *unborn* branch (a
            # fresh `git init` before the first commit), where rev-parse fails
            # with "ambiguous argument 'HEAD'". It exits non-zero on a detached
            # HEAD, which is the one case worth a second call.
            r = subprocess.run(
                ["git", "symbolic-ref", "--short", "-q", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                branch = r.stdout.strip()
            else:
                d = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=5,
                )
                if d.returncode == 0 and d.stdout.strip():
                    branch = f"detached HEAD at {d.stdout.strip()}"
        except (subprocess.TimeoutExpired, OSError):
            branch = ""
        _BRANCH_CACHE[0] = branch
    return _BRANCH_CACHE[0] or ""


def _branch_line() -> str:
    """`[branch: X]` footer for a mutating op (#381).

    Two near-misses in one session were the same shape — right file, wrong
    branch — and supertool is the thing that knows. A handful of tokens per
    call, against a class of mistake that is otherwise silent until commit time.
    """
    branch = _current_branch()
    return f"[branch: {branch}]\n" if branch else ""


def _normalise_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _edit_miss_diagnostic(old: str, content: str) -> str:
    """Why `old` didn't match (#380).

    `ERROR: old string not found` was the whole message, so the natural next
    move was a `read` round-trip — the one the payload route exists to save.
    These are the three ways a payload comes back close but not exact, ranked
    by how often each was the real cause.
    """
    hints: List[str] = []

    # 1. Doubled backslashes. TOML literal strings ('''...''') do not process
    #    escapes, so `\\302` in a payload is backslash-backslash-3-0-2 and can
    #    never match a file holding `\302`.
    if "\\\\" in old and old.replace("\\\\", "\\") in content:
        hints.append(
            "the file matches with SINGLE backslashes — TOML literal strings "
            "('''...''') do not process escapes, so `\\\\` in the payload is "
            "two literal backslashes"
        )

    lines = content.splitlines()
    if len(lines) > _EDIT_DIAG_MAX_LINES:
        return "".join(f"  {mark('↳')} {h}\n" for h in hints)

    # 2. Whitespace. Indentation drift is the other half of "close but not
    #    exact", and it is invisible in a diff read by eye.
    if not hints:
        norm_old = _normalise_ws(old)
        if norm_old and "\n" not in old.strip():
            for i, ln in enumerate(lines, 1):
                if _normalise_ws(ln) == norm_old:
                    hints.append(
                        f"line {i} matches ignoring whitespace: {ln.strip()!r} "
                        f"— check indentation"
                    )
                    break

    # 3. Nearest line. Even one line of "closest is line 142: ..." turns a
    #    three-call debug loop into one.
    if not hints:
        first = next((ln for ln in old.splitlines() if ln.strip()), "")
        if first:
            best_ratio, best_i = 0.0, 0
            matcher = difflib.SequenceMatcher(a=first, autojunk=False)
            for i, ln in enumerate(lines, 1):
                matcher.set_seq2(ln)
                if matcher.real_quick_ratio() <= best_ratio:
                    continue
                if matcher.quick_ratio() <= best_ratio:
                    continue
                ratio = matcher.ratio()
                if ratio > best_ratio:
                    best_ratio, best_i = ratio, i
            if best_ratio >= 0.6:
                hints.append(
                    f"nearest match at line {best_i} ({best_ratio:.0%}): "
                    f"{lines[best_i - 1]!r}"
                )

    return "".join(f"  {mark('↳')} {h}\n" for h in hints)
# A mutating op prints its full arguments in the section header and then the
# diff underneath, so for a content-heavy edit the old and new strings appear
# twice. Above this many characters the header is rebuilt from the parsed
# fields instead — the diff below is the useful part and already shows what
# changed. Short ops keep their verbatim header (#384).
_HEADER_ARG_MAX = 160
_HEADER_ANCHOR_MAX = 60

_SH_SUFFIXES = (".sh", ".bash", ".zsh", ".ksh")
# A run of backslashes at end of line, plus any whitespace between it and the
# newline. Two ways this is not the continuation it looks like:
#   - PARITY: bash consumes backslashes pairwise from the left, so an even run
#     is all escaped backslashes and the line genuinely ends; an odd run leaves
#     one over to continue it. Only even runs are the bug.
#   - TRAILING WHITESPACE: a backslash followed by spaces or tabs never
#     continues the line, whatever the parity — the escape applies to the
#     space. Invisible in a diff, and it silently ends a command.
_TRAILING_BACKSLASH_RUN = re.compile(r"(\\+)([ \t]*)\r?\n")

# Warnings raised at the write chokepoint, drained by dispatch onto the
# receipt. Keyed by path so a rollback can retract the warning for the
# content it just reverted — the bytes complained about are no longer on
# disk, and a warning about them would be worse than none.
_WRITE_WARNINGS: List[Tuple[str, str]] = []

# Bumped when a mutating op RUNS, before its outcome is known — the branch
# footer's signal. A write counter cannot serve that role: `_retract_write`
# decrements on rollback and a failed edit never reaches `_atomic_write` at all,
# so keying the footer on bytes-that-landed silently drops the two cases where a
# wrong-branch hypothesis is most useful. Read once per call by dispatch; never
# decremented.
_MUTATION_ATTEMPTS: List[int] = [0]

# Bumped by _atomic_write. Lets dispatch ask 'did this op actually write?'
# instead of sniffing the receipt for an ERROR prefix — receipts are prose
# and not every no-op failure says ERROR (op_replace's zero-match returns
# "(0 occurrences of 'x' found)").
_WRITE_COUNT: List[int] = [0]


def _drop_write_warnings(path: str) -> None:
    """Retract queued warnings for `path` — its write was rolled back."""
    key = os.path.abspath(path)
    _WRITE_WARNINGS[:] = [w for w in _WRITE_WARNINGS if w[0] != key]


def _retract_write(path: str) -> None:
    """A rollback reverted a write: un-count it and drop its warnings.

    Both rollback paths restore the previous bytes with a raw write that never
    reaches `_atomic_write`, so without this the op still looks like it wrote.
    That matters beyond tidiness: the compact header is gated on the counter,
    and a change that did not stick leaves no diff to read it from — the same
    reproducibility gap the header rule exists to avoid.
    """
    _drop_write_warnings(path)
    if _WRITE_COUNT[0] > 0:
        _WRITE_COUNT[0] -= 1


def _elide(s: str, limit: int) -> str:
    """One-line, length-capped rendering of an op argument for a header.

    Reports the elided character count rather than trailing off — a silent
    truncation reads as "that was the whole argument".
    """
    s = s.replace("\r\n", "⏎").replace("\n", "⏎")
    if len(s) <= limit:
        return s
    return f"{s[:limit]}… (+{len(s) - limit} chars)"


def _compact_header_arg(op: str, parts: List[str]) -> str:
    """Identifying header for a content-heavy mutating op, or "" to keep the
    verbatim one. Each op keeps whatever identifies the *target* — path, line
    range, anchor — and drops the content the diff is about to show anyway."""
    def _p(i: int) -> str:
        return parts[i] if len(parts) > i else ""

    if op in ("edit", "replace", "replace_dry"):
        return f'{op}: "{_elide(_p(1), _HEADER_ANCHOR_MAX)}" → {_p(3)}'
    if op == "replace_lines":
        return f"{op}: {_p(1)} lines {_p(2)}-{_p(3)}"
    if op in ("paste", "append"):
        content = ":".join(parts[2:])
        return f"{op}: {_p(1)} ({len(content)} chars)"
    if op == "vim":
        return f"{op}: {_p(1)} {_elide(':'.join(parts[2:]), _HEADER_ANCHOR_MAX)}"
    return ""


def _sh_backslash_warning(path: str, content: str) -> str:
    """Flag `\\\\` at end-of-line in a shell script (#380).

    The trap is that getting the escaping wrong does not always fail to match —
    sometimes it writes. `FOO=$(cmd \\\\` + newline is syntactically valid bash
    (an escaped backslash, then a new command), passes `bash -n`, passes the
    bash-check validator, and does something entirely different from what was
    meant. The validator cannot see it; only the byte pattern can.
    """
    if not path.endswith(_SH_SUFFIXES):
        return ""
    trailing_ws = False
    even_run = False
    for m in _TRAILING_BACKSLASH_RUN.finditer(content):
        if m.group(2):
            trailing_ws = True
        elif len(m.group(1)) % 2 == 0:
            even_run = True
    if not (trailing_ws or even_run):
        return ""
    if trailing_ws and not even_run:
        return (
            f"{mark('⚠')} {path}: a line ends with a backslash followed by "
            f"whitespace. That is not a line continuation — the backslash "
            f"escapes the space, the command ends there, and the difference "
            f"is invisible in a diff.\n"
        )
    return (
        f"{mark('⚠')} {path}: a line ends with `\\\\`. In bash that is an "
        f"escaped backslash, not a line continuation — it parses cleanly and "
        f"runs differently. TOML literal strings ('''...''') preserve "
        f"backslashes verbatim, so `\\\\` in a payload stays two.\n"
    )


def op_edit(old: str, new: str, path: str) -> str:
    """Single-file, single-occurrence edit — mirrors native Edit semantics.

    Errors on 0 or >1 matches. For replace_all, use the `replace` op.
    Returns a receipt with ±2 lines of context around the change.
    """
    if not old:
        return "ERROR: empty old string\n"
    if old == new:
        return "ERROR: old and new strings are identical\n"
    if not path:
        return "ERROR: empty path\n"
    if not os.path.isfile(path):
        return f"ERROR: file not found: {path}\n"

    try:
        # surrogateescape: lone bytes that aren't valid UTF-8 round-trip via
        # _atomic_write back to their original byte values. Prevents silent
        # corruption of bytes outside the match window (CVE-class data loss
        # on files with mixed encodings or partial binary content).
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
            content = f.read()
    except OSError as e:
        return f"ERROR: failed to read {path}: {e}\n"

    count = content.count(old)
    if count == 0:
        return (f"ERROR: old string not found in {path}\n"
                + _edit_miss_diagnostic(old, content))
    if count > 1:
        return (
            f"ERROR: old string found {count} times in {path} — ambiguous. "
            f"Use a larger snippet to make it unique, or use replace for "
            f"replace_all semantics.\n"
        )

    new_content = content.replace(old, new, 1)
    try:
        _atomic_write(path, new_content)
    except OSError as e:
        return f"ERROR: failed to write {path}: {e}\n"

    # Receipt — locate the change and show ±2 lines context
    pre = content[: content.index(old)]
    start_line = pre.count("\n") + 1
    new_lines = new_content.splitlines()
    new_block_line_count = new.count("\n") + 1
    end_line = start_line + new_block_line_count - 1
    ctx_start = max(1, start_line - 2)
    ctx_end = min(len(new_lines), end_line + 2)

    out = [f"edited {path} (line {start_line}"]
    if end_line != start_line:
        out.append(f"-{end_line}")
    out.append(")\n")
    for ln in range(ctx_start, ctx_end + 1):
        marker = "→" if start_line <= ln <= end_line else " "
        out.append(f"  {ln:>5} {marker} {new_lines[ln - 1]}\n")
    return "".join(out)


def op_paste(path: str, content: str) -> str:
    """Replace entire file with content. Atomic. Creates file (and parent dirs)
    if missing.

    Use for full-file rewrites — no vim macro gymnastics, no `:r` insert-after
    off-by-one cuts (e.g. `<?php` eaten), no `:::` separator abuse. CONTENT
    arrives via triple-colon separator so it can hold any chars (`:`, quotes,
    braces, newlines).
    """
    if not path:
        return "ERROR: empty path\n"
    # Containment check BEFORE makedirs — otherwise a path like
    # `../../tmp/evil/foo` would create directories outside cwd before
    # _atomic_write's own _safe_path check rejects the write itself.
    try:
        safe_resolved = _safe_path(path)
    except SecurityError as e:
        return f"ERROR: {e}\n"
    parent = os.path.dirname(safe_resolved)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return f"ERROR: failed to create parent dir {parent}: {e}\n"
    # Ensure trailing newline (POSIX text files)
    if content and not content.endswith("\n"):
        content += "\n"
    existed = os.path.isfile(path)
    old_size = os.path.getsize(path) if existed else 0
    try:
        _atomic_write(path, content)
    except OSError as e:
        return f"ERROR: failed to write {path}: {e}\n"
    new_size = len(content.encode("utf-8"))
    new_lines = content.count("\n")
    verb = "rewrote" if existed else "created"
    return (
        f"{verb} {path} ({new_lines} lines, {old_size} → {new_size} bytes)\n"
    )


_APPEND_RECEIPT_LINES = 10


def op_append(path: str, content: str) -> str:
    """Append content to the end of a file. Atomic. Creates it if missing.

    Appending used to need two calls: `wc:PATH` to learn the line count, then
    `replace_lines` with `start = N+1, end = N` — the inverted-range insert
    form. That is a round-trip spent computing an argument, and `887:886` reads
    like a typo to whoever reviews the command later.

    A file whose last line has no trailing newline gets one first, so the
    appended block always starts on its own line; the receipt says so, since
    silently touching a byte the caller did not ask about is worth a word.
    """
    if not path:
        return "ERROR: empty path\n"
    if not content:
        return "ERROR: empty content — nothing to append\n"
    # Containment check BEFORE makedirs, same ordering as op_paste: a path like
    # `../../tmp/evil/foo` must not create directories outside cwd.
    try:
        safe_resolved = _safe_path(path)
    except SecurityError as e:
        return f"ERROR: {e}\n"
    if os.path.isdir(path):
        # Explicit, unlike op_paste which lets _atomic_write's OSError surface.
        # `append` is the op most likely to be aimed at a directory by mistake
        # — a notes/ or logs/ path with the filename left off — and saying so
        # beats an mkstemp errno.
        return f"ERROR: {path} is a directory\n"
    parent = os.path.dirname(safe_resolved)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return f"ERROR: failed to create parent dir {parent}: {e}\n"

    existed = os.path.isfile(path)
    orig = ""
    if existed:
        try:
            # surrogateescape: lone non-UTF-8 bytes round-trip through
            # _atomic_write instead of being mangled to U+FFFD — same contract
            # as op_edit / op_replace_lines.
            # newline="": no universal-newline translation, so a CRLF file is
            # not silently rewritten to LF throughout. An append touches the
            # end of the file; every other byte must come back out unchanged.
            with open(path, "r", encoding="utf-8", errors="surrogateescape",
                      newline="") as f:
                orig = f.read()
        except OSError as e:
            return f"ERROR: failed to read {path}: {e}\n"

    block = content if content.endswith("\n") else content + "\n"
    newline_hint = ""
    if orig and not orig.endswith(("\n", "\r")):
        # Match the file's own convention rather than imposing LF on a CRLF file.
        orig += "\r\n" if "\r\n" in orig else "\n"
        newline_hint = " [added the missing trailing newline first]"

    old_size = len(orig.encode("utf-8", errors="surrogateescape")) if existed else 0
    new_content = orig + block
    try:
        _atomic_write(path, new_content)
    except OSError as e:
        return f"ERROR: failed to write {path}: {e}\n"

    all_lines = new_content.splitlines()
    added = block.count("\n")
    start_line = len(all_lines) - added + 1
    new_size = len(new_content.encode("utf-8", errors="surrogateescape"))
    verb = "appended to" if existed else "created"
    out = [
        f"{verb} {path}: {added} lines at {start_line}-{len(all_lines)} "
        f"({old_size} → {new_size} bytes){newline_hint}\n"
    ]
    # Receipt shows 2 lines of preceding context so the caller can see what the
    # block landed after, then the block itself — capped, because append is the
    # op you reach for with a long changelog entry and echoing it back in full
    # is pure token cost on content the caller already had.
    ctx_start = max(1, start_line - 2)
    shown_end = min(len(all_lines), start_line + _APPEND_RECEIPT_LINES - 1)
    for ln in range(ctx_start, shown_end + 1):
        marker = "→" if ln >= start_line else " "
        out.append(f"  {ln:>5} {marker} {all_lines[ln - 1]}\n")
    if shown_end < len(all_lines):
        out.append(f"  … (+{len(all_lines) - shown_end} more appended lines)\n")
    return "".join(out)


def op_replace_lines(path: str, start: int, end: int, content: str) -> str:
    """Replace lines [start, end] (1-indexed, inclusive) with content.

    Modes (all via the same op):
      insert  — end < start (e.g. 42:41) inserts CONTENT before line `start`
      replace — end >= start, swap range with CONTENT
      delete  — empty CONTENT, removes lines in range

    Returns receipt with new line numbers + ±2 context.
    """
    if not path:
        return "ERROR: empty path\n"
    if not os.path.isfile(path):
        return f"ERROR: file not found: {path}\n"
    if start < 1:
        return f"ERROR: start ({start}) must be >= 1\n"
    if end < 0:
        return f"ERROR: end ({end}) must be >= 0\n"

    try:
        # surrogateescape (not 'replace'): round-trip lone non-UTF-8 bytes
        # via _atomic_write. 'replace' would silently mutate them to U+FFFD
        # in untouched regions — same bug fix as op_edit / op_replace.
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
            orig = f.read()
    except OSError as e:
        return f"ERROR: failed to read {path}: {e}\n"

    orig_lines = orig.splitlines(keepends=True)
    total = len(orig_lines)

    if start > total + 1:
        return f"ERROR: start ({start}) > file length ({total}) + 1\n"

    insert_only = end < start
    # Off-by-one autocorrect: END == total + 1 — Kevin guessed line count, clearly
    # meant "through EOF". Clamp + flag in receipt. END > total+1 = real mistake.
    clamped_hint = ""
    if not insert_only and end == total + 1:
        clamped_hint = f" [autocorrect: end ({end}) clamped to file length ({total})]"
        end = total
    if not insert_only and end > total:
        return f"ERROR: end ({end}) > file length ({total})\n"

    new_block = content
    if new_block and not new_block.endswith("\n"):
        new_block += "\n"
    new_block_lines = new_block.splitlines(keepends=True) if new_block else []

    if insert_only:
        before = orig_lines[: start - 1]
        after = orig_lines[start - 1:]
        removed = 0
    else:
        before = orig_lines[: start - 1]
        after = orig_lines[end:]
        removed = end - start + 1

    new_lines = before + new_block_lines + after
    try:
        _atomic_write(path, "".join(new_lines))
    except OSError as e:
        return f"ERROR: failed to write {path}: {e}\n"

    added = len(new_block_lines)
    new_start = start
    new_end = start + added - 1 if added > 0 else start - 1

    if added == 0:
        verb = f"deleted lines {start}-{end}"
    elif insert_only:
        verb = f"inserted {added} lines before line {start}"
    else:
        verb = f"replaced lines {start}-{end} with lines {new_start}-{new_end}"
    out = [f"{verb} in {path} (Δ {added - removed:+d}){clamped_hint}\n"]

    ctx_start = max(1, new_start - 2)
    ctx_end = min(len(new_lines), max(new_end, new_start) + 2)
    for ln in range(ctx_start, ctx_end + 1):
        marker = "→" if added > 0 and new_start <= ln <= new_end else " "
        text = new_lines[ln - 1].rstrip("\n")
        out.append(f"  {ln:>5} {marker} {text}\n")
    return "".join(out)



def _vim_cursor_state_path(file_path: str) -> str:
    """Return the sidecar path that persists vim cursor for `file_path`."""
    abs_path = os.path.abspath(file_path)
    digest = hashlib.sha1(abs_path.encode("utf-8")).hexdigest()
    return os.path.join(str(_cache_root() / "vim-cursor"), digest)


def _vim_load_state(file_path: str, content_len: int) -> dict:
    """Load persisted vim state for `file_path`. Returns dict with keys
    cursor (int), marks (dict[str,int]), last_edit (int|None),
    macros (dict[str,str]).
    Backward-compat: if the file is a bare int, treat as legacy cursor-only.
    """
    default = {"cursor": 0, "marks": {}, "last_edit": None, "macros": {}, "last_change": None}
    if os.environ.get("SUPERTOOL_VIM_NO_PERSIST"):
        return default
    try:
        with open(_vim_cursor_state_path(file_path), "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return default
    if not raw:
        return default
    # Try JSON dict first
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            cur = int(data.get("cursor", 0))
            marks_raw = data.get("marks", {}) or {}
            marks = {k: int(v) for k, v in marks_raw.items() if isinstance(k, str)}
            le = data.get("last_edit", None)
            le_val = int(le) if le is not None else None
            # Clamp
            cur = max(0, min(content_len, cur))
            marks = {k: max(0, min(content_len, v)) for k, v in marks.items()}
            if le_val is not None:
                le_val = max(0, min(content_len, le_val))
            macros_raw = data.get("macros", {}) or {}
            macros = {k: str(v) for k, v in macros_raw.items()
                      if isinstance(k, str) and len(k) == 1 and "a" <= k <= "z"}
            # last_change: dict with verb/count/arg for `.` repeat, or None
            lc_raw = data.get("last_change", None)
            lc_val = None
            if isinstance(lc_raw, dict):
                lc_verb = str(lc_raw.get("verb", ""))
                lc_count = int(lc_raw.get("count", 1))
                lc_arg = str(lc_raw.get("arg", ""))
                if lc_verb:
                    lc_val = {"verb": lc_verb, "count": lc_count, "arg": lc_arg}
            return {"cursor": cur, "marks": marks, "last_edit": le_val, "macros": macros, "last_change": lc_val}
    except (ValueError, TypeError):
        pass
    # Legacy: bare int
    try:
        return {
            "cursor": max(0, min(content_len, int(raw))),
            "marks": {},
            "last_edit": None,
            "macros": {},
            "last_change": None,
        }
    except ValueError:
        return default


def _vim_save_state(file_path: str, cursor: int, marks: dict, last_edit, macros: dict = None, last_change=None) -> None:
    """Persist vim state for `file_path` so the next vim call resumes here."""
    if os.environ.get("SUPERTOOL_VIM_NO_PERSIST"):
        return
    state_path = _vim_cursor_state_path(file_path)
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        lc_payload = None
        if isinstance(last_change, dict) and last_change.get("verb"):
            lc_payload = {
                "verb": str(last_change["verb"]),
                "count": int(last_change.get("count", 1)),
                "arg": str(last_change.get("arg", "")),
            }
        payload = json.dumps({
            "cursor": int(cursor),
            "marks": {k: int(v) for k, v in (marks or {}).items()},
            "last_edit": int(last_edit) if last_edit is not None else None,
            "macros": {k: str(v) for k, v in (macros or {}).items()},
            "last_change": lc_payload,
        })
        with open(state_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
    except OSError:
        pass


def _vim_load_cursor(file_path: str, content_len: int) -> int:
    """Backcompat shim: load just the cursor."""
    return _vim_load_state(file_path, content_len)["cursor"]


def _vim_save_cursor(file_path: str, cursor: int) -> None:
    """Backcompat shim: save cursor only, preserving existing marks/last_edit/macros."""
    if os.environ.get("SUPERTOOL_VIM_NO_PERSIST"):
        return
    # Preserve existing marks/last_edit/macros
    try:
        existing = _vim_load_state(file_path, 10**9)
    except Exception:
        existing = {"marks": {}, "last_edit": None, "macros": {}}
    _vim_save_state(file_path, cursor, existing.get("marks", {}), existing.get("last_edit"),
                    existing.get("macros", {}), existing.get("last_change"))



# Diagnostic from the last _vim_load_undo_snapshot call that failed with an
# OS-level error (e.g. permission denied).  Surfaced on the next `u` receipt.
_last_undo_diagnostic: "Optional[str]" = None


def _vim_undo_state_path(file_path: str) -> str:
    """Return the sidecar path for cross-call undo snapshot for `file_path`."""
    abs_path = os.path.abspath(file_path)
    digest = hashlib.sha1(abs_path.encode("utf-8")).hexdigest()
    return os.path.join(str(_cache_root() / "vim-undo"), digest + ".last")


def _vim_load_undo_snapshot(file_path: str) -> "Optional[dict]":
    """Load the cross-call undo snapshot (pre-edit state from last script).
    Returns dict with content (str), cursor (int), marks (dict) or None if absent.
    """
    if os.environ.get("SUPERTOOL_VIM_NO_PERSIST"):
        return None
    try:
        with open(_vim_undo_state_path(file_path), "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "content" in data:
            marks_raw = data.get("marks", {}) or {}
            return {
                "content": str(data["content"]),
                "cursor": int(data.get("cursor", 0)),
                "marks": {k: int(v) for k, v in marks_raw.items() if isinstance(k, str)},
            }
    except (ValueError, TypeError, KeyError):
        pass
    return None


def _vim_save_undo_snapshot(file_path: str, content: str, cursor: int, marks: dict) -> None:
    """Persist the cross-call undo snapshot (state before this script ran)."""
    if os.environ.get("SUPERTOOL_VIM_NO_PERSIST"):
        return
    undo_path = _vim_undo_state_path(file_path)
    try:
        os.makedirs(os.path.dirname(undo_path), exist_ok=True)
        payload = json.dumps({
            "content": content,
            "cursor": int(cursor),
            "marks": {k: int(v) for k, v in (marks or {}).items()},
        })
        with open(undo_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
    except OSError:
        pass

class _TextObjectError(Exception):
    """Raised when a text-object cannot be resolved (no match, EOF, etc.)."""


def _resolve_text_object(content: str, cursor: int, kind: str, around: bool) -> tuple:
    """Return (start, end) byte offsets for vim text-object at cursor.

    kind: w W s p " ' ` ( ) [ ] { } < > b B t
    around: False = inner (i<X>), True = around (a<X>)
    """
    n = len(content)
    # Normalize aliases
    if kind == "b":
        kind = "("
    elif kind == "B":
        kind = "{"
    # Pair canonical: close-bracket variant maps to its opener
    pair_close_to_open = {")": "(", "]": "[", "}": "{", ">": "<"}
    if kind in pair_close_to_open:
        kind = pair_close_to_open[kind]

    # word (iw/aw): \w run [+ trailing/leading whitespace for aw]
    if kind == "w":
        if cursor >= n:
            raise _TextObjectError("iw/aw at EOF")
        def is_word(ch: str) -> bool:
            return ch.isalnum() or ch == "_"
        c = content[cursor]
        if is_word(c):
            s = cursor
            while s > 0 and is_word(content[s - 1]):
                s -= 1
            e = cursor
            while e < n and is_word(content[e]):
                e += 1
        else:
            # cursor on non-word: text-object is the non-word run (vim parity)
            s = cursor
            while s > 0 and not is_word(content[s - 1]) and content[s - 1] not in " \t\n":
                s -= 1
            e = cursor
            while e < n and not is_word(content[e]) and content[e] not in " \t\n":
                e += 1
        if around:
            # extend over trailing whitespace (or leading if at EOL)
            ext = e
            while ext < n and content[ext] in (" ", "\t"):
                ext += 1
            if ext > e:
                e = ext
            else:
                while s > 0 and content[s - 1] in (" ", "\t"):
                    s -= 1
        return (s, e)

    # WORD (iW/aW): whitespace-separated
    if kind == "W":
        if cursor >= n:
            raise _TextObjectError("iW/aW at EOF")
        def is_ws(ch: str) -> bool:
            return ch in " \t\n"
        if is_ws(content[cursor]):
            # on whitespace — span the whitespace run for iW
            s = cursor
            while s > 0 and is_ws(content[s - 1]) and content[s - 1] != "\n":
                s -= 1
            e = cursor
            while e < n and is_ws(content[e]) and content[e] != "\n":
                e += 1
        else:
            s = cursor
            while s > 0 and not is_ws(content[s - 1]):
                s -= 1
            e = cursor
            while e < n and not is_ws(content[e]):
                e += 1
        if around:
            ext = e
            while ext < n and content[ext] in (" ", "\t"):
                ext += 1
            if ext > e:
                e = ext
            else:
                while s > 0 and content[s - 1] in (" ", "\t"):
                    s -= 1
        return (s, e)

    # sentence (is/as): ends at . ! ? followed by space/EOL
    if kind == "s":
        # find sentence start: scan back for . ! ? + whitespace, or BOF
        s = cursor
        while s > 0:
            prev = content[s - 1]
            if prev in ".!?" and s < n and content[s] in (" ", "\t", "\n"):
                # skip leading whitespace after terminator
                while s < n and content[s] in (" ", "\t"):
                    s += 1
                break
            s -= 1
        # find sentence end: forward to first . ! ? (inclusive)
        e = cursor
        while e < n and content[e] not in ".!?":
            e += 1
        if e < n:
            e += 1  # include terminator
        if around:
            while e < n and content[e] in (" ", "\t"):
                e += 1
        return (s, e)

    # paragraph (ip/ap): blank-line delimited
    if kind == "p":
        lines = content.split("\n")
        # find cursor's line index
        cum = 0
        line_idx = 0
        for idx, ln in enumerate(lines):
            if cum + len(ln) >= cursor:
                line_idx = idx
                break
            cum += len(ln) + 1
        else:
            line_idx = len(lines) - 1
        # paragraph = contiguous non-empty lines around cursor
        # if cursor is on blank line, span the blank-line block (vim parity)
        on_blank = lines[line_idx] == ""
        start_idx = line_idx
        while start_idx > 0 and (lines[start_idx - 1] == "") == on_blank:
            start_idx -= 1
        end_idx = line_idx
        while end_idx + 1 < len(lines) and (lines[end_idx + 1] == "") == on_blank:
            end_idx += 1
        # compute offsets
        s = sum(len(l) + 1 for l in lines[:start_idx])
        e = sum(len(l) + 1 for l in lines[:end_idx + 1])  # include trailing \n
        if not around:
            # inner: don't include the trailing \n on the last line if it's the only sep
            pass
        else:
            # around: include trailing blank lines
            j = end_idx + 1
            while j < len(lines) and lines[j] == "":
                j += 1
            e = sum(len(l) + 1 for l in lines[:j])
        return (s, min(e, n))

    # quoted strings: " ' `
    if kind in ('"', "'", "`"):
        q = kind
        # search on the cursor's line first
        bol = content.rfind("\n", 0, cursor) + 1
        eol_pos = content.find("\n", cursor)
        if eol_pos == -1:
            eol_pos = n
        line = content[bol:eol_pos]
        # find pair surrounding cursor within line
        rel_cur = cursor - bol
        # gather quote positions on the line
        positions = [i for i, ch in enumerate(line) if ch == q]
        if len(positions) < 2:
            raise _TextObjectError(f"no matching {q} pair on line")
        # pair them sequentially (1st-2nd, 3rd-4th, ...)
        pair = None
        for k in range(0, len(positions) - 1, 2):
            p1, p2 = positions[k], positions[k + 1]
            if p1 <= rel_cur <= p2:
                pair = (p1, p2)
                break
        if pair is None:
            # cursor outside any pair — use first pair after cursor, else first pair
            for k in range(0, len(positions) - 1, 2):
                if positions[k] >= rel_cur:
                    pair = (positions[k], positions[k + 1])
                    break
            if pair is None:
                pair = (positions[0], positions[1])
        p1, p2 = pair
        if around:
            return (bol + p1, bol + p2 + 1)
        return (bol + p1 + 1, bol + p2)

    # bracket pairs: ( [ { <
    if kind in ("(", "[", "{", "<"):
        opener = kind
        closer = {"(": ")", "[": "]", "{": "}", "<": ">"}[opener]
        # Find enclosing pair: scan backward for unmatched opener
        depth = 0
        s = -1
        k = cursor
        # If cursor sits on opener, count from there; else scan
        while k >= 0:
            ch = content[k]
            if ch == closer:
                depth += 1
            elif ch == opener:
                if depth == 0:
                    s = k
                    break
                depth -= 1
            k -= 1
        if s == -1:
            # Fallback: cursor not inside a pair; search forward for next opener
            fwd = content.find(opener, cursor)
            if fwd == -1:
                raise _TextObjectError(f"no opening {opener} found")
            s = fwd
        # forward match closer with nesting
        depth = 1
        e = -1
        j = s + 1
        while j < n:
            if content[j] == opener:
                depth += 1
            elif content[j] == closer:
                depth -= 1
                if depth == 0:
                    e = j
                    break
            j += 1
        if e == -1:
            raise _TextObjectError(f"no matching {closer}")
        if around:
            return (s, e + 1)
        return (s + 1, e)

    # tag (it/at): HTML/XML tags. <tag ...>content</tag>
    if kind == "t":
        import re as _re
        tag_re = _re.compile(r"<(/?)([A-Za-z][A-Za-z0-9_:-]*)[^<>]*>")
        # Scan whole file, build stack of opens; find enclosing pair for cursor.
        # An opener at pos_o with end open_end "encloses" cursor if its matching
        # closer's end >= cursor and pos_o <= cursor (or cursor < open_end → also include).
        stack = []  # list of (open_start, open_end, name)
        pairs = []  # resolved (open_start, open_end, close_start, close_end, name)
        for m in tag_re.finditer(content):
            is_close = m.group(1) == "/"
            name = m.group(2)
            if is_close:
                for k in range(len(stack) - 1, -1, -1):
                    if stack[k][2] == name:
                        os_, oe_, nm_ = stack.pop(k)
                        pairs.append((os_, oe_, m.start(), m.end(), nm_))
                        break
            else:
                # self-closing tags don't open
                if m.group(0).rstrip(">").endswith("/"):
                    continue
                stack.append((m.start(), m.end(), name))
        # find innermost pair enclosing cursor
        enclosing = None
        for p in pairs:
            os_, oe_, cs_, ce_, nm_ = p
            if os_ <= cursor < ce_:
                if enclosing is None or os_ > enclosing[0]:
                    enclosing = p
        if enclosing is None:
            raise _TextObjectError("not inside a tag")
        os_, oe_, cs_, ce_, nm_ = enclosing
        if around:
            return (os_, ce_)
        return (oe_, cs_)

    raise _TextObjectError(f"unknown text-object kind {kind!r}")


_VIM_DIFF_HUNK_CAP = 5


def _vim_render_diff(before: str, after: str) -> str:
    """Render up to 5 unified-diff hunks (-old +new ±2 ctx) of the edit.

    Capped at _VIM_DIFF_HUNK_CAP hunks; surplus collapsed into a footer.
    No-op edits produce an explicit '--- diff: no changes ---' marker so
    Kevin can trust an in-band confirmation that the buffer is unchanged.
    """
    if before == after:
        return "--- diff: no changes ---\n"
    b_lines = before.splitlines(keepends=True)
    a_lines = after.splitlines(keepends=True)
    raw = list(difflib.unified_diff(b_lines, a_lines, n=2, lineterm=""))
    if not raw:
        return "--- diff: no changes ---\n"
    # Strip file headers (--- /+++) emitted by unified_diff
    body = [ln for ln in raw if not ln.startswith("---") and not ln.startswith("+++")]
    # Group by @@ hunk headers
    hunks: list[list[str]] = []
    current: list[str] = []
    for ln in body:
        if ln.startswith("@@"):
            if current:
                hunks.append(current)
            # Rewrite header to '@@ line N @@' for clarity
            m = re.match(r"@@ -(\d+)", ln)
            new_line = m.group(1) if m else "?"
            current = [f"@@ line {new_line} @@"]
        else:
            current.append(ln.rstrip("\n"))
    if current:
        hunks.append(current)

    total = len(hunks)
    shown = hunks[:_VIM_DIFF_HUNK_CAP]
    extra = total - len(shown)
    out = [f"--- diff ({total} hunk{'s' if total != 1 else ''}) ---\n"]
    for h in shown:
        for ln in h:
            out.append(ln + "\n")
    if extra > 0:
        out.append(f"... and {extra} more hunk{'s' if extra != 1 else ''}\n")
    return "".join(out)


_LINT_TIMEOUT_DEFAULT = 5


def _lint_timeout() -> int:
    """Post-edit lint subprocess timeout, overridable per environment (#396).

    A slow runner (Windows antivirus scanning a freshly written temp file is
    the usual suspect) needs room without a code change.
    """
    try:
        val = int(os.environ.get("SUPERTOOL_LINT_TIMEOUT", ""))
    except (TypeError, ValueError):
        return _LINT_TIMEOUT_DEFAULT
    return val if val > 0 else _LINT_TIMEOUT_DEFAULT


def _vim_render_lint(path: str) -> str:
    """Post-edit syntax lint based on file extension.

    Returns "" when no lint applies (unknown ext or missing binary) — silence
    means clean, and only that.
    On success: '--- lint: <tool> ---\\n<output>\\n'.
    On timeout: '--- POST-EDIT LINT TIMED OUT — <tool> (<N>s) ---' (#396) —
    never "", which would read as a file that linted clean.
    On failure: '--- POST-EDIT LINT FAILED — <tool> ---\\n<output>\\n'.
    Never raises; never rolls back the edit.
    """
    ext = os.path.splitext(path)[1].lower()
    tool = ""
    cmd: list[str] = []
    parse_inline = False

    if ext == ".php":
        if not shutil.which("php"):
            return ""
        tool = "php -l"
        cmd = ["php", "-l", path]
    elif ext == ".json":
        tool = "json"
        parse_inline = True
    elif ext == ".xml":
        if not shutil.which("xmllint"):
            return ""
        tool = "xmllint"
        cmd = ["xmllint", "--noout", path]
    elif ext == ".py":
        if not shutil.which("python3"):
            return ""
        tool = "py_compile"
        cmd = ["python3", "-m", "py_compile", path]
    else:
        return ""

    if parse_inline:
        # JSON: try to parse, no subprocess
        try:
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)
            return f"--- lint: {tool} ---\nValid JSON\n"
        except (OSError, json.JSONDecodeError) as e:
            return f"--- POST-EDIT LINT FAILED — {tool} ---\n{e}\n"

    timeout = _lint_timeout()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return (
            f"--- POST-EDIT LINT TIMED OUT — {tool} ({timeout}s) ---\n"
            "lint did not run to completion; the file was NOT checked. "
            "Raise SUPERTOOL_LINT_TIMEOUT if this recurs.\n"
        )
    except (FileNotFoundError, OSError):
        return ""

    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode == 0:
        return f"--- lint: {tool} ---\n{output or 'OK'}\n"
    return f"--- POST-EDIT LINT FAILED — {tool} ---\n{output or '(no output)'}\n"


def _vim_resolve_ex_address(addr: str, cursor_line: int, total_lines: int) -> int:
    """Resolve a vim ex address to a line number.

    Supports:
      - `.` (cursor), `$` (last line), `N` (literal line number)
      - relative offsets: `+N`, `-N` (shortcut for `.+N`/`.-N`)
      - base + offset: `.+1`, `.-2`, `$-5`, `5+3`
    """
    addr = addr.strip()
    if not addr:
        raise ValueError("empty address")
    base = addr
    offset = 0
    # Detect a `+` or `-` that splits base from offset. Leading +/- is the
    # shorthand `.+N`/`.-N`; mid-string +/- splits an explicit base.
    if addr[0] in "+-":
        base = "."
        sign = addr[0]
        rest = addr[1:]
        if rest == "":
            offset = 1 if sign == "+" else -1
        else:
            try:
                offset = int(sign + rest)
            except ValueError:
                raise ValueError(f"bad offset {addr!r}")
    else:
        # Find the last +/- after position 0
        split_idx = -1
        for i in range(1, len(addr)):
            if addr[i] in "+-":
                split_idx = i
        if split_idx > 0:
            base = addr[:split_idx]
            try:
                offset = int(addr[split_idx:])
            except ValueError:
                raise ValueError(f"bad offset {addr[split_idx:]!r}")
    if base == ".":
        line = cursor_line
    elif base == "$":
        line = total_lines
    elif base.isdigit():
        line = int(base)
    else:
        raise ValueError(f"bad address {addr!r}")
    return line + offset


def _vim_literal_decode(pat: str) -> str:
    """Convert a regex-style pattern to the literal string the caller
    probably meant. Used by the no-match autocorrect on `/PAT` and `:s`.

    - Decode `\\xHH`, `\\uHHHH`, `\\n`, `\\t`, `\\r` to real chars (preserve
      their intended meaning before stripping).
    - Iteratively strip `\\X` → `X` for non-digit X so over-escaped
      `\\$this` → `\\$this` → `$this` flattens to literal.
    """
    out = pat
    # Strip leading `^` anchor (Kevin's literal `^` would be `\^`).
    if out.startswith("^"):
        out = out[1:]
    # Strip trailing `$` anchor when not preceded by `\` (escaped `\$` is
    # a literal dollar Kevin intends to match).
    if out.endswith("$") and not out.endswith("\\$"):
        out = out[:-1]
    out = re.sub(r"\\x([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), out)
    out = re.sub(r"\\u([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), out)
    out = out.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
    while True:
        nxt = re.sub(r"\\(\D)", r"\1", out)
        if nxt == out:
            return out
        out = nxt


def _vim_nearest_literal_hint(
    content: str,
    pat: str,
    max_lines: int = 3,
    original: Optional[str] = None,
) -> str:
    """When /PAT or :s misses, return a short hint with file lines that
    contain the longest literal chunk of the pattern. Helps the caller see
    what's actually in the file instead of guessing again.

    Returns "" when no useful hint can be produced (empty pattern, no
    literal substring, or no occurrence in file).
    """
    if not pat or not content:
        return ""
    # Split on regex metacharacters AND newlines to get literal chunks.
    chunks = [c for c in re.split(r"[\\.\^\$\*\+\?\(\)\[\]\{\}\|\n]+", pat) if len(c) >= 3]
    if not chunks:
        return ""
    # Try longest chunks first — most specific. Fall back to shorter if no
    # hits (the long chunk may not be in file at all).
    chunks.sort(key=len, reverse=True)
    lines = content.split("\n")
    def _scan(probe: str) -> List[tuple]:
        hits: List[tuple] = []
        for lno, line in enumerate(lines, 1):
            if probe in line:
                snippet = line.strip()
                if len(snippet) > 80:
                    snippet = snippet[:77] + "..."
                hits.append((lno, snippet))
                if len(hits) >= max_lines:
                    break
        return hits

    for probe in chunks:
        hits = _scan(probe)
        if hits:
            parts = [f"line {lno}: {snip!r}" for lno, snip in hits]
            label = "buffer near" if original is not None and content != original else "near"
            return f" ({label} {probe!r}: " + "; ".join(parts) + ")"
    # Prefix fallback: longest chunk has no hits. Try its leading prefix
    # at decreasing lengths to surface the closest line.
    longest = chunks[0]
    for cut in (len(longest) - 4, len(longest) // 2, 8, 5):
        if cut < 4 or cut >= len(longest):
            continue
        probe = longest[:cut]
        hits = _scan(probe)
        if hits:
            parts = [f"line {lno}: {snip!r}" for lno, snip in hits]
            label = "buffer near" if original is not None and content != original else "near"
            return f" ({label} {probe!r}: " + "; ".join(parts) + ")"
    return ""


def _verb_token_at(s: str, pos: int) -> tuple:
    """Identify the vim verb starting at position `pos` in string `s`.

    Returns (verb_end, enters_text_mode) where verb_end is the index just
    past the verb's fixed structure. enters_text_mode=True means the verb
    consumes greedy TEXT until ESC/EOS (insert verbs, ex, search,
    change-family).

    Shared core used by both the main tokenizer's _verb_token() (which
    operates on the `normalized` closure) and the macro tokenizer's
    _greedy_verb() (which operates on an arbitrary string).  Visual-mode
    V/v blocks are handled by the caller (_verb_token) and are not
    replicated here.
    """
    sn = len(s)
    if pos >= sn:
        return (pos, False)
    c = s[pos]
    # Insert verbs — greedy text
    if c in "iaAIoO":
        return (pos + 1, True)
    # Insert-mode-entry shortcuts: s/S/C; R = overwrite mode
    if c in "sSCR":
        return (pos + 1, True)
    # Search / ex — greedy
    if c in "/?:":
        return (pos + 1, True)
    # vim alias `%s/…` — 2-char greedy
    if c == "%" and pos + 1 < sn and s[pos + 1] == "s":
        return (pos + 2, True)
    # Change-family greedy: cc cw c$ c0
    if c == "c" and pos + 1 < sn and s[pos + 1] in ("c", "w", "$", "0"):
        return (pos + 2, True)
    # ciw / ci<delim> — three-char form
    if c == "c" and pos + 1 < sn and s[pos + 1] == "i" and pos + 2 < sn and s[pos + 2] in ('w', '"', "'", "(", "[", "{"):
        return (pos + 3, True)
    # Full text-object family: c/d/y + i/a + kind
    _TO = set('wWsp"\'`()[]{}<>bBt')
    if c in "cdy" and pos + 2 < sn and s[pos + 1] in "ia" and s[pos + 2] in _TO:
        return (pos + 3, c == "c")
    # Two-char op (g~, gu, gU) + i/a + kind
    if c == "g" and pos + 3 < sn and s[pos + 1] in ("~", "u", "U") \
            and s[pos + 2] in "ia" and s[pos + 3] in _TO:
        return (pos + 4, False)
    # c + new motions (text mode for c)
    if c == "c" and pos + 1 < sn and s[pos + 1] in "{})(%+-_WBES;,^":
        return (pos + 2, True)
    # cf/cF/ct/cT — three-char greedy
    if c == "c" and pos + 1 < sn and s[pos + 1] in "fFtT":
        return (min(pos + 3, sn), True)
    # d/y + char-find: df<c>, dt<c>, etc.
    if c in "dy" and pos + 1 < sn and s[pos + 1] in "fFtT":
        return (min(pos + 3, sn), False)
    # d/y + greedy search operator-motion
    if c in "dy" and pos + 1 < sn and s[pos + 1] in "/?":
        return (pos + 2, True)
    # r<c> — single-char arg, no text mode
    if c == "r":
        return (min(pos + 2, sn), False)
    # Char-find: f/F/t/T<c>
    if c in "fFtT":
        return (min(pos + 2, sn), False)
    # Two-char no-arg verbs
    if pos + 1 < sn:
        two = s[pos: pos + 2]
        _TWO_NOARG = {
            "dd", "dw", "d$", "d0", "dG", "d^", "dh", "dj", "dk", "dl",
            "d{", "d}", "d(", "d)", "d%", "d+", "d-", "d_", "dW", "dB", "dE", "d;", "d,",
            "yy", "yw", "y$", "y0", "yG", "y^", "yh", "yj", "yk", "yl",
            "y{", "y}", "y(", "y)", "y%", "y+", "y-", "y_", "yW", "yB", "yE", "y;", "y,",
            "gg", "ge", "gE", "g_", "gJ",
            "g~~", "guu", "gUU",
            ">>", "<<", "==",
        }
        if two in _TWO_NOARG:
            return (pos + 2, False)
    # d/y/c + gg/ge/gE/g_ (3-char operator-motion)
    if c in "dyc" and pos + 2 < sn and s[pos + 1] == "g" and s[pos + 2] in "geE_g":
        return (pos + 3, c == "c")
    # Linewise case verbs: g~~/guu/gUU (3-char)
    if c == "g" and pos + 2 < sn and s[pos + 1] in "~uU" and s[pos + 2] == s[pos + 1]:
        return (pos + 3, False)
    # g~ / gu / gU + motion (3-char)
    if c == "g" and pos + 2 < sn and s[pos + 1] in "~uU":
        return (pos + 3, False)
    # gg / ge / gE / g_ / gJ (2-char, not already in _TWO_NOARG path above)
    if c == "g" and pos + 1 < sn and s[pos + 1] in ("g", "e", "E", "_", "i", "J"):
        greedy = s[pos + 1] == "i"
        return (pos + 2, greedy)
    # Tilde toggle-case
    if c == "~":
        return (pos + 1, False)
    # Ctrl-A increment / Ctrl-X decrement
    if c in ("\x01", "\x18"):
        return (pos + 1, False)
    # m{a-zA-Z} — set mark
    if c == "m" and pos + 1 < sn and (("a" <= s[pos + 1] <= "z") or ("A" <= s[pos + 1] <= "Z")):
        return (pos + 2, False)
    # `{a-zA-Z} or `` — jump to mark exact / back to last jump
    if c == "`" and pos + 1 < sn and (
        ("a" <= s[pos + 1] <= "z") or ("A" <= s[pos + 1] <= "Z") or s[pos + 1] == "`"
    ):
        return (pos + 2, False)
    # '{a-zA-Z} or '' — jump to mark line / back to last jump
    if c == "'" and pos + 1 < sn and (
        ("a" <= s[pos + 1] <= "z") or ("A" <= s[pos + 1] <= "Z") or s[pos + 1] == "'"
    ):
        return (pos + 2, False)
    # >> << == — indent/dedent/re-indent (2-char no-arg)
    if c in "><=" and pos + 1 < sn and s[pos + 1] == c:
        return (pos + 2, False)
    # > / < / = + [count] + motion
    if c in "><=" and pos + 1 < sn:
        mc = pos + 1
        while mc < sn and s[mc].isdigit():
            mc += 1
        if mc < sn:
            nxt = s[mc]
            _TO2 = set('wWsp"\'`()[]{}<>bBt')
            if nxt in "ia" and mc + 1 < sn and s[mc + 1] in _TO2:
                return (mc + 2, False)
            if nxt == "g" and mc + 1 < sn and s[mc + 1] == "g":
                return (mc + 2, False)
            if nxt in ("j", "k", "h", "l", "G", "{", "}", "(", ")",
                       "%", "+", "-", "_", "w", "b", "e", "W", "B", "E",
                       "$", "0", "^"):
                return (mc + 1, False)
    # undo / redo
    if c == "u" or c == "\x12":
        return (pos + 1, False)
    # Single-char no-arg fallback
    return (pos + 1, False)


def op_vim(path: str, script: str) -> str:
    """Public wrapper for op_vim. Vim ops are atomic — file only gets
    written if every action succeeds. On ERROR we tell the caller the
    file is untouched, so they don't panic-rewrite from scratch.
    """
    out = _op_vim_impl(path, script)
    if out.startswith("ERROR"):
        suffix = " (file unchanged — vim ops are atomic, no actions applied)\n"
        # Ensure the suffix sits on its own line right before EOF.
        if out.endswith("\n"):
            out = out[:-1] + suffix
        else:
            out = out + suffix
    return out


def _op_vim_impl(path: str, script: str) -> str:
    """vim-flavored cursor-based multi-action edit op.

    Actions split by newline OR semicolon. Each action: optional count
    prefix + verb + optional arg. Lifted from vim for token economy in
    LLM-generated edits.

    Cursor persistence: the cursor offset is saved to
    ~/.cache/supertool/vim-cursor/<sha1(abspath)> after each successful op
    and restored on the next call against the same path. Set
    SUPERTOOL_VIM_NO_PERSIST=1 to disable. Start a script with `gg` to
    force-reset to BOF.

    Cursor / search:
        gg          — top of file (BOF)
        G           — end of file (EOF)
        nG          — goto line n (1-indexed)
        0           — BOL
        ^           — first non-blank of line
        $           — EOL
        g_          — last non-blank of line
        +           — first non-blank of next line
        -           — first non-blank of prev line
        _           — first non-blank of current line (N_ goes down N-1)
        /PAT        — find PAT forward (regex; literal fallback on re.error)
        ?PAT        — find PAT backward (regex; literal fallback on re.error)
        nh          — n chars left (default 1)
        nl          — n chars right (default 1)
        nj          — n lines down
        nk          — n lines up
        w b e       — word motions (alnum+_)
        W B E       — WORD motions (whitespace-delimited)
        ge gE       — back to word/WORD end
        { }         — paragraph (blank-line) back/forward
        ( )         — sentence back/forward
        %           — match bracket (cursor on (){}[])
        f F t T     — find/till char on line (forward/back)
        ; ,         — repeat last f/F/t/T (, reverses)

    Inserts (TEXT runs to end of action; \\n / \\t decoded):
        iTEXT       — insert before cursor
        aTEXT       — append after cursor
        ITEXT       — insert at BOL of current line
        ATEXT       — append at EOL of current line
        oTEXT       — open new line below, insert
        OTEXT       — open new line above, insert
        With count, TEXT is inserted N times (e.g., 5i-).

    Deletes:
        x   / nx    — delete n chars at cursor (default 1)
        dd  / ndd   — delete n lines (default 1)
        D           — delete from cursor to EOL

    Change (replace + insert in one verb; TEXT runs to end of action,
    `\\n`/`\\t`/`\\;` decoded):
        ciwTEXT     — change inner word (word at cursor → TEXT)
        cwTEXT      — change from cursor to end of word
        ccTEXT      — change current line content (keeps trailing \\n)
        nccTEXT     — change next n lines (single TEXT replaces all)
        ci"TEXT     — change inside "" (replace content between quotes)
        ci'TEXT     — change inside ''
        ci(TEXT     — change inside (...)  [matches nested ()]
        ci[TEXT     — change inside [...]
        ci{TEXT     — change inside {...}

    No visual mode (V / v):
        Use line-range ex instead — `Ndd`, `:N,Md`, `Ncc`, `:%s/PAT/REPL/`.
        For block inserts use `o`/`O` (single-line) or `:r FILE` (multi-line).

    Join:
        J / nJ      — join next n lines with cursor's line (single space sep)

    Replace:
        rc          — replace single char at cursor with c

    Escapes inside TEXT:
        \\n \\t \\r  → newline / tab / CR
        \\;          → literal `;` (otherwise `;` ends the action)
        \\\\         → literal backslash

    Examples:
        # Annotate function signature
        vim:::foo.py:::/def foo;A  # entry point

        # Insert a multi-line block before a marker
        vim:::skill.md:::/## Process;O## Task list;o1. Foo;o2. Bar

        # Rename a variable
        vim:::foo.py:::/old_name;ciwnew_name

        # Replace a string literal
        vim:::foo.py:::/setLabel(;l;ci"New Label"

        # Replace a function arg list
        vim:::foo.py:::/foo(;ci(x, y, z

        # Replace a whole line
        vim:::foo.py:::/return false;ccreturn true;

        # Insert code that contains a semicolon
        vim:::foo.py:::Areturn $x\\;

        # Join 2 lines
        vim:::log.txt:::5G;J

        # Delete 3 lines starting at line 10
        vim:::log.txt:::10G;3dd
    """
    if not path:
        return "ERROR: empty path\n"
    if not os.path.isfile(path):
        return f"ERROR: file not found: {path}\n"
    if not script.strip():
        return "ERROR: empty script\n"

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        return f"ERROR: failed to read {path}: {e}\n"

    _before_content = content

    # Stateful real-vim tokenizer. Matches LLM/vim-macro mental model:
    # - Normal mode: chars are verbs (with optional count prefix). After a
    #   verb consumes its fixed arg, the next char starts the next verb.
    # - "Greedy" verbs (i/a/A/I/o/O insert; /? search; : ex) consume their
    #   arg until `\e` (ESC, U+001B) or end-of-script. `\e` returns to
    #   normal mode without producing a new action.
    # - No separator chars. `;`, `{`, `}`, `␞`, newlines, etc. are just
    #   literal data — never special.
    # - `\x1b` (real ESC) and the literal two-char `\e` escape are both
    #   recognized as mode-exit, matching how vim macros are written.
    ESC = "\x1b"
    # Tokenize → list of action strings (each already in count+verb+arg
    # shape, ready for _parse).
    raw_actions: List[str] = []
    # Pre-normalize: turn literal `\e` (two chars: backslash + e) and the
    # ASCII RS `\x1e` (legacy from the `␞` era — Kevin still types
    # `$'\x1e'`) and `␞` itself into actual ESC. Real `\x1b` passes
    # through.
    normalized = script.replace("\\e", ESC).replace("\x1e", ESC).replace("␞", ESC)
    # Decode Ctrl-A / Ctrl-X escapes (`\C-a` / `\C-x`) to their real bytes so
    # the tokenizer sees single-char verbs. Real \x01 / \x18 pass through.
    normalized = normalized.replace("\\C-a", "\x01").replace("\\C-x", "\x18")
    # Script-level autocorrects (applied before tokenizing):
    # - `<digits>gg` → `<digits>G`: vim's `gg` ignores count.
    # - `d5d` → `5dd`, `c5w` → `5cw`, etc.: count-in-middle typo.
    normalized = re.sub(r"(?<![a-zA-Z0-9])([1-9]\d*)gg(?![a-zA-Z])", r"\1G", normalized)
    _COUNTABLE_PAIRS = {"dd", "dw", "d$", "d0", "cc", "cw", "c$", "c0", "yy", "yw", "y$"}

    def _count_middle_sub(m):
        first, digits, third = m.group(1), m.group(2), m.group(3)
        if first + third in _COUNTABLE_PAIRS:
            return f"{digits}{first}{third}"
        return m.group(0)

    normalized = re.sub(
        r"(?<![a-zA-Z0-9])([dcy])([1-9]\d*)([dwycs0$])",
        _count_middle_sub,
        normalized,
    )
    # Kevin autocorrect: bare `g/PAT/...`, `v/PAT/...`, `%g/PAT/...`, `%v/PAT/...`
    # at the start of an action chain (or after ESC) → prepend `:` so they
    # parse as ex commands. Real vim requires `:` prefix; Kevin's muscle memory
    # drops it. Bare `g`/`v` followed by `/` is never useful as a normal-mode
    # sequence (`g` waits for second char, `v` enters visual then `/` searches).
    # Match at start of string or after ESC/newline.
    normalized = re.sub(
        r"(^|[" + ESC + r"\n])([%]?[gv]/)",
        lambda m: m.group(1) + ":" + m.group(2),
        normalized,
    )
    # Strip redundant `%` from `:%g/.../` and `:%v/.../` — `:g`/`:v` already
    # operate on whole buffer by default (no range needed). Real vim accepts
    # `:%g` but our parser doesn't; collapse to `:g`.
    normalized = re.sub(r":\%([gv]/)", r":\1", normalized)
    # Ex append: `:Na\nBODY\n.` (real vim multi-line ex-append after line N).
    # Convert to `<N>GoBODY<ESC>` — goto line N, open below, insert body.
    # The `o` executor handles auto-indent + body line splits.
    normalized = re.sub(
        r":(\d+|\$|\.)a\n(.*?)\n\.\n?",
        lambda m: (
            ("G" if m.group(1) in ("$", ".") else m.group(1) + "G")
            + "o" + m.group(2) + ESC
        ),
        normalized,
        flags=re.DOTALL,
    )
    # Ex insert: `:Ni\nBODY\n.` — insert before line N. Use `O` (open above).
    normalized = re.sub(
        r":(\d+|\$|\.)i\n(.*?)\n\.\n?",
        lambda m: (
            ("G" if m.group(1) in ("$", ".") else m.group(1) + "G")
            + "O" + m.group(2) + ESC
        ),
        normalized,
        flags=re.DOTALL,
    )
    # Kevin abandoned-range autocorrect: `64,` (digits + comma + EOS/ESC/EOL)
    # — `,` is the find-repeat verb, but with no previous f/F/t/T it errors
    # confusingly. Kevin meant to type a range and forgot the command.
    # Strip the abandoned digits+comma so the rest of the script keeps running.
    normalized = re.sub(
        r"(?<![a-zA-Z0-9])(\d+),(?=[" + ESC + r"\n]|$)",
        "",
        normalized,
    )
    i = 0
    n = len(normalized)

    def _verb_token(start: int) -> tuple:
        """Identify the verb starting at `start` in `normalized`.

        Handles V/v visual-mode blocks (which need access to the outer
        `normalized` closure) then delegates to the module-level
        _verb_token_at() for everything else.
        """
        if start >= n:
            return (start, False)
        c = normalized[start]
        # V (visual-line) — consume V[count][motion]<op|ex> as a single
        # verb so the post-tokenize V-alias rewriter can collapse it
        # into a line-op or ex range. Greedy when op is `c` (change) or
        # when followed by an ex command (`:`).
        if c == "V":
            j = start + 1
            while j < n and normalized[j].isdigit():
                j += 1
            # Optional motion: j/k/G/gg
            if j < n and normalized[j] in "jkG":
                j += 1
            elif j + 1 < n and normalized[j] == "g" and normalized[j + 1] == "g":
                j += 2
            # Ex command after motion: V<motion>:<rest> — greedy until ESC
            if j < n and normalized[j] == ":":
                return (j + 1, True)
            # Operator: d/y/c (single or doubled cc/dd/yy)
            if j < n and normalized[j] in "dyc":
                op_char = normalized[j]
                j += 1
                if j < n and normalized[j] == op_char:
                    j += 1
                return (j, op_char == "c")
            # V alone — fall through to single-char (unknown-verb hint)
            return (start + 1, False)
        # v (char-visual) — consume v[count][motion]<op> as a single verb
        # so the post-tokenize v-alias rewriter can collapse it into the
        # standard operator-motion form <op><motion>.  Greedy when op is
        # `c` (change).  Motions supported: simple one-char motions,
        # gg, text-objects i<X>/a<X>, char-finds f/F/t/T<c>, and search
        # motions /<pat>//<pat>.
        if c == "v":
            j = start + 1
            while j < n and normalized[j].isdigit():
                j += 1
            if j >= n:
                return (start + 1, False)
            # Text-object: v[count]i<X><op> or v[count]a<X><op>
            _TO_KINDS_V = set('wWsp"\'`()[]{}<>bBt')
            if normalized[j] in "ia" and j + 1 < n and normalized[j + 1] in _TO_KINDS_V:
                motion_end = j + 2
                if motion_end < n and normalized[motion_end] in "dyc":
                    op_char = normalized[motion_end]
                    return (motion_end + 1, op_char == "c")
                return (start + 1, False)
            # gg motion
            if j + 1 < n and normalized[j] == "g" and normalized[j + 1] == "g":
                motion_end = j + 2
                if motion_end < n and normalized[motion_end] in "dyc":
                    op_char = normalized[motion_end]
                    return (motion_end + 1, op_char == "c")
                return (start + 1, False)
            # Char-find: f/F/t/T<c> motion (consumes 2 chars)
            if normalized[j] in "fFtT" and j + 1 < n:
                motion_end = j + 2
                if motion_end < n and normalized[motion_end] in "dyc":
                    op_char = normalized[motion_end]
                    return (motion_end + 1, op_char == "c")
                return (start + 1, False)
            # Simple one-char motions
            _V_SIMPLE_MOTIONS = set("wbeWBEjkhl$0^G{}()%;,")
            if normalized[j] in _V_SIMPLE_MOTIONS:
                motion_end = j + 1
                if motion_end < n and normalized[motion_end] in "dyc":
                    op_char = normalized[motion_end]
                    return (motion_end + 1, op_char == "c")
                return (start + 1, False)
            # v alone (or unrecognized motion) — fall through
            return (start + 1, False)
        return _verb_token_at(normalized, start)

    # macros_pending: register → raw body string (captured during tokenizing,
    # before the action loop runs). Populated by q<reg>...q recording blocks.
    macros_pending: dict = {}

    def _greedy_verb(s: str, pos: int) -> tuple:
        """Delegates to module-level _verb_token_at().

        Used by macro recording/replay inline tokenizers to classify verbs
        in an arbitrary string (not the outer `normalized` closure).
        """
        return _verb_token_at(s, pos)

    while i < n:
        # Skip whitespace AND stray ESC between actions. (ESC is a mode
        # exit; in normal mode it's a no-op. Vim macros use it for
        # readability and to "reset" defensively.)
        while i < n and normalized[i] in (" \t\n\r" + ESC):
            i += 1
        if i >= n:
            break
        action_start = i
        # --- macro recording: q<reg>...q ---
        # `q<a-z>` starts recording into register <reg>. Everything up to
        # the next bare `q` is the macro body. Real vim executes the body
        # as you type it — we honour that: tokenize and emit the body's
        # actions so they run now, then emit the sentinel so the macro is
        # stored for future @<reg> replay.
        if normalized[i] == "q" and i + 1 < n and "a" <= normalized[i + 1] <= "z":
            reg = normalized[i + 1]
            body_start = i + 2
            # Walk the body with the greedy tokenizer to find the *real*
            # closing `q` in NORMAL mode, not inside insert/search/ex text.
            # A plain .find("q") closes on the first `q` anywhere (wrong:
            # `qaiquery\eq` would close on the `q` inside "query").
            _scan = body_start
            close_q = -1
            while _scan < n:
                while _scan < n and normalized[_scan] in (" \t\n\r" + ESC):
                    _scan += 1
                if _scan >= n:
                    break
                # Bare `q` in normal mode = close of recording
                if normalized[_scan] == "q":
                    close_q = _scan
                    break
                # Skip count prefix
                _sv = _scan
                if normalized[_sv].isdigit() and normalized[_sv] != "0":
                    while _sv < n and normalized[_sv].isdigit():
                        _sv += 1
                if _sv >= n:
                    _scan = n
                    break
                # @<reg> inside body: 2-char verb, no text arg
                if normalized[_sv] == "@" and _sv + 1 < n and (
                    ("a" <= normalized[_sv + 1] <= "z") or normalized[_sv + 1] == "@"
                ):
                    _scan = _sv + 2
                    continue
                # Determine if verb enters text (greedy until ESC) or not
                _vend, _vgreedy = _greedy_verb(normalized, _sv)
                if _vgreedy:
                    _esc_at = normalized.find(ESC, _vend)
                    if _esc_at == -1:
                        _scan = n
                    else:
                        _scan = _esc_at + 1
                else:
                    _scan = _vend
            if close_q == -1:
                body = normalized[body_start:]
                i = n
            else:
                body = normalized[body_start:close_q]
                i = close_q + 1
            macros_pending[reg] = body
            # Inline-tokenize body so the actions execute during recording.
            _rec_norm = body.replace("\\e", ESC).replace("\x1e", ESC).replace("␞", ESC)
            _rec_norm = _rec_norm.replace("\\C-a", "\x01").replace("\\C-x", "\x18")
            _rec_actions: List[str] = []
            _ri = 0
            _rn = len(_rec_norm)
            while _ri < _rn:
                while _ri < _rn and _rec_norm[_ri] in (" \t\n\r" + ESC):
                    _ri += 1
                if _ri >= _rn:
                    break
                _rstart = _ri
                _rverb_pos = _ri
                if _rec_norm[_ri].isdigit() and _rec_norm[_ri] != "0":
                    while _rverb_pos < _rn and _rec_norm[_rverb_pos].isdigit():
                        _rverb_pos += 1
                if _rverb_pos >= _rn:
                    _rec_actions.append(_rec_norm[_rstart:])
                    break
                if _rec_norm[_rverb_pos] == "@" and _rverb_pos + 1 < _rn and (
                    ("a" <= _rec_norm[_rverb_pos + 1] <= "z") or _rec_norm[_rverb_pos + 1] == "@"
                ):
                    _rec_actions.append(_rec_norm[_rstart: _rverb_pos + 2])
                    _ri = _rverb_pos + 2
                    continue
                _rverb_end, _renters_text = _greedy_verb(_rec_norm, _rverb_pos)
                if _renters_text:
                    _resc = _rec_norm.find(ESC, _rverb_end)
                    if _resc == -1:
                        _rec_actions.append(_rec_norm[_rstart:])
                        _ri = _rn
                    else:
                        _rec_actions.append(_rec_norm[_rstart:_resc])
                        _ri = _resc + 1
                else:
                    _rec_actions.append(_rec_norm[_rstart:_rverb_end])
                    _ri = _rverb_end
            _rec_actions = [a for a in _rec_actions if a]
            raw_actions.extend(_rec_actions)
            raw_actions.append(f"__macro_def_{reg}")
            continue
        # Parse count: leading digits, but not if `0` alone (BOL verb).
        verb_pos = i
        if normalized[i].isdigit() and normalized[i] != "0":
            while verb_pos < n and normalized[verb_pos].isdigit():
                verb_pos += 1
        if verb_pos >= n:
            # trailing digits with no verb — emit as-is, _parse will error
            raw_actions.append(normalized[action_start:])
            break
        # --- @<reg> / @@ replay: tokenize as count + 2-char verb ---
        # Checked after digit-parse so `5@a` emits `5@a` as one token.
        if normalized[verb_pos] == "@" and verb_pos + 1 < n and (
            ("a" <= normalized[verb_pos + 1] <= "z") or normalized[verb_pos + 1] == "@"
        ):
            raw_actions.append(normalized[action_start: verb_pos + 2])
            i = verb_pos + 2
            continue
        # Identify verb shape and consumption mode.
        verb_end, enters_text = _verb_token(verb_pos)
        if enters_text:
            # greedy: consume verb (+ fixed prefix) + TEXT until ESC or EOS
            esc_at = normalized.find(ESC, verb_end)
            if esc_at == -1:
                raw_actions.append(normalized[action_start:])
                i = n
            else:
                raw_actions.append(normalized[action_start:esc_at])
                i = esc_at + 1
        else:
            raw_actions.append(normalized[action_start:verb_end])
            i = verb_end
    # Drop empty actions (from stray whitespace/ESC runs). Don't strip
    # the actions themselves — trailing whitespace inside an insert TEXT
    # (e.g. `ihello ` ending with a space) is significant.
    raw_actions = [a for a in raw_actions if a]
    if not raw_actions:
        return "ERROR: no actions in script\n"

    def _line_start(text: str, off: int) -> int:
        nl = text.rfind("\n", 0, off)
        return nl + 1

    def _line_end(text: str, off: int) -> int:
        nl = text.find("\n", off)
        return nl if nl != -1 else len(text)

    def _goto_line(text: str, line: int) -> int:
        lines = text.split("\n")
        if line < 1 or line > len(lines):
            raise ValueError(f"line {line} out of range (file has {len(lines)} lines)")
        return sum(len(l) + 1 for l in lines[: line - 1])

    def _offset_to_line_col(text: str, off: int) -> tuple:
        pre = text[:off]
        line = pre.count("\n") + 1
        last_nl = pre.rfind("\n")
        col = off - last_nl
        return line, col

    def _parse(action: str) -> tuple:
        """Return (count:int, verb:str, arg:str). count defaults to 1."""
        if not action:
            return (1, "", "")
        # Kevin typo autocorrect: `:%%d` / `:%%s/...` (double %) → `:%d` / `:%s/...`.
        # Real vim treats `:%%` as range error. Kevin reflex: stutters `%`.
        if action.startswith(":%%"):
            # Collapse run of % after `:` to a single %.
            k = 1
            while k < len(action) and action[k] == "%":
                k += 1
            action = ":%" + action[k:]
        # Kevin autocorrect: bare `g/PAT/d`, `g/PAT/...`, `%g/PAT/d`, `v/PAT/d`,
        # `%v/PAT/d` → prepend `:` so they parse as ex commands. Real vim
        # requires `:` prefix; Kevin's muscle memory drops it. Bare `g`/`v`
        # standalone are useless (g+motion = no-op), so no false-positive risk.
        if len(action) >= 3 and action[0] == "g" and action[1] == "/":
            action = ":" + action
        elif len(action) >= 3 and action[0] == "v" and action[1] == "/":
            action = ":" + action
        elif len(action) >= 4 and action[0] == "%" and action[1] in ("g", "v") and action[2] == "/":
            action = ":" + action
        i = 0
        # count: leading digits, but `0` alone is the BOL verb
        if action[0].isdigit() and action[0] != "0":
            while i < len(action) and action[i].isdigit():
                i += 1
        count = int(action[:i]) if i > 0 else 1
        rest = action[i:]
        if not rest:
            return (count, "", "")
        # three-char verbs first: ciw, ci<delim>
        if len(rest) >= 3 and rest[:3] == "ciw":
            return (count, "ciw", rest[3:])
        if len(rest) >= 3 and rest[:2] == "ci" and rest[2] in ('"', "'", "(", "[", "{"):
            return (count, "ci" + rest[2], rest[3:])
        # Full text-object family: <op>i<X> / <op>a<X>
        # ops single-char: c d y. ops two-char: g~ gu gU.
        # X kinds: w W s p " ' ` ( ) [ ] { } < > b B t
        _to_kinds = set('wWsp"\'`()[]{}<>bBt')
        if (
            len(rest) >= 3
            and rest[0] in ("c", "d", "y")
            and rest[1] in ("i", "a")
            and rest[2] in _to_kinds
        ):
            return (count, rest[:3], rest[3:])
        if (
            len(rest) >= 4
            and rest[0] == "g"
            and rest[1] in ("~", "u", "U")
            and rest[2] in ("i", "a")
            and rest[3] in _to_kinds
        ):
            return (count, rest[:4], rest[4:])
        # vim :%!cmd — pipe whole buffer through shell command
        if len(rest) >= 3 and rest[:3] == ":%!":
            return (count, ":!", "\x1d%\x1d" + rest[3:])
        # vim alias: :%s/PAT/REPL/flags maps to :s (whole buffer)
        if len(rest) >= 3 and rest[:3] == ":%s":
            return (count, ":s", rest[3:])
        # vim alias without leading colon: %s/PAT/REPL/flags maps to :s
        if len(rest) >= 2 and rest[:2] == "%s":
            return (count, ":s", rest[2:])
        # vim line-range substitute/delete: :Ns/..., :N,Ms/..., :.s/..., :$s/...
        # and :Nd, :N,Md, :.d, :$d, :.,$d, :2,$d, :.,4d, etc.
        # Range = optional addr (N | . | $) + optional `,addr`. Encoded into
        # arg with a `\x1d` (group separator) sentinel: arg becomes
        # f"\x1d{range_spec}\x1d/PAT/REPL/flags" which the :s handler decodes.
        if len(rest) >= 2 and rest[0] == ":" and rest[1] in "0123456789.$+-":
            j = 1
            # first address — allow digits, `.`, `$`, and `+`/`-` for offsets
            # like `.+1`, `$-2`, `+1` (shortcut for `.+1`).
            while j < len(rest) and (rest[j].isdigit() or rest[j] in ".$+-"):
                j += 1
            # optional `,addr2`
            if j < len(rest) and rest[j] == ",":
                j += 1
                # `,/PAT/` — pattern address (real vim: `:.,/end/d`).
                # Consume `/`, then chars up to next unescaped `/`.
                if j < len(rest) and rest[j] == "/":
                    j += 1
                    while j < len(rest):
                        if rest[j] == "\\" and j + 1 < len(rest) and rest[j + 1] == "/":
                            j += 2
                            continue
                        if rest[j] == "/":
                            j += 1
                            break
                        j += 1
                else:
                    while j < len(rest) and (rest[j].isdigit() or rest[j] in ".$+-"):
                        j += 1
            # Multi-char ex verbs MUST be checked before single-char s/d/m/t
            # so that e.g. `:2,4sort` doesn't get parsed as `:2,4s` with body
            # `ort`. Order matters: longest prefix first.
            # :N,Msort[!|u|n], :Nsort, etc.
            if rest[j:j + 4] == "sort":
                range_spec = rest[1:j]
                body = rest[j + 4:]
                return (count, ":sort", f"\x1d{range_spec}\x1d{body}")
            if rest[j:j + 7] == "reverse":
                range_spec = rest[1:j]
                body = rest[j + 7:]
                return (count, ":reverse", f"\x1d{range_spec}\x1d{body}")
            # :N,Mm K  or  :N,Mmove K
            if rest[j:j + 4] == "move":
                range_spec = rest[1:j]
                body = rest[j + 4:]
                return (count, ":move", f"\x1d{range_spec}\x1d{body}")
            if rest[j:j + 1] == "m" and (j + 1 >= len(rest) or not rest[j + 1].isalpha()):
                range_spec = rest[1:j]
                body = rest[j + 1:]
                return (count, ":move", f"\x1d{range_spec}\x1d{body}")
            # :N,Mcopy K  or  :Nt K
            if rest[j:j + 4] == "copy":
                range_spec = rest[1:j]
                body = rest[j + 4:]
                return (count, ":copy", f"\x1d{range_spec}\x1d{body}")
            if rest[j:j + 1] == "t" and (j + 1 >= len(rest) or not rest[j + 1].isalpha()):
                range_spec = rest[1:j]
                body = rest[j + 1:]
                return (count, ":copy", f"\x1d{range_spec}\x1d{body}")
            # :N,Mnorm CMDS  (greedy body)
            if rest[j:j + 4] == "norm":
                range_spec = rest[1:j]
                body = rest[j + 4:]
                return (count, ":norm", f"\x1d{range_spec}\x1d{body}")
            # :N!cmd / :N,M!cmd — filter range through shell command
            if j < len(rest) and rest[j] == "!":
                range_spec = rest[1:j]
                cmd = rest[j + 1:]
                return (count, ":!", f"\x1d{range_spec}\x1d{cmd}")
            # Single-char ex verbs LAST (so longer prefixes win first).
            if j < len(rest) and rest[j] == "s":
                range_spec = rest[1:j]
                body = rest[j + 1:]
                return (count, ":s", f"\x1d{range_spec}\x1d{body}")
            if j < len(rest) and rest[j] == "d":
                range_spec = rest[1:j]
                trailing = rest[j + 1:]
                return (count, ":d", f"\x1d{range_spec}\x1d{trailing}")
            # :Nr FILE — read FILE after line N. Encode line via sentinel so
            # the :r handler can position the insertion.
            if j < len(rest) and rest[j] == "r":
                range_spec = rest[1:j]
                body = rest[j + 1:]
                return (count, ":r", f"\x1d{range_spec}\x1d{body}")
            # Bare `:N` / `:$` / `:.` (no command after range) — line goto.
            # Real vim: `:N\n` jumps to line N. Kevin types `:110\e` reflexively
            # instead of `110G`. Treat as goto so chained ops keep flowing.
            # Single address only (`:N,M` with no command is invalid in vim too).
            if j == len(rest) and "," not in rest[1:j]:
                spec = rest[1:j]
                if spec.isdigit() or spec in ("$", "."):
                    return (count, ":goto", spec)
        # vim :%d — delete whole buffer (alias for :1,$d)
        if len(rest) >= 3 and rest[:3] == ":%d":
            return (count, ":d", "\x1d%\x1d" + rest[3:])
        # vim :%sort, :%reverse, :%norm (whole buffer)
        if len(rest) >= 6 and rest[:6] == ":%sort":
            return (count, ":sort", "\x1d%\x1d" + rest[6:])
        if len(rest) >= 9 and rest[:9] == ":%reverse":
            return (count, ":reverse", "\x1d%\x1d" + rest[9:])
        if len(rest) >= 6 and rest[:6] == ":%norm":
            return (count, ":norm", "\x1d%\x1d" + rest[6:])
        # Bare ex commands (no range) — default to whole-buffer where applicable.
        # :sort[!|u|n] — default range = %
        if len(rest) >= 5 and rest[:5] == ":sort":
            return (count, ":sort", "\x1d%\x1d" + rest[5:])
        if len(rest) >= 8 and rest[:8] == ":reverse":
            return (count, ":reverse", "\x1d%\x1d" + rest[8:])
        # :retab [N] — whole-buffer; arg is the tab width (optional)
        if len(rest) >= 6 and rest[:6] == ":retab":
            return (count, ":retab", rest[6:])
        # :!cmd — run shell command, insert stdout at cursor (no range = insert mode)
        if len(rest) >= 2 and rest[:2] == ":!" and len(rest) > 2:
            return (count, ":!", "\x1d\x1d" + rest[2:])
        # :norm CMDS — default range = current line (.)
        if len(rest) >= 5 and rest[:5] == ":norm":
            return (count, ":norm", "\x1d.\x1d" + rest[5:])
        # :move K / :copy K / :t K  — no range (defaults to current line)
        if len(rest) >= 5 and rest[:5] == ":move":
            return (count, ":move", "\x1d.\x1d" + rest[5:])
        if len(rest) >= 5 and rest[:5] == ":copy":
            return (count, ":copy", "\x1d.\x1d" + rest[5:])
        if len(rest) >= 2 and rest[:2] == ":m" and (len(rest) < 3 or not rest[2].isalpha()):
            return (count, ":move", "\x1d.\x1d" + rest[2:])
        if len(rest) >= 2 and rest[:2] == ":t" and (len(rest) < 3 or not rest[2].isalpha()):
            return (count, ":copy", "\x1d.\x1d" + rest[2:])
        # vim :g/PAT/d and :v/PAT/d and :g!/PAT/d — global delete
        # Encoded as :d with sentinel \x1d{mode}:{PAT}\x1d  where mode is g|v.
        if len(rest) >= 4 and (rest[:2] == ":g" or rest[:2] == ":v"):
            mode = "v" if rest[:2] == ":v" else "g"
            k = 2
            # :g! is equivalent to :v
            if rest[:2] == ":g" and k < len(rest) and rest[k] == "!":
                mode = "v"
                k += 1
            if k < len(rest) and rest[k] == "/":
                k += 1
                pat_buf: List[str] = []
                while k < len(rest):
                    ch = rest[k]
                    if ch == "\\" and k + 1 < len(rest) and rest[k + 1] == "/":
                        pat_buf.append("/")
                        k += 2
                        continue
                    if ch == "/":
                        break
                    pat_buf.append(ch)
                    k += 1
                if (
                    k < len(rest) and rest[k] == "/"
                    and k + 1 < len(rest) and rest[k + 1] == "d"
                ):
                    pat = "".join(pat_buf)
                    trailing = rest[k + 2:]
                    return (count, ":d", f"\x1d{mode}:{pat}\x1d{trailing}")
        # two-char ex commands: :s/PAT/REPL/flags  and  :r FILE
        if len(rest) >= 2 and rest[:2] == ":s":
            return (count, ":s", rest[2:])
        if len(rest) >= 2 and rest[:2] == ":r":
            return (count, ":r", rest[2:])
        # :w / :write / :wq / :wq! / :wa / :x / :x! — supertool writes
        # atomically; treat all write-quit variants as no-op. Kevin types :w/:wq
        # reflexively. Match exact known prefixes — don't fall through to
        # heuristics that miss alpha suffixes like `q`/`a`.
        _WRITE_NOOP_PREFIXES = (
            ":wq!", ":wq", ":wa!", ":wa", ":write", ":w!", ":w",
            ":x!", ":x", ":xa!", ":xa",
        )
        for _wp in _WRITE_NOOP_PREFIXES:
            if rest == _wp or rest.startswith(_wp) and (
                len(rest) == len(_wp) or rest[len(_wp)] in " \t"
            ):
                return (count, ":noop", rest[len(_wp):])
        # three-char operator-motion: dgg, ygg, cgg, dge, dgE, dg_, yge, ygE, yg_, cge, cgE, cg_
        if len(rest) >= 3 and rest[:3] in (
            "dgg", "ygg", "cgg",
            "dge", "ygE", "yg_", "ygE", "yge",
            "dgE", "dg_", "cge", "cgE", "cg_",
        ):
            return (count, rest[:3], rest[3:])
        # Linewise case verbs: g~~, guu, gUU
        if len(rest) >= 3 and rest[:3] in ("g~~", "guu", "gUU"):
            return (count, rest[:3], rest[3:])
        # Operator-motion case verbs: g~<motion>, gu<motion>, gU<motion>.
        # Returns verb = "g~"|"gu"|"gU", arg = motion char (+ any tail).
        if len(rest) >= 3 and rest[0] == "g" and rest[1] in ("~", "u", "U"):
            return (count, rest[:2], rest[2:])
        # standalone ge / gE / g_ / gJ
        if len(rest) >= 2 and rest[:2] in ("ge", "gE", "g_", "gJ"):
            return (count, rest[:2], rest[2:])
        # gi — insert at last edit position (greedy text after)
        if len(rest) >= 2 and rest[:2] == "gi":
            return (count, "gi", rest[2:])
        # R — overwrite mode (greedy text)
        if rest[0] == "R":
            return (count, "R", rest[1:])
        # m{X} — set mark (X = a-zA-Z)
        if len(rest) >= 2 and rest[0] == "m" and (
            ("a" <= rest[1] <= "z") or ("A" <= rest[1] <= "Z")
        ):
            return (count, "m", rest[1:2] + rest[2:][:0]) if False else (count, "m" + rest[1], rest[2:])
        # `{X} — jump to mark exact, or `` for last jump
        if len(rest) >= 2 and rest[0] == "`" and (
            ("a" <= rest[1] <= "z") or ("A" <= rest[1] <= "Z") or rest[1] == "`"
        ):
            return (count, "`" + rest[1], rest[2:])
        # '{X} — jump to mark line, or '' for last jump
        if len(rest) >= 2 and rest[0] == "'" and (
            ("a" <= rest[1] <= "z") or ("A" <= rest[1] <= "Z") or rest[1] == "'"
        ):
            return (count, "'" + rest[1], rest[2:])
        # >> << == — indent/dedent/re-indent current line
        if len(rest) >= 2 and rest[:2] in (">>", "<<", "=="):
            return (count, rest[:2], rest[2:])
        # > / < / = + [motion-count] + motion  (e.g. >j, >2j, <G, =ap)
        if len(rest) >= 2 and rest[0] in "><=" and rest[1] != rest[0]:
            op = rest[0]
            # skip optional embedded motion count digits
            mi = 1
            while mi < len(rest) and rest[mi].isdigit():
                mi += 1
            motion_count = int(rest[1:mi]) if mi > 1 else 1
            tail = rest[mi:]  # everything after the digits
            _to = set('wWsp"\'`()[]{}<>bBt')
            # text-object form: >iw, <ap, =ap, etc. (no digit before i/a)
            if mi == 1 and len(tail) >= 2 and tail[0] in "ia" and tail[1] in _to:
                return (count, op + tail[0] + tail[1], tail[2:])
            # gg (3-char: op + gg)
            if mi == 1 and len(tail) >= 2 and tail[0] == "g" and tail[1] == "g":
                return (count, op + "gg", tail[2:])
            # simple motion target — outer count repeats the op, motion_count
            # is the motion distance (e.g. 3>2j = indent 3 lines, 3 times).
            if tail and tail[0] in ("j", "k", "h", "l", "G", "{", "}", "(", ")",
                                    "%", "+", "-", "_", "w", "b", "e", "W", "B", "E",
                                    "$", "0", "^"):
                return (count, op + tail[0], str(motion_count) + tail[1:])
        # two-char yank/delete word/eol: yw, y$, yy, dw, d$, d0, c$, c0, cf, cF, ct, cT, df, dF, dt, dT
        # plus operator-motion: dG d^ dh dj dk dl, yG y^ yh yj yk yl, d/ d? y/ y?
        if len(rest) >= 2 and rest[:2] in (
            "gg", "dd", "cc", "cw",
            "yy", "yw", "y$",
            "dw", "d$", "d0",
            "c$", "c0",
            "dG", "d^", "dh", "dj", "dk", "dl",
            "yG", "y^", "yh", "yj", "yk", "yl",
            "d/", "d?", "y/", "y?",
            # New: paragraph/sentence/bracket/line/word operator-motion targets.
            "d{", "d}", "d(", "d)", "d%", "d+", "d-", "d_",
            "dW", "dB", "dE", "d;", "d,",
            "y{", "y}", "y(", "y)", "y%", "y+", "y-", "y_",
            "yW", "yB", "yE", "y;", "y,",
            "cG", "c^", "ch", "cj", "ck", "cl",
            "c{", "c}", "c(", "c)", "c%", "c+", "c-", "c_",
            "cW", "cB", "cE", "c;", "c,",
            "c/", "c?",
        ):
            return (count, rest[:2], rest[2:])
        # c/d/y + char-find motion (cf<c>, cF<c>, ct<c>, cT<c>, df<c>, ..., yt<c>)
        # arg = target char followed by optional TEXT (for c-variants only)
        if (
            len(rest) >= 3
            and rest[0] in ("c", "d", "y")
            and rest[1] in ("f", "F", "t", "T")
        ):
            return (count, rest[:2], rest[2:])
        c = rest[0]
        # search
        if c in ("/", "?"):
            return (count, c, rest[1:])
        # inserts — TEXT runs to end
        if c in ("i", "a", "I", "A", "o", "O"):
            return (count, c, rest[1:])
        # insert-mode-entry shortcuts: s (subst chars), S (subst lines),
        # C (change to EOL). TEXT runs to end.
        if c in ("s", "S", "C"):
            return (count, c, rest[1:])
        # single-char arg
        if c == "r":
            return (count, c, rest[1:2])
        # char-find on line: f<c>, F<c>, t<c>, T<c>
        if c in ("f", "F", "t", "T") and len(rest) >= 2:
            return (count, c, rest[1])
        # standalone
        if c in (
            "h", "j", "k", "l", "0", "$", "G", "D", "x", "J", "n", "N", "p", "P",
            "w", "b", "e", "^",
            # New motions:
            "W", "B", "E", "{", "}", "(", ")", "%", "+", "-", "_", ";", ",",
            # Case toggle + number ops:
            "~", "\x01", "\x18",
            # Tier-1 grab-bag single-char verbs:
            "Y", "*", "#",
            # undo / redo:
            "u", "\x12",
            # repeat last change:
            ".",
        ):
            return (count, c, rest[1:])
        # @<reg> / @@ — macro replay (2-char verb, no arg)
        if c == "@" and len(rest) >= 2 and (
            ("a" <= rest[1] <= "z") or rest[1] == "@"
        ):
            return (count, rest[:2], rest[2:])
        return (count, "", rest)  # unknown

    _state = _vim_load_state(path, len(content))
    cursor = _state["cursor"]
    marks: dict = dict(_state["marks"])  # {char: offset}
    last_edit = _state["last_edit"]      # int|None
    last_change = _state.get("last_change")  # dict|None: last buffer-mutating action for `.`
    macros: dict = dict(_state.get("macros", {}))  # {reg: raw_body_str}
    macros.update(macros_pending)        # definitions from this script win
    last_replayed_macro: Optional[str] = None  # register name; @@ uses this
    _macro_replay_count: int = 0  # recursion guard: total @<reg> dispatches this script
    prev_cursor = cursor                 # for `` and '' jump-back
    log: List[str] = []
    last_search: Optional[tuple] = None  # (pattern, direction "/"|"?")
    last_find: Optional[tuple] = None  # (verb in fFtT, target char) for ; ,
    register: str = ""  # anonymous yank/paste register
    register_linewise: bool = False  # True if last yank was line-wise (yy)
    # Undo / redo stacks (Tier 1: within-script).  Each entry = (content, cursor, marks).
    undo_stack: List[tuple] = []
    redo_stack: List[tuple] = []
    # Tier 2: cross-call snapshot — pre-edit state from the *previous* script call.
    # Loaded lazily on the first `u` that finds an empty undo_stack.
    _xundo_snapshot = _vim_load_undo_snapshot(path)  # None or {content, cursor, marks}
    # Snapshot the state at script entry for cross-call undo (saved at end).
    _entry_content = content
    _entry_cursor = cursor
    _entry_marks = dict(marks)
    # V-alias rewrites: V is visual-line in real vim, but supertool has no
    # visual mode. Kevin's muscle memory reaches for `Vcc`/`Vdd`/`Vyy`/
    # `Vjcc`/`VGd` anyway. These are all expressible as line-ops or ex
    # ranges. Rewrite at action-list level (NORMAL-mode only — insert
    # text is greedy until ESC so `iVcc` already arrives as one action
    # starting with `i`, not `V`).
    _V_LITERAL_REWRITES = {
        "Vcc": "cc",
        "Vdd": "dd",
        "Vyy": "yy",
        "Vd": "dd",
        "Vy": "yy",
        "Vc": "cc",
        "VGd": ":.,$d",
        "VGy": ":.,$y",
        "Vggd": ":1,.d",
        "Vggy": ":1,.y",
    }
    _V_MOTION_LINE = re.compile(r"^V(\d*)([jk])(cc|dd|yy|[dyc])(.*)$", re.DOTALL)
    # V<N>G<op> — visual-line + goto line N + op = `:.,<N><op>`.
    # E.g. `V145Gd` (line cursor through 145, delete) → `:.,145d`.
    _V_GOTO_LINE_OP = re.compile(r"^V(\d+)G([dyc])(.*)$", re.DOTALL)
    # V<motion>:<ex> — visual-line + ex command applied to the line range.
    # VG:<ex>   → :%<ex>    (current to EOF; with prior `gg` this is whole file)
    # Vgg:<ex>  → :1,.<ex>  (start to current)
    # V:<ex>    → :.<ex>    (current line only)
    _V_EX_REWRITES = (
        ("VG:", ":%"),
        ("Vgg:", ":1,."),
        ("V:", ":."),
    )

    def _rewrite_v_alias(act: str) -> str:
        if not act or act[0] != "V":
            return act
        for prefix, repl in _V_LITERAL_REWRITES.items():
            if act.startswith(prefix):
                return repl + act[len(prefix):]
        for prefix, repl in _V_EX_REWRITES:
            if act.startswith(prefix):
                rest = act[len(prefix):]
                # Kevin sometimes uses both V<motion> AND an explicit ex
                # range (`VG:%d`, `Vgg:1,5d`). The user-provided ex range
                # wins — strip our prefix's range to avoid `:%%d`/`:1,.1,5d`.
                if rest.startswith("%") or (rest and rest[0].isdigit()) or rest.startswith("."):
                    return ":" + rest
                return repl + rest
        # V<n>?j/k<op>... → <n+1><op><op>... (V + n-line motion = n+1 lines)
        m = _V_MOTION_LINE.match(act)
        if m is not None:
            n = int(m.group(1) or "1")
            op = m.group(3)
            # Single op (d/y/c) → double it for line-op semantics
            if len(op) == 1:
                op = op + op
            return f"{n + 1}{op}{m.group(4)}"
        # V<N>G<op>... → :.,<N><op>...  (line-cursor through line N + op)
        m = _V_GOTO_LINE_OP.match(act)
        if m is not None:
            return f":.,{m.group(1)}{m.group(2)}{m.group(3)}"
        return act

    # v-char-alias rewrites: `v<motion><op>` → `<op><motion>`.
    # char-visual selects then applies op; without visual mode the
    # standard operator-motion form is equivalent.
    # Pattern: v + optional count + motion + op (d/y/c) + optional tail.
    # Text-object motions: i/a + kind char.
    # gg motion (two chars).
    # Char-find motions: f/F/t/T + one char.
    # Simple motions: single char from the set below.
    _V_CHAR_SIMPLE = set("wbeWBEjkhl$0^G{}()%;,")
    _V_CHAR_RE = re.compile(
        r"^v(\d*)"
        r"(gg|[ia][wWsp\"'`()\[\]{}<>bBt]|[fFtT].|[wbeWBEjkhl$0^G{}();,%])"
        r"([dyc])"
        r"(.*)$",
        re.DOTALL,
    )

    def _rewrite_v_char_alias(act: str) -> str:
        if not act or act[0] != "v":
            return act
        m = _V_CHAR_RE.match(act)
        if m is None:
            return act
        count, motion, op, tail = m.group(1), m.group(2), m.group(3), m.group(4)
        # Reconstruct as <count><op><motion><tail>
        return f"{count}{op}{motion}{tail}"

    # cc-typo: Kevin types `cciw<TEXT>` thinking it means `ciw<TEXT>` (change
    # inner word). Real vim parses as cc + greedy text "iwTEXT" (line replace
    # with literal "iwTEXT"). Detect `cc<ia><kind>` prefix and drop one c.
    _CC_TYPO = re.compile(r"^cc([ia])([wWsp\"'`()\[\]{}<>bBt])(.*)$", re.DOTALL)

    def _rewrite_cc_typo(act: str) -> str:
        m = _CC_TYPO.match(act)
        if m is None:
            return act
        return f"c{m.group(1)}{m.group(2)}{m.group(3)}"

    raw_actions = [_rewrite_cc_typo(_rewrite_v_alias(_rewrite_v_char_alias(a))) for a in raw_actions]

    def _push_undo() -> None:
        """Snapshot current state onto undo stack; clear redo stack."""
        undo_stack.append((content, cursor, dict(marks)))
        redo_stack.clear()

    for i, action in enumerate(raw_actions, 1):
        # Macro definition sentinel — body already in `macros`, nothing to execute.
        if action.startswith("__macro_def_"):
            reg = action[len("__macro_def_"):]
            log.append(f"  {i}. q{reg}...q (macro recorded, {len(macros.get(reg, ''))} chars)")
            continue

        count, verb, arg = _parse(action)

        if verb == "" and count != 1:
            return f"ERROR: action {i} '{action}': count without verb\n"

        # --- cursor movement ---
        if verb == "gg":
            cursor = 0
            log.append(f"  {i}. gg (BOF)")
        elif verb == "G":
            if action.lstrip()[:1].isdigit():
                # explicit count: goto line
                try:
                    cursor = _goto_line(content, count)
                except ValueError as e:
                    return f"ERROR: action {i} '{action}': {e}\n"
                log.append(f"  {i}. {count}G (line {count})")
            else:
                # vi G: go to BOL of LAST LINE (not past it). If file ends
                # with a trailing newline, skip it so cursor lands on the
                # real last line, not on a phantom empty line. This makes
                # `G;O...` correctly open above the last line (e.g. above
                # a class's closing `}`) and `G;dd` delete the last line.
                if not content:
                    cursor = 0
                else:
                    end = len(content)
                    if content[end - 1] == "\n":
                        end -= 1
                    cursor = _line_start(content, end)
                log.append(f"  {i}. G (BOL of last line, cursor={cursor})")
        elif verb == "0":
            cursor = _line_start(content, cursor)
            log.append(f"  {i}. 0 (BOL)")
        elif verb == "$":
            # vi: $ lands on LAST CHAR of line, not on \n.
            # Empty line: $ stays at BOL.
            eol = _line_end(content, cursor)
            bol = _line_start(content, cursor)
            cursor = max(bol, eol - 1) if eol > bol else bol
            log.append(f"  {i}. $ (last char, cursor={cursor})")
        elif verb == "/":
            if not arg:
                return f"ERROR: action {i} '{action}': empty / pattern\n"
            pat = arg
            idx = -1
            try:
                rx = re.compile(pat, re.MULTILINE)
                m = rx.search(content, cursor)
                if m is not None and m.start() != m.end():
                    idx = m.start()
            except re.error:
                pass
            if idx == -1:
                idx = content.find(pat, cursor)
            if idx == -1 and pat.endswith("/") and len(pat) > 1:
                # Autocorrect: trailing `/` is a sed/ex muscle-memory leftover
                # (e.g. `/NullLogger/`). Strip it and re-search. Always switch
                # `pat` to the trimmed form so the downstream BOF retry uses
                # the right needle.
                trimmed = pat[:-1]
                pat = trimmed
                try:
                    rx2 = re.compile(trimmed, re.MULTILINE)
                    m2 = rx2.search(content, cursor)
                    if m2 is not None and m2.start() != m2.end():
                        idx = m2.start()
                except re.error:
                    pass
                if idx == -1:
                    idx = content.find(trimmed, cursor)
            bof_retry = False
            if idx == -1 and cursor > 0:
                # Autocorrect: cursor persists across vim::: calls. If forward
                # search misses from a mid-file cursor, retry from BOF — the
                # match might be earlier in the file. Kevin's mental model
                # assumes each call starts at BOF.
                try:
                    rx_b = re.compile(pat, re.MULTILINE)
                    m_b = rx_b.search(content, 0)
                    if m_b is not None and m_b.start() != m_b.end():
                        idx = m_b.start()
                        bof_retry = True
                except re.error:
                    pass
                if idx == -1:
                    idx = content.find(pat, 0)
                    if idx != -1:
                        bof_retry = True
            if idx == -1:
                # sed-style auto-split: try truncating pattern at first `/<verb>`
                # boundary. Kevin's training has `/PAT/cmd` muscle memory; if
                # the short pattern matches, treat the trailing portion as a
                # follow-up action.
                split_m = re.search(
                    r"/([oOiIaAJ]|cc|cw|ciw|ci[\"'([{}]|cf|cF|ct|cT|dd|dw|d\$|d0|c\$|c0)\b",
                    pat,
                )
                if split_m is not None:
                    short_pat = pat[:split_m.start()]
                    trail = pat[split_m.start() + 1:]
                    s_idx = -1
                    try:
                        rx2 = re.compile(short_pat, re.MULTILINE)
                        sm = rx2.search(content, cursor)
                        if sm is not None and sm.start() != sm.end():
                            s_idx = sm.start()
                    except re.error:
                        pass
                    if s_idx == -1:
                        s_idx = content.find(short_pat, cursor)
                    if s_idx != -1:
                        cursor = s_idx
                        last_search = (short_pat, "/")
                        log.append(
                            f"  {i}. /{short_pat!r} → {cursor} (auto-split sed-style)"
                        )
                        # queue the trailing action for the next iteration
                        raw_actions.insert(i, trail)
                        continue
                # Literal-fallback: decode then strip backslash escapes,
                # try plain content.find. Same logic as :s — handles
                # unescaped `(`, `)`, `$` and hex/unicode escapes.
                literal_pat = _vim_literal_decode(pat)
                if literal_pat:
                    for start in (cursor, 0):
                        lit_idx = content.find(literal_pat, start)
                        if lit_idx != -1:
                            cursor = lit_idx
                            last_search = (literal_pat, "/")
                            note = " (literal-mode autocorrect)"
                            if start == 0 and start < cursor:
                                note += " (retried from BOF)"
                            log.append(f"  {i}. /{literal_pat!r} → {cursor}{note}")
                            break
                    else:
                        lit_idx = -1
                    if lit_idx != -1:
                        continue
                hint = ""
                if split_m is not None:
                    suggested = pat[:split_m.start()] + ";" + pat[split_m.start() + 1:]
                    hint = f" (hint: '/' is not an action separator — did you mean '/{suggested}'? Use ';' to chain actions.)"
                near = _vim_nearest_literal_hint(content, pat, original=_before_content)
                return f"ERROR: action {i} '{action}': pattern not found forward{hint}{near}\n"
            cursor = idx
            last_search = (pat, "/")
            note = " (retried from BOF — cursor persisted from previous call)" if bof_retry else ""
            log.append(f"  {i}. /{pat!r} → {cursor}{note}")
        elif verb == "?":
            if not arg:
                return f"ERROR: action {i} '{action}': empty ? pattern\n"
            pat = arg
            idx = -1
            try:
                rx = re.compile(pat, re.MULTILINE)
                last = None
                # vi `?` includes the line/char cursor is on, so scan up to
                # cursor+1 and accept matches that END at or before cursor+1.
                for m in rx.finditer(content):
                    if m.end() > cursor + 1:
                        break
                    if m.start() != m.end():
                        last = m
                if last is not None:
                    idx = last.start()
            except re.error:
                pass
            if idx == -1:
                idx = content.rfind(pat, 0, cursor + 1)
            if idx == -1 and pat.endswith("/") and len(pat) > 1:
                # Autocorrect: trailing `/` is a sed/ex muscle-memory leftover.
                # Always reassign `pat` so the EOF retry below uses the
                # trimmed needle.
                trimmed = pat[:-1]
                pat = trimmed
                try:
                    rx2 = re.compile(trimmed, re.MULTILINE)
                    last2 = None
                    for m in rx2.finditer(content):
                        if m.end() > cursor + 1:
                            break
                        if m.start() != m.end():
                            last2 = m
                    if last2 is not None:
                        idx = last2.start()
                except re.error:
                    pass
                if idx == -1:
                    idx = content.rfind(trimmed, 0, cursor + 1)
            eof_retry = False
            if idx == -1 and cursor < len(content):
                # Autocorrect: cursor persists across vim::: calls. If backward
                # search misses from a near-BOF cursor, retry across the whole
                # file — the match might be later. Symmetric to the BOF retry
                # on `/PAT`.
                try:
                    rx_e = re.compile(pat, re.MULTILINE)
                    last_e = None
                    for m in rx_e.finditer(content):
                        if m.start() != m.end():
                            last_e = m
                    if last_e is not None:
                        idx = last_e.start()
                        eof_retry = True
                except re.error:
                    pass
                if idx == -1:
                    idx = content.rfind(pat)
                    if idx != -1:
                        eof_retry = True
            if idx == -1:
                # Literal-fallback for unescaped regex meta (`(`, `)`, `.`).
                literal_pat = _vim_literal_decode(pat)
                if literal_pat:
                    for upper in (cursor + 1, len(content)):
                        lit_idx = content.rfind(literal_pat, 0, upper)
                        if lit_idx != -1:
                            cursor = lit_idx
                            last_search = (literal_pat, "?")
                            note = " (literal-mode autocorrect)"
                            if upper == len(content) and upper > cursor + 1:
                                note += " (retried to EOF)"
                            log.append(f"  {i}. ?{literal_pat!r} → {cursor}{note}")
                            break
                    else:
                        lit_idx = -1
                    if lit_idx != -1:
                        continue
                near = _vim_nearest_literal_hint(content, pat, original=_before_content)
                return f"ERROR: action {i} '{action}': pattern not found backward{near}\n"
            cursor = idx
            last_search = (pat, "?")
            note = " (retried from EOF — cursor persisted from previous call)" if eof_retry else ""
            log.append(f"  {i}. ?{pat!r} → {cursor}{note}")
        elif verb == "h":
            cursor = max(0, cursor - count)
            log.append(f"  {i}. {count}h (cursor={cursor})")
        elif verb == "l":
            cursor = min(len(content), cursor + count)
            log.append(f"  {i}. {count}l (cursor={cursor})")
        elif verb in ("j", "k"):
            cur_line, cur_col = _offset_to_line_col(content, cursor)
            total_lines = content.count("\n") + 1
            target = cur_line - count if verb == "k" else cur_line + count
            target = max(1, min(total_lines, target))
            try:
                base = _goto_line(content, target)
            except ValueError as e:
                return f"ERROR: action {i} '{action}': {e}\n"
            line_text = content[base:_line_end(content, base)]
            cursor = base + min(cur_col - 1, len(line_text))
            log.append(f"  {i}. {count}{verb} (cursor={cursor})")

        # --- inserts ---
        elif verb in ("i", "a", "I", "A", "o", "O"):
            _push_undo()
            # Verb-bleed autocorrect: Kevin's muscle memory types `oi<indent>TEXT`
            # because real vim users habitually type an insert verb after `o`/`O`
            # (which already enter insert mode). In real vim this inserts the
            # literal verb char. Strip a redundant insert verb followed by
            # whitespace (indent) — Kevin never wants `i        text` literal,
            # and the whitespace makes false positives near-zero.
            verb_bleed_hint = ""
            if (
                len(arg) >= 2
                and arg[0] in ("i", "I", "a", "A", "o", "O")
                and arg[1] in (" ", "\t")
            ):
                verb_bleed_hint = (
                    f" [autocorrect: stripped redundant '{arg[0]}' verb bleed]"
                )
                arg = arg[1:]
            # Search-then-open autocorrect: Kevin (T6+T10 CoverageAudit) types
            # `o?PAT\e<more>` thinking `o?` searches backward then opens. Real
            # vim inserts `?PAT` as literal. When TEXT after `o`/`O` is a single
            # line starting with `?` or `/` followed by 2+ non-whitespace chars
            # and NOTHING ELSE — that's the search reflex, not content. Defer
            # the open: cursor jumps via search, the FOLLOWING action handles
            # the actual insert. Here we just drop this no-op open and replay
            # the search inline by mutating cursor.
            if (
                verb in ("o", "O")
                and len(arg) >= 3
                and arg[0] in ("?", "/")
                and "\n" not in arg
                and " " not in arg
                and "\t" not in arg
            ):
                # Run the search now; skip the open (Kevin never wanted content here).
                _pat = arg[1:]
                _direction = arg[0]
                try:
                    _rx = re.compile(_pat, re.MULTILINE)
                except re.error:
                    _rx = None
                if _rx is not None:
                    if _direction == "/":
                        _m = _rx.search(content, cursor)
                        if _m is None:
                            _m = _rx.search(content)
                    else:
                        # backward — find last match before cursor
                        _hits = list(_rx.finditer(content[:cursor]))
                        _m = _hits[-1] if _hits else None
                        if _m is None:
                            _hits = list(_rx.finditer(content))
                            _m = _hits[-1] if _hits else None
                    if _m is not None:
                        cursor = _m.start()
                        last_search = (_pat, _direction)
                        log.append(
                            f"  {i}. {verb}{arg!r} → autocorrect: search-then-open reflex; "
                            f"jumped to match at {cursor}, awaiting next action for content"
                        )
                        continue
            text = _decode_escapes(arg) * count
            # Auto-indent for `o`/`O` (vim's default `autoindent` behavior).
            # Prepend the current line's leading whitespace to TEXT first line
            # so Kevin doesn't manually re-indent every inserted block.
            # Skip when TEXT already starts with whitespace (Kevin provided it).
            if verb in ("o", "O") and text and text[0] not in (" ", "\t"):
                _bol = _line_start(content, cursor)
                _eol_cur = _line_end(content, cursor)
                _cur_line_text = content[_bol:_eol_cur]
                _indent = _cur_line_text[:len(_cur_line_text) - len(_cur_line_text.lstrip(" \t"))]
                if _indent:
                    text = _indent + text
            if verb == "i":
                pos = cursor
            elif verb == "a":
                pos = min(len(content), cursor + 1)
            elif verb == "I":
                pos = _line_start(content, cursor)
            elif verb == "A":
                pos = _line_end(content, cursor)
            elif verb == "o":
                eol = _line_end(content, cursor)
                content = content[:eol] + "\n" + content[eol:]
                pos = eol + 1
            else:  # 'O'
                bol = _line_start(content, cursor)
                content = content[:bol] + "\n" + content[bol:]
                pos = bol
            content = content[:pos] + text + content[pos:]
            # Shift marks/last_edit at or after insert point by len(text)
            delta = len(text)
            if delta:
                for _mk in list(marks.keys()):
                    if marks[_mk] >= pos:
                        marks[_mk] += delta
                if last_edit is not None and last_edit >= pos:
                    last_edit += delta
            cursor = pos + len(text)
            last_edit = cursor
            last_change = {"verb": verb, "count": count, "arg": arg}
            preview = text if len(text) <= 30 else text[:27] + "..."
            log.append(f"  {i}. {verb}{preview!r} (len={len(text)}){verb_bleed_hint}")

        # --- deletes ---
        elif verb == "x":
            _push_undo()
            end = min(len(content), cursor + count)
            content = content[:cursor] + content[end:]
            last_change = {"verb": "x", "count": count, "arg": ""}
            log.append(f"  {i}. {count}x ({end - cursor} chars)")
        elif verb == "dd":
            _push_undo()
            # delete count whole lines starting at current line
            bol = _line_start(content, cursor)
            end = bol
            for _ in range(count):
                nl = content.find("\n", end)
                end = nl + 1 if nl != -1 else len(content)
                if end >= len(content):
                    break
            content = content[:bol] + content[end:]
            cursor = bol if bol < len(content) else max(0, len(content))
            last_change = {"verb": "dd", "count": count, "arg": ""}
            log.append(f"  {i}. {count}dd (cursor={cursor})")
        elif verb == "D":
            _push_undo()
            eol = _line_end(content, cursor)
            content = content[:cursor] + content[eol:]
            log.append(f"  {i}. D ({eol - cursor} chars)")

        # --- insert-mode-entry shortcuts: s / S / C ---
        # s  = Ns: delete N chars from cursor, insert TEXT.
        # S  = NS: delete N whole lines starting at cursor's line
        #         (drop trailing \n of last so we re-insert into a blank line
        #         at BOL — like vim's cc), insert TEXT at BOL.
        # C  = c$: delete cursor → EOL (not past \n), insert TEXT.
        elif verb == "s":
            _push_undo()
            end = min(len(content), cursor + count)
            text = _decode_escapes(arg)
            content = content[:cursor] + text + content[end:]
            cursor = cursor + len(text)
            preview = text if len(text) <= 30 else text[:27] + "..."
            log.append(f"  {i}. {count}s{preview!r} (cursor={cursor})")
        elif verb == "S":
            _push_undo()
            bol = _line_start(content, cursor)
            end = bol
            for _ in range(count):
                nl = content.find("\n", end)
                if nl == -1:
                    end = len(content)
                    break
                end = nl + 1
            # Like cc: preserve the trailing \n of the last replaced line.
            keep_nl = end > bol and content[end - 1] == "\n"
            slice_end = end - 1 if keep_nl else end
            text = _decode_escapes(arg)
            content = content[:bol] + text + content[slice_end:]
            cursor = bol + len(text)
            preview = text if len(text) <= 30 else text[:27] + "..."
            log.append(f"  {i}. {count}S{preview!r} (cursor={cursor})")
        elif verb == "C":
            _push_undo()
            eol = _line_end(content, cursor)
            text = _decode_escapes(arg)
            content = content[:cursor] + text + content[eol:]
            cursor = cursor + len(text)
            preview = text if len(text) <= 30 else text[:27] + "..."
            log.append(f"  {i}. C{preview!r} (cursor={cursor})")

        # --- change inner word ---
        elif verb == "ciw":
            _push_undo()
            if cursor >= len(content) or not (content[cursor].isalnum() or content[cursor] == "_"):
                return f"ERROR: action {i} '{action}': ciw needs cursor on word char\n"
            ws = cursor
            while ws > 0 and (content[ws - 1].isalnum() or content[ws - 1] == "_"):
                ws -= 1
            we = cursor
            while we < len(content) and (content[we].isalnum() or content[we] == "_"):
                we += 1
            text = _decode_escapes(arg)
            content = content[:ws] + text + content[we:]
            cursor = ws + len(text)
            last_change = {"verb": "ciw", "count": 1, "arg": arg}
            preview = text if len(text) <= 30 else text[:27] + "..."
            log.append(f"  {i}. ciw{preview!r} (cursor={cursor})")

        # --- change inside delimiter: ci" ci' ci( ci[ ci{ ---
        elif verb in ('ci"', "ci'", "ci(", "ci[", "ci{"):
            _push_undo()
            opener = verb[2]
            pairs = {'"': '"', "'": "'", "(": ")", "[": "]", "{": "}"}
            closer = pairs[opener]
            # Find opener at-or-before cursor, closer after cursor.
            # For symmetric delims (" '): search the nearest pair surrounding cursor.
            if opener == closer:
                start = content.rfind(opener, 0, cursor + 1)
                if start == -1:
                    start = content.find(opener, cursor)
                if start == -1:
                    return f"ERROR: action {i} '{action}': no opening {opener} found\n"
                end = content.find(closer, start + 1)
                if end == -1:
                    return f"ERROR: action {i} '{action}': no closing {closer} found\n"
            else:
                start = content.rfind(opener, 0, cursor + 1)
                if start == -1:
                    start = content.find(opener, cursor)
                if start == -1:
                    return f"ERROR: action {i} '{action}': no opening {opener} found\n"
                # match nested pairs forward from start+1
                depth = 1
                end = -1
                j = start + 1
                while j < len(content):
                    if content[j] == opener:
                        depth += 1
                    elif content[j] == closer:
                        depth -= 1
                        if depth == 0:
                            end = j
                            break
                    j += 1
                if end == -1:
                    return f"ERROR: action {i} '{action}': no matching {closer} found\n"
            text = _decode_escapes(arg)
            content = content[:start + 1] + text + content[end:]
            cursor = start + 1 + len(text)
            preview = text if len(text) <= 30 else text[:27] + "..."
            log.append(f"  {i}. {verb}{preview!r} (cursor={cursor})")

        # --- generic text-object family: <op>i<X> / <op>a<X> ---
        # ops: d c y g~ gu gU. kinds: w W s p " ' ` ( ) [ ] { } < > b B t
        elif (
            (len(verb) == 3 and verb[0] in ("c", "d", "y") and verb[1] in ("i", "a") and verb[2] in 'wWsp"\'`()[]{}<>bBt')
            or (len(verb) == 4 and verb[0] == "g" and verb[1] in ("~", "u", "U") and verb[2] in ("i", "a") and verb[3] in 'wWsp"\'`()[]{}<>bBt')
        ):
            if len(verb) == 3:
                op = verb[0]
                around = verb[1] == "a"
                kind = verb[2]
            else:
                op = verb[:2]
                around = verb[2] == "a"
                kind = verb[3]
            try:
                ts, te = _resolve_text_object(content, cursor, kind, around)
            except _TextObjectError as e:
                return f"ERROR: action {i} '{action}': {e}\n"
            slice_ = content[ts:te]
            if op == "y":
                register = slice_
                register_linewise = False
                log.append(f"  {i}. {verb} (yanked {len(slice_)} chars)")
            elif op == "d":
                register = slice_
                register_linewise = False
                content = content[:ts] + content[te:]
                cursor = min(ts, len(content))
                log.append(f"  {i}. {verb} (deleted {len(slice_)} chars)")
            elif op == "c":
                register = slice_
                register_linewise = False
                text = _decode_escapes(arg) if arg else ""
                content = content[:ts] + text + content[te:]
                cursor = ts + len(text)
                preview = text if len(text) <= 30 else text[:27] + "..."
                log.append(f"  {i}. {verb}{preview!r} (cursor={cursor})")
            elif op in ("g~", "gu", "gU"):
                if op == "g~":
                    new = slice_.swapcase()
                elif op == "gu":
                    new = slice_.lower()
                else:
                    new = slice_.upper()
                content = content[:ts] + new + content[te:]
                cursor = ts
                log.append(f"  {i}. {verb} ({len(slice_)} chars)")

        # --- change word (cursor to end of word) ---
        elif verb == "cw":
            if cursor >= len(content):
                return f"ERROR: action {i} '{action}': cw at EOF\n"
            we = cursor
            on_word = content[we].isalnum() or content[we] == "_"
            if on_word:
                while we < len(content) and (content[we].isalnum() or content[we] == "_"):
                    we += 1
            else:
                while we < len(content) and not (content[we].isalnum() or content[we] == "_") and content[we] != "\n":
                    we += 1
            text = _decode_escapes(arg)
            content = content[:cursor] + text + content[we:]
            cursor = cursor + len(text)
            last_change = {"verb": "cw", "count": 1, "arg": arg}
            preview = text if len(text) <= 30 else text[:27] + "..."
            log.append(f"  {i}. cw{preview!r} (cursor={cursor})")

        # --- change line(s) ---
        elif verb == "cc":
            bol = _line_start(content, cursor)
            end = bol
            for _ in range(count):
                nl = content.find("\n", end)
                if nl == -1:
                    end = len(content)
                    break
                end = nl + 1
            # cc keeps the trailing newline of the last line replaced (like vi: replaces line content, not the \n)
            keep_nl = end > bol and content[end - 1] == "\n"
            slice_end = end - 1 if keep_nl else end
            text = _decode_escapes(arg)
            content = content[:bol] + text + content[slice_end:]
            cursor = bol + len(text)
            last_change = {"verb": "cc", "count": count, "arg": arg}
            preview = text if len(text) <= 30 else text[:27] + "..."
            log.append(f"  {i}. {count}cc{preview!r} (cursor={cursor})")

        # --- join lines ---
        elif verb == "J":
            _push_undo()
            joined = 0
            for _ in range(count):
                nl = content.find("\n", cursor)
                if nl == -1:
                    break
                # vi J replaces \n + leading whitespace of next line with a single space (unless next line empty)
                k = nl + 1
                while k < len(content) and content[k] in (" ", "\t"):
                    k += 1
                sep = " " if k < len(content) and content[k] != "\n" else ""
                content = content[:nl] + sep + content[k:]
                cursor = nl + (1 if sep else 0)
                joined += 1
            log.append(f"  {i}. {count}J (joined {joined})")

        # --- replace ---
        elif verb == "r":
            _push_undo()
            if not arg:
                return f"ERROR: action {i} '{action}': r needs a char\n"
            if cursor >= len(content):
                return f"ERROR: action {i} '{action}': r at EOF\n"
            content = content[:cursor] + arg[0] + content[cursor + 1:]
            log.append(f"  {i}. r{arg[0]!r}")

        # --- char-find on line: f<c> F<c> t<c> T<c> ---
        elif verb in ("f", "F", "t", "T"):
            if not arg:
                return f"ERROR: action {i} '{action}': {verb} needs a char\n"
            target = arg[0]
            bol = _line_start(content, cursor)
            eol = _line_end(content, cursor)
            if verb == "f":
                idx = content.find(target, cursor + 1, eol)
            elif verb == "F":
                idx = content.rfind(target, bol, cursor)
            elif verb == "t":
                hit = content.find(target, cursor + 1, eol)
                idx = hit - 1 if hit != -1 else -1
            else:  # T
                hit = content.rfind(target, bol, cursor)
                idx = hit + 1 if hit != -1 else -1
            if idx == -1:
                return f"ERROR: action {i} '{action}': {verb}{target!r} not found on line\n"
            cursor = idx
            last_find = (verb, target)
            log.append(f"  {i}. {verb}{target!r} → {cursor}")

        # --- word motion: w b e ^ ---
        elif verb == "w":
            def _is_w(ch: str) -> bool:
                return ch.isalnum() or ch == "_"
            for _ in range(count):
                if cursor >= len(content):
                    break
                if _is_w(content[cursor]):
                    while cursor < len(content) and _is_w(content[cursor]):
                        cursor += 1
                elif not content[cursor].isspace():
                    while (
                        cursor < len(content)
                        and not _is_w(content[cursor])
                        and not content[cursor].isspace()
                    ):
                        cursor += 1
                while (
                    cursor < len(content)
                    and content[cursor].isspace()
                    and content[cursor] != "\n"
                ):
                    cursor += 1
            log.append(f"  {i}. {count}w (cursor={cursor})")
        elif verb == "b":
            def _is_w(ch: str) -> bool:
                return ch.isalnum() or ch == "_"
            for _ in range(count):
                if cursor == 0:
                    break
                cursor -= 1
                while cursor > 0 and content[cursor].isspace():
                    cursor -= 1
                if _is_w(content[cursor]):
                    while cursor > 0 and _is_w(content[cursor - 1]):
                        cursor -= 1
                else:
                    while (
                        cursor > 0
                        and not _is_w(content[cursor - 1])
                        and not content[cursor - 1].isspace()
                    ):
                        cursor -= 1
            log.append(f"  {i}. {count}b (cursor={cursor})")
        elif verb == "e":
            def _is_w(ch: str) -> bool:
                return ch.isalnum() or ch == "_"
            for _ in range(count):
                if cursor >= len(content):
                    break
                if (
                    cursor + 1 < len(content)
                    and _is_w(content[cursor])
                    and not _is_w(content[cursor + 1])
                ):
                    cursor += 1
                while cursor < len(content) and content[cursor].isspace():
                    cursor += 1
                while (
                    cursor + 1 < len(content)
                    and _is_w(content[cursor + 1])
                ):
                    cursor += 1
            log.append(f"  {i}. {count}e (cursor={cursor})")
        elif verb == "^":
            bol = _line_start(content, cursor)
            eol = _line_end(content, cursor)
            pos = bol
            while pos < eol and content[pos] in (" ", "\t"):
                pos += 1
            cursor = pos
            log.append(f"  {i}. ^ (cursor={cursor})")

        # --- WORD motions: W B E (whitespace-delimited) ---
        elif verb == "W":
            for _ in range(count):
                if cursor >= len(content):
                    break
                # skip current non-whitespace WORD
                while cursor < len(content) and not content[cursor].isspace():
                    cursor += 1
                # skip whitespace (but not past \n in vim - actually W crosses lines)
                while cursor < len(content) and content[cursor].isspace():
                    cursor += 1
            log.append(f"  {i}. {count}W (cursor={cursor})")
        elif verb == "B":
            for _ in range(count):
                if cursor == 0:
                    break
                cursor -= 1
                # skip whitespace backward
                while cursor > 0 and content[cursor].isspace():
                    cursor -= 1
                # back to start of WORD
                while cursor > 0 and not content[cursor - 1].isspace():
                    cursor -= 1
            log.append(f"  {i}. {count}B (cursor={cursor})")
        elif verb == "E":
            for _ in range(count):
                if cursor >= len(content):
                    break
                # if already on last char of WORD, step forward into whitespace
                if (
                    cursor + 1 < len(content)
                    and not content[cursor].isspace()
                    and content[cursor + 1].isspace()
                ):
                    cursor += 1
                # skip whitespace
                while cursor < len(content) and content[cursor].isspace():
                    cursor += 1
                # advance to last non-whitespace of WORD
                while (
                    cursor + 1 < len(content)
                    and not content[cursor + 1].isspace()
                ):
                    cursor += 1
            log.append(f"  {i}. {count}E (cursor={cursor})")

        # --- back-to-word-end: ge / gE ---
        elif verb == "ge":
            def _is_w(ch: str) -> bool:
                return ch.isalnum() or ch == "_"
            for _ in range(count):
                if cursor == 0:
                    break
                cursor -= 1
                # skip whitespace backward
                while cursor > 0 and content[cursor].isspace():
                    cursor -= 1
                # if on a word char, step left while previous is same class (no-op: we want END of prev word)
                # cursor is now at end of some word/non-word run — that's the answer.
            log.append(f"  {i}. {count}ge (cursor={cursor})")
        elif verb == "gE":
            for _ in range(count):
                if cursor == 0:
                    break
                cursor -= 1
                while cursor > 0 and content[cursor].isspace():
                    cursor -= 1
            log.append(f"  {i}. {count}gE (cursor={cursor})")

        # --- line motions: g_, +, -, _ ---
        elif verb == "g_":
            # last non-blank of line (with count: down count-1 lines first)
            for _ in range(max(0, count - 1)):
                nl = content.find("\n", cursor)
                if nl == -1:
                    break
                cursor = nl + 1
            bol = _line_start(content, cursor)
            eol = _line_end(content, cursor)
            pos = eol - 1
            while pos >= bol and content[pos] in (" ", "\t"):
                pos -= 1
            cursor = max(bol, pos)
            log.append(f"  {i}. g_ (cursor={cursor})")
        elif verb == "+":
            for _ in range(count):
                nl = content.find("\n", cursor)
                if nl == -1:
                    break
                cursor = nl + 1
            # first non-blank of resulting line
            bol = _line_start(content, cursor)
            eol = _line_end(content, cursor)
            pos = bol
            while pos < eol and content[pos] in (" ", "\t"):
                pos += 1
            cursor = pos
            log.append(f"  {i}. {count}+ (cursor={cursor})")
        elif verb == "-":
            for _ in range(count):
                bol = _line_start(content, cursor)
                if bol == 0:
                    break
                cursor = _line_start(content, bol - 1)
            bol = _line_start(content, cursor)
            eol = _line_end(content, cursor)
            pos = bol
            while pos < eol and content[pos] in (" ", "\t"):
                pos += 1
            cursor = pos
            log.append(f"  {i}. {count}- (cursor={cursor})")
        elif verb == "_":
            # current line first non-blank; count goes down count-1 lines
            for _ in range(max(0, count - 1)):
                nl = content.find("\n", cursor)
                if nl == -1:
                    break
                cursor = nl + 1
            bol = _line_start(content, cursor)
            eol = _line_end(content, cursor)
            pos = bol
            while pos < eol and content[pos] in (" ", "\t"):
                pos += 1
            cursor = pos
            log.append(f"  {i}. {count}_ (cursor={cursor})")

        # --- paragraph motions: { } (blank-line boundaries) ---
        elif verb == "}":
            for _ in range(count):
                # find next blank line at or after cursor
                # blank line = "\n\n" or content starting with \n then \n.
                # Algorithm: walk forward from cursor; find offset of a \n
                # such that the next char is also \n or EOF.
                pos = cursor
                # if already on a blank line, step past it first
                bol = _line_start(content, pos)
                eol = _line_end(content, pos)
                if bol == eol:
                    pos = eol + 1 if eol < len(content) else len(content)
                while pos < len(content):
                    nl = content.find("\n", pos)
                    if nl == -1:
                        pos = len(content)
                        break
                    # line after this \n starts at nl+1
                    next_bol = nl + 1
                    next_eol = content.find("\n", next_bol)
                    if next_eol == -1:
                        next_eol = len(content)
                    if next_bol == next_eol:
                        # blank line found
                        pos = next_bol
                        break
                    pos = next_bol
                cursor = pos
            log.append(f"  {i}. {count}}} (cursor={cursor})")
        elif verb == "{":
            for _ in range(count):
                pos = cursor
                bol = _line_start(content, pos)
                eol = _line_end(content, pos)
                # if on a blank line, step back past it
                if bol == eol and bol > 0:
                    pos = bol - 1
                else:
                    pos = bol
                while pos > 0:
                    prev_eol = pos - 1  # this is a \n or before
                    prev_bol = _line_start(content, prev_eol)
                    prev_line_eol = _line_end(content, prev_bol)
                    if prev_bol == prev_line_eol:
                        pos = prev_bol
                        break
                    pos = prev_bol
                else:
                    pos = 0
                cursor = pos
            log.append(f"  {i}. {count}{{ (cursor={cursor})")

        # --- sentence motions: ( ) ---
        elif verb == ")":
            # forward to start of next sentence. Sentence boundary = .!? followed by space/newline/EOF.
            for _ in range(count):
                pos = cursor
                while pos < len(content):
                    ch = content[pos]
                    if ch in ".!?":
                        # check what follows
                        k = pos + 1
                        if k >= len(content):
                            pos = len(content)
                            break
                        if content[k] in (" ", "\t", "\n"):
                            # skip the punctuation and the whitespace
                            k += 1
                            while k < len(content) and content[k] in (" ", "\t", "\n"):
                                k += 1
                            pos = k
                            break
                    pos += 1
                cursor = pos
            log.append(f"  {i}. {count}) (cursor={cursor})")
        elif verb == "(":
            # backward to start of current sentence (or prev if already at start).
            for _ in range(count):
                pos = cursor
                # step back at least one to allow finding the previous boundary
                if pos > 0:
                    pos -= 1
                # walk back to find a .!? followed by whitespace, then advance past
                found = 0
                while pos > 0:
                    ch = content[pos]
                    if ch in ".!?" and pos + 1 < len(content) and content[pos + 1] in (" ", "\t", "\n"):
                        # found end of previous sentence; advance to start of current
                        k = pos + 1
                        while k < len(content) and content[k] in (" ", "\t", "\n"):
                            k += 1
                        found = k
                        break
                    pos -= 1
                cursor = found
            log.append(f"  {i}. {count}( (cursor={cursor})")

        # --- bracket match: % ---
        elif verb == "%":
            if cursor >= len(content):
                return f"ERROR: action {i} '{action}': % at EOF\n"
            pairs_fwd = {"(": ")", "[": "]", "{": "}"}
            pairs_bwd = {")": "(", "]": "[", "}": "{"}
            ch = content[cursor]
            if ch in pairs_fwd:
                opener, closer = ch, pairs_fwd[ch]
                depth = 1
                k = cursor + 1
                while k < len(content):
                    if content[k] == opener:
                        depth += 1
                    elif content[k] == closer:
                        depth -= 1
                        if depth == 0:
                            cursor = k
                            break
                    k += 1
                else:
                    return f"ERROR: action {i} '{action}': % no matching {closer!r}\n"
            elif ch in pairs_bwd:
                opener, closer = pairs_bwd[ch], ch
                depth = 1
                k = cursor - 1
                while k >= 0:
                    if content[k] == closer:
                        depth += 1
                    elif content[k] == opener:
                        depth -= 1
                        if depth == 0:
                            cursor = k
                            break
                    k -= 1
                else:
                    return f"ERROR: action {i} '{action}': % no matching {opener!r}\n"
            else:
                return f"ERROR: action {i} '{action}': % not on a bracket char (found {ch!r})\n"
            log.append(f"  {i}. % (cursor={cursor})")

        # --- repeat last find: ; , ---
        elif verb in (";", ","):
            if last_find is None:
                return f"ERROR: action {i} '{action}': no previous f/F/t/T to repeat\n"
            fverb, ftarget = last_find
            # , reverses direction
            if verb == ",":
                reverse_map = {"f": "F", "F": "f", "t": "T", "T": "t"}
                fverb = reverse_map[fverb]
            bol = _line_start(content, cursor)
            eol = _line_end(content, cursor)
            if fverb == "f":
                idx = content.find(ftarget, cursor + 1, eol)
            elif fverb == "F":
                idx = content.rfind(ftarget, bol, cursor)
            elif fverb == "t":
                hit = content.find(ftarget, cursor + 1, eol)
                # if cursor is right before the previously-found target, skip past
                if hit != -1 and hit == cursor + 1:
                    hit = content.find(ftarget, cursor + 2, eol)
                idx = hit - 1 if hit != -1 else -1
            else:  # T
                hit = content.rfind(ftarget, bol, cursor)
                if hit != -1 and hit == cursor - 1:
                    hit = content.rfind(ftarget, bol, cursor - 1)
                idx = hit + 1 if hit != -1 else -1
            if idx == -1:
                return f"ERROR: action {i} '{action}': {verb} no match\n"
            cursor = idx
            log.append(f"  {i}. {verb} → {cursor}")

        # --- repeat search: n / N ---
        elif verb in ("n", "N"):
            if last_search is None:
                return f"ERROR: action {i} '{action}': no previous search for {verb}\n"
            spat, sdir = last_search
            forward = (sdir == "/") if verb == "n" else (sdir != "/")
            idx = -1
            try:
                rx = re.compile(spat, re.MULTILINE)
                if forward:
                    m = rx.search(content, cursor + 1)
                    if m is not None and m.start() != m.end():
                        idx = m.start()
                else:
                    last = None
                    for m in rx.finditer(content[:cursor]):
                        if m.start() != m.end():
                            last = m
                    if last is not None:
                        idx = last.start()
            except re.error:
                if forward:
                    idx = content.find(spat, cursor + 1)
                else:
                    idx = content.rfind(spat, 0, cursor)
            if idx == -1:
                return f"ERROR: action {i} '{action}': {verb} no further match\n"
            cursor = idx
            log.append(f"  {i}. {verb} → {cursor}")

        # --- ex substitute: :s/PAT/REPL/flags ---
        elif verb == ":s":
            _push_undo()
            # Decode optional line-range prefix: \x1d{range}\x1d{body}.
            # The parser encodes ranges from `:Ns/...`, `:N,Ms/...`, `:.s/...`,
            # `:$s/...`, etc. Resolve `.` against cursor and `$` against
            # content here (parse-time didn't have either).
            sub_start = 0
            sub_end = len(content)
            if arg.startswith("\x1d"):
                close = arg.find("\x1d", 1)
                if close == -1:
                    return f"ERROR: action {i} '{action}': :s malformed range encoding\n"
                range_spec = arg[1:close]
                arg = arg[close + 1:]
                lines = content.split("\n")
                # vim line count excludes trailing-empty from a final `\n`
                total_lines = len(lines) - (1 if lines and lines[-1] == "" else 0)
                cursor_line, _ = _offset_to_line_col(content, cursor)

                def _resolve(addr: str) -> int:
                    return _vim_resolve_ex_address(addr, cursor_line, total_lines)

                if "," in range_spec:
                    a, b = range_spec.split(",", 1)
                else:
                    a, b = range_spec, range_spec
                try:
                    line_a = _resolve(a)
                    line_b = _resolve(b)
                except ValueError as e:
                    return f"ERROR: action {i} '{action}': :s range: {e}\n"
                if line_a < 1 or line_b < 1 or line_a > total_lines or line_b > total_lines:
                    return (
                        f"ERROR: action {i} '{action}': :s range {line_a}..{line_b} "
                        f"out of bounds (1..{total_lines})\n"
                    )
                if line_a > line_b:
                    return (
                        f"ERROR: action {i} '{action}': :s range start ({line_a}) "
                        f"is after end ({line_b})\n"
                    )
                # Compute byte slice for lines [line_a..line_b] (inclusive).
                # line N starts at byte offset of line N's first char.
                line_starts: List[int] = [0]
                for k, ch in enumerate(content):
                    if ch == "\n":
                        line_starts.append(k + 1)
                sub_start = line_starts[line_a - 1]
                # End offset: start of line_b+1 (exclusive), or len(content)
                # if line_b is the last line.
                if line_b < len(line_starts):
                    sub_end = line_starts[line_b]  # exclusive
                else:
                    sub_end = len(content)
            if not arg or arg[0] != "/":
                return f"ERROR: action {i} '{action}': :s needs /PAT/REPL/[flags]\n"
            # parse /PAT/REPL/flags honoring \/ as literal
            parts: List[str] = []
            buf: List[str] = []
            j = 1
            while j < len(arg):
                ch = arg[j]
                if ch == "\\" and j + 1 < len(arg) and arg[j + 1] == "/":
                    buf.append("/")
                    j += 2
                    continue
                if ch == "/":
                    parts.append("".join(buf))
                    buf = []
                    j += 1
                    if len(parts) == 2:
                        parts.append(arg[j:])
                        j = len(arg)
                    continue
                buf.append(ch)
                j += 1
            if len(parts) < 2:
                parts.append("".join(buf))
            while len(parts) < 3:
                parts.append("")
            spat, srepl, sflags = parts[0], parts[1], parts[2]
            if not spat:
                return f"ERROR: action {i} '{action}': :s needs non-empty PAT\n"
            flags_re = re.MULTILINE
            if "i" in sflags:
                flags_re |= re.IGNORECASE
            try:
                rx = re.compile(spat, flags_re)
            except re.error as e:
                # Regex parse failed — most common Kevin case is unescaped
                # parens (`assertEquals(`). Try literal-fallback before
                # erroring: decode the intended literal string and use
                # content.replace. Same gotcha covered for missed-matches
                # below, but parse-error path skipped it entirely.
                literal_pat = _vim_literal_decode(spat)
                if literal_pat and literal_pat in content:
                    is_global = "g" in sflags
                    is_dry = "d" in sflags
                    # In literal mode, also literal-decode the REPL — if Kevin
                    # over-escaped the PAT he likely over-escaped the REPL too
                    # (`assertSame\(` should become `assertSame(`).
                    srepl_dec_early = _vim_literal_decode(srepl) or _decode_escapes(srepl)
                    body = content[sub_start:sub_end]
                    occurrences = body.count(literal_pat)
                    if occurrences > 0 and not is_dry:
                        new_body = body.replace(
                            literal_pat,
                            srepl_dec_early,
                            -1 if is_global else 1,
                        )
                        content = content[:sub_start] + new_body + content[sub_end:]
                        cursor = min(cursor, len(content))
                        n_done = occurrences if is_global else 1
                        log.append(
                            f"  {i}. :s/{spat!r}/{srepl_dec_early!r}/{sflags} ({n_done} subs)"
                            f" [autocorrect: regex parse failed ({e}); literal mode → {literal_pat!r}]"
                        )
                        continue
                return f"ERROR: action {i} '{action}': :s regex: {e}\n"
            is_dry = "d" in sflags
            n_max = 0 if "g" in sflags else 1
            srepl_dec = _decode_escapes(srepl)
            # Escape literal backslashes for re.sub: \X (X non-digit) must be
            # passed as \\X or re.sub raises "bad escape" on \B, \R, etc.
            # Digit-prefixed backslashes (\1..\9) are preserved as backrefs.
            srepl_safe = re.sub(r"\\(?=\D)", r"\\\\", srepl_dec)
            # Per-line iteration matches real vim's line-oriented :s semantics
            # and avoids the `.*` empty-match-per-line-boundary bug. But if
            # the pattern explicitly contains a newline (`\n` decoded), the
            # user wants cross-line matching — fall back to whole-buffer.
            spat_decoded = _decode_escapes(spat)
            pattern_is_multiline = "\n" in spat_decoded
            def _run_sub(_rx):
                if pattern_is_multiline:
                    # Whole-buffer: pattern needs to see newlines.
                    head = content[:sub_start]
                    tail = content[sub_end:]
                    body = content[sub_start:sub_end]
                    new_body, _n = _rx.subn(srepl_safe, body, count=n_max)
                    return head + new_body + tail, _n
                # Single-line pattern → iterate per-line in the range so
                # /g vs no-flag means "all per line" vs "first per line",
                # and `.*` doesn't double-fire at line boundaries.
                head = content[:sub_start]
                tail = content[sub_end:]
                body = content[sub_start:sub_end]
                has_trailing_nl = body.endswith("\n")
                body_lines = body.split("\n")
                if has_trailing_nl:
                    body_lines = body_lines[:-1]
                is_global = "g" in sflags
                _n = 0
                new_lines: List[str] = []
                for ln in body_lines:
                    subbed_ln, k = _rx.subn(
                        srepl_safe, ln, count=0 if is_global else 1
                    )
                    new_lines.append(subbed_ln)
                    _n += k
                new_body = "\n".join(new_lines) + ("\n" if has_trailing_nl else "")
                return head + new_body + tail, _n
            try:
                new_content, n = _run_sub(rx)
            except re.error as e:
                return f"ERROR: action {i} '{action}': :s replacement: {e}\n"
            # Backslash over-escape autocorrect: Kevin (and bash users) often
            # write `\\\\` (4 chars after bash-quoting) when 2 are correct.
            # `\\\\` in a regex matches 2 literal backslashes; to match ONE,
            # write `\\`. If the original pattern matched nothing AND contains
            # 4 consecutive backslashes, retry with each `\\\\` halved to `\\`.
            autocorrect_hint = ""
            if n == 0 and "\\\\\\\\" in spat:
                spat_fixed = spat.replace("\\\\\\\\", "\\\\")
                try:
                    rx_fixed = re.compile(spat_fixed, flags_re)
                    new_content2, n2 = _run_sub(rx_fixed)
                except re.error:
                    n2 = 0
                    new_content2 = content
                if n2 > 0:
                    new_content = new_content2
                    n = n2
                    rx = rx_fixed
                    spat = spat_fixed
                    autocorrect_hint = (
                        f" [autocorrect: halved \\\\\\\\ → \\\\ in pattern → {spat_fixed!r}]"
                    )
            # Literal-fallback autocorrect: Kevin often writes regex metachars
            # he means as literals — `(`, `)`, `$`, plus over-escaped `\\X`.
            # Strip `\<X>` → `<X>` to get his intended literal string and try
            # plain content.replace. If it hits, use that result.
            if n == 0:
                # Always try literal fallback when regex misses — handles
                # both over-escaped patterns (`\\$`, `\\[`) AND unescaped
                # regex metas Kevin meant as literals (`(`, `)`, `.`).
                literal_pat = _vim_literal_decode(spat)
                if literal_pat:
                    body = content[sub_start:sub_end]
                    occurrences = body.count(literal_pat)
                    if occurrences > 0:
                        is_global = "g" in sflags
                        replace_count = -1 if is_global else 1
                        new_body = body.replace(literal_pat, srepl_dec, replace_count)
                        new_content = content[:sub_start] + new_body + content[sub_end:]
                        n = occurrences if is_global else 1
                        autocorrect_hint = (
                            f" [autocorrect: literal mode → {literal_pat!r}]"
                        )
            if n == 0:
                near = _vim_nearest_literal_hint(content, spat, original=_before_content)
                return f"ERROR: action {i} '{action}': :s no match for {spat!r}{near}\n"
            if is_dry:
                # Preview only. Show up to 5 match line numbers + the rendered
                # replacement, don't touch the buffer or persist anything.
                preview: List[str] = []
                shown = 0
                for m in rx.finditer(content):
                    if shown >= 5:
                        break
                    line_no = content[:m.start()].count("\n") + 1
                    try:
                        repl_rendered = m.expand(srepl_safe)
                    except re.error:
                        repl_rendered = srepl_dec
                    preview.append(
                        f"      line {line_no}: {m.group(0)!r} → {repl_rendered!r}"
                    )
                    shown += 1
                more = f"\n      ... and {n - shown} more" if n > shown else ""
                log.append(
                    f"  {i}. :s/{spat!r}/{srepl_dec!r}/{sflags} DRY — would replace {n}:\n"
                    + "\n".join(preview)
                    + more
                )
            else:
                content = new_content
                cursor = min(cursor, len(content))
                log.append(
                    f"  {i}. :s/{spat!r}/{srepl_dec!r}/{sflags} ({n} subs)"
                    + autocorrect_hint
                )

        # --- ex line goto: bare `:N`, `:$`, `:.` (no command after range) ---
        # Real vim: `:N\n` jumps to line N. Kevin types this instead of `NG`.
        # arg is the address spec (digits | `$` | `.`).
        elif verb == ":goto":
            spec = arg
            if spec.isdigit():
                try:
                    cursor = _goto_line(content, int(spec))
                except ValueError as e:
                    return f"ERROR: action {i} '{action}': {e}\n"
                log.append(f"  {i}. :{spec} (goto line {spec})")
            elif spec == "$":
                if not content:
                    cursor = 0
                else:
                    end = len(content)
                    if content[end - 1] == "\n":
                        end -= 1
                    cursor = end
                    # Move to BOL of last line
                    bol = content.rfind("\n", 0, cursor) + 1
                    cursor = bol
                log.append(f"  {i}. :$ (goto last line)")
            else:  # spec == "."
                log.append(f"  {i}. :. (current line, no-op)")

        # --- ex no-op: :w, :write, :wq, :wa, :x — supertool writes atomically ---
        elif verb == ":noop":
            log.append(f"  {i}. :w (no-op — supertool writes atomically)")

        # --- ex read file: :r FILE  (or `:r -` to read stdin, `:r !CMD` to shell) ---
        elif verb == ":r":
            _push_undo()
            # Range-prefix support: `:Nr FILE` → encoded as `\x1d{N}\x1d FILE`.
            # Resolve N to a cursor position so the standard insert-after-line
            # logic below targets line N.
            if arg.startswith("\x1d"):
                _close = arg.find("\x1d", 1)
                if _close != -1:
                    _spec = arg[1:_close].strip()
                    arg = arg[_close + 1:]
                    if _spec:
                        try:
                            _cur_line, _ = _offset_to_line_col(content, cursor)
                            _total = content.count("\n") + (
                                0 if content.endswith("\n") else 1
                            )
                            _ln = _vim_resolve_ex_address(_spec, _cur_line, _total)
                            cursor = _goto_line(content, _ln)
                        except ValueError as _e:
                            return f"ERROR: action {i} '{action}': :r range: {_e}\n"
            path_arg = arg.strip()
            if not path_arg:
                return f"ERROR: action {i} '{action}': :r needs a file path\n"
            if path_arg.startswith("!"):
                cmd = path_arg[1:].strip()
                if not cmd:
                    return f"ERROR: action {i} '{action}': :r ! needs a command\n"
                # #147: gate :r !cmd behind explicit opt-in.
                _vim_gate = _check_vim_shell_allowed()
                if _vim_gate is not None:
                    return f"ERROR: action {i} '{action}': {_vim_gate}"
                import subprocess as _sp
                try:
                    proc = _sp.run(
                        cmd, shell=True, capture_output=True, text=True, timeout=30
                    )
                except (OSError, _sp.TimeoutExpired) as e:
                    return f"ERROR: action {i} '{action}': :r !{cmd}: {e}\n"
                if proc.returncode != 0:
                    return (
                        f"ERROR: action {i} '{action}': :r !{cmd}: exit "
                        f"{proc.returncode}: {proc.stderr.strip()}\n"
                    )
                file_text = proc.stdout
            elif path_arg == "-":
                import sys as _sys
                file_text = _sys.stdin.read()
            else:
                # #146/#147: enforce cwd containment on :r FILE (without `!`).
                try:
                    _safe_path(path_arg)
                except SecurityError as _se:
                    return f"ERROR: action {i} '{action}': :r {path_arg!r}: {_se}\n"
                try:
                    with open(path_arg, "r", encoding="utf-8", errors="replace") as _fh:
                        file_text = _fh.read()
                except OSError as e:
                    return f"ERROR: action {i} '{action}': :r failed to read {path_arg!r}: {e}\n"
            # vim :r inserts AFTER the current line. Ensure a newline boundary.
            eol = _line_end(content, cursor)
            bol = _line_start(content, cursor)
            current_line = content[bol:eol]
            # Autocorrect: if cursor is on the LAST non-empty line of the
            # buffer AND that line is `}` alone (optional indent), insert
            # the snippet BEFORE the `}` instead of after — catches the
            # `G␞:r FILE` mistake that drops snippets outside the class.
            tail_after_eol = content[eol:].strip("\n \t")
            is_last_real_line = tail_after_eol == ""
            is_brace_line = current_line.strip() == "}"
            if is_last_real_line and is_brace_line:
                insert_pos = bol
            elif eol < len(content):
                # cursor is on a line followed by `\n`; insert after that `\n`
                insert_pos = eol + 1
            else:
                # cursor on last line with no trailing `\n` — add one
                if content and not content.endswith("\n"):
                    content += "\n"
                insert_pos = len(content)
            if file_text and not file_text.endswith("\n"):
                file_text += "\n"
            content = content[:insert_pos] + file_text + content[insert_pos:]
            cursor = insert_pos
            log.append(f"  {i}. :r {path_arg!r} ({len(file_text)} chars inserted)")

        # --- ex shell filter: :!cmd, :%!cmd, :N!cmd, :N,M!cmd ---
        # WARNING: cmd runs with the same OS privileges as supertool.
        # arg encoding: \x1d{range_spec}\x1d{cmd}
        #   range_spec = ""   -> bare :!cmd (insert stdout after cursor line)
        #   range_spec = "%"  -> :%!cmd (pipe whole buffer through cmd, replace buffer)
        #   range_spec = "N" or "N,M" -> pipe those lines, replace with stdout
        elif verb == ":!":
            if not arg.startswith("\x1d"):
                return f"ERROR: action {i} '{action}': :! malformed encoding\n"
            close = arg.find("\x1d", 1)
            if close == -1:
                return f"ERROR: action {i} '{action}': :! malformed encoding\n"
            range_spec = arg[1:close]
            cmd = arg[close + 1:]
            if not cmd.strip():
                return f"ERROR: action {i} '{action}': :! needs a command\n"
            _lines = content.split("\n")
            _has_trailing_nl = _lines and _lines[-1] == ""
            _total_lines = len(_lines) - (1 if _has_trailing_nl else 0)
            _cursor_line, _ = _offset_to_line_col(content, cursor)
            # #147: gate :!cmd / :%!cmd / :N!cmd behind explicit opt-in.
            _vim_gate = _check_vim_shell_allowed()
            if _vim_gate is not None:
                return f"ERROR: action {i} '{action}': {_vim_gate}"
            if range_spec == "":
                # bare :!cmd — run command, insert stdout after cursor line
                try:
                    proc = subprocess.run(
                        cmd, shell=True, capture_output=True, text=True, timeout=30
                    )
                except (OSError, subprocess.TimeoutExpired) as e:
                    return f"ERROR: action {i} '{action}': :!{cmd}: {e}\n"
                if proc.returncode != 0:
                    return (
                        f"ERROR: action {i} '{action}': :!{cmd}: exit "
                        f"{proc.returncode}: {proc.stderr.strip()}\n"
                    )
                out = proc.stdout
                if out and not out.endswith("\n"):
                    out += "\n"
                _push_undo()
                eol = _line_end(content, cursor)
                if eol < len(content):
                    insert_pos = eol + 1
                else:
                    if content and not content.endswith("\n"):
                        content += "\n"
                    insert_pos = len(content)
                content = content[:insert_pos] + out + content[insert_pos:]
                cursor = insert_pos
                last_change = {"verb": ":!", "count": count, "arg": arg}
                log.append(f"  {i}. :!{cmd} ({len(out)} chars inserted) {mark('⚠')} SHELL EXECUTION (cmd ran with shell=True, no sanitization)")
            else:
                # ranged :N!cmd / :%!cmd — pipe selected lines, replace with stdout
                def _vim_resolve_ex(addr: str) -> int:
                    return _vim_resolve_ex_address(addr, _cursor_line, _total_lines)
                if range_spec == "%":
                    line_a, line_b = 1, _total_lines
                elif "," in range_spec:
                    a_part, b_part = range_spec.split(",", 1)
                    try:
                        line_a = _vim_resolve_ex(a_part)
                        line_b = _vim_resolve_ex(b_part)
                    except ValueError as e:
                        return f"ERROR: action {i} '{action}': :! range: {e}\n"
                else:
                    try:
                        line_a = line_b = _vim_resolve_ex(range_spec)
                    except ValueError as e:
                        return f"ERROR: action {i} '{action}': :! range: {e}\n"
                if line_a < 1 or line_b < 1 or line_a > _total_lines or line_b > _total_lines:
                    return (
                        f"ERROR: action {i} '{action}': :! range {line_a}..{line_b} "
                        f"out of bounds (1..{_total_lines})\n"
                    )
                if line_a > line_b:
                    return (
                        f"ERROR: action {i} '{action}': :! range start ({line_a}) "
                        f"is after end ({line_b})\n"
                    )
                _line_starts: List[int] = [0]
                for _k, _ch in enumerate(content):
                    if _ch == "\n":
                        _line_starts.append(_k + 1)
                slice_start = _line_starts[line_a - 1]
                slice_end = _line_starts[line_b] if line_b < len(_line_starts) else len(content)
                region = content[slice_start:slice_end]
                try:
                    proc = subprocess.run(
                        cmd, shell=True, input=region,
                        capture_output=True, text=True, timeout=30
                    )
                except (OSError, subprocess.TimeoutExpired) as e:
                    return f"ERROR: action {i} '{action}': :!{cmd}: {e}\n"
                if proc.returncode != 0:
                    return (
                        f"ERROR: action {i} '{action}': :!{cmd}: exit "
                        f"{proc.returncode}: {proc.stderr.strip()}\n"
                    )
                out = proc.stdout
                if out and not out.endswith("\n"):
                    out += "\n"
                _push_undo()
                content = content[:slice_start] + out + content[slice_end:]
                cursor = slice_start
                last_change = {"verb": ":!", "count": count, "arg": arg}
                log.append(
                    f"  {i}. :{range_spec}!{cmd} "
                    f"(replaced {line_b - line_a + 1} lines -> {out.count(chr(10))} lines)"
                    f" {mark('⚠')} SHELL EXECUTION (cmd ran with shell=True, no sanitization)"
                )

        # --- ex delete: :%d, :Nd, :N,Md, :.d, :$d, :.,$d, :g/PAT/d, :v/PAT/d ---
        elif verb == ":d":
            _push_undo()
            # arg is always sentinel-encoded by the parser:
            #   \x1d{range_spec}\x1d{trailing}   (range_spec is %, N, ., $, N,M, etc.)
            #   \x1d{g|v}:{PAT}\x1d{trailing}    (global/inverse-global delete)
            if not arg.startswith("\x1d"):
                return f"ERROR: action {i} '{action}': :d malformed encoding\n"
            close = arg.find("\x1d", 1)
            if close == -1:
                return f"ERROR: action {i} '{action}': :d malformed encoding\n"
            spec = arg[1:close]
            # trailing chars after the encoded :d are not used today but kept for forward-compat.
            lines = content.split("\n")
            has_trailing_nl = lines and lines[-1] == ""
            total_lines = len(lines) - (1 if has_trailing_nl else 0)
            if total_lines == 0:
                return f"ERROR: action {i} '{action}': :d on empty buffer\n"

            # --- pattern mode: :g/PAT/d  or  :v/PAT/d ---
            if spec.startswith("g:") or spec.startswith("v:"):
                mode = spec[0]
                pat = spec[2:]
                if not pat:
                    return f"ERROR: action {i} '{action}': :{mode}/PAT/d needs non-empty PAT\n"
                try:
                    rx = re.compile(pat)
                except re.error as e:
                    return f"ERROR: action {i} '{action}': :{mode}/PAT/d regex: {e}\n"
                body_lines = lines[:-1] if has_trailing_nl else lines
                if mode == "g":
                    kept = [ln for ln in body_lines if rx.search(ln) is None]
                    n_deleted = len(body_lines) - len(kept)
                else:  # 'v'
                    kept = [ln for ln in body_lines if rx.search(ln) is not None]
                    n_deleted = len(body_lines) - len(kept)
                if n_deleted == 0:
                    return f"ERROR: action {i} '{action}': :{mode}/{pat}/d no lines matched\n"
                new_content = "\n".join(kept)
                if kept and has_trailing_nl:
                    new_content += "\n"
                elif not kept:
                    new_content = ""
                content = new_content
                cursor = min(cursor, len(content))
                log.append(f"  {i}. :{mode}/{pat!r}/d ({n_deleted} lines deleted)")
                continue

            # --- line-range mode ---
            cursor_line, _ = _offset_to_line_col(content, cursor)

            body_lines_for_pat = lines[:-1] if has_trailing_nl else lines

            def _resolve_d(addr: str) -> int:
                # Pattern address `/PAT/` — line number of first match.
                # Search forward from cursor line (matches real vim).
                if addr.startswith("/") and addr.endswith("/") and len(addr) >= 2:
                    pat = addr[1:-1]
                    try:
                        rxp = re.compile(pat)
                    except re.error as e:
                        raise ValueError(f"bad pattern {addr!r}: {e}")
                    # Search from cursor_line (1-indexed) onward.
                    for ln_idx in range(cursor_line - 1, len(body_lines_for_pat)):
                        if rxp.search(body_lines_for_pat[ln_idx]):
                            return ln_idx + 1
                    # Wrap to start
                    for ln_idx in range(0, cursor_line - 1):
                        if rxp.search(body_lines_for_pat[ln_idx]):
                            return ln_idx + 1
                    raise ValueError(f"pattern not found: {addr!r}")
                return _vim_resolve_ex_address(addr, cursor_line, total_lines)

            if spec == "%":
                line_a, line_b = 1, total_lines
            else:
                if "," in spec:
                    a, b = spec.split(",", 1)
                else:
                    a, b = spec, spec
                try:
                    line_a = _resolve_d(a)
                    line_b = _resolve_d(b)
                except ValueError as e:
                    return f"ERROR: action {i} '{action}': :d range: {e}\n"
            if line_a < 1 or line_b < 1 or line_a > total_lines or line_b > total_lines:
                return (
                    f"ERROR: action {i} '{action}': :d range {line_a}..{line_b} "
                    f"out of bounds (1..{total_lines})\n"
                )
            if line_a > line_b:
                return (
                    f"ERROR: action {i} '{action}': :d range start ({line_a}) "
                    f"is after end ({line_b})\n"
                )
            # Compute byte slice for lines [line_a..line_b] (inclusive of trailing \n).
            line_starts: List[int] = [0]
            for k, ch in enumerate(content):
                if ch == "\n":
                    line_starts.append(k + 1)
            del_start = line_starts[line_a - 1]
            if line_b < len(line_starts):
                del_end = line_starts[line_b]  # exclusive: start of next line
            else:
                del_end = len(content)
            content = content[:del_start] + content[del_end:]
            cursor = min(del_start, len(content))
            log.append(f"  {i}. :{spec}d ({line_b - line_a + 1} lines deleted)")

        # --- Y: yank to EOL (alias for y$, real-vim default) ---
        elif verb == "Y":
            eol = _line_end(content, cursor)
            register = content[cursor:eol]
            register_linewise = False
            log.append(f"  {i}. Y ({len(register)} chars)")

        # --- gJ: join lines without inserting space ---
        elif verb == "gJ":
            joined = 0
            for _ in range(count):
                nl = content.find("\n", cursor)
                if nl == -1:
                    break
                # remove only the \n; preserve next line's leading whitespace
                content = content[:nl] + content[nl + 1:]
                cursor = nl
                joined += 1
            log.append(f"  {i}. {count}gJ (joined {joined})")

        # --- * / # : search for word under cursor with word boundaries ---
        elif verb in ("*", "#"):
            # find word at cursor
            if cursor >= len(content) or not (
                content[cursor].isalnum() or content[cursor] == "_"
            ):
                # try to find next word on line for *
                p = cursor
                eol = _line_end(content, p)
                while p < eol and not (content[p].isalnum() or content[p] == "_"):
                    p += 1
                if p >= eol:
                    return f"ERROR: action {i} '{action}': {verb} no word under cursor\n"
                cursor = p
            ws = cursor
            while ws > 0 and (content[ws - 1].isalnum() or content[ws - 1] == "_"):
                ws -= 1
            we = cursor
            while we < len(content) and (content[we].isalnum() or content[we] == "_"):
                we += 1
            word = content[ws:we]
            pat = r"\b" + re.escape(word) + r"\b"
            try:
                rx = re.compile(pat, re.MULTILINE)
            except re.error as e:
                return f"ERROR: action {i} '{action}': {verb} regex: {e}\n"
            if verb == "*":
                m = rx.search(content, we)
                if m is None:
                    return f"ERROR: action {i} '{action}': * no further match for {word!r}\n"
                cursor = m.start()
                last_search = (pat, "/")
            else:  # #
                last = None
                for m in rx.finditer(content[:ws]):
                    last = m
                if last is None:
                    return f"ERROR: action {i} '{action}': # no earlier match for {word!r}\n"
                cursor = last.start()
                last_search = (pat, "?")
            log.append(f"  {i}. {verb} ({word!r} → {cursor})")

        # --- ex range helpers shared by :sort/:reverse/:move/:copy/:norm ---
        elif verb in (":sort", ":reverse", ":move", ":copy", ":norm"):
            if not arg.startswith("\x1d"):
                return f"ERROR: action {i} '{action}': {verb} malformed range encoding\n"
            close = arg.find("\x1d", 1)
            if close == -1:
                return f"ERROR: action {i} '{action}': {verb} malformed range encoding\n"
            range_spec = arg[1:close]
            body = arg[close + 1:]

            lines = content.split("\n")
            has_trailing_nl = lines and lines[-1] == ""
            total_lines = len(lines) - (1 if has_trailing_nl else 0)
            if total_lines == 0:
                return f"ERROR: action {i} '{action}': {verb} on empty buffer\n"
            cursor_line, _ = _offset_to_line_col(content, cursor)

            def _resolve_addr(addr: str) -> int:
                return _vim_resolve_ex_address(addr, cursor_line, total_lines)

            if range_spec == "%" or range_spec == "":
                line_a, line_b = 1, total_lines
            elif "," in range_spec:
                a, b = range_spec.split(",", 1)
                try:
                    line_a = _resolve_addr(a)
                    line_b = _resolve_addr(b)
                except ValueError as e:
                    return f"ERROR: action {i} '{action}': {verb} range: {e}\n"
            else:
                try:
                    line_a = _resolve_addr(range_spec)
                    line_b = line_a
                except ValueError as e:
                    return f"ERROR: action {i} '{action}': {verb} range: {e}\n"
            if line_a < 1 or line_b < 1 or line_a > total_lines or line_b > total_lines:
                return (
                    f"ERROR: action {i} '{action}': {verb} range {line_a}..{line_b} "
                    f"out of bounds (1..{total_lines})\n"
                )
            if line_a > line_b:
                return (
                    f"ERROR: action {i} '{action}': {verb} range start ({line_a}) "
                    f"is after end ({line_b})\n"
                )

            body_lines = lines[:-1] if has_trailing_nl else lines[:]

            if verb == ":sort":
                _push_undo()
                # parse flags from body: !, u, n (whitespace-tolerant)
                flags = body.strip()
                reverse = "!" in flags
                unique = "u" in flags
                numeric = "n" in flags
                segment = body_lines[line_a - 1:line_b]

                def _numkey(s: str) -> tuple:
                    m = re.search(r"-?\d+", s)
                    if m:
                        return (0, int(m.group(0)), s)
                    return (1, 0, s)

                if numeric:
                    segment.sort(key=_numkey, reverse=reverse)
                else:
                    segment.sort(reverse=reverse)
                if unique:
                    seen: set = set()
                    deduped: List[str] = []
                    for ln in segment:
                        if ln not in seen:
                            seen.add(ln)
                            deduped.append(ln)
                    segment = deduped
                new_body = body_lines[:line_a - 1] + segment + body_lines[line_b:]
                content = "\n".join(new_body) + ("\n" if has_trailing_nl else "")
                cursor = min(cursor, len(content))
                log.append(f"  {i}. :{range_spec}sort{flags} ({len(segment)} lines)")

            elif verb == ":reverse":
                _push_undo()
                segment = body_lines[line_a - 1:line_b]
                segment.reverse()
                new_body = body_lines[:line_a - 1] + segment + body_lines[line_b:]
                content = "\n".join(new_body) + ("\n" if has_trailing_nl else "")
                cursor = min(cursor, len(content))
                log.append(f"  {i}. :{range_spec}reverse ({len(segment)} lines)")

            elif verb in (":move", ":copy"):
                _push_undo()
                target_str = body.strip()
                if not target_str:
                    return f"ERROR: action {i} '{action}': {verb} needs target line\n"
                try:
                    target = _resolve_addr(target_str)
                except ValueError as e:
                    return f"ERROR: action {i} '{action}': {verb}: {e}\n"
                # target 0 = before line 1; target N = after line N
                if target < 0 or target > total_lines:
                    return (
                        f"ERROR: action {i} '{action}': {verb} target {target} out of "
                        f"bounds (0..{total_lines})\n"
                    )
                segment = body_lines[line_a - 1:line_b]
                if verb == ":move":
                    # disallow moving into own range
                    if line_a - 1 <= target <= line_b:
                        return (
                            f"ERROR: action {i} '{action}': :move target {target} "
                            f"inside source range {line_a}..{line_b}\n"
                        )
                    remaining = body_lines[:line_a - 1] + body_lines[line_b:]
                    # adjust target if it was after the source
                    adj_target = target - len(segment) if target > line_b else target
                    new_body = remaining[:adj_target] + segment + remaining[adj_target:]
                else:  # :copy
                    new_body = body_lines[:target] + segment + body_lines[target:]
                content = "\n".join(new_body) + ("\n" if has_trailing_nl else "")
                cursor = min(cursor, len(content))
                log.append(
                    f"  {i}. :{range_spec}{verb[1:]} {target} ({len(segment)} lines)"
                )

            elif verb == ":norm":
                _push_undo()
                # run body as a vim script per line in range
                cmds = body
                if not cmds:
                    return f"ERROR: action {i} '{action}': :norm needs commands\n"
                # operate on a snapshot of body lines; re-split after each op
                # to keep line indexing sane if the user mutates lines.
                # For simplicity: apply per-line in order, rebuild content
                # after each. Use 1G<count of line>;cmds via direct execution
                # by spinning a small recursion on op_vim — but file-based.
                # Simpler: for each target line, write segment to a temp,
                # apply, read back. That changes write count. Cleaner: do
                # an in-process recursion via op_vim on the same path with
                # a goto-line + cmds.
                import tempfile as _tf
                # Persist current state first so the recursive op sees it.
                try:
                    _atomic_write(path, content)
                except OSError as e:
                    return f"ERROR: failed to write {path}: {e}\n"
                total_run = 0
                cur_line_iter = line_a
                # End line shrinks/grows? For canonical :norm, vim re-evaluates
                # the line index each iteration. We track end by line count delta.
                line_b_eff = line_b
                while cur_line_iter <= line_b_eff:
                    # Build a sub-script that goes to the target line, then runs cmds.
                    sub_script = f"{cur_line_iter}G\x1b{cmds}"
                    sub_out = op_vim(path, sub_script)
                    if sub_out.startswith("ERROR"):
                        return f"ERROR: action {i} '{action}': :norm at line {cur_line_iter}: {sub_out}"
                    total_run += 1
                    cur_line_iter += 1
                    # Refresh line count in case cmds added/removed lines.
                    with open(path, "r", encoding="utf-8", errors="replace") as _fh:
                        new_content = _fh.read()
                    new_lines_full = new_content.split("\n")
                    new_total = len(new_lines_full) - (1 if new_lines_full and new_lines_full[-1] == "" else 0)
                    line_b_eff = line_b + (new_total - total_lines)
                # Re-read final content for the main loop.
                with open(path, "r", encoding="utf-8", errors="replace") as _fh:
                    content = _fh.read()
                cursor = min(cursor, len(content))
                log.append(f"  {i}. :{range_spec}norm {cmds!r} ({total_run} lines)")

        # --- :retab N — convert leading tabs to N spaces ---
        elif verb == ":retab":
            _push_undo()
            width_str = arg.strip()
            try:
                width = int(width_str) if width_str else 4
            except ValueError:
                return f"ERROR: action {i} '{action}': :retab needs integer width\n"
            if width < 1:
                return f"ERROR: action {i} '{action}': :retab width must be >= 1\n"
            spaces = " " * width
            new_lines: List[str] = []
            for ln in content.split("\n"):
                # convert leading tabs (only) to spaces
                k = 0
                while k < len(ln) and ln[k] == "\t":
                    k += 1
                new_lines.append(spaces * k + ln[k:])
            content = "\n".join(new_lines)
            cursor = min(cursor, len(content))
            log.append(f"  {i}. :retab {width}")

        # --- change to end-of-line / BOL ---
        elif verb == "c$":
            _push_undo()
            eol = _line_end(content, cursor)
            text = _decode_escapes(arg)
            content = content[:cursor] + text + content[eol:]
            cursor += len(text)
            log.append(f"  {i}. c${text!r}")
        elif verb == "c0":
            _push_undo()
            bol = _line_start(content, cursor)
            text = _decode_escapes(arg)
            content = content[:bol] + text + content[cursor:]
            cursor = bol + len(text)
            log.append(f"  {i}. c0{text!r}")

        # --- delete to motion ---
        elif verb == "d$":
            eol = _line_end(content, cursor)
            register = content[cursor:eol]
            register_linewise = False
            content = content[:cursor] + content[eol:]
            log.append(f"  {i}. d$ ({len(register)} chars)")
        elif verb == "d0":
            bol = _line_start(content, cursor)
            register = content[bol:cursor]
            register_linewise = False
            content = content[:bol] + content[cursor:]
            cursor = bol
            log.append(f"  {i}. d0 ({len(register)} chars)")
        elif verb == "dw":
            we = cursor
            on_word = we < len(content) and (content[we].isalnum() or content[we] == "_")
            if on_word:
                while we < len(content) and (content[we].isalnum() or content[we] == "_"):
                    we += 1
            else:
                while we < len(content) and not (content[we].isalnum() or content[we] == "_") and content[we] != "\n":
                    we += 1
            while we < len(content) and content[we] in (" ", "\t"):
                we += 1
            register = content[cursor:we]
            register_linewise = False
            content = content[:cursor] + content[we:]
            last_change = {"verb": "dw", "count": count, "arg": ""}
            log.append(f"  {i}. dw ({len(register)} chars)")
        elif verb == "cw":
            # already exists above; keep — this branch is unreachable
            pass

        # --- yank ---
        elif verb == "yy":
            bol = _line_start(content, cursor)
            end = bol
            for _ in range(count):
                nl = content.find("\n", end)
                end = nl + 1 if nl != -1 else len(content)
                if end >= len(content):
                    break
            register = content[bol:end]
            register_linewise = True
            log.append(f"  {i}. {count}yy ({len(register)} chars, linewise)")
        elif verb == "yw":
            we = cursor
            on_word = we < len(content) and (content[we].isalnum() or content[we] == "_")
            if on_word:
                while we < len(content) and (content[we].isalnum() or content[we] == "_"):
                    we += 1
            register = content[cursor:we]
            register_linewise = False
            log.append(f"  {i}. yw ({len(register)} chars)")
        elif verb == "y$":
            eol = _line_end(content, cursor)
            register = content[cursor:eol]
            register_linewise = False
            log.append(f"  {i}. y$ ({len(register)} chars)")

        # --- operator-motion family: d/y/c + various motions ---
        # Also supports case operators g~/gu/gU which reuse the same motion
        # ranges but transform the slice in place (swapcase/lower/upper).
        elif (
            (len(verb) >= 2 and verb[0] in ("d", "y", "c") and verb[1:] in (
                "G", "gg", "^", "h", "j", "k", "l", "/", "?", "$", "0",
                "{", "}", "(", ")", "%", "+", "-", "_",
                "W", "B", "E", ";", ",",
                "ge", "gE", "g_",
            ))
            or (verb in ("g~", "gu", "gU") and arg[:1] in (
                "G", "^", "h", "j", "k", "l", "$", "0",
                "{", "}", "(", ")", "%", "+", "-", "_",
                "w", "b", "e",
                "W", "B", "E", ";", ",",
            ))
        ):
            _push_undo()
            if verb in ("g~", "gu", "gU"):
                op = verb
                motion = arg[:1]
                # consume the motion char from arg so any tail (unlikely) stays
                arg = arg[1:]
            else:
                op = verb[0]
                motion = verb[1:]
            linewise = False
            if motion == "G":
                # cursor's line BOL .. EOF (inclusive of trailing newline if any)
                start = _line_start(content, cursor)
                end = len(content)
                linewise = True
            elif motion == "gg":
                # BOF .. cursor's line end (inclusive of trailing newline)
                start = 0
                line_eol = _line_end(content, cursor)
                end = line_eol + 1 if line_eol < len(content) else len(content)
                linewise = True
            elif motion == "^":
                # cursor back to first non-blank of line
                bol = _line_start(content, cursor)
                eol = _line_end(content, cursor)
                first_nb = bol
                while first_nb < eol and content[first_nb] in (" ", "\t"):
                    first_nb += 1
                if first_nb <= cursor:
                    start, end = first_nb, cursor
                else:
                    # cursor already at/before first non-blank — empty motion
                    start, end = cursor, cursor
            elif motion == "h":
                start = max(0, cursor - 1)
                end = cursor
            elif motion == "l":
                start = cursor
                end = min(len(content), cursor + 1)
            elif motion == "$":
                # to last char of line (exclusive of trailing \n)
                start = cursor
                end = _line_end(content, cursor)
            elif motion == "0":
                # to BOL
                start = _line_start(content, cursor)
                end = cursor
            elif motion in ("w", "b", "e"):
                pos = cursor
                if motion == "w":
                    on_word = pos < len(content) and (content[pos].isalnum() or content[pos] == "_")
                    if on_word:
                        while pos < len(content) and (content[pos].isalnum() or content[pos] == "_"):
                            pos += 1
                    while pos < len(content) and content[pos] in (" ", "\t"):
                        pos += 1
                    start, end = cursor, pos
                elif motion == "b":
                    if pos > 0:
                        pos -= 1
                        while pos > 0 and content[pos] in (" ", "\t"):
                            pos -= 1
                        while pos > 0 and (content[pos - 1].isalnum() or content[pos - 1] == "_"):
                            pos -= 1
                    start, end = pos, cursor
                else:  # e — inclusive end of word
                    if pos < len(content) and (content[pos].isalnum() or content[pos] == "_") and (
                        pos + 1 >= len(content) or not (content[pos + 1].isalnum() or content[pos + 1] == "_")
                    ):
                        pos += 1
                    while pos < len(content) and not (content[pos].isalnum() or content[pos] == "_"):
                        pos += 1
                    while pos + 1 < len(content) and (content[pos + 1].isalnum() or content[pos + 1] == "_"):
                        pos += 1
                    start, end = cursor, pos + 1
            elif motion == "j":
                # current line BOL .. end of next line (inclusive of next \n)
                start = _line_start(content, cursor)
                first_nl = content.find("\n", cursor)
                if first_nl == -1:
                    end = len(content)
                else:
                    second_nl = content.find("\n", first_nl + 1)
                    end = second_nl + 1 if second_nl != -1 else len(content)
                linewise = True
            elif motion == "k":
                # previous line BOL .. end of current line (inclusive of \n)
                bol = _line_start(content, cursor)
                if bol == 0:
                    # no previous line — operate only on current line
                    prev_bol = 0
                else:
                    prev_bol = _line_start(content, bol - 1)
                cur_eol = _line_end(content, cursor)
                start = prev_bol
                end = cur_eol + 1 if cur_eol < len(content) else len(content)
                linewise = True
            elif motion == "/":
                if not arg:
                    return f"ERROR: action {i} '{action}': {verb} empty pattern\n"
                pat = arg
                idx = -1
                try:
                    rx = re.compile(pat, re.MULTILINE)
                    m = rx.search(content, cursor)
                    if m is not None and m.start() != m.end():
                        idx = m.start()
                except re.error:
                    pass
                if idx == -1:
                    idx = content.find(pat, cursor)
                if idx == -1:
                    return f"ERROR: action {i} '{action}': pattern not found forward\n"
                last_search = (pat, "/")
                start, end = cursor, idx
            elif motion in ("W", "B", "E"):
                # Compute end-of-motion position by simulating the standalone
                # motion. WORD = non-whitespace run.
                pos = cursor
                if motion == "W":
                    if pos < len(content):
                        while pos < len(content) and not content[pos].isspace():
                            pos += 1
                        while pos < len(content) and content[pos].isspace():
                            pos += 1
                    start, end = cursor, pos
                elif motion == "B":
                    if pos > 0:
                        pos -= 1
                        while pos > 0 and content[pos].isspace():
                            pos -= 1
                        while pos > 0 and not content[pos - 1].isspace():
                            pos -= 1
                    start, end = pos, cursor
                else:  # E — inclusive of WORD-end char
                    if (
                        pos + 1 < len(content)
                        and not content[pos].isspace()
                        and content[pos + 1].isspace()
                    ):
                        pos += 1
                    while pos < len(content) and content[pos].isspace():
                        pos += 1
                    while (
                        pos + 1 < len(content)
                        and not content[pos + 1].isspace()
                    ):
                        pos += 1
                    start, end = cursor, pos + 1  # inclusive
            elif motion in ("ge", "gE"):
                # back-to-word-end: deletes from char AFTER prev word-end up to
                # cursor exclusive (so trailing whitespace between WORDs is
                # removed but the cursor's char stays).
                pos = cursor
                if pos > 0:
                    pos -= 1
                    while pos > 0 and content[pos].isspace():
                        pos -= 1
                start, end = pos + 1, cursor
                if end < start:
                    start, end = end, start
            elif motion == "g_":
                # to last non-blank, inclusive
                bol = _line_start(content, cursor)
                eol = _line_end(content, cursor)
                pos = eol - 1
                while pos >= bol and content[pos] in (" ", "\t"):
                    pos -= 1
                last_nb = max(bol, pos)
                start, end = cursor, last_nb + 1
                if end < start:
                    start, end = end, start
            elif motion == "+":
                # linewise: current line through end of next line
                start = _line_start(content, cursor)
                first_nl = content.find("\n", cursor)
                if first_nl == -1:
                    end = len(content)
                else:
                    second_nl = content.find("\n", first_nl + 1)
                    end = second_nl + 1 if second_nl != -1 else len(content)
                linewise = True
            elif motion == "-":
                # linewise: prev line through end of current line
                bol = _line_start(content, cursor)
                if bol == 0:
                    prev_bol = 0
                else:
                    prev_bol = _line_start(content, bol - 1)
                cur_eol = _line_end(content, cursor)
                start = prev_bol
                end = cur_eol + 1 if cur_eol < len(content) else len(content)
                linewise = True
            elif motion == "_":
                # linewise: current line only (with count: count lines)
                bol = _line_start(content, cursor)
                e2 = bol
                for _ in range(count):
                    nl = content.find("\n", e2)
                    if nl == -1:
                        e2 = len(content)
                        break
                    e2 = nl + 1
                start, end = bol, e2
                linewise = True
            elif motion == "{":
                # back to prev blank line — exclusive of cursor
                pos = cursor
                cbol = _line_start(content, pos)
                ceol = _line_end(content, pos)
                if cbol == ceol and cbol > 0:
                    pos = cbol - 1
                else:
                    pos = cbol
                while pos > 0:
                    prev_bol = _line_start(content, pos - 1)
                    prev_eol = _line_end(content, prev_bol)
                    if prev_bol == prev_eol:
                        pos = prev_bol
                        break
                    pos = prev_bol
                else:
                    pos = 0
                start, end = pos, cursor
            elif motion == "}":
                # forward to next blank line — exclusive
                pos = cursor
                cbol = _line_start(content, pos)
                ceol = _line_end(content, pos)
                if cbol == ceol:
                    pos = ceol + 1 if ceol < len(content) else len(content)
                while pos < len(content):
                    nl = content.find("\n", pos)
                    if nl == -1:
                        pos = len(content)
                        break
                    next_bol = nl + 1
                    next_eol = content.find("\n", next_bol)
                    if next_eol == -1:
                        next_eol = len(content)
                    if next_bol == next_eol:
                        pos = next_bol
                        break
                    pos = next_bol
                start, end = cursor, pos
            elif motion == "(":
                pos = cursor
                if pos > 0:
                    pos -= 1
                found = 0
                while pos > 0:
                    ch = content[pos]
                    if ch in ".!?" and pos + 1 < len(content) and content[pos + 1] in (" ", "\t", "\n"):
                        k = pos + 1
                        while k < len(content) and content[k] in (" ", "\t", "\n"):
                            k += 1
                        found = k
                        break
                    pos -= 1
                start, end = found, cursor
            elif motion == ")":
                pos = cursor
                while pos < len(content):
                    ch = content[pos]
                    if ch in ".!?":
                        k = pos + 1
                        if k >= len(content):
                            pos = len(content)
                            break
                        if content[k] in (" ", "\t", "\n"):
                            k += 1
                            while k < len(content) and content[k] in (" ", "\t", "\n"):
                                k += 1
                            pos = k
                            break
                    pos += 1
                start, end = cursor, pos
            elif motion == "%":
                if cursor >= len(content):
                    return f"ERROR: action {i} '{action}': % at EOF\n"
                pairs_fwd = {"(": ")", "[": "]", "{": "}"}
                pairs_bwd = {")": "(", "]": "[", "}": "{"}
                ch = content[cursor]
                if ch in pairs_fwd:
                    opener, closer = ch, pairs_fwd[ch]
                    depth = 1
                    k = cursor + 1
                    target = -1
                    while k < len(content):
                        if content[k] == opener:
                            depth += 1
                        elif content[k] == closer:
                            depth -= 1
                            if depth == 0:
                                target = k
                                break
                        k += 1
                    if target == -1:
                        return f"ERROR: action {i} '{action}': % no matching {closer!r}\n"
                    start, end = cursor, target + 1  # inclusive of closer
                elif ch in pairs_bwd:
                    opener, closer = pairs_bwd[ch], ch
                    depth = 1
                    k = cursor - 1
                    target = -1
                    while k >= 0:
                        if content[k] == closer:
                            depth += 1
                        elif content[k] == opener:
                            depth -= 1
                            if depth == 0:
                                target = k
                                break
                        k -= 1
                    if target == -1:
                        return f"ERROR: action {i} '{action}': % no matching {opener!r}\n"
                    start, end = target, cursor + 1  # inclusive of cursor's bracket
                else:
                    return f"ERROR: action {i} '{action}': % not on bracket\n"
            elif motion in (";", ","):
                if last_find is None:
                    return f"ERROR: action {i} '{action}': no previous f/F/t/T to repeat\n"
                fverb, ftarget = last_find
                if motion == ",":
                    reverse_map = {"f": "F", "F": "f", "t": "T", "T": "t"}
                    fverb = reverse_map[fverb]
                bol = _line_start(content, cursor)
                eol = _line_end(content, cursor)
                if fverb == "f":
                    hit = content.find(ftarget, cursor + 1, eol)
                    if hit == -1:
                        return f"ERROR: action {i} '{action}': {motion} no match\n"
                    start, end = cursor, hit + 1  # inclusive
                elif fverb == "F":
                    hit = content.rfind(ftarget, bol, cursor)
                    if hit == -1:
                        return f"ERROR: action {i} '{action}': {motion} no match\n"
                    start, end = hit, cursor
                elif fverb == "t":
                    hit = content.find(ftarget, cursor + 1, eol)
                    if hit == -1:
                        return f"ERROR: action {i} '{action}': {motion} no match\n"
                    start, end = cursor, hit  # up to but not including target
                else:  # T
                    hit = content.rfind(ftarget, bol, cursor)
                    if hit == -1:
                        return f"ERROR: action {i} '{action}': {motion} no match\n"
                    start, end = hit + 1, cursor
            elif motion == "?":
                if not arg:
                    return f"ERROR: action {i} '{action}': {verb} empty pattern\n"
                pat = arg
                idx = -1
                try:
                    rx = re.compile(pat, re.MULTILINE)
                    last = None
                    for m in rx.finditer(content[:cursor]):
                        if m.start() != m.end():
                            last = m
                    if last is not None:
                        idx = last.start()
                except re.error:
                    pass
                if idx == -1:
                    idx = content.rfind(pat, 0, cursor)
                if idx == -1:
                    return f"ERROR: action {i} '{action}': pattern not found backward\n"
                last_search = (pat, "?")
                start, end = idx, cursor

            slice_ = content[start:end]
            if op in ("g~", "gu", "gU"):
                if op == "g~":
                    new = slice_.swapcase()
                elif op == "gu":
                    new = slice_.lower()
                else:
                    new = slice_.upper()
                content = content[:start] + new + content[end:]
                # cursor stays at start (vim parity)
                cursor = start
            else:
                register = slice_
                register_linewise = linewise
                if op == "d":
                    content = content[:start] + content[end:]
                    cursor = min(start, len(content))
                elif op == "c":
                    # change: delete + insert TEXT from arg. For pattern-based motions
                    # the arg already held the pattern (consumed above), so don't
                    # re-insert in that case.
                    if motion in ("/", "?"):
                        text = ""
                    else:
                        text = _decode_escapes(arg) if arg else ""
                    content = content[:start] + text + content[end:]
                    cursor = start + len(text)
            log.append(f"  {i}. {verb} ({len(slice_)} chars{', linewise' if linewise else ''})")

        # --- tilde toggle-case (N~) ---
        elif verb == "~":
            _push_undo()
            end_pos = min(len(content), cursor + count)
            seg = content[cursor:end_pos]
            content = content[:cursor] + seg.swapcase() + content[end_pos:]
            cursor = end_pos
            log.append(f"  {i}. {count}~ ({len(seg)} chars toggled)")

        # --- linewise case verbs: g~~ guu gUU (N lines) ---
        elif verb in ("g~~", "guu", "gUU"):
            _push_undo()
            bol = _line_start(content, cursor)
            end_pos = bol
            for _ in range(count):
                nl = content.find("\n", end_pos)
                if nl == -1:
                    end_pos = len(content)
                    break
                end_pos = nl + 1
            # operate on the lines but preserve the trailing \n boundary
            seg = content[bol:end_pos]
            if seg.endswith("\n"):
                body, tail = seg[:-1], "\n"
            else:
                body, tail = seg, ""
            if verb == "g~~":
                new = body.swapcase()
            elif verb == "guu":
                new = body.lower()
            else:
                new = body.upper()
            content = content[:bol] + new + tail + content[end_pos:]
            cursor = bol
            log.append(f"  {i}. {verb} ({len(body)} chars)")

        # --- Ctrl-A / Ctrl-X: increment / decrement number ---
        elif verb in ("\x01", "\x18"):
            _push_undo()
            # find digit run: current pos if on digit, else scan forward on
            # current line for first digit. Handles leading minus.
            eol = _line_end(content, cursor)
            p = cursor
            if p >= eol or not content[p].isdigit():
                while p < eol and not content[p].isdigit():
                    p += 1
            if p >= eol or not content[p].isdigit():
                return f"ERROR: action {i} '{action}': no number on line\n"
            # walk back to leading minus if adjacent (vim treats -42 as -42)
            start_d = p
            while start_d > 0 and content[start_d - 1].isdigit():
                start_d -= 1
            end_d = p
            while end_d < eol and content[end_d].isdigit():
                end_d += 1
            # optional leading minus
            if start_d > 0 and content[start_d - 1] == "-":
                start_d -= 1
            num_str = content[start_d:end_d]
            try:
                value = int(num_str)
            except ValueError:
                return f"ERROR: action {i} '{action}': bad number {num_str!r}\n"
            delta = count if verb == "\x01" else -count
            new_val = value + delta
            new_str = str(new_val)
            content = content[:start_d] + new_str + content[end_d:]
            cursor = start_d + len(new_str) - 1
            op_label = 'C-a' if verb == '\x01' else 'C-x'
            log.append(f"  {i}. {op_label} {num_str} → {new_str}")

        # --- paste ---
        elif verb == "p":
            _push_undo()
            if not register:
                return f"ERROR: action {i} '{action}': p with empty register\n"
            if register_linewise:
                eol = _line_end(content, cursor)
                # paste a full line after current line (after the \n)
                pos = eol + 1 if eol < len(content) else len(content)
                content = content[:pos] + register + content[pos:]
                cursor = pos
            else:
                pos = min(len(content), cursor + 1)
                content = content[:pos] + register + content[pos:]
                cursor = pos + len(register) - 1 if register else pos
            log.append(f"  {i}. p ({len(register)} chars)")
            last_change = {"verb": "p", "count": 1, "arg": ""}
        elif verb == "P":
            _push_undo()
            if not register:
                return f"ERROR: action {i} '{action}': P with empty register\n"
            if register_linewise:
                bol = _line_start(content, cursor)
                content = content[:bol] + register + content[bol:]
                cursor = bol
            else:
                content = content[:cursor] + register + content[cursor:]
                cursor = cursor + len(register) - 1 if register else cursor
            log.append(f"  {i}. P ({len(register)} chars)")

        # --- c/d/y + char-find motion ---
        elif len(verb) == 2 and verb[0] in ("c", "d", "y") and verb[1] in ("f", "F", "t", "T"):
            _push_undo()
            if not arg:
                return f"ERROR: action {i} '{action}': {verb} needs target char\n"
            target = arg[0]
            text = _decode_escapes(arg[1:]) if verb[0] == "c" else ""
            bol = _line_start(content, cursor)
            eol = _line_end(content, cursor)
            if verb[1] == "f":
                hit = content.find(target, cursor, eol)
                if hit == -1:
                    return f"ERROR: action {i} '{action}': {verb} target {target!r} not found\n"
                start, end = cursor, hit + 1
            elif verb[1] == "F":
                hit = content.rfind(target, bol, cursor)
                if hit == -1:
                    return f"ERROR: action {i} '{action}': {verb} target {target!r} not found\n"
                start, end = hit, cursor
            elif verb[1] == "t":
                hit = content.find(target, cursor, eol)
                if hit == -1:
                    return f"ERROR: action {i} '{action}': {verb} target {target!r} not found\n"
                start, end = cursor, hit
            else:  # T
                hit = content.rfind(target, bol, cursor)
                if hit == -1:
                    return f"ERROR: action {i} '{action}': {verb} target {target!r} not found\n"
                start, end = hit + 1, cursor
            slice_ = content[start:end]
            if verb[0] == "y":
                register = slice_
                register_linewise = False
                log.append(f"  {i}. {verb}{target!r} (yanked {len(slice_)} chars)")
            else:
                # c or d: delete the slice, c also inserts text
                register = slice_
                register_linewise = False
                content = content[:start] + text + content[end:]
                cursor = start + len(text)
                log.append(f"  {i}. {verb}{target!r} ({len(slice_)} chars → {len(text)})")

        # --- indent operators: >> << == and >{motion} <{motion} ={motion} ---
        elif verb in (">>", "<<", "==") or (
            len(verb) >= 2 and verb[0] in "><=" and verb not in (">>", "<<", "==")
        ):
            _push_undo()
            op = verb[0]
            # Determine the [line_a, line_b] line range (1-indexed inclusive)
            cur_line, _ = _offset_to_line_col(content, cursor)
            total_lines = content.count("\n") + 1
            # indent_repeat: how many indent levels to apply per line.
            # For >> (count expands line range), always 1.
            # For >motion (count repeats the op), equals outer count.
            indent_repeat = 1
            if verb in (">>", "<<", "=="):
                line_a = cur_line
                line_b = min(total_lines, cur_line + count - 1)
            else:
                indent_repeat = count
                motion = verb[1:]
                target = cursor
                if len(motion) == 2 and motion[0] in "ia" and motion[1] in 'wWsp"\'`()[]{}<>bBt':
                    try:
                        ts, te = _resolve_text_object(content, cursor, motion[1], motion[0] == "a")
                        # Convert to line range covering [ts, te-1]
                        la, _ = _offset_to_line_col(content, ts)
                        lb, _ = _offset_to_line_col(content, max(ts, te - 1))
                        line_a, line_b = la, lb
                    except _TextObjectError as e:
                        return f"ERROR: action {i} '{action}': {e}\n"
                else:
                    # Compute motion endpoint
                    if motion == "G":
                        target = len(content) - (1 if content.endswith("\n") else 0)
                    elif motion == "gg":
                        target = 0
                    elif motion == "j":
                        # arg encodes motion_count (lines to move); outer
                        # count repeats the indent operation on that range.
                        _mc_str = arg.lstrip()
                        _mc = int(_mc_str) if _mc_str.isdigit() else 1
                        pos = cursor
                        for _ in range(_mc):
                            nl = content.find("\n", pos)
                            if nl == -1:
                                break
                            pos = nl + 1
                        target = pos
                    elif motion == "k":
                        _mc_str = arg.lstrip()
                        _mc = int(_mc_str) if _mc_str.isdigit() else 1
                        bol = _line_start(content, cursor)
                        pos = bol
                        for _ in range(_mc):
                            if pos == 0:
                                break
                            pos = _line_start(content, pos - 1)
                        target = pos
                    elif motion == "}":
                        pos = cursor
                        bol = _line_start(content, pos)
                        eol = _line_end(content, pos)
                        if bol == eol:
                            pos = eol + 1 if eol < len(content) else len(content)
                        while pos < len(content):
                            nl = content.find("\n", pos)
                            if nl == -1:
                                pos = len(content)
                                break
                            nb = nl + 1
                            ne = content.find("\n", nb)
                            if ne == -1:
                                ne = len(content)
                            if nb == ne:
                                pos = nb
                                break
                            pos = nb
                        target = pos
                    elif motion == "{":
                        pos = cursor
                        bol = _line_start(content, pos)
                        eol = _line_end(content, pos)
                        if bol == eol and bol > 0:
                            pos = bol - 1
                        else:
                            pos = bol
                        while pos > 0:
                            prev_bol = _line_start(content, pos - 1)
                            prev_eol = _line_end(content, prev_bol)
                            if prev_bol == prev_eol:
                                pos = prev_bol
                                break
                            pos = prev_bol
                        target = pos
                    elif motion == "%":
                        # match bracket
                        if cursor < len(content):
                            pairs_fwd = {"(": ")", "[": "]", "{": "}"}
                            pairs_bwd = {")": "(", "]": "[", "}": "{"}
                            ch = content[cursor]
                            if ch in pairs_fwd:
                                depth = 1
                                k = cursor + 1
                                while k < len(content):
                                    if content[k] == ch:
                                        depth += 1
                                    elif content[k] == pairs_fwd[ch]:
                                        depth -= 1
                                        if depth == 0:
                                            target = k
                                            break
                                    k += 1
                            elif ch in pairs_bwd:
                                depth = 1
                                k = cursor - 1
                                while k >= 0:
                                    if content[k] == ch:
                                        depth += 1
                                    elif content[k] == pairs_bwd[ch]:
                                        depth -= 1
                                        if depth == 0:
                                            target = k
                                            break
                                    k -= 1
                    elif motion in ("+", "-"):
                        if motion == "+":
                            nl = content.find("\n", cursor)
                            target = (nl + 1) if nl != -1 else cursor
                        else:
                            bol = _line_start(content, cursor)
                            target = _line_start(content, bol - 1) if bol > 0 else cursor
                    else:
                        # default: use cursor (no-op range = current line)
                        target = cursor
                    la, _ = _offset_to_line_col(content, min(cursor, target))
                    lb, _ = _offset_to_line_col(content, max(cursor, target))
                    line_a, line_b = la, lb
            # Apply indent/dedent to lines [line_a, line_b]
            line_a = max(1, line_a)
            line_b = min(total_lines, line_b)
            if line_a > line_b:
                line_a, line_b = line_b, line_a
            # Build new content by line
            lines = content.split("\n")
            # Trailing empty string from final \n — preserve it
            trailing_empty = lines and lines[-1] == ""
            real_lines = lines[:-1] if trailing_empty else lines
            shift = "    "
            for ln_idx in range(line_a - 1, min(line_b, len(real_lines))):
                if op == ">":
                    # Vim: skip empty lines for indent
                    if real_lines[ln_idx] == "":
                        continue
                    # indent_repeat: 1 for >> (count = line range), outer count for >motion
                    real_lines[ln_idx] = shift * indent_repeat + real_lines[ln_idx]
                elif op == "=":
                    # Re-indent: match indent depth of nearest preceding non-blank
                    # line, but emit using the TARGET line's indent style (tabs or
                    # spaces) to avoid mangling mixed-indent files.
                    ref_depth = 0  # indent depth in "levels" (1 level = 4 spaces)
                    for ref_idx in range(ln_idx - 1, -1, -1):
                        ref = real_lines[ref_idx]
                        if ref.strip():
                            raw_indent = ref[: len(ref) - len(ref.lstrip(" \t"))]
                            if "\t" in raw_indent:
                                ref_depth = raw_indent.count("\t")
                            else:
                                ref_depth = len(raw_indent) // 4
                            break
                    # Detect target line's indent style: tabs win if any tab present
                    target_raw = real_lines[ln_idx]
                    target_prefix = target_raw[: len(target_raw) - len(target_raw.lstrip(" \t"))]
                    if "\t" in target_prefix:
                        new_indent = "\t" * ref_depth
                    else:
                        new_indent = "    " * ref_depth
                    new_line = new_indent + target_raw.lstrip(" \t")
                    if new_line == target_raw:
                        continue  # already correct — avoid spurious diff
                    real_lines[ln_idx] = new_line
                else:
                    s = real_lines[ln_idx]
                    if s.startswith("\t"):
                        real_lines[ln_idx] = s[1:]
                    else:
                        # strip up to 4 leading spaces
                        k = 0
                        while k < 4 and k < len(s) and s[k] == " ":
                            k += 1
                        real_lines[ln_idx] = s[k:]
            new_lines2 = real_lines + ([""] if trailing_empty else [])
            content = "\n".join(new_lines2)
            # Cursor → first non-blank of line_a
            try:
                bol = _goto_line(content, line_a)
                eol = _line_end(content, bol)
                pos = bol
                while pos < eol and content[pos] in (" ", "\t"):
                    pos += 1
                cursor = pos
            except ValueError:
                cursor = min(cursor, len(content))
            last_edit = cursor
            log.append(f"  {i}. {verb} (lines {line_a}..{line_b})")

        # --- R — overwrite mode ---
        elif verb == "R":
            _push_undo()
            text = _decode_escapes(arg)
            # Overwrite char-by-char within the current line; append past EOL.
            eol = _line_end(content, cursor)
            line_chars_avail = eol - cursor
            n_overwrite = min(len(text), line_chars_avail)
            n_append = len(text) - n_overwrite
            new_content = (
                content[:cursor]
                + text[:n_overwrite]
                + text[n_overwrite:n_overwrite + n_append]
                + content[cursor + n_overwrite:]
            )
            content = new_content
            cursor = cursor + len(text)
            last_edit = cursor
            preview = text if len(text) <= 30 else text[:27] + "..."
            log.append(f"  {i}. R{preview!r} (len={len(text)})")

        # --- m{X} — set mark ---
        elif len(verb) == 2 and verb[0] == "m" and (
            ("a" <= verb[1] <= "z") or ("A" <= verb[1] <= "Z")
        ):
            mark_char = verb[1].lower()  # uppercase same as lowercase for our scope
            marks[mark_char] = cursor
            log.append(f"  {i}. m{verb[1]} (mark={cursor})")

        # --- `{X} — jump to mark exact offset, `` — jump to prev cursor ---
        elif len(verb) == 2 and verb[0] == "`":
            target_ch = verb[1]
            if target_ch == "`":
                cursor, prev_cursor = prev_cursor, cursor
                log.append(f"  {i}. `` (cursor={cursor})")
            else:
                key = target_ch.lower()
                if key not in marks:
                    return f"ERROR: action {i} '{action}': mark {target_ch!r} not set\n"
                prev_cursor = cursor
                cursor = min(marks[key], len(content))
                log.append(f"  {i}. `{target_ch} (cursor={cursor})")

        # --- '{X} — jump to mark line (first non-blank), '' — prev cursor ---
        elif len(verb) == 2 and verb[0] == "'":
            target_ch = verb[1]
            if target_ch == "'":
                cursor, prev_cursor = prev_cursor, cursor
                log.append(f"  {i}. '' (cursor={cursor})")
            else:
                key = target_ch.lower()
                if key not in marks:
                    return f"ERROR: action {i} '{action}': mark {target_ch!r} not set\n"
                prev_cursor = cursor
                off = min(marks[key], len(content))
                bol = _line_start(content, off)
                eol = _line_end(content, bol)
                pos = bol
                while pos < eol and content[pos] in (" ", "\t"):
                    pos += 1
                cursor = pos
                log.append(f"  {i}. '{target_ch} (cursor={cursor})")


        # --- . — repeat last change ---
        # Replays the last buffer-mutating action at the current cursor
        # position. Scoped to: i a I A o O, cc cw ciw, dd dw, x, p.
        # Verbs outside this set are silently noted in the log.
        elif verb == ".":
            if last_change is None:
                log.append(f"  {i}. . (nothing to repeat)")
            else:
                lc_verb = last_change["verb"]
                lc_count = last_change["count"]
                lc_arg = last_change["arg"]
                _DOT_SUPPORTED = frozenset([
                    "i", "a", "I", "A", "o", "O",
                    "cc", "cw", "ciw",
                    "dd", "dw",
                    "x",
                    "p",
                    ":!",
                ])
                if lc_verb not in _DOT_SUPPORTED:
                    log.append(f"  {i}. . (repeat {lc_verb!r} not supported — skipped)")
                else:
                    if lc_verb in ("i", "a", "I", "A", "o", "O"):
                        _text = _decode_escapes(lc_arg) * lc_count
                        if lc_verb == "i":
                            _pos = cursor
                        elif lc_verb == "a":
                            _pos = min(len(content), cursor + 1)
                        elif lc_verb == "I":
                            _pos = _line_start(content, cursor)
                        elif lc_verb == "A":
                            _pos = _line_end(content, cursor)
                        elif lc_verb == "o":
                            _eol = _line_end(content, cursor)
                            content = content[:_eol] + "\n" + content[_eol:]
                            _pos = _eol + 1
                        else:  # O
                            _bol = _line_start(content, cursor)
                            content = content[:_bol] + "\n" + content[_bol:]
                            _pos = _bol
                        content = content[:_pos] + _text + content[_pos:]
                        _delta = len(_text)
                        if _delta:
                            for _mk in list(marks.keys()):
                                if marks[_mk] >= _pos:
                                    marks[_mk] += _delta
                            if last_edit is not None and last_edit >= _pos:
                                last_edit += _delta
                        cursor = _pos + _delta
                        last_edit = cursor
                        _preview = _text if len(_text) <= 30 else _text[:27] + "..."
                        log.append(f"  {i}. .({lc_verb}{_preview!r}) (len={len(_text)})")
                    elif lc_verb == "x":
                        _end = min(len(content), cursor + lc_count)
                        content = content[:cursor] + content[_end:]
                        log.append(f"  {i}. .({lc_count}x) ({_end - cursor} chars)")
                    elif lc_verb == "dd":
                        _bol = _line_start(content, cursor)
                        _end = _bol
                        for _ in range(lc_count):
                            _nl = content.find("\n", _end)
                            _end = _nl + 1 if _nl != -1 else len(content)
                            if _end >= len(content):
                                break
                        content = content[:_bol] + content[_end:]
                        cursor = _bol if _bol < len(content) else max(0, len(content))
                        log.append(f"  {i}. .({lc_count}dd) (cursor={cursor})")
                    elif lc_verb == "dw":
                        if cursor < len(content):
                            def _is_wdot(ch: str) -> bool:
                                return ch.isalnum() or ch == "_"
                            _we = cursor
                            if _is_wdot(content[_we]):
                                while _we < len(content) and _is_wdot(content[_we]):
                                    _we += 1
                            else:
                                while _we < len(content) and not _is_wdot(content[_we]) and content[_we] != "\n":
                                    _we += 1
                            # Match regular dw: also consume trailing horizontal whitespace.
                            while _we < len(content) and content[_we] in (" ", "\t"):
                                _we += 1
                            content = content[:cursor] + content[_we:]
                            log.append(f"  {i}. .(dw) deleted {_we - cursor} chars")
                    elif lc_verb == "cw":
                        if cursor < len(content):
                            def _is_wcw(ch: str) -> bool:
                                return ch.isalnum() or ch == "_"
                            _we = cursor
                            if _is_wcw(content[_we]):
                                while _we < len(content) and _is_wcw(content[_we]):
                                    _we += 1
                            else:
                                while _we < len(content) and not _is_wcw(content[_we]) and content[_we] != "\n":
                                    _we += 1
                            _text = _decode_escapes(lc_arg)
                            content = content[:cursor] + _text + content[_we:]
                            cursor = cursor + len(_text)
                            last_edit = cursor
                            _preview = _text if len(_text) <= 30 else _text[:27] + "..."
                            log.append(f"  {i}. .(cw{_preview!r}) (cursor={cursor})")
                    elif lc_verb == "ciw":
                        if cursor < len(content) and (content[cursor].isalnum() or content[cursor] == "_"):
                            _ws = cursor
                            while _ws > 0 and (content[_ws - 1].isalnum() or content[_ws - 1] == "_"):
                                _ws -= 1
                            _we = cursor
                            while _we < len(content) and (content[_we].isalnum() or content[_we] == "_"):
                                _we += 1
                            _text = _decode_escapes(lc_arg)
                            content = content[:_ws] + _text + content[_we:]
                            cursor = _ws + len(_text)
                            last_edit = cursor
                            _preview = _text if len(_text) <= 30 else _text[:27] + "..."
                            log.append(f"  {i}. .(ciw{_preview!r}) (cursor={cursor})")
                        else:
                            log.append(f"  {i}. .(ciw) skipped — not on word char")
                    elif lc_verb == "cc":
                        _bol = _line_start(content, cursor)
                        _end = _bol
                        for _ in range(lc_count):
                            _nl = content.find("\n", _end)
                            if _nl == -1:
                                _end = len(content)
                                break
                            _end = _nl + 1
                        _keep_nl = _end > _bol and content[_end - 1] == "\n"
                        _slice_end = _end - 1 if _keep_nl else _end
                        _text = _decode_escapes(lc_arg)
                        content = content[:_bol] + _text + content[_slice_end:]
                        cursor = _bol + len(_text)
                        last_edit = cursor
                        _preview = _text if len(_text) <= 30 else _text[:27] + "..."
                        log.append(f"  {i}. .({lc_count}cc{_preview!r}) (cursor={cursor})")
                    elif lc_verb == "p":
                        if register:
                            if register_linewise:
                                _eol = _line_end(content, cursor)
                                _ins = _eol + 1 if _eol < len(content) else len(content)
                                _paste = register if register.endswith("\n") else register + "\n"
                                content = content[:_ins] + _paste + content[_ins:]
                                cursor = _ins
                            else:
                                _ins = min(len(content), cursor + 1)
                                content = content[:_ins] + register + content[_ins:]
                                cursor = _ins + len(register) - 1
                            log.append(f"  {i}. .(p) pasted {len(register)} chars")
                        else:
                            log.append(f"  {i}. .(p) register empty — skipped")
                    elif lc_verb == ":!":
                        # Replay :!cmd — re-parse lc_arg (same \x1d encoding as original handler).
                        # #147 gate applies here too — dot-repeat of a shell verb still runs shell.
                        # Returns ERROR (not just log) so batch:@file callers checking for
                        # "ERROR" in output actually see the rejection.
                        _vim_gate = _check_vim_shell_allowed()
                        if _vim_gate is not None:
                            return f"ERROR: action {i} '.(:!)': {_vim_gate}"
                        if lc_arg.startswith("\x1d"):
                            _dot_close = lc_arg.find("\x1d", 1)
                            if _dot_close != -1:
                                _dot_range = lc_arg[1:_dot_close]
                                _dot_cmd = lc_arg[_dot_close + 1:]
                                _dot_lines = content.split("\n")
                                _dot_has_trail = _dot_lines and _dot_lines[-1] == ""
                                _dot_total = len(_dot_lines) - (1 if _dot_has_trail else 0)
                                _dot_cur_line, _ = _offset_to_line_col(content, cursor)
                                if _dot_range == "":
                                    # bare :!cmd — insert stdout after cursor line
                                    try:
                                        _dot_proc = subprocess.run(
                                            _dot_cmd, shell=True, capture_output=True,
                                            text=True, timeout=30
                                        )
                                    except (OSError, subprocess.TimeoutExpired) as _dot_e:
                                        log.append(f"  {i}. .(:!{_dot_cmd}) ERROR: {_dot_e}")
                                    else:
                                        if _dot_proc.returncode != 0:
                                            log.append(
                                                f"  {i}. .(:!{_dot_cmd}) ERROR exit "
                                                f"{_dot_proc.returncode}: {_dot_proc.stderr.strip()}"
                                            )
                                        else:
                                            _dot_out = _dot_proc.stdout
                                            if _dot_out and not _dot_out.endswith("\n"):
                                                _dot_out += "\n"
                                            _push_undo()
                                            _dot_eol = _line_end(content, cursor)
                                            if _dot_eol < len(content):
                                                _dot_ins = _dot_eol + 1
                                            else:
                                                if content and not content.endswith("\n"):
                                                    content += "\n"
                                                _dot_ins = len(content)
                                            content = content[:_dot_ins] + _dot_out + content[_dot_ins:]
                                            cursor = _dot_ins
                                            log.append(f"  {i}. .(:!{_dot_cmd}) ({len(_dot_out)} chars inserted)")
                                else:
                                    # ranged :%!cmd / :N,M!cmd
                                    def _dot_resolve(addr: str) -> int:
                                        return _vim_resolve_ex_address(addr, _dot_cur_line, _dot_total)
                                    if _dot_range == "%":
                                        _dot_la, _dot_lb = 1, _dot_total
                                    elif "," in _dot_range:
                                        _dot_a, _dot_b = _dot_range.split(",", 1)
                                        try:
                                            _dot_la = _dot_resolve(_dot_a)
                                            _dot_lb = _dot_resolve(_dot_b)
                                        except ValueError as _dot_ve:
                                            log.append(f"  {i}. .(:!{_dot_cmd}) range error: {_dot_ve}")
                                            _dot_la = _dot_lb = -1
                                    else:
                                        try:
                                            _dot_la = _dot_lb = _dot_resolve(_dot_range)
                                        except ValueError as _dot_ve:
                                            log.append(f"  {i}. .(:!{_dot_cmd}) range error: {_dot_ve}")
                                            _dot_la = _dot_lb = -1
                                    if _dot_la > 0 and _dot_lb > 0:
                                        _dot_lstarts: List[int] = [0]
                                        for _dk, _dc in enumerate(content):
                                            if _dc == "\n":
                                                _dot_lstarts.append(_dk + 1)
                                        _dot_ss = _dot_lstarts[_dot_la - 1]
                                        _dot_se = _dot_lstarts[_dot_lb] if _dot_lb < len(_dot_lstarts) else len(content)
                                        _dot_region = content[_dot_ss:_dot_se]
                                        try:
                                            _dot_proc = subprocess.run(
                                                _dot_cmd, shell=True, input=_dot_region,
                                                capture_output=True, text=True, timeout=30
                                            )
                                        except (OSError, subprocess.TimeoutExpired) as _dot_e:
                                            log.append(f"  {i}. .(:!{_dot_cmd}) ERROR: {_dot_e}")
                                        else:
                                            if _dot_proc.returncode != 0:
                                                log.append(
                                                    f"  {i}. .(:!{_dot_cmd}) ERROR exit "
                                                    f"{_dot_proc.returncode}: {_dot_proc.stderr.strip()}"
                                                )
                                            else:
                                                _dot_out = _dot_proc.stdout
                                                if _dot_out and not _dot_out.endswith("\n"):
                                                    _dot_out += "\n"
                                                _push_undo()
                                                content = content[:_dot_ss] + _dot_out + content[_dot_se:]
                                                cursor = _dot_ss
                                                log.append(
                                                    f"  {i}. .({_dot_range}!{_dot_cmd}) "
                                                    f"(replaced {_dot_lb - _dot_la + 1} lines "
                                                    f"-> {_dot_out.count(chr(10))} lines)"
                                                )
        # --- gi — insert at last edit position ---
        elif verb == "gi":
            _push_undo()
            text = _decode_escapes(arg) * count
            pos = last_edit if last_edit is not None else cursor
            pos = min(pos, len(content))
            content = content[:pos] + text + content[pos:]
            cursor = pos + len(text)
            last_edit = cursor
            preview = text if len(text) <= 30 else text[:27] + "..."
            log.append(f"  {i}. gi{preview!r} (cursor={cursor})")

        # --- macro definition sentinel (from q<reg>...q recording) ---
        elif action.startswith("__macro_def_"):
            reg = action[len("__macro_def_"):]
            # Body already merged into `macros` at state-init time via macros_pending.
            # This action is a no-op at execution; it exists only for log consistency.
            log.append(f"  {i}. q{reg}...q (macro recorded, {len(macros.get(reg, ''))} chars)")

        # --- @<reg> / @@ — macro replay ---
        elif len(verb) == 2 and verb[0] == "@" and (
            ("a" <= verb[1] <= "z") or verb[1] == "@"
        ):
            reg_ch = verb[1]
            if reg_ch == "@":
                if last_replayed_macro is None:
                    return f"ERROR: action {i} '{action}': @@ — no previously replayed macro\n"
                reg_ch = last_replayed_macro
            if reg_ch not in macros:
                return f"ERROR: action {i} '{action}': macro register '{reg_ch}' not defined\n"
            body = macros[reg_ch]
            if not body:
                log.append(f"  {i}. @{reg_ch} (empty macro, no-op)")
            else:
                # Re-tokenize the body through the same normalization so ESC,
                # insert verbs, etc. all work correctly on replay.
                _body_norm = body.replace("\\e", ESC).replace("\x1e", ESC).replace("␞", ESC)
                _body_norm = _body_norm.replace("\\C-a", "\x01").replace("\\C-x", "\x18")
                _body_actions: List[str] = []
                _bi = 0
                _bn = len(_body_norm)
                while _bi < _bn:
                    while _bi < _bn and _body_norm[_bi] in (" \t\n\r" + ESC):
                        _bi += 1
                    if _bi >= _bn:
                        break
                    _bstart = _bi
                    if _body_norm[_bi] == "@" and _bi + 1 < _bn and (
                        ("a" <= _body_norm[_bi + 1] <= "z") or _body_norm[_bi + 1] == "@"
                    ):
                        _body_actions.append(_body_norm[_bstart: _bi + 2])
                        _bi += 2
                        continue
                    _bverb_pos = _bi
                    if _body_norm[_bi].isdigit() and _body_norm[_bi] != "0":
                        while _bverb_pos < _bn and _body_norm[_bverb_pos].isdigit():
                            _bverb_pos += 1
                    if _bverb_pos >= _bn:
                        _body_actions.append(_body_norm[_bstart:])
                        break
                    _bverb_end, _benters_text = _greedy_verb(_body_norm, _bverb_pos)
                    if _benters_text:
                        _besc = _body_norm.find(ESC, _bverb_end)
                        if _besc == -1:
                            _body_actions.append(_body_norm[_bstart:])
                            _bi = _bn
                        else:
                            _body_actions.append(_body_norm[_bstart:_besc])
                            _bi = _besc + 1
                    else:
                        _body_actions.append(_body_norm[_bstart:_bverb_end])
                        _bi = _bverb_end
                _body_actions = [a for a in _body_actions if a]
                # Splice count copies immediately after current position.
                # enumerate starts at 1, so list index of current = i-1;
                # insert-after in list = i.
                _macro_replay_count += count
                if _macro_replay_count > 100:
                    return f"ERROR: action {i} '@{reg_ch}': macro recursion depth limit 100 reached (likely infinite loop)\n"
                splice = _body_actions * count
                for _s in reversed(splice):
                    raw_actions.insert(i, _s)
                last_replayed_macro = reg_ch
                log.append(f"  {i}. @{reg_ch} x{count} ({len(splice)} actions spliced)")

        elif verb == "u":
            # undo: pop from undo_stack; if empty try cross-call snapshot
            if undo_stack:
                prev_content, prev_cursor, prev_marks = undo_stack.pop()
                redo_stack.append((content, cursor, dict(marks)))
                content = prev_content
                cursor = prev_cursor
                marks = dict(prev_marks)
                log.append(f"  {i}. u (undo — {len(undo_stack)} left in stack)")
            elif _xundo_snapshot is not None:
                redo_stack.append((content, cursor, dict(marks)))
                content = _xundo_snapshot["content"]
                cursor = _xundo_snapshot["cursor"]
                marks = dict(_xundo_snapshot["marks"])
                log.append(f"  {i}. u (cross-call undo)")
            else:
                log.append(f"  {i}. u (no prior state to undo — first edit on this file?)")

        elif verb == "\x12":  # Ctrl-R = redo
            if redo_stack:
                redo_content, redo_cursor, redo_marks = redo_stack.pop()
                undo_stack.append((content, cursor, dict(marks)))
                content = redo_content
                cursor = redo_cursor
                marks = dict(redo_marks)
                log.append(f"  {i}. C-r (redo — {len(redo_stack)} left in redo stack)")
            else:
                log.append(f"  {i}. C-r (nothing to redo — no-op)")

        else:
            # Concise "did you mean" — Kevin loops harder when buried in
            # an 80-item catalog. Pick close matches from a short, curated
            # list of common verbs.
            _COMMON_VERBS = [
                "gg", "G", "0", "^", "$", "h", "j", "k", "l", "w", "b", "e",
                "W", "B", "E", "{", "}", "(", ")", "%", "/", "?", "n", "N",
                "i", "a", "I", "A", "o", "O", "s", "S", "C", "r", "x", "X",
                "J", "p", "P", "~",
                "dd", "D", "dw", "yy", "Y", "yw", "cc", "cw", "ciw",
                "ci\"", "ci'", "ci(", "ci[", "ci{",
                ":s", ":%s", ":d", ":r", ":g",
            ]
            # Try the typed verb plus the lead char of the offending action.
            probes = [verb] if verb else []
            head = action[:2] if action else ""
            if head and head not in probes:
                probes.append(head)
            suggestions: List[str] = []
            for probe in probes:
                if not probe:
                    continue
                for m in difflib.get_close_matches(probe, _COMMON_VERBS, n=3, cutoff=0.4):
                    if m not in suggestions:
                        suggestions.append(m)
            # Visual-line `V` / char-visual `v` → suggest line ops users
            # actually want (no visual mode in this op).
            if action and action[:1] in ("V", "v"):
                for m in ("dd", "yy", "cc", ":%s"):
                    if m not in suggestions:
                        suggestions.append(m)
            hint = (
                f"did you mean: {', '.join(suggestions[:5])}"
                if suggestions
                else "see ./supertool 'ops' for the full verb list"
            )
            return (
                f"ERROR: action {i} '{action}': unknown verb '{verb}' — {hint}\n"
            )

    # Save cross-call undo snapshot (pre-edit state captured at script entry).
    _vim_save_undo_snapshot(path, _entry_content, _entry_cursor, _entry_marks)
    try:
        _atomic_write(path, content)
    except OSError as e:
        return f"ERROR: failed to write {path}: {e}\n"

    _vim_save_state(
        path,
        min(cursor, len(content)),
        {k: min(v, len(content)) for k, v in marks.items()},
        min(last_edit, len(content)) if last_edit is not None else None,
        macros,
        last_change=last_change,
    )

    final_line, final_col = _offset_to_line_col(content, cursor)
    new_lines = content.split("\n")
    ctx_start = max(1, final_line - 2)
    ctx_end = min(len(new_lines), final_line + 2)

    out = [
        f"vim {path} ({len(raw_actions)} actions, "
        f"cursor at {final_line}:{final_col})\n"
    ]
    out.extend(line + "\n" for line in log)
    out.append("--- context ---\n")
    for ln in range(ctx_start, ctx_end + 1):
        marker = "→" if ln == final_line else " "
        text = new_lines[ln - 1] if ln - 1 < len(new_lines) else ""
        out.append(f"  {ln:>5} {marker} {text}\n")

    out.append(_vim_render_diff(_before_content, content))
    lint_out = _vim_render_lint(path)
    if lint_out.startswith("--- POST-EDIT LINT FAILED"):
        # Vim's internal lint is informational only — it does NOT auto-roll
        # back. Configure a validator with rollback_on_fail in .supertool.json
        # for true atomicity. Make this explicit in the receipt so the caller
        # doesn't assume the broken edit was reverted.
        lint_out += "[note] file modified despite syntax fail — review or restore manually. Configure a validator with rollback_on_fail for auto-rollback.\n"
    out.append(lint_out)
    return "".join(out)


def op_introduction() -> str:
    """Output the project-specific introduction text from .supertool.json."""
    config = _load_config()
    intro = config.get("introduction", "")
    if not intro:
        return "No introduction configured in .supertool.json\n"
    return str(intro) + "\n\n"


def op_output_format() -> str:
    """Output the output format examples from .supertool.json."""
    config = _load_config()
    fmt = config.get("output-format", "")
    if not fmt:
        return "No output-format configured in .supertool.json\n"
    return str(fmt) + "\n\n"


def op_version() -> str:
    """Output the supertool version."""
    return f"supertool {VERSION}\n"


def op_help(op_name: str) -> str:
    """Output the full reference for a single op from .supertool.json.

    Same metadata `ops` lists, but scoped to one op and never compacted — so
    payload shapes (e.g. vim's macro grammar) are readable without grepping
    source. Looks through builtin-ops, then custom ops, then aliases.
    """
    if not op_name:
        return ("ERROR: help needs an op name — help:OP (e.g. help:vim).\n"
                "Run 'ops' for the full list.\n")
    config = _load_config()
    for section in ("builtin-ops", "ops", "aliases"):
        entry = config.get(section, {})
        if not isinstance(entry, dict) or op_name not in entry:
            continue
        info = entry[op_name]
        if not isinstance(info, dict):
            continue
        out: List[str] = [str(info.get("syntax", op_name))]
        desc = info.get("description", "")
        if desc:
            out.append("")
            out.append(str(desc))
        ops_list = info.get("ops", [])
        if ops_list:
            out.append("")
            out.append("Ops: " + " ".join(str(o) for o in ops_list))
        example = info.get("example", "")
        if example:
            out.append("")
            out.append(f"Example: {example}")
        return "\n".join(out) + "\n"
    if op_name in _BUILTIN_OPS:
        return (f"ERROR: op '{op_name}' has no documented help in "
                f".supertool.json. It's a valid built-in — run 'ops' for the "
                f"list, or see docs/operations.\n")
    return (f"ERROR: no help for op: {op_name}\n"
            f"Run 'ops' for the full list of operations.\n")


# Threshold above which compact ops output gets a "truncation likely" warning.
# Claude Code's hook-stdout cap appears to be ~7KB; anything over that gets
# saved to disk and only a ~2KB preview is injected into the model's context,
# silently hiding the tail of the ops list. (Empirical: 6.6KB landed full,
# 11KB+ got truncated — threshold sits in between.)
_HOOK_OUTPUT_CAP_BYTES = 7168


def op_ops(compact: bool = False) -> str:
    """Output the ops reference from .supertool.json (builtin-ops + ops sections).

    Source of truth is the JSON config. If no config exists, falls back to
    listing built-in op names without descriptions.

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
        lines.append("Built-in operations: " + ", ".join(sorted(_BUILTIN_OPS)))
        lines.append("")
        lines.append("Add a \"builtin-ops\" section to .supertool.json to describe them.")
        return "\n".join(lines) + "\n"

    def _emit_example(info: dict) -> bool:
        """Whether to print the Example: line for this op given current mode."""
        if not info.get("example"):
            return False
        if not compact:
            return True
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
        if not compact:
            return desc
        return desc if info.get("hint") else ""

    # Operations section — built-in and custom merged into one flat list
    has_ops = False
    if builtin_ops or custom_ops:
        lines.append("## Operations\n")
        has_ops = True

    if builtin_ops:
        for name, info in builtin_ops.items():
            if not isinstance(info, dict):
                continue
            if not info.get("status", 1):
                continue
            syntax = info.get("syntax", name)
            desc = _emit_desc(info)
            lines.append(f"- `{syntax}` — {desc}" if desc else f"- `{syntax}`")
            if _emit_example(info):
                lines.append(f"  Example: `{info['example']}`")

    active_custom = {k: v for k, v in custom_ops.items()
                     if isinstance(v, dict) and v.get("status", 1)}
    if active_custom:
        for name, info in active_custom.items():
            desc = _emit_desc(info)
            syntax = info.get("syntax", f"{name}:PATH")
            lines.append(f"- `{syntax}` — {desc}" if desc else f"- `{syntax}`")
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
            ops_list = info.get("ops", [])
            syntax = info.get("syntax", f"{name}:PATH")
            lines.append(f"- `{syntax}` — {desc}" if desc else f"- `{syntax}`")
            if _emit_example(info):
                lines.append(f"  Example: `{info['example']}`")
        lines.append("")

    body = "\n".join(lines) + "\n"

    # In compact mode, only warn if the body still won't fit the harness cap.
    # When it fits, no warning — the absence is itself a signal that the listing
    # is complete.
    if compact and len(body.encode("utf-8")) > _HOOK_OUTPUT_CAP_BYTES:
        warning = (
            f"> {mark('⚠')} Output is {len(body.encode('utf-8'))} bytes, exceeds the "
            f"~{_HOOK_OUTPUT_CAP_BYTES}-byte SessionStart hook cap. The tail "
            f"of this listing will be truncated — ops below the cut-off are "
            f"hidden. Run `./supertool 'ops'` to see the full listing.\n\n"
        )
        body = warning + body

    return body


_NO_EXCLUDE_SUFFIX = ":::no-exclude"


# Validator hooks (PR1). Each entry maps an op name to a callable that
# extracts the target file path from already-parsed `parts`. Only ops listed
# here can be wrapped with a validator. PR2 will add more entries as needed.
_OP_TARGETS: Dict[str, Any] = {
    "edit":          lambda parts: parts[3] if len(parts) > 3 else "",
    "replace":       lambda parts: parts[3] if len(parts) > 3 else "",
    "replace_lines": lambda parts: parts[1] if len(parts) > 1 else "",
    "paste":         lambda parts: parts[1] if len(parts) > 1 else "",
    "append":        lambda parts: parts[1] if len(parts) > 1 else "",
    "vim":           lambda parts: parts[1] if len(parts) > 1 else "",
}


# Built-in syntax backstop (#477). The mutating routes advertise "validators run
# post-edit and roll back on a syntax failure" — a guarantee the tool makes, so
# the tool has to keep it whether or not a repo configured anything. Before this,
# a repo whose only Python validator was `lsp-diag` (a *semantic* diagnostics
# pass, served from a warm daemon cache, rollback_on_fail: false) got
# "ok (no new errors)" on a file that did not parse; a repo with no config at all
# got no check whatsoever. The check is in-process (`compile()`), so it costs
# microseconds and cannot itself be stale.
#
# Deliberately narrow: the interpreter running supertool can parse Python for
# free and nothing else. Other languages need a configured validator — see
# .supertool.example.json, whose parse checks now carry "syntax": true.
_BUILTIN_SYNTAX_VALIDATORS: Dict[str, Dict[str, Any]] = {
    "py-syntax": {
        "builtin": "python",
        "match": "*.py",
        "syntax": True,
        "rollback_on_fail": True,
    },
}


def _builtin_syntax_run(name: str, kind: str, file: str) -> Dict[str, Any]:
    """In-process parse check. SCHEMA.md-shaped, same as a subprocess adapter.

    Anything that is not a verdict — unknown kind, unreadable file — comes back
    as `skipped`, never as ok. A checker that cannot answer must say so, or the
    caller reads its silence as a clean bill (#454, #469).
    """
    import time
    _t0 = time.monotonic()
    if kind != "python":
        return {"tool": name, "file": file, "skipped": f"unknown builtin {kind!r}"}
    try:
        with open(file, "rb") as f:
            src = f.read()
    except OSError as e:
        return {"tool": name, "file": file, "skipped": f"unreadable: {e}"}
    try:
        compile(src, file, "exec", dont_inherit=True)
    except SyntaxError as e:
        err: Dict[str, Any] = {
            "line": getattr(e, "lineno", None),
            "col": getattr(e, "offset", None),
            "severity": "error",
            "code": "syntax",
            "msg": (getattr(e, "msg", None) or str(e)).strip()[:300],
        }
        return {"tool": name, "file": file, "ok": False, "count": 1,
                "errors": [err], "elapsed_s": time.monotonic() - _t0}
    except (ValueError, MemoryError, RecursionError) as e:
        # Null bytes, absurd nesting: the source is rejected by the compiler but
        # not with a line number. Still a hard "does not compile".
        return {"tool": name, "file": file, "ok": False, "count": 1,
                "errors": [{"line": None, "col": None, "severity": "error",
                            "code": "syntax", "msg": str(e)[:300]}],
                "elapsed_s": time.monotonic() - _t0}
    return {"tool": name, "file": file, "ok": True, "count": 0, "errors": [],
            "elapsed_s": time.monotonic() - _t0}


def _builtin_syntax_backstop(op: str, path: str,
                             configured: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Backstop validators for `path`, minus any the repo already covers.

    Defers to a configured validator that declares `"syntax": true` and matches
    the same path — the repo's own parse check is authoritative, and running two
    would double every syntax row.
    """
    if op not in _OP_TARGETS or not path:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for name, spec in _BUILTIN_SYNTAX_VALIDATORS.items():
        if not _match_glob(path, spec["match"]):
            continue
        if name in configured:
            continue
        if any(s.get("syntax") and _match_glob(path, s.get("match", "*"))
               for s in configured.values()):
            continue
        out[name] = spec
    return out


def _applicable_validators(op: str, path: str) -> Dict[str, Dict[str, Any]]:
    """Return validators that should wrap this op call. Skips opt_in."""
    cfg = _load_config()
    validators = cfg.get("validators") or {}
    if not validators:
        return dict(_builtin_syntax_backstop(op, path, {}))
    import fnmatch
    out: Dict[str, Dict[str, Any]] = {}
    for name, spec in validators.items():
        if not isinstance(spec, dict):
            continue
        if op not in (spec.get("hooks_into") or []):
            continue
        if spec.get("opt_in"):
            continue
        glob = spec.get("match", "*")
        if path and glob and not _match_glob(path, glob):
            continue
        if path and _matches_any_glob(path, spec.get("exclude")):
            continue
        out[name] = spec
    out.update(_builtin_syntax_backstop(op, path, out))
    return out


def _applicable_notifiers(op: str, path: str) -> Dict[str, Dict[str, Any]]:
    """Notifiers are validator's read-friendly sibling: same hooks_into/match shape,
    but fire-and-forget. Hook any op (reads included). No rollback, no receipt parsing.
    """
    cfg = _load_config()
    notifiers = cfg.get("notifiers") or {}
    if not notifiers:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for name, spec in notifiers.items():
        if not isinstance(spec, dict):
            continue
        if op not in (spec.get("hooks_into") or []):
            continue
        glob = spec.get("match", "*")
        if path and glob and not _match_glob(path, glob):
            continue
        out[name] = spec
    return out


def _sweep_old_notifier_temp_files(max_age_seconds: int = 3600) -> None:
    """Unlink stale supertool-before-* files older than max_age_seconds.

    NOT cleanup-on-exit: notifiers are fire-and-forget — the parent supertool
    exits within milliseconds of spawning the notifier, but consumers
    (cursor-witness extension) need the temp file alive for seconds (load
    into a diff view) up to a minute (extension's 60s cleanup timer).
    Deleting on parent atexit would race the consumer.

    Strategy: each supertool invocation sweeps before-files older than 1h.
    Long enough that no live diff view depends on them, short enough that
    /tmp doesn't fill over months.
    """
    import glob
    now = time.time()
    for p in glob.glob("/tmp/supertool-before-*"):
        try:
            if now - os.path.getmtime(p) > max_age_seconds:
                os.unlink(p)
        except OSError:
            pass


# Run at import time AND atexit. Import-time sweep clears orphans from prior
# sessions before any new notifier fires. atexit catches any our process spawned
# whose consumer didn't pick them up (best-effort double cleanup).
_sweep_old_notifier_temp_files()
atexit.register(_sweep_old_notifier_temp_files)


# ---------------------------------------------------------------------------
# Cache GC (#474)
#
# ~/.cache/supertool grew to 1.0 GB / 242k files in two weeks with no reaper
# anywhere in the tree. Every writer here is supertool's, so the retention
# policy is too.
#
# Two rules the implementation is built around, both learned the hard way:
#   * The unlink happens in Python. BSD `find -delete` and `find -exec rm {} +`
#     silently no-opped on macOS while listing the same files as matching —
#     269 files left untouched, no error, zero exit. A deletion tool that
#     reports success without deleting is the worst possible shape here.
#   * An entry whose age cannot be determined is never removed. Not knowing
#     how old something is is not evidence that it is stale.
# ---------------------------------------------------------------------------

# Per-kind, because the measurement per kind differs. `vim-cursor` and
# `vim-undo` were 99% older than 7 days — that is where the gigabyte lives.
# `validators` was *entirely* hot (zero entries older than 7 days) and is
# keyed by content hash rather than by time, so its window only has to bound
# unbounded growth, not reclaim anything today: 30 days is a no-op against
# the measured population by design. `vi-cursor` is a legacy directory no
# code still writes to.
_GC_DEFAULT_RETENTION_DAYS: Dict[str, float] = {
    "vim-cursor": 7,
    "vim-undo": 7,
    "vi-cursor": 7,
    "validators": 30,
}

_GC_DEFAULT_INTERVAL_SECONDS = 3600.0
_GC_STAMP_NAME = ".gc-stamp"


def _cache_root() -> Path:
    """The one place ~/.cache/supertool is spelled. Honours XDG_CACHE_HOME."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".cache")
    return base / "supertool"


def _gc_config() -> Dict[str, Any]:
    block = _load_config().get("gc")
    return block if isinstance(block, dict) else {}


def _gc_retention_seconds(kind: str) -> float:
    """Retention window for `kind`, in seconds. 0 or negative means never."""
    overrides = _gc_config().get("retention_days")
    raw: Any = None
    if isinstance(overrides, dict):
        raw = overrides.get(kind)
    if raw is None:
        raw = _GC_DEFAULT_RETENTION_DAYS.get(kind, 7)
    try:
        days = float(raw)
    except (TypeError, ValueError):
        days = float(_GC_DEFAULT_RETENTION_DAYS.get(kind, 7))
    if days <= 0:
        return float("inf")
    return days * 86400.0


def _gc_sweep_kind(kind: str, retention_seconds: float, dry: bool = True,
                   now: "Optional[float]" = None) -> Dict[str, Any]:
    """Prune one cache-kind directory. Non-recursive, `os.unlink` only.

    Deletes strictly on `age > retention` — an entry exactly at the boundary
    is kept. Anything that is not a plain regular file, whose `stat` fails,
    or whose mtime is in the future is counted in `skipped` and left alone.
    """
    ts = time.time() if now is None else now
    result: Dict[str, Any] = {
        "kind": kind, "removed": 0, "bytes": 0, "kept": 0, "skipped": 0,
        "missing": False, "retention_seconds": retention_seconds,
    }
    try:
        scanner = os.scandir(_cache_root() / kind)
    except OSError:
        result["missing"] = True
        return result
    with scanner:
        for entry in scanner:
            try:
                if not entry.is_file(follow_symlinks=False):
                    result["skipped"] += 1
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                result["skipped"] += 1
                continue
            age = ts - st.st_mtime
            if age < 0:
                result["skipped"] += 1
                continue
            if age <= retention_seconds:
                result["kept"] += 1
                continue
            if not dry:
                try:
                    os.unlink(entry.path)
                except OSError:
                    result["skipped"] += 1
                    continue
            result["removed"] += 1
            result["bytes"] += st.st_size
    return result


def _gc_sweep_all(kinds: "Optional[List[str]]" = None, dry: bool = True,
                  now: "Optional[float]" = None) -> List[Dict[str, Any]]:
    names = list(kinds) if kinds else list(_GC_DEFAULT_RETENTION_DAYS)
    return [_gc_sweep_kind(k, _gc_retention_seconds(k), dry=dry, now=now)
            for k in names]


def _gc_fmt_bytes(n: float) -> str:
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024.0
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} GB"


def _gc_fmt_window(seconds: float) -> str:
    return "never" if seconds == float("inf") else f"{seconds / 86400:g}d"


def op_gc(mode: str = "", kind: str = "") -> str:
    """`gc` / `gc:dry` preview, `gc:run` delete. Optional third arg: one kind."""
    known = list(_GC_DEFAULT_RETENTION_DAYS)
    mode = (mode or "dry").strip().lower()
    kind = (kind or "").strip()
    if mode in known and not kind:
        mode, kind = "dry", mode
    if mode not in ("dry", "run"):
        return (f"ERROR: unknown gc mode '{mode}' — expected 'dry' (preview, "
                f"the default) or 'run' (delete)\n")
    if kind and kind not in known:
        return (f"ERROR: unknown cache kind '{kind}' — known kinds: "
                f"{', '.join(known)}\n")

    dry = mode == "dry"
    results = _gc_sweep_all([kind] if kind else None, dry=dry)
    verb = "stale" if dry else "removed"

    lines = ["gc — dry run, nothing deleted" if dry else "gc — deleted"]
    total_n = 0
    total_b = 0
    for r in results:
        total_n += int(r["removed"])
        total_b += int(r["bytes"])
        note = "  (no such directory)" if r["missing"] else ""
        lines.append(
            f"  {r['kind']:<12} {r['removed']} {verb} / "
            f"{_gc_fmt_bytes(r['bytes'])}   (kept {r['kept']}, "
            f"skipped {r['skipped']}, retention "
            f"{_gc_fmt_window(r['retention_seconds'])}){note}"
        )
    lines.append(f"  {'total':<12} {total_n} {verb} / {_gc_fmt_bytes(total_b)}")
    if any(r["skipped"] for r in results):
        lines.append("  skipped = not a regular file, stat failed, or mtime in "
                     "the future — age unknown, so never deleted")
    if dry:
        suffix = f":{kind}" if kind else ""
        lines.append(f"  run `gc:run{suffix}` to delete")
    return "\n".join(lines) + "\n"


def _maybe_auto_gc() -> None:
    """Sweep at most once per `interval_seconds`, gated on a stamp file.

    Deterministic rather than probabilistic on purpose: a stamp mtime is one
    `stat` per invocation, it is testable without monkeypatching `random`,
    and it gives a bounded, explainable answer to "why did that call take
    400ms?" — at most one call an hour pays, and the user can name which.

    Never raises. A cache prune that dies during someone's edit is a worse
    bug than the disk usage it was cleaning up.
    """
    try:
        if os.environ.get("SUPERTOOL_GC_DISABLE"):
            return
        cfg = _gc_config()
        if cfg.get("enabled") is False:
            return
        try:
            interval = float(cfg.get("interval_seconds", _GC_DEFAULT_INTERVAL_SECONDS))
        except (TypeError, ValueError):
            interval = _GC_DEFAULT_INTERVAL_SECONDS
        root = _cache_root()
        stamp = root / _GC_STAMP_NAME
        now = time.time()
        try:
            if now - stamp.stat().st_mtime < interval:
                return
        except OSError:
            pass
        # Stamp BEFORE sweeping. A sweep that dies must not re-arm itself on
        # every subsequent invocation for the rest of the day.
        try:
            root.mkdir(parents=True, exist_ok=True)
            with open(stamp, "w", encoding="utf-8") as fh:
                fh.write(str(int(now)))
        except OSError:
            return
        _gc_sweep_all(dry=False)
    except Exception:
        pass


atexit.register(_maybe_auto_gc)


def _first_changed_line(pre: bytes, post_path: str) -> Optional[int]:
    """Return the 1-indexed line of the first difference between pre bytes
    and the current contents of post_path. None if the file is unreadable
    or unchanged. Used by mutating-op notifiers so observers (cursor-witness)
    can scroll the diff view to the edit (issue #236)."""
    try:
        with open(post_path, "rb") as f:
            post = f.read()
    except OSError:
        return None
    if pre == post:
        return None
    pre_lines = pre.splitlines(keepends=True)
    post_lines = post.splitlines(keepends=True)
    n = min(len(pre_lines), len(post_lines))
    for i in range(n):
        if pre_lines[i] != post_lines[i]:
            return i + 1
    # All shared lines match — the divergence is at the tail (insert/delete).
    return n + 1 if (len(pre_lines) != len(post_lines)) else None


def _run_notifiers(op: str, path: str, line: Optional[int] = None,
                   pre_content: Optional[bytes] = None,
                   line_end: Optional[int] = None) -> None:
    """Spawn-and-forget every matching notifier. Returns immediately.

    line: start line (1-indexed) when known
    line_end: end line (1-indexed inclusive) when the op exposes a range
    pre_content: file bytes BEFORE the op ran. Written to a temp file and
    exposed as `{before_file}` in the notifier cmd template — enables diff
    visualization in observers like cursor-witness.

    Notifier failures are swallowed — observation must never break the op.
    """
    specs = _applicable_notifiers(op, path)
    if not specs:
        _notifier_log(f"no notifier applicable for op={op} path={path}")
        return
    # Mutating ops carry no caller-supplied line but the diff against pre_content
    # gives us the first changed line — let observers (cursor-witness) scroll to it.
    if line is None and pre_content is not None and path and os.path.isfile(path):
        line = _first_changed_line(pre_content, path)
    _notifier_log(f"dispatch op={op} path={path} line={line} line_end={line_end} pre_content={len(pre_content) if pre_content else 0}B notifiers={list(specs.keys())}")

    before_file = ""
    if pre_content is not None:
        try:
            ext = os.path.splitext(path)[1] or ".txt"
            # tempfile.gettempdir() — `/tmp` is POSIX-only.
            fd, before_file = tempfile.mkstemp(
                prefix="supertool-before-", suffix=ext, dir=tempfile.gettempdir())
            with os.fdopen(fd, "wb") as f:
                f.write(pre_content)
        except OSError:
            before_file = ""

    for name, spec in specs.items():
        cmd = spec.get("cmd")
        if not cmd:
            continue
        # Empty placeholders must survive shlex.split as empty string args, not
        # collapse into "two spaces" — which would shift positional argv on
        # consumers like notify.py. shlex.quote("") → '' keeps the slot.
        def _sub(val: Any) -> str:
            s = str(val) if val is not None and val != "" else ""
            return shlex.quote(s)
        cmd = _substitute_placeholders(cmd, {
            "python": _python_token(),
            "op": _sub(op),
            "file": _sub(path),
            "line": _sub(line),
            "line_end": _sub(line_end),
            "before_file": _sub(before_file),
            "supertool_dir": _INSTALL_DIR,
        })
        try:
            subprocess.Popen(
                shlex.split(cmd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, ValueError):
            pass


def _validator_resolve(spec: Dict[str, Any], file: str) -> Optional[str]:
    """Run optional `resolve` cmd to map source→target (e.g. source→test).

    Returns the resolved path, original file if no resolve cmd, or None if
    the resolve cmd succeeded but returned empty (signal: skip this validator).
    """
    if "resolve" not in spec:
        return file
    import subprocess
    # argv-form (shell=False): shell metachars in spec["resolve"] are literal
    # tokens. {file} is still shlex.quote'd so values with spaces survive
    # shlex.split. {supertool_dir} is a known constant.
    cmd = _substitute_placeholders(spec["resolve"], {
        "supertool_dir": _INSTALL_DIR,
        "python": _python_token(),
        "file": shlex.quote(file),
    })
    _prefix_env, cmd = _extract_env_prefix(cmd)
    _merged_env = {**os.environ, **_prefix_env}
    cmd = _expand_env(cmd, _merged_env)
    # Pass merged env to child so prefix vars actually reach the subprocess.
    _run_env = _merged_env if _prefix_env else None
    try:
        r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, timeout=30,
                           env=_run_env)
        resolved = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
        return resolved if resolved else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _validator_cache_enabled() -> bool:
    if os.environ.get("SUPERTOOL_NO_VALIDATOR_CACHE"):
        return False
    return bool(_load_config().get("validator_cache", True))


# Tool fingerprints are stable for a process lifetime — stat once per distinct
# (cmd, spec paths) pair rather than on every cached lookup.
_VALIDATOR_FINGERPRINT_CACHE: Dict[str, str] = {}


def _stat_signature(path: str) -> Optional[str]:
    """`path`'s identity as (size, mtime_ns), or None when it is not a real file."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{path}:{st.st_size}:{st.st_mtime_ns}"


def _validator_fingerprint(spec: Dict[str, Any], cmd: str,
                           exclude: Optional[str] = None) -> str:
    """Identify the TOOLS behind a validator, so upgrading one misses the cache.

    The cache key used to describe only what was analysed, never what did the
    analysing — so a fixed analyser and a buggy one produced the same key, and a
    result computed by the buggy version kept being replayed after the upgrade
    (mcp-phpstan-warm 0.6.0 -> 0.7.0 was found this way). TTL bounded that to a
    day; this closes it.

    Two sources, both cheap stats:

    - every token of `cmd` that resolves to an existing file — the adapter
      script, the interpreter, any binary passed inline. Catches adapter edits.
    - `fingerprint_paths` on the validator spec, plus `validator_fingerprint_paths`
      at config top level. This is where a lockfile belongs: `composer.lock` or
      `package-lock.json` changes on ANY dependency upgrade, which covers
      analysers whose launcher is a stable wrapper script whose own bytes never
      change between versions (composer bin proxies are exactly that).

    An unreadable path contributes nothing rather than failing the lookup: a
    missing lockfile must not disable caching, it only makes the fingerprint
    weaker — which is where we already were.
    """
    cache_key = repr((cmd, spec.get("fingerprint_paths"), exclude))
    memo = _VALIDATOR_FINGERPRINT_CACHE.get(cache_key)
    if memo is not None:
        return memo

    parts: list = []
    # Two tokenisations, unioned. shlex handles quoted paths containing spaces;
    # a naive whitespace split handles paths containing backslashes, which shlex
    # in POSIX mode eats as escapes, so a Windows path shreds into a
    # token that matches no file, so on Windows every cmd token silently
    # contributed nothing and the fingerprint degraded to a constant.
    tokens = set(cmd.split())
    try:
        tokens |= set(shlex.split(cmd, posix=(os.name != "nt")))
    except ValueError:
        pass
    # The analysed file is itself a cmd token ({file} is substituted before the
    # key is built), and it must NOT contribute: the cache is content-addressed
    # so identical content reuses a result. Stat-ing the target would put its
    # mtime in the key, and a checkout/stash/rsync that rewrites identical bytes
    # would miss the cache and re-run every validator on every touched file.
    skip = os.path.realpath(exclude) if exclude else None
    for token in tokens:
        token = token.strip("'\"")
        if skip is not None and os.path.realpath(token) == skip:
            continue
        sig = _stat_signature(token)
        if sig is not None:
            parts.append(sig)

    extra = list(spec.get("fingerprint_paths") or [])
    cfg_extra = _load_config().get("validator_fingerprint_paths") or []
    if isinstance(cfg_extra, list):
        extra.extend(str(p) for p in cfg_extra)
    for path in extra:
        sig = _stat_signature(path)
        if sig is not None:
            parts.append(sig)

    import hashlib
    fingerprint = hashlib.sha256("\x00".join(sorted(parts)).encode("utf-8")).hexdigest()
    _VALIDATOR_FINGERPRINT_CACHE[cache_key] = fingerprint
    return fingerprint


def _validator_cache_key(file_path: str, name: str, cmd: str,
                         spec: Optional[Dict[str, Any]] = None) -> Optional[str]:
    import hashlib
    try:
        with open(file_path, "rb") as f:
            content = f.read()
    except OSError:
        return None
    h = hashlib.sha256()
    h.update(content)
    h.update(b"\x00" + name.encode("utf-8"))
    h.update(b"\x00" + cmd.encode("utf-8"))
    h.update(b"\x00" + _validator_fingerprint(spec or {}, cmd, file_path).encode("utf-8"))
    return h.hexdigest()


def _validator_cache_path(key: str) -> Path:
    return _cache_root() / "validators" / f"{key}.json"


def _validator_cache_secret() -> bytes:
    """Per-user HMAC secret for cache integrity (closes #150 cache-poison).

    32-byte random secret stored at `~/.cache/supertool/.cache_key`, mode
    0600. Attacker with write access to the cache dir (compromised account,
    malicious npm postinstall) cannot forge a passing `ok: true` entry
    without also reading the secret.
    """
    secret_path = _cache_root() / ".cache_key"
    try:
        if secret_path.is_file():
            data = secret_path.read_bytes()
            if len(data) == 32:
                return data
    except OSError:
        pass
    secret = os.urandom(32)
    try:
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        # O_BINARY is required on Windows to prevent CR/LF translation that
        # would corrupt the 32-byte raw secret and make len(data) != 32 on
        # subsequent reads, causing a new secret to be generated every call.
        _o_binary = getattr(os, "O_BINARY", 0)
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _o_binary, 0o600)
        try:
            os.write(fd, secret)
        finally:
            os.close(fd)
        return secret
    except FileExistsError:
        try:
            return secret_path.read_bytes()
        except OSError:
            return secret
    except OSError:
        return secret


def _validator_cache_read(key: str) -> Optional[Dict[str, Any]]:
    """Read + HMAC-verify a cache entry. Returns None on missing / tampered.

    Legacy unwrapped entries (pre-HMAC) treated as miss — they get rewritten
    in wrapped form next time the validator runs.
    """
    import hashlib
    import hmac
    import json
    import time
    p = _validator_cache_path(key)
    if not p.exists():
        return None
    # TTL: a backstop for staleness the key still cannot see. Tool upgrades and
    # adapter edits are now keyed directly (see _validator_fingerprint), but a
    # transient engine failure that a clean re-run would pass, or a config file
    # nobody listed in fingerprint_paths, still slips through. Expire on access
    # (treat as a miss, which re-runs and rewrites with a fresh mtime) so no
    # staleness survives past the window. Config `validator_cache_ttl_hours`
    # (default 24; 0 disables expiry).
    try:
        _ttl_hours = float(_load_config().get("validator_cache_ttl_hours", 24))
    except (TypeError, ValueError):
        _ttl_hours = 24.0
    if _ttl_hours > 0:
        try:
            if time.time() - p.stat().st_mtime > _ttl_hours * 3600:
                return None
        except OSError:
            return None
    try:
        wrapped = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(wrapped, dict) or "data" not in wrapped or "mac" not in wrapped:
        return None  # legacy unwrapped — don't trust ok=True
    payload = json.dumps(wrapped["data"], sort_keys=True).encode("utf-8")
    expected = hmac.new(_validator_cache_secret(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(wrapped.get("mac", ""))):
        return None  # tampered or written by another machine's secret
    return wrapped["data"] if isinstance(wrapped["data"], dict) else None


def _validator_cache_write(key: str, data: Dict[str, Any]) -> None:
    """Write a cache entry wrapped with HMAC over its JSON body."""
    import hashlib
    import hmac
    import json
    p = _validator_cache_path(key)
    payload = json.dumps(data, sort_keys=True).encode("utf-8")
    mac = hmac.new(_validator_cache_secret(), payload, hashlib.sha256).hexdigest()
    wrapped = {"data": data, "mac": mac}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(wrapped), encoding="utf-8")
    except OSError:
        pass


# Engine-failure error codes/messages that are NON-deterministic: a clean re-run
# can flip them. These must never be cached (the cache key is the file's content
# hash, so a frozen failure replays on every later run until the file changes).
_NONDETERMINISTIC_ERROR_CODES = {"mcp", "orchestrator", "rector.exit"}


def _validator_result_is_cacheable(data: Dict[str, Any]) -> bool:
    """True unless the result is a non-deterministic engine/transport failure.

    WHY THIS EXISTS (2026-06, the 2100-poisoned-entries incident):
    rector-mcp's warm daemon intermittently trips rector's own known bug —
    `System error: "ClassReflection must be resolved for class XTest"` — on test
    classes. It depends on warm-process state, NOT on the file: a cold/clean
    daemon (and plain `rector` CLI) reflect the same file fine. But the failed
    result got cached keyed on file content, so every subsequent run replayed the
    stale error — same message, same frozen duration_ms — long after the live
    daemon recovered. 2100 test files were silently "failing" rector this way.

    Real findings stay cacheable (phpstan types, `rector.refactor` suggestions are
    deterministic — same input, same output, caching them is the whole point).
    This core filter is intentionally GENERIC: it keys only off non-deterministic
    error *codes* (MCP transport errors, non-zero exits), never off tool-specific
    message text (SCHEMA.md: "Validator core never parses tool-specific output").
    Message-level engine-glitch suppression (rector's "System error:" /
    "toMutatingScope() on null") now lives in the adapter, configured per-mcp via
    the .supertool.json `validators.rector.engine_glitches` prop; see
    validators/rector-mcp/rector-mcp.py, which drops those at the source so they
    never reach this cache as a red.
    """
    if "skipped" in data:
        # A skip is decided by config (scope allowlists, missing tool), not by
        # file content — and the key is a content hash. Freezing one here keeps
        # skipping a file that config later brings into scope (#406).
        return False
    if data.get("ok"):
        return True
    for err in data.get("errors") or []:
        if not isinstance(err, dict):
            continue
        if err.get("code") in _NONDETERMINISTIC_ERROR_CODES:
            return False
    return True


def _validator_run_one(name: str, spec: Dict[str, Any], file: str) -> Optional[Dict[str, Any]]:
    """Run one validator adapter on `file`. Returns SCHEMA.md-compliant dict.

    Adapter contract: prints one JSON object on last stdout line. Exit 0 unless
    infra fail. Failures here produce a synthetic error dict so the row still
    renders. Cached by (file content hash, name, cmd, tool fingerprint) at
    ~/.cache/supertool/validators/<sha256>.json — see _validator_fingerprint for
    why the tools themselves are part of the key.
    """
    import subprocess
    import json
    target = _validator_resolve(spec, file)
    if target is None:
        return {"tool": name, "skipped": "no target resolved"}
    # Built-in validators (#477) have no adapter and no `cmd`: they run in this
    # process. Handled before the cmd substitution below, which would KeyError.
    if spec.get("builtin"):
        return _builtin_syntax_run(name, str(spec["builtin"]), target)
    # argv-form (shell=False) downstream: shell metachars in spec["cmd"] are
    # literal tokens. {file} stays shlex.quote'd so values with spaces survive
    # shlex.split. {supertool_dir} is a known constant.
    cmd = _substitute_placeholders(spec["cmd"], {
        "supertool_dir": _INSTALL_DIR,
        "python": _python_token(),
        "file": shlex.quote(target),
    })
    # Lift leading `KEY=VAL` shell env-prefix into env dict (shipped cmd
    # templates use this to set MCP_*_WORKING_DIR before the python invocation).
    _prefix_env, cmd = _extract_env_prefix(cmd)
    # $VAR / ${VAR} expansion + child env both need spec.env + prefix env.
    _spec_env_dict = {**_prefix_env, **(spec.get("env") or {})}
    _merged_env = {**os.environ, **{str(k): str(v) for k, v in _spec_env_dict.items()}}
    cmd = _expand_env(cmd, _merged_env)
    timeout = int(spec.get("timeout", 60))

    # Per-validator opt-out: spec.cache = false disables caching for this validator.
    # Useful when the adapter's input file isn't the only thing that affects results
    # (e.g. phpunit: source + test + bootstrap + DI graph all matter, but cache key
    # only hashes the resolved file).
    spec_cache_enabled = bool(spec.get("cache", True))

    cache_key: Optional[str] = None
    if _validator_cache_enabled() and spec_cache_enabled:
        cache_key = _validator_cache_key(target, name, cmd, spec)
        if cache_key:
            cached = _validator_cache_read(cache_key)
            if cached is not None:
                return cached

    # Use _merged_env (built above) so the prefix env-vars reach the child too.
    run_env = _merged_env if _spec_env_dict else None

    import time
    _t0 = time.monotonic()
    try:
        r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, timeout=timeout,
                           env=run_env)
        _elapsed = time.monotonic() - _t0
        out = r.stdout.strip()
        if not out:
            return {"tool": name, "file": target, "ok": False, "count": 1,
                    "errors": [{"line": None, "col": None, "severity": "error",
                                "code": "orchestrator", "msg": "adapter produced no output"}],
                    "duration_ms": 0, "elapsed_s": _elapsed}
        data = json.loads(out.splitlines()[-1])
        data["elapsed_s"] = _elapsed
        if target != file:
            data["resolved_to"] = target
        if cache_key and _validator_result_is_cacheable(data):
            _validator_cache_write(cache_key, data)
        return data
    except subprocess.TimeoutExpired:
        return {"tool": name, "file": target, "ok": False, "count": 1,
                "errors": [{"line": None, "col": None, "severity": "error",
                            "code": "orchestrator", "msg": f"timeout after {timeout}s"}],
                "duration_ms": timeout * 1000, "elapsed_s": time.monotonic() - _t0,
                "timeout": True}
    except OSError as e:
        return {"tool": name, "file": target, "ok": False, "count": 1,
                "errors": [{"line": None, "col": None, "severity": "error",
                            "code": "orchestrator", "msg": f"adapter not found or unrunnable: {e}"}],
                "duration_ms": 0, "elapsed_s": time.monotonic() - _t0}
    except (json.JSONDecodeError, IndexError) as e:
        return {"tool": name, "file": target, "ok": False, "count": 1,
                "errors": [{"line": None, "col": None, "severity": "error",
                            "code": "orchestrator", "msg": f"adapter bad json: {e}"}],
                "duration_ms": 0, "elapsed_s": time.monotonic() - _t0}


def _validator_render_row(data: Dict[str, Any], verbose: bool = False) -> list:
    """Render a single validator result as a list of display lines.

    verbose=False (default): compact mode — summary header + up to 5 errors,
    then ``... +N more`` if there are additional errors.

    verbose=True: full mode — summary header + ALL errors (no cap), plus the
    adapter's raw stdout/stderr appended verbatim when present in the result
    dict under the ``"raw_stdout"`` / ``"raw_stderr"`` keys.

    The ``"raw_stdout"`` / ``"raw_stderr"`` keys are optional; adapters that
    want verbose output to include their full output should populate them.
    """
    if "skipped" in data:
        return [f"{data['tool']:12s}: skipped — {data['skipped']}"]
    tool = data.get("tool", "?")
    ok = data.get("ok", False)
    count = data.get("count", 0)
    dur = data.get("duration_ms", 0)
    status = "ok" if ok else f"{count} err"
    line = f"{tool:12s}: {status:<10}  ({dur}ms)"
    metrics = data.get("metrics")
    if metrics and tool == "git-status":
        added = metrics.get("lines_added", 0)
        removed = metrics.get("lines_removed", 0)
        state = metrics.get("state", "")
        line += f"  +{added} -{removed} {state}"
    if data.get("resolved_to"):
        line += f"  → {data['resolved_to']}"
    out = [line]
    errors = data.get("errors") or []
    if verbose:
        for e in errors:
            line_n = f"L{e['line']}" if e.get("line") else "  "
            code = e.get("code") or ""
            msg = (e.get("msg") or "").strip().replace("\n", " ")
            out.append(f"  {line_n} {code}  {msg}")
            for ctx_line in (e.get("source_context") or []):
                out.append(f"    {ctx_line}")
        for key, label in (("raw_stdout", "stdout"), ("raw_stderr", "stderr")):
            raw = (data.get(key) or "").strip()
            if raw:
                out.append(f"  [{label}]")
                for raw_line in raw.splitlines():
                    out.append(f"    {raw_line}")
        diff = (data.get("diff") or "").strip()
        if diff:
            out.append("  [diff]")
            for diff_line in diff.splitlines():
                out.append(f"  {diff_line}")
            out.append("  [/diff]")
    else:
        for e in errors[:5]:
            line_n = f"L{e['line']}" if e.get("line") else "  "
            code = e.get("code") or ""
            msg = (e.get("msg") or "").strip().replace("\n", " ")[:120]
            out.append(f"  {line_n} {code}  {msg}")
        if len(errors) > 5:
            out.append(f"  ... +{len(errors) - 5} more")
    return out


def _validator_regressed(before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> bool:
    """Did this op make this validator worse? The single definition of ✗ (#406).

    Both the rendered marker and the rollback decision read from here, so the
    red the caller sees and the revert it triggers can never disagree.

    Three states, not two: a `skipped` result is an absence of information, not
    a finding, so it can never regress — and must never roll back an edit.
    A failure that was already there before the op is not a regression either.
    """
    if "skipped" in after:
        return False
    if after.get("ok", False):
        return False
    b_count = before.get("count", 0) if before else 0
    a_count = after.get("count", 0)
    b_ok = before.get("ok", True) if before else True
    if b_count == a_count and b_ok == after.get("ok", False):
        return False
    return a_count - b_count >= 0


def _validator_render_diff(before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> list:
    # Skipped path never started a timer, so elapsed_s is absent — `-` rendered in time col.
    elapsed = after.get("elapsed_s")
    time_col = f"{elapsed:.1f}s" if elapsed is not None else "-"
    if "skipped" in after:
        # Name the reason. "skipped" alone sends the reader back to the config
        # to work out which of a dozen reasons applied (#406).
        reason = str(after["skipped"]).strip().replace("\n", " ")[:80]
        state_col = f"({reason})" if reason else ""
        return [f"{after['tool']:12s}: {'skipped':<10}  {state_col}  {time_col:>5}"]
    tool = after["tool"]
    b_count = before.get("count", 0) if before else 0
    a_count = after.get("count", 0)
    delta = a_count - b_count
    b_ok = before.get("ok", True) if before else True
    a_ok = after.get("ok", False)
    if b_count == a_count and b_ok == a_ok:
        # Count/ok unchanged — surface metric deltas (e.g. tests_total) so the LLM
        # knows whether scope actually changed (7 tests → 10 tests, both pass).
        b_metrics = (before or {}).get("metrics") or {}
        a_metrics = after.get("metrics") or {}
        metric_parts = []
        for k, av in a_metrics.items():
            if not isinstance(av, (int, float)):
                continue
            bv = b_metrics.get(k, 0)
            if not isinstance(bv, (int, float)):
                bv = 0
            if av == bv:
                continue
            d = av - bv
            metric_parts.append(f"{k} {bv}\u2192{av} ({'+' if d > 0 else ''}{d})")
        if metric_parts:
            marker = mark("\u2713") if a_ok else mark("\u2717")
            return [f"{tool:12s}: {', '.join(metric_parts)} {marker}  {'':<11}  {time_col:>5}"]
        # Truly unchanged — fold the most relevant absolute metric into the row.
        if a_ok and a_metrics:
            primary = None
            for k in ("tests_total", "tests_passed", "changes_count"):
                if k in a_metrics:
                    primary = (k, a_metrics[k]); break
            if primary is not None:
                status = f"ok {primary[0]}={primary[1]}"
                return [f"{tool:12s}: {status:<10}  {'(no new errors)':<15}  {time_col:>5}"]
        status = "ok" if a_ok else f"{a_count} err"
        if a_ok:
            # Not "(unchanged)" — that reads as "the file is unchanged", which is
            # the opposite of what just happened. This column reports the delta in
            # the validator's own result (#380).
            marker_col = "(no new errors)"
        elif after.get("timeout"):
            marker_col = "(timeout)"
        else:
            marker_col = "(pre-existing — not from this edit)"
        out = [f"{tool:12s}: {status:<10}  {marker_col}  {time_col:>5}"]
        if not a_ok and not after.get("timeout"):
            for e in (after.get("errors") or [])[:5]:
                line_n = f"L{e['line']}" if e.get("line") else "  "
                code = e.get("code") or ""
                msg = (e.get("msg") or "").strip().replace("\n", " ")[:120]
                out.append(f"  {line_n} {code}  {msg}")
            if len(after.get("errors") or []) > 5:
                out.append(f"  ... +{len(after['errors']) - 5} more")
        return out
    marker = (mark("✗") if _validator_regressed(before, after)
              else (mark("✓") if a_ok else mark("⚠")))
    arrow = f"{b_count} → {a_count}"
    sign = f"({'+' if delta >= 0 else ''}{delta})"
    state_col = f"{sign} {marker}"
    out = [f"{tool:12s}: {arrow:<10}  {state_col:<11}  {time_col:>5}"]
    if not a_ok:
        before_msgs = {e.get("msg") for e in (before.get("errors") or [])} if before else set()
        new = [e for e in (after.get("errors") or []) if e.get("msg") not in before_msgs]
        for e in new[:5]:
            line_n = f"L{e['line']}" if e.get("line") else "  "
            code = e.get("code") or ""
            msg = (e.get("msg") or "").strip().replace("\n", " ")[:120]
            out.append(f"  + {line_n} {code}  {msg}")
        if len(new) > 5:
            out.append(f"  + ... +{len(new) - 5} more new")
    return out


def _validators_run_batch(
    applicable: Dict[str, Dict[str, Any]], path: str
) -> Dict[str, Dict[str, Any]]:
    """Run all validators on path. Parallel if `parallel >= 2` in config."""
    workers = _parallel_workers()
    if workers >= 2 and len(applicable) > 1:
        from concurrent.futures import ThreadPoolExecutor
        max_workers = min(workers, len(applicable))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {name: ex.submit(_validator_run_one, name, spec, path)
                       for name, spec in applicable.items()}
            return {name: f.result() for name, f in futures.items()
                    if f.result() is not None}
    out: Dict[str, Dict[str, Any]] = {}
    for name, spec in applicable.items():
        data = _validator_run_one(name, spec, path)
        if data is not None:
            out[name] = data
    return out


# ---------------------------------------------------------------------------
# Formatter hooks — mirror of the validator system.
# Run order: edit → formatter(s) → validator(s) → rollback if validate fails.
# Formatters mutate the file in place (e.g. prettier --write).
# rollback_on_fail defaults to False — formatters are cosmetic; validators
# are the safety net.
# ---------------------------------------------------------------------------

# Config files that prove a repo actually runs a given formatter (#393).
# Keyed by a substring of the formatter's name in .supertool.json. A tool with
# no entry here (gofmt, which has no config, or anything custom) is never
# gated — absence of knowledge is not evidence of opt-out.
#
# Each value is (filename globs, ((manifest file, substring that must appear), ...)).
_FORMATTER_CONFIG_MARKERS: Dict[str, Any] = {
    "prettier": (
        (".prettierrc*", "prettier.config.*"),
        (("package.json", '"prettier"'),),
    ),
    "black": ((), (("pyproject.toml", "[tool.black]"),)),
    "ruff": (("ruff.toml", ".ruff.toml"), (("pyproject.toml", "[tool.ruff"),)),
    "isort": ((".isort.cfg",), (("pyproject.toml", "[tool.isort]"),
                                ("setup.cfg", "[isort]"))),
    "eslint": ((".eslintrc*", "eslint.config.*"),
               (("package.json", '"eslintConfig"'),)),
    "php-cs-fixer": ((".php-cs-fixer*.php", ".php_cs*"), ()),
    "phpcbf": (("phpcs.xml*", ".phpcs.xml*", "phpcs.dist.xml"), ()),
    "phpcs": (("phpcs.xml*", ".phpcs.xml*", "phpcs.dist.xml"), ()),
    "rustfmt": (("rustfmt.toml", ".rustfmt.toml"), ()),
    "clang-format": ((".clang-format",), ()),
}

# Formatters gated out by the opt-in rule, drained onto the receipt by dispatch.
# A silent skip reads as "nothing to format here", which is the same failure the
# gate exists to fix, one direction over: the caller cannot tell a formatted file
# from an ungated one. Keyed by name so a batch of edits reports each tool once.
_FORMATTER_SKIPS: List[str] = []


# An env key ending in one of these, with a value, means the spec carries its
# own rules — the repo opted in through .supertool.json rather than through a
# config file of the tool's own (DVSI's phpcbf runs PSR12 with no phpcs.xml).
_FORMATTER_EXPLICIT_ENV_SUFFIXES = ("_CONFIG", "_STANDARD", "_RULES", "_RULESET")


def _formatter_markers_for(name: str) -> Optional[Any]:
    """Marker table entry for a formatter name, or None when the tool is unknown.

    The config name must CONTAIN the table key ("prettier-write" → prettier), not
    the other way round: a spec called "fmt" is a house tool, and matching it
    against "rustfmt" because one is a substring of the other would gate a
    formatter on config for a tool it has nothing to do with.
    """
    lowered = name.lower()
    for key, markers in _FORMATTER_CONFIG_MARKERS.items():
        if key in lowered:
            return markers
    return None


def _repo_opts_into_formatter(name: str, spec: Dict[str, Any], path: str) -> bool:
    """Does the repo holding `path` show evidence it runs this formatter? (#393)

    A formatter rewrites the whole file, so running one the repo never runs
    turns a two-line edit into a hundred-line diff of changes nobody asked
    for — and in a repo with hand-aligned tables it is simply wrong. The
    default flips to "validate, never rewrite" unless there is evidence:

      * `requires_config: false` in the spec — explicit always-run opt-out;
      * an `env` entry naming the tool's config or standard (the spec itself
        carries the rules, so no repo config file is expected);
      * a config file for the tool, searched from the file's own directory up
        to its repo root — NOT from cwd, so editing another repo from this
        shell applies that repo's answer, not this one's;
      * an unknown tool (no marker table entry), which is left alone.
    """
    if os.environ.get("SUPERTOOL_FORMAT_WITHOUT_CONFIG") == "1":
        return True
    requires = spec.get("requires_config")
    if requires is False:
        return True
    markers = _formatter_markers_for(name)
    if isinstance(requires, str):
        markers = ((requires,), ())
    elif isinstance(requires, list) and requires:
        markers = (tuple(str(m) for m in requires), ())
    if markers is None:
        return True
    env = spec.get("env")
    if isinstance(env, dict):
        for key, value in env.items():
            if value and str(key).upper().endswith(_FORMATTER_EXPLICIT_ENV_SUFFIXES):
                return True
    import fnmatch
    globs, manifests = markers
    for directory in _dirs_up_to_repo_root(path):
        for glob in globs:
            try:
                if any(fnmatch.fnmatch(entry, glob) for entry in os.listdir(directory)):
                    return True
            except OSError:
                continue
        for manifest, needle in manifests:
            candidate = os.path.join(directory, manifest)
            try:
                with open(candidate, "r", encoding="utf-8", errors="replace") as fh:
                    if needle in fh.read():
                        return True
            except OSError:
                continue
    return False


_REPO_ROOT_WALK_CACHE: Dict[str, List[str]] = {}


def _dirs_up_to_repo_root(path: str) -> List[str]:
    """`path`'s directory and every parent up to and including its repo root.

    Stops at the first directory holding `.git` (worktrees use a `.git` file,
    so existence is the test, not is-a-directory), else at the filesystem root.

    Symlinks are resolved first, for the same reason `_atomic_write` resolves
    them: a file reached through a symlinked directory has its real repo
    somewhere else entirely, and walking the link's own location climbs to the
    filesystem root without ever meeting the config that governs the file.

    Cached per directory — a batch editing 40 files under one root would
    otherwise repeat the identical walk 40 times, once per formatter.
    """
    real = os.path.realpath(path)
    start = os.path.dirname(real) or os.sep
    cached = _REPO_ROOT_WALK_CACHE.get(start)
    if cached is not None:
        return cached
    current = start
    out: List[str] = []
    while True:
        out.append(current)
        if os.path.exists(os.path.join(current, ".git")):
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    _REPO_ROOT_WALK_CACHE[start] = out
    return out


def _applicable_formatters(op: str, path: str) -> Dict[str, Dict[str, Any]]:
    """Return formatters that should run after this op. Same logic as validators,
    plus the opt-in gate of #393 — see `_repo_opts_into_formatter`."""
    cfg = _load_config()
    formatters = cfg.get("formatters") or {}
    if not formatters:
        return {}
    import fnmatch
    out: Dict[str, Dict[str, Any]] = {}
    for name, spec in formatters.items():
        if not isinstance(spec, dict):
            continue
        if op not in (spec.get("hooks_into") or []):
            continue
        glob = spec.get("match", "*")
        if path and glob and not _match_glob(path, glob):
            continue
        if path and _matches_any_glob(path, spec.get("exclude")):
            continue
        if path and not _repo_opts_into_formatter(name, spec, path):
            if name not in _FORMATTER_SKIPS:
                _FORMATTER_SKIPS.append(name)
            continue
        out[name] = spec
    return out


def _formatter_run_one(name: str, spec: Dict[str, Any], file: str) -> Dict[str, Any]:
    """Run one formatter against `file`. Returns a SCHEMA-shaped result dict.

    If the adapter emits valid SCHEMA.md JSON on stdout, that is parsed directly
    and used as the result (preferred — gives metrics + structured errors).
    Legacy adapters that emit nothing / non-JSON still work: exit 0 → ok, else fail.
    The result always carries ``"name"`` so callers can identify it.
    """
    import subprocess
    # argv-form (shell=False): shell metachars in spec["cmd"] are literal
    # tokens, not shell operators. {file} stays shlex.quote'd so values with
    # spaces survive shlex.split. {supertool_dir} is a known constant.
    cmd = _substitute_placeholders(spec["cmd"], {
        "supertool_dir": _INSTALL_DIR,
        "python": _python_token(),
        "file": shlex.quote(file),
    })
    _prefix_env, cmd = _extract_env_prefix(cmd)
    _spec_env_dict = {**_prefix_env, **(spec.get("env") or {})}
    _merged_env = {**os.environ, **{str(k): str(v) for k, v in _spec_env_dict.items()}}
    cmd = _expand_env(cmd, _merged_env)
    timeout = int(spec.get("timeout", 30))
    run_env = _merged_env if _spec_env_dict else None
    try:
        r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, timeout=timeout,
                           env=run_env)
        stdout = r.stdout.strip()
        # Try to parse SCHEMA.md JSON from stdout.
        if stdout:
            try:
                data = json.loads(stdout)
                if isinstance(data, dict) and "ok" in data:
                    data["name"] = name
                    return data
            except (json.JSONDecodeError, ValueError):
                pass
        # Legacy fallback: non-JSON adapter. Preserve the raw output so the
        # renderer can show it verbatim — we can't compute metrics without an
        # adapter-emitted before/after diff, and silent-on-noop would hide
        # legacy formatters' output entirely.
        raw_combined = (stdout + ("\n" + r.stderr.strip() if r.stderr.strip() else "")).strip()
        return {
            "name": name,
            "ok": r.returncode == 0,
            "raw": raw_combined,
            "duration_ms": 0,
            "metrics": {"lines_added": 0, "lines_removed": 0},
        }
    except subprocess.TimeoutExpired:
        return {"name": name, "ok": False, "msg": f"timeout after {timeout}s",
                "duration_ms": timeout * 1000,
                "metrics": {"lines_added": 0, "lines_removed": 0}}
    except OSError as e:
        return {"name": name, "ok": False, "msg": str(e), "duration_ms": 0,
                "metrics": {"lines_added": 0, "lines_removed": 0}}


def _formatter_render_row(result: Dict[str, Any]) -> Optional[str]:
    """Render one formatter result as a display line.

    Returns None (silent) when the formatter was a no-op:
    ok=True and metrics.lines_added == 0 and metrics.lines_removed == 0.
    Failures always produce a row.
    """
    name = result.get("name") or result.get("tool") or "?"
    ok = result.get("ok", False)
    dur = result.get("duration_ms", 0)
    metrics = result.get("metrics") or {}
    added = metrics.get("lines_added", 0)
    removed = metrics.get("lines_removed", 0)

    # Legacy non-JSON adapter: show raw output verbatim (can't compute metrics).
    # Silent only when the formatter ran cleanly AND printed nothing.
    if "raw" in result:
        raw = (result.get("raw") or "").strip()
        if ok and not raw:
            return None  # quiet clean run
        status = "ok" if ok else "fail"
        if raw:
            return f"{name:8s}: {status}       {raw}"
        return f"{name:8s}: {status}"

    if ok and added == 0 and removed == 0:
        return None  # silent no-op

    if ok:
        line = f"{name:8s}: ok         ({dur}ms) +{added} -{removed}"
    else:
        errors = result.get("errors") or []
        msg = result.get("msg") or (errors[0].get("msg") if errors else "") or "failed"
        msg = str(msg)[:120]
        line = f"{name:8s}: fail       ({dur}ms)  {msg}"
    return line


# Deferred-formatter state for multi-op invocations.
# When _DEFER_FORMATTERS is True, _run_with_validators queues formatter
# (path → {name: spec}) instead of running them inline. main() drains the
# queue once after all ops complete, ensuring tidy rules (e.g.
# no_unused_imports) don't strip code that a later op in the same call
# was about to use. See issue #164.
_DEFER_FORMATTERS: bool = False
_FORMAT_QUEUE: Dict[str, Dict[str, Dict[str, Any]]] = {}

# Deferred-validator state for multi-op invocations (issue #219).
# Validators with tier="slow" are queued here as (name, path) pairs instead of
# running per-op. main() drains once after all ops complete, deduping by
# (name, path) and preserving insertion order.
_VALIDATOR_DEFER_QUEUE: "list[tuple[str, Dict[str, Any], str]]" = []
_VALIDATOR_DEFER_SEEN: "set[tuple[str, str]]" = set()


def _drain_format_queue() -> str:
    """Run queued formatters on each path once. Returns rendered output block."""
    global _FORMAT_QUEUE
    if not _FORMAT_QUEUE:
        return ""
    rows: list = []
    for path, applicable in _FORMAT_QUEUE.items():
        if not applicable:
            continue
        results = _formatters_run_batch(applicable, path)
        path_rows: list = []
        for result in results:
            row = _formatter_render_row(result)
            if row:
                path_rows.append(row)
        if path_rows:
            rows.append(f"  {path}")
            rows.extend(f"    {r}" for r in path_rows)
    _FORMAT_QUEUE = {}
    if not rows:
        return ""
    return "\n--- formatters (deferred) ---\n" + "\n".join(rows) + "\n"


def _drain_validator_queue() -> str:
    """Run queued slow validators once per unique (name, path) pair. Returns rendered output block.

    Output groups results by path with a file header per group, so the
    reader knows which file each validator row belongs to (issue #234).
    """
    global _VALIDATOR_DEFER_QUEUE, _VALIDATOR_DEFER_SEEN
    if not _VALIDATOR_DEFER_QUEUE:
        return ""
    by_path: "dict[str, list[str]]" = {}
    for name, spec, path in _VALIDATOR_DEFER_QUEUE:
        data = _validator_run_one(name, spec, path)
        if data is None:
            continue
        path_rows = _validator_render_diff(None, data)
        if path_rows:
            by_path.setdefault(path, []).extend(path_rows)
    _VALIDATOR_DEFER_QUEUE = []
    _VALIDATOR_DEFER_SEEN = set()
    if not by_path:
        return ""
    rows: list = []
    for path, path_rows in by_path.items():
        rows.append(f"  {path}")
        rows.extend(f"    {r}" for r in path_rows)
    return "\n[validators-deferred]\n" + "\n".join(rows) + "\n"


def _formatters_run_batch(
    applicable: Dict[str, Dict[str, Any]], path: str
) -> list:
    """Run all formatters on path. Parallel if `parallel >= 2` in config."""
    workers = _parallel_workers()
    if workers >= 2 and len(applicable) > 1:
        from concurrent.futures import ThreadPoolExecutor
        max_workers = min(workers, len(applicable))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {name: ex.submit(_formatter_run_one, name, spec, path)
                       for name, spec in applicable.items()}
            return [futures[name].result() for name in applicable]
    return [_formatter_run_one(name, spec, path) for name, spec in applicable.items()]


_ADVICE_DEFAULT_OPS = ("edit", "paste", "append", "replace", "replace_lines", "vim")


def _advice_added_text(path: str, pre_content: Optional[bytes]) -> str:
    """Text the op introduced: lines in the current file absent from
    ``pre_content``. When ``pre_content`` is None (no snapshot taken) the whole
    current file is returned — correct for a freshly created file, slightly
    broad for an in-place edit. Gates ``contains`` rules on what the op *added*,
    not on what the file already held."""
    try:
        with open(path, "rb") as f:
            post = f.read()
    except OSError:
        return ""
    if pre_content is None:
        return post.decode("utf-8", "replace")
    # Multiset diff (not set): a line duplicated by the op counts as added even
    # when an identical line already existed. Each post line consumes one pre
    # occurrence; the leftovers are what the op introduced.
    pre_counts: Dict[bytes, int] = {}
    for ln in pre_content.splitlines():
        pre_counts[ln] = pre_counts.get(ln, 0) + 1
    added = []
    for ln in post.splitlines():
        if pre_counts.get(ln, 0) > 0:
            pre_counts[ln] -= 1
        else:
            added.append(ln)
    return b"\n".join(added).decode("utf-8", "replace")


def _advice_resolve(resolve_cmd: str, path: str) -> Optional[str]:
    """Run a rule's ``resolve`` subprocess (a source→target resolver). Returns
    the target string (possibly empty) when the resolver signals "advice
    applies" via exit 3 — the would-be target rides on stderr while stdout stays
    empty so a validator reusing the same cmd still skips. Returns None to
    suppress (exit 0 = target already exists, or any error)."""
    cmd = _substitute_placeholders(resolve_cmd, {
        "supertool_dir": _INSTALL_DIR,
        "python": _python_token(),
        "file": shlex.quote(path),
    })
    _prefix_env, cmd = _extract_env_prefix(cmd)
    _merged_env = {**os.environ, **_prefix_env}
    cmd = _expand_env(cmd, _merged_env)
    try:
        r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True,
                           text=True, timeout=30,
                           env=(_merged_env if _prefix_env else None))
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 3:
        return None
    return r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ""


def _resolve_cmd_from_validators(cfg: Dict[str, Any],
                                 name: Optional[str] = None) -> Optional[str]:
    """A ``resolve`` cmd declared on a validator — lets an advice rule reuse the
    source→target resolver instead of duplicating it. ``name`` picks a specific
    validator (unambiguous when several declare a resolver); without it, the
    first validator that declares one wins."""
    validators = cfg.get("validators") or {}
    if name:
        spec = validators.get(name)
        return spec.get("resolve") if isinstance(spec, dict) else None
    for spec in validators.values():
        if isinstance(spec, dict) and spec.get("resolve"):
            return spec["resolve"]
    return None


def _eval_advice_rule(spec: Dict[str, Any], op: str, path: str,
                      pre_existed: bool, pre_content: Optional[bytes],
                      cfg: Dict[str, Any]) -> str:
    """Evaluate one advice rule. Returns the rendered advice line, or "" when
    the rule does not apply to this op/path/state."""
    if op not in (spec.get("hooks_into") or _ADVICE_DEFAULT_OPS):
        return ""
    glob = spec.get("match", "*")
    if glob and not _match_glob(path, glob):
        return ""
    when = spec.get("when", "always")
    if when == "new-file" and pre_existed:
        return ""
    if when == "existing-file" and not pre_existed:
        return ""
    contains = spec.get("contains")
    if contains:
        try:
            if not re.search(contains, _advice_added_text(path, pre_content)):
                return ""
        except re.error:
            return ""
    target = None
    rfv = spec.get("resolveFromValidator")
    if spec.get("resolve") or rfv:
        resolve_cmd = spec.get("resolve")
        if not resolve_cmd and rfv:
            resolve_cmd = _resolve_cmd_from_validators(
                cfg, rfv if isinstance(rfv, str) else None)
        if not resolve_cmd:
            return ""
        target = _advice_resolve(resolve_cmd, path)
        if target is None:
            return ""
    # One pass, so neither a resolver-produced target nor a path can be
    # re-scanned for placeholders. The {target} test reads the TEMPLATE, not the
    # substituted text — otherwise a path containing the literal string
    # "{target}" would silently pick the interpolate branch over the append one.
    # strip() on the append branch drops the leading space left when the
    # configured message is empty.
    raw_message = spec.get("message", "")
    message = _substitute_placeholders(raw_message, {
        "path": path,
        "op": op,
        "target": target or "",
    })
    if "{target}" not in raw_message and target:
        message = f"{message} — consider {target}".strip()
    return f"{mark('ℹ')} {message}".rstrip()


def _run_advice(op: str, path: str, pre_existed: bool,
                pre_content: Optional[bytes] = None) -> str:
    """Advisory (never blocks): emit config-driven hints after a mutating op.

    Rules live under the top-level ``advice`` config block. Each rule may gate
    on ``hooks_into`` (ops, default all mutating), ``match`` (path glob),
    ``when`` (new-file|existing-file|always), ``contains`` (regex over the
    content the op *added*) and ``resolve``/``resolveFromValidator`` (a
    subprocess emitting a would-be target via exit 3 + stderr). ``message`` is
    the line shown; ``{target}``/``{path}``/``{op}`` interpolate, and a bare
    ``{target}``-less message gets " — consider <target>" appended when a
    resolver produced one. Returns an ``[advice]`` block, or "" when nothing
    applies."""
    cfg = _load_config()
    rules = {name: spec for name, spec in (cfg.get("advice") or {}).items()
             if isinstance(spec, dict)}
    if not rules:
        return ""
    lines = []
    for spec in rules.values():
        line = _eval_advice_rule(spec, op, path, pre_existed, pre_content, cfg)
        if line:
            lines.append(line)
    if not lines:
        return ""
    return "\n[advice]\n" + "\n".join(lines) + "\n"


def _advice_wants_pre(op: str, path: str) -> bool:
    """True when a configured advice rule with a ``contains`` gate applies to
    this op/path. The caller snapshots pre-edit bytes so the added-content diff
    is exact even when no rollback/notifier would otherwise capture them —
    without this, ``contains`` silently falls back to whole-file matching and
    fires on content the op did not introduce."""
    for spec in (_load_config().get("advice") or {}).values():
        if not isinstance(spec, dict) or not spec.get("contains"):
            continue
        if op not in (spec.get("hooks_into") or _ADVICE_DEFAULT_OPS):
            continue
        glob = spec.get("match", "*")
        if glob and not _match_glob(path, glob):
            continue
        return True
    return False


def _run_with_validators(op: str, parts: Any, do_op: Any) -> str:
    """Wrap edit op with format+snapshot+run+diff using configured formatters/validators.

    Run order: edit → formatter(s) → validator(s) → rollback if validate fails.
    No-op when op not in _OP_TARGETS, no target path, or no applicable
    formatters/validators. Guarantees `do_op()` runs in all paths.
    """
    extract = _OP_TARGETS.get(op)
    if not extract:
        return do_op()
    # Counted here, before the op runs and whatever it returns: this is the
    # branch footer's signal, and the cases worth reporting most are the ones
    # where nothing lands on disk — a failed anchor, a validator rollback.
    _MUTATION_ATTEMPTS[0] += 1
    try:
        path = extract(parts)
    except (IndexError, TypeError):
        return do_op()
    if not path:
        return do_op()
    _pre_existed = os.path.isfile(path)
    applicable_fmt = _applicable_formatters(op, path)
    applicable_all = _applicable_validators(op, path)
    applicable_notif = _applicable_notifiers(op, path)

    # New file (#239): warm LSP daemons don't index brand-new classes, so they
    # report phantom errors. Servers opting into stopOnNewFile must be stopped
    # once this op creates the file, before ANY validator (inline OR deferred
    # slow-tier) runs against it. Computed here, fired after do_op() in whichever
    # path runs below — deferred slow validators (drained later by main()) rely
    # on this stop having already cold-restarted the daemon.
    _new_file_servers = [] if _pre_existed else _mcp_servers_to_stop_on_new_file(path)

    # Split validators into fast (run per-op) and slow (deferred to end-of-call).
    # tier="slow" validators are queued in _VALIDATOR_DEFER_QUEUE and drained by
    # main() after all ops complete. Dedup by (name, path) preserves insertion order.
    # When not in defer mode (single-op call), all validators run inline regardless of tier.
    applicable: Dict[str, Dict[str, Any]] = {}
    if _DEFER_FORMATTERS:
        for name, spec in applicable_all.items():
            if spec.get("tier", "fast") == "slow":
                key = (name, os.path.abspath(path))
                if key not in _VALIDATOR_DEFER_SEEN:
                    _VALIDATOR_DEFER_SEEN.add(key)
                    _VALIDATOR_DEFER_QUEUE.append((name, spec, os.path.abspath(path)))
            else:
                applicable[name] = spec
    else:
        applicable = applicable_all

    # Multi-op invocation: queue formatters for end-of-batch instead of
    # running inline. Tidy rules (no_unused_imports) would otherwise strip
    # symbols a later op in the same call is about to consume. Issue #164.
    if _DEFER_FORMATTERS and applicable_fmt:
        abs_path = os.path.abspath(path)
        bucket = _FORMAT_QUEUE.setdefault(abs_path, {})
        bucket.update(applicable_fmt)
        applicable_fmt = {}
    if not applicable_fmt and not applicable:
        # No validators/formatters — still need pre_content for notifier diff view
        pre_for_notif = None
        if (applicable_notif or _advice_wants_pre(op, path)) and os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    pre_for_notif = f.read()
            except OSError:
                pass
        body = do_op()
        _run_notifiers(op, path, pre_content=pre_for_notif)
        if isinstance(body, str) and body.startswith("ERROR"):
            return body
        for _srv in _new_file_servers:
            _mcp_stop_server(_srv)
        return body + _run_advice(op, path, _pre_existed, pre_for_notif)

    needs_rollback = any(v.get("rollback_on_fail") for v in applicable.values())
    needs_fmt_rollback = any(v.get("rollback_on_fail") for v in applicable_fmt.values())

    # Capture pre_content for rollback AND/OR notifier diff view
    pre_content: Optional[bytes] = None
    needs_pre = (needs_rollback or needs_fmt_rollback or bool(applicable_notif)
                or _advice_wants_pre(op, path))
    if needs_pre and os.path.isfile(path):
        try:
            with open(path, "rb") as f:
                pre_content = f.read()
        except OSError:
            pre_content = None

    before = _validators_run_batch(applicable, path) if applicable else {}

    body = do_op()

    # Fire notifiers (observers) — never blocks, never raises
    _run_notifiers(op, path, pre_content=pre_content)

    if isinstance(body, str) and body.startswith("ERROR"):
        return body

    # Run formatters after the edit, before validators.
    fmt_rows: list = []
    if applicable_fmt:
        fmt_results = _formatters_run_batch(applicable_fmt, path)
        for result in fmt_results:
            if not result["ok"]:
                result_name = result.get("name", "")
                if result_name in applicable_fmt and applicable_fmt[result_name].get("rollback_on_fail"):
                    if pre_content is not None:
                        try:
                            with open(path, "wb") as fw:
                                fw.write(pre_content)
                            _retract_write(path)
                            fmt_rows.append(f"[rolled back] {result_name} failed; file restored")
                        except OSError as e:
                            fmt_rows.append(f"[ROLLBACK FAILED] {result_name}: {e}")
                    else:
                        row = _formatter_render_row(result)
                        if row:
                            fmt_rows.append(row)
                else:
                    row = _formatter_render_row(result)
                    if row:
                        fmt_rows.append(row)
            else:
                row = _formatter_render_row(result)
                if row:
                    fmt_rows.append(row)

    # Stop warm daemons for this new file before the inline validators run, so
    # they cold-start with the file indexed (see _new_file_servers above). Same
    # list covers any deferred slow-tier validators drained later by main().
    for _srv in _new_file_servers:
        _mcp_stop_server(_srv)

    after_results = _validators_run_batch(applicable, path) if applicable else {}
    diff_lines: list = []
    for name in applicable:  # stable order from config
        if name in after_results:
            diff_lines.extend(_validator_render_diff(before.get(name), after_results[name]))

    diff_out = "\n".join(diff_lines) + ("\n" if diff_lines else "")

    if needs_rollback and pre_content is not None:
        # Decided from the result dicts, not from the rendered rows: a scan for
        # a ✗ on a line starting with the validator's name reverted `phpstan`
        # whenever `phpstan-mcp` went red, and could not tell a skip from a
        # finding at all (#406).
        for name, spec in applicable.items():
            if not spec.get("rollback_on_fail"):
                continue
            after_data = after_results.get(name)
            if after_data is None or not _validator_regressed(before.get(name), after_data):
                continue
            try:
                with open(path, "wb") as f:
                    f.write(pre_content)
                _retract_write(path)
                diff_out += f"\n[rolled back] {name} regressed; file restored\n"
            except OSError as e:
                diff_out += f"\n[ROLLBACK FAILED] {name}: {e}\n"
            break

    suffix = ""
    if fmt_rows:  # silent when all formatters are no-op
        suffix += "\n[formatters]\n" + "\n".join(fmt_rows) + "\n"
    if applicable:
        suffix += "\n[validators]\n" + diff_out

    return body + suffix + _run_advice(op, path, _pre_existed, pre_content)


# Filter sentinel: `@syntax` selects validators that declare `"syntax": true`
# in their spec (parser/compiler checks), keeping the syntax scope declarative
# in config instead of hardcoded in callers (e.g. git-resolve's digest).
_SYNTAX_FILTER_SENTINEL = "@syntax"


def _select_validators(validators: dict, tool_filter: Optional[list]) -> dict:
    """Apply a tool_filter to a validators dict.

    A plain filter keeps validators whose name is in the list. The
    ``@syntax`` sentinel keeps validators whose spec sets ``syntax: true``.
    """
    if not tool_filter:
        return validators
    if _SYNTAX_FILTER_SENTINEL in tool_filter:
        return {k: v for k, v in validators.items()
                if isinstance(v, dict) and v.get("syntax")}
    return {k: v for k, v in validators.items() if k in tool_filter}


def _validate_one_block(path: str, validators: dict, verbose: bool = False) -> List[str]:
    """Render the validator rows for a single ``path`` (no trailing newline join).

    Returns the lines for one ``validate: PATH`` block — shared by the
    single-file and multi-file forms so they stay byte-identical per file.
    """
    out = [f"validate: {path}"]
    for name, spec in validators.items():
        glob = spec.get("match", "*")
        if path and glob and not _match_glob(path, glob):
            continue
        data = _validator_run_one(name, spec, path)
        if data is None:
            continue
        out.extend(_validator_render_row(data, verbose=verbose))
    return out


def op_validate(path: str, tool_filter: Optional[list] = None, verbose: bool = False) -> str:
    """Manual one-shot: run validators on ``path``, render current state (no diff).

    verbose=True: show all errors (no cap) and raw adapter output when available.
    """
    if not path:
        return "ERROR: validate requires file path\n"
    cfg = _load_config()
    validators = cfg.get("validators") or {}
    if not validators:
        return "no validators configured\n"
    if tool_filter:
        validators = _select_validators(validators, tool_filter)
        if not validators:
            return "no validators matched filter\n"
    return "\n".join(_validate_one_block(path, validators, verbose=verbose)) + "\n"


def op_validate_multi(paths: list, tool_filter: Optional[list] = None,
                      verbose: bool = False) -> str:
    """List form: validate several files in one invocation.

    Renders one ``validate: PATH`` block per file, in order, so a caller can
    fold each block back to its source file. Config is loaded once for the whole
    batch — the throughput win over shelling ``validate:PATH`` per file.

    A single-element list is byte-identical to ``op_validate(paths[0], …)``.
    """
    paths = [p for p in (paths or []) if p]
    if not paths:
        return "ERROR: validate requires file path\n"
    cfg = _load_config()
    validators = cfg.get("validators") or {}
    if not validators:
        return "no validators configured\n"
    if tool_filter:
        validators = _select_validators(validators, tool_filter)
        if not validators:
            return "no validators matched filter\n"
    blocks: List[str] = []
    for path in paths:
        blocks.append("\n".join(_validate_one_block(path, validators, verbose=verbose)))
    return "\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# LSP-backed single-file ops: diag, hover, rename
#
# All three delegate to the MCP server configured for the file's extension via
# the `mcp` block in .supertool.json. Without an MCP route the op returns a
# clear "no LSP configured" message — no heuristic fallback (these ops only
# make sense with a real language server).
# ---------------------------------------------------------------------------

# Patterns that mark an MCP text result as an infrastructure condition (timeout,
# overload) rather than a real tool result. Some servers (cclsp) swallow their own
# timeout and return it as normal text content with the `isError` flag unset —
# these patterns catch that case. Overridable per server via
# mcp.<name>.infra_patterns in .supertool.json. See #346.
_MCP_INFRA_DEFAULT_PATTERNS = ("orchestrator timeout", "timed out after")


def _mcp_result_text(result: object) -> str:
    """Join the text content items of an MCP tool result into one string."""
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        texts = [item.get("text", "") for item in content
                 if isinstance(item, dict) and item.get("type") == "text"]
        return "\n".join(t for t in texts if t)
    return ""


def _mcp_result_is_infra(result: object, patterns: Iterable[str]) -> bool:
    """True if an MCP tool result is an infra condition, not real content.

    Two signals, in order:
      1. structural — the MCP `isError` flag (spec-standard, any server).
      2. textual — the content matches a configured infra pattern, for servers
         that report a timeout/overload as normal text with isError unset.
    """
    if not isinstance(result, dict):
        return False
    if result.get("isError"):
        return True
    text = _mcp_result_text(result).lower()
    return bool(text) and any(p.lower() in text for p in patterns)


def _mcp_call_or_message(op_name: str, file_path: str, args: dict) -> str:
    """Shared dispatch for diag/hover/rename. Returns the MCP text result or a
    diagnostic message if no route / no server / call failed.

    Infra conditions (timeout/overload) are returned prefixed `op_name: ...` —
    same shape as our own errors — so adapters (lsp-diag) drop them via their
    op_name-guard instead of counting them as findings. See #346.
    """
    if not file_path:
        return f"{op_name}: missing file path\n"
    route = _mcp_route(file_path, op_name)
    if route is None:
        return f"{op_name}: no LSP configured for {file_path} (add mcp.{op_name} mapping in .supertool.json)\n"
    server_name, mcp_tool = route
    server = _mcp_ensure_server(server_name)
    if server is None:
        return f"{op_name}: MCP server '{server_name}' unavailable\n"
    try:
        result = _mcp_call(server_name, mcp_tool, args)
    except (MCPServerError, MCPTimeout) as e:
        return f"{op_name}: MCP error: {e}\n"
    if result is None:
        return f"{op_name}: no result from {mcp_tool}\n"
    # Infra condition (timeout/overload) → prefix it so adapters drop it (#346).
    infra_patterns = _mcp_specs.get(server_name, {}).get(
        "infra_patterns", _MCP_INFRA_DEFAULT_PATTERNS)
    if _mcp_result_is_infra(result, infra_patterns):
        text = _mcp_result_text(result).strip() or "infra condition"
        return f"{op_name}: {text}\n"
    # Pull text content (most common MCP response shape)
    text = _mcp_result_text(result)
    if text:
        return text.rstrip("\n") + "\n"
    return json.dumps(result, indent=2) + "\n"


def op_diag(file_path: str) -> str:
    """LSP diagnostics (errors/warnings) for FILE. Requires `mcp.<server>.tools.diag` mapping."""
    return _mcp_call_or_message("diag", file_path, {"file_path": os.path.abspath(file_path) if file_path else ""})


def op_hover(symbol: str, file_path: str) -> str:
    """LSP hover info (type, signature, doc) for SYMBOL in FILE.

    Two-step internally:
      1. find_workspace_symbols(query=symbol) → first match's (file, line, character)
      2. get_hover(file_path, line, character) → text result

    Some MCP/LSP servers (cclsp) require position-based hover. This op hides that.
    Requires both `tools.resolve` (or `tools.hover_resolve`) and `tools.hover` mappings.
    """
    if not symbol or not file_path:
        return "hover: usage hover:SYMBOL:FILE\n"
    abs_file = os.path.abspath(file_path)

    # Step 1: locate the symbol via workspace symbols. Use the configured `resolve`
    # tool — it's expected to be find_workspace_symbols (returns 'at /path:line:col').
    resolve_route = _mcp_route(file_path, "resolve")
    if resolve_route is None:
        return "hover: no resolve mapping (needed to locate symbol position) — add mcp.<server>.tools.resolve\n"
    rs_server, rs_tool = resolve_route
    server = _mcp_ensure_server(rs_server)
    if server is None:
        return f"hover: MCP server '{rs_server}' unavailable\n"
    try:
        rs_result = _mcp_call(rs_server, rs_tool, {
            "query": symbol, "symbol_name": symbol, "file_path": abs_file,
        })
    except (MCPServerError, MCPTimeout) as e:
        return f"hover: locate failed: {e}\n"
    if rs_result is None:
        return f"hover: '{symbol}' not found in workspace\n"

    # Parse "at /path:line:character" from text content; prefer same-file matches
    pos: Optional[Tuple[str, int, int]] = None
    fallback_pos: Optional[Tuple[str, int, int]] = None
    content = rs_result.get("content") if isinstance(rs_result, dict) else None
    if isinstance(content, list):
        for item in content:
            text = item.get("text", "") if isinstance(item, dict) else ""
            for m in re.finditer(r"\sat\s+(\S+?):(\d+):(\d+)", text):
                p_file, p_line, p_char = m.group(1), int(m.group(2)), int(m.group(3))
                cand = (p_file, p_line, p_char)
                if os.path.abspath(p_file) == abs_file:
                    pos = cand; break
                if fallback_pos is None:
                    fallback_pos = cand
            if pos: break
    if pos is None:
        pos = fallback_pos
    if pos is None:
        return f"hover: '{symbol}' not found (no position in resolve result)\n"

    target_file, line, character = pos

    # The line:col from find_workspace_symbols often points at the declaration start
    # (e.g. `public` keyword) — LSP hover at that column returns nothing. Re-anchor
    # to the actual identifier offset within the source line. Use word-boundary regex
    # so `handle` doesn't match the param `$handle` instead of the method name.
    try:
        with open(target_file, "rb") as f:
            src_lines = f.readlines()
        if 0 < line <= len(src_lines):
            src = src_lines[line - 1].decode("utf-8", errors="replace")
            m = re.search(r"\b" + re.escape(symbol) + r"\b", src)
            if m:
                character = m.start() + 1  # 1-indexed
    except OSError:
        pass

    # Step 2: hover at position
    return _mcp_call_or_message("hover", file_path, {
        "file_path": os.path.abspath(target_file),
        "line": line, "character": character,
    })


def op_rename(old_symbol: str, new_symbol: str, file_path: str) -> str:
    """LSP workspace rename: OLD_SYMBOL → NEW_SYMBOL across the workspace. Requires `mcp.<server>.tools.rename` mapping.

    The MCP server applies changes across all affected files (cclsp's rename_symbol
    writes .bak backups). Returns the server's report of modified files.
    """
    if not old_symbol or not new_symbol:
        return "rename: usage rename:OLD_SYMBOL:NEW_SYMBOL:FILE\n"
    return _mcp_call_or_message("rename", file_path, {
        "symbol_name": old_symbol, "query": old_symbol, "new_name": new_symbol,
        "file_path": os.path.abspath(file_path) if file_path else "",
    })


# ---------------------------------------------------------------------------
# workspace — one-shot IDE-style view of a single file
# ---------------------------------------------------------------------------

# Symbols that are too common to run an unrestricted reference search on.
_WORKSPACE_COMMON_SYMBOLS = frozenset({
    "index", "main", "init", "__init__", "app", "base", "utils", "helpers",
    "helper", "config", "settings", "common", "core", "util", "test",
    "tests", "setup", "models", "model", "views", "view", "routes",
})

# Extension family map for workspace References scan.
# A file with ext X searches for references in files matching any ext in the family.
# Default: same-ext only (handled by the fallback in op_workspace).
_EXT_FAMILIES: Dict[str, tuple] = {
    ".ts":   (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
    ".tsx":  (".ts", ".tsx", ".js", ".jsx"),
    ".js":   (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"),
    ".jsx":  (".js", ".jsx", ".ts", ".tsx"),
    ".mjs":  (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"),
    ".cjs":  (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"),
    ".php":  (".php",),  # DVSI's .class.php matched via endswith(".php")
    ".py":   (".py", ".pyi"),
    ".pyi":  (".py", ".pyi"),
}


# ---------------------------------------------------------------------------
# op_resolve — smart-glob "go to definition" resolver
# ---------------------------------------------------------------------------

def op_resolve(symbol: str, from_file: Optional[str] = None, _cache: Optional[dict] = None) -> str:
    """Resolve a symbol/import string to a project file path.

    Detection rules (in priority order):
      - Contains backslash → PHP FQN  → **/<path>.class.php, fallback **/<path>.php
      - Starts with one or more dots (Python relative import, e.g. ".", ".utils") →
        resolve relative to from_file's directory when provided; otherwise → external
      - Contains dot only (no / or ./) → Python dotted import → **/<path>.py
      - Starts with ./ or ../ → relative path → try common extensions
      - Bare word (no separators) → ambiguous → try multi-ext glob
      - Otherwise → external (npm/pip/etc.)

    Args:
        symbol: The import/symbol string to resolve.
        from_file: Optional path to the file that contains the import (used for
            Python relative imports like "." or ".utils"). Without this, relative
            Python imports return "external".
        _cache: Optional dict used as a per-call resolve cache (avoids repeated
            full-repo globs for the same symbol within a single workspace call).

    Returns: "SYMBOL → PATH" on success, "SYMBOL → external", or "SYMBOL → not found".
    """
    if not symbol:
        return "resolve: empty symbol\n"

    # Per-call cache: key is (symbol, from_file)
    if _cache is not None:
        cache_key = (symbol, from_file)
        if cache_key in _cache:
            return _cache[cache_key]

    result = _op_resolve_inner(symbol, from_file)

    if _cache is not None:
        _cache[cache_key] = result

    return result


def _op_resolve_inner(symbol: str, from_file: Optional[str] = None) -> str:
    """Core resolve logic — called by op_resolve (which handles caching)."""
    excl = _get_exclude_paths("resolve")

    # MCP route (sub-PR 2): if a configured LSP MCP matches this file's extension,
    # try it first. Falls through to heuristic glob on miss/error.
    if from_file:
        route = _mcp_route(from_file, "resolve")
        if route:
            server_name, mcp_tool = route
            server = _mcp_ensure_server(server_name)
            if server is not None:
                try:
                    # Send under multiple naming conventions so the tool picks what it needs:
                    # cclsp find_definition uses symbol_name/file_path, find_workspace_symbols uses query.
                    result = _mcp_call(server_name, mcp_tool, {
                        "symbol_name": symbol, "file_path": from_file, "query": symbol,
                    })
                    if result is not None:
                        path = _extract_path_from_mcp_result(result)
                        if path:
                            return f"{symbol} → {path}\n"
                except (MCPServerError, MCPTimeout):
                    pass

    # ── PHP FQN (contains backslash) ─────────────────────────────────────────
    if "\\" in symbol:
        fqn_path = symbol.replace("\\", "/")
        basename = fqn_path.rsplit("/", 1)[-1]
        # _glob_files doesn't deep-match `**/dir1/dir2/file`, so glob by basename
        # then filter to candidates whose path ends with the FQN suffix.
        for ext in (".class.php", ".php"):
            suffix = f"/{fqn_path}{ext}"
            hits = _glob_files(f"**/{basename}{ext}", excl)
            for h in hits:
                norm = os.path.normpath(h).replace(os.sep, "/")
                if norm.endswith(suffix) or norm == f"{fqn_path}{ext}":
                    return f"{symbol} → {_safe_relpath(h)}\n"
        return f"{symbol} → not found\n"

    # ── Python relative import (starts with one or more dots, no /) ──────────
    # Matches: ".", ".utils", "..models", ".sub.module" etc.
    # Does NOT match "./" or "../" (those are handled below as relative paths).
    if re.match(r"^\.+\w*(?:\.\w+)*$", symbol) or symbol in (".", ".."):
        if not from_file:
            return f"{symbol} → external\n"
        base_dir = os.path.dirname(os.path.abspath(from_file))
        # Strip leading dots to find the module name; count dots for package depth
        # Single dot: same package. ".utils" → utils in same dir.
        # ".." / "..models" → parent package (we resolve one level up per leading dot beyond 1)
        m = re.match(r"^(\.+)(.*)", symbol)
        if not m:
            return f"{symbol} → external\n"
        dots, rest = m.group(1), m.group(2)
        # Each extra dot beyond the first means go up one directory
        target_dir = base_dir
        for _ in range(len(dots) - 1):
            target_dir = os.path.dirname(target_dir)
        if rest:
            module_path = rest.replace(".", "/")
            for ext in (".py", ".pyi"):
                candidate = os.path.join(target_dir, module_path + ext)
                if os.path.isfile(candidate):
                    rel = _safe_relpath(candidate)
                    return f"{symbol} → {rel}\n"
            # Also try as a package (directory with __init__.py)
            pkg_init = os.path.join(target_dir, module_path, "__init__.py")
            if os.path.isfile(pkg_init):
                rel = _safe_relpath(pkg_init)
                return f"{symbol} → {rel}\n"
            return f"{symbol} → not found\n"
        else:
            # Bare "." or ".." — refers to the package itself
            pkg_init = os.path.join(target_dir, "__init__.py")
            if os.path.isfile(pkg_init):
                rel = _safe_relpath(pkg_init)
                return f"{symbol} → {rel}\n"
            return f"{symbol} → not found\n"

    # ── Python dotted import (dots but no / and not starting with ./ or ../) ─
    if "." in symbol and "/" not in symbol and not symbol.startswith("."):
        py_path = symbol.replace(".", "/")
        basename = py_path.rsplit("/", 1)[-1]
        suffix = f"/{py_path}.py"
        hits = _glob_files(f"**/{basename}.py", excl)
        for h in hits:
            norm = os.path.normpath(h).replace(os.sep, "/")
            if norm.endswith(suffix) or norm == f"{py_path}.py":
                return f"{symbol} → {_safe_relpath(h)}\n"
        return f"{symbol} → not found\n"

    # ── Relative path (starts with ./ or ../) ────────────────────────────────
    if symbol.startswith("./") or symbol.startswith("../"):
        base = symbol
        # Try adding common extensions if no extension present
        if not os.path.splitext(base)[1]:
            for ext in (".ts", ".tsx", ".js", ".jsx", ".py", ".php"):
                candidate = base + ext
                if os.path.isfile(candidate):
                    rel = _safe_relpath(candidate)
                    return f"{symbol} → {rel}\n"
            # Also try .class.php
            candidate = base + ".class.php"
            if os.path.isfile(candidate):
                rel = _safe_relpath(candidate)
                return f"{symbol} → {rel}\n"
        else:
            if os.path.isfile(base):
                rel = _safe_relpath(base)
                return f"{symbol} → {rel}\n"
        return f"{symbol} → not found\n"

    # ── Bare word (no separators at all) ─────────────────────────────────────
    if re.match(r"^[A-Za-z0-9_-]+$", symbol):
        for pat in (
            f"**/{symbol}.ts", f"**/{symbol}.tsx",
            f"**/{symbol}.js", f"**/{symbol}.jsx",
            f"**/{symbol}.py", f"**/{symbol}.php",
            f"**/{symbol}.class.php",
        ):
            hits = _glob_files(pat, excl)
            if hits:
                rel = _safe_relpath(hits[0])
                return f"{symbol} → {rel}\n"
        return f"{symbol} → not found\n"

    # ── Everything else — treat as external ───────────────────────────────────
    return f"{symbol} → external\n"


# ---------------------------------------------------------------------------
# Import parser helpers for op_workspace
# ---------------------------------------------------------------------------

_PHP_USE_RE = re.compile(
    r"^\s*use\s+(?:function\s+|const\s+)?([\w\\]+)(?:\s+as\s+(\w+))?\s*;", re.MULTILINE
)
_PY_FROM_RE = re.compile(
    r"^\s*from\s+(\.+\w*(?:\.\w+)*|\w+(?:\.\w+)*)\s+import", re.MULTILINE
)
_PY_IMPORT_RE = re.compile(
    r"^\s*import\s+([\w.]+)", re.MULTILINE
)
_JS_FROM_RE = re.compile(
    r"""^\s*import\s+.*?from\s+['"]([^'"]+)['"]""", re.MULTILINE
)
_JS_BARE_RE = re.compile(
    r"""^\s*import\s+['"]([^'"]+)['"]""", re.MULTILINE
)


def _parse_imports(path: str, content: str) -> List[tuple]:  # noqa: same signature, from_file is path
    """Return list of (symbol, alias_or_None) pairs for the file's import statements."""
    ext = os.path.splitext(path)[1].lower()
    results: List[tuple] = []
    seen: set = set()

    if ext == ".php":
        for m in _PHP_USE_RE.finditer(content):
            sym = m.group(1)
            alias = m.group(2)
            key = (sym, alias)
            if key not in seen:
                seen.add(key)
                results.append(key)
    elif ext == ".py":
        for m in _PY_FROM_RE.finditer(content):
            sym = m.group(1)
            key = (sym, None)
            if key not in seen:
                seen.add(key)
                results.append(key)
        for m in _PY_IMPORT_RE.finditer(content):
            sym = m.group(1)
            key = (sym, None)
            if key not in seen:
                seen.add(key)
                results.append(key)
    elif ext in (".js", ".jsx", ".ts", ".tsx"):
        for m in _JS_FROM_RE.finditer(content):
            sym = m.group(1)
            key = (sym, None)
            if key not in seen:
                seen.add(key)
                results.append(key)
        for m in _JS_BARE_RE.finditer(content):
            sym = m.group(1)
            key = (sym, None)
            if key not in seen:
                seen.add(key)
                results.append(key)

    return results


def op_workspace(path: str) -> str:
    """One-shot IDE-style view: file + symbols + validators + siblings + git + references + tests.

    Sections (in order):
      ## File: PATH       full read (1000-line cap)
      ## Symbols          map: output
      ## Validators       op_validate output (skipped when no validators)
      ## Siblings         ls of dirname (skipped when dirname == cwd root)
      ## Git              branch, file status, recent commits, blame contributors
      ## References       grep for main symbol across project
      ## Tests            matching test file info (PHP / Python)
    """
    if not os.path.isfile(path):
        return f"workspace: {path} not found\n"

    out: List[str] = []

    # ── Section 1: File ──────────────────────────────────────────────────────
    out.append(f"## File: {path}\n\n")
    # Use render_file directly with 1000-line cap (bypass rtk for consistency)
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            raw_lines = f.read().splitlines(keepends=True)
    except OSError as e:
        out.append(f"ERROR: could not read {path}: {e}\n\n")
        raw_lines = []
        size = 0

    line_count = len(raw_lines)
    _WS_LINE_CAP = 1000
    out.append(f"({line_count} lines, {size} bytes){_path_meta_suffix(path, b''.join(raw_lines[:64]))}\n")
    shown = min(line_count, _WS_LINE_CAP)
    for i in range(shown):
        try:
            line = raw_lines[i].decode("utf-8", errors="replace")
        except Exception:
            line = "<binary line>\n"
        out.append(f"{i + 1:>6}→{line}")
    if line_count > _WS_LINE_CAP:
        out.append(f"... ({line_count - _WS_LINE_CAP} more lines — use read:{path}:OFFSET:LIMIT)\n")
    else:
        out.append("[complete file — no more lines]\n")
    out.append("\n")

    # ── Section 1.5: Diagnostics (LSP, only when configured) ────────────────
    _diag_route = _mcp_route(path, "diag")
    if _diag_route:
        _diag_server_name, _diag_mcp_tool = _diag_route
        _diag_server = _mcp_ensure_server(_diag_server_name)
        if _diag_server:
            try:
                _diag_result = _mcp_call(_diag_server_name, _diag_mcp_tool,
                                         {"file_path": os.path.abspath(path)})
                if isinstance(_diag_result, dict):
                    _diag_text = _extract_symbols_from_mcp_result(_diag_result)
                    if _diag_text and _diag_text.strip():
                        out.append("## Diagnostics\n\n")
                        out.append(_diag_text)
                        if not _diag_text.endswith("\n"):
                            out.append("\n")
                        out.append("\n")
            except (MCPServerError, MCPTimeout):
                pass

    # ── Section 2: Symbols ───────────────────────────────────────────────────
    out.append("## Symbols\n\n")
    _sym_mcp_used = False
    _sym_route = _mcp_route(path, "symbols")
    if _sym_route:
        _sym_server_name, _sym_mcp_tool = _sym_route
        _sym_server = _mcp_ensure_server(_sym_server_name)
        if _sym_server:
            try:
                _sym_mcp_result = _mcp_call(_sym_server_name, _sym_mcp_tool, {"file_path": os.path.abspath(path)})
                if _sym_mcp_result is not None:
                    _sym_text = _extract_symbols_from_mcp_result(_sym_mcp_result)
                    if _sym_text is not None:
                        out.append(_sym_text)
                        _sym_mcp_used = True
            except (MCPServerError, MCPTimeout):
                pass
    if not _sym_mcp_used:
        out.append(op_map(path))
    out.append("\n")

    # ── Section 3: Imports ───────────────────────────────────────────────────
    # Read file content for import parsing (already read above into raw_lines)
    try:
        file_content = "".join(
            ln.decode("utf-8", errors="replace") for ln in raw_lines
        )
    except Exception:
        file_content = ""

    _imports = _parse_imports(path, file_content)
    if _imports:
        _imports = _imports[:40]  # cap at 40 entries
        _resolve_cache: dict = {}
        out.append(f"## Imports ({len(_imports)})\n\n")
        for sym, alias in _imports:
            resolved_line = op_resolve(sym, from_file=path, _cache=_resolve_cache).strip()
            # resolved_line is "SYMBOL → PATH" — extract just the path part
            arrow_idx = resolved_line.find(" → ")
            resolved_path = resolved_line[arrow_idx + 3:] if arrow_idx != -1 else resolved_line
            label = f"{sym} (as {alias})" if alias else sym
            out.append(f"  {label:<50} → {resolved_path}\n")
        out.append("\n")

    # ── Section 4: Validators ────────────────────────────────────────────────
    cfg = _load_config()
    validators = cfg.get("validators") or {}
    if validators:
        out.append("## Validators\n\n")
        out.append(op_validate(path, verbose=True))
        out.append("\n")

    # ── Section 5: Siblings ──────────────────────────────────────────────────
    dirname = os.path.dirname(os.path.abspath(path))
    cwd = os.path.abspath(os.getcwd())
    if dirname != cwd:
        my_name = os.path.basename(path)
        try:
            entries = [e for e in os.listdir(dirname) if not e.startswith(".")]
        except OSError:
            entries = []
        # Skip the section entirely when there are no real siblings — only
        # the input file itself, or an empty/unreadable dir.
        real_siblings = [e for e in entries if e != my_name]
        if real_siblings:
            out.append("## Siblings\n\n")
            ls_out = op_ls(dirname)
            # Mark the input file with "← me" for orientation. Match either
            # bare basename or basename+"/" (op_ls suffixes directories).
            marked_lines = []
            for line in ls_out.splitlines():
                stripped = line.rstrip()
                if stripped == my_name or stripped == my_name + "/":
                    marked_lines.append(f"{line}  ← me")
                else:
                    marked_lines.append(line)
            out.append("\n".join(marked_lines))
            if not ls_out.endswith("\n"):
                out.append("\n")
            out.append("\n")

    # ── Section 6: Git ───────────────────────────────────────────────────────
    # Check if inside a git repo
    try:
        git_check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
        )
        in_git = git_check.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        in_git = False

    if in_git:
        out.append("## Git\n\n")

        # Branch + ahead/behind
        try:
            branch_r = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            branch = branch_r.stdout.strip() if branch_r.returncode == 0 else "?"
            # ahead/behind
            ab_r = subprocess.run(
                ["git", "rev-list", "--left-right", "--count", f"{branch}...@{{u}}"],
                capture_output=True, text=True, timeout=5,
            )
            if ab_r.returncode == 0 and ab_r.stdout.strip():
                parts_ab = ab_r.stdout.strip().split()
                ahead, behind = (parts_ab + ["0", "0"])[:2]
                out.append(f"branch: {branch}  ahead {ahead}  behind {behind}\n")
            else:
                out.append(f"branch: {branch}\n")
        except (subprocess.TimeoutExpired, OSError):
            out.append("branch: (git error)\n")

        # File git status
        try:
            status_r = subprocess.run(
                ["git", "status", "--porcelain", path],
                capture_output=True, text=True, timeout=5,
            )
            if status_r.returncode == 0:
                status_line = status_r.stdout.strip()
                if status_line:
                    xy = status_line[:2]
                    if xy[0] in "MADRC":
                        file_status = "staged"
                    elif xy[1] in "MD":
                        file_status = "modified"
                    else:
                        file_status = status_line.strip()
                else:
                    file_status = "clean"
                out.append(f"file status: {file_status}\n")
        except (subprocess.TimeoutExpired, OSError):
            pass

        # Recent commits touching PATH
        try:
            log_r = subprocess.run(
                ["git", "log", "--oneline", "-5", "--", path],
                capture_output=True, text=True, timeout=5,
            )
            if log_r.returncode == 0 and log_r.stdout.strip():
                out.append("recent commits:\n")
                for line in log_r.stdout.strip().splitlines():
                    # Truncate runaway subjects (Kevin commits sometimes list
                    # hundreds of files in the subject). Keep ~120 chars.
                    if len(line) > 120:
                        line = line[:117] + "..."
                    out.append(f"  {line}\n")
        except (subprocess.TimeoutExpired, OSError):
            pass

        # Top blame contributors
        try:
            blame_r = subprocess.run(
                ["git", "blame", "--line-porcelain", path],
                capture_output=True, text=True, timeout=15,
            )
            if blame_r.returncode == 0 and blame_r.stdout:
                author_counts: Dict[str, int] = {}
                total_blame_lines = 0
                for bline in blame_r.stdout.splitlines():
                    if bline.startswith("author "):
                        author = bline[7:].strip()
                        author_counts[author] = author_counts.get(author, 0) + 1
                        total_blame_lines += 1
                if author_counts and total_blame_lines > 0:
                    top3 = sorted(author_counts.items(), key=lambda x: -x[1])[:3]
                    out.append("top contributors:\n")
                    for author, count in top3:
                        pct = round(100 * count / total_blame_lines)
                        out.append(f"  {author} ({pct}%)\n")
        except (subprocess.TimeoutExpired, OSError):
            pass

        out.append("\n")

    # ── Section 7: References ────────────────────────────────────────────────
    basename = os.path.basename(path)
    ext = os.path.splitext(basename)[1]  # e.g. ".php"
    # Strip extension. For "Foo.class.php" → "Foo.class" → strip again by splitext
    symbol = os.path.splitext(basename)[0]  # strips last extension
    # For "Foo.class.php" → symbol = "Foo.class"; strip another extension if still has one
    if "." in symbol:
        symbol = os.path.splitext(symbol)[0]

    display_cap = 20
    noisy_note = ""
    if symbol.lower() in _WORKSPACE_COMMON_SYMBOLS:
        display_cap = 10
        noisy_note = f"  (common symbol — results may be noisy)\n"

    # Grep with a high internal cap so we can show "X of Y" in the header.
    # Tests live in the dedicated ## Tests section — exclude them here so
    # the quota goes to production usages.
    _refs_mcp_used = False
    _refs_route = _mcp_route(path, "refs")
    if _refs_route:
        _refs_server_name, _refs_mcp_tool = _refs_route
        _refs_server = _mcp_ensure_server(_refs_server_name)
        if _refs_server:
            try:
                _refs_mcp_result = _mcp_call(_refs_server_name, _refs_mcp_tool, {"symbol_name": symbol, "file_path": os.path.abspath(path)})
                if _refs_mcp_result is not None:
                    _mcp_refs = _extract_refs_from_mcp_result(_refs_mcp_result)
                    if _mcp_refs is not None:
                        filtered_hits = _mcp_refs
                        _refs_mcp_used = True
            except (MCPServerError, MCPTimeout):
                pass
    if not _refs_mcp_used:
        excl = _get_exclude_paths("grep")
        # hits now (file, lineno, content) tuples — drive-letter safe.
        hits_tuples = _grep_recursive(symbol, ".", 200, excl)
        abs_path = os.path.abspath(path)
        ext_family = _EXT_FAMILIES.get(ext, (ext,)) if ext else ()
        _test_marker = re.compile(r"(?:^|/)(?:test_[^/]+|[^/]+_test|[^/]+Test)\.[^/]+$")
        filtered_hits = []
        for hit_file, lineno, content in hits_tuples:
            if os.path.abspath(hit_file) == abs_path:
                continue
            if ext_family and not any(hit_file.endswith(e) for e in ext_family):
                continue
            if _test_marker.search(hit_file):
                continue
            filtered_hits.append(f"{hit_file}:{lineno}:{content}")

    total = len(filtered_hits)
    shown = filtered_hits[:display_cap]
    if total > display_cap:
        out.append(f"## References (showing {len(shown)} of {total})\n\n")
    else:
        out.append(f"## References ({total})\n\n")

    if noisy_note:
        out.append(noisy_note)

    if shown:
        current_file = ""
        for hit in shown:
            # Drive-letter aware split — skip leading `X:` if a Windows path.
            _start = 2 if len(hit) > 2 and hit[1] == ":" and hit[0].isalpha() else 0
            colon1 = hit.index(":", _start)
            rest = hit[colon1 + 1:]
            colon2 = rest.index(":")
            hit_file = hit[:colon1]
            lineno = rest[:colon2]
            content = rest[colon2 + 1:]
            if hit_file != current_file:
                current_file = hit_file
                out.append(f"{hit_file}\n")
            out.append(f"  {lineno}:{content}\n")
    else:
        out.append(f"(no references to {symbol!r} found in *{ext} files)\n")
    out.append("\n")

    # ── Section 8: Tests ─────────────────────────────────────────────────────
    _ws_test_path: Optional[str] = None

    if ext == ".php":
        # PHP: look for *Test.php matching the base symbol
        test_pattern = f"**/{symbol}Test.php"
        from glob import glob as _glob
        candidates = _glob(test_pattern, recursive=True)
        if candidates:
            _ws_test_path = candidates[0]
    elif ext == ".py":
        # Python: test_*.py or *_test.py matching the symbol
        sym_lower = symbol.lower()
        for tpat in (f"**/test_{sym_lower}.py", f"**/{sym_lower}_test.py",
                     f"**/test_{symbol}.py", f"**/{symbol}_test.py"):
            from glob import glob as _glob
            candidates = _glob(tpat, recursive=True)
            if candidates:
                _ws_test_path = candidates[0]
                break

    if _ws_test_path and os.path.isfile(_ws_test_path):
        out.append("## Tests\n\n")
        try:
            test_lines = _count_lines(_ws_test_path)
            test_mtime = os.path.getmtime(_ws_test_path)
            test_mtime_str = datetime.fromtimestamp(test_mtime).strftime("%Y-%m-%d %H:%M")
            out.append(f"{_ws_test_path}  ({test_lines} lines, last modified {test_mtime_str})\n")
        except OSError:
            out.append(f"{_ws_test_path}\n")
        out.append("\n")

    return "".join(out)


def op_format(path: str, tool_filter: Optional[list] = None, verbose: bool = False,
              gated: bool = False) -> str:
    """Manual one-shot: run formatters on ``path``, render ok/fail + duration.

    verbose=True: show the formatter's full error message (untruncated) and
    a ``[verbose]`` marker on the row so callers can distinguish the mode.

    gated=True applies the #393 repo opt-in rule. Off by default and on for
    `format_staged`, which is the honest split: `format:PATH` names one file,
    so the caller has already said what they want done to it and a tool that
    silently declined would be the wrong answer. `format_staged` sweeps files
    nobody named, frequently from a pre-commit hook, which is the same shape
    as the post-edit hook the gate was written for.
    """
    if not path:
        return "ERROR: format requires file path\n"
    cfg = _load_config()
    formatters = cfg.get("formatters") or {}
    if not formatters:
        return "no formatters configured\n"
    if tool_filter:
        formatters = {k: v for k, v in formatters.items() if k in tool_filter}
        if not formatters:
            return "no formatters matched filter\n"
    import fnmatch
    out = [f"format: {path}"]
    matched = False
    for name, spec in formatters.items():
        if not isinstance(spec, dict):
            continue
        glob = spec.get("match", "*")
        if path and glob and not _match_glob(path, glob):
            continue
        if gated and not _repo_opts_into_formatter(name, spec, path):
            matched = True
            out.append(
                f"\n  {name}: skipped — no config for it in this file's repo (#393)"
            )
            continue
        matched = True
        result = _formatter_run_one(name, spec, path)
        row = _formatter_render_row(result)
        if row is None:
            # no-op: show a muted marker in manual mode so the user knows it ran
            name_key = result.get("name") or result.get("tool") or name
            dur = result.get("duration_ms", 0)
            row = f"{name_key:8s}: ok (no-op)  ({dur}ms)"
        if verbose:
            row = row + "  [verbose]"
            errors = result.get("errors") or []
            out.append(row)
            for e in errors:
                line_n = f"L{e['line']}" if e.get("line") else "  "
                code = e.get("code") or ""
                msg = (e.get("msg") or "").strip().replace("\n", " ")
                out.append(f"  {line_n} {code}  {msg}")
        else:
            out.append(row)
    if not matched:
        out.append("(no formatters matched this file)")
    return "\n".join(out) + "\n"


def op_validate_staged(tool_filter: Optional[list] = None, verbose: bool = False) -> str:
    """Run validators on every currently staged file.

    verbose=True: passed through to op_validate for each file — shows all errors
    and raw adapter output instead of the compact capped form.

    #150: uses `git diff -z` for NUL-separated names (filenames with newlines /
    quotes survive intact) and rejects symlinks (staged symlink to /etc/passwd
    would otherwise be passed to validators that could process it).
    """
    import subprocess
    try:
        r = subprocess.run(
            ["git", "diff", "--cached", "-z", "--name-only", "--diff-filter=ACMR"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            msg = (r.stderr.strip() or "git diff failed")
            return f"ERROR: {msg}\n"
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"ERROR: git unavailable: {e}\n"

    # Split on NUL (git diff -z), reject empty + symlinks + paths outside cwd.
    staged = []
    for p in r.stdout.split("\x00"):
        if not p or os.path.islink(p) or not os.path.isfile(p):
            continue
        # Reject paths that resolve outside cwd (symlink-following could leak).
        real = os.path.realpath(p)
        root = os.path.realpath(os.getcwd())
        if real != root and not real.startswith(root + os.sep):
            continue
        staged.append(p)
    if not staged:
        return "no staged files\n"

    parts = []
    for fpath in staged:
        parts.append(f"validate_staged: {fpath}")
        block = op_validate(fpath, tool_filter, verbose=verbose)
        # indent the block for readability
        for line in block.splitlines():
            parts.append(f"  {line}")
    return "\n".join(parts) + "\n"


def op_format_staged(tool_filter: Optional[list] = None, verbose: bool = False) -> str:
    """Run formatters on every currently staged file.

    verbose=True: passed through to op_format for each file — shows full error
    messages and a [verbose] marker instead of the compact truncated form.

    #150: uses `git diff -z` for NUL-separated names and rejects symlinks —
    a staged symlink to /etc/hosts would otherwise be REWRITTEN by formatters
    (prettier, php-cs-fixer, etc.).
    """
    import subprocess
    try:
        r = subprocess.run(
            ["git", "diff", "--cached", "-z", "--name-only", "--diff-filter=ACMR"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            msg = (r.stderr.strip() or "git diff failed")
            return f"ERROR: {msg}\n"
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"ERROR: git unavailable: {e}\n"

    staged = []
    for p in r.stdout.split("\x00"):
        if not p or os.path.islink(p) or not os.path.isfile(p):
            continue
        real = os.path.realpath(p)
        root = os.path.realpath(os.getcwd())
        if real != root and not real.startswith(root + os.sep):
            continue
        staged.append(p)
    if not staged:
        return "no staged files\n"

    parts = []
    for fpath in staged:
        parts.append(f"format_staged: {fpath}")
        block = op_format(fpath, tool_filter, verbose=verbose, gated=True)
        for line in block.splitlines():
            parts.append(f"  {line}")
    return "\n".join(parts) + "\n"


def _detect_payload_format(raw: str) -> str:
    """Return 'json' if first non-whitespace char is { or [, else 'toml'.

    Exception: a leading '[[' is a TOML table-array header (never valid
    JSON), so it is detected as TOML. This lets '[[ops]]' batch payloads
    parse correctly instead of being misread as a JSON array.
    """
    stripped = raw.lstrip(" \t\r\n")
    if stripped.startswith("[["):
        return "toml"
    for c in stripped:
        return "json" if c in "{[" else "toml"
    return "json"


_TOML_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "b": "\b", "f": "\f"}


def _toml_basic_unescape(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            out.append(_TOML_ESCAPES.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _toml_skip_ws_comments(raw: str, i: int) -> int:
    """Advance past whitespace, newlines and # comments; return the new offset.

    Used inside inline arrays, where TOML allows both to appear between
    elements — a `paths = [\n  "a",  # keep\n  "b",\n]` payload is ordinary.
    """
    n = len(raw)
    while i < n:
        if raw[i] in " \t\r\n":
            i += 1
        elif raw[i] == "#":
            while i < n and raw[i] != "\n":
                i += 1
        else:
            break
    return i


def _toml_parse_array(raw: str, i: int, key: str) -> Tuple[List[Any], int]:
    """Parse an inline array at *i* (on its '['); return (items, next offset).

    Elements are whatever `_toml_parse_value` accepts, so arrays nest. A
    trailing comma is allowed (TOML permits it), whitespace and comments may
    separate elements, and a missing separator is an error rather than a
    silently truncated list.
    """
    n = len(raw)
    i += 1
    items: List[Any] = []
    while True:
        i = _toml_skip_ws_comments(raw, i)
        if i >= n:
            raise ValueError(f"unterminated array for '{key}'")
        if raw[i] == "]":
            return items, i + 1
        val, i = _toml_parse_value(raw, i, key)
        items.append(val)
        i = _toml_skip_ws_comments(raw, i)
        if i < n and raw[i] == ",":
            i += 1
            continue
        if i < n and raw[i] == "]":
            return items, i + 1
        raise ValueError(
            f"expected ',' or ']' in array for '{key}' at offset {i}"
        )


def _toml_parse_value(raw: str, i: int, key: str) -> Tuple[Any, int]:
    """Parse one TOML value at *i*; return (value, offset just past it).

    Split out of `_mini_toml_loads` so inline arrays can recurse into it
    rather than reimplementing every scalar form.
    """
    n = len(raw)
    if raw[i:i + 3] == '"""':
        i += 3
        end = raw.find('"""', i)
        if end < 0:
            raise ValueError(f"unterminated \"\"\" for '{key}'")
        val: Any = _toml_basic_unescape(raw[i:end])
        if val.startswith("\r\n"):
            val = val[2:]
        elif val.startswith("\n"):
            val = val[1:]
        return val, end + 3
    if raw[i:i + 3] == "'''":
        i += 3
        end = raw.find("'''", i)
        if end < 0:
            raise ValueError(f"unterminated ''' for '{key}'")
        val = raw[i:end]
        if val.startswith("\r\n"):
            val = val[2:]
        elif val.startswith("\n"):
            val = val[1:]
        return val, end + 3
    if raw[i] == '"':
        i += 1
        buf = []
        while i < n and raw[i] != '"':
            if raw[i] == "\\" and i + 1 < n:
                buf.append(_TOML_ESCAPES.get(raw[i + 1], raw[i + 1]))
                i += 2
            elif raw[i] == "\n":
                raise ValueError(f"newline in single-line string for '{key}'")
            else:
                buf.append(raw[i])
                i += 1
        if i >= n:
            raise ValueError(f"unterminated string for '{key}'")
        return "".join(buf), i + 1
    if raw[i] == "'":
        i += 1
        end = raw.find("'", i)
        if end < 0 or raw.find("\n", i, end) >= 0:
            raise ValueError(f"unterminated literal for '{key}'")
        return raw[i:end], end + 1
    if raw[i:i + 4] == "true" and (i + 4 == n or not raw[i + 4].isalnum()):
        return True, i + 4
    if raw[i:i + 5] == "false" and (i + 5 == n or not raw[i + 5].isalnum()):
        return False, i + 5
    if raw[i] == "[":
        return _toml_parse_array(raw, i, key)
    if raw[i] == "-" or raw[i].isdigit():
        ns = i
        if raw[i] == "-":
            i += 1
        while i < n and raw[i].isdigit():
            i += 1
        try:
            return int(raw[ns:i]), i
        except ValueError as _e:
            raise ValueError(f"bad number for '{key}': {_e}") from _e
    raise ValueError(f"unknown value type for '{key}' at offset {i}")


def _mini_toml_loads(raw: str) -> Dict[str, Any]:
    """Minimal TOML parser for @file payloads.

    Supports: bare keys, integers, true/false, single-line strings
    ("..." with escapes, '...' literal), multi-line strings (\"\"\"...\"\"\"
    with escapes, '''...''' literal), inline arrays (nesting, trailing comma,
    comments between elements), # comments, and `[[table]]` array-of-tables
    headers. No single `[table]`, no dotted keys, no dates — only what
    payloads need.

    Inline arrays matter specifically: a variadic payload field is written as
    a list, and `git-commit:@-` with `paths = ["a", "b"]` is the documented
    form. Without them that payload parsed on 3.11+ (stdlib `tomllib`) and
    died below it with `unknown value type for 'paths'` — the op's own
    documented syntax failing on a third of the supported matrix.

    `[[ops]]` matters specifically: it is the shape a `batch:@-` payload takes,
    and this parser is what runs on Python <3.11, where stdlib `tomllib` is
    absent. Without it a batch payload parses on 3.11+ and dies below it.

    Used as fallback when stdlib `tomllib` is unavailable (Python <3.11).
    """
    result: Dict[str, Any] = {}
    # Key/value pairs land here: the top-level dict, or the most recent
    # [[table]] entry once one has been opened.
    current: Dict[str, Any] = result
    i, n = 0, len(raw)
    while i < n:
        while i < n and raw[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        if raw[i] == "#":
            while i < n and raw[i] != "\n":
                i += 1
            continue
        if raw[i] == "[":
            if raw[i:i + 2] != "[[":
                raise ValueError(
                    f"single [table] header at offset {i} is not supported by the "
                    "fallback TOML parser (Python <3.11); use [[table]] or JSON"
                )
            end = raw.find("]]", i + 2)
            if end < 0:
                raise ValueError(f"unterminated [[table]] header at offset {i}")
            name = raw[i + 2:end].strip()
            if not name or not all(c.isalnum() or c in "_-" for c in name):
                raise ValueError(f"bad [[table]] name {name!r} at offset {i}")
            bucket = result.setdefault(name, [])
            if not isinstance(bucket, list):
                raise ValueError(f"{name!r} is both a value and a [[table]]")
            current = {}
            bucket.append(current)
            i = end + 2
            continue
        ks = i
        while i < n and (raw[i].isalnum() or raw[i] in "_-"):
            i += 1
        if i == ks:
            raise ValueError(f"bad key at offset {i}")
        key = raw[ks:i]
        while i < n and raw[i] in " \t":
            i += 1
        if i >= n or raw[i] != "=":
            raise ValueError(f"expected '=' after key '{key}'")
        i += 1
        while i < n and raw[i] in " \t":
            i += 1
        if i >= n:
            raise ValueError(f"missing value for '{key}'")
        val, i = _toml_parse_value(raw, i, key)
        current[key] = val
        while i < n and raw[i] in " \t":
            i += 1
        if i < n and raw[i] == "#":
            while i < n and raw[i] != "\n":
                i += 1
    return result


def _toml_delimiter_hint(raw: str) -> str:
    """Explain a TOML parse failure caused by ''' inside a ''' block (#394).

    The parse error points at a column in the payload, which is where the
    delimiter closed — not at the ''' in the content that closed it. An odd
    number of ''' runs is exactly that shape: every literal block opens and
    closes, so a stray one means the content carried its own.

    Silent when the payload has no ''' at all — then the failure is ordinary
    TOML and a delimiter lecture would be noise. Silent too when no ''' ever
    opens a value: `new = "isn't it''' odd"` carries the run harmlessly inside
    a basic string, and a delimiter lecture there sends the reader after the
    wrong cause, which is worse than saying nothing at all.
    """
    if raw.count("'''") % 2 == 0:
        return ""
    if not re.search(r"=[ \t]*'''", raw):
        return ""
    return (
        f"\n  {mark('↳')} the payload has an odd number of ''' runs — content containing "
        "''' closes the block early.\n"
        '    Use a \"\"\"basic\"\"\" block instead (escapes apply, so \\ doubles), '
        "or the JSON payload form,\n"
        "    which needs no delimiter: {\"path\": ..., \"old\": ..., \"new\": ...}\n"
    )


def _load_at_file(ref: str) -> Any:
    """Load JSON or TOML from an @file reference.

    Accepts:
      @path/to/file.json   — read from filesystem
      @-                   — read from stdin

    Format detected from first non-whitespace char: { or [ → JSON, else TOML.
    TOML lets you embed code blocks with backslashes/quotes/newlines without
    JSON's double-escaping. Use '''triple-single-quote''' for literal content.

    When the content itself contains ''' — Python source that inspects Python
    source is the common case — that delimiter cannot carry it. Fall back to a
    \"\"\"basic\"\"\" block (escapes apply, so backslashes double) or to the JSON
    payload form, which needs no delimiter at all. #394.

    Returns the parsed value (dict, list, etc.).
    Raises ValueError with a human-readable message on any error.
    """
    if ref == "@-":
        raw = sys.stdin.read()
        source = "<stdin>"
    else:
        fpath = ref[1:]  # strip leading @
        if not os.path.isfile(fpath):
            raise ValueError(f"@file not found: {fpath}")
        try:
            with open(fpath, "r", encoding="utf-8") as _f:
                raw = _f.read()
        except OSError as _e:
            raise ValueError(f"@file read error: {fpath}: {_e}") from _e
        source = fpath
    fmt = _detect_payload_format(raw)
    if fmt == "json":
        try:
            return json.loads(raw)
        except json.JSONDecodeError as _e:
            raise ValueError(f"@file JSON parse error ({source}): {_e}") from _e
    try:
        import tomllib  # stdlib, Python 3.11+
        parser = tomllib.loads
    except ImportError:
        parser = _mini_toml_loads
    try:
        return parser(raw)
    except Exception as _e:
        raise ValueError(
            f"@file TOML parse error ({source}): {_e}{_toml_delimiter_hint(raw)}"
        ) from _e


# Dynamic @file field registry — built lazily from op syntax strings.
# Maps op name → ordered list of JSON field names (positional parts[1..N]).
# Populated on first dispatch call via _build_at_file_registry().
_AT_FILE_REGISTRY: Dict[str, List[Tuple[str, bool, bool]]] = {}
_AT_FILE_REGISTRY_BUILT: bool = False


def _fields_from_syntax(syntax: str) -> List[Tuple[str, bool, bool]]:
    """Derive field specs from a syntax string using ':::' separator.

    Returns a list of (name, optional, variadic) tuples:
      - name:     lowercased field name, stripped of [ ] ... and whitespace
      - optional: field sits inside a trailing [...] optional group
      - variadic: field token carried '...' — payload value may be a list,
                  expanded into multiple positional parts

    Takes the first alternative (before ' | '), splits on ':::', drops the
    first token (op name). Returns [] if the syntax has no ':::' (read-only
    op — no @file route). Optionality is tracked by '[' / ']' bracket depth,
    so a field is optional whenever an unclosed group is open at its position
    (correct even for a non-trailing optional group).

    Returns [] when any derived field name is not a clean identifier
    ([a-z][a-z0-9_]*). That guards against syntax strings carrying inline
    prose or punctuation a payload key could never match — e.g. git-resolve's
    'PATH[,PATH...][:::BLOCKS]  (SIDE: ...)'. Such ops simply have no @file
    route rather than a falsely-registered, non-functional one.

    Examples:
      'edit:::OLD:::NEW:::PATH'            → [('old',F,F),('new',F,F),('path',F,F)]
      'git-commit:::MESSAGE[:::PATHS...]'  → [('message',F,F),('paths',T,T)]
      'read:PATH'                          → []
    """
    first_alt = re.split(r"\s*\|\s*", syntax)[0]
    if ":::" not in first_alt:
        return []
    specs: List[Tuple[str, bool, bool]] = []
    depth = 0
    for tok in first_alt.split(":::")[1:]:
        variadic = "..." in tok
        optional = depth > 0
        depth += tok.count("[") - tok.count("]")
        name = (tok.replace("[", "").replace("]", "")
                   .replace("...", "").strip().lower())
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            return []
        specs.append((name, optional, variadic))
    return specs


_AT_FILE_BUILTIN_DEFAULTS: Dict[str, List[Tuple[str, bool, bool]]] = {
    "edit":          [("old", False, False), ("new", False, False), ("path", False, False)],
    "replace":       [("old", False, False), ("new", False, False), ("path", False, False)],
    "replace_dry":   [("old", False, False), ("new", False, False), ("path", False, False)],
    "replace_lines": [("path", False, False), ("start", False, False), ("end", False, False), ("content", False, False)],
    "paste":         [("path", False, False), ("content", False, False)],
    "append":        [("path", False, False), ("content", False, False)],
    "vim":           [("path", False, False), ("script", False, False)],
}


def _build_at_file_registry() -> None:
    """Populate _AT_FILE_REGISTRY from builtin-ops and custom/preset op syntax.

    Starts from _AT_FILE_BUILTIN_DEFAULTS so the builtins always work even
    when no config file is present (e.g. in tests). Config-derived entries
    overlay the defaults, allowing syntax-driven overrides and automatically
    giving preset ops with ':::' syntax their own @file routes.

    Called once on the first dispatch invocation.
    """
    global _AT_FILE_REGISTRY, _AT_FILE_REGISTRY_BUILT
    if _AT_FILE_REGISTRY_BUILT:
        return
    registry: Dict[str, List[Tuple[str, bool, bool]]] = dict(_AT_FILE_BUILTIN_DEFAULTS)
    config = _load_config()
    for section in ("builtin-ops", "ops"):
        for op_name, info in config.get(section, {}).items():
            if not isinstance(info, dict):
                continue
            syntax = info.get("syntax", "")
            if not syntax:
                continue
            fields = _fields_from_syntax(syntax)
            if fields:
                registry[op_name] = fields
    _AT_FILE_REGISTRY = registry
    _AT_FILE_REGISTRY_BUILT = True


def _at_file_specs(op: str) -> List[Tuple[str, bool, bool]]:
    """Return (name, optional, variadic) specs for *op*, or [] if no @file route."""
    _build_at_file_registry()
    return _AT_FILE_REGISTRY.get(op, [])


def _at_file_fields(op: str) -> List[str]:
    """Return the field NAMES for *op*, or [] if the op has no @file route.

    Kept name-only for the truthiness/sub-op callers; field semantics
    (optional, variadic) live in _at_file_specs.
    """
    return [name for name, _opt, _var in _at_file_specs(op)]


def _reorder_batch_for_snapshot(batch_ops: List[Any]) -> Tuple[List[Any], str]:
    """Reorder replace_lines ops within a batch so line numbers refer to the
    original file state (snapshot semantics), not the file as mutated by
    earlier ops in the same batch.

    Strategy: group replace_lines ops by path. For each file appearing in 2+
    replace_lines ops, sort them by start descending and write them back into
    their original slots. Non-replace_lines ops keep their order.

    Bottom-up application means earlier (in batch order, now last-applied)
    line numbers stay valid because mutations happen at lines AFTER the
    next op's range.

    Other op types (edit, replace, paste, vim) are content-matched or
    full-file rewrites, so they're inherently snapshot-safe and need no
    reordering.

    Returns (new_ops, error_message). On overlap detection, returns
    (original_ops, error_message) — caller should surface the error before
    applying anything.
    """
    by_file: Dict[str, List[int]] = {}
    for i, item in enumerate(batch_ops):
        if (
            isinstance(item, dict)
            and item.get("op") == "replace_lines"
            and isinstance(item.get("path"), str)
        ):
            by_file.setdefault(item["path"], []).append(i)

    # Overlap detection — flag conflicts before doing any reorder.
    for path, indices in by_file.items():
        if len(indices) < 2:
            continue
        ranges = []
        for idx in indices:
            item = batch_ops[idx]
            s, e = item.get("start"), item.get("end")
            if not isinstance(s, int) or not isinstance(e, int):
                continue  # let normal dispatch surface type errors
            ranges.append((min(s, e), max(s, e)))
        ranges.sort()
        for i in range(len(ranges) - 1):
            # Treat pure-insert (end < start) as a zero-width range; only
            # error if a true range collides.
            if ranges[i][1] >= ranges[i + 1][0]:
                return batch_ops, (
                    f"batch snapshot: overlapping replace_lines ranges on "
                    f"{path}: [{ranges[i][0]},{ranges[i][1]}] and "
                    f"[{ranges[i + 1][0]},{ranges[i + 1][1]}]"
                )

    # Reorder — for each file with 2+ replace_lines ops, sort descending by
    # start and place back into the same slots.
    if not any(len(v) > 1 for v in by_file.values()):
        return batch_ops, ""
    new_ops = list(batch_ops)
    for path, indices in by_file.items():
        if len(indices) < 2:
            continue
        items_at = [batch_ops[i] for i in indices]
        items_sorted = sorted(items_at, key=lambda x: -int(x.get("start", 0)))
        for slot, item in zip(indices, items_sorted):
            new_ops[slot] = item
    return new_ops, ""


def _at_file_to_parts(op: str, payload: Any) -> Tuple[List[str], bool]:
    """Convert a JSON payload dict to (parts, replace_all) for the given op.

    The returned parts list is [op, field1_value, field2_value, ...] matching
    the positional form that the existing dispatch handlers expect.

    All values are coerced to str so the downstream handlers work unchanged.

    replace_all is extracted from the payload and returned separately so the
    dispatch handler can act on it without polluting the parts list.
    """
    if isinstance(payload, list) or (
        isinstance(payload, dict) and isinstance(payload.get("ops"), list)
    ):
        raise ValueError(
            f"this payload is an ops array — use 'batch:@file' instead of "
            f"'{op}:@file' (e.g. batch:@payload.toml)"
        )
    if not isinstance(payload, dict):
        raise ValueError(
            f"@file payload for op '{op}' must be a JSON object, "
            f"got {type(payload).__name__}"
        )
    specs = _at_file_specs(op)
    if not specs:
        raise ValueError(f"@file route not supported for op '{op}'")
    # Case-insensitive key lookup — normalise payload keys once.
    lower_payload = {k.lower(): v for k, v in payload.items()}
    parts = [op]
    for name, optional, variadic in specs:
        if name not in lower_payload:
            if optional:
                continue
            raise ValueError(
                f"@file payload for op '{op}' missing required field '{name}'"
            )
        value = lower_payload[name]
        if variadic:
            # Accept a single scalar or a list; each element becomes one
            # positional part (e.g. git-commit paths → PATH PATH ...). A null
            # value or null elements are dropped, so paths:null / paths:[]
            # cleanly omit rather than emitting a literal "None" arg.
            if value is None:
                items: List[Any] = []
            elif isinstance(value, list):
                items = value
            else:
                items = [value]
            parts.extend(str(v) for v in items if v is not None)
        else:
            parts.append(str(value))
    replace_all = bool(lower_payload.get("replace_all", False))
    return parts, replace_all


import threading as _threading
_DISPATCH_STATE = _threading.local()
_DISPATCH_MAX_DEPTH = int(os.environ.get("SUPERTOOL_DISPATCH_MAX_DEPTH", "32"))


def dispatch(arg: str, pre_parsed: "Optional[Tuple[List[str], bool]]" = None) -> str:
    """Parse 'op:arg1:arg2:...' and route to the matching op function.

    *pre_parsed*, when given, is an already-structured (parts, replace_all)
    tuple — the same shape `_at_file_to_parts` produces from a JSON payload.
    Callers (batch sub-ops) pass it to bypass BOTH the `:::` re-tokenization
    and the shell-escape decoding, so content containing `:::` or backslashes
    survives verbatim — exactly as a standalone `edit:@file` call behaves.

    Traversal ops (grep, glob, tree, map) support an optional :::no-exclude
    suffix that bypasses all exclude-paths for that one call.
    Example: 'grep:pattern:vendor/:10:::no-exclude'

    Mutating ops (edit, replace, replace_lines, paste, vim) additionally
    accept an @file route:
    Example: 'edit:@.max/e1.json'  — reads {"path","old","new"} from file.
    Use '@-' to read JSON payload from stdin.

    The 'batch' op runs multiple ops from a JSON file:
    Example: 'batch:@.max/ops.json'
    Payload: array of {"op":"X",...fields} OR
             {"continue_on_error":true,"ops":[...]}

    A self-referencing batch (or any op chain) is bounded by
    SUPERTOOL_DISPATCH_MAX_DEPTH (default 32) — exceeding returns a clean
    ERROR string instead of a Python RecursionError. Depth counter is
    threading.local so concurrent calls from worker threads or free-
    threaded CPython (3.13t+) don't share/corrupt each other's count.
    """
    depth = getattr(_DISPATCH_STATE, "depth", 0)
    if depth >= _DISPATCH_MAX_DEPTH:
        return (
            f"ERROR: dispatch recursion limit ({_DISPATCH_MAX_DEPTH}) exceeded "
            f"— check for a self-referencing batch payload\n"
        )
    _DISPATCH_STATE.depth = depth + 1
    try:
        return _dispatch_impl(arg, pre_parsed)
    finally:
        _DISPATCH_STATE.depth = depth
        # _FORMATTER_SKIPS is module-level and drained on the normal return
        # path. An exception escaping _dispatch_impl skips that drain, and the
        # next top-level call would report skips belonging to a call that
        # already died. The outermost frame owns the reset either way.
        if depth == 0:
            _FORMATTER_SKIPS.clear()


def _dispatch_impl(arg: str, pre_parsed: "Optional[Tuple[List[str], bool]]" = None) -> str:
    """Body of dispatch — separated so the recursion guard stays minimal."""
    # Strip :::no-exclude before splitting so it doesn't interfere with arg parsing
    no_exclude = arg.endswith(_NO_EXCLUDE_SUFFIX)
    if no_exclude:
        arg = arg[: -len(_NO_EXCLUDE_SUFFIX)]

    header = f"--- {arg}{_NO_EXCLUDE_SUFFIX if no_exclude else ''} ---\n"

    # `op:::FIELD:::FIELD:::...` — triple-colon mode for write ops with
    # arbitrary `:` in content. Only triggers when the op name is followed
    # immediately by `:::`. Existing `:::no-exclude` (suffix, stripped above)
    # and `read:PATH:::grep=` (mid-arg) keep working under single-colon parsing.
    _at_file_replace_all: bool = False
    _at_file_used: bool = False
    if pre_parsed is not None:
        # Batch sub-op: parts already structured from a JSON payload (via
        # _at_file_to_parts). Skip ALL string tokenization and reuse the
        # @file semantics — literal bytes, no `:::` split, no escape decode.
        # This is what routes `:::`-containing content through unharmed.
        parts, _at_file_replace_all = pre_parsed
        _at_file_used = True
        op = parts[0] if parts else ""
    else:
        # `op:::FIELD:::FIELD:::...` — triple-colon mode for write ops with
        # arbitrary `:` in content. Only triggers when the op name is followed
        # immediately by `:::`. Existing `:::no-exclude` (suffix, stripped above)
        # and `read:PATH:::grep=` (mid-arg) keep working under single-colon parsing.
        import re as _re
        triple_match = _re.match(r"^([a-zA-Z_][a-zA-Z0-9_-]*):::", arg)
        if triple_match:
            parts = arg.split(":::")
        else:
            parts = _split_arg(arg)
        op = parts[0] if parts else ""

    # @file route — 'op:@path' or 'op:@-' (stdin).
    # Load JSON, rebuild parts list, then fall through to the normal handlers.
    # Applies to mutating ops that have ':::' fields in their syntax string.
    if (
        pre_parsed is None
        and len(parts) >= 2
        and parts[1].startswith("@")
        and _at_file_fields(op)
    ):
        if len(parts) > 2:
            return header + (
                f"ERROR: {op}:@... takes the @reference as the only argument "
                f"(e.g. {op}:@payload.json or {op}:@-). Put fields in the "
                f"JSON/TOML payload, not on the colon CLI.\n"
            )
        try:
            payload = _load_at_file(parts[1])
            parts, _at_file_replace_all = _at_file_to_parts(op, payload)
            _at_file_used = True
        except ValueError as _e:
            return header + f"ERROR: {_e}\n"

    # When parts come from @file (JSON/TOML payload), they hold literal
    # bytes — backslashes and newlines must NOT be reinterpreted as shell-
    # style escapes. Only colon-CLI input needs `_decode_escapes`.
    _dec = (lambda s: s) if _at_file_used else _decode_escapes

    # A content-heavy mutating op echoes its old and new strings in the header
    # and then again in the diff underneath. Rebuild the header from the parsed
    # fields once the arguments are long enough for that to cost real tokens —
    # the diff below is the useful part and already shows what changed (#384).
    # A batch sub-op arrives with `arg` joined from its parts, which is exactly
    # the case the issue was filed about.
    #
    # Deferred, not applied here: on FAILURE no diff renders, and the verbatim
    # header is then the only surviving copy of what the caller sent. Eliding
    # it would take the reproduction material away at the one moment it is
    # needed. So the compact form is computed now, while `parts` is in hand,
    # and swapped in at the end only if the op succeeded.
    _compact_header = ""
    if len(arg) > _HEADER_ARG_MAX:
        _compact_header = _compact_header_arg(op, parts)
    _writes_before = _WRITE_COUNT[0]
    _attempts_before = _MUTATION_ATTEMPTS[0]

    # #146: dispatch-level path containment. Each op has a known position(s)
    # of its path arg(s) in `parts`. _safe_path enforces cwd containment
    # unless SUPERTOOL_ALLOW_OUTSIDE_CWD=1 is set. Chokepoint coverage
    # (render_file, _atomic_write) catches internal callers that bypass
    # dispatch (alias expansion, test code calling op_X directly).
    _PATH_ARG_POSITIONS = {
        "read": (1,), "head": (1,), "tail": (1,), "wc": (1,),
        "stat": (1,), "around_line": (1,), "ls": (1,), "tree": (1,),
        "map": (1,), "blame": (1,), "validate": (1,), "format": (1,),
        "workspace": (1,), "diag": (1,),
        # around:PATTERN:PATH, grep_around:PATTERN:PATH, grep:PATTERN:PATH
        "around": (2,), "grep_around": (2,), "grep": (2,),
        # hover:SYMBOL:FILE, rename:OLD:NEW:FILE, resolve:SYMBOL[:FROM_FILE]
        "hover": (2,), "rename": (3,), "resolve": (2,),
        # diff:PATH1:PATH2
        "diff": (1, 2),
        # between:SYMBOL:PATH (path at 2) | between:re:START:END:PATH (path at 4)
        # — checking both positions covers both forms.
        "between": (2, 4),
        # check:PRESET:PATH — runs a custom op, path forwarded as {file}.
        "check": (2,),
        # mutating ops (also covered by _atomic_write chokepoint):
        "edit": (3,), "replace": (3,), "replace_dry": (3,),
        "replace_lines": (1,), "paste": (1,), "append": (1,), "vim": (1,),
    }
    for _pos in _PATH_ARG_POSITIONS.get(op, ()):
        if _pos < len(parts):
            _candidate = parts[_pos]
            # Skip empty / sentinel values — handlers default these to "." themselves.
            if not _candidate or _candidate in (".", "full", "raw"):
                continue
            try:
                _safe_path(_candidate)
            except SecurityError as _se:
                return header + f"ERROR: {_se}\n"

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
                        return header + "ERROR: read range START must be >= 1\n"
                    if r_end < r_start:
                        return header + (
                            f"ERROR: read range END ({r_end}) is before "
                            f"START ({r_start})\n"
                        )
                    offset = r_start - 1
                    limit = r_end - r_start + 1
                    range_form = True
                else:
                    offset = int(parts[2])
            if len(parts) > 3 and parts[3]:
                if parts[3] in ("full", "raw"):
                    force_full = True
                elif parts[3].startswith("grep="):
                    pass  # picked up by the filter scan below
                elif range_form:
                    return header + (
                        f"ERROR: read:PATH:START-END takes no LIMIT "
                        f"(got {parts[3]!r}) — the range already bounds it\n"
                    )
                else:
                    limit = int(parts[3])
            # The filter can land in any trailing slot: parts[4] for the
            # documented `read:PATH:::grep=` (the `:::` yields two empty parts),
            # parts[3] when a range consumed only one slot. Scan rather than
            # index, so every spelling reaches the same place.
            grep_filter = ""
            for _tok in parts[3:]:
                if _tok.startswith("grep="):
                    grep_filter = _tok[5:]
                    break
            body = op_read(path, offset, limit, grep_filter, force_full)
            if not range_form:
                body += _read_range_note(path, offset, limit, body)
        elif op == "grep":
            pattern, path, limit, context, count_only, no_auto_read = \
                _parse_grep_args(parts)
            body = op_grep(pattern, path, limit, context, count_only,
                           no_exclude=no_exclude, no_auto_read=no_auto_read)
        elif op == "grep_around":
            # grep_around:PATTERN:PATH[:N[:LIMIT]] — every match with N lines
            # context. Sane defaults for "show me how everyone uses this".
            ga_pattern = parts[1] if len(parts) > 1 else ""
            ga_path = parts[2] if len(parts) > 2 and parts[2] else "."
            ga_context = int(parts[3]) if len(parts) > 3 and parts[3] else 3
            ga_limit = int(parts[4]) if len(parts) > 4 and parts[4] else 10
            body = op_grep(ga_pattern, ga_path, ga_limit, ga_context,
                           count_only=False, no_exclude=no_exclude)
        elif op == "wc":
            path = parts[1] if len(parts) > 1 else ""
            body = op_wc(path)
        elif op == "glob":
            pattern = parts[1] if len(parts) > 1 else ""
            no_auto_read = len(parts) > 2 and parts[2] == "no-auto-read"
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
            pattern, path, n = _parse_around_args(parts)
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
                # Pattern mode opt-in: between:re:START:END:PATH
                # 're:' is reserved as the mode marker — never falls through
                # to symbol mode, even if arg count is wrong, since 're' as
                # a symbol name is highly unlikely and silent fallthrough
                # produces misleading "file not found" errors when single-
                # letter args trip the Windows drive-letter merge in
                # _split_arg.
                if len(parts) >= 5:
                    start_pat = parts[2]
                    end_pat = parts[3]
                    path = ":".join(parts[4:])
                    body = op_between_pattern(start_pat, end_pat, path)
                else:
                    body = ("ERROR: between:re: requires START:END:PATH "
                            f"(got {len(parts) - 2} args after 're')\n")
            elif len(parts) >= 3:
                # Symbol mode: between:SYMBOL:PATH
                # Join middle parts on ':' so PHP Foo::bar style names work.
                symbol = ":".join(parts[1:-1])
                path = parts[-1]
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
                # CONTENT may legitimately contain ':' — rejoin remaining parts
                rl_content = _dec(":".join(parts[4:]) if len(parts) > 4 else "")
                body = _run_with_validators(op, parts, lambda: op_replace_lines(rl_path, rl_start, rl_end, rl_content))
        elif op == "paste":
            p_path = parts[1] if len(parts) > 1 else ""
            # CONTENT may contain ':' — rejoin everything after the path
            p_content = _dec(":".join(parts[2:]) if len(parts) > 2 else "")
            body = _run_with_validators(op, parts, lambda: op_paste(p_path, p_content))
        elif op == "append":
            a_path = parts[1] if len(parts) > 1 else ""
            # CONTENT may contain ':' — rejoin everything after the path
            a_content = _dec(":".join(parts[2:]) if len(parts) > 2 else "")
            body = _run_with_validators(op, parts, lambda: op_append(a_path, a_content))
        elif op == "vim":
            vim_path = parts[1] if len(parts) > 1 else ""
            vim_script = ":".join(parts[2:]) if len(parts) > 2 else ""
            body = _run_with_validators(op, parts, lambda: op_vim(vim_path, vim_script))
        elif op == "batch":
            # batch:@file — run multiple ops from a JSON file.
            # Payload: bare array of {"op":"X",...} objects, OR wrapper object
            # {"continue_on_error": bool, "ops": [...]}.
            # Default: continue_on_error=True (keep running after a failed op).
            ref = parts[1] if len(parts) > 1 else ""
            if not ref.startswith("@"):
                body = "ERROR: batch requires an @file argument, e.g. batch:@ops.json\n"
            else:
                try:
                    raw_payload = _load_at_file(ref)
                except ValueError as _be:
                    body = f"ERROR: {_be}\n"
                else:
                    # Normalise to (continue_on_error, ops_list)
                    if isinstance(raw_payload, list):
                        batch_ops = raw_payload
                        continue_on_error = True
                    elif isinstance(raw_payload, dict):
                        if "ops" not in raw_payload and [
                            k for k in raw_payload if k != "continue_on_error"
                        ]:
                            # Mirror of the single-op-route misroute (#468): this
                            # looks like one op's own fields (e.g. old/new/path),
                            # not a batch wrapper — say so instead of silently
                            # running zero ops.
                            batch_ops = None  # signal: already set body
                            body = (
                                "ERROR: this payload has no 'ops' array — it looks "
                                "like a single op's fields. Use 'OP:@file' (e.g. "
                                "'edit:@file') for a single op, or wrap it as "
                                '{"ops": [...]} for batch.\n'
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
                        batch_ops = None  # signal: already set body

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
                                # Snapshot mode: reorder replace_lines ops on
                                # the same file bottom-up so caller line
                                # numbers refer to the original file state,
                                # not the file as mutated by earlier ops.
                                batch_ops, _snap_err = _reorder_batch_for_snapshot(batch_ops)
                                if _snap_err:
                                    body = f"ERROR: {_snap_err}\n"
                                    batch_ops = []
                            results: List[str] = []
                            # A batch is one logical change: defer per-op formatters
                            # (no_unused_imports etc.) so an import added by op N survives
                            # until op N+1's usage lands. main()'s len(argv)>1 guard never
                            # fires for a lone batch:@file arg, so own the defer locally —
                            # unless already inside a deferred multi-arg call, where main()
                            # owns the queue. Issue #291.
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
                                        if not continue_on_error:
                                            break
                                        continue
                                    _sub_op = _item.get("op", "")
                                    if not _sub_op:
                                        err = "ERROR: batch op missing 'op' field\n"
                                        results.append(err)
                                        if not continue_on_error:
                                            break
                                        continue
                                    # Build the arg string from the op + its fields,
                                    # using the @file→parts machinery for mutating ops
                                    # (preserves validators) and plain dispatch for others.
                                    _sub_pre_parsed = None
                                    if _at_file_fields(_sub_op):
                                        try:
                                            _sub_parts, _sub_replace_all = _at_file_to_parts(_sub_op, _item)
                                        except ValueError as _ve:
                                            err = f"ERROR: {_ve}\n"
                                            results.append(err)
                                            if not continue_on_error:
                                                break
                                            continue
                                        # replace_all: true on an edit op → promote to replace
                                        if _sub_replace_all and _sub_op == "edit":
                                            _sub_parts[0] = "replace"
                                        # Route the ALREADY-structured parts straight through
                                        # dispatch via pre_parsed — do NOT re-serialize to a
                                        # `:::` string, which would re-tokenize and corrupt
                                        # content that itself contains `:::` (issue #252).
                                        # A readable colon summary is used only for the header.
                                        _sub_pre_parsed = (_sub_parts, _sub_replace_all)
                                        _sub_arg = ":".join(_sub_parts)
                                    else:
                                        # Read-only op: build plain colon arg from known fields.
                                        # For unknown ops, pass what we have and let dispatch error.
                                        _fields = [str(_item[k]) for k in sorted(_item) if k != "op"]
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
                            # Only override `body` with joined results when no
                            # upstream error fired (cap rejection, snapshot reorder).
                            if not _snap_err and not _cap_exceeded:
                                body = "".join(results)
                                if _batch_owns_defer:
                                    body += _drain_format_queue()
                                    body += _drain_validator_queue()
        elif op == "validate":
            # verbose flag: literal "verbose" token anywhere after op name.
            # Forms: validate:PATH:verbose  or  validate:PATH:tool1,tool2:verbose
            #   list form: validate:f1,f2,...:tool1,tool2:verbose  (commas in PATH)
            v_verbose = "verbose" in parts[1:]
            v_parts = [p for p in parts[1:] if p != "verbose"]
            v_path = v_parts[0] if len(v_parts) > 0 else ""
            v_tools = [t for t in (v_parts[1].split(",") if len(v_parts) > 1 and v_parts[1] else []) if t]
            v_files = [f for f in v_path.split(",") if f]
            if len(v_files) > 1:
                try:
                    for _vf in v_files:
                        _safe_path(_vf)
                except SecurityError as _se:
                    return header + f"ERROR: {_se}\n"
                body = op_validate_multi(v_files, v_tools or None, verbose=v_verbose)
            else:
                body = op_validate(v_path, v_tools or None, verbose=v_verbose)
        elif op == "format":
            # verbose flag: literal "verbose" token anywhere after op name.
            # Forms: format:PATH:verbose  or  format:PATH:tool1,tool2:verbose
            f_verbose = "verbose" in parts[1:]
            f_parts = [p for p in parts[1:] if p != "verbose"]
            f_path = f_parts[0] if len(f_parts) > 0 else ""
            f_tools = [t for t in (f_parts[1].split(",") if len(f_parts) > 1 and f_parts[1] else []) if t]
            body = op_format(f_path, f_tools or None, verbose=f_verbose)
        elif op == "validate_staged":
            # verbose flag: literal "verbose" token anywhere after op name.
            # Forms: validate_staged:verbose  or  validate_staged::tool1,tool2:verbose
            vs_verbose = "verbose" in parts[1:]
            vs_parts = [p for p in parts[1:] if p != "verbose"]
            vs_tools = [t for t in (vs_parts[0].split(",") if len(vs_parts) > 0 and vs_parts[0] else []) if t]
            body = op_validate_staged(vs_tools or None, verbose=vs_verbose)
        elif op == "format_staged":
            # verbose flag: literal "verbose" token anywhere after op name.
            # Forms: format_staged:verbose  or  format_staged::tool1,tool2:verbose
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
        elif op in ("introduction", "output-format", "ops", "ops-compact", "version"):
            # Meta-ops use markdown headers instead of --- header ---
            header = ""
            if op == "introduction":
                body = op_introduction()
            elif op == "output-format":
                body = op_output_format()
            elif op == "version":
                body = op_version()
            elif op == "ops-compact":
                body = op_ops(compact=True)
            else:
                body = op_ops()
        else:
            # Fallthrough: try custom ops, then aliases
            custom = _resolve_custom_op(op, parts)
            if custom is not None:
                body = custom
            else:
                alias = _resolve_alias(op, parts)
                if alias is not None:
                    body = alias
                else:
                    body = (f"ERROR: unknown operation: {op}\n"
                            f"Valid operations: read, grep, glob, ls, tail, "
                            f"head, around, around_line, between, wc, check, map, diff, stat, tree, "
                            f"replace, replace_dry\n")
    except (ValueError, IndexError) as e:
        body = f"ERROR: argument parsing: {e}\n"

    # Fire read-op notifiers (mutating ops already fire inside _run_with_validators)
    try:
        _notify_read_op(op, parts)
    except Exception:
        pass  # observation must never break the call

    if _WRITE_WARNINGS:
        body += "".join(w[1] for w in _WRITE_WARNINGS)
        _WRITE_WARNINGS.clear()

    if _FORMATTER_SKIPS and getattr(_DISPATCH_STATE, "depth", 1) <= 1:
        body += (
            "[formatters] skipped: " + ", ".join(_FORMATTER_SKIPS)
            + " — no config for it in the edited file's repo (#393)\n"
        )
        _FORMATTER_SKIPS.clear()

    # Swap in the compact header only if the op actually wrote — see the note
    # where it was built. The test is the write counter, not an ERROR prefix on
    # the receipt: `op_replace`'s zero-match returns "(0 occurrences of 'x'
    # found)", which is a failure that says nothing about being one, and that
    # is precisely the case where the caller needs the verbatim `old` back.
    if _compact_header and _WRITE_COUNT[0] > _writes_before:
        header = (f"--- {_compact_header}"
                  f"{_NO_EXCLUDE_SUFFIX if no_exclude else ''} ---\n")
    # Right file, wrong branch is silent until commit time, and supertool is
    # the thing that knows (#381). Success and failure both get it — a failed
    # edit is the exact moment a wrong-branch hypothesis should be available,
    # instead of being reached for only after re-reading the file.
    #
    # Once per call, never per sub-op: a batch runs each of its ops through this
    # same function recursively, so an unguarded footer would print the branch
    # once per edit — 50 identical lines for a 50-edit batch, which is the
    # opposite of the handful of tokens this is meant to cost. The batch itself
    # carries the single footer, and only when it actually mutated something.
    #
    # The "did anything get written" test is the write counter, not a flag set
    # in the batch loop: `"batch"` is not in `_OP_TARGETS`, so a mutation buried
    # in an INNER batch never propagated outward and a nested batch reported no
    # branch at all (#392). The counter is bumped at `_atomic_write`, which every
    # mutating op passes through however deeply it is nested.
    if getattr(_DISPATCH_STATE, "depth", 1) <= 1:
        if op in _OP_TARGETS or _MUTATION_ATTEMPTS[0] > _attempts_before:
            body += _branch_line()

    return header + body


# Read-op extractors → (path, line_start, line_end). Used by _notify_read_op.
# Each entry maps `op` to a function that takes the parsed `parts` list and
# returns either (path, line, line_end) or None if the op doesn't have a
# meaningful single-file/range to notify on.
def _read_target_around_line(parts: List[str]) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    # around_line:PATH:LINE[:N]   default N=10
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
    # read:PATH[:OFFSET:LIMIT|:full]
    if len(parts) < 2:
        return None
    path = parts[1]
    if len(parts) >= 4:
        try:
            offset = int(parts[2]); limit = int(parts[3])
            return (path, max(1, offset), offset + limit - 1)
        except (TypeError, ValueError):
            return (path, None, None)
    return (path, None, None)


def _read_target_file_only(parts: List[str]) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    if len(parts) < 2:
        return None
    return (parts[1], None, None)


def _read_target_between(parts: List[str]) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    # between:SYMBOL:PATH (resolve range via tree-sitter)
    # between:re:START:END:PATH (regex — line numbers unknown without re-running)
    if len(parts) < 3:
        return None
    if parts[1] == "re" and len(parts) >= 5:
        # Regex variant — return file only, no precomputable range
        return (parts[4], None, None)
    symbol = _normalize_symbol_query(parts[1])
    path = parts[2]
    if not _has_tree_sitter():
        return (path, None, None)
    ext = os.path.splitext(path)[1].lower()  # keep the leading dot — that's the key
    lang = _TS_LANG_MAP.get(ext)
    if not lang:
        return (path, None, None)
    found = _ts_find_node(path, lang, symbol)
    if found is None:
        return (path, None, None)
    node, _kind, _total = found
    start_line = node.start_point[0] + 1  # 0-indexed → 1-indexed
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
    "blame":       _read_target_file_only,
}


def _notify_read_op(op: str, parts: List[str]) -> None:
    """Fire notifiers for read ops (mutating ops fire inside _run_with_validators)."""
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
    """Build a short caller identity string for the log line.

    Claude Code doesn't expose session_id in env to Bash tools (it only
    appears in hook stdin payloads). The best session-stable proxy we have
    is PPID — the parent bash's PID stays the same within one Claude Code
    session, so grouping by ppid gives per-session totals.
    """
    user = os.environ.get("USER", "?")
    ppid = os.getppid()
    entry = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "?")
    return f"user={user} ppid={ppid} entry={entry}"


def log_call(args: List[str], out_bytes: int) -> None:
    """Append timestamped call log with caller id + output size.

    The ops count and out_bytes let post-analysis compute per-call cost and
    estimate round-trips saved vs a naive (one-op-per-call) baseline.
    """
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            meta = f"ops={len(args)} out={out_bytes}b"
            f.write(f"{timestamp} | {caller_tag()} | {meta} | {' '.join(args)}\n")
    except OSError:
        pass  # Logging is best-effort


# ---------------------------------------------------------------------------
# MCP client primitives
# ---------------------------------------------------------------------------

class MCPTimeout(Exception):
    """Raised when an MCP JSON-RPC call exceeds the configured timeout."""


class MCPServerError(Exception):
    """Raised when the MCP server returns a JSON-RPC error object."""

    def __init__(self, message: str, code: int = 0, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


# ---------------------------------------------------------------------------
# Module-level server registry + lifecycle
# ---------------------------------------------------------------------------

_MCP_SERVERS: Dict[str, MCPClient] = {}
_MCP_LOCK = threading.Lock()


def _mcp_shutdown_all() -> None:
    """Shut down all spawned MCP servers. Called by atexit + signal handlers."""
    with _MCP_LOCK:
        servers = list(_MCP_SERVERS.values())
    for server in servers:
        try:
            server.shutdown()
        except Exception:
            pass


atexit.register(_mcp_shutdown_all)


def _mcp_signal_handler(signum: int, frame: Any) -> None:
    _mcp_shutdown_all()
    # Re-raise default disposition
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


for _sig in (signal.SIGTERM, signal.SIGINT):
    try:
        signal.signal(_sig, _mcp_signal_handler)
    except (OSError, ValueError):
        pass  # Can't set signal handlers in non-main threads


# ---------------------------------------------------------------------------
# MCP config routing helpers (sub-PR 2)
# ---------------------------------------------------------------------------

def _mcp_route(path: str, op: str) -> Optional[Tuple[str, str]]:
    """Find (server_name, mcp_tool) for an op on this file's extension, or None."""
    if not path:
        return None
    # Iteration order matches config insertion order (Python 3.7+ dict);
    # first server whose `match` glob matches wins. Document at spec §6.
    for name, spec in _mcp_specs.items():
        glob = spec.get("match")
        if glob and _match_glob(path, glob):
            tool = (spec.get("tools") or {}).get(op)
            if tool:
                return (name, tool)
    return None


_MCP_DAEMON_SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "presets", "mcp", "daemon.py")
_MCP_STOP_SCRIPT = os.path.join(os.path.dirname(_MCP_DAEMON_SCRIPT), "stop.py")

# #148: socket/pid paths live under the per-user runtime dir, NOT /tmp. The
# daemon/status/stop helpers all compute them via _paths.socket_pid_paths — the
# client MUST use the same helper or it polls a path the daemon never binds.
_MCP_SOCKET_PID_PATHS_FN = None


def _mcp_socket_pid_paths(cwd: str, name: str) -> Tuple[str, str]:
    """Compute (sock_path, pid_path) via presets/mcp/_paths.py — the single source
    of truth the daemon binds with (#148).

    Loaded lazily by absolute file path under a unique module name: avoids
    prepending presets/mcp to the process-wide sys.path (where the generic name
    `_paths` could shadow other imports), and a missing/broken _paths.py only
    fails MCP ops instead of crashing the whole tool at import time.
    """
    global _MCP_SOCKET_PID_PATHS_FN
    if _MCP_SOCKET_PID_PATHS_FN is None:
        import importlib.util
        paths_file = os.path.join(os.path.dirname(_MCP_DAEMON_SCRIPT), "_paths.py")
        spec = importlib.util.spec_from_file_location("_supertool_mcp_paths", paths_file)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MCP_SOCKET_PID_PATHS_FN = mod.socket_pid_paths
    return _MCP_SOCKET_PID_PATHS_FN(cwd, name)


def _mcp_stop_server(name: str) -> None:
    """Best-effort SIGTERM the warm daemon for `name` via stop.py.

    The next op that touches this server cold-starts a fresh daemon, so its LSP
    re-indexes the workspace. Used by the new-file auto-invalidation path (#239):
    a just-created class isn't in the warm reflection cache, so a stale daemon
    reports phantom errors. Silent on any failure — invalidation is an
    optimization, never blocks the op.
    """
    import subprocess
    try:
        subprocess.run(
            [sys.executable, _MCP_STOP_SCRIPT, name],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _mcp_servers_to_stop_on_new_file(path: str) -> List[str]:
    """MCP servers whose `match` covers `path` and that opt into `stopOnNewFile`.

    Returns [] for non-LSP files or when no server opts in — the common case.
    """
    if not path:
        return []
    out: List[str] = []
    for name, spec in _mcp_specs.items():
        if not spec.get("stopOnNewFile"):
            continue
        glob = spec.get("match")
        if glob and _match_glob(path, glob):
            out.append(name)
    return out


class MCPClient:
    """MCP client that talks to a long-lived daemon over a Unix socket using NDJSON.

    Why: subprocess-per-call spawns the LSP server (intelephense etc.) cold every time,
    paying 30s+ indexing on each invocation. A persistent daemon keeps the LSP warm.

    Wire format: each JSON-RPC message is a single line terminated by `\n` (NDJSON).
    Matches what the official MCP Python SDK speaks over stdio.

    socket_path: optional override. Default = _paths.socket_pid_paths(cwd, name)[0]
    (per-user runtime dir, #148) — the same helper the daemon binds with.
    Tests pass an explicit path to talk to a pre-spawned mock server.
    """

    def __init__(self, name: str, timeout: int = 30, socket_path: Optional[str] = None) -> None:
        self.name = name
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._id_counter = 0
        self._id_lock = threading.Lock()
        self._buf = b""
        self._dead = False
        if socket_path:
            self._sock_path = socket_path
            self._auto_spawn = False
        else:
            cwd = os.path.abspath(os.getcwd())
            self._sock_path, _ = _mcp_socket_pid_paths(cwd, name)
            self._auto_spawn = True

    # Auto-spawn connect-retry budget. Cold-starting cclsp+intelephense on a
    # large repo (DVSI: 600K LOC) routinely takes 30-60s to bind the socket.
    # First attempt fires the detached spawn; subsequent attempts poll.
    # Override via SUPERTOOL_MCP_CONNECT_TIMEOUT (seconds).
    _CONNECT_TIMEOUT_SECONDS = 60

    def spawn(self) -> None:
        """Connect to daemon socket. Auto-spawn detached daemon if not running."""
        with self._lock:
            if self._sock is not None:
                return
            if not hasattr(socket, "AF_UNIX"):
                # GH-hosted Windows Python builds don't expose AF_UNIX even when
                # the OS supports it. Callers (_mcp_ensure_server) catch this
                # specific error and fall back to the non-MCP heuristic path.
                raise MCPServerError(
                    "MCP daemon requires socket.AF_UNIX — not available on this platform"
                )
            budget = float(os.environ.get("SUPERTOOL_MCP_CONNECT_TIMEOUT", self._CONNECT_TIMEOUT_SECONDS))
            # Explicit socket_path (tests, externally managed daemons) → no one
            # else will spawn it. Single-shot connect, fail fast on miss.
            # Polling the same dead path burns the full 60s budget for nothing.
            if not self._auto_spawn:
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(self.timeout)
                    s.connect(self._sock_path)
                    self._sock = s
                    return
                except (FileNotFoundError, ConnectionRefusedError) as e:
                    stderr_log = f"{self._sock_path}.stderr"
                    hint = (f"check {stderr_log} for cclsp/LSP startup errors"
                            if os.path.exists(stderr_log)
                            else "daemon never wrote a stderr log — check that mcp.<name>.cmd is on PATH")
                    raise MCPServerError(
                        f"MCP socket {self._sock_path} not reachable: {e}. {hint}"
                    )
            poll = 0.5
            deadline = time.time() + budget
            spawned = False
            while time.time() < deadline:
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(self.timeout)
                    s.connect(self._sock_path)
                    self._sock = s
                    return
                except (FileNotFoundError, ConnectionRefusedError):
                    if not spawned:
                        try:
                            subprocess.Popen(
                                [sys.executable, _MCP_DAEMON_SCRIPT, self.name, "--detach"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, close_fds=True,
                            )
                            spawned = True
                        except OSError:
                            pass
                    time.sleep(poll)
            stderr_log = f"{self._sock_path}.stderr"
            hint = (f"check {stderr_log} for cclsp/LSP startup errors"
                    if os.path.exists(stderr_log)
                    else "daemon never wrote a stderr log — check that mcp.<name>.cmd is on PATH")
            raise MCPServerError(
                f"MCP daemon for {self.name!r} did not bind {self._sock_path} within {budget:.0f}s. {hint}"
            )

    def is_alive(self) -> bool:
        return self._sock is not None and not self._dead

    def shutdown(self) -> None:
        """Close socket. Does NOT kill daemon (it stays alive for other clients)."""
        with self._lock:
            if self._sock is not None:
                try: self._sock.close()
                except OSError: pass
                self._sock = None

    def _next_id(self) -> int:
        with self._id_lock:
            self._id_counter += 1
            return self._id_counter

    def _send(self, payload: dict) -> None:
        if self._sock is None:
            raise RuntimeError(f"MCP daemon '{self.name}' not connected")
        line = (json.dumps(payload) + "\n").encode("utf-8")
        self._sock.sendall(line)

    def _recv_line(self) -> bytes:
        """Read one NDJSON-framed message (until newline). Honors self.timeout."""
        if self._sock is None:
            raise RuntimeError(f"MCP daemon '{self.name}' not connected")
        deadline = time.time() + self.timeout
        while b"\n" not in self._buf:
            remaining = deadline - time.time()
            if remaining <= 0:
                self._dead = True
                raise MCPTimeout(f"MCP daemon '{self.name}' read timed out after {self.timeout}s")
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                self._dead = True
                raise MCPTimeout(f"MCP daemon '{self.name}' read timed out after {self.timeout}s")
            if not chunk:
                self._dead = True
                raise MCPServerError(f"MCP daemon '{self.name}' closed connection")
            self._buf += chunk
        line, _, rest = self._buf.partition(b"\n")
        self._buf = rest
        return line

    def _call(self, method: str, params: Optional[dict] = None) -> Any:
        """Send a JSON-RPC request and wait for the matching response."""
        msg_id = self._next_id()
        payload = {"jsonrpc": "2.0", "method": method, "id": msg_id}
        if params is not None:
            payload["params"] = params
        with self._lock:
            self._send(payload)
            # Loop until we find OUR id (skip notifications/other responses)
            for _ in range(100):
                line = self._recv_line()
                msg = json.loads(line.decode("utf-8"))
                if msg.get("id") != msg_id:
                    continue  # not for us
                if "error" in msg:
                    err = msg["error"]
                    raise MCPServerError(
                        err.get("message", "unknown error"),
                        code=err.get("code", 0),
                        data=err.get("data"),
                    )
                return msg.get("result")
            raise MCPServerError(f"MCP daemon '{self.name}': no matching response for id={msg_id}")

    def initialize(self) -> dict:
        result = self._call("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "supertool", "version": VERSION},
        })
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        with self._lock:
            try: self._send(notif)
            except OSError: pass
        return result or {}

    def list_tools(self) -> List[dict]:
        result = self._call("tools/list")
        if isinstance(result, dict):
            return result.get("tools", [])
        return []

    def call_tool(self, name: str, args: dict) -> dict:
        result = self._call("tools/call", {"name": name, "arguments": args})
        if result is None:
            return {}
        return result


def _mcp_ensure_server(name: str):
    """Get-or-spawn an MCP client (daemon or subprocess transport). None on failure.

    Client connects to a long-lived daemon over Unix socket; daemon owns the real MCP
    server subprocess (cclsp, etc.) and keeps it warm across supertool invocations.
    """
    server = _mcp_get_server(name)
    if server is not None:
        return server
    spec = _mcp_specs.get(name)
    if spec is None:
        return None
    try:
        server = MCPClient(name=name, timeout=int(spec.get("timeout", 30)),
                           socket_path=spec.get("socket_path"))
        server.spawn()
        server.initialize()
    except (OSError, MCPServerError, MCPTimeout, KeyError):
        return None
    _mcp_register(name, server)
    return server


def _extract_refs_from_mcp_result(result: Any) -> Optional[List[str]]:
    """Normalize MCP response for a refs/references tool into a list of 'file:line:content' strings."""
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "").strip()
                if text:
                    return [line.rstrip() for line in text.splitlines() if line.strip()]
    return None


def _extract_symbols_from_mcp_result(result: Any) -> Optional[str]:
    """Normalize MCP response for a symbols/documentSymbol tool into a formatted string."""
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "").strip()
                if text:
                    return text + "\n"
    return None


def _extract_path_from_mcp_result(result: Any) -> Optional[str]:
    """Normalize MCP response into a single file path string.

    Handles a few common shapes produced by different MCP servers:
      - text content = single path or file:// URI
      - bullet list  = `• Name (kind) at /path:line:col` (cclsp find_workspace_symbols)
      - {uri: file://...} or {path: "/..."}
    """
    from urllib.parse import urlparse, unquote

    if not isinstance(result, dict):
        return None

    def _normalize_file_url_or_path(s: str) -> str:
        if s.startswith("file://"):
            parsed = urlparse(s)
            return unquote(parsed.path)
        return s

    def _extract_first_path_from_bullets(text: str) -> Optional[str]:
        # cclsp find_workspace_symbols shape:
        #   "Found N symbol(s) matching "Foo":\n\n• Foo (class) at /path/Foo.php:19:1\n..."
        # Grab the FIRST `at /path:line:col` we can find.
        m = re.search(r"\sat\s+(/[^\s:]+(?:\:[^\s:]+)*?)(?:\:\d+(?:\:\d+)?)?\s*$",
                      text, flags=re.MULTILINE)
        return m.group(1) if m else None

    # Shape 1: {"content": [{"type": "text", "text": "..."}]}
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "").strip()
                if not text:
                    continue
                # If it looks like a bullet listing, parse out the first path
                if "• " in text or "\n• " in text or text.startswith("Found "):
                    p = _extract_first_path_from_bullets(text)
                    if p:
                        return p
                return _normalize_file_url_or_path(text)
    # Shape 2: {"uri": "file:///path"}
    uri = result.get("uri")
    if isinstance(uri, str):
        return _normalize_file_url_or_path(uri)
    # Shape 3: {"path": "/path"}
    if isinstance(result.get("path"), str):
        return result["path"]
    return None


# ---------------------------------------------------------------------------
# Helper API for supertool ops (sub-PR 2 entry points)
# ---------------------------------------------------------------------------

def _mcp_get_server(name: str) -> Optional[MCPClient]:
    """Return a live MCPClient for *name* from the registry, or None.

    Removes dead servers so the registry stays clean. Does NOT spawn — callers
    that want lazy-spawn should use _mcp_register first or call _mcp_call with
    a spawn_factory.
    """
    with _MCP_LOCK:
        if name in _MCP_SERVERS:
            srv = _MCP_SERVERS[name]
            if srv.is_alive():
                return srv
            # Dead server — remove and let caller retry or return None
            del _MCP_SERVERS[name]
    return None


def _mcp_register(name: str, server: MCPClient) -> None:
    """Pre-register a server instance under *name*.

    Used by tests and by the sub-PR 2 config loader to inject servers before
    the first _mcp_call. Does not spawn or initialize — caller is responsible.
    """
    with _MCP_LOCK:
        _MCP_SERVERS[name] = server


def _mcp_call(server_name: str, tool: str, args: dict) -> Optional[dict]:
    """High-level: call a tool on a registered MCP server.

    Returns the result dict, or None if the server is not registered or any
    error occurs. Caller decides whether to retry or fall back.

    Lazy spawn contract
    -------------------
    This function does NOT spawn servers itself. To enable lazy-spawn, the
    caller must pre-register a server via _mcp_register() before the first
    call. _mcp_ensure_server() handles config-block parsing, lazy-spawn,
    and registration automatically (sub-PR 2).
    """
    server = _mcp_get_server(server_name)
    if server is None:
        return None
    try:
        return server.call_tool(tool, args)
    except (MCPTimeout, MCPServerError, OSError, EOFError, ValueError):
        return None


_AUTO_CWD_MARKER = ".supertool.json"


def _project_root_above_cwd() -> Optional[str]:
    """Nearest ancestor of cwd holding a .supertool.json, or None.

    Returns None when cwd IS a project root — nothing to recover from there.
    """
    d = os.path.realpath(os.getcwd())
    if os.path.isfile(os.path.join(d, _AUTO_CWD_MARKER)):
        return None
    parent = os.path.dirname(d)
    while parent and parent != d:
        if os.path.isfile(os.path.join(parent, _AUTO_CWD_MARKER)):
            return parent
        d, parent = parent, os.path.dirname(parent)
    return None


def _auto_cwd_root(argv: List[str]) -> Optional[str]:
    """Project root to chdir into so this call's path args resolve (#363).

    cwd drift (a `cd` into a subdir for a test run, then a root-relative op)
    used to die with "path not found … wrong CWD?" and cost two round-trips:
    read the error, retry with `cwd:`. Recover instead — but only when the
    evidence is unambiguous:

      * an ancestor dir carries a .supertool.json (explicit project marker),
      * no path-shaped arg resolves against the current cwd,
      * at least one path-shaped arg resolves against that root.

    Anything else returns None and the call runs exactly as before.
    """
    root = _project_root_above_cwd()
    if root is None:
        return None
    candidates: List[str] = []
    for arg in argv:
        if ":" not in arg:
            continue
        for tok in arg.split(":")[1:]:
            tok = tok.strip()
            if not tok or tok.startswith(("@", "-", "~", "/")):
                continue
            if "/" not in tok and "." not in tok:
                continue
            if WILDCARD_CHARS.search(tok):
                continue
            if os.path.exists(tok):
                return None  # resolves locally — cwd is right, leave it alone
            candidates.append(tok)
    for tok in candidates:
        if os.path.exists(os.path.join(root, tok)):
            return root
    return None


def main(argv: List[str]) -> int:
    # Cheap insurance: a stray glyph in user content must never crash the
    # process on a non-UTF-8 console (Windows cp1252). Runs even in plain mode.
    _reconfigure_stdout_utf8()

    # Every child we launch — presets, validators, formatters, notifiers — is a
    # separate process that inherits none of the reconfiguration above, and we
    # decode what it writes as UTF-8 (#415). So the writer is pinned to match
    # the reader here, once, instead of in each of the four spawn sites and
    # every preset script: on a cp1252 console a preset otherwise dies with
    # UnicodeEncodeError printing its own ✓ success line, and the work lands
    # while the receipt says it crashed — which invites the operator to run a
    # state-mutating op twice. An explicit `VAR=… cmd` prefix on an op still
    # overrides it, since that env is applied after os.environ is copied.
    os.environ["PYTHONIOENCODING"] = "utf-8"

    # --plain consumes the flag and exports SUPERTOOL_PLAIN=1 so preset
    # subprocesses (run via {python} {path}*.py) inherit it through the env.
    if "--plain" in argv:
        argv = [a for a in argv if a != "--plain"]
        os.environ["SUPERTOOL_PLAIN"] = "1"

    if not argv:
        sys.stderr.write(
            "Usage: supertool [--plain] op:args [op:args ...]\n"
            "       supertool 'read:file.py' 'grep:foo:src/:20' 'glob:**/*.md'\n"
        )
        return 1

    # cwd:PATH — must be the FIRST op. chdir once before any dispatch so every
    # remaining op resolves against PATH (mirrors `cd PATH && …`), then strip
    # it. Handled here in the pre-pass (like --plain) — never reaches dispatch,
    # so it can't race the parallel read path or force a batch sequential.
    # Required-first keeps the rule unambiguous: appearing later is an error,
    # not a silently-honored mid-call cwd switch.
    cwd_positions = [i for i, a in enumerate(argv) if a.split(":", 1)[0] == "cwd"]
    if cwd_positions:
        if len(cwd_positions) > 1:
            sys.stderr.write("cwd: only one cwd: op is allowed per call\n")
            return 1
        if cwd_positions != [0]:
            sys.stderr.write("cwd: must be the first op (cwd:PATH op1 op2 ...)\n")
            return 1
        spec = argv[0]
        if ":" not in spec:
            sys.stderr.write("cwd: requires a path (cwd:PATH)\n")
            return 1
        target = os.path.expanduser(os.path.expandvars(spec.split(":", 1)[1]))
        if not target:
            sys.stderr.write("cwd: empty path (cwd:PATH)\n")
            return 1
        if not os.path.isdir(target):
            sys.stderr.write(f"cwd: not a directory: {target}\n")
            return 1
        os.chdir(target)
        argv = argv[1:]
        if not argv:
            return 0
    else:
        # No explicit cwd: — recover from cwd drift when the args only make
        # sense from the project root (#363). Best-effort: never let a probe
        # failure break the call.
        try:
            auto_root = _auto_cwd_root(argv)
        except OSError:
            auto_root = None
        if auto_root:
            os.chdir(auto_root)
            sys.stdout.write(
                f"[cwd auto-resolved to project root: {auto_root}]\n")

    # At most one '@-' (stdin) op per call. sys.stdin is a single stream:
    # the first op's sys.stdin.read() drains it, so a second '@-' reads empty
    # and dies with an opaque '@file ... parse error' that names neither the
    # cause nor the fix. Detect the clash up front and point at the escape
    # hatches (per-op @file, or one batch:@- ops array). Issue #341.
    stdin_ops = [a for a in argv
                 if ":" in a and a.split(":", 1)[1].lstrip(":") == "@-"]
    if len(stdin_ops) > 1:
        sys.stderr.write(
            "stdin: only one '@-' op is allowed per call "
            f"(got {len(stdin_ops)}: {', '.join(stdin_ops)}). sys.stdin is a "
            "single stream — the second '@-' reads empty and fails. Give the "
            "others a file payload (e.g. edit:@.max/e1.toml), or fold them "
            "into one 'batch:@-' ops array.\n"
        )
        return 1

    # Loader warnings, once, before any op output. Skipping an unreadable
    # config beats a startup traceback that blocks every op — but a skip
    # nobody can see is the other way to lose (#418), so it goes to stderr,
    # which keeps op receipts on stdout clean.
    _preset_warnings = list(_load_config().get("_preset_warnings") or [])
    for _warning in list(_CONFIG_WARNINGS) + _preset_warnings:
        sys.stderr.write(f"supertool: {_warning}\n")

    # Normal batched-ops mode
    total_out_bytes = 0
    any_failure = False

    # Optional parallel execution — opt-in, only when every op is read-only.
    # Custom ops are excluded (could mutate via shell). Mixed batches stay
    # sequential to keep reasoning simple. Output order = input order.
    bodies: List[str]
    workers = _parallel_workers()
    parallel_path = (
        workers >= 2
        and len(argv) > 1
        and all(_is_parallel_safe(a) for a in argv)
    )
    # Defer formatters for multi-op sequential invocations (mutating ops).
    # Parallel path is read-only — no formatters fire there anyway.
    global _DEFER_FORMATTERS, _FORMAT_QUEUE, _VALIDATOR_DEFER_QUEUE, _VALIDATOR_DEFER_SEEN
    defer = len(argv) > 1 and not parallel_path
    if defer:
        _DEFER_FORMATTERS = True
        _FORMAT_QUEUE = {}
        _VALIDATOR_DEFER_QUEUE = []
        _VALIDATOR_DEFER_SEEN = set()

    try:
        if parallel_path:
            # Warm caches before threads so module-global init races are avoided
            _load_config()
            _has_rtk()
            _has_tree_sitter()
            _has_ctags()
            from concurrent.futures import ThreadPoolExecutor
            max_workers = min(workers, len(argv))
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                bodies = list(ex.map(dispatch, argv))
        else:
            bodies = [dispatch(a) for a in argv]
    finally:
        if defer:
            _DEFER_FORMATTERS = False

    for body in bodies:
        sys.stdout.write(body)
        total_out_bytes += len(body.encode("utf-8"))
        if _body_indicates_failure(body):
            any_failure = True

    # Drain deferred formatters now that every op has landed.
    if defer:
        drain_out = _drain_format_queue()
        if drain_out:
            sys.stdout.write(drain_out)
            total_out_bytes += len(drain_out.encode("utf-8"))
        validator_drain_out = _drain_validator_queue()
        if validator_drain_out:
            sys.stdout.write(validator_drain_out)
            total_out_bytes += len(validator_drain_out.encode("utf-8"))

    log_call(argv, total_out_bytes)
    return 1 if any_failure else 0


# Op failure marker — matches FAIL/ERROR emitted by supertool itself, not
# user content that happens to contain those words. Anchored to the line
# immediately after the '--- op:args ---' header so a grep result returning
# a line starting with 'ERROR:' won't trigger a false-positive exit code.
_FAIL_MARKER = re.compile(r"^---[^\n]*\n(FAIL\b|ERROR:\s)", re.MULTILINE)


def _body_indicates_failure(body: str) -> bool:
    """True iff the dispatch body's first content line starts with FAIL or ERROR:.

    Intentionally narrow: only the line immediately after the '--- header ---'
    counts. Deeper FAIL/ERROR strings are user content and must not flip the
    process exit code.
    """
    return _FAIL_MARKER.search(body) is not None


def _cli() -> int:
    return main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
