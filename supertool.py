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

import json
import difflib
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "0.10.0"

MAX_READ_LINES = 300
MAX_READ_BYTES = 20000  # ~20KB cap — prevents Claude Code "Output too large"
MAX_GREP_RESULTS = 10
MAX_GLOB_RESULTS = 50
LOG_FILE = os.path.join(tempfile.gettempdir(), "supertool-calls.log")
GREP_FILE_INCLUDES = ("*.php", "*.xml", "*.py", "*.js", "*.ts", "*.md")
_GREP_EXTENSIONS_EFFECTIVE: Tuple[str, ...] | None = None

# Default exclude-paths applied to all traversal ops (glob, grep, tree, map).
# These are pruned at the directory-walk boundary — the dirs are never opened.
# Match is prefix-relative-to-cwd; trailing slash is normalised in _get_exclude_paths.
_DEFAULT_EXCLUDE_PATHS: Tuple[str, ...] = (
    ".git/", "node_modules/", ".svn/", ".hg/", ".idea/", ".vscode/",
    "__pycache__/", ".venv/", "venv/", "dist/", "build/",
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

    After loading, merges any preset ops declared in "presets" key.
    """
    global _CONFIG, _CONFIG_CHECKED
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

# Enforcement — pre-tool-block hook reads this state file (absent = permissive)
ENFORCE_STATE_FILE = os.path.expanduser("~/.claude/supertool-enforced")

# Tools blocked when enforcement is active
BLOCKED_TOOLS = {"Grep", "Glob", "LS"}
BLOCKED_BASH_COMMANDS = {"cat", "find", "grep", "ls", "sed", "awk", "tail", "head"}

# Built-in op names — custom ops/aliases with these names are ignored
_BUILTIN_OPS = {"read", "grep", "grep_around", "glob", "ls", "tail", "head", "wc", "check", "around", "map", "diff", "stat", "around_line", "tree", "replace", "replace_dry", "edit", "replace_lines", "vi"}

# Read-only built-in ops — safe to run in parallel across a batch.
# Excludes mutating ops (replace, edit, replace_lines) and custom ops
# (could shell out to anything). `between` is included — pure file read.
_PARALLEL_SAFE_OPS = {
    "read", "grep", "glob", "ls", "head", "tail", "wc", "stat",
    "map", "tree", "around", "around_line", "between", "diff", "blame",
    "version",
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
    except Exception:
        return []

    try:
        with open(path, "rb") as f:
            source = f.read()
        tree = parser.parse(source)
    except Exception:
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
    except Exception:
        return None

    try:
        with open(path, "rb") as f:
            source = f.read()
        tree = parser.parse(source)
    except Exception:
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
    tier = "tree-sitter" if use_ts else ("ctags" if use_ctags else "regex")

    out = [f"({len(files)} files, tier: {tier})\n"]

    for fpath in files:
        ext = os.path.splitext(fpath)[1].lower()
        line_count = _count_lines(fpath)

        symbols_found = False

        if use_ts:
            lang_name = _TS_LANG_MAP.get(ext)
            if lang_name:
                symbols = _ts_extract(fpath, lang_name)
                if symbols:
                    out.append(_format_map_symbols(symbols, fpath, line_count))
                    symbols_found = True

        if not symbols_found and use_ctags:
            symbols_ct = _ctags_extract(fpath)
            if symbols_ct:
                out.append(_format_ctags_symbols(
                    symbols_ct, fpath, line_count))
                symbols_found = True

        if not symbols_found:
            symbols_rx = _regex_extract(fpath)
            if symbols_rx:
                out.append(_format_map_symbols(
                    symbols_rx, fpath, line_count))
                symbols_found = True

        if not symbols_found:
            # File exists but no symbols extracted — show it as empty
            out.append(f"{fpath} ({line_count} lines)\n  (no symbols)\n")

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
    out = [f"{verb} in {path} (Δ {added - removed:+d})\n"]

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
    cursor (int), marks (dict[str,int]), last_edit (int|None).
    Backward-compat: if the file is a bare int, treat as legacy cursor-only.
    """
    default = {"cursor": 0, "marks": {}, "last_edit": None}
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
        import json as _json
        data = _json.loads(raw)
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
            return {"cursor": cur, "marks": marks, "last_edit": le_val}
    except (ValueError, TypeError):
        pass
    # Legacy: bare int
    try:
        return {"cursor": max(0, min(content_len, int(raw))), "marks": {}, "last_edit": None}
    except ValueError:
        return default


def _vim_save_state(file_path: str, cursor: int, marks: dict, last_edit) -> None:
    """Persist vim state for `file_path` so the next vim call resumes here."""
    if os.environ.get("SUPERTOOL_VIM_NO_PERSIST"):
        return
    state_path = _vim_cursor_state_path(file_path)
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        import json as _json
        payload = _json.dumps({
            "cursor": int(cursor),
            "marks": {k: int(v) for k, v in (marks or {}).items()},
            "last_edit": int(last_edit) if last_edit is not None else None,
        })
        with open(state_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
    except OSError:
        pass


def _vim_load_cursor(file_path: str, content_len: int) -> int:
    """Backcompat shim: load just the cursor."""
    return _vim_load_state(file_path, content_len)["cursor"]


def _vim_save_cursor(file_path: str, cursor: int) -> None:
    """Backcompat shim: save cursor only, preserving existing marks/last_edit."""
    if os.environ.get("SUPERTOOL_VIM_NO_PERSIST"):
        return
    # Preserve existing marks/last_edit
    try:
        existing = _vim_load_state(file_path, 10**9)
    except Exception:
        existing = {"marks": {}, "last_edit": None}
    _vim_save_state(file_path, cursor, existing.get("marks", {}), existing.get("last_edit"))


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
    i = 0
    n = len(normalized)

    def _verb_token(start: int) -> tuple:
        """Identify the verb starting at `start`. Returns
        (verb_end, enters_text_mode) where verb_end is the index just
        past the verb's fixed structure. enters_text_mode=True means
        the verb consumes greedy TEXT until ESC/EOS (insert verbs, ex,
        search, change-family).
        """
        if start >= n:
            return (start, False)
        c = normalized[start]
        # Insert verbs — greedy text
        if c in "iaAIoO":
            return (start + 1, True)
        # Insert-mode-entry shortcuts — greedy text (delete + insert in one
        # verb). s = subst char(s); S = subst line(s); C = change to EOL.
        # gi (insert at last edit pos) intentionally NOT implemented —
        # requires cross-call last-insert-position tracking.
        if c in "sSC":
            return (start + 1, True)
        # Search / ex — greedy
        if c in "/?:":
            return (start + 1, True)
        # vim alias `%s/PAT/REPL/flags` — bare percent (no colon) for
        # whole-buffer substitute. Treat `%s` as a 2-char greedy verb.
        if c == "%" and start + 1 < n and normalized[start + 1] == "s":
            return (start + 2, True)
        # Change-family — greedy (deletes then enters insert).
        # Two-char forms: cc, cw, c$, c0 (greedy after the verb).
        if c == "c" and start + 1 < n and normalized[start + 1] in ("c", "w", "$", "0"):
            return (start + 2, True)
        # ciw / ci<delim> — three-char form, greedy after.
        if c == "c" and start + 1 < n and normalized[start + 1] == "i":
            if start + 2 < n and normalized[start + 2] in ('w', '"', "'", "(", "[", "{"):
                return (start + 3, True)
        # Full text-object family: <op>i<X> / <op>a<X>
        # ops: c d y (single char) and g~ gu gU (two char)
        # X (text-object kind): w W s p " ' ` ( ) [ ] { } < > b B t
        # c-family enters text mode (greedy); d/y/g~/gu/gU do not.
        _TO_KINDS = set('wWsp"\'`()[]{}<>bBt')
        # Single-char op + i/a + kind
        if c in "cdy" and start + 2 < n and normalized[start + 1] in "ia" and normalized[start + 2] in _TO_KINDS:
            return (start + 3, c == "c")
        # Two-char op (g~, gu, gU) + i/a + kind
        if c == "g" and start + 3 < n and normalized[start + 1] in ("~", "u", "U") \
                and normalized[start + 2] in "ia" and normalized[start + 3] in _TO_KINDS:
            return (start + 4, False)
        # cf<c> / cF<c> / ct<c> / cT<c> — three-char form, greedy after.
        if c == "c" and start + 1 < n and normalized[start + 1] in "fFtT":
            return (min(start + 3, n), True)
        # Replace: r<c> — single-char arg, no text mode.
        if c == "r":
            return (min(start + 2, n), False)
        # Char-find: f<c>, F<c>, t<c>, T<c>.
        if c in "fFtT":
            return (min(start + 2, n), False)
        # Delete with char arg: df<c>/dF<c>/dt<c>/dT<c>, yf<c>...
        if c in "dy" and start + 1 < n and normalized[start + 1] in "fFtT":
            return (min(start + 3, n), False)
        # Operator-motion: d/PAT\e, d?PAT\e, y/PAT\e, y?PAT\e — greedy
        if c in "dy" and start + 1 < n and normalized[start + 1] in ("/", "?"):
            return (start + 2, True)
        # Operator-motion: dgg, ygg, cgg — three-char no-arg
        if c in "dyc" and start + 2 < n and normalized[start + 1] == "g" and normalized[start + 2] == "g":
            return (start + 3, c == "c")  # cgg enters text mode
        # Operator + ge/gE/g_ — three-char no-arg (operator-motion)
        if c in "dyc" and start + 2 < n and normalized[start + 1] == "g" and normalized[start + 2] in ("e", "E", "_"):
            return (start + 3, c == "c")
        # Two-char no-arg verbs in d/y family: dd dw d$ d0 dG d^ dh dj dk dl,
        # yy yw y$ yG y^ yh yj yk yl, plus new motions:
        # d{ d} d( d) d% d+ d- d_ dW dB dE d; d, (and y/c equivalents).
        if c in "dy" and start + 1 < n and normalized[start + 1] in (
            "d", "c", "w", "y", "$", "0", "G", "^", "h", "j", "k", "l",
            "{", "}", "(", ")", "%", "+", "-", "_", "W", "B", "E", ";", ",",
        ):
            return (start + 2, False)
        # c + new motions (text mode for c). cc cw c$ c0 already handled above.
        if c == "c" and start + 1 < n and normalized[start + 1] in (
            "{", "}", "(", ")", "%", "+", "-", "_", "W", "B", "E", ";", ",", "^",
        ):
            return (start + 2, True)
        # gg (go to BOF), ge, gE, g_
        if c == "g" and start + 1 < n and normalized[start + 1] in ("g", "e", "E", "_"):
            return (start + 2, False)
        # Linewise case verbs: g~~, guu, gUU (3-char no-arg)
        if c == "g" and start + 2 < n and (
            (normalized[start + 1] == "~" and normalized[start + 2] == "~")
            or (normalized[start + 1] == "u" and normalized[start + 2] == "u")
            or (normalized[start + 1] == "U" and normalized[start + 2] == "U")
        ):
            return (start + 3, False)
        # Operator-motion case verbs: g~<motion>, gu<motion>, gU<motion>.
        # Treat the trailing motion char like the d/y operator-motion family.
        if c == "g" and start + 2 < n and normalized[start + 1] in ("~", "u", "U"):
            return (start + 3, False)
        # Tilde toggle-case: single-char no-arg verb
        if c == "~":
            return (start + 1, False)
        # Ctrl-A increment / Ctrl-X decrement: single-char no-arg verbs
        if c in ("\x01", "\x18"):
            return (start + 1, False)
        # gJ — join without space (two-char no-arg)
        if c == "g" and start + 1 < n and normalized[start + 1] == "J":
            return (start + 2, False)
        # gi — insert at last edit position (greedy text after)
        if c == "g" and start + 1 < n and normalized[start + 1] == "i":
            return (start + 2, True)
        # R — overwrite mode (greedy text until ESC)
        if c == "R":
            return (start + 1, True)
        # m{a-zA-Z} — set mark (two-char no-arg)
        if c == "m" and start + 1 < n and (
            ("a" <= normalized[start + 1] <= "z")
            or ("A" <= normalized[start + 1] <= "Z")
        ):
            return (start + 2, False)
        # `{a-zA-Z} or `` — jump to mark exact / back to last jump
        if c == "`" and start + 1 < n and (
            ("a" <= normalized[start + 1] <= "z")
            or ("A" <= normalized[start + 1] <= "Z")
            or normalized[start + 1] == "`"
        ):
            return (start + 2, False)
        # '{a-zA-Z} or '' — jump to mark line / back to last jump
        if c == "'" and start + 1 < n and (
            ("a" <= normalized[start + 1] <= "z")
            or ("A" <= normalized[start + 1] <= "Z")
            or normalized[start + 1] == "'"
        ):
            return (start + 2, False)
        # >> << — indent/dedent current line (two-char no-arg)
        if c in "><" and start + 1 < n and normalized[start + 1] == c:
            return (start + 2, False)
        # > / < + motion: text-object (iw/aw/i{/a{ etc) or simple motion char
        if c in "><" and start + 1 < n:
            nxt = normalized[start + 1]
            # text-object: >iw, >aw, >ip, <ap, etc.
            _TO_KINDS2 = set('wWsp"\'`()[]{}<>bBt')
            if nxt in "ia" and start + 2 < n and normalized[start + 2] in _TO_KINDS2:
                return (start + 3, False)
            # gg / ge / gE / g_ (3-char)
            if nxt == "g" and start + 2 < n and normalized[start + 2] == "g":
                return (start + 3, False)
            # simple motion targets
            if nxt in ("j", "k", "h", "l", "G", "{", "}", "(", ")",
                       "%", "+", "-", "_", "w", "b", "e", "W", "B", "E",
                       "$", "0", "^"):
                return (start + 2, False)
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
        # Single-char no-arg verbs (G, h, j, k, l, x, D, J, n, N, p, P,
        # $, 0, ^, w, b, e, W, B, E, %, Y, *, #, etc.)
        return (start + 1, False)

    while i < n:
        # Skip whitespace AND stray ESC between actions. (ESC is a mode
        # exit; in normal mode it's a no-op. Vim macros use it for
        # readability and to "reset" defensively.)
        while i < n and normalized[i] in (" \t\n\r" + ESC):
            i += 1
        if i >= n:
            break
        action_start = i
        # Parse count: leading digits, but not if `0` alone (BOL verb).
        verb_pos = i
        if normalized[i].isdigit() and normalized[i] != "0":
            while verb_pos < n and normalized[verb_pos].isdigit():
                verb_pos += 1
        if verb_pos >= n:
            # trailing digits with no verb — emit as-is, _parse will error
            raw_actions.append(normalized[action_start:])
            break
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
            # Single-char ex verbs LAST (so longer prefixes win first).
            if j < len(rest) and rest[j] == "s":
                range_spec = rest[1:j]
                body = rest[j + 1:]
                return (count, ":s", f"\x1d{range_spec}\x1d{body}")
            if j < len(rest) and rest[j] == "d":
                range_spec = rest[1:j]
                trailing = rest[j + 1:]
                return (count, ":d", f"\x1d{range_spec}\x1d{trailing}")
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
        # >> and << — indent/dedent current line
        if len(rest) >= 2 and rest[:2] in (">>", "<<"):
            return (count, rest[:2], rest[2:])
        # > / < + motion
        if len(rest) >= 2 and rest[0] in "><":
            op = rest[0]
            # text-object form: >iw, <ap, etc.
            _to = set('wWsp"\'`()[]{}<>bBt')
            if len(rest) >= 3 and rest[1] in "ia" and rest[2] in _to:
                return (count, op + rest[1] + rest[2], rest[3:])
            # gg / ge / gE / g_
            if len(rest) >= 3 and rest[1] == "g" and rest[2] in ("g", "e", "E", "_"):
                return (count, op + rest[1:3], rest[3:])
            # simple motion target
            if rest[1] in ("j", "k", "h", "l", "G", "{", "}", "(", ")",
                           "%", "+", "-", "_", "w", "b", "e", "W", "B", "E",
                           "$", "0", "^"):
                return (count, op + rest[1], rest[2:])
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
        ):
            return (count, c, rest[1:])
        return (count, "", rest)  # unknown

    _state = _vim_load_state(path, len(content))
    cursor = _state["cursor"]
    marks: dict = dict(_state["marks"])  # {char: offset}
    last_edit = _state["last_edit"]      # int|None
    prev_cursor = cursor                 # for `` and '' jump-back
    log: List[str] = []
    last_search: Optional[tuple] = None  # (pattern, direction "/"|"?")
    last_find: Optional[tuple] = None  # (verb in fFtT, target char) for ; ,
    register: str = ""  # anonymous yank/paste register
    register_linewise: bool = False  # True if last yank was line-wise (yy)
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
    _V_MOTION_LINE = re.compile(r"^V(\d*)([jk])(cc|dd|yy)(.*)$", re.DOTALL)
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
            return f"{n + 1}{m.group(3)}{m.group(4)}"
        return act

    # cc-typo: Kevin types `cciw<TEXT>` thinking it means `ciw<TEXT>` (change
    # inner word). Real vim parses as cc + greedy text "iwTEXT" (line replace
    # with literal "iwTEXT"). Detect `cc<ia><kind>` prefix and drop one c.
    _CC_TYPO = re.compile(r"^cc([ia])([wWsp\"'`()\[\]{}<>bBt])(.*)$", re.DOTALL)

    def _rewrite_cc_typo(act: str) -> str:
        m = _CC_TYPO.match(act)
        if m is None:
            return act
        return f"c{m.group(1)}{m.group(2)}{m.group(3)}"

    raw_actions = [_rewrite_cc_typo(_rewrite_v_alias(a)) for a in raw_actions]
    for i, action in enumerate(raw_actions, 1):
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
            text = _decode_escapes(arg) * count
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
            preview = text if len(text) <= 30 else text[:27] + "..."
            log.append(f"  {i}. {verb}{preview!r} (len={len(text)})")

        # --- deletes ---
        elif verb == "x":
            end = min(len(content), cursor + count)
            content = content[:cursor] + content[end:]
            log.append(f"  {i}. {count}x ({end - cursor} chars)")
        elif verb == "dd":
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
            log.append(f"  {i}. {count}dd (cursor={cursor})")
        elif verb == "D":
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
            end = min(len(content), cursor + count)
            text = _decode_escapes(arg)
            content = content[:cursor] + text + content[end:]
            cursor = cursor + len(text)
            preview = text if len(text) <= 30 else text[:27] + "..."
            log.append(f"  {i}. {count}s{preview!r} (cursor={cursor})")
        elif verb == "S":
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
            eol = _line_end(content, cursor)
            text = _decode_escapes(arg)
            content = content[:cursor] + text + content[eol:]
            cursor = cursor + len(text)
            preview = text if len(text) <= 30 else text[:27] + "..."
            log.append(f"  {i}. C{preview!r} (cursor={cursor})")

        # --- change inner word ---
        elif verb == "ciw":
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
            preview = text if len(text) <= 30 else text[:27] + "..."
            log.append(f"  {i}. ciw{preview!r} (cursor={cursor})")

        # --- change inside delimiter: ci" ci' ci( ci[ ci{ ---
        elif verb in ('ci"', "ci'", "ci(", "ci[", "ci{"):
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
            preview = text if len(text) <= 30 else text[:27] + "..."
            log.append(f"  {i}. {count}cc{preview!r} (cursor={cursor})")

        # --- join lines ---
        elif verb == "J":
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

        # --- ex read file: :r FILE  (or `:r -` to read stdin, `:r !CMD` to shell) ---
        elif verb == ":r":
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

        # --- ex delete: :%d, :Nd, :N,Md, :.d, :$d, :.,$d, :g/PAT/d, :v/PAT/d ---
        elif verb == ":d":
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

            def _resolve_d(addr: str) -> int:
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
                segment = body_lines[line_a - 1:line_b]
                segment.reverse()
                new_body = body_lines[:line_a - 1] + segment + body_lines[line_b:]
                content = "\n".join(new_body) + ("\n" if has_trailing_nl else "")
                cursor = min(cursor, len(content))
                log.append(f"  {i}. :{range_spec}reverse ({len(segment)} lines)")

            elif verb in (":move", ":copy"):
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
            eol = _line_end(content, cursor)
            text = _decode_escapes(arg)
            content = content[:cursor] + text + content[eol:]
            cursor += len(text)
            log.append(f"  {i}. c${text!r}")
        elif verb == "c0":
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
            while we < len(content) and content[we] in (" ", "\t"):
                we += 1
            register = content[cursor:we]
            register_linewise = False
            content = content[:cursor] + content[we:]
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
            end_pos = min(len(content), cursor + count)
            seg = content[cursor:end_pos]
            content = content[:cursor] + seg.swapcase() + content[end_pos:]
            cursor = end_pos
            log.append(f"  {i}. {count}~ ({len(seg)} chars toggled)")

        # --- linewise case verbs: g~~ guu gUU (N lines) ---
        elif verb in ("g~~", "guu", "gUU"):
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
            log.append(f"  {i}. {'C-a' if verb == '\x01' else 'C-x'} {num_str} → {new_str}")

        # --- paste ---
        elif verb == "p":
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
        elif verb == "P":
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

        # --- indent operators: >> << and >{motion} <{motion} ---
        elif verb in (">>", "<<") or (
            len(verb) >= 2 and verb[0] in "><" and verb != ">>" and verb != "<<"
        ):
            op = verb[0]
            # Determine the [line_a, line_b] line range (1-indexed inclusive)
            cur_line, _ = _offset_to_line_col(content, cursor)
            total_lines = content.count("\n") + 1
            if verb in (">>", "<<"):
                line_a = cur_line
                line_b = min(total_lines, cur_line + count - 1)
            else:
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
                        nl = content.find("\n", cursor)
                        target = (nl + 1) if nl != -1 else cursor
                    elif motion == "k":
                        bol = _line_start(content, cursor)
                        target = _line_start(content, bol - 1) if bol > 0 else cursor
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
                    real_lines[ln_idx] = shift + real_lines[ln_idx]
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

        # --- gi — insert at last edit position ---
        elif verb == "gi":
            text = _decode_escapes(arg) * count
            pos = last_edit if last_edit is not None else cursor
            pos = min(pos, len(content))
            content = content[:pos] + text + content[pos:]
            cursor = pos + len(text)
            last_edit = cursor
            preview = text if len(text) <= 30 else text[:27] + "..."
            log.append(f"  {i}. gi{preview!r} (cursor={cursor})")

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

    try:
        _atomic_write(path, content)
    except OSError as e:
        return f"ERROR: failed to write {path}: {e}\n"

    _vim_save_state(
        path,
        min(cursor, len(content)),
        {k: min(v, len(content)) for k, v in marks.items()},
        min(last_edit, len(content)) if last_edit is not None else None,
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
    out.append(_vim_render_lint(path))
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


def dispatch(arg: str) -> str:
    """Parse 'op:arg1:arg2:...' and route to the matching op function.

    Traversal ops (grep, glob, tree, map) support an optional :::no-exclude
    suffix that bypasses all exclude-paths for that one call.
    Example: 'grep:pattern:vendor/:10:::no-exclude'
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
            old_str = _decode_escapes(parts[1] if len(parts) > 1 else "")
            new_str = _decode_escapes(parts[2] if len(parts) > 2 else "")
            rpath = parts[3] if len(parts) > 3 and parts[3] else "."
            dry = op == "replace_dry"
            body = op_replace(old_str, new_str, rpath, dry=dry)
        elif op == "edit":
            old_str = _decode_escapes(parts[1] if len(parts) > 1 else "")
            new_str = _decode_escapes(parts[2] if len(parts) > 2 else "")
            epath = parts[3] if len(parts) > 3 else ""
            body = op_edit(old_str, new_str, epath)
        elif op == "replace_lines":
            rl_path = parts[1] if len(parts) > 1 else ""
            try:
                rl_start = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                rl_end = int(parts[3]) if len(parts) > 3 and parts[3] else 0
            except ValueError:
                body = "ERROR: replace_lines START/END must be integers\n"
            else:
                # CONTENT may legitimately contain ':' — rejoin remaining parts
                rl_content = _decode_escapes(":".join(parts[4:]) if len(parts) > 4 else "")
                body = op_replace_lines(rl_path, rl_start, rl_end, rl_content)
        elif op == "vim":
            vim_path = parts[1] if len(parts) > 1 else ""
            vim_script = ":".join(parts[2:]) if len(parts) > 2 else ""
            body = op_vim(vim_path, vim_script)
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


# ---------------------------------------------------------------------------
# PreToolUse hook — pure logic, testable without stdin/env
# ---------------------------------------------------------------------------

def pre_tool_hook(payload: Dict[str, Any], enforced: bool) -> Tuple[int, str]:
    """Decide whether a tool call should be blocked.

    Args:
        payload: Claude Code hook payload (parsed JSON). Interesting keys:
            - tool_name: str (e.g. "Grep", "Bash")
            - tool_input.command: str (for Bash, the shell command)
        enforced: Whether enforcement is active (state file present).

    Returns:
        (exit_code, stderr_message). exit_code 0 = allow, 2 = block.
        stderr_message is shown to the model when blocked.
    """
    # Permissive mode: never block.
    if not enforced:
        return 0, ""

    tool_name = payload.get("tool_name", "")

    # Direct tool blocks
    if tool_name in BLOCKED_TOOLS:
        return 2, (
            f"Use ./supertool instead of {tool_name}.\n\n"
            "  ./supertool 'grep:PATTERN:PATH:LIMIT'\n"
            "  ./supertool 'glob:PATTERN'   (supports **)\n"
            "  ./supertool 'ls:PATH'\n\n"
            "Batch multiple ops in one call: "
            "./supertool 'read:A' 'read:B' 'grep:X:src/' 'glob:**/*.md'\n\n"
            "Disable enforcement: /supertool off\n"
        )

    # Bash command inspection
    if tool_name == "Bash":
        command = payload.get("tool_input", {}).get("command", "")
        # First token is the binary being invoked; handle leading whitespace.
        first_token = command.strip().split()[0] if command.strip() else ""
        # Strip leading env-var assignments (e.g. "FOO=1 grep ...") — check
        # the first real command token.
        while "=" in first_token and not first_token.startswith("="):
            # Looks like VAR=value; advance to next token
            tokens = command.strip().split()
            if len(tokens) < 2:
                break
            command = " ".join(tokens[1:])
            first_token = tokens[1]
        if first_token in BLOCKED_BASH_COMMANDS:
            return 2, (
                f"Bash({first_token} ...) is blocked while supertool "
                "enforcement is active.\n\n"
                "Use ./supertool instead:\n"
                "  cat FILE         → ./supertool 'read:FILE'\n"
                "  grep PAT PATH    → ./supertool 'grep:PAT:PATH:LIMIT'\n"
                "  find/glob        → ./supertool 'glob:PATTERN'\n"
                "  ls PATH          → ./supertool 'ls:PATH'\n"
                "  tail -N FILE     → ./supertool 'tail:FILE:N'\n"
                "  head -N FILE     → ./supertool 'head:FILE:N'\n"
                "  sed -n X,Yp FILE → ./supertool 'read:FILE:X:Y-X'\n\n"
                "Batch multiple ops in one call. "
                "Disable enforcement: /supertool off\n"
            )

    return 0, ""


def is_enforced() -> bool:
    """Check whether the enforcement state file is present."""
    return os.path.isfile(ENFORCE_STATE_FILE)


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


def main(argv: List[str]) -> int:
    if not argv:
        sys.stderr.write(
            "Usage: supertool op:args [op:args ...]\n"
            "       supertool 'read:file.py' 'grep:foo:src/:20' 'glob:**/*.md'\n"
            "       supertool --pre-tool-hook  (reads hook payload from stdin)\n"
        )
        return 1

    # Plugin hook mode — invoked by Claude Code's PreToolUse hook
    if argv[0] == "--pre-tool-hook":
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError:
            # Malformed input — allow the tool call to proceed (fail-open)
            return 0
        code, message = pre_tool_hook(payload, is_enforced())
        if message:
            sys.stderr.write(message)
        return code

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
