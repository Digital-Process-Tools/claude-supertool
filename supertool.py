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
    read:PATH:OFFSET:LIMIT     Read with offset and line limit
    grep:PATTERN:PATH          Search pattern (10 results default).
                                Auto-reads full file if PATH is a concrete
                                file < 20KB with a match.
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
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "0.11.0"

MAX_READ_LINES = 300
MAX_READ_BYTES = 20000  # ~20KB cap — prevents Claude Code "Output too large"
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


# Default exclude-paths applied to all traversal ops (glob, grep, tree, map).
# These are pruned at the directory-walk boundary — the dirs are never opened.
# Match is prefix-relative-to-cwd; trailing slash is normalised in _get_exclude_paths.
_DEFAULT_EXCLUDE_PATHS: Tuple[str, ...] = (
    ".git/", "node_modules/", ".svn/", ".hg/", ".idea/", ".vscode/",
    "__pycache__/", ".venv/", "venv/", "dist/", "build/",
    "phpstan-result-cache/", ".phpunit.cache/", ".rector/",
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

# MCP server specs parsed from _CONFIG["mcp"] — populated by _load_config()
_mcp_specs: Dict[str, dict] = {}

# Supertool install directory (where supertool.py actually lives, following symlinks)
_INSTALL_DIR = os.path.dirname(os.path.realpath(__file__))


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


def _resolve_preset_cmd(cmd: str, preset_dir: str) -> str:
    """Replace {path} placeholder with the preset's directory (trailing slash).

    Example: 'python3 {path}gitlab/issue.py {arg}'
    becomes: 'python3 /home/user/.local/supertool/presets/gitlab/issue.py {arg}'
    """
    path_prefix = preset_dir.rstrip("/") + "/"
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
            with open(preset_path) as f:
                preset_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            config.setdefault("_preset_warnings", []).append(
                f"preset {name!r}: failed to load {preset_path}"
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

    # Project-level ops override preset ops
    merged_ops.update(project_ops)
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
                with open(candidate) as f:
                    _CONFIG = json.load(f)
                    project_dir = d
                    _merge_presets(_CONFIG, project_dir)
                    # Parse MCP server specs from the optional "mcp" block.
                    mcp_block = _CONFIG.get("mcp")
                    if isinstance(mcp_block, dict):
                        for srv_name, spec in mcp_block.items():
                            if isinstance(spec, dict) and "cmd" in spec:
                                _mcp_specs[srv_name] = spec
                    return _CONFIG
            except (json.JSONDecodeError, OSError):
                pass
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    _CONFIG = {}
    return _CONFIG


def _is_compact() -> bool:
    """Check if compact mode is enabled in .supertool.json."""
    return bool(_load_config().get("compact", False))


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
    """Return True if rel_path starts with any of the exclude prefixes.

    rel_path should be relative to cwd and use os.sep.  The comparison
    normalises separators and strips a leading './' so callers don't need to.
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
    for prefix in exclude_paths:
        if normalised.startswith(prefix):
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


def _rtk_enabled() -> bool:
    """Check if RTK delegation is enabled in .supertool.json. Default: true."""
    return bool(_load_config().get("rtk", True))


# RTK integration — when rtk is installed, delegate read/grep/wc for compressed output
_RTK_PATH: str | None = None
_RTK_CHECKED = False


def _has_rtk() -> str | None:
    """Return rtk binary path if available, None otherwise. Cached."""
    global _RTK_PATH, _RTK_CHECKED
    if not _RTK_CHECKED:
        _RTK_CHECKED = True
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
_BUILTIN_OPS = {"read", "grep", "grep_around", "glob", "ls", "tail", "head", "wc", "check", "around", "map", "diff", "stat", "around_line", "tree", "replace", "replace_dry", "edit", "replace_lines", "paste", "vi", "validate", "format", "validate_staged", "format_staged", "workspace", "resolve"}

# Read-only built-in ops — safe to run in parallel across a batch.
# Excludes mutating ops (replace, edit, replace_lines) and custom ops
# (could shell out to anything). `between` is included — pure file read.
_PARALLEL_SAFE_OPS = {
    "read", "grep", "glob", "ls", "head", "tail", "wc", "stat",
    "map", "tree", "around", "around_line", "between", "diff", "blame",
    "version", "validate", "validate_staged", "format_staged", "workspace",
    "resolve",
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

def _resolve_custom_op(op: str, parts: List[str]) -> str | None:
    """Try to run op as a custom shell command from config["ops"].

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

    # Build the command — replace {file}, {dir}, {arg}, {args}, {argjoin} placeholders
    file_arg = parts[1] if len(parts) > 1 else ""
    cmd = cmd_template.replace("{file}", shlex.quote(file_arg))
    dir_arg = os.path.dirname(file_arg) if file_arg else "."
    cmd = cmd.replace("{dir}", shlex.quote(dir_arg))
    cmd = cmd.replace("{arg}", shlex.quote(file_arg))
    all_args = " ".join(shlex.quote(p) for p in parts[1:]) if len(parts) > 1 else ""
    cmd = cmd.replace("{args}", all_args)
    # {argjoin}: parts[1:] rejoined with ':::' as a single shell-quoted arg.
    # Lets the receiving script split fields itself when they contain colons
    # (e.g. XPath like .//ns:tag or [position()=1]).
    arg_join = ":::".join(parts[1:]) if len(parts) > 1 else ""
    cmd = cmd.replace("{argjoin}", shlex.quote(arg_join))

    # Pass extra config keys as SUPERTOOL_ env vars
    _RESERVED_KEYS = {"cmd", "timeout", "description", "syntax", "example", "status"}
    env = dict(os.environ)
    if isinstance(entry, dict):
        for k, v in entry.items():
            if k not in _RESERVED_KEYS:
                env[f"SUPERTOOL_{k.upper()}"] = str(v)

    t0 = time.monotonic()
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout,
            env=env,
        )
        elapsed = time.monotonic() - t0
        output = result.stdout
        if result.returncode != 0:
            if result.stderr:
                output += result.stderr
            return f"FAIL ({elapsed:.2f}s)\n{output}"
        return f"PASS ({elapsed:.2f}s)\n{output}"
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        return f"FAIL (timeout {elapsed:.1f}s > {timeout}s)\n"
    except OSError as e:
        return f"FAIL: {e}\n"


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
        for expanded_op in op_list:
            resolved = expanded_op.replace("{file}", file_arg)
            resolved = resolved.replace("{dir}", dir_arg)
            resolved = resolved.replace("{arg}", file_arg)
            resolved = resolved.replace("{args}", all_args)
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
    """
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
    out = [f"({line_count} lines, {size} bytes)\n"]
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
    return render_file(path, offset, limit, grep_filter, force_full)


def op_grep(pattern: str, path: str = ".", limit: int = 0,
            context: int = 0, count_only: bool = False,
            no_exclude: bool = False) -> str:
    """Search pattern recursively. Auto-reads small single file on match.

    When context > 0, emits N lines before/after each match in grep -C style:
      match lines:   path:lineno:content  (colon separator)
      context lines: path-lineno-content  (dash separator)
    Non-adjacent groups are separated by --.
    Auto-read is skipped when context > 0 (output already contains context).

    When count_only=True, returns match counts per file instead of content.
    """
    if limit <= 0:
        limit = _get_op_int("grep", "max_results", MAX_GREP_RESULTS)
    if not pattern:
        return "ERROR: empty pattern\n"

    # Auto-convert bash grep BRE alternation (\|) to Python regex (|)
    if "\\|" in pattern:
        pattern = pattern.replace("\\|", "|")

    # Early exit if path doesn't exist (don't silently return 0 results)
    if path != "." and not os.path.isfile(path) and not os.path.isdir(path):
        # Could be a glob pattern — check if it expands to anything
        from glob import glob as _glob
        if not _glob(path, recursive=True):
            return f"ERROR: path not found: {path}\n"

    excl = _get_exclude_paths("grep", no_exclude)

    # RTK delegation — basic grep (no context, no count). Thread excludes through
    # via grep's --exclude-dir for single-segment prefixes (.git/, node_modules/,
    # etc.). Multi-segment prefixes (e.g. "Dvsi/dvsi-private/libs/") can't be
    # expressed as --exclude-dir; fall through to the native walker in that case.
    if not count_only and context == 0 and _rtk_enabled() and _has_rtk():
        single, multi = _split_exclude_prefixes(excl)
        if not multi:
            rtk_args = ["grep", "-rn", "-m", str(limit)]
            for d in single:
                rtk_args.append(f"--exclude-dir={d}")
            rtk_args.extend([pattern, path])
            rtk_out = _rtk_run(rtk_args)
            if rtk_out is not None:
                return rtk_out + "\n"

    if count_only:
        counts = _grep_count(pattern, path, limit, excl)
        total = sum(counts.values())
        file_count = len(counts)
        out = [f"({total} total matches across {file_count} files)\n"]
        for fp, cnt in sorted(counts.items()):
            out.append(f"{fp}:{cnt}\n")
        out.append("\n")
        return "".join(out)

    if context > 0:
        groups = _grep_recursive_context(pattern, path, limit, context, excl)
        count = sum(
            1 for g in groups for line in g if line[2] == "match"
        )
        file_count = len({g[0][0] for g in groups if g})
        out = [f"({count} results in {file_count} files, "
               f"limit {limit}, context {context})\n"]
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
                if kind == "match":
                    out.append(f"  {lineno}:{content}\n")
                else:
                    out.append(f"  {lineno}-{content}\n")
        out.append("\n")
        return "".join(out)

    hits = _grep_recursive(pattern, path, limit, excl)
    count = len(hits)
    file_count = len({h.split(":", 1)[0] for h in hits if ":" in h})

    out = [f"({count} results in {file_count} files, limit {limit})\n"]
    current_file = ""
    for hit in hits:
        # hits are "path:lineno:content" — split on first two colons
        parts = hit.split(":", 2)
        if len(parts) >= 3:
            fp, lineno, content = parts[0], parts[1], parts[2]
            if fp != current_file:
                current_file = fp
                out.append(f"{fp}\n")
            out.append(f"  {lineno}:{content}\n")
        else:
            out.append(hit + "\n")
    out.append("\n")

    # Auto-read: single small file + at least one match → emit full file
    if (count > 0
            and os.path.isfile(path)
            and os.path.getsize(path) < _get_op_int("read", "max_bytes", MAX_READ_BYTES)):
        out.append(f"[auto-read: single file < {_get_op_int('read', 'max_bytes', MAX_READ_BYTES)} bytes, "
                   "match found]\n")
        out.append(render_file(path, 0, _get_op_int("read", "max_lines", MAX_READ_LINES)))

    return "".join(out)


_AROUND_DIR_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", "vendor"}
_AROUND_DIR_MAX_FILES = 20


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
                rendered = _around_one_file(regex, fpath, n)
                if rendered and rendered.startswith("ERROR:"):
                    continue
                if not rendered:
                    continue
                rel = os.path.relpath(fpath, path)
                hits.append(f"=== {rel} ===\n{rendered}")
                if len(hits) >= _AROUND_DIR_MAX_FILES:
                    break
            if len(hits) >= _AROUND_DIR_MAX_FILES:
                break
        if not hits:
            return f"(no match for {pattern!r} in {path}, scanned {scanned} file(s))\n\n"
        header = f"(matched {len(hits)} file(s) under {path}"
        if len(hits) >= _AROUND_DIR_MAX_FILES:
            header += f", capped at {_AROUND_DIR_MAX_FILES}"
        header += f", scanned {scanned})\n"
        return header + "".join(hits)

    if not os.path.isfile(path):
        return f"ERROR: file not found: {path}\n"

    rendered = _around_one_file(regex, path, n)
    if not rendered:
        return f"(no match for {pattern!r} in {path})\n\n"
    return rendered


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
    if found is None:
        return f"ERROR: symbol {symbol!r} not found in {path}\n"
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

    files = _glob_files(pattern, _get_exclude_paths("glob", no_exclude))
    # Strip common directory prefix when 2+ files share one
    prefix = ""
    if len(files) >= 2:
        prefix = os.path.commonpath(files)
        if prefix and not prefix.endswith(os.sep):
            prefix += os.sep
        # Only strip if it saves something meaningful (> 10 chars)
        if len(prefix) <= 10:
            prefix = ""
    out = [f"({len(files)} files)\n"]
    if prefix:
        out.append(f"{prefix}\n")
        for f in files:
            out.append(f"  {f[len(prefix):]}\n")
    else:
        for f in files:
            out.append(f + "\n")
    out.append("\n")

    # Auto-read: glob returned exactly 1 file — save the follow-up read round-trip
    if not no_auto_read and len(files) == 1 and os.path.getsize(files[0]) < _get_op_int("read", "max_bytes", MAX_READ_BYTES):
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


def op_tail(path: str, n: int = 20) -> str:
    if not path or not os.path.isfile(path):
        return f"ERROR: file not found: {path}\n"
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
    return f"{lines} {words} {chars} {path}\n"


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
        with open(path1, "r", errors="replace") as f:
            lines1 = f.readlines()
        with open(path2, "r", errors="replace") as f:
            lines2 = f.readlines()
    except OSError as e:
        return f"ERROR: could not read file: {e}\n"

    diff = list(difflib.unified_diff(
        lines1, lines2, fromfile=path1, tofile=path2, lineterm=""
    ))
    if not diff:
        return "files are identical\n"
    return "\n".join(diff) + "\n"


def op_stat(path: str) -> str:
    """Show file or directory metadata: size and last modified time."""
    if not path:
        return "ERROR: empty path\n"
    if not os.path.exists(path):
        return f"ERROR: not found: {path}\n"

    try:
        st = os.stat(path)
    except OSError as e:
        return f"ERROR: could not stat {path}: {e}\n"

    size = st.st_size
    modified = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    kind = "dir" if os.path.isdir(path) else "file"
    return f"{size} {modified} {kind} {path}\n"


def op_around_line(path: str, line: int, n: int = 10) -> str:
    """Show N lines of context around a specific line number."""
    if not path or not os.path.isfile(path):
        return f"ERROR: file not found: {path}\n"
    if line < 1:
        return f"ERROR: line number must be >= 1, got {line}\n"

    try:
        with open(path, "r", errors="replace") as f:
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
                rel = os.path.relpath(os.path.join(dir_path, d), cwd)
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
        with open(path, "r", errors="replace") as f:
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
    out = [f"{path} ({line_count} lines)\n"]
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
    out = [f"{path} ({line_count} lines)\n"]
    for kind, name, line, scope in symbols:
        depth = 1 if scope else 0
        indent = "  " * (depth + 1)
        out.append(f"{indent}{kind} {name}  [{line}]\n")
    return "".join(out)


# Supported extensions for map scanning
_MAP_EXTENSIONS = frozenset(
    list(_TS_LANG_MAP.keys()) + list(_REGEX_PATTERNS.keys())
)


def _count_lines(path: str) -> int:
    """Count lines in a file (fast, doesn't read into memory if big)."""
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


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
        rel_root = os.path.relpath(root, cwd)
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
            out_files.append(f"{fpath} ({line_count} lines)\n  (no symbols)\n")

    out = [f"({len(files)} files, tier: {actual_tier})\n"] + out_files
    if truncated:
        out.append(f"\n... (truncated at {MAX_MAP_FILES} files)\n")
    out.append("\n")
    return "".join(out)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _grep_count(
    pattern: str, path: str, limit: int,
    exclude_paths: Tuple[str, ...] = ()
) -> Dict[str, int]:
    """Return match counts per file as {filepath: count}."""
    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(re.escape(pattern))

    counts: Dict[str, int] = {}
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
    mutation) so their subtrees are never opened.
    """
    candidates: List[str] = []
    if os.path.isfile(path):
        candidates.append(path)
    elif os.path.isdir(path):
        exts = _grep_file_includes()  # None = all files
        cwd = os.getcwd()
        for root, dirs, files in os.walk(path):
            if exclude_paths:
                rel_root = os.path.relpath(root, cwd)
                dirs[:] = [
                    d for d in dirs
                    if not _is_excluded(os.path.join(rel_root, d), exclude_paths)
                ]
            for name in files:
                if exts is None or any(name.endswith(ext.lstrip("*")) for ext in exts):
                    candidates.append(os.path.join(root, name))
    return candidates


def _grep_recursive(
    pattern: str, path: str, limit: int,
    exclude_paths: Tuple[str, ...] = ()
) -> List[str]:
    """Return up to `limit` match lines as 'path:lineno:content' strings.

    Filters by common code/doc extensions when walking directories.
    Always searches when `path` is a single file.
    """
    try:
        regex = re.compile(pattern)
    except re.error:
        # Fall back to literal substring
        regex = re.compile(re.escape(pattern))

    results: List[str] = []
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
                        results.append(f"{file_path}:{lineno}:{line.rstrip()}")
                        if len(results) >= limit:
                            break
        except OSError:
            continue
    return results


def _grep_recursive_context(
    pattern: str, path: str, limit: int, context: int,
    exclude_paths: Tuple[str, ...] = ()
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
                group.append((file_path, i + 1, kind, lines[i]))
                if kind == "match":
                    match_count += 1
            groups.append(group)

    return groups


def _glob_files(
    pattern: str, exclude_paths: Tuple[str, ...] = ()
) -> List[str]:
    """Glob matching files, supports ** recursive. Returns up to MAX_GLOB_RESULTS.

    When exclude_paths is provided and the pattern contains '**', uses an
    os.walk-based implementation that prunes excluded directories at the walk
    boundary (never opens them).  For non-recursive patterns, falls back to
    glob.glob and filters results post-hoc (no subtree to prune anyway).
    """
    max_results = _get_op_int("glob", "max_results", MAX_GLOB_RESULTS)

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
        files: List[str] = []
        for root, dirs, filenames in os.walk(root_part):
            rel_root = os.path.relpath(root, cwd)
            dirs[:] = sorted(
                d for d in dirs
                if not _is_excluded(os.path.join(rel_root, d), exclude_paths)
            )
            for name in sorted(filenames):
                full = os.path.join(root, name)
                # Match the tail pattern against the relative path from root_part
                rel_from_root = os.path.relpath(full, root_part)
                if not tail or fnmatch.fnmatch(name, tail) or fnmatch.fnmatch(rel_from_root, tail):
                    if os.path.isfile(full):
                        files.append(full)
                        if len(files) >= max_results:
                            return files
        return files

    from glob import glob
    matches = sorted(glob(pattern, recursive=True))
    files_out = [m for m in matches if os.path.isfile(m)]
    if exclude_paths:
        cwd = os.getcwd()
        files_out = [
            m for m in files_out
            if not _is_excluded(os.path.relpath(m, cwd), exclude_paths)
        ]
    return files_out[:max_results]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_DRIVE_LETTER = re.compile(r"^[A-Za-z]$")
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
            is_drive = (
                _DRIVE_LETTER.match(last_seg) is not None
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
        return ("", ".", _get_op_int("grep", "max_results", MAX_GREP_RESULTS), 0, False)

    # Peel known trailing fields from the right
    count_only = False
    if args and args[-1] == "count":
        count_only = True
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

    return (pattern, path, limit, context, count_only)


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

    candidates = _grep_candidates(path)
    if not candidates:
        return "(0 files to search)\n"

    # Collect matches via whole-file scan so multi-line `old` patterns work.
    # Line-by-line matching would silently miss any pattern containing '\n'.
    file_matches: List[Tuple[str, List[int]]] = []  # (filepath, [match_start_offsets])
    total_count = 0
    for file_path in candidates:
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
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
            out.append(f"\n{filepath}\n")
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
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
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
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
        out.append(f"  {fp} ({cnt})\n")
    out.append(f"\nDone: '{old}' → '{new}'\n")
    return "".join(out)


def _atomic_write(path: str, content: str) -> None:
    """Write content to path atomically — temp file + os.replace.

    Crash-safe: if interrupted mid-write, the original file is preserved
    (the temp file is incomplete but the target path still has old data).
    """
    import tempfile
    target_dir = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=".supertool-", suffix=".tmp", dir=target_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        return f"ERROR: failed to read {path}: {e}\n"

    count = content.count(old)
    if count == 0:
        return f"ERROR: old string not found in {path}\n"
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
    parent = os.path.dirname(path)
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
        with open(path, "r", encoding="utf-8", errors="replace") as f:
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
    cache_dir = os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
        "supertool",
        "vim-cursor",
    )
    return os.path.join(cache_dir, digest)


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
    cache_dir = os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
        "supertool",
        "vim-undo",
    )
    return os.path.join(cache_dir, digest + ".last")


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


def _vim_render_lint(path: str) -> str:
    """Post-edit syntax lint based on file extension.

    Returns "" when no lint applies (unknown ext or missing binary).
    On success: '--- lint: <tool> ---\\n<output>\\n'.
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

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
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
            def _run_sub(_rx):
                if sub_start == 0 and sub_end == len(content):
                    return _rx.subn(srepl_safe, content, count=n_max)
                # ranged path — iterate per line in the range so non-/g
                # replaces the first match on EACH line (vim behavior),
                # not just the first match in the whole range.
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
                log.append(f"  {i}. :!{cmd} ({len(out)} chars inserted) ⚠ SHELL EXECUTION (cmd ran with shell=True, no sanitization)")
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
                    f" ⚠ SHELL EXECUTION (cmd ran with shell=True, no sanitization)"
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
                        # Replay :!cmd — re-parse lc_arg (same \x1d encoding as original handler)
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
            f"> ⚠ Output is {len(body.encode('utf-8'))} bytes, exceeds the "
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
    "vim":           lambda parts: parts[1] if len(parts) > 1 else "",
}


def _applicable_validators(op: str, path: str) -> Dict[str, Dict[str, Any]]:
    """Return validators that should wrap this op call. Skips opt_in."""
    cfg = _load_config()
    validators = cfg.get("validators") or {}
    if not validators:
        return {}
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
        out[name] = spec
    return out


def _validator_resolve(spec: Dict[str, Any], file: str) -> Optional[str]:
    """Run optional `resolve` cmd to map source→target (e.g. source→test).

    Returns the resolved path, original file if no resolve cmd, or None if
    the resolve cmd succeeded but returned empty (signal: skip this validator).
    """
    if "resolve" not in spec:
        return file
    import subprocess
    cmd = spec["resolve"].replace("{supertool_dir}", _INSTALL_DIR).replace("{file}", file)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        resolved = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
        return resolved if resolved else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _validator_cache_enabled() -> bool:
    if os.environ.get("SUPERTOOL_NO_VALIDATOR_CACHE"):
        return False
    return bool(_load_config().get("validator_cache", True))


def _validator_cache_key(file_path: str, name: str, cmd: str) -> Optional[str]:
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
    return h.hexdigest()


def _validator_cache_path(key: str) -> Path:
    return Path.home() / ".cache" / "supertool" / "validators" / f"{key}.json"


def _validator_cache_read(key: str) -> Optional[Dict[str, Any]]:
    import json
    p = _validator_cache_path(key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _validator_cache_write(key: str, data: Dict[str, Any]) -> None:
    import json
    p = _validator_cache_path(key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))
    except OSError:
        pass


def _validator_run_one(name: str, spec: Dict[str, Any], file: str) -> Optional[Dict[str, Any]]:
    """Run one validator adapter on `file`. Returns SCHEMA.md-compliant dict.

    Adapter contract: prints one JSON object on last stdout line. Exit 0 unless
    infra fail. Failures here produce a synthetic error dict so the row still
    renders. Cached by (file content hash, name, cmd) at
    ~/.cache/supertool/validators/<sha256>.json.
    """
    import subprocess
    import json
    target = _validator_resolve(spec, file)
    if target is None:
        return {"tool": name, "skipped": "no target resolved"}
    cmd = spec["cmd"].replace("{supertool_dir}", _INSTALL_DIR).replace("{file}", target)
    timeout = int(spec.get("timeout", 60))

    cache_key: Optional[str] = None
    if _validator_cache_enabled():
        cache_key = _validator_cache_key(target, name, cmd)
        if cache_key:
            cached = _validator_cache_read(cache_key)
            if cached is not None:
                return cached

    spec_env = spec.get("env") or {}
    run_env = {**os.environ, **{str(k): str(v) for k, v in spec_env.items()}} if spec_env else None

    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout,
                           env=run_env)
        out = r.stdout.strip()
        if not out:
            return {"tool": name, "file": target, "ok": False, "count": 1,
                    "errors": [{"line": None, "col": None, "severity": "error",
                                "code": "orchestrator", "msg": "adapter produced no output"}],
                    "duration_ms": 0}
        data = json.loads(out.splitlines()[-1])
        if target != file:
            data["resolved_to"] = target
        if cache_key:
            _validator_cache_write(cache_key, data)
        return data
    except subprocess.TimeoutExpired:
        return {"tool": name, "file": target, "ok": False, "count": 1,
                "errors": [{"line": None, "col": None, "severity": "error",
                            "code": "orchestrator", "msg": f"timeout after {timeout}s"}],
                "duration_ms": timeout * 1000}
    except (json.JSONDecodeError, IndexError) as e:
        return {"tool": name, "file": target, "ok": False, "count": 1,
                "errors": [{"line": None, "col": None, "severity": "error",
                            "code": "orchestrator", "msg": f"adapter bad json: {e}"}],
                "duration_ms": 0}


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
        return [f"{data['tool']:8s}: skipped — {data['skipped']}"]
    tool = data.get("tool", "?")
    ok = data.get("ok", False)
    count = data.get("count", 0)
    dur = data.get("duration_ms", 0)
    status = "ok" if ok else f"{count} err"
    line = f"{tool:8s}: {status:12s} ({dur}ms)"
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


def _validator_render_diff(before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> list:
    if "skipped" in after:
        return [f"{after['tool']:8s}: skipped"]
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
            marker = "\u2713" if a_ok else "\u2717"
            return [f"{tool:8s}: {', '.join(metric_parts)} {marker}"]
        # Truly unchanged — fold the most relevant absolute metric into the row.
        if a_ok and a_metrics:
            primary = None
            for k in ("tests_total", "tests_passed", "changes_count"):
                if k in a_metrics:
                    primary = (k, a_metrics[k]); break
            if primary is not None:
                return [f"{tool:8s}: ok {primary[0]}={primary[1]} (unchanged) \u00b7"]
        status = "ok" if a_ok else f"{a_count} err"
        return [f"{tool:8s}: {status:12s} (unchanged) \u00b7"]
    marker = "✓" if a_ok else ("⚠" if delta < 0 else "✗")
    arrow = f"{b_count} → {a_count}"
    sign = f"({'+' if delta >= 0 else ''}{delta})"
    out = [f"{tool:8s}: {arrow:12s} {sign:6s} {marker}"]
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

def _applicable_formatters(op: str, path: str) -> Dict[str, Dict[str, Any]]:
    """Return formatters that should run after this op. Same logic as validators."""
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
    cmd = spec["cmd"].replace("{supertool_dir}", _INSTALL_DIR).replace("{file}", file)
    timeout = int(spec.get("timeout", 30))
    spec_env = spec.get("env") or {}
    run_env = {**os.environ, **{str(k): str(v) for k, v in spec_env.items()}} if spec_env else None
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout,
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


def _run_with_validators(op: str, parts: Any, do_op: Any) -> str:
    """Wrap edit op with format+snapshot+run+diff using configured formatters/validators.

    Run order: edit → formatter(s) → validator(s) → rollback if validate fails.
    No-op when op not in _OP_TARGETS, no target path, or no applicable
    formatters/validators. Guarantees `do_op()` runs in all paths.
    """
    extract = _OP_TARGETS.get(op)
    if not extract:
        return do_op()
    try:
        path = extract(parts)
    except (IndexError, TypeError):
        return do_op()
    if not path:
        return do_op()
    applicable_fmt = _applicable_formatters(op, path)
    applicable = _applicable_validators(op, path)
    if not applicable_fmt and not applicable:
        return do_op()

    needs_rollback = any(v.get("rollback_on_fail") for v in applicable.values())
    needs_fmt_rollback = any(v.get("rollback_on_fail") for v in applicable_fmt.values())

    pre_content: Optional[bytes] = None
    if (needs_rollback or needs_fmt_rollback) and os.path.isfile(path):
        try:
            with open(path, "rb") as f:
                pre_content = f.read()
        except OSError:
            pre_content = None

    before = _validators_run_batch(applicable, path) if applicable else {}

    body = do_op()

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

    after_results = _validators_run_batch(applicable, path) if applicable else {}
    diff_lines: list = []
    for name in applicable:  # stable order from config
        if name in after_results:
            diff_lines.extend(_validator_render_diff(before.get(name), after_results[name]))

    diff_out = "\n".join(diff_lines) + ("\n" if diff_lines else "")

    if needs_rollback and pre_content is not None:
        for name, spec in applicable.items():
            if not spec.get("rollback_on_fail"):
                continue
            for line in diff_lines:
                if line.lstrip().startswith(name) and "✗" in line:
                    try:
                        with open(path, "wb") as f:
                            f.write(pre_content)
                        diff_out += f"\n[rolled back] {name} regressed; file restored\n"
                    except OSError as e:
                        diff_out += f"\n[ROLLBACK FAILED] {name}: {e}\n"
                    break

    suffix = ""
    if fmt_rows:  # silent when all formatters are no-op
        suffix += "\n[formatters]\n" + "\n".join(fmt_rows) + "\n"
    if applicable:
        suffix += "\n[validators]\n" + diff_out

    return body + suffix


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
        validators = {k: v for k, v in validators.items() if k in tool_filter}
        if not validators:
            return "no validators matched filter\n"
    import fnmatch
    out = [f"validate: {path}"]
    for name, spec in validators.items():
        glob = spec.get("match", "*")
        if path and glob and not _match_glob(path, glob):
            continue
        data = _validator_run_one(name, spec, path)
        if data is None:
            continue
        out.extend(_validator_render_row(data, verbose=verbose))
    return "\n".join(out) + "\n"


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
                    result = _mcp_call(server_name, mcp_tool, {"symbol": symbol, "file": from_file})
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
                    return f"{symbol} → {os.path.relpath(h)}\n"
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
                    rel = os.path.relpath(candidate)
                    return f"{symbol} → {rel}\n"
            # Also try as a package (directory with __init__.py)
            pkg_init = os.path.join(target_dir, module_path, "__init__.py")
            if os.path.isfile(pkg_init):
                rel = os.path.relpath(pkg_init)
                return f"{symbol} → {rel}\n"
            return f"{symbol} → not found\n"
        else:
            # Bare "." or ".." — refers to the package itself
            pkg_init = os.path.join(target_dir, "__init__.py")
            if os.path.isfile(pkg_init):
                rel = os.path.relpath(pkg_init)
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
                return f"{symbol} → {os.path.relpath(h)}\n"
        return f"{symbol} → not found\n"

    # ── Relative path (starts with ./ or ../) ────────────────────────────────
    if symbol.startswith("./") or symbol.startswith("../"):
        base = symbol
        # Try adding common extensions if no extension present
        if not os.path.splitext(base)[1]:
            for ext in (".ts", ".tsx", ".js", ".jsx", ".py", ".php"):
                candidate = base + ext
                if os.path.isfile(candidate):
                    rel = os.path.relpath(candidate)
                    return f"{symbol} → {rel}\n"
            # Also try .class.php
            candidate = base + ".class.php"
            if os.path.isfile(candidate):
                rel = os.path.relpath(candidate)
                return f"{symbol} → {rel}\n"
        else:
            if os.path.isfile(base):
                rel = os.path.relpath(base)
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
                rel = os.path.relpath(hits[0])
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
    out.append(f"({line_count} lines, {size} bytes)\n")
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

    # ── Section 2: Symbols ───────────────────────────────────────────────────
    out.append("## Symbols\n\n")
    _sym_mcp_used = False
    _sym_route = _mcp_route(path, "symbols")
    if _sym_route:
        _sym_server_name, _sym_mcp_tool = _sym_route
        _sym_server = _mcp_ensure_server(_sym_server_name)
        if _sym_server:
            try:
                _sym_mcp_result = _mcp_call(_sym_server_name, _sym_mcp_tool, {"file": os.path.abspath(path)})
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
                _refs_mcp_result = _mcp_call(_refs_server_name, _refs_mcp_tool, {"symbol": symbol, "file": os.path.abspath(path)})
                if _refs_mcp_result is not None:
                    _mcp_refs = _extract_refs_from_mcp_result(_refs_mcp_result)
                    if _mcp_refs is not None:
                        filtered_hits = _mcp_refs
                        _refs_mcp_used = True
            except (MCPServerError, MCPTimeout):
                pass
    if not _refs_mcp_used:
        excl = _get_exclude_paths("grep")
        hits = _grep_recursive(symbol, ".", 200, excl)
        abs_path = os.path.abspath(path)
        ext_family = _EXT_FAMILIES.get(ext, (ext,)) if ext else ()
        _test_marker = re.compile(r"(?:^|/)(?:test_[^/]+|[^/]+_test|[^/]+Test)\.[^/]+$")
        filtered_hits = []
        for hit in hits:
            # hit format: "filepath:lineno:content"
            colon1 = hit.index(":")
            hit_file = hit[:colon1]
            if os.path.abspath(hit_file) == abs_path:
                continue
            if ext_family and not any(hit_file.endswith(e) for e in ext_family):
                continue
            if _test_marker.search(hit_file):
                continue
            filtered_hits.append(hit)

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
            colon1 = hit.index(":")
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


def op_format(path: str, tool_filter: Optional[list] = None, verbose: bool = False) -> str:
    """Manual one-shot: run formatters on ``path``, render ok/fail + duration.

    verbose=True: show the formatter's full error message (untruncated) and
    a ``[verbose]`` marker on the row so callers can distinguish the mode.
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
    """
    import subprocess
    try:
        r = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            msg = (r.stderr.strip() or "git diff failed")
            return f"ERROR: {msg}\n"
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"ERROR: git unavailable: {e}\n"

    staged = [p for p in r.stdout.splitlines() if p and os.path.isfile(p)]
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
    """
    import subprocess
    try:
        r = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            msg = (r.stderr.strip() or "git diff failed")
            return f"ERROR: {msg}\n"
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"ERROR: git unavailable: {e}\n"

    staged = [p for p in r.stdout.splitlines() if p and os.path.isfile(p)]
    if not staged:
        return "no staged files\n"

    parts = []
    for fpath in staged:
        parts.append(f"format_staged: {fpath}")
        block = op_format(fpath, tool_filter, verbose=verbose)
        for line in block.splitlines():
            parts.append(f"  {line}")
    return "\n".join(parts) + "\n"


def _detect_payload_format(raw: str) -> str:
    """Return 'json' if first non-whitespace char is { or [, else 'toml'."""
    for c in raw:
        if c in " \t\r\n":
            continue
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


def _mini_toml_loads(raw: str) -> Dict[str, Any]:
    """Minimal TOML parser for @file payloads.

    Supports: bare keys, integers, true/false, single-line strings
    ("..." with escapes, '...' literal), multi-line strings (\"\"\"...\"\"\"
    with escapes, '''...''' literal), # comments. No arrays, no tables,
    no dotted keys, no dates — only what payloads need.

    Used as fallback when stdlib `tomllib` is unavailable (Python <3.11).
    """
    result: Dict[str, Any] = {}
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
        if raw[i:i + 3] == '"""':
            i += 3
            end = raw.find('"""', i)
            if end < 0:
                raise ValueError(f"unterminated \"\"\" for '{key}'")
            val = _toml_basic_unescape(raw[i:end])
            if val.startswith("\r\n"):
                val = val[2:]
            elif val.startswith("\n"):
                val = val[1:]
            i = end + 3
        elif raw[i:i + 3] == "'''":
            i += 3
            end = raw.find("'''", i)
            if end < 0:
                raise ValueError(f"unterminated ''' for '{key}'")
            val = raw[i:end]
            if val.startswith("\r\n"):
                val = val[2:]
            elif val.startswith("\n"):
                val = val[1:]
            i = end + 3
        elif raw[i] == '"':
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
            val = "".join(buf)
            i += 1
        elif raw[i] == "'":
            i += 1
            end = raw.find("'", i)
            if end < 0 or raw.find("\n", i, end) >= 0:
                raise ValueError(f"unterminated literal for '{key}'")
            val = raw[i:end]
            i = end + 1
        elif raw[i:i + 4] == "true" and (i + 4 == n or not raw[i + 4].isalnum()):
            val = True
            i += 4
        elif raw[i:i + 5] == "false" and (i + 5 == n or not raw[i + 5].isalnum()):
            val = False
            i += 5
        elif raw[i] == "-" or raw[i].isdigit():
            ns = i
            if raw[i] == "-":
                i += 1
            while i < n and raw[i].isdigit():
                i += 1
            try:
                val = int(raw[ns:i])
            except ValueError as _e:
                raise ValueError(f"bad number for '{key}': {_e}") from _e
        else:
            raise ValueError(f"unknown value type for '{key}' at offset {i}")
        result[key] = val
        while i < n and raw[i] in " \t":
            i += 1
        if i < n and raw[i] == "#":
            while i < n and raw[i] != "\n":
                i += 1
    return result


def _load_at_file(ref: str) -> Any:
    """Load JSON or TOML from an @file reference.

    Accepts:
      @path/to/file.json   — read from filesystem
      @-                   — read from stdin

    Format detected from first non-whitespace char: { or [ → JSON, else TOML.
    TOML lets you embed code blocks with backslashes/quotes/newlines without
    JSON's double-escaping. Use '''triple-single-quote''' for literal content.

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
        raise ValueError(f"@file TOML parse error ({source}): {_e}") from _e


# Dynamic @file field registry — built lazily from op syntax strings.
# Maps op name → ordered list of JSON field names (positional parts[1..N]).
# Populated on first dispatch call via _build_at_file_registry().
_AT_FILE_REGISTRY: Dict[str, List[str]] = {}
_AT_FILE_REGISTRY_BUILT: bool = False


def _fields_from_syntax(syntax: str) -> List[str]:
    """Derive positional field names from a syntax string using ':::' separator.

    Takes the first alternative (before ' | '), splits on ':::', drops the
    first token (op name), and lowercases the rest.

    Returns [] if the syntax has no ':::' (read-only op — no @file route).

    Examples:
      'edit:::OLD:::NEW:::PATH'    → ['old', 'new', 'path']
      'paste:::PATH:::CONTENT'     → ['path', 'content']
      'read:PATH'                  → []
    """
    first_alt = re.split(r"\s*\|\s*", syntax)[0]
    if ":::" not in first_alt:
        return []
    tokens = first_alt.split(":::")
    return [t.strip().lower() for t in tokens[1:]]


_AT_FILE_BUILTIN_DEFAULTS: Dict[str, List[str]] = {
    "edit":          ["old", "new", "path"],
    "replace":       ["old", "new", "path"],
    "replace_dry":   ["old", "new", "path"],
    "replace_lines": ["path", "start", "end", "content"],
    "paste":         ["path", "content"],
    "vim":           ["path", "script"],
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
    registry: Dict[str, List[str]] = dict(_AT_FILE_BUILTIN_DEFAULTS)
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


def _at_file_fields(op: str) -> List[str]:
    """Return the field list for *op*, or [] if the op has no @file route."""
    _build_at_file_registry()
    return _AT_FILE_REGISTRY.get(op, [])


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
    if not isinstance(payload, dict):
        raise ValueError(
            f"@file payload for op '{op}' must be a JSON object, "
            f"got {type(payload).__name__}"
        )
    fields = _at_file_fields(op)
    if not fields:
        raise ValueError(f"@file route not supported for op '{op}'")
    # Case-insensitive key lookup — normalise payload keys once.
    lower_payload = {k.lower(): v for k, v in payload.items()}
    parts = [op]
    for field in fields:
        if field not in lower_payload:
            raise ValueError(
                f"@file payload for op '{op}' missing required field '{field}'"
            )
        parts.append(str(lower_payload[field]))
    replace_all = bool(lower_payload.get("replace_all", False))
    return parts, replace_all


def dispatch(arg: str) -> str:
    """Parse 'op:arg1:arg2:...' and route to the matching op function.

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
    """
    # Strip :::no-exclude before splitting so it doesn't interfere with arg parsing
    no_exclude = arg.endswith(_NO_EXCLUDE_SUFFIX)
    if no_exclude:
        arg = arg[: -len(_NO_EXCLUDE_SUFFIX)]

    header = f"--- {arg}{_NO_EXCLUDE_SUFFIX if no_exclude else ''} ---\n"

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
    _at_file_replace_all: bool = False
    _at_file_used: bool = False
    if (
        len(parts) >= 2
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

    try:
        if op == "read":
            path = parts[1] if len(parts) > 1 else ""
            offset = 0
            limit = 0
            force_full = False
            if len(parts) > 2 and parts[2]:
                if parts[2] in ("full", "raw"):
                    force_full = True
                else:
                    offset = int(parts[2])
            if len(parts) > 3 and parts[3]:
                if parts[3] in ("full", "raw"):
                    force_full = True
                else:
                    limit = int(parts[3])
            grep_filter = ""
            if len(parts) > 4 and parts[4].startswith("grep="):
                grep_filter = parts[4][5:]
            body = op_read(path, offset, limit, grep_filter, force_full)
        elif op == "grep":
            pattern, path, limit, context, count_only = _parse_grep_args(parts)
            body = op_grep(pattern, path, limit, context, count_only,
                           no_exclude=no_exclude)
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
                            # Snapshot mode: reorder replace_lines ops on the
                            # same file bottom-up so caller's line numbers
                            # refer to the original file state, not the file
                            # as mutated by earlier ops in the same batch.
                            batch_ops, _snap_err = _reorder_batch_for_snapshot(batch_ops)
                            if _snap_err:
                                body = f"ERROR: {_snap_err}\n"
                                batch_ops = []
                            results: List[str] = []
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
                                    # Reconstruct a triple-colon arg string so dispatch
                                    # parses it correctly (handles colons in content).
                                    _sub_arg = ":::".join(_sub_parts)
                                else:
                                    # Read-only op: build plain colon arg from known fields.
                                    # For unknown ops, pass what we have and let dispatch error.
                                    _fields = [str(_item[k]) for k in sorted(_item) if k != "op"]
                                    _sub_arg = ":".join([_sub_op] + _fields) if _fields else _sub_op
                                _sub_result = dispatch(_sub_arg)
                                results.append(_sub_result)
                                if not continue_on_error and _sub_result.split("\n")[1:2] and (
                                    _sub_result.split("\n")[1].startswith("ERROR")
                                ):
                                    break
                            if not _snap_err:
                                body = "".join(results)
        elif op == "validate":
            # verbose flag: literal "verbose" token anywhere after op name.
            # Forms: validate:PATH:verbose  or  validate:PATH:tool1,tool2:verbose
            v_verbose = "verbose" in parts[1:]
            v_parts = [p for p in parts[1:] if p != "verbose"]
            v_path = v_parts[0] if len(v_parts) > 0 else ""
            v_tools = [t for t in (v_parts[1].split(",") if len(v_parts) > 1 and v_parts[1] else []) if t]
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
            body = op_resolve(rs_symbol)
        elif op == "workspace":
            ws_path = parts[1] if len(parts) > 1 else ""
            body = op_workspace(ws_path)
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

    return header + body


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
        with open(LOG_FILE, "a") as f:
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


class MCPServer:
    """Manages a single MCP server subprocess with JSON-RPC 2.0 over stdio."""

    def __init__(
        self,
        name: str,
        cmd: str,
        env: Optional[Dict[str, str]] = None,
        timeout: int = 30,
    ) -> None:
        self.name = name
        self.cmd = cmd
        self.env = env
        self.timeout = timeout
        self._proc: Optional[subprocess.Popen] = None  # type: ignore[type-arg]
        self._lock = threading.Lock()
        self._id_counter = 0
        self._id_lock = threading.Lock()
        self._dead = False  # Set on timeout; cleared only by constructing a new instance

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def spawn(self) -> None:
        """Start the server process. Idempotent — does nothing if already alive."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            merged_env = os.environ.copy()
            if self.env:
                merged_env.update(self.env)
            self._proc = subprocess.Popen(
                shlex.split(self.cmd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=merged_env,
            )

    def is_alive(self) -> bool:
        """Return True if the subprocess is running."""
        return self._proc is not None and self._proc.poll() is None

    def shutdown(self) -> None:
        """Gracefully shut down the server; SIGTERM if it hangs."""
        with self._lock:
            if self._proc is None:
                return
            proc = self._proc
            self._proc = None
            dead = self._dead
        if proc.poll() is not None:
            return
        # Dead server (marked dead after timeout) — skip graceful path. The
        # child is most likely stuck in a sleep/blocking call that won't
        # respond to stdin close. Go straight to SIGTERM.
        if dead:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            return
        # Healthy server — try graceful shutdown.
        # Send JSON-RPC shutdown notification before closing stdin so the
        # server can clean up state. Wrapped in try because the pipe may
        # already be dead.
        try:
            if proc.stdin:
                body = json.dumps({"jsonrpc": "2.0", "method": "shutdown"}).encode("utf-8")
                header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
                proc.stdin.write(header + body)
                proc.stdin.flush()
        except (OSError, BrokenPipeError):
            pass
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ------------------------------------------------------------------
    # JSON-RPC 2.0 transport
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        with self._id_lock:
            self._id_counter += 1
            return self._id_counter

    def _send(self, payload: dict) -> None:
        """Encode and write one JSON-RPC message with Content-Length framing."""
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError(f"MCP server '{self.name}' is not running")
        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        self._proc.stdin.write(header + body)
        self._proc.stdin.flush()

    def _recv(self) -> dict:
        """Read one Content-Length-framed JSON-RPC message from stdout."""
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError(f"MCP server '{self.name}' is not running")
        stdout = self._proc.stdout
        # Read headers until blank line
        content_length = 0
        while True:
            line = stdout.readline()
            if not line:
                # TODO(sub-PR-2): crash recovery + backoff per spec §5
                raise EOFError(f"MCP server '{self.name}' closed stdout")
            line = line.decode("utf-8").rstrip("\r\n")
            if line == "":
                break
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())
        if content_length == 0:
            raise ValueError(f"MCP server '{self.name}': missing Content-Length")
        raw = b""
        remaining = content_length
        while remaining > 0:
            chunk = stdout.read(remaining)
            if not chunk:
                raise EOFError(f"MCP server '{self.name}' closed stdout mid-body")
            raw += chunk
            remaining -= len(chunk)
        return json.loads(raw.decode("utf-8"))

    def _call(self, method: str, params: Optional[dict] = None) -> Any:
        """Send a JSON-RPC request and return the result, with timeout.

        Raises MCPTimeout on deadline, MCPServerError on RPC error object.
        """
        req_id = self._next_id()
        payload: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params

        result_holder: List[Any] = []
        error_holder: List[BaseException] = []

        def _worker() -> None:
            try:
                self._send(payload)
                msg = self._recv()
                result_holder.append(msg)
            except BaseException as exc:
                error_holder.append(exc)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout=self.timeout)
        if t.is_alive():
            # Mark dead and close stdout so the blocked worker thread unblocks.
            # Subsequent calls will fail fast via the _dead guard below.
            self._dead = True
            try:
                if self._proc and self._proc.stdout:
                    self._proc.stdout.close()
            except OSError:
                pass
            raise MCPTimeout(
                f"MCP server '{self.name}' timed out after {self.timeout}s "
                f"(method={method})"
            )
        if self._dead:
            raise MCPServerError(f"MCP server '{self.name}' marked dead after timeout")
        if error_holder:
            raise error_holder[0]
        msg = result_holder[0]
        if "error" in msg:
            err = msg["error"]
            raise MCPServerError(
                err.get("message", "unknown error"),
                code=err.get("code", 0),
                data=err.get("data"),
            )
        return msg.get("result")

    # ------------------------------------------------------------------
    # MCP protocol methods
    # ------------------------------------------------------------------

    def initialize(self) -> dict:
        """Send the MCP initialize handshake and return the server's capabilities."""
        result = self._call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "supertool", "version": VERSION},
            },
        )
        # Send initialized notification (no response expected)
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        try:
            self._send(notif)
        except OSError:
            pass
        return result or {}

    def list_tools(self) -> List[dict]:
        """Return the list of tools exposed by this MCP server."""
        result = self._call("tools/list")
        if isinstance(result, dict):
            return result.get("tools", [])
        return []

    def call_tool(self, name: str, args: dict) -> dict:
        """Invoke a tool by name and return the result dict."""
        result = self._call("tools/call", {"name": name, "arguments": args})
        if result is None:
            return {}
        return result


# ---------------------------------------------------------------------------
# Module-level server registry + lifecycle
# ---------------------------------------------------------------------------

_MCP_SERVERS: Dict[str, MCPServer] = {}
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


def _mcp_ensure_server(name: str) -> Optional[MCPServer]:
    """Get-or-spawn MCPServer for a configured name. None on any failure."""
    server = _mcp_get_server(name)
    if server is not None:
        return server
    spec = _mcp_specs.get(name)
    if spec is None:
        return None
    try:
        server = MCPServer(
            name=name, cmd=spec["cmd"],
            env=spec.get("env"), timeout=int(spec.get("timeout", 30)),
        )
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
    """Normalize MCP response into a single file path string."""
    from urllib.parse import urlparse, unquote

    if not isinstance(result, dict):
        return None

    def _normalize_file_url_or_path(s: str) -> str:
        if s.startswith("file://"):
            parsed = urlparse(s)
            return unquote(parsed.path)
        return s

    # Shape 1: {"content": [{"type": "text", "text": "..."}]}
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                if text:
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

def _mcp_get_server(name: str) -> Optional[MCPServer]:
    """Return a live MCPServer for *name* from the registry, or None.

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


def _mcp_register(name: str, server: MCPServer) -> None:
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


def main(argv: List[str]) -> int:
    if not argv:
        sys.stderr.write(
            "Usage: supertool op:args [op:args ...]\n"
            "       supertool 'read:file.py' 'grep:foo:src/:20' 'glob:**/*.md'\n"
        )
        return 1

    # Normal batched-ops mode
    total_out_bytes = 0
    any_failure = False

    # Optional parallel execution — opt-in, only when every op is read-only.
    # Custom ops are excluded (could mutate via shell). Mixed batches stay
    # sequential to keep reasoning simple. Output order = input order.
    bodies: List[str]
    workers = _parallel_workers()
    if (
        workers >= 2
        and len(argv) > 1
        and all(_is_parallel_safe(a) for a in argv)
    ):
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

    for body in bodies:
        sys.stdout.write(body)
        total_out_bytes += len(body.encode("utf-8"))
        if _body_indicates_failure(body):
            any_failure = True
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
