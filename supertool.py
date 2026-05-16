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
import os
import re
import shlex
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

    if exclude_paths and "**" in pattern:
        # Walk-based implementation for recursive globs with exclusions.
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



def op_vi(path: str, script: str) -> str:
    """vi-flavored cursor-based multi-action edit op.

    Actions split by newline OR semicolon. Each action: optional count
    prefix + verb + optional arg. Lifted from vi for token economy in
    LLM-generated edits.

    Cursor / search:
        gg          — top of file (BOF)
        G           — end of file (EOF)
        nG          — goto line n (1-indexed)
        0           — BOL
        $           — EOL
        /PAT        — find PAT forward (regex; literal fallback on re.error)
        ?PAT        — find PAT backward (regex; literal fallback on re.error)
        nh          — n chars left (default 1)
        nl          — n chars right (default 1)
        nj          — n lines down
        nk          — n lines up

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
        vi:::foo.py:::/def foo;A  # entry point

        # Insert a multi-line block before a marker
        vi:::skill.md:::/## Process;O## Task list;o1. Foo;o2. Bar

        # Rename a variable
        vi:::foo.py:::/old_name;ciwnew_name

        # Replace a string literal
        vi:::foo.py:::/setLabel(;l;ci"New Label"

        # Replace a function arg list
        vi:::foo.py:::/foo(;ci(x, y, z

        # Replace a whole line
        vi:::foo.py:::/return false;ccreturn true;

        # Insert code that contains a semicolon
        vi:::foo.py:::Areturn $x\\;

        # Join 2 lines
        vi:::log.txt:::5G;J

        # Delete 3 lines starting at line 10
        vi:::log.txt:::10G;3dd
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

    raw_actions: List[str] = []
    for line in script.split("\n"):
        # Split on `;` but honor `\;` as a literal semicolon so inserted TEXT
        # can contain semicolons without colliding with the action separator.
        # Backslash-related escapes (`\\`, `\n`, `\t`, `\r`) live in
        # _decode_escapes — applied later on TEXT arguments only.
        parts: List[str] = []
        buf: List[str] = []
        idx = 0
        while idx < len(line):
            ch = line[idx]
            if ch == "\\" and idx + 1 < len(line) and line[idx + 1] == ";":
                buf.append(";")
                idx += 2
            elif ch == ";":
                parts.append("".join(buf))
                buf = []
                idx += 1
            else:
                buf.append(ch)
                idx += 1
        parts.append("".join(buf))
        for seg in parts:
            # lstrip only — trailing whitespace inside insert TEXT is significant
            seg = seg.lstrip()
            if seg:
                raw_actions.append(seg)
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
        # two-char ex command :s/PAT/REPL/flags
        if len(rest) >= 2 and rest[:2] == ":s":
            return (count, ":s", rest[2:])
        # two-char yank/delete word/eol: yw, y$, yy, dw, d$, d0, c$, c0, cf, cF, ct, cT, df, dF, dt, dT
        if len(rest) >= 2 and rest[:2] in (
            "gg", "dd", "cc", "cw",
            "yy", "yw", "y$",
            "dw", "d$", "d0",
            "c$", "c0",
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
        # single-char arg
        if c == "r":
            return (count, c, rest[1:2])
        # char-find on line: f<c>, F<c>, t<c>, T<c>
        if c in ("f", "F", "t", "T") and len(rest) >= 2:
            return (count, c, rest[1])
        # standalone
        if c in ("h", "j", "k", "l", "0", "$", "G", "D", "x", "J", "n", "N", "p", "P"):
            return (count, c, rest[1:])
        return (count, "", rest)  # unknown

    cursor = 0
    log: List[str] = []
    last_search: Optional[tuple] = None  # (pattern, direction "/"|"?")
    register: str = ""  # anonymous yank/paste register
    register_linewise: bool = False  # True if last yank was line-wise (yy)
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
            if idx == -1:
                hint = ""
                m = re.search(r"/([oOiIaAJ]|cc|cw|ciw|ci[\"'([{}]|cf|cF|ct|cT|dd|dw|d\$|d0|c\$|c0)\b", pat)
                if m is not None:
                    suggested = pat[:m.start()] + ";" + pat[m.start() + 1:]
                    hint = f" (hint: '/' is not an action separator — did you mean '/{suggested}'? Use ';' to chain actions.)"
                return f"ERROR: action {i} '{action}': pattern not found forward{hint}\n"
            cursor = idx
            last_search = (pat, "/")
            log.append(f"  {i}. /{pat!r} → {cursor}")
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
            if idx == -1:
                return f"ERROR: action {i} '{action}': pattern not found backward\n"
            cursor = idx
            last_search = (pat, "?")
            log.append(f"  {i}. ?{pat!r} → {cursor}")
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
            cursor = pos + len(text)
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
            log.append(f"  {i}. {verb}{target!r} → {cursor}")

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
            n_max = 0 if "g" in sflags else 1
            srepl_dec = _decode_escapes(srepl)
            # Escape literal backslashes for re.sub: \X (X non-digit) must be
            # passed as \\X or re.sub raises "bad escape" on \B, \R, etc.
            # Digit-prefixed backslashes (\1..\9) are preserved as backrefs.
            srepl_safe = re.sub(r"\\(?=\D)", r"\\\\", srepl_dec)
            try:
                new_content, n = rx.subn(srepl_safe, content, count=n_max)
            except re.error as e:
                return f"ERROR: action {i} '{action}': :s replacement: {e}\n"
            if n == 0:
                return f"ERROR: action {i} '{action}': :s no match for {spat!r}\n"
            content = new_content
            cursor = min(cursor, len(content))
            log.append(f"  {i}. :s/{spat!r}/{srepl_dec!r}/{sflags} ({n} subs)")

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

        else:
            return (
                f"ERROR: action {i} '{action}': unknown verb '{verb}' "
                f"(expected gg, G, nG, 0, $, /, ?, n, N, h, j, k, l, J, "
                f"f, F, t, T, i, a, I, A, o, O, x, dd, D, dw, d$, d0, "
                f"cw, cc, c$, c0, ciw, ci\", ci', ci(, ci[, ci{{, "
                f"cf<c>, cF<c>, ct<c>, cT<c>, df<c>, dF<c>, dt<c>, dT<c>, "
                f"yy, yw, y$, yf<c>, yF<c>, yt<c>, yT<c>, p, P, r, :s/PAT/REPL/[gi])\n"
            )

    try:
        _atomic_write(path, content)
    except OSError as e:
        return f"ERROR: failed to write {path}: {e}\n"

    final_line, final_col = _offset_to_line_col(content, cursor)
    new_lines = content.split("\n")
    ctx_start = max(1, final_line - 2)
    ctx_end = min(len(new_lines), final_line + 2)

    out = [
        f"vi {path} ({len(raw_actions)} actions, "
        f"cursor at {final_line}:{final_col})\n"
    ]
    out.extend(line + "\n" for line in log)
    out.append("--- context ---\n")
    for ln in range(ctx_start, ctx_end + 1):
        marker = "→" if ln == final_line else " "
        text = new_lines[ln - 1] if ln - 1 < len(new_lines) else ""
        out.append(f"  {ln:>5} {marker} {text}\n")
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
        elif op == "vi":
            vi_path = parts[1] if len(parts) > 1 else ""
            vi_script = ":".join(parts[2:]) if len(parts) > 2 else ""
            body = op_vi(vi_path, vi_script)
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
