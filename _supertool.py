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
    read:PATH                  Read file (first 300 lines, 20KB cap). A repeat
                                read of a BYTE-IDENTICAL file inside 15 minutes
                                returns a one-line elision naming the sha, the
                                bytes withheld and read:PATH:full to get them.
                                A changed file is never elided.
    read:PATH:START-END        Read an explicit line range, inclusive
    read:PATH:OFFSET:LIMIT     Read with offset and line limit. OFFSET is a
                                skip count, not a start line — :19:1 returns
                                line 20. The window it actually returned is
                                stated in the header whenever OFFSET > 0.
                                Prefer :START-END when you know the lines.
    grep:PATTERN:PATH          Search pattern (10 results default).
                                Auto-reads full file if PATH is a concrete
                                file < 20KB with a match.
    grep:PATTERN:PATH:no-auto-read
                               Suppress the single-file auto-read — only the
                                matching line(s) are emitted (parity with glob).
    grep:PATTERN:PATH:LIMIT    Search with custom result limit. LIMIT 0 is
                                refused — it is not "unlimited" here. Omit
                                LIMIT for the default.
    grep:PATTERN:PATH:all      Every match, no cap — for "find every one"
                                (a call-site audit, a rename, a sweep). The
                                count line reads `limit all` and can never
                                carry TRUNCATED. `all` is a LIMIT only —
                                anywhere else in the token list it is
                                refused, never re-read as something it
                                could have meant.
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
                                read:PATH:::grep=P searches the WHOLE file; give
                                an OFFSET/LIMIT and the zero names what it did
                                not search.
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
from typing import Any, Dict, Iterable, List, MutableMapping, NamedTuple, Optional, Sequence, Tuple

VERSION = "0.33.0"


def _fwd(p: str) -> str:
    """Normalize path separators to forward slashes for cross-platform output."""
    return p.replace(os.sep, "/")


DETERMINISTIC_TIME_ENV = "SUPERTOOL_DETERMINISTIC_TIME"


def _deterministic_time() -> bool:
    """Is the duration freeze on? See `_elapsed_since` for why it exists."""
    return os.environ.get(DETERMINISTIC_TIME_ENV) == "1"


def _timeout_verdict_line(t0: float, timeout: float) -> str:
    """The `FAIL (timeout ...)` line, with its elapsed figure kept honest (#727).

    `FAIL (timeout 0.0s > 10s)` asserts an op blew a 10s budget and reports
    that it took no time at all. A reader cannot tell which half to believe,
    and read literally the message points at the budget rather than at the
    process — a diagnostic whose own figures contradict its verdict is worse
    than one that says nothing.

    Two ways the elapsed can come out below the budget, and they need
    different words because they are different facts:

    * The `SUPERTOOL_DETERMINISTIC_TIME` freeze (#643) is on. Everywhere else
      zeroing a measured duration removes noise; on this one path it removes
      the evidence, because the elapsed is the only number the message exists
      to carry. The freeze still applies here — exempting this renderer would
      put a varying field back into rendered output, which is the hole #643
      closed *at the renderer* precisely so no call site has to remember it.
      What changes is that the absence announces itself instead of posing as a
      measurement. That is this repo's three-state contract applied to a number
      rather than to a verdict.
    * Anything else. `subprocess.run(timeout=T)` raises `TimeoutExpired` only
      once T has actually elapsed, so a measured span under the budget is not a
      fast machine or a near miss — it means this reporting path did not
      measure the interval that expired. Say that, rather than printing the
      number as if it were a result.
    """
    if _deterministic_time():
        return (f"FAIL (timeout after its {timeout}s budget - elapsed frozen, "
                f"deterministic-time mode)")
    elapsed = time.monotonic() - t0
    if elapsed < timeout:
        return (f"FAIL (timeout {elapsed:.1f}s > {timeout}s - elapsed is under "
                f"the budget, which no measured timeout can be: this is a bug "
                f"in the reporting path, not a result (#727))")
    return f"FAIL (timeout {elapsed:.1f}s > {timeout}s)"


def _elapsed_since(t0: float) -> float:
    """Seconds since `t0`, or 0.0 when durations must be deterministic (#643).

    Every duration supertool prints — the `[validators]` time column, the
    `PASS (0.02s)` header on a custom op — is wall clock it measured itself. So
    two runs of the same op render two different strings, differing only in
    that field.

    That breaks tests in the dangerous direction. A test asserting two rendered
    blocks are *indistinguishable* passes on the jitter alone, reports the
    defect fixed, and does it non-deterministically depending on scheduling and
    on whether xdist is in play. It happened in #621's own RED run: the test
    went green while the bug it targeted was fully present.

    Set `SUPERTOOL_DETERMINISTIC_TIME=1` and every measured duration renders as
    a frozen placeholder, so no comparison can ever see a varying field.
    `tests/conftest.py` sets it for the whole suite. Test-only: nothing in
    normal operation sets it, and a real run still reports the real time —
    that number is how a human sees which validator is slow.
    """
    if _deterministic_time():
        return 0.0
    return time.monotonic() - t0


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
# `all` in grep's LIMIT slot (#1328). `grep` had one shape for two questions —
# "show me some", where a cap is a feature, and "find every one", where a cap is
# a wrong answer with an honest marker under it. The token is a sentinel rather
# than a big number so the count line can print `limit all`: a caller reading
# `limit 9223372036854775807` learns nothing about whether the sweep was
# complete, and the count line is the part that survives a pipe.
GREP_LIMIT_ALL = -1
_GREP_ALL_TOKEN = "all"
# `all` parsed out of the CONTEXT slot. Reading it as a limit would run a call
# nobody typed, and dropping it would run the default under a token the caller
# believes changed something.
GREP_LIMIT_ALL_MISPLACED = -2
MAX_GREP_COUNT_CEILING = 1000  # how far past LIMIT grep keeps counting so a
# truncated answer can state its scope (#1073). Counting every match forfeits
# the early exit: measured over a 67,855-file tree, a dense pattern's walk went
# from 0.01s to 10.3s uncounted-to-counted, while stopping at 1000 cost 0.05s.
# The bound is on matches, not files, so a sparse pattern still reads the tree
# — which is what a non-truncated grep already does, so the op's worst case is
# unchanged rather than raised.
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
        preset_ops = data.get("ops")
        if not isinstance(preset_ops, dict):
            continue
        for op_name in preset_ops:
            if isinstance(op_name, str) and op_name not in _BUILTIN_OPS:
                index.setdefault(op_name, fname[:-5])
    _SHIPPED_PRESET_OPS = index
    return index


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
    """Ops that honour a leading ``repo:OWNER/NAME``."""
    return {op for op, mode in _repo_target_modes().items() if mode == "op"}


def _repo_refusal(op: str) -> str:
    """Why this call cannot carry a ``repo:`` op, and what to do instead.

    Named rather than generic, because the two reasons have different fixes: an
    op with its own payload key wants the target moved, an op with no repo
    dimension at all wants the target dropped or the call split.
    """
    if _repo_target_modes().get(op) == "payload":
        return (
            f"repo: {op!r} takes its repo target in the payload "
            f'(repo = "OWNER/NAME"), not from a repo: op — so there is one '
            f"place the target comes from. Set it there and drop the repo: op.\n"
        )
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
    return (f"> Built-in ops only. No .supertool.json was found from {os.getcwd()}, "
            f"so {len(missing)} shipped presets ({names}) — {n_ops} ops — are not "
            f"loaded here. Run from a project that enables them, or make the first "
            f"op 'cwd:<project-path>'.")


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
    global _CONFIG, _CONFIG_CHECKED, _CONFIG_PATH, _mcp_specs
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
    """The third state for a config op whose provenance is unknown (#678).

    Not a finding — nothing was found wrong with the op. An absence: the tool
    cannot say which version would answer, so it says that instead of printing a
    `PASS` indistinguishable from one the invoked build produced. Same contract
    as `docs/validators.md` §"Declining instead of guessing".
    """
    core, other = pair
    return (
        f"SKIPPED: '{op}' comes from a different supertool tree than the core "
        f"that is running.\n"
        f"  core:    {core}/supertool.py (the file you invoked)\n"
        f"  presets: {other} (resolved from this cwd — its .supertool.json and "
        f"presets/ would answer)\n"
        f"Declined rather than PASSing for a build the tool cannot name: the "
        f"code you meant to exercise would not have run, and the answer would "
        f"have looked exactly like a correct one (#678).\n"
        f"Fix: run from {core}, or make the first op 'cwd:{core}'. To mix on "
        f"purpose, set {_MIXED_TREE_ENV}=1 — the receipt then carries the "
        f"pairing instead of a bare PASS.\n"
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
    """
    env_key = f"SUPERTOOL_{op_name.upper()}_{key.upper()}"
    env_val = os.environ.get(env_key)
    cfg = _load_config()
    op_cfg = cfg.get("builtin-ops", {}).get(op_name, {})
    val = op_cfg.get(key)
    fallback = val if isinstance(val, int) and val > 0 else default
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
    signal = tuple(p for p in exclude_paths if p not in _NOISE_EXCLUDE_SET)
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
            [rtk] + args, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace"
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None

# Built-in op names — custom ops/aliases with these names are ignored
_BUILTIN_OPS = {"read", "grep", "grep_around", "glob", "ls", "tail", "head", "wc", "check", "around", "map", "diff", "stat", "around_line", "tree", "replace", "replace_dry", "edit", "replace_lines", "paste", "append", "vi", "validate", "format", "validate_staged", "format_staged", "workspace", "resolve", "diag", "hover", "rename"}

# Ops the dispatcher handles but that are absent from _BUILTIN_OPS, which is a
# shadowing blocklist ("custom ops with these names are ignored") and not a
# capability list. Kept separate so the blocklist's semantics are untouched.
_DISPATCH_ONLY_OPS = {
    "between", "vim", "batch", "gc", "help", "version",
    "ops", "ops-compact", "introduction", "output-format",
}

# Valid from the CLI but never reaching dispatch(): main() honours and strips
# them before the op loop. They belong in any list a caller reads.
_MAIN_LEVEL_OPS = {"cwd", "repo"}


def _valid_op_names() -> List[str]:
    """Every op name this binary accepts regardless of config, sorted.

    The unknown-op error used to carry a hand-written list of 18 names while the
    dispatcher accepted 40+ — the tool under-reporting its own capability, which
    is exactly the defect #614 is about, one layer in. Derived from the sets
    dispatch really uses so it cannot rot again. ``vi`` is dropped: it lingers in
    _BUILTIN_OPS from before the op was renamed ``vim`` and no branch handles it.
    """
    return sorted((_BUILTIN_OPS | _DISPATCH_ONLY_OPS | _MAIN_LEVEL_OPS) - {"vi"})


def _unknown_op_message(op: str) -> str:
    """Answer "can I do this?" in three states, not two (#614).

    ``docs/validators.md``'s "Declining instead of guessing" one layer out: a
    checker that cannot act must distinguish *no such thing* from *not from
    here*, because emitting the first when it means the second is an absence the
    tool produced being read as an absence in the world. It cost this repo's
    heaviest user two debugging detours in one evening — ``unknown operation:
    gh-job`` read as "the installed build predates that op", and several turns
    went into hand-rolled ``gh api`` calls that worked, so nothing looked broken.

    The only absence namable honestly is a shipped preset op: it ships beside
    supertool.py, so its existence is a fact about this binary. Everything else —
    a typo, a custom op from a config we never saw — stays *unknown*, on purpose.
    Hedging every miss into "maybe you need a project root" would trade a correct
    message for a guess, which is the same bad trade in the other direction.
    """
    preset = _shipped_preset_ops().get(op)
    if preset is not None:
        if _CONFIG_PATH:
            return (
                f"ERROR: op '{op}' is unavailable here, not unknown — it is provided "
                f"by the shipped preset '{preset}', which {_CONFIG_PATH} does not "
                f"enable.\n"
                f'       Fix: add "{preset}" to that file\'s "presets" list, or make '
                f"this call's first op 'cwd:<project-path>' pointing at a project "
                f"that already enables it.\n"
                f"       'ops' lists what is loaded here.\n"
            )
        return (
            f"ERROR: op '{op}' is unavailable here, not unknown — it is provided by "
            f"the shipped preset '{preset}'.\n"
            f"       No .supertool.json was found from {os.getcwd()} or any parent, "
            f"so no preset ops and no project ops are loaded — only the built-ins.\n"
            f"       Fix: run it from a project that enables the '{preset}' preset, "
            f"or make this call's first op 'cwd:<project-path>'.\n"
            f"       'ops' lists what is loaded here.\n"
        )
    msg = (f"ERROR: unknown operation: {op}\n"
           f"Valid operations: {', '.join(_valid_op_names())}\n")
    loaded = _load_config().get("ops") or {}
    if loaded:
        msg += (f"Plus {len(loaded)} project/preset ops loaded from "
                f"{_CONFIG_PATH or 'config'} — run 'ops' for the full list.\n")
    return msg


# The three safety classes a roster row can carry (#1231).
#
# `ops` is 47,254 bytes and `ops-compact` 9,067 against a ~7,168-byte
# SessionStart cap, so the startup listing is truncated *today* and every op
# alphabetically after `grep` is hidden — the whole gh-*/git-* families, radar,
# watch. What is lost is **existence**, which a reader cannot miss because they
# never learn there was something to look for. A roster of every name fits in
# about a fifth of the cap.
#
# A bare name is only actionable for an op you may probe: `between:F:747:820`
# answers "'820' was read as the path" and names the op that does take a range,
# so roster → call → the error teaches the form. You cannot probe `oss_train`,
# which force-pushes a merge train. Hence the class.
#
# Declared, never inferred. `_PARALLEL_SAFE_OPS` looks like a ready-made
# read-only set and is not one — it is a dispatch-safety set, and until #1244
# it carried `format_staged`, which runs formatters over every staged file and
# writes them. Deriving "read-only" from it here would have rendered a mutating
# op probe-safe: an error in the one direction that costs something.
#
# The two agree as of #1244 and a test now holds them together, so this is no
# longer a live contradiction. The rule survives the repair anyway: a class
# read off another set's membership is only ever as right as that set's last
# edit, and this table is the declaration site.
#
# "acts" is about consequence, not about spawning: nearly every preset op runs
# a python subprocess. `*_status_since` is the case that proves it — it reads a
# feed, which sounds read-only, and writes `~/.config/<service>/last_check` on
# success. One probe to learn the signature advances the watermark, and the
# next real briefing reports an empty window it silently consumed. That is this
# repo's own defect wearing a safety class, so those three are "acts".
_SAFETY_CLASSES = ("read-only", "writes", "acts")

# Marker rendered beside a name. Read-only is unmarked, and that is a positive
# claim rather than an absence: the fallback for an op whose class is not
# declared is "acts", so a missing class renders `!` and is never the quiet one.
_SAFETY_MARKERS = {"read-only": "", "writes": "*", "acts": "!"}

# Safety class of every built-in. A fact about this binary, so it lives beside
# the sets that declare the built-ins exist rather than in a project's
# `.supertool.json` — which may be absent, stale, or another project's.
_OP_SAFETY_BUILTIN: Dict[str, str] = {
    # read-only — call blind; the error message teaches the signature
    "around": "read-only", "around_line": "read-only", "between": "read-only",
    "check": "read-only", "cwd": "read-only", "diag": "read-only",
    "diff": "read-only", "glob": "read-only", "grep": "read-only",
    "grep_around": "read-only", "head": "read-only", "help": "read-only",
    "hover": "read-only", "introduction": "read-only", "ls": "read-only",
    "map": "read-only", "ops": "read-only", "ops-compact": "read-only",
    "output-format": "read-only", "read": "read-only",
    "repo": "read-only",
    "replace_dry": "read-only", "resolve": "read-only", "stat": "read-only",
    "tail": "read-only", "tree": "read-only", "validate": "read-only",
    "validate_staged": "read-only", "version": "read-only",
    "wc": "read-only", "workspace": "read-only",
    # writes — changes files in this tree
    "append": "writes", "batch": "writes", "edit": "writes",
    "format": "writes", "format_staged": "writes", "gc": "writes",
    "paste": "writes", "rename": "writes", "replace": "writes",
    "replace_lines": "writes", "vim": "writes",
}


# Read-only built-in ops. Two consumers, one predicate, because they ask the
# same question: `_main`'s ThreadPool gate, and the `_path_meta_bulk_drop()` in
# `dispatch`'s `finally` that keeps the repo-wide `git status` snapshot from
# outliving a write. Excludes mutating ops (replace, edit, replace_lines) and
# custom ops (could shell out to anything). `between` is included — pure file
# read.
#
# "Read-only" is the contract, not a description of whoever is currently in the
# set, and the difference cost something. Until #1244 this carried
# `format_staged`, which shells formatters over every staged file and rewrites
# them. Under SUPERTOOL_PARALLEL, `supertool 'format_staged' 'read:f.txt'`
# rendered the *pre*-format bytes — under `[complete file — no more lines]`,
# a positive completeness claim — while the post-format bytes were already on
# disk. The same call sequentially was right, so the answer depended on a
# performance switch.
#
# "Safe to run concurrently" was the tempting weaker reading and it is not
# available: parallel safety is a property of the whole set of ops in one call,
# and a writer is unsafe beside any reader of the same path. Membership is
# read-only or nothing.
#
# `_OP_SAFETY_BUILTIN` above declares the same fact for `ops` output, and
# tests/test_parallel_safe_writes_1244.py asserts the two cannot drift apart
# again — two sources for one truth is what filed this.
_PARALLEL_SAFE_OPS = {
    "read", "grep", "glob", "ls", "head", "tail", "wc", "stat",
    "map", "tree", "around", "around_line", "between", "diff", "blame",
    "version", "validate", "validate_staged", "workspace",
    "resolve", "diag", "hover", "help",
}


def _is_parallel_safe(arg: str) -> bool:
    """Return True if the op name is in the read-only safe set.

    Detects op name from `op:...` or `op:::...` prefix. Anything else —
    custom ops, mutating ops, malformed args — is treated as unsafe.

    Callers use it for two different-looking questions — may these ops share a
    thread pool, and may the status snapshot survive this op — which are the
    same question about whether the op writes.
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


def _safe_path(p: str, *, allow_outside_cwd: Optional[bool] = None,
               root: Optional[str] = None, boundary: str = "cwd") -> str:
    """Resolve `p` and enforce repo-root containment (closes #146).

    Strict mode (default): the realpath of `p` must equal cwd or live under
    cwd. Symlinks crossing the boundary are rejected. `..` traversal that
    escapes cwd is rejected. Returns the resolved absolute path.

    `root` moves the boundary without moving the rule (#1287). The core's own
    boundary is the cwd and stays the default; a preset op that resolves its
    argument against something else — `claims` resolves a relative path against
    the repository root, so a cwd boundary would refuse `claims:docs/x.md` run
    from `docs/` — declares that root and gets the *same* check under it.
    `boundary` is only the word the refusal uses, so the message names the line
    the caller actually crossed. Parameterised rather than reimplemented: the
    one thing this repo has learned twice about containment is that a second
    copy of the rule drifts (#882, #889).

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
        raise SecurityError(f"path cannot be resolved: {shown!r} ({e})") from e
    if allow_outside_cwd:
        return abs_p
    # Windows: NTFS is case-insensitive (`C:\Users` == `c:\users`) and uses
    # backslash separators. `os.path.normcase` lowercases + normalises
    # separators on Windows; on POSIX it's a no-op so the check stays exact.
    # This also handles drive-letter case (`c:\` vs `C:\`) and forward-slash
    # variants (`C:/Users` vs `C:\Users`).
    abs_p_cmp = os.path.normcase(abs_p)
    base = os.path.realpath(root) if root else os.path.realpath(os.getcwd())
    root_cmp = os.path.normcase(base)
    if abs_p_cmp == root_cmp:
        return abs_p
    if not abs_p_cmp.startswith(root_cmp + os.sep):
        # %s for the root, %r for the caller's own string: on Windows a %r
        # doubles every separator, so a caller comparing the printed root
        # against a real one would never match (#1283). Named only when it is
        # NOT the cwd — the default message is load-bearing in a dozen tests
        # and gains nothing from echoing the directory the caller stands in.
        where = "" if root is None else f", root {base}"
        raise SecurityError(
            f"path escapes {boundary}: {p!r} (resolved to {abs_p!r}{where}). "
            f"To allow: set SUPERTOOL_ALLOW_OUTSIDE_CWD=1 (env), or add "
            f'`\"allow_outside_cwd\": true` to .supertool.json.'
        )
    return abs_p


def _containment_error(candidates: Iterable[str], *,
                       root: Optional[str] = None,
                       boundary: str = "cwd") -> Optional[str]:
    """The one containment gate every path-bearing route passes through.

    Returns the ``ERROR: …`` line to print, or ``None`` when every candidate is
    contained. Dispatch applies it positionally from ``_PATH_ARG_POSITIONS``;
    the ``@payload`` route applies it to the fields a payload names. **One rule,
    one implementation, several callers** — #882 was a second copy of this rule
    written beside the real one, and it covered the list form while missing the
    single-path one, so ``{"path":"/etc/hosts"}`` validated a file
    ``validate:/etc/hosts`` refuses. A third copy would drift the same way.

    ``""`` and ``"."`` are skipped because neither is a filename: one means no
    path was given, the other *is* cwd, and ``_safe_path`` allows both anyway
    — the skip saves a `realpath` and grants nothing.

    ``"full"`` and ``"raw"`` used to sit beside them and are a different thing
    entirely: they are names a repo can hold. They were read's mode tokens,
    which live at ``parts[2]``/``parts[3]`` — no ``_PATH_ARG_POSITIONS`` slot
    is ever one, so the skip never had a case to serve even on dispatch. What
    it did have was an effect: #884 moved it from the dispatch loop into this
    helper, and the ``@payload`` route, whose ``paths`` entries are always
    filenames, inherited it — a symlink named ``raw`` pointing outside the root
    then validated where an ordinary name was refused (#889). Parity held and
    was not the property wanted; the routes agreed on the wrong rule. Deleted
    rather than moved back or made a parameter: a skip that guards nothing and
    opens something is only the hole.
    """
    for candidate in candidates:
        if not candidate or candidate == ".":
            continue
        try:
            _safe_path(candidate, root=root, boundary=boundary)
        except SecurityError as exc:
            return f"ERROR: {exc}\n"
    return None


#: Registry keys whose value is a boundary this core knows how to enforce.
#: `cwd` is the core's own and the default everywhere else; `repo` is the
#: repository root, which is what `claims` resolves relative arguments against.
_PATH_BOUNDARIES = ("cwd", "repo")

#: Syntax-string components that mean "this argument is a filesystem path".
#: Matched per `_`-separated component so `MD_FILE`, `TEXT_OR_FILE_OR_file`
#: and `@FILE` all count, while `NUMBER_OR_BRANCH` and `PATTERN` do not.
_PATH_SYNTAX_COMPONENTS = frozenset(("PATH", "PATHS", "FILE", "FILES"))

_SYNTAX_TOKEN_RE = re.compile(r"[^A-Za-z0-9_]+")

#: The core's own path placeholders in a `cmd` template. `{file}` is
#: `parts[1]`; `{dir}` is its `os.path.dirname`. Both are substituted by
#: `_resolve_custom_op` below, so an op writing either has already told the
#: core which argument it means a filesystem path to be — a stronger signal
#: than a prose `syntax` string, and the one #1350 was filed about.
#:
#: `{arg}` is deliberately absent even though it substitutes the very same
#: `parts[1]`. Sixteen shipped ops pass a handle, a ref, a tag, an ID or a
#: repo slug through it and none of them takes a path, so promoting it would
#: refuse all sixteen and gate nothing. `{file}` and `{dir}` are the
#: placeholders whose NAME is the claim; `{arg}` is the one that declines to
#: make it. An op that means a path and writes `{arg}` is still ungated, and
#: that is a naming problem in the op rather than a hole the core can close
#: without over-refusing.
_PATH_CMD_PLACEHOLDERS = ("{file}", "{dir}")

#: Preset ops that name a path and predate the declaration (#1287). **This set
#: only ever shrinks.** It is not a policy — it is a debt register: 24 shipped
#: PRESET ops name a path, 5 declare a boundary, these 19 do not. It opened at
#: 20 — see the #1351 note below for the one it has lost. Counting this repo's
#: own `.supertool.json` as well makes it 25 and 6, the extra one being
#: `oss_train`; all four numbers are pinned in
#: `tests/test_cmd_placeholder_path_detector_1350.py` so this comment cannot
#: drift again.
#: Refusing them at the time would have broken every one of them in the same
#: release that introduced the rule, so they are grandfathered *by name*,
#: which means a
#: newly written op cannot inherit the old default by being written after it —
#: it is refused at dispatch and red in
#: `tests/test_preset_path_chokepoint_1287.py` until it declares.
#:
#: It has shrunk once, to 19: `gl-api` now declares `{"args": []}` for real
#: (#1351). It was the op `_syntax_names_a_path`'s docstring held up as the
#: worked example of the declared pattern while sitting in this register — the
#: guard citing a member of the set it exists to empty.
#:
#: Several of these cannot be expressed as a `parts` index at all: the publish
#: ops carry their file inside a `|`-separated blob, `git-commit` takes a
#: `:::`-separated tail, `git-resolve` a comma list. Draining the register is
#: therefore per-op work, not a sweep, and the ops living in files with open
#: PRs (`presets/git*`, `presets/github*`, `presets/gitlab*`) were left alone
#: here on purpose rather than overlooked.
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
    """Does this op's registry syntax string name a filesystem path argument?

    The detector, not the declaration. The registry's `syntax` is already
    parsed rather than merely displayed, and it is the one field every op
    fills in, so it is what tells the core that an op *should* have declared a
    boundary. It cannot say *where* the path is — `TITLE|MD_FILE|CANONICAL`
    hides one inside a pipe-separated blob — which is exactly why the position
    is declared explicitly in `paths` and this function only ever produces the
    question, never the answer.

    Over-detection is the safe direction and it happens: `gl-api:PATH` is an
    API route, not a file. That op answers with `"paths": {"args": []}` — a
    declaration that no argument here is a filesystem path, which is a claim
    someone made rather than a default nobody noticed. It only became true in
    #1351: for one release `gl-api` was cited here as the declared example
    while sitting in `_UNDECLARED_PATH_OPS`, so the sentence teaching the next
    author what "declared" looks like pointed at an ungated op.

    It is also not the only detector. `_cmd_names_a_path` reads the `cmd`
    template for the core's `{file}` / `{dir}` placeholders, and
    `_entry_names_a_path` is the OR of the two — see #1350. The two are not
    ranked: of the 24 shipped ops this function detects, zero carry either
    placeholder, so a `cmd`-supersedes-`syntax` detector would have disarmed
    the gate for every one of them.
    """
    if not isinstance(syntax, str):
        return False
    for token in _SYNTAX_TOKEN_RE.split(syntax):
        for component in token.split("_"):
            if component in _PATH_SYNTAX_COMPONENTS:
                return True
    return False


def _cmd_names_a_path(cmd: Any) -> Optional[str]:
    """Which core path placeholder this `cmd` template substitutes, if any.

    Returns the placeholder rather than a bool so the refusal can name the
    signal that actually fired. An op with no `syntax` key was told it "names
    a path in its syntax ()", which reads as a bug in the guard rather than a
    demand on the op.
    """
    if not isinstance(cmd, str):
        return None
    for placeholder in _PATH_CMD_PLACEHOLDERS:
        if placeholder in cmd:
            return placeholder
    return None


def _entry_names_a_path(entry: Any) -> Optional[str]:
    """Why this op is being asked to declare a boundary, or `None` (#1350).

    The whole detector, in one place, returning the phrase the refusal prints.
    Two signals, OR'd, neither superseding the other:

    * the `syntax` string names a `PATH`/`FILE` component — 24 shipped ops;
    * the `cmd` template substitutes `{file}` or `{dir}` — one, `oss_train`.

    Before this, only the first was read, so an op with no `syntax` key at all
    took the `return None` arm: no declaration demanded, no check run, and a
    verdict indistinguishable from declared-clean. That is #1287's own rule
    answering "no path here" where it meant "I could not tell" — the two-state
    shape the rule exists to refuse, inside the rule.

    A bare-string op entry (`{"ops": {"lint": "php -l {file}"}}`) never reaches
    here: `_preset_path_containment` returns early on a non-dict. A string has
    no `paths` key and so no way to answer the demand, and the answer is to
    write the op as an object — `docs/contributing.md` has the reasoning.
    """
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
    """How a boundary is named in a refusal.

    A function rather than a module-level dict: #397's register in
    `tests/test_state_reset_and_lint_timeout.py` accounts for every mutable
    global in this file, and a constant lookup table is still a mutable global.
    Two strings rather than one word because "path escapes cwd" is already in a
    dozen tests and dozens of transcripts, and the repo one has to read as a
    sentence.
    """
    return "the repository root" if boundary == "repo" else "cwd"


def _repo_root_for_containment() -> str:
    """Nearest ancestor of cwd holding a `.git`, else cwd.

    `.git` is tested with `exists`, not `isdir`: in a linked worktree it is a
    *file* pointing at the common dir, and this repo's own agents work almost
    exclusively in worktrees.

    No subprocess. `git rev-parse --show-toplevel` is what `presets/claims`
    uses and the two agree everywhere it matters; spawning git on every gated
    call to learn something a directory walk already knows would put a process
    launch in front of every preset op that takes a path. Where they could
    disagree — `GIT_WORK_TREE` pointing elsewhere — the core has already
    scrubbed those variables out of the environment (#692, #714).

    Falling back to cwd rather than to `/` is deliberate: a narrower boundary
    refuses more, and the failure mode of guessing wide is the bug this whole
    chokepoint exists to close.
    """
    d = os.path.realpath(os.getcwd())
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.path.realpath(os.getcwd())
        d = parent


def _undeclared_path_refusal(op: str, signal: str) -> str:
    """What an op that takes a path and declares no boundary gets (#1287).

    A refusal, not a `skipped`. The three-state rule this repo applies
    everywhere else — `ok`, a finding, `skipped` — has no third state here,
    because a path argument that reaches no check is not a check that could
    not run. It is an unchecked read, and it renders identically to a checked
    one.
    """
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
    """The universal half of #1287: one chokepoint, a declared boundary.

    Every preset and custom op reaches `_resolve_custom_op`, so that is where
    a path argument can be gated once instead of per-op. What the core cannot
    supply is the *boundary*: `_PATH_ARG_POSITIONS` imposes the cwd on every
    builtin, and imposing it on presets too would refuse `claims:docs/x.md`
    run from `docs/` — a call that works today and should, because `claims`
    resolves relative arguments against the repository root.

    So the op declares both, and the core enforces both:

        "paths": {"args": [1], "root": "repo"}

    Returns the `ERROR: …` line to print, or `None` to let the op run.
    """
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
        # Negative indices are refused rather than resolved Python-style. They
        # would read as "the last argument", which is a real shape — `between`
        # symbol mode takes `parts[-1]` — but the core cannot honour it here:
        # `parts[-1]` on a bare `op` call is the op NAME, so the declaration
        # would gate a token that is never a path and skip the one that is.
        # Refused loudly because the alternative found in review was worse:
        # an out-of-range index was silently filtered out of the generator
        # below, so a typo'd declaration ran the op completely unchecked while
        # looking exactly like a declared one.
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
    return _containment_error(
        (parts[i] for i in decl["args"] if 0 <= i < len(parts)),
        root=_repo_root_for_containment() if boundary == "repo" else None,
        boundary=_path_boundary_label(boundary),
    )


def _path_not_found(path: str, *, label: str = "path",
                     suggest: Optional[str] = None,
                     op: Optional[str] = None,
                     call_prefix: Optional[str] = None) -> str:
    """The "not found" error, naming the path it actually tried (#624).

    `ERROR: file not found: src/foo.py` is true and useless: it cannot tell a
    typo from a cwd that drifted — a `cd` in an earlier shell call, which is
    the shape #624 was filed about. The absolute path tried separates the two
    at a glance, with no second `pwd` round-trip.

    When cwd sits under a project root that DOES hold the path, the root and
    the exact `cwd:` prefix that would reach it are named as well. Named, not
    used: auto-recovery (#363) already re-roots the unambiguous call, so every
    call that reaches here is one it declined — ambiguous by construction.
    Resolving it here would trade a loud wrong path for a quiet wrong root,
    which is the defect this tracker is about, not the fix.

    `suggest`, when given, replaces the generic `wrong CWD?` hint (#734).
    The cwd-drift explanation is a real default, but a caller that already
    knows a *specific*, more plausible mistake — e.g. `around` recognising
    its PATH argument as an all-digit token that looks like the LINE its
    sibling `around_line` wants — should say that instead of pointing at the
    one thing that provably did not cause this failure.

    `op` asks this helper to derive that suggestion itself, from the two
    shapes known to produce a joined-up filename: a comma list (#921) and a
    whitespace-separated path list (#1261). Derived here rather than at each
    call site because six ops reach this function and only `grep` had ever
    been wired to the first of the two — `read`, `around`, `around_line`,
    `map` and `replace`, the other five, answered both with `wrong CWD?`,
    the one cause that provably did not apply. `call_prefix` is the op call
    up to but excluding the path, used to print the batched repair; omit it
    where the path is not the op's last argument, since the printed call
    would then be one nobody can run.
    """
    if not path:
        return f"ERROR: {label} not found: {path}\n"
    if not suggest and op:
        suggest = (_comma_path_list_suggest(op, path)
                   or _multi_path_suggest(op, path, call_prefix)
                   or None)
    tried = os.path.abspath(os.path.expanduser(path))
    lines = [
        f"ERROR: {label} not found: {path}",
        f"  tried: {tried} (cwd: {os.getcwd()})",
    ]
    root = None
    if not os.path.isabs(path):
        try:
            root = _project_root_above_cwd()
        except OSError:
            root = None
    if root and os.path.exists(os.path.join(root, path)):
        lines.append(
            f"  exists at {os.path.join(root, path)} — prefix the call with "
            f"'cwd:{root}' to run it from the project root"
        )
    elif suggest:
        lines.append(f"  {suggest}")
    else:
        lines.append(
            "  wrong CWD? Prefix the call with cwd:PATH to run it from "
            "elsewhere."
        )
    return "\n".join(lines) + "\n"


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
    # `\Z` with DOTALL, not `$`. Two effects, and the second is the wider one.
    # `$` matches before a final newline, so `FOO='bar<LF>' cmd` set FOO to
    # "bar" and dropped the newline the caller quoted on purpose — the
    # assignment still matched, so nothing said so. And without DOTALL a value
    # with a newline *inside* it matched nothing at all, so the whole token
    # fell through as argv[0] and the env was never set; POSIX sets it. Both
    # are the same character deciding where a value ends (#1188).
    # anchored-ok: DOTALL is the point. `(.*)` is meant to reach the true end
    # and keep a newline the caller quoted, so the `\Z` is not the #1241 no-op
    # it reads as -- it states the intent the flag already carries.
    _kv = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z", re.DOTALL)
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


# Git's repo pointers. Any of these, set anywhere in the parent environment,
# overrides discovery-from-cwd and points a git command at the repository they
# name instead — so an op run from repoB acts on repoA (#692). Proven: a
# `git-commit` with cwd=repoB and GIT_DIR=repoA/.git wrote the commit into
# repoA, and the receipt named no repository at all.
#
# Not hypothetical, and not something the caller has to do to themselves: git
# exports GIT_DIR to every hook it runs, and `.githooks/pre-commit` invokes
# `./supertool 'git-diff:staged'`. This repo hands supertool a leaked git
# environment as a matter of routine.
#
# #416 learned this once and fixed it for the TEST RUNNER (`.githooks/pre-push`
# unsets them before pytest; `tests/conftest.py` again before every test). The
# ops never got the same treatment. This is the same list, kept in one place —
# conftest imports it from here, and a test pins the hook's `unset` line to it,
# because three copies of one lesson is how the ops came to be missed.
#
# Membership rule: does the variable change WHICH repository, index, or refs a
# git command reads or writes? GIT_COMMON_DIR and GIT_NAMESPACE do and were not
# in #416's five — the first redirects config and refs for a worktree, the
# second redirects every ref a push writes. GIT_CEILING_DIRECTORIES and
# GIT_DISCOVERY_ACROSS_FILESYSTEM do not: they only restrict discovery, so the
# worst they cause is finding no repo rather than the wrong one, and people set
# them deliberately on slow network mounts. Scrubbing those would make
# supertool disagree with the user's own shell about where they are.
GIT_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
)

# What `_main` removed from `os.environ` on the way in, so the notice can name
# it after the cwd has settled — the scrub happens before argv is parsed, the
# cwd it acted under is only known after `cwd:`/auto-root.
#
# Per-run scratch, filled in place rather than rebound: a daemon reuses the
# process, and a leak list left over from an earlier call would print a notice
# on a later clean one — #680's lesson about `_SKIP_COUNT`, same shape (#714).
_LEAKED_GIT_ENV: List[str] = []


def scrub_git_env(env: MutableMapping[str, str]) -> List[str]:
    """Delete git's repo pointers from `env`; return the names removed.

    `env` is `os.environ` itself at the one call site (#714), not a copy: a
    `del` there unsets the variable for this process AND for every child it
    spawns, which is what makes the guard total. Typed as a MutableMapping
    rather than a Dict because `os._Environ` is not a dict.
    """
    removed = [name for name in GIT_ENV_VARS if name in env]
    for name in removed:
        del env[name]
    return removed


def _git_env_notice(removed: List[str]) -> str:
    """One line naming what was scrubbed, or "" when nothing was.

    Scrubbed rather than refused: refusing would break the caller this repo
    creates itself — `.githooks/pre-commit` runs `git-diff:staged` under git's
    exported GIT_DIR, and a commit hook that aborts because supertool declined
    is a worse outcome than one that reads the right repo. But scrubbed
    LOUDLY. A silent scrub makes the tool ignore something the caller may have
    set on purpose and say nothing about it — the same "quiet where a loud
    answer belonged" that made the original bug invisible, just pointing the
    other way. The line costs one row of output and is the only thing that
    tells a caller their environment is leaking.

    Said once per call rather than once per op (#714). The scrub is now a
    property of the process, so repeating the line under each of six reads
    would describe one event six times.
    """
    if not removed:
        return ""
    return (
        f"scrubbed inherited git env: {', '.join(removed)} — this call acted "
        f"on the repo at {os.getcwd()}, not the one those variables named "
        f"(#692, #714)\n"
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
    # Cleared before anything can return, so a frame whose op declined without
    # running never inherits the previous frame's verdict. `None` is the third
    # state and it is load-bearing: "did not run" must not read as "failed",
    # and it must certainly not read as "succeeded".
    _CUSTOM_OP_OK[0] = None

    # #678 — this op is defined by whatever tree the cwd resolved to, which is
    # not necessarily the tree this core came from. Decide before running it:
    # once the subprocess has answered, its output is indistinguishable from
    # the right one.
    _mixed = _mixed_tree_pair()
    if _mixed is not None and not _mixed_tree_allowed():
        _SKIP_COUNT[0] += 1
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

    # #1287 — the universal path chokepoint for preset and custom ops. Builtins
    # have had `_PATH_ARG_POSITIONS` in `_dispatch_impl` since #146; no preset
    # op was ever in it, so a preset op with a path argument enforced
    # containment itself or not at all, and "not at all" was the default for a
    # newly written one. #1283 was one instance of that, not the class.
    #
    # Above the subprocess, not inside it: an out-of-boundary read has already
    # happened by the time any of the op's output could be filtered.
    # Not counted as a skip. `_mixed_tree_decline` above is a skip because it
    # is one — the op could not be run safely and nobody was refused anything.
    # This is the builtin containment gate's twin, which returns its ERROR and
    # touches no counter, and the whole argument of #1287 is that an unchecked
    # path is a refusal rather than a check that could not run.
    _paths = _preset_path_containment(op, entry, parts)
    if _paths:
        return _paths

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
    # No scrub here any more (#714). #692 put one on this line because every
    # PRESET op is launched from it — true, and the reasoning holds, but this
    # is the launcher for half the op table. Built-ins never reach it, and core
    # spawns git in six of its own functions. `os.environ` is scrubbed once in
    # `_main` instead, so this copy is already clean and a preset stays covered
    # by being launched rather than by opting in.
    env = dict(os.environ)
    # Which separator produced the argv this op is about to receive (#946).
    # A preset that reconstructs the caller's input — git-commit's spilled
    # message refusal is the one that does — cannot otherwise tell ':::' from
    # ':' from a payload whose fields were never split, and rejoining on the
    # wrong one hands back a suggestion that silently rewrites the message.
    env["SUPERTOOL_ARG_SEP"] = _ARG_SEP[0]
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
        elapsed = _elapsed_since(t0)
        output = result.stdout
        _CUSTOM_OP_OK[0] = result.returncode == 0
        if result.returncode != 0:
            if result.stderr:
                output += result.stderr
            return f"FAIL ({elapsed:.2f}s)\n{output}"
        # A deliberate mix still never prints a bare PASS — the verdict line
        # carries which two trees produced it (#678).
        _stamp = f" [{_mixed_tree_note(_mixed)}]" if _mixed is not None else ""
        return f"PASS ({elapsed:.2f}s){_stamp}\n{output}{_maybe_restart_mcp(entry)}"
    except subprocess.TimeoutExpired as e:
        # Not `_elapsed_since`: the freeze it applies zeroes the one number
        # this line exists to report (#727).
        return (f"{_timeout_verdict_line(t0, timeout)}\n"
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

    A stop that failed is reported as failed rather than counted as a restart
    (#547). This is the one caller that already asserts an outcome out loud, so
    the honest version costs no new noise — only the false claim goes away. A
    daemon that would not die keeps answering from the index it captured before
    the op cleared the state, which is exactly what `restartMcp` exists to
    prevent, so the reader needs to know it did not happen.
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
    restarted, failed = [], []
    for name in known:
        outcome = _mcp_stop_server(name)
        (restarted if outcome.ok else failed).append(name)
    note = ""
    if restarted:
        note += f"mcp: restarted {len(restarted)} daemon(s) ({', '.join(restarted)})\n"
    if failed:
        note += (f"mcp: FAILED to stop {len(failed)} daemon(s) ({', '.join(failed)})"
                 f" — they may still answer from a stale index"
                 f" (SUPERTOOL_DEBUG=1 for the reason)\n")
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
                grep_filter: str = "", force_full: bool = False,
                range_form: bool = False) -> str:
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
    # A caller who names no LIMIT alongside `grep=` is asking about the file,
    # not about the file's first `read.max_lines` lines (#1052). Recorded here,
    # before the default lands, because afterwards the two are indistinguishable.
    filter_scan_all = bool(grep_filter) and limit <= 0
    if limit <= 0:
        limit = _get_op_int("read", "max_lines", MAX_READ_LINES)
    if not path or not os.path.isfile(path):
        return _path_not_found(path, label="file", op="read",
                               call_prefix="read")

    # RTK delegation — simple reads without offset/filter/limit changes
    if not grep_filter and offset == 0 and limit == _get_op_int("read", "max_lines", MAX_READ_LINES) and _rtk_enabled() and _has_rtk():
        rtk_args = ["read", "-n", "--max-lines", str(_get_op_int("read", "max_lines", MAX_READ_LINES))]
        if _is_compact():
            rtk_args += ["--level", "aggressive"]
        rtk_args.append(path)
        rtk_out = _rtk_run(rtk_args)
        if rtk_out is not None:
            # rtk renders the body, but the line-numbering disclosure is
            # supertool's own contract and rtk knows nothing about it. A
            # delegated read that stays silent is the same silence #1060 is
            # about, one layer down.
            try:
                with open(path, "rb") as _probe:
                    _ambiguity = _line_break_ambiguity_note(_probe.read())
            except OSError:
                _ambiguity = ""
            return _ambiguity + rtk_out + "\n"

    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return f"ERROR: could not read {path}: {e}\n"
    # One definition of a line, shared with the ops that edit *by* line number
    # (#1060). `bytes.splitlines` already was this definition; going through the
    # helper is what stops the two sides drifting apart a second time.
    raw_lines = _split_lines_keepends(data)

    line_count = len(raw_lines)
    if filter_scan_all:
        # A filter is not a window. `read.max_lines` bounds how much is
        # *emitted*; applying it to how much is *searched* made the inline
        # filter answer `(no lines matching X)` about a file whose only match
        # sat at line 328 of 351 — a confident negative produced by the default
        # LIMIT, not by the file (#1052). The byte cap below still bounds the
        # output, and a filtered read emits only the lines that matched.
        limit = max(0, line_count - offset)
    out = [f"({line_count} lines, {size} bytes){_path_meta_suffix(path, b''.join(raw_lines[:64]))}\n"]
    _ambiguity = _line_break_ambiguity_note(data)
    if _ambiguity:
        out.append(_ambiguity)
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
    filter_literal_why = ""
    if grep_filter:
        try:
            filter_regex = re.compile(grep_filter)
        except re.error as e:
            # The fallback is right — an unusable regex should not fail a read.
            # The silence was not: the literal search's zero was rendered in the
            # same words as a real absence, so a rejected pattern read as a
            # missing string (#1052).
            filter_regex = re.compile(re.escape(grep_filter))
            filter_literal_why = str(e)

    compact = not filter_regex and _is_compact()
    matched_any = False
    # `printed` counts *emitted* lines; the two `continue`s below advance the
    # read without incrementing it. `offset + printed` is therefore neither
    # where reading stopped nor how much of the file is left, and every count
    # in this render that was derived from it named a line that was not the
    # last one shown (#945). `last_scanned` is the 1-based index of the last
    # line the loop actually looked at, which is the only honest answer to
    # both questions.
    last_scanned = offset
    capped = False
    for i in range(offset, end):
        last_scanned = i + 1
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
            capped = True
            break

    unsearched = 0
    # Collected rather than appended: every line in here is a note *about* the
    # scan, and it is inserted above the content at the end of this function.
    # See the insert below for why.
    filter_notes: List[str] = []
    if filter_regex:
        unsearched = line_count - (last_scanned - offset)
        if filter_literal_why:
            filter_notes.append(
                f"(the grep= pattern is not a usable regex — "
                f"{filter_literal_why}; searched for it as a literal "
                f"string instead)\n")
    if filter_regex and not matched_any:
        if unsearched > 0 and last_scanned <= offset:
            # The loop never ran, so `last_scanned` is still `offset` and the
            # range below would read `1001-1000` -- a span whose start is past
            # its end. The count was right and the range naming it was not, and
            # a disclosure that reads as nonsense is not read (PR #1057
            # review).
            filter_notes.append(
                f"(no lines matching {grep_filter!r} -- offset "
                f"{offset + 1} is past the end of this {line_count}-line "
                f"file, so no line was searched and this is not an answer "
                f"about the file)\n")
        elif unsearched > 0:
            # Three states, not two: found, not found, and did-not-look. The
            # old single line said the second when it meant the third (#1052).
            plural = "s" if unsearched != 1 else ""
            verb = "were" if unsearched != 1 else "was"
            filter_notes.append(
                f"(no lines matching {grep_filter!r} in lines "
                f"{offset + 1}-{last_scanned} of {line_count} — the other "
                f"{unsearched} line{plural} {verb} NOT searched, so this is "
                f"not an answer about the whole file)\n")
        else:
            filter_notes.append(
                f"(no lines matching {grep_filter!r} in any of "
                f"{line_count} lines)\n")
    elif filter_regex and capped:
        # The byte cap breaks the scan loop, not just the emission: a filtered
        # read that matched and *then* hit the cap has stopped looking. This
        # used to fall through to the generic `elif capped:` wording below,
        # which offers `(R more lines)` — a phrase a reader can only take as
        # "R lines that did not match". That is the same absence-read-as-
        # presence #1052 was filed to remove, left standing in the one case
        # where the file is large enough for it to cost something (PR #1057
        # review).
        plural = "s" if unsearched != 1 else ""
        verb = "were" if unsearched != 1 else "was"
        if unsearched > 0:
            filter_notes.append(
                f"(the grep= filter searched lines {offset + 1}-"
                f"{last_scanned} of {line_count} and stopped there — the "
                f"output reached the {byte_cap}-byte cap, so the other "
                f"{unsearched} line{plural} {verb} NOT searched and this is "
                f"not an answer about the whole file — continue with "
                f"read:PATH:{last_scanned}:LIMIT:grep=PATTERN)\n")
        else:
            filter_notes.append(
                f"(the grep= filter searched all {line_count} lines; the "
                f"output reached the {byte_cap}-byte cap on line "
                f"{last_scanned}, the last line of the file)\n")
    elif filter_regex and unsearched > 0:
        plural = "s" if unsearched != 1 else ""
        verb = "were" if unsearched != 1 else "was"
        filter_notes.append(
            f"(the grep= filter searched lines {offset + 1}-{last_scanned} "
            f"of {line_count} — {unsearched} line{plural} outside that range "
            f"{verb} NOT searched)\n")
    elif capped:
        remaining = line_count - last_scanned
        out.append(
            f"... (truncated at {_get_op_int('read', 'max_bytes', MAX_READ_BYTES)} bytes "
            f"— showed lines {offset + 1}-{last_scanned} of {line_count} "
            f"({remaining} more line{'s' if remaining != 1 else ''}) — "
            f"use read:PATH:OFFSET:LIMIT to get more)\n"
        )
    elif not filter_regex and last_scanned < line_count:
        out.append(f"... ({line_count - last_scanned} more lines)\n")
    elif not filter_regex and offset > 0:
        # `[complete file — no more lines]` after a windowed read was a plain
        # falsehood: lines 1-OFFSET were never emitted, and when OFFSET sits
        # past EOF *nothing* was emitted and the render still said the whole
        # file had been shown (#945). The empty case is left to the window
        # note, which is the only line that can honestly describe it.
        if printed:
            out.append(f"[end of file — lines 1-{offset} not shown]\n")
    elif not filter_regex:
        out.append("[complete file — no more lines]\n")
    if filter_notes:
        # Header position, above the content, for #955's reason rather than by
        # analogy with it: "Construction order is not render order, and a note
        # that arrives after the wrong window has already been paid for is
        # barely a note." That is an argument about what the reader has spent
        # by the time the correction reaches them, and it was made about this
        # same `out` list in this same function. A filtered read that emits
        # 20 KB of matches and only then admits 308 lines were never searched
        # charges for the wrong answer first and corrects it afterwards
        # (PR #1057 review).
        for note in reversed(filter_notes):
            out.insert(1, note)
    if offset > 0:
        # Inserted at index 1 — after the count header, before the first line
        # of content. Construction order is not render order, and a correction
        # the caller reads *after* paying for the wrong window is not a
        # disclosure (#945).
        out.insert(1, _read_window_note(
            path, limit if apply_byte_cap else max(0, line_count - offset),
            offset, line_count, printed,
            last_scanned=last_scanned, capped=capped,
            byte_cap=byte_cap if apply_byte_cap else 0,
            limit_synthetic=not apply_byte_cap,
            range_form=range_form,
            skipped_by=("the grep= filter" if filter_regex
                        else "compact mode" if compact else "")))
    out.append("\n")
    return "".join(out)


def _read_window_note(path: str, limit: int, offset: int,
                      line_count: int, shown: int, last_scanned: int = 0,
                      capped: bool = False, byte_cap: int = 0,
                      limit_synthetic: bool = False,
                      range_form: bool = False,
                      skipped_by: str = "") -> str:
    """One line naming the window the caller asked for and the window actually
    returned, emitted for every `read` with a non-zero OFFSET (#945).

    OFFSET is a *skip count*, so `read:f:19:1` renders line 20 and line 19 is
    absent from the output entirely. Nothing in the old render distinguished
    "here is line 19" from "here is a line near 19", so a caller quoting the
    result into a brief or an issue quoted the wrong line with full confidence.

    The window is disclosed rather than the semantics changed: `read:PATH:A-B`
    already spells 1-based inclusive addressing, and silently re-basing OFFSET
    would break every caller who had it right — trading a visible wrong answer
    for an invisible one.

    The `read:PATH:A-B` suggestion is withheld when LIMIT > OFFSET, because
    that is the shape `_read_range_note` (#382) already speaks to: it reads
    `A:B` as START:END and would name a different range. Two hints proposing
    two different ranges is worse than one.

    A window ends for one of three reasons and the note names which: the LIMIT
    was reached, the file ended, or the byte cap cut it short. The first
    version of this note computed the shortfall against the requested end and
    attributed all of it to EOF, so a 20KB truncation was announced as "the end
    of the file" at the top of a render whose own footer said 146 lines
    remained. When two reasons land on the same line the note says they
    coincide rather than picking the flattering one — an end it cannot
    attribute is declined, not guessed.
    """
    req_start = offset + 1
    req_end = offset + limit
    if last_scanned < offset:
        last_scanned = offset + shown
    # "offset N + limit M = lines A-B" rather than "requested lines A-B":
    # LIMIT is often the 300-line default the caller never typed, and calling
    # that a request would be its own small untruth.
    asked = f"offset {offset} + limit {limit} = lines {req_start}-{req_end}"
    if shown <= 0:
        if last_scanned > offset:
            skipped = last_scanned - offset
            by = f"{skipped_by} suppressed" if skipped_by else "nothing matched"
            return (f"window: {asked}; scanned lines {req_start}-"
                    f"{last_scanned} of {line_count} and emitted none — "
                    f"{by} all {skipped}\n")
        return (f"window: {asked}; returning nothing — the file has "
                f"{line_count} lines\n")
    hint = ""
    # `range_form` says the caller typed `read:PATH:A-B`, whose OFFSET this
    # function only ever sees after the range was converted to one. Telling
    # that caller "OFFSET is a skip count" corrects a form they did not use,
    # and names A-1..B-1 — off by one against the lines they asked for. A
    # correct call being told it was wrong is why the range form reads as
    # non-existent even though it shipped in 0.19.0 (#983).
    if not range_form and 0 < limit <= offset:
        hint = ("; OFFSET is a skip count, not a start line — for lines "
                f"{offset}-{offset + limit - 1} use "
                f"read:{path}:{offset}-{offset + limit - 1}")
    suppressed = (last_scanned - offset) - shown
    held = ""
    if suppressed > 0:
        held = (f", {shown} of those {last_scanned - offset} lines emitted "
                f"({skipped_by or 'a filter'} skipped {suppressed})")
    reasons = []
    if capped:
        reasons.append(f"cut short by the {byte_cap}-byte cap")
    if last_scanned >= line_count:
        reasons.append("the end of the file")
    if not limit_synthetic and limit > 0 and last_scanned >= req_end:
        reasons.append("the limit was reached")
    if not reasons:
        stops = f", stopping at line {last_scanned} for no reason this op can name"
    elif len(reasons) == 1:
        stops = f", stopping at line {last_scanned}: {reasons[0]}"
    else:
        stops = (f", stopping at line {last_scanned}: "
                 + " and ".join(reasons)
                 + " coincide here — which one ended the window cannot be told apart")
    return (f"window: {asked}; returning lines {req_start}-{last_scanned} "
            f"of {line_count}{held}{stops}{hint}\n")


_READ_RANGE_RE = re.compile(r"\d+-\d+")


def _read_range_note(path: str, offset: int, limit: int, body: str) -> str:
    """One-line nudge when `read:PATH:A:B` looks like a misread line range (#382).

    `:A:B` is OFFSET:LIMIT, but it reads like START:END to anyone who has used
    `sed -n 'A,Bp'`, and the overshoot is quiet — the output just looks long.
    Requires LIMIT > OFFSET throughout — a real limit is seldom larger than the
    point it starts from — plus one of two independent tells:

    * OFFSET+LIMIT runs past EOF (#382's original gate), or
    * LIMIT < 2*OFFSET (#1020). The filed call was `read:PATH:5370:5460` on a
      19571-line file: it does NOT overrun, so #382's note stayed silent on the
      exact shape it was written for. What gives it away instead is that 5460
      sits just past 5370 — an END line lands NEAR its START, while an
      independent LIMIT does not. The doubling threshold keeps #382's own
      counter-example (`read:PATH:10:20`, a legitimate skip-then-read) quiet.

    Disclosure rather than refusal, because both readings are legitimate here
    and there is no gate that separates them without breaking working calls.
    """
    if offset <= 0 or limit <= 0 or limit <= offset:
        return ""
    if body.startswith("ERROR:"):
        return ""
    total = _count_lines(path)
    if total <= 0:
        return ""
    if offset + limit <= total and limit >= 2 * offset:
        return ""
    # What was ASKED FOR, not what came back: the byte cap can truncate the
    # window, and #382's "read N lines" then states a number the call did not
    # produce — the same reporting-a-number-as-a-fact defect one level down.
    # The span is always true and carries the contrast better anyway.
    span = limit - offset + 1
    return (
        f"note: this asked for {limit} lines from offset {offset} — those "
        f"args are OFFSET:LIMIT, not START:END. For lines {offset}-{limit} "
        f"({span} lines), use read:{path}:{offset}-{limit}\n"
    )


def _abstract_lang(path: str) -> str:
    """tree-sitter language name for PATH's extension, or "" when the abstract
    read has no language table for it.

    The gate used to be `path.endswith(".php")` while `_TS_LANG_MAP` already
    covered eighteen extensions (#670) — so a TypeScript or Python user got the
    raw file, which is the thing they already had."""
    lang = _TS_LANG_MAP.get(os.path.splitext(path)[1].lower(), "")
    return "" if lang in _ABSTRACT_READ_SKIP_LANGS else lang


# Languages whose symbol map is not a stand-in for their source. A signature
# list substitutes for a function body; a heading list does not substitute for
# the prose underneath it, so `read:` on a markdown file returns the document
# (#887). `map:` still builds the heading tree — this gate is read-only.
_ABSTRACT_READ_SKIP_LANGS: FrozenSet[str] = frozenset({"markdown"})


def _abstract_map(path: str, lang: str, size_bytes: int) -> Tuple[str, str]:
    """Symbol map for PATH, or the reason there isn't a usable one.

    Returns `(map, "")` on success and `("", reason)` when the caller should
    fall back to raw source. Two ways to fail, and the caller states which:

    - **No symbols.** The parser ran and found no definitions (a data-only
      module), or no parser matched at all. Returning that empty map would
      read as "this file has no code" — the absence of an answer wearing the
      shape of one. See docs/validators.md, "Declining instead of guessing".
    - **No saving.** A map that is not smaller than the bytes this read would
      otherwise emit is a worse answer than the source. Measured on 263 real
      files across 16 languages this fires on ~4% of them, so it is rare but
      not theoretical.
    """
    body = op_map(path)
    if (body.startswith("ERROR:") or "(no symbols)" in body
            or _NO_PARSER_MARKER in body
            or "no supported files" in body):
        reason = f"no symbols found in {path} ({lang})"
        if not _has_tree_sitter():
            reason += " — tree-sitter is not installed, so only the regex tier ran"
        return "", reason
    map_bytes = len(body.encode("utf-8", errors="replace"))
    budget = min(size_bytes, _get_op_int("read", "max_bytes", MAX_READ_BYTES))
    if map_bytes >= budget:
        return "", (f"symbol map for {path} ({lang}) is {map_bytes} bytes, "
                    f"not smaller than the {budget} bytes this read emits")
    return body, ""


# ---------------------------------------------------------------------------
# #1329 — eliding a repeat read of a byte-identical file
# ---------------------------------------------------------------------------
# The op cannot observe whether the caller still holds the first copy. A
# re-read after a context compaction is the NORMAL case, not the edge case:
# the earlier result was evicted and the model is asking again precisely
# because it no longer has it. Nothing in this process can tell that apart
# from a redundant second ask, so the design does not try to. It bounds the
# damage instead, and every bound below is one of this repo's own rules:
#
#   - the elision is always ONE round-trip from the bytes, and the command
#     that returns them is printed in the line itself, not in the docs;
#   - it only fires inside a recency window measured from the last time
#     content was ACTUALLY returned — never bumped by an elision, or a file
#     polled every minute would be elided forever;
#   - a state file that cannot be read or written returns the content. An
#     unanswered cache is `skipped`, not silence;
#   - a file whose bytes changed is never elided, at any age. Repeat reads of
#     files that MOVED are the ones that carry information.
#
# Measured on the supertool corpus (`claude-log:cost`, #1252): unchanged
# re-reads are 0.0% of result bytes, because batching already prevents the
# pattern here. This is built for what it prevents, not for what it recovers.
_READ_ELIDE_KIND = "read-elide"
_READ_ELIDE_WINDOW_SECONDS = 900


def _read_elide_enabled() -> bool:
    """Whether a repeat read may be elided at all.

    Deliberately NOT routed through `_get_op_int`. That helper exists for
    positive-integer thresholds and reads a configured `0` as "unset",
    substituting its own default (`val if isinstance(val, int) and val > 0`).
    `read.elide` is the first boolean here whose default is ON, so through
    that helper `"elide": 0` was documented in three places and inert — a
    switch that reports the state it was asked for and does not enter it.
    Every other `_get_op_int` call in this file is a genuine threshold; this
    was the only site of the class.
    """
    off = os.environ.get("SUPERTOOL_READ_NO_ELIDE", "")
    if off.strip() and off.strip() != "0":
        return False
    ops = _load_config().get("builtin-ops", {})
    block = ops.get("read", {}) if isinstance(ops, dict) else {}
    val = block.get("elide") if isinstance(block, dict) else None
    if val is None:
        return True
    if isinstance(val, str):
        return val.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(val)


def _read_elide_window() -> float:
    return float(_get_op_int("read", "elide_window_seconds",
                             _READ_ELIDE_WINDOW_SECONDS))


def _read_elide_session_key() -> str:
    """Identity that two concurrent agents must never share.

    PPID is the session proxy `caller_tag` already uses — Claude Code does not
    expose session_id to Bash tools, only to hook stdin. Nine worktrees were
    live on this machine on 2026-08-11, so the resolved cwd is mixed in too:
    two agents that somehow share a parent still key separately per tree.
    The two failure directions are not symmetric — over-keying costs one file
    returned again, under-keying suppresses content the caller never saw — so
    the key is deliberately the narrower of the two.
    """
    # USER is POSIX; Windows sets USERNAME. Neither is load-bearing on its own
    # — PPID plus the resolved cwd already separate two agents — but a "?" for
    # every Windows caller would silently drop a component of the key.
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "?"
    return "|".join((user, str(os.getppid()),
                     os.path.realpath(os.getcwd())))


def _read_elide_state_path(file_path: str) -> str:
    """One sidecar per (session, file), so concurrent supertool processes never
    read-modify-write a shared index."""
    # sha256, unlike the two path-only cache-name hashes below, because this
    # key carries USER: CodeQL flags a weak hash over identity-bearing input
    # (alert 11 on #1331). The digest is only a filename either way, but a
    # standing alert on a shipped line is a cost paid by every later reader.
    key = f"{_read_elide_session_key()}|{os.path.realpath(file_path)}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return os.path.join(str(_cache_root() / _READ_ELIDE_KIND), digest)


def _read_elide_load(file_path: str) -> "Optional[Tuple[str, float, int]]":
    """(sha256, when content was last RETURNED, bytes) — or None for no record.

    OSError is deliberately not swallowed here: the caller has to be able to
    tell "no record" from "the cache could not answer", and both must end in
    the content being returned rather than in an elision.
    """
    with open(_read_elide_state_path(file_path), "r", encoding="utf-8") as fh:
        raw = fh.read().strip()
    parts = raw.split()
    if len(parts) != 3:
        return None
    try:
        return parts[0], float(parts[1]), int(parts[2])
    except ValueError:
        return None


def _read_elide_record(file_path: str, digest: str, size: int,
                       now: float) -> None:
    target = _read_elide_state_path(file_path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = f"{target}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(f"{digest} {now:.3f} {size}\n")
    os.replace(tmp, target)


def _read_elide_line(file_path: str, digest: str, size: int,
                     when: float) -> str:
    # "on disk", not "not returned": a >20KB file is byte-capped on the way
    # out (`apply_byte_cap = in_claude or not force_full`), so the file's size
    # and the bytes the first read actually handed over are not the same
    # number. Naming the file's size as the withheld amount would overstate it
    # on exactly the files where the cap bites.
    stamp = datetime.fromtimestamp(when).strftime("%H:%M:%S")
    return (f"[read elided — {file_path} is byte-identical to your read at "
            f"{stamp} (sha256 {digest[:12]}, {size:,} bytes on disk), so this "
            f"would return what you already have. If you no longer have it: "
            f"read:{file_path}:full]\n")


def _read_elide(path: str, offset: int, limit: int, grep_filter: str,
                force_full: bool, range_form: bool) -> str:
    """The elision line, or "" meaning "return the content".

    Records on every path that returns content, including `full` — after a
    forced read the caller demonstrably holds the bytes again.
    """
    if offset or limit or grep_filter or range_form:
        # A recorded whole-file read says nothing about a slice request, and
        # a slice must not arm an elision of the whole file.
        return ""
    if not _read_elide_enabled():
        return ""
    hasher = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                hasher.update(chunk)
                size += len(chunk)
    except OSError:
        # Missing, a directory, unreadable — render_file owns that message.
        # (Windows raises PermissionError where POSIX raises
        # IsADirectoryError; both are OSError, which is why this catches the
        # base class rather than either name.)
        return ""
    digest = hasher.hexdigest()
    now = time.time()
    try:
        prior = _read_elide_load(path)
    except OSError:
        prior = None
    if (prior is not None and not force_full and prior[0] == digest
            and 0 <= now - prior[1] <= _read_elide_window()):
        return _read_elide_line(path, digest, prior[2], prior[1])
    try:
        _read_elide_record(path, digest, size, now)
    except OSError:
        pass  # A cache that cannot be written never suppresses content.
    return ""


def op_read(path: str, offset: int = 0, limit: int = 0,
            grep_filter: str = "", force_full: bool = False,
            range_form: bool = False) -> str:
    # Abstract mode — when enabled, read:PATH on a file in a language
    # tree-sitter knows, with no offset/limit/grep, returns the symbol map
    # (measured 3-18% of the source bytes, median ~5%). Skipped when:
    #   - the extension is not in _TS_LANG_MAP (no map to build)
    #   - file size <= threshold (small files fit raw in the cap, abstract
    #     buys nothing)
    #   - caller passes :full / :raw (force_full)
    #   - explicit offset/limit/grep
    #   - the map would be empty or no smaller than the raw read — those two
    #     fall back to source *and say so*, never silently
    skip_note = ""
    if (offset == 0 and limit == 0 and not grep_filter and not force_full
            and (_get_op_int("read", "abstract", 0)
                 or _get_op_int("read", "php_abstract", 0))):
        lang = _abstract_lang(path)
        threshold = _get_op_int("read", "abstract_threshold_bytes",
                                _get_op_int("read", "max_bytes", MAX_READ_BYTES))
        try:
            size_bytes = os.path.getsize(path)
        except OSError:
            size_bytes = 0
        if lang and size_bytes > threshold:
            body, reason = _abstract_map(path, lang, size_bytes)
            if body:
                line_count = 0
                try:
                    with open(path, "rb") as f:
                        # B007: the binding is read *after* the loop, which is
                        # the one shape the rule cannot see.
                        for line_count, _ in enumerate(f, 1):  # noqa: B007
                            pass
                except OSError:
                    pass
                return (body
                        + f"\n[abstract read — {lang}, {line_count} lines, "
                          f"{size_bytes} bytes raw — "
                          f"use read:{path}:full for content "
                          f"or read:{path}:::grep=PATTERN to filter]\n")
            skip_note = (f"[abstract read skipped — {reason}; "
                         f"showing raw source]\n")
    # `grep=` with no LIMIT is left at 0 so render_file can tell "the caller
    # named no window" from "the caller asked for 300 lines". Applying the
    # default here made the two identical, and the filter then searched only
    # the first 300 lines while reporting its zero as a fact about the file
    # (#1052).
    elision = _read_elide(path, offset, limit, grep_filter, force_full,
                          range_form)
    if elision:
        return elision
    if limit <= 0 and not grep_filter:
        limit = _get_op_int("read", "max_lines", MAX_READ_LINES)
    body = render_file(path, offset, limit, grep_filter, force_full,
                       range_form)
    return skip_note + body + _read_edit_hint(path, body)


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


# Patterns whose meaning differs between Python's `re` and POSIX ERE (#987).
# The delegated path hands the pattern to the system grep, so anything matching
# this never leaves the native walker:
#   \X  — every escape EXCEPT the punctuation both dialects agree on
#         (`\. \$ \* \+ \? \( \) \[ \] \{ \} \| \\ \/ \-`). That covers Python-only
#         classes (\d \w \s \b \A) and backreferences, and also GNU's word
#         boundaries \< \>, which ERE honours and Python reads as `<` and `>`
#         — a divergence a `\<alnum>` rule silently let through.
#   (?  — lookaround, non-capturing groups, inline flags: Python only
#   *? +? ??  — non-greedy; ERE reads a second, stray quantifier
#   [: [. [=  — POSIX bracket classes, which Python reads literally
_ERE_UNSAFE = re.compile(r"\\[^.^$*+?()\[\]{}|\\/-]|\\$|\(\?|[*+?}]\?|\[[:.=]")


# #1120 — supertool rewrites bash-grep BRE alternation so `a\|b` behaves the way
# a caller's fingers expect. The rewrite is unconditional, and it cannot tell that
# `\| \{` was an ESCAPED LITERAL pipe: it produces `| \{`, whose left alternation
# branch is empty. An empty branch matches the empty string, so the pattern matches
# every line of every file — and `1000+ matches` renders identically to a search
# that genuinely found a lot. The `:`-tokenizer, the filed suspect, is innocent.
_BRE_ALT = "\\|"


def _bre_alternation_rewrite(pattern: str) -> Tuple[str, bool]:
    """Apply the BRE-alternation rewrite, reporting whether it changed anything."""
    if _BRE_ALT not in pattern:
        return pattern, False
    return pattern.replace(_BRE_ALT, "|"), True


def _top_level_branches(pattern: str) -> List[str]:
    """Split on `|` at nesting depth 0, outside character classes, unescaped.

    Depth matters: `colo(u|)r` has an empty branch and matches exactly two words,
    while a bare `colou|r` with an empty branch would match everything. A `|`
    inside `[...]` is an ordinary character and starts no alternation at all.
    """
    branches: List[str] = []
    depth = 0
    in_class = False
    start = 0
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif c == "|" and depth == 0:
            branches.append(pattern[start:i])
            start = i + 1
        i += 1
    branches.append(pattern[start:])
    return branches


# Probe strings for "does this branch match every line?" (#1314). A branch that
# matches the EMPTY string matches at position 0 of every line, so the whole
# alternation does — that is the property, and an empty branch is only its
# simplest spelling. `^`, `$`, `.*` and `z*` all have it and all sailed past the
# #1120 predicate, which tested for `b == ""`: the filed call
# `grep:^\|def op_:_supertool.py:5` reported 5 of "1000+ matches" for a pattern
# that matched every line of the file.
#
# The empty probe alone is not enough, and this is where the line is drawn.
# `^$` matches the empty string and NOT `x`, so `^$|alpha` is a real search
# ("blank lines or alpha") and refusing it would remove the op for a legitimate
# caller. A branch has to match every probe — including non-empty ones — before
# it is called saturating. Same reason `.` and `\b` stay out: neither matches a
# blank line, so neither makes the pattern match every line.
_SATURATION_PROBES = ("", "x", "supertool 42", "  ")


def _branch_matches_everything(branch: str) -> bool:
    """True when this one top-level branch matches every line there is."""
    if branch == "":
        return True
    try:
        regex = re.compile(branch)
    except re.error:
        # An uncompilable branch is not a saturation claim to make here; the
        # pattern's own compile (and its literal fallback) decides what happens.
        return False
    return all(regex.search(probe) is not None for probe in _SATURATION_PROBES)


def _saturating_branch(pattern: str) -> Optional[str]:
    """The first TOP-LEVEL alternation branch that matches every line, or None.

    Returns the branch rather than a bool because the refusal has to name it:
    `^|def op_` and `|def op_` are one keystroke apart and want different fixes,
    and a diagnosis that does not say which half saturated is not actionable.
    """
    if "|" not in pattern:
        return None
    branches = _top_level_branches(pattern)
    if len(branches) < 2:
        return None
    for branch in branches:
        if _branch_matches_everything(branch):
            return branch
    return None


def _saturates(pattern: str) -> bool:
    """True when a TOP-LEVEL alternation branch matches every line, i.e. the
    pattern matches every line. Not a search — a saturation."""
    return _saturating_branch(pattern) is not None


def _saturating_pattern_refusal(written: str, effective: str,
                                rewritten: bool) -> str:
    """Refuse a pattern that matches every line, naming the spelling that works.

    The three-state contract: `ok`, a finding, and — here — declining, because a
    saturated match is not an answer to the question that was asked and its
    report cannot be told apart from a real one. Nothing downstream can recover
    the distinction, so it has to be refused at the call.
    """
    branch = _saturating_branch(effective)
    if branch is None:
        return ""
    if branch == "":
        why = ("has an empty alternation branch, so it matches every line of "
               "every file scanned")
    else:
        why = (f"has an alternation branch `{branch}` that matches the empty "
               f"string, so the whole pattern matches every line of every "
               f"file scanned")
    # Backticks, not !r: repr() DOUBLES every backslash, and a backslash is the
    # one character this message exists to show. `'\\| \\{'` for a pattern the
    # caller typed as `\| \{` is the tool mangling its own diagnosis.
    lines = [
        f"ERROR: pattern `{effective}` {why}. That is a saturated pattern, not "
        f"a search, and its result count is indistinguishable from a search "
        f"that genuinely found a lot.",
    ]
    if rewritten:
        became = ("a bare `|` with nothing to its left" if branch == ""
                  else "a top-level `|` that split the pattern into branches")
        lines.append(
            f"  written as `{written}` — supertool rewrites bash-grep BRE "
            f"alternation, so `{_BRE_ALT}` became {became}.")
        lines.append(
            f"  for a literal `|`, use a character class: "
            f"`{written.replace(_BRE_ALT, '[|]')}`")
    else:
        lines.append(
            "  for a literal `|`, use a character class: `[|]`")
    return chr(10).join(lines) + chr(10)


def _bre_rewrite_note(written: str, effective: str, rewritten: bool) -> str:
    """Disclose the rewrite whenever it fired (#1120).

    An escape being eaten is invisible in a result set that looks plausible, and
    the same move — say which pattern actually ran — is what #1065 added for the
    ':' rejoin and what `scanned N files` added for the zero case.
    """
    if not rewritten:
        return ""
    return (f"(pattern rewritten to `{effective}` — `{_BRE_ALT}` is bash-grep "
            f"BRE alternation and became a plain `|`. For a literal `|`, use "
            f"`[|]`.)" + chr(10))


def _grep_pattern_note(pattern: str, path: str) -> str:
    """Name the pattern grep actually ran, when a ':' made that a choice (#1065).

    `grep:re:Checks|failed:PATH` tokenizes to the pattern `re:Checks|failed`,
    and `|` binds looser than concatenation, so the first alternation branch is
    the literal `re:Checks` — which matches nothing. The op then reports three
    confident results for a question nobody asked, and `scanned 1 files` is
    both true and useless: it did look, at someone else's pattern.

    Nothing is refused here, because the tokenization is not ambiguous — it is
    documented, deterministic, and the payload route already covers what the
    colon CLI cannot express. What was missing is the disclosure. A ':' in the
    pattern is exactly the condition under which the rejoin happened, so that
    is when the effective pattern is echoed back.

    The path is named too (#1166). The split has two outputs and the note
    disclosed one: a receipt that is precise about the pattern and silent about
    which of the remaining tokens became the FILE reads as complete, which is
    exactly what stops a reader checking the half that moved.
    """
    if ":" not in pattern:
        return ""
    note = (f"(pattern read as {pattern!r}, path as {path!r} — the ':' is part "
            "of the regex, not a separator. Use grep:@- with a `pattern` key "
            "if the split was meant to fall elsewhere.)" + chr(10))
    if pattern.startswith("re:"):
        note += ("(grep has no `re:` prefix — every grep pattern is already a "
                 "regex, so `re:` is literal text and forms part of the first "
                 "alternation branch. `between:re:START:END:PATH` is the op "
                 "that has one.)" + chr(10))
    return note


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


# #1328 — `all` names the LIMIT slot and only that slot. Both neighbours are
# refusals rather than readings: `all` in the CONTEXT slot read as a limit runs
# a call nobody typed, and a third trailing token dropped on the floor (the
# parser peels every trailing digit and reads two) runs the default under a
# token the caller believes removed the cap.
_GREP_ALL_OUTSIDE_LIMIT_SLOT = (
    "ERROR: grep read `all` outside the LIMIT slot (grep:PATTERN:PATH:LIMIT:"
    "CONTEXT). `all` is a LIMIT — it removes the result cap so a sweep is "
    "complete; CONTEXT is a number of lines around each match and has no "
    "`all`, and nothing follows CONTEXT. Did you mean "
    "grep:PATTERN:PATH:all:CONTEXT?" + chr(10)
)

# `grep_around` is PATTERN:PATH:N:LIMIT — context FIRST, the opposite order to
# grep's LIMIT:CONTEXT. Copying `grep:PATTERN:PATH:all` across lands `all` in
# the N slot, where int() used to raise and the caller got the exception text.
_GREP_AROUND_ALL_IN_N_SLOT = (
    "ERROR: grep_around takes PATTERN:PATH:N:LIMIT — context first, the "
    "opposite order to grep's LIMIT:CONTEXT — so `all` landed in the N slot. "
    "Did you mean grep_around:PATTERN:PATH:N:all (e.g. "
    "grep_around:PATTERN:PATH:3:all), or grep:PATTERN:PATH:all:N?" + chr(10)
)


# #945 — `0` is the near-universal spelling of "no limit", and grep accepted it,
# ignored it, and quietly applied the default instead. Neither meaning is
# guessed: honouring it as unlimited would hand a caller who typed one character
# an unbounded dump into a shared output budget, and substituting the default
# means the number that ran was never the number that was typed.
_GREP_ZERO_LIMIT = (
    'ERROR: grep LIMIT 0 is not "unlimited" here, and supertool will not guess '
    "which of the two it meant. Uncapped output would land in the caller's "
    "context before it could be declined, and silently substituting the default "
    "would mean the LIMIT that ran was never the LIMIT that was typed. Pass a "
    "positive LIMIT (grep:PATTERN:PATH:200) or omit it for the default." + chr(10)
)


def op_grep(pattern: str, path: str = ".", limit: int = 0,
            context: int = 0, count_only: bool = False,
            no_exclude: bool = False, no_auto_read: bool = False) -> str:
    """grep, prefixed by #1065's disclosure of the pattern that actually ran."""
    effective, rewritten = _bre_alternation_rewrite(pattern)
    refusal = _saturating_pattern_refusal(pattern, effective, rewritten)
    if refusal:
        return refusal
    return (_grep_pattern_note(pattern, path)
            + _bre_rewrite_note(pattern, effective, rewritten)
            + _op_grep(pattern, path, limit, context, count_only,
                       no_exclude, no_auto_read))


def _grep_limit_and_label(limit: int) -> Tuple[int, str]:
    """Resolve grep's LIMIT into (effective cap, what the count line says).

    `all` (#1328) arrives as the GREP_LIMIT_ALL sentinel and leaves as a cap
    nothing can reach, plus the label `all`. Printing the number instead would
    put `limit 9223372036854775807` on the line a reader uses to decide whether
    a sweep was complete — a cap they then have to recognise to interpret.
    """
    if limit == GREP_LIMIT_ALL:
        return sys.maxsize, _GREP_ALL_TOKEN
    if limit <= 0:
        limit = _get_op_int("grep", "max_results", MAX_GREP_RESULTS)
    return limit, str(limit)


def _op_grep(pattern: str, path: str = ".", limit: int = 0,
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
    unlimited = limit == GREP_LIMIT_ALL
    limit, limit_label = _grep_limit_and_label(limit)
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

    # Auto-convert bash grep BRE alternation (\|) to Python regex (|). Single-
    # sourced with op_grep/op_around so the rewrite and the refusal that guards
    # it (#1120) can never drift apart — two hand-written copies is how `around`
    # came to carry the same saturation bug that was filed against `grep`.
    pattern, _ = _bre_alternation_rewrite(pattern)

    # Early exit if path doesn't exist (don't silently return 0 results)
    if path != "." and not os.path.isfile(path) and not os.path.isdir(path):
        # Could be a glob pattern — check if it expands to anything
        from glob import glob as _glob
        if not _glob(path, recursive=True):
            return _path_not_found(path, op="grep",
                                   call_prefix=f"grep:{pattern}")

    excl = _get_exclude_paths("grep", no_exclude)

    # RTK delegation — basic grep (no context, no count). Excludes are threaded
    # through as --exclude-dir AND --exclude for single-segment entries (.git/,
    # node_modules/, .env/) and re-applied to whatever comes back. Multi-segment
    # prefixes (e.g. "Dvsi/dvsi-private/libs/") can't be expressed as either;
    # fall through to the native walker in that case.
    # `-E` and the _ERE_UNSAFE gate together (#987). rtk shells out to the
    # system grep, which without `-E` reads a POSIX *BRE*: `|`, `+`, `?`, `(`
    # and `{` are ordinary characters there. `cc|dd` therefore came back as the
    # one line containing a literal pipe — a smaller, confident, wrong answer,
    # and only when the BRE reading happened to match at all, which is why it
    # survived (a BRE that matches nothing exits non-zero and falls through to
    # the native walker, where the result is right). `-E` closes the gap for
    # alternation, groups and quantifiers; the gate declines delegation for the
    # constructs ERE still cannot express, rather than translating them.
    # `all` never delegates (#1328): the delegated report cannot count what it
    # did not collect, so its truncation clause is `total not counted` — the one
    # state a completeness sweep must not come back in. The native walker is the
    # only engine that can say `all` and mean it.
    if (not count_only and context == 0 and not unlimited
            and _rtk_enabled() and _has_rtk()
            and not _ERE_UNSAFE.search(pattern)):
        _, multi = _split_exclude_prefixes(excl)
        if not multi and not _gitignore_residual(path, excl):
            # limit + 1 so the report can tell "exactly N" from "stopped at N"
            # (#448). The extra line is trimmed off before output.
            rtk_args = ["grep", "-rn", "-E", "-m", str(limit + 1)]
            rtk_args.extend(_grep_exclude_flags(excl))
            rtk_args.extend([pattern, path])
            rtk_out = _rtk_run(rtk_args)
            if rtk_out is not None and rtk_out.strip():
                rtk_out, rtk_dropped = _rtk_drop_excluded(rtk_out, excl)
                if not rtk_dropped:
                    return _rtk_grep_report(rtk_out, limit)
                # An excluded file came back anyway — expected whenever the
                # list carries a negation, since those wildcards are withheld
                # from the argv. Printing the filtered lines under the
                # delegated header would leave its count, its `limit + 1`
                # truncation probe and its `?` denominator all describing a
                # result set that no longer exists. Redo the walk natively
                # instead: same filter, honest report, and the two engines
                # answer identically in the one case where it matters (#691).
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
    hidden_files: List[str] = []
    candidates = _grep_candidates(path, excl, hidden_files)
    scanned = len(candidates)
    hidden = _hidden_suffix(len(hidden_files))

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
               f"({total} total matches across {file_count} files{_scanned_suffix(scanned)}{hidden})\n"]
        if total == 0:
            out.append(_shim_facade_note(path))
        # `PATH:N` is the shape every grep-like tool uses for PATH:LINE, so a
        # count of 30 read as "one match, at line 30" — the opposite of what
        # the op said, in the op you call *before* deciding whether to look
        # (#988). The unit makes the two unconfusable.
        for fp, cnt in sorted(counts.items()):
            out.append(f"{_fwd(fp)}: {cnt} match{'' if cnt == 1 else 'es'}" + chr(10))
        out.append("\n")
        return "".join(out)

    if context > 0:
        ceiling = _grep_count_ceiling(limit)
        groups = _grep_recursive_context(
            pattern, path, ceiling + 1, context, excl, candidates=candidates)
        literal = False
        if not groups and _is_regexy(pattern):
            groups = _grep_recursive_context(
                re.escape(pattern), path, ceiling + 1, context, excl, candidates=candidates)
            literal = bool(groups)
        total, capped = _grep_total(
            sum(1 for g in groups for line in g if line[2] == "match"), ceiling)
        groups, truncated = _trim_context_groups(groups, limit)
        count = sum(
            1 for g in groups for line in g if line[2] == "match"
        )
        literal_note = _literal_note(pattern, count) if literal else ""
        file_count = len({g[0][0] for g in groups if g})
        out = [literal_note, f"({count} results in {file_count} files{_scanned_suffix(scanned)}{hidden}, "
               f"limit {limit_label}, context {context}"
               f"{_truncation_suffix(truncated, total, capped)})\n"]
        if count == 0:
            out.append(_shim_facade_note(path))
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

    # ceiling + 1 (#448, #1073): a count that equals the limit is ambiguous
    # between "exactly N matches" and "stopped at N", and only looking past the
    # cap settles it. #448 looked exactly one past, which settled the yes/no and
    # left the scope unknown; the bound is the counting ceiling now, so the same
    # walk answers "how many" as well as "were there more".
    #
    # The walk itself is still already paid for — `candidates` above traversed
    # the whole tree to produce `scanned` — so this is never a second walk. What
    # it costs is reading file *contents* further: to the (limit+1)th match
    # before, to the (ceiling+1)th now. Measured on a 67,855-file tree, dense
    # pattern, limit 20: 0.0103s then, 0.05s now, against 10.3s for counting
    # everything and 4.3s for the traversal both of them sit on top of. On an
    # exact result neither stops early at all, which is what proving exactness
    # has always meant here.
    ceiling = _grep_count_ceiling(limit)
    hits = _grep_recursive(pattern, path, ceiling + 1, excl, candidates=candidates)
    literal = False
    if not hits and _is_regexy(pattern):
        hits = _grep_recursive(re.escape(pattern), path, ceiling + 1, excl, candidates=candidates)
        literal = bool(hits)
    truncated = len(hits) > limit
    total, capped = _grep_total(len(hits), ceiling)
    hits = hits[:limit]
    count = len(hits)
    literal_note = _literal_note(pattern, count) if literal else ""
    file_count = len({fp for fp, _, _ in hits})

    out = [literal_note,
           f"({count} results in {file_count} files{_scanned_suffix(scanned)}{hidden}, "
           f"limit {limit_label}{_truncation_suffix(truncated, total, capped)})\n"]
    if count == 0:
        out.append(_shim_facade_note(path))
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
    """around, guarded and disclosed exactly as op_grep is (#1120).

    Split from the body for the same reason op_grep is: the refusal and the
    rewrite disclosure belong to every caller of the op, and leaving `around`
    with only the refusal reproduced the original asymmetry one level down —
    a rewrite that merely changes the pattern was still invisible here.
    """
    effective, rewritten = _bre_alternation_rewrite(pattern)
    refusal = _saturating_pattern_refusal(pattern, effective, rewritten)
    if refusal:
        return refusal
    return (_bre_rewrite_note(pattern, effective, rewritten)
            + _op_around(pattern, path, n))


def _op_around(pattern: str, path: str, n: int = 10) -> str:
    """Show N lines before and after the first match of PATTERN in PATH.

    PATH can be a file (first match in that file) or a directory (first
    match per file, skipping files with no match, capped at
    _AROUND_DIR_MAX_FILES). Hidden and heavy dirs (.git, node_modules,
    vendor, …) are skipped during dir walk.
    """
    if not pattern:
        return "ERROR: empty pattern\n"
    # The rewrite is applied here as well as in op_around so a direct call to
    # the private body still runs the pattern the caller meant; the helper is
    # idempotent, and the refusal/disclosure live in the public wrapper.
    pattern, _ = _bre_alternation_rewrite(pattern)
    if not path:
        return "ERROR: empty path\n"

    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(re.escape(pattern))

    if not os.path.isdir(path) and not os.path.isfile(path):
        # #734: `around` is PATTERN:PATH[:N] and its sibling `around_line`
        # is PATH:LINE[:N] — same op-name family, opposite argument order.
        # Swap them and PATH resolves to a line number, which is never a
        # real path but IS a plausible typo. `wrong CWD?` cannot be right
        # here (the cwd was fine), so name the actual mistake instead —
        # never redirect the call itself, only the advice.
        suggest = None
        if path.isdigit():
            suggest = (
                "`around` takes PATTERN:PATH[:N] — "
                f"'{path}' was read as the path. Did you mean: "
                f"around_line:{pattern}:{path}[:N]"
            )
        return _path_not_found(path, label="file", suggest=suggest,
                               op="around")

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
            return (f"(no match for {pattern!r} in {path})\n"
                    + _shim_facade_note(path) + "\n"), False
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
    if found is None and lang_name in _TS_GRAMMAR_FAILED:
        return (f"ERROR: tree-sitter grammar for {ext!r} failed to load "
                f"({_TS_GRAMMAR_FAILED[lang_name]}) - cannot search for "
                f"symbols. Use 'between:re:START:END:PATH' for regex line "
                f"slicing.\n")
    # Retry with modifiers/parens stripped so a signature pasted from source
    # resolves like the bare name would (#363). Exact match still wins.
    normalized = _normalize_symbol_query(symbol)
    if found is None and normalized != symbol:
        found = _ts_find_node(path, lang_name, normalized)
        if found is not None:
            symbol = normalized
    if found is None:
        extra = "" if normalized == symbol else f" (also tried {normalized!r})"
        return (f"ERROR: symbol {symbol!r} not found in {path}{extra}\n"
                + _shim_facade_note(path))
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
        return (f"ERROR: start pattern {start!r} not matched in {path}\n"
                + _shim_facade_note(path))

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
    hidden_files: List[str] = []
    files = _glob_files(pattern, excl, over_fetch=1, hidden=hidden_files)
    # glob is repo-root relative, so a pattern naming a mid-path segment
    # (`SiBrief/**/*.php` for a dir nested under Dvsi/src2/) returns 0 while the
    # same segment works fine in grep. Retry once with a `**/` prefix so both
    # ops accept the same mental model (#363).
    midpath_note = ""
    if (not files and "/" in pattern
            and not pattern.startswith(("/", "~", "**", "./", "../"))):
        retry = "**/" + pattern
        hidden_files = []
        files = _glob_files(retry, excl, over_fetch=1, hidden=hidden_files)
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
    out = [midpath_note,
           f"({len(files)} files{_hidden_suffix(len(hidden_files))}{truncation})\n"]
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


# git's way of saying "nothing here is version-controlled" — an answer, and
# the common one for a file outside any checkout. Every other non-zero exit
# (a held index lock, a dubious-ownership refusal, a corrupt object store) is
# a lookup that did not happen, and renders as PATH_META_UNKNOWN.
_NOT_A_REPO = "not a git repository"

# The third state of the working-tree marker (#705). `?`, `!` and `m` are
# answers; their joint absence used to mean both "this file matches the index"
# and "the lookup failed", which inverts the marker's whole job on the failure
# leg — a modified file reading as clean, on every `read`.
#
# It is a token rather than a punctuation mark on purpose. The field looks
# one character wide because its busiest members are, but it is a
# space-separated token list whose members already run to `non-utf8`, `crlf`
# and `->target broken`, so nothing was costing a character. A punctuation
# mark would have had to be one the reader has no meaning for yet — and every
# free character is free precisely because it says nothing, which is the
# wrong property for the token that has to say the most. `git?` names the
# check that declined and cannot be confused with the three answers it
# replaces, none of which mention git.
PATH_META_UNKNOWN = "git?"


# One `git status` answer per repo root, reused across the paths rendered in a
# single process (#1126). The filed shape was a memo keyed by path; that cannot
# help, because the case it exists for — a batched `read` of seven files — asks
# about seven *different* paths and every lookup would miss. What repeats is the
# spawn, not the question, so this coalesces the query instead of remembering
# the answer: 6 paths cost 2 spawns rather than 6.
#
# Values, three states rather than two, so "we did not look" stays distinct from
# "we looked and it was clean" (docs/validators.md, "Declining instead of
# guessing"):
#
#   "primed"    one per-path query has been paid here; the next path escalates.
#               A lone `read` — the overwhelmingly common call — therefore costs
#               exactly what it always did, with no bulk query bolted on.
#   "declined"  the repo-wide query timed out or failed. Stay on the per-path
#               route forever in this process rather than reading its silence
#               as a clean tree. A repo with a very large ignored subtree is the
#               expected way to land here.
#   dict        {"codes": …, "taken_ns": …} — servable.
#
# Three things invalidate it, and they are the whole answer to "what is the
# correct lifetime":
#
#   1. `_atomic_write` clears it — every mutating op passes through there, so an
#      `edit` between two `read`s cannot be answered from the older snapshot.
#   2. `dispatch` clears it after any op outside `_PARALLEL_SAFE_OPS` — that is
#      what catches an index change from a preset (`git-commit`), which moves no
#      file mtime and so is invisible to (3).
#   3. A path whose mtime is at or after the snapshot instant is not served from
#      it. This is the one that covers a writer outside supertool entirely.
#
# Residual: a file rewritten by another process within the same filesystem mtime
# tick as the snapshot. Accepted knowingly, and it is a marker one call stale,
# not a marker computed against the wrong repository.
_PATH_META_BULK: Dict[str, Any] = {}


def _path_meta_bulk_drop() -> None:
    """Invalidate every snapshot, keeping the `declined` verdicts.

    A snapshot describes a tree at an instant, so a write invalidates it. A
    `declined` does not describe the tree at all — it records that this repo's
    status query does not come back inside the budget, which is a property of
    the repository (a very large ignored subtree, most likely) and is just as
    true after the write as before it. Clearing it wholesale meant the second
    path after every single edit re-paid the full 2s timeout to rediscover the
    same fact, turning a one-off cost into a per-edit one.
    """
    for key in [k for k, v in _PATH_META_BULK.items() if v != "declined"]:
        del _PATH_META_BULK[key]


def _path_meta_bulk_fill(root: str) -> Optional[Dict[str, Any]]:
    """One repo-wide `git status`, parsed into {relpath: XY}. None = declined.

    `-z` rather than the quoting `--porcelain` default: with NUL separators git
    emits path bytes verbatim, so a filename with a quote, a newline or a
    non-UTF-8 byte survives instead of arriving backslash-escaped and failing to
    match the path we were asked about.

    `taken_ns` is sampled *before* the spawn, so a file written while git was
    still walking the tree compares as newer than the snapshot and is refused by
    the caller's mtime check rather than answered from it.
    """
    taken_ns = time.time_ns()
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain", "-z", "--ignored=matching"],
            capture_output=True, timeout=2, cwd=root,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    codes: Dict[str, str] = {}
    fields = r.stdout.split(b"\x00")
    i = 0
    while i < len(fields):
        record = fields[i]
        i += 1
        if len(record) < 4:
            continue
        xy = record[:2].decode("ascii", errors="replace")
        name = record[3:].decode("utf-8", errors="surrogateescape")
        if xy[:1] in ("R", "C"):
            # A rename or copy is two fields: the new name, then the original.
            i += 1
        codes[name.rstrip("/")] = xy
    return {"codes": codes, "taken_ns": taken_ns}


_PATH_META_ROOT_CACHE: Dict[str, str] = {}


def _path_meta_repo_root(path: str) -> str:
    """Repo root for `path`, walking the path AS WRITTEN — links unresolved.

    Deliberately not `_dirs_up_to_repo_root`, which calls `os.path.realpath`
    first. That is correct for the formatter/validator machinery it was built
    for, where the question is which config governs the real file. It is the
    wrong question here, and measurably so: a symlink `link.txt` inside repo A
    pointing at a file in repo B resolved to **B's root**, so the marker beside
    a file in A was computed from a completely different repository's status.

    The per-path query this coalesces runs with `cwd=os.path.dirname(
    os.path.abspath(path))`, so climbing from that same directory is what keeps
    the two routes answering about the same repository.
    """
    start = os.path.dirname(os.path.abspath(path)) or os.sep
    cached = _PATH_META_ROOT_CACHE.get(start)
    if cached is not None:
        return cached
    current = start
    root = ""
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            root = current
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    _PATH_META_ROOT_CACHE[start] = root
    return root


def _path_meta_bulk_code(codes: Dict[str, str], rel: str) -> str:
    """This path's status letters from a repo-wide snapshot, or "" for clean.

    Ancestors are consulted because a repo-wide `git status` collapses whole
    directories: an ignore rule of `build/` yields one `!! build/` record and
    nothing for the files under it, and an untracked directory collapses the
    same way. Asked per-path, git names the file itself — so without this walk
    a `read` of an ignored file would lose its `!` marker the moment the query
    was coalesced, which is a cheaper answer that is not the same answer.
    """
    code = codes.get(rel)
    if code:
        return code
    parts = rel.split("/")
    for depth in range(len(parts) - 1, 0, -1):
        code = codes.get("/".join(parts[:depth]))
        if code in ("!!", "??"):
            return code
    return ""


def _path_meta_suffix(path: str, sample: bytes = b"") -> str:
    """Compact suffix for read/workspace meta line. Empty when nothing notable.
    Tokens: ->target [broken] | bin | non-utf8 | ? | ! | m | x | crlf | Nd|Nw|Nmo
            | git? (the working-tree lookup declined — state unknown, not clean)
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
    mtime_ns = None
    try:
        st = os.lstat(path)
        mtime_ns = st.st_mtime_ns
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

    # Answer from this process's repo-wide snapshot when there is one and it is
    # old enough to speak for this file (#1126). `code` stays None until
    # something has actually looked, so the per-path spawn below is skipped only
    # on a real answer and never on a missing one.
    code = None
    absolute = os.path.abspath(path)
    root = _path_meta_repo_root(path)
    # A repo-wide status keys every record by the path as it sits in the tree.
    # If any component of this path is a link, the name we would look up is not
    # the name git recorded, the lookup misses, and a miss is indistinguishable
    # from clean — a modified or untracked file rendering as if it were neither.
    # One `realpath` compare is cheaper than being wrong, and the per-path query
    # below handles links correctly today.
    through_a_link = os.path.realpath(path) != absolute
    if root and not through_a_link:
        entry = _PATH_META_BULK.get(root)
        if entry is None:
            _PATH_META_BULK[root] = "primed"
        elif entry == "primed":
            filled = _path_meta_bulk_fill(root)
            entry = filled if filled is not None else "declined"
            _PATH_META_BULK[root] = entry
        servable = (
            isinstance(entry, dict)
            and mtime_ns is not None
            and mtime_ns < entry["taken_ns"]
        )
        if servable:
            try:
                rel = os.path.relpath(absolute, root)
            except ValueError:
                # Different drives on Windows. The walk said this path is under
                # `root`, so this should not happen; if it does, re-ask git
                # rather than invent a relative path.
                rel = ""
            if rel and not rel.startswith(os.pardir):
                code = _path_meta_bulk_code(entry["codes"], rel.replace(os.sep, "/"))

    if code is None:
        try:
            # The pathspec is the bare filename, not `path`: this runs with a
            # cwd of the file's own directory, so a path written relative with a
            # directory component in it (`sub/s.txt`, the form the CLI hands
            # over) was resolved a second time against that directory. git
            # warned on stderr, exited 0 with empty stdout, and the marker
            # silently vanished while the bulk arm answered correctly for the
            # same file — #1186. The cwd itself stays, because it is what keeps
            # this route and `_path_meta_repo_root` talking about the same
            # repository when a path crosses a repo boundary.
            #
            # `:(literal)` because a filename is not a pattern: a clean
            # `t[a].txt` globbed onto its modified sibling `ta.txt` and reported
            # that file's ` m` as its own. The bulk arm looks the name up in a
            # dict, so it was already literal — this is the same two-answers
            # divergence, in the direction that invents a marker rather than
            # losing one. The magic prefix and not the `--literal-pathspecs`
            # flag: that one has to precede the subcommand, and the shims the
            # decline tests install match on `$1` being `status` (#705). A flag
            # that silently un-shims a fixture is a test that stops testing.
            r = subprocess.run(
                ["git", "status", "--porcelain", "--ignored=matching", "--",
                 ":(literal)" + os.path.basename(absolute)],
                capture_output=True, text=True, timeout=2,
                cwd=os.path.dirname(absolute) or ".", encoding="utf-8", errors="replace",
            )
            if r.returncode == 0:
                code = r.stdout[:2]
            elif _NOT_A_REPO not in r.stderr.lower():
                parts.append(PATH_META_UNKNOWN)
        except subprocess.TimeoutExpired:
            parts.append(PATH_META_UNKNOWN)
        except OSError:
            # No git on this machine, or the file's directory went away under us.
            # Nothing here was ever going to answer, so a decline that can never
            # resolve would be noise on every read (docs/validators.md,
            # "Declining instead of guessing").
            pass

    if code == "??":
        parts.append("?")
    elif code == "!!":
        parts.append("!")
    elif code and ("M" in code or "A" in code):
        parts.append("m")
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
        return _path_not_found(path, label="file", op="around_line")
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
    hidden: List[str] = []
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
            if exclude_paths:
                rel_f = _safe_relpath(os.path.join(dir_path, f), cwd)
                if _is_excluded(rel_f, exclude_paths):
                    if _is_disclosable_exclusion(rel_f, exclude_paths):
                        hidden.append(f)
                    continue
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
    if hidden:
        out.append(f"({len(hidden)} files hidden by exclude-paths)\n")
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
    ".md": "markdown", ".markdown": "markdown",
}

# Names that differ between the two supported packages for the same
# language. tree-sitter-languages (older) used "c_sharp"; the actively
# maintained tree-sitter-language-pack calls it "csharp" (#790). Keyed
# either direction — _ts_get_parser tries both the requested name and,
# if that fails, its counterpart here.
_TS_LANG_ALIASES: Dict[str, str] = {
    "c_sharp": "csharp",
}

# lang_name -> reason, populated the first time a grammar fails to load
# under either spelling. Lets callers report "grammar unavailable"
# distinctly from "parsed fine, file has no definitions" (#790) instead
# of a LookupError silently becoming an empty result.
_TS_GRAMMAR_FAILED: Dict[str, str] = {}


def _ts_get_parser(lang_name: str) -> Any:
    """Resolve a tree-sitter parser for lang_name, trying the other
    package's spelling before giving up (#790).

    Raises LookupError, with both attempted names in the message, when
    neither spelling resolves under the installed package. Failures are
    cached in _TS_GRAMMAR_FAILED so repeated calls for the same language
    (e.g. across every file of a map: run) don't re-attempt both names
    and so callers can distinguish this from a working grammar that
    simply found nothing.
    """
    if lang_name in _TS_GRAMMAR_FAILED:
        raise LookupError(_TS_GRAMMAR_FAILED[lang_name])

    if _TS_PACKAGE == "pack":
        from tree_sitter_language_pack import get_parser
    else:
        from tree_sitter_languages import get_parser

    try:
        return get_parser(lang_name)
    except LookupError as first_err:
        alt = _TS_LANG_ALIASES.get(lang_name)
        if alt is None:
            alt = next(
                (k for k, v in _TS_LANG_ALIASES.items() if v == lang_name), None)
        if alt is not None:
            try:
                return get_parser(alt)
            except LookupError as second_err:
                reason = (f"neither {lang_name!r} nor {alt!r} recognised by "
                          f"the installed tree-sitter package "
                          f"({first_err}; {second_err})")
                _TS_GRAMMAR_FAILED[lang_name] = reason
                raise LookupError(reason) from second_err
        reason = f"{lang_name!r} not recognised by the installed tree-sitter package ({first_err})"
        _TS_GRAMMAR_FAILED[lang_name] = reason
        raise LookupError(reason) from first_err

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
    try:
        parser = _ts_get_parser(lang_name)
    except LookupError:
        # Grammar could not be loaded under either known spelling — recorded
        # in _TS_GRAMMAR_FAILED by _ts_get_parser. Returning [] keeps the
        # existing contract (ctags/regex tiers still get a chance below
        # this call), but the failure is now discoverable rather than
        # silently identical to "parsed fine, no definitions" (#790).
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

    if lang_name == "markdown":
        return _ts_extract_markdown(source, tree)

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


_MD_HEADING_NODES = frozenset({"atx_heading", "setext_heading"})


def _ts_extract_markdown(source: bytes, tree: Any) -> List[Tuple[str, str, int, int, int]]:
    """Extract the heading tree from a parsed markdown document (#887).

    Headings are markdown's symbols, but they do not fit the generic walker:
    the level lives in a marker child (`atx_h2_marker`, `setext_h1_underline`)
    rather than in the node type, the name lives in an `inline` child rather
    than a `name` field, and nesting is by level rather than by containment.

    Returned as (kind, name, line, end_line, depth) with kind "h1".."h6" and
    depth = level - 1, so `## Foo` renders one step in from `# Foo`.
    """
    symbols: List[Tuple[str, str, int, int, int]] = []

    def _level(node: Any) -> int:
        for child in node.children:
            ctype = child.type
            if ctype.startswith("atx_h") and ctype.endswith("_marker"):
                return int(ctype[5:-7])
            if ctype.startswith("setext_h") and ctype.endswith("_underline"):
                return int(ctype[8:-10])
        return 1

    def _title(node: Any) -> str:
        for child in node.children:
            if child.type in ("inline", "paragraph", "heading_content"):
                return source[child.start_byte:child.end_byte].decode(
                    "utf-8", errors="replace").strip()
        raw = source[node.start_byte:node.end_byte].decode(
            "utf-8", errors="replace")
        return raw.splitlines()[0].lstrip("#").strip(" #").strip()

    def _walk(node: Any) -> None:
        if node.type in _MD_HEADING_NODES:
            level = _level(node)
            name = _title(node)
            if name:
                line = node.start_point[0] + 1
                symbols.append((f"h{level}", name, line, line, level - 1))
            return
        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    symbols.sort(key=lambda s: s[2])
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
    try:
        parser = _ts_get_parser(lang_name)
    except LookupError:
        # See _ts_extract: failure is recorded in _TS_GRAMMAR_FAILED so
        # op_between_symbol can report it instead of a misleading
        # "symbol not found" (#790).
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
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace"
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
    path: str, exclude_paths: Tuple[str, ...],
    hidden: Optional[List[str]] = None,
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
            if ext not in _MAP_EXTENSIONS:
                continue
            rel_fn = os.path.join(rel_root, fn)
            if exclude_paths and _is_excluded(rel_fn, exclude_paths):
                if hidden is not None and _is_disclosable_exclusion(
                        rel_fn, exclude_paths):
                    hidden.append(os.path.join(root, fn))
                continue
            files.append(os.path.join(root, fn))
    return files


MAX_MAP_FILES = 100  # Cap to prevent overwhelming output

# Substring every "we could not look" render shares, so callers that key off
# map's output (`_abstract_map`) can recognise the third state without
# re-deriving which extensions have parsers.
_NO_PARSER_MARKER = "no symbol parser for "


def _map_no_parser_reason(ext: str, use_ts: bool, use_ctags: bool) -> str:
    """Why no tier could look at EXT, or "" when at least one could (#887).

    `map` had two renders for three facts: symbols, none found, and no parser
    for this file type. The third collapsed into the second, so a markdown
    file dense with headings reported `(no symbols)` — an absence produced by
    the tool, stated as an absence in the document. This computes the third
    state so the render can keep it separate; see docs/validators.md,
    "Declining instead of guessing".

    A tier counts as able to look when it has patterns or a grammar for EXT,
    not merely when it is installed. ctags is deliberately not treated as a
    parser here: `op_map` only consults it when tree-sitter is absent, and the
    build on PATH may be BSD ctags, which cannot be queried for its language
    list — so a note is appended rather than a capability claimed.
    """
    ts_lang = _TS_LANG_MAP.get(ext) if use_ts else None
    if ts_lang and ts_lang not in _TS_GRAMMAR_FAILED:
        return ""
    if ext in _REGEX_PATTERNS:
        return ""

    if not ext:
        ext = "(no extension)"
    if use_ts:
        detail = f"tree-sitter and the regex tier have no {ext} grammar"
    else:
        detail = (f"tree-sitter is not installed and the regex tier has no "
                  f"{ext} patterns")
    if use_ctags:
        detail += "; ctags found nothing"
    return f"{_NO_PARSER_MARKER}{ext} - {detail}"


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
        return _path_not_found(path, op="map", call_prefix="map")

    hidden_files: List[str] = []
    files = _collect_files(
        path, _get_exclude_paths("map", no_exclude), hidden_files)
    if not files:
        return f"(no supported files found in {path})\n"

    truncated = len(files) > MAX_MAP_FILES
    files = files[:MAX_MAP_FILES]

    # Detect available tier
    use_ts = _has_tree_sitter()
    use_ctags = not use_ts and _has_ctags()

    # tier label is computed after extraction to reflect what actually produced symbols
    actual_tier: str = "regex"
    unparsed = 0

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
            no_parser = _map_no_parser_reason(ext, use_ts, use_ctags)
            ts_lang = _TS_LANG_MAP.get(ext) if use_ts else None
            if no_parser:
                # No tier has a grammar or a pattern set for this extension.
                # Saying "(no symbols)" here would report the tool's blind
                # spot as a property of the file (#887).
                out_files.append(
                    f"{_fwd(fpath)} ({line_count} lines)\n  ({no_parser})\n")
                unparsed += 1
            elif ts_lang and ts_lang in _TS_GRAMMAR_FAILED:
                # Every tier came up empty AND tree-sitter's grammar never
                # loaded for this language — say so, rather than rendering
                # byte-identical to a file with genuinely zero definitions
                # (#790).
                out_files.append(
                    f"{_fwd(fpath)} ({line_count} lines)\n"
                    f"  (tree-sitter grammar unavailable for {ext}: "
                    f"{_TS_GRAMMAR_FAILED[ts_lang]} - no symbols from any tier)\n")
            else:
                # File exists but no symbols extracted — show it as empty
                out_files.append(f"{_fwd(fpath)} ({line_count} lines)\n  (no symbols)\n")

    if unparsed == len(files):
        # Naming a tier that never had a pattern to try is the report line
        # telling the same lie the body used to (#887).
        actual_tier = "none"
    out = [f"({len(files)} files{_hidden_suffix(len(hidden_files))}, tier: {actual_tier})\n"] + out_files
    # A map of the entry-point shim is the module's surface to whoever reads
    # it, and it is not (#1272). Fires on the shim named directly — a map of
    # the *directory* enumerates `_supertool.py` on the next line, so there is
    # nothing left to disclose and the gate's basename test declines it.
    out.append(_shim_facade_surface_note(path))
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


def _truncation_suffix(truncated: bool, total: Optional[int] = None,
                       capped: bool = False) -> str:
    """Report format's truncation disclosure (#448, #1073).

    `(1 results in 1 files, scanned 118353 files, limit 1)` reads as an
    exhaustive answer and is not one, which is how a coverage audit concluded a
    class had no test when the test was sitting one match past the cap. The
    marker is only ever emitted when a match past the limit was actually seen,
    so its absence is a positive statement: this count is exact.

    `more matches exist` said the answer was partial without saying how partial
    (#1073). 21 matches and 500 matches produced identical bytes and warrant
    opposite next actions, so the scope is now part of the marker — in three
    states that must not collapse into each other:

    * `N matches total`                  — counted, and this is all of them;
    * `N+ matches total (count capped…)` — counting stopped at the ceiling, so
      the number is a floor rather than a total;
    * `more matches exist (total not counted)` — nothing counted. The delegated
      rtk report has no candidate list to count over, and "we did not count"
      must not render as "we counted and there are some".
    """
    if not truncated:
        return ""
    if total is None:
        return " — TRUNCATED, more matches exist (total not counted)"
    if capped:
        return (f" — TRUNCATED, {total}+ matches total "
                f"(count capped at {total})")
    return f" — TRUNCATED, {total} matches total"


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


def _hidden_suffix(hidden: int) -> str:
    """Report format's ", N files hidden by exclude-paths" clause (#691).

    An exclusion that leaves no trace in the output is indistinguishable from a
    file that was not there — the same silent-failure shape the `scanned N`
    denominator and the TRUNCATED marker were both added to close. Credential
    files are the reason the list exists, but "your search skipped something"
    is the caller's to know, and the way back (`no-exclude`) is one flag away.

    Which exclusions count is decided by `_is_disclosable_exclusion`, not by
    file-versus-directory: built-in noise entries stay out of the number so it
    reads zero on the ordinary call. A counter that is never zero is one a
    reader learns to skip, and then the call that fires because a real `.env`
    was hidden looks like all the others.
    """
    if hidden <= 0:
        return ""
    return f", {hidden} files hidden by exclude-paths"


def _grep_count_ceiling(limit: int) -> int:
    """How many matches grep will count before it stops counting (#1073).

    Never below the caller's own LIMIT: a ceiling under the limit would cap the
    total below the number of rows printed underneath it, which is a worse
    render than the one this replaces.
    """
    return max(_get_op_int("grep", "count_ceiling", MAX_GREP_COUNT_CEILING),
               limit)


def _grep_total(seen: int, ceiling: int) -> Tuple[int, bool]:
    """`(total, capped)` for a walk that stopped once it had `ceiling + 1`.

    `seen` may overshoot by more than one in context mode, where matches are
    collected a window at a time — so the reported floor is clamped rather than
    passed through, and a number printed as exact is always one that is.
    """
    return min(seen, ceiling), seen > ceiling


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
    path: str, exclude_paths: Tuple[str, ...] = (),
    hidden: Optional[List[str]] = None,
) -> List[str]:
    """Return list of file paths to search for a given path argument.

    When exclude_paths is provided, directories whose path-relative-to-cwd
    starts with one of the prefixes are pruned at the walk boundary (dirs[:]
    mutation) so their subtrees are never opened. Gitignored directories are
    pruned at the same boundary (#449) — and because the pruning happens
    before the files are collected, the returned length is what op_grep reports
    as `scanned N`, so #407's denominator shrinks with the walk instead of
    counting agent worktrees six times over.

    **Files are filtered too** (#691). This loop used to test the extension and
    nothing else, so `.env` — on the default exclude list since #146 — was read
    and printed like any other file. Excluded files are appended to `hidden`
    when a list is passed, rather than merely counted: op_grep discloses how
    many there were, and anyone questioning that number needs the names.

    A `path` that IS an excluded file is still searched. Naming it is a
    deliberate act and `read` never gated it, so gating it here would buy
    nothing and break the case someone meant.
    """
    candidates: List[str] = []
    if os.path.isfile(path):
        candidates.append(path)
    elif os.path.isdir(path):
        exts = _grep_file_includes()  # None = all files
        cwd = os.getcwd()
        ignored = _git_ignored_dirs(path) if exclude_paths else frozenset()
        for root, dirs, files in os.walk(path):
            rel_root = _safe_relpath(root, cwd) if exclude_paths else ""
            if exclude_paths:
                dirs[:] = [
                    d for d in dirs
                    if not _is_excluded(os.path.join(rel_root, d), exclude_paths)
                    and not _is_git_ignored(rel_root, d, ignored)
                ]
            for name in files:
                if exts is not None and not any(
                        name.endswith(ext.lstrip("*")) for ext in exts):
                    continue
                rel_name = os.path.join(rel_root, name)
                if exclude_paths and _is_excluded(rel_name, exclude_paths):
                    if hidden is not None and _is_disclosable_exclusion(
                            rel_name, exclude_paths):
                        hidden.append(os.path.join(root, name))
                    continue
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
    pattern: str, exclude_paths: Tuple[str, ...] = (), over_fetch: int = 0,
    hidden: Optional[List[str]] = None,
) -> List[str]:
    """Glob matching files, supports ** recursive. Returns up to MAX_GLOB_RESULTS.

    `over_fetch` raises the internal cap by that many files without changing
    the cap callers are told about. op_glob passes 1 so it can tell a list that
    happens to be cap-length from one that was cut short (#448).

    When exclude_paths is provided and the pattern contains '**', uses an
    os.walk-based implementation that prunes excluded directories at the walk
    boundary (never opens them).  For non-recursive patterns, falls back to
    glob.glob and filters results post-hoc (no subtree to prune anyway).

    Both halves filter *files* against exclude_paths (#691). Only the glob.glob
    half ever did, so one op gave two answers: `glob:.env*` hid `.env` and
    `glob:**/.env*` listed it. Excluded files land in `hidden` when a list is
    passed, so op_glob can say how many it dropped.
    """
    max_results = _get_op_int("glob", "max_results", MAX_GLOB_RESULTS) + over_fetch

    # Brace expansion: `*.{json,xml}` → fan out + dedupe. Shell/fd semantics.
    expanded = _expand_braces(pattern)
    if expanded != [pattern]:
        seen: set = set()
        results: List[str] = []
        for sub_pattern in expanded:
            for f in _glob_files(sub_pattern, exclude_paths, over_fetch, hidden):
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
                        rel_full = _safe_relpath(full, cwd)
                        if _is_excluded(rel_full, exclude_paths):
                            if hidden is not None and (
                                    _is_disclosable_exclusion(
                                        rel_full, exclude_paths)):
                                hidden.append(full)
                            continue
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
        if hidden is not None:
            hidden.extend(
                m for m in files_out
                if _is_disclosable_exclusion(
                    _safe_relpath(m, cwd), exclude_paths)
            )
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

_DRIVE_LETTER = re.compile(r"^@?[A-Za-z]\Z")  # \Z, not $ — #1188
_URL_SCHEMES = ("http", "https", "ftp", "ftps", "ssh", "git", "file", "ws", "wss")
# Numeric port, optionally followed by '/path' or '?query' or end — used to
# absorb 'https://host' + ':8080/path' fragments that arose from `:`-splitting.
_URL_PORT = re.compile(r"^\d+(?:[/?#].*)?\Z")  # \Z, not $ — #1188


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

    # Peel the trailing LIMIT[:CONTEXT] slots: format is ...PATH:LIMIT:CONTEXT.
    # Two trailing tokens = limit + context; one = limit only. `all` (#1328) is
    # accepted in the LIMIT slot alongside the digits — before this it was not a
    # digit, so it fell through to the PATH slot and `grep:PAT:PATH:all` searched
    # a directory called `all`.
    context = 0
    limit = _get_op_int("grep", "max_results", MAX_GREP_RESULTS)
    trailing = []
    while len(args) >= 3 and (args[-1].isdigit()
                              or args[-1] == _GREP_ALL_TOKEN):
        trailing.insert(0, args[-1])
        args = args[:-1]
    if len(trailing) == 1:
        limit = (GREP_LIMIT_ALL if trailing[0] == _GREP_ALL_TOKEN
                 else int(trailing[0]))
    elif len(trailing) >= 2:
        limit = (GREP_LIMIT_ALL if trailing[0] == _GREP_ALL_TOKEN
                 else int(trailing[0]))
        if _GREP_ALL_TOKEN in trailing[1:]:
            # `all` is a LIMIT. Reading it as one here would run a call nobody
            # typed (limit `all` with the caller's number silently demoted to
            # context), and ignoring it would run the default under a token the
            # caller believes changed something. `trailing[1:]` rather than
            # `trailing[1]` because the peel takes every trailing token and the
            # read takes two: a third one is dropped, and a dropped `all` is
            # exactly the silent-completeness bug this token exists to close.
            limit = GREP_LIMIT_ALL_MISPLACED
        else:
            context = int(trailing[1])

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


def _around_line_delegation(pattern: str, path: str, n: int) -> str:
    """Answer `around:PATH:LINE[:N]` as `around_line`, and say so (#1086).

    `around` takes PATTERN:PATH[:N] and `around_line` takes PATH:LINE[:N] — the
    same op family, the same output, opposite argument order, and nothing in
    either name says which. Four agents in one session picked wrong; every one
    recovered off the error message, which is why it stayed unfiled and also why
    a better error was never going to be the fix.

    Gated so it only ever converts a call that ALREADY FAILS into an answer: the
    numeric argument must not name a real file (a file called `1160` is a path,
    not a line), the argument in front of it must pass cwd containment, and it
    must resolve. No call that works today changes meaning, and the
    `:`-tokenizer is not touched — this is post-parse recovery inside the op.

    `between:PATH:START:END` is deliberately NOT given the same treatment: its
    redirect target is `read`, a different op, and #983 decided that `between`
    should keep doing exactly one thing rather than grow a fourth spelling of a
    range read. Its error already carries the fully-substituted `read` command.
    """
    if not path.isdigit() or os.path.exists(path):
        return ""
    line = int(path)
    if line < 1:
        return ""
    # The promotion happens HERE and nowhere else: past this point `pattern` has
    # stopped being a pattern and is a filename. Dispatch already ran containment
    # on the path the parser computed, which in THIS reading is the numeric
    # token — so parts[1] arrives unchecked, and `around:/etc/hosts:3` read a
    # file that `around:localhost:/etc/hosts:1` — the same file, named in the
    # slot the parser does treat as a path — refuses (#1135).
    #
    # The check is here rather than at the dispatch guard because parts[1] is a
    # PATTERN in every other reading, and a pattern is not a path: gating it
    # unconditionally would refuse `around:/etc/passwd:code.py`, i.e. searching
    # a repo for an absolute path string. The dispatch guard answers "the path
    # this call resolved"; this slot only becomes a path conditionally, so the
    # guard has to be conditional too. (#1166 dropped `around` from
    # `_PATH_ARG_POSITIONS` entirely — the table gated a fixed slot the parser
    # did not necessarily use — but that changed nothing here: the promoted slot
    # was never in it.)
    _contained = _containment_error([pattern])
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
    """Error text for `between:PATH:START:END` and `between:PATH:LINE` (#983).

    `between` is SYMBOL:PATH, so a trailing line number is tokenized as the
    path and the call fails with `path not found: '20'` — a filesystem
    complaint about a number, which is the tool's own mis-split wearing the
    shape of an absence on disk. Three agents in one evening read that as
    "supertool cannot do this" and fell back to `sed` or the harness Read.

    The redirect names the op that already answers, rather than growing a
    fourth spelling of the same read: `read:PATH:START-END` is an inclusive
    1-based range and has been since 0.19.0, and `around_line:PATH:LINE[:N]`
    covers the single-line case. `between` is left doing exactly one thing.

    Returns "" whenever the shape is not unambiguously a line range — the
    numeric token must not name a real path (a file called `12` is a range
    nobody asked for), and the path in front of it must resolve, or this
    would be guessing at a plain typo.

    That resolve is a filesystem probe on parts[1], and parts[1] is a SYMBOL in
    every other reading of `between` — so `_PATH_ARG_POSITIONS["between"] =
    (2, 4)` does not cover it and dispatch's gate has already passed on a slot
    this call did not use as a path. Unguarded, the two answers below differ by
    whether the file is there, which is an existence oracle for anything the
    process can stat (#1142):

        between:/etc/hosts:3:5   -> the range redirect
        between:/etc/nope:3:5    -> path not found: '5'

    Contained here rather than in the table, the shape #1135 established: the
    slot only becomes a path conditionally, so the guard has to be conditional
    too — widening the table would refuse `between:/etc/passwd:code.py`, a
    perfectly ordinary symbol lookup. And it refuses rather than falling
    silent, because the redirect this hint would print is `read:PATH:START-END`
    on a path `read` itself refuses: advice whose remedy is refused is worse
    than the refusal. `_containment_error` never stats, so the refusal reads
    the same whether or not the file exists.
    """
    if len(parts) not in (3, 4):
        return ""
    nums = parts[2:]
    # `read:` takes both `PATH:OFFSET:LIMIT` and `PATH:START-END`, so both
    # spellings arrive here from the same muscle memory. Only the colon one used
    # to be caught; the dash one fell through to `file not found: '1-30'` — a
    # filesystem complaint about a line range, which is the exact mis-split this
    # hint exists to translate, one spelling later (#1234).
    if len(nums) == 1 and nums[0].count("-") == 1 and not os.path.exists(nums[0]):
        _a, _b = nums[0].split("-")
        if _a.isdigit() and _b.isdigit():
            nums = [_a, _b]
    if not all(t.isdigit() and not os.path.exists(t) for t in nums):
        return ""
    path = parts[1]
    if not path:
        return ""
    _contained = _containment_error([path])
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
    """Hint for a `PATH,PATH,...` list handed to an op that takes one path.

    `git-resolve` accepts a comma list, so reaching for it elsewhere is the
    spelling the operator was already taught. Joined into one filename it
    fails, and the generic `wrong CWD?` advice then names the one thing that
    provably did not cause it: every entry resolves from the current
    directory. Following it produces the same error a second time (#921).

    Returns "" unless at least one entry exists — a path with a literal comma
    that is simply absent is an ordinary typo, and inventing a list there
    would trade one misreport for another. The count is stated rather than
    implied, so the caller can tell "all of these exist" from "some do".
    """
    if "," not in path:
        return ""
    entries = [e for e in path.split(",") if e]
    if len(entries) < 2:
        return ""
    # Containment before the stat, not after: the loop below is an existence
    # probe on every entry, and dispatch only ever gated the comma-JOINED
    # string — `a.py,/etc/shadow` resolves under the cwd because its first
    # character does, so the tally leaked whether the second entry existed
    # (#1142, the same oracle #1135 closed for `around`).
    if _containment_error(entries):
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


#: Entry-point shims and the sibling that holds the code they stand for. A
#: named pair, not a general "facade" test. A general one — a small module
#: that re-exports a bigger sibling — would fire on every `__init__.py` in a
#: package and still not be the thing anyone greps by mistake; this pair is
#: the one the rename created (#931) and it fires every time.
_SHIM_CORE = {"supertool.py": "_supertool.py"}


def _shim_core_beside(path: str) -> str:
    """The core file name, if PATH is the entry-point shim with it on disk.

    The gate both facade notes share. Gated on the pair actually being on disk
    together: a lone file named `supertool.py` in someone else's tree is an
    ordinary file, and a note there would be a guess.
    """
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
    """Disclose the facade beside a result that is CORRECT but partial (#1272).

    `map:supertool.py` returns the shim's two real symbols. Small, correct,
    positive — and misleading in exactly the way #1259's zero was, because the
    reader concludes they have seen the module's surface. It is the harder half:
    an absence at least looks like nothing, whereas an answer-shaped answer
    gives nobody a reason to make the second call.

    So the wording is not #1259's. That note says the result is evidence about
    the shim rather than about supertool, and sends the caller to re-run —
    right, because there the result was empty. Here the listed symbols really
    are the shim's and the list really is complete for that file, so a note
    that read as a correction would be false about a true result. This one
    affirms the result, then says the surface is next door and offers a second
    call to ADD, never a re-run to replace.
    """
    core = _shim_core_beside(path)
    if not core:
        return ""
    return (f"(note: {os.path.basename(path)} is only the entry point — "
            f"supertool's implementation lives in {core} beside it (#931). "
            f"The symbols above are the shim's own and this map is complete "
            f"for that file; supertool's own surface is next door. Add "
            f"map:{core} to see it.)" + chr(10))


def _shim_facade_note(path: str) -> str:
    """Disclose that an empty result came from an entry-point shim (#1259).

    `grep:SYMBOL:supertool.py` scans a file that by construction holds almost
    nothing and reports `0 results in 0 files, scanned 1 files`. Every clause
    is true and the three-state contract is working — `scanned 1 files` proves
    the op looked. What it cannot say is that the file it looked at is a
    facade, so the zero is byte-identical to a zero from the core, and every
    instinct to grep `supertool.py` has landed here since the split.

    Disclosure, never redirect. Scanning `_supertool.py` for a caller who
    named `supertool.py` would answer a question nobody asked and report it
    as the answer to the one they did — a quieter wrong answer than the one
    it replaced.

    Gated by `_shim_core_beside` on the pair being on disk together. Only ever
    called where the result is already empty, so a *hit* — which IS evidence
    about the shim — is left alone by this one. A hit that claims to be the
    whole file's surface is a third case, and #1272 gives it its own wording in
    `_shim_facade_surface_note`: the note there may not read as a correction.
    """
    core = _shim_core_beside(path)
    if not core:
        return ""
    return (f"(note: {os.path.basename(path)} is only the entry point — "
            f"supertool's implementation lives in {core} beside it (#931). "
            f"An empty result here is evidence about the shim, not about "
            f"supertool. Re-run against {core}.)" + chr(10))


def _multi_path_suggest(op: str, path: str,
                        call_prefix: Optional[str] = None) -> str:
    """Hint for `PATH PATH ...` handed to an op that takes one path (#1261).

    `grep PATTERN a.py b.py` is the shell spelling every caller already has.
    Written into the colon CLI the whole argument list lands in the single
    PATH slot and the call fails as one missing filename. What answered
    before was the `:`-split hint, and its prescribed repair is a payload —
    where `path` is a scalar too, so following it reproduces the failure one
    form further along. The op can positively tell the two apart: the value
    it could not resolve contains whitespace and its parts each exist.

    Returns "" unless EVERY part exists. A partial match is a genuinely
    missing path, and stacking a second diagnosis on the first would trade
    one misreport for another — the three-state rule, `docs/validators.md`.

    Returns "" as well when any part fails containment, and that check runs
    BEFORE the existence loop rather than after it: the loop is a stat on
    each part, and dispatch upstream only ever gated the whitespace-JOINED
    string, which resolves under the cwd whenever its first part does. Left
    unguarded the disclosure is an existence oracle for anything the process
    can reach (#1142).

    No new syntax on purpose. Accepting a whitespace list would make a
    filename containing a space unrepresentable in the one slot that must be
    able to name any file — and the tree already has a delimiter for the ops
    that do take lists (`validate:a.py,b.py`, `git-resolve`), so a second one
    would mean two spellings for one idea. Batching is the tool's premise and
    it already buys the round-trip the caller was reaching for.
    """
    parts = path.split()
    if len(parts) < 2:
        return ""
    if _containment_error(parts):
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


def _looks_like_path(tok: str) -> bool:
    """Could *tok* plausibly be a path the caller typed?

    Only used to decide whether a not-found path is more likely a typo or the
    tail of a `:`-split pattern. Whitespace, quotes, pipes and regex punctuation
    are what a spilled pattern carries; a path the caller meant has none.
    """
    if not tok or tok != tok.strip():
        return False
    return not any(c in tok for c in " \t\"'()|<>*?")


def _colon_split_hint(op: str, leading: str, path: str,
                      keys: Tuple[str, ...] = ("pattern",),
                      call_prefix: Optional[str] = None) -> str:
    """Error text for a read op whose PATH does not exist and probably should.

    A read op that mis-tokenizes must never read as an absence in the world.
    grep/around/between already fail loudly on a missing path rather than
    returning a bare zero — this makes the failure NAME the cause and carry
    the escape, so the next caller does not re-derive it from scratch (#625).

    Returns "" when the missing path is an ordinary typo (no ':' in the
    leading argument and the path token still looks like a path) — crying
    wolf on every wrong path would make the advice worthless.
    """
    if not path or path == "." or os.path.exists(path):
        return ""
    if ":" not in leading and _looks_like_path(path):
        return ""
    # Ahead of the `:` diagnosis, not after it. A value that whitespace-splits
    # into parts which all exist is positively a different mistake, and the
    # colon advice points at a payload whose `path` is a scalar as well — a
    # repair that fails identically one form further along (#1261).
    if call_prefix is None:
        call_prefix = f"{op}:{leading}"
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
        return _path_not_found(path, op="replace")

    candidates = _grep_candidates(path, _get_exclude_paths("replace"))
    if not candidates:
        return "(0 files to search)\n"

    # Collect matches via whole-file scan so multi-line `old` patterns work.
    # Line-by-line matching would silently miss any pattern containing '\n'.
    # (filepath, [match_start_offsets], effective_old, effective_new). The two
    # effective strings are carried rather than recomputed: this pass and the
    # write pass below used to read the same file with different newline
    # settings, so on a CRLF file one found an LF `old` and the other did not.
    # The write became a no-op and the receipt reported *this* pass's number —
    # `(2 replacements in 2 files)` over two unchanged files (#1049).
    file_matches: List[Tuple[str, List[int], str, str]] = []
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
            with open(file_path, "r", encoding="utf-8",
                      errors="surrogateescape", newline="") as f:
                content = f.read()
        except OSError:
            continue
        positions: List[int] = []
        eff_old, eff_new = old, new
        for nl, cand in _newline_variants(old):
            start = 0
            found: List[int] = []
            while True:
                idx = content.find(cand, start)
                if idx == -1:
                    break
                found.append(idx)
                start = idx + len(cand)
            if found:
                positions = found
                eff_old = cand
                eff_new = _retermed(new, nl) if nl else new
                break
        if positions:
            file_matches.append((file_path, positions, eff_old, eff_new))
            total_count += len(positions)

    if total_count == 0:
        # The one no-op receipt in the tool that does not say ERROR, so it
        # never reached the exit code either (#680). A preview finding nothing
        # is a truthful preview, not a decline — only the real op declines.
        if not dry:
            _SKIP_COUNT[0] += 1
        return f"(0 occurrences of '{old}' found)\n"

    if dry:
        out: List[str] = [f"({total_count} occurrences in {len(file_matches)} files)\n"]
        for filepath, positions, eff_old, eff_new in file_matches:
            # splitlines, not split("\n"): the effective strings may be CRLF or
            # CR terminated, and a split on "\n" leaves a trailing \r on every
            # preview line.
            old_lines = eff_old.splitlines() or [eff_old]
            new_lines = eff_new.splitlines() or [eff_new]
            out.append(f"\n{_fwd(filepath)}\n")
            try:
                with open(filepath, "r", encoding="utf-8",
                          errors="surrogateescape", newline="") as f:
                    content = f.read()
            except OSError:
                continue
            for pos in positions:
                start_line = _line_number_at(content, pos)
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
    vanished: List[str] = []
    for file_path, _scan_positions, eff_old, eff_new in file_matches:
        try:
            # newline="": see op_edit / op_append. Without it every line of a
            # CRLF file was rewritten to LF by a replace that matched one
            # string (#1049). The scan's positions are deliberately unused
            # here — recounting against the bytes about to be written is the
            # whole point.
            with open(file_path, "r", encoding="utf-8",
                      errors="surrogateescape", newline="") as f:
                content = f.read()
        except OSError:
            continue
        # Counted here, from the bytes about to be written, never from the scan
        # pass. A number taken from the scan survived the write matching
        # nothing and reported replacements that had not happened (#1049).
        hits = content.count(eff_old)
        if not hits:
            vanished.append(file_path)
            continue
        new_content = content.replace(eff_old, eff_new)
        try:
            _atomic_write(file_path, new_content)
            files_modified[file_path] = hits
        except OSError as e:
            return f"ERROR: failed to write {file_path}: {e}\n"

    total = sum(files_modified.values())
    # `_newline_note` states the invariant this discloses: "re-writing the
    # caller's own endings is always disclosed". `op_edit` and
    # `op_replace_lines` call it; `replace` did the same `_newline_variants`
    # re-termination and said nothing, so a CRLF replace rewrote the caller's
    # block in silence (#1049, third pass).
    #
    # `_newline_note` itself is not reusable here and is deliberately not
    # called: it writes one sentence about one file, and `replace` is
    # multi-file with a different answer per file — a summary line true of the
    # run would be false of every file that matched literally. So the fact goes
    # on the file's own line and the sentence is said once.
    #
    # Derived from `eff_old`, not carried out of the scan: `_newline_variants`
    # drops any candidate equal to the literal, so `eff_old != old` is exactly
    # "a re-terminated variant matched", and the resolved scan/write pair above
    # stays untouched.
    retermed: Dict[str, str] = {}
    for _fp, _positions, _eff_old, _eff_new in file_matches:
        if _fp in files_modified and _eff_old != old:
            retermed[_fp] = ("CRLF" if "\r\n" in _eff_old
                             else "CR" if "\r" in _eff_old else "LF")
    out = [f"({total} replacements in {len(files_modified)} files)\n"]
    for fp, cnt in sorted(files_modified.items()):
        tag = f" [{retermed[fp]}]" if fp in retermed else ""
        out.append(f"  {_fwd(fp)} ({cnt}){tag}\n")
    if retermed:
        # Only where a choice was made. A uniform file whose `old` matched
        # literally is not marked and produces no line at all — on Windows
        # every file is CRLF, and a note that fires on every call is the noise
        # #1049's second pass removed.
        out.append(f"  {mark('↳')} line endings: the text you supplied did not "
                   f"match {len(retermed)} of these files byte for byte and "
                   f"was re-terminated to the convention marked above to make "
                   f"it match — every untouched line is unchanged\n")
    if vanished:
        # Matched during the scan, gone by the time the write read it back.
        # Dropping these silently is how the count and the disk disagree.
        out.append(f"\n{len(vanished)} file(s) matched during the scan and no "
                   f"longer matched when the write read them back — NOT "
                   f"modified:\n")
        for fp in sorted(vanished):
            out.append(f"  {_fwd(fp)}\n")
    out.append(f"\nDone: '{old}' → '{new}'\n")
    return "".join(out)


def _write_target(path: str) -> str:
    """The path a write to *path* actually lands on — a symlink followed (#1136).

    `_atomic_write` resolves a symlink before `os.replace` so the real file is
    written rather than clobbered by a regular file. Anything that has to reason
    about what that write did to the filesystem — above all the rollback, which
    deletes — has to ask the same question of the same path, or it decides
    identity on an object the writer never touched. One expression, two callers.
    """
    return os.path.realpath(path) if os.path.islink(path) else path


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
    # Every mutating op passes through here, which makes it the one place that
    # can promise a coalesced `git status` snapshot never outlives a write
    # (#1126). Cleared before the write rather than after: an exception on the
    # way out would otherwise leave a snapshot describing a tree that has
    # already partly moved.
    _path_meta_bulk_drop()
    # Byte-pattern warnings a syntax validator structurally cannot catch, raised
    # at the one place every mutating op passes through (#380).
    _warn = _sh_backslash_warning(path, content)
    _key = os.path.abspath(path)
    real_path = _write_target(path)
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


_BRANCH_CACHE: List[Optional[Tuple[str, str]]] = [None]

# Above this, the nearest-line scan is skipped — the diagnostic is a courtesy
# on a failure path and must never become the slow part of a failed edit.
_EDIT_DIAG_MAX_LINES = 20000

_GIT_TIMEOUT_DEFAULT = 5


def _git_timeout() -> int:
    """Budget for the git calls supertool makes about itself (#650).

    Overridable per environment for the same reason SUPERTOOL_LINT_TIMEOUT is
    (#553): a loaded runner occasionally needs room, and that is a fact about
    the runner, never a decision about the product. The shipped default does
    not move — pinned by test_the_suite_budget_does_not_move_the_product_default.
    """
    return _env_int("SUPERTOOL_GIT_TIMEOUT", _GIT_TIMEOUT_DEFAULT, minimum=1)


def _branch_reading() -> Tuple[str, str]:
    """`(branch, why_unavailable)` — three states, not two (#650).

    `("my-feature", "")` git answered and named a branch; `("", "")` git
    answered and there is no branch to name; `("", why)` git did not answer.

    The third state used to be the second. Both were `""`, and `_branch_line()`
    renders `""` as silence, so a receipt whose branch lookup timed out was
    byte-identical to one taken outside a repo — an absence the tool produced,
    read as an absence in the world (docs/validators.md, "Declining instead of
    guessing"). It is the wrong direction to be wrong in: the footer exists to
    catch right-file-wrong-branch, so it fell silent on exactly the run where
    the caller had least idea what state the repo was in.

    A missing git binary stays in the middle state deliberately. Nothing on
    that machine was ever going to name a branch, which is the one honest
    silence — the same line `_vim_render_lint` draws for an uninstalled checker.

    Cached for the process: a single supertool invocation cannot change branch
    mid-call, and a batch of edits would otherwise pay a subprocess each. The
    decline is cached with it — a stalled read is the expensive one to repeat.
    """
    if _BRANCH_CACHE[0] is None:
        branch = ""
        why = ""
        budget = _git_timeout()
        try:
            # symbolic-ref, not rev-parse: it resolves an *unborn* branch (a
            # fresh `git init` before the first commit), where rev-parse fails
            # with "ambiguous argument 'HEAD'". It exits non-zero on a detached
            # HEAD, which is the one case worth a second call.
            r = subprocess.run(
                ["git", "symbolic-ref", "--short", "-q", "HEAD"],
                capture_output=True, text=True, timeout=budget,
                encoding="utf-8", errors="replace",
            )
            if r.returncode == 0:
                branch = r.stdout.strip()
            else:
                d = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=budget,
                    encoding="utf-8", errors="replace",
                )
                if d.returncode == 0 and d.stdout.strip():
                    branch = f"detached HEAD at {d.stdout.strip()}"
        except FileNotFoundError:
            pass
        except subprocess.TimeoutExpired as exc:
            cmd = " ".join(str(a) for a in (exc.cmd or ["git"]))
            why = f"`{cmd}` did not answer within {budget}s"
        except OSError as exc:
            why = f"`git symbolic-ref` could not be run — {exc}"
        _BRANCH_CACHE[0] = (branch, why)
    return _BRANCH_CACHE[0]


def _current_branch() -> str:
    """Current git branch, or "" when there isn't one to report.

    The plain-string contract every caller but the receipt wants; *why* the
    string is empty is `_branch_reading()`'s second value.
    """
    return _branch_reading()[0]


def _branch_line() -> str:
    """`[branch: X]` footer for a mutating op (#381).

    Two near-misses in one session were the same shape — right file, wrong
    branch — and supertool is the thing that knows. A handful of tokens per
    call, against a class of mistake that is otherwise silent until commit time.
    """
    branch, why = _branch_reading()
    if branch:
        return f"[branch: {branch}]\n"
    # A read that failed is not a repo without a branch (#650). Silence is
    # reserved for the second; the first says so and names what stalled.
    if why:
        return f"[branch: UNKNOWN — {why}]\n"
    return ""


def _result_line(ops: int, writes: int, skipped: int = 0,
                 reapplied: int = 0,
                 not_checked: Optional[Sequence[str]] = None,
                 rolled_back: int = 0,
                 validated: Optional[Sequence[Tuple[str, bool, bool]]] = None) -> str:
    """`[result] N ops run, M writes[, K skipped][, K rolled back][, K re-applied]` (#621),
    or, for a read-only `validate:` run, `[result] N files, M with findings,
    K not checked` (#990).

    The receipt a mutating op prints sits ABOVE the `[validators]` block, and a
    long validators block is exactly when a reader reaches for `| tail -4`. So
    the last thing on screen was `git-status : ok` — which describes the
    validators and reads as though it described the edit. A no-match and an
    11-file replace were indistinguishable that way, twice, at real cost: once a
    teammate was told files were unfixed when they had been fixed, once an agent
    reported a no-matched batch edit as landed and sent a broken branch to CI.

    The invariant this pins is stronger than "print the summary last": an op
    which changed nothing must not END with output that looks like an op which
    did. So this is a footer, not a move — the detailed receipt stays where
    positional readers and every existing ordering test expect it, and one
    honest line is repeated below the noise.

    Both numbers come from counters, never from re-reading the prose receipt.
    `ops` is `_MUTATION_ATTEMPTS` (bumped when a mutating op runs, outcome
    unknown); `writes` is `_WRITE_COUNT` (bumped at `_atomic_write`, decremented
    by `_retract_write`), so a rolled-back edit correctly reports 0. In a batch
    that is precisely "requested" vs "applied", which is the single line that
    would have caught the #634 incident: one no-match among five successes.

    `ops == 0` means no mutating op was accounted for and the state cannot be
    determined — this declines rather than guessing a tidy `0 writes`, per the
    three-state contract in docs/validators.md.

    `skipped` names the third state for the ops themselves (#680). `ops` vs
    `writes` already carried the information, but only as a subtraction the
    reader had to perform while suspicious: a batch that reported
    `6 ops run, 4 writes` had dropped two edits, and the branch went to CI.
    A count you must diff is not a signal; a word is. So the word appears
    whenever an op declined, and never otherwise — `0 skipped` on the green
    path is exactly the kind of number a reader learns to stop seeing, which
    is how `4 writes` failed in the first place.

    `re-applied` names a fourth state (#701): the op wrote, and what it wrote
    was already there. Re-running a payload whose anchor survives its own edit
    applies the edit a SECOND time, and pre-fix the two runs differed only by a
    line range — so an identical footer read as "the same thing happened" when
    what happened was another mutation. It is a separate word from `skipped`
    deliberately: a skipped op left the disk alone, a re-applied one did not,
    and collapsing them would degrade `skipped` into "something was odd".

    It is also NOT a failure. `_SKIP_COUNT` drives the exit code so a `&&` chain
    stops on a half-applied batch; a re-apply is a legitimate outcome (appending
    a second repeated element has exactly this shape), so it discloses and exits
    0. Refusing would be guessing at intent, which is the line #680 drew too.

    `not_checked` names a fifth state, and it IS a failure (#665). It is about
    the checkers rather than the op: the edit landed, and a validator the
    operator named in `$SUPERTOOL_REQUIRE_VALIDATORS` produced no verdict about
    the file. Before this, that case reached the reader as
    `1 err  (pre-existing — not from this edit)` in the block above and as
    `1 op run, 1 write` here, and exited 0 — so the one knob that exists to
    stop "the gate is not running" from reading as a pass read as a pass. The
    validators are named on the line rather than counted, because the line has
    to be actionable on its own for `| tail -1` to be the documented read.

    It is also the one state that can reach a reader with `ops == 0`, and so it
    is the one exception to the bail below (#969). `validate:` mutates nothing,
    so it had no footer at all: its non-verdict was visible in the row and in
    the exit code, and absent from the line the docs tell readers to trust. The
    counts stay off that line — there was no op to count.

    `validated` is what makes that footer unconditional for `validate:` (#990).
    #979 emitted it only on a decline, so a clean run still ENDED on the last
    file's own row — a per-file verdict standing where a whole-run one belongs,
    on a multi-file run the same defect as #970's forged row. `0 ops run,
    0 writes` remains the wrong summary of a read-only op (#621), so this branch
    reports what a `validate:` run actually produced instead:

    ```
    [result] 3 files, 1 with findings, 0 not checked
    ```

    File counts, and they do not partition — one file can hold a finding AND a
    checker that declined. `not checked` counts a file where at least one
    validator returned no verdict, `skipped` included: #665 refused to
    *escalate* an optional tool nobody installed, and disclosing it on a count
    line escalates nothing. It is shown as a `0` rather than suppressed, which
    is the opposite of the rule `skipped` follows above and deliberately so — on
    a line whose entire content is counts, the not-checked slice is the one a
    reader must be able to find without knowing whether it fired, and a run that
    could not check something has to say so whether or not anything failed.

    `NOT RUN` stays absent from a clean run. It is the token consumers grep for,
    and rendering `0 validators NOT RUN` would put it in the output of every
    green validate — #621's zero-nobody-reads with a live tripwire attached. The
    #979 clause is appended after the counts when it fires, not instead of them.

    `rolled_back` names a sixth state (#952): the op matched, wrote, failed a
    validator and was reverted. `writes` already excluded it — `_retract_write`
    decrements — but a count is only a signal when the reader is already
    suspicious. In the single-op case the exclusion rendered as a sentence
    (`0 writes — nothing changed on disk`); in a batch where other ops did
    write it rendered as `3 ops run, 2 writes`, an arithmetic mismatch the
    reader has to notice AND explain. It is not `skipped`: a skipped op
    declined and left the disk alone, a rolled-back one wrote and had the write
    undone, and the remedies differ (fix the anchor vs fix the code). It is not
    folded into `nothing changed on disk` either, because a no-match prints
    exactly that too — the two most confusable outcomes on this path.
    """
    if ops <= 0:
        names = list(dict.fromkeys(not_checked or ()))
        files = list(validated or ())
        if files:
            n = len(files)
            with_findings = sum(1 for _p, f, _nv in files if f)
            unchecked = sum(1 for _p, _f, nv in files if nv)
            line = (f"[result] {n} file{'' if n == 1 else 's'}, "
                    f"{with_findings} with findings, {unchecked} not checked")
            if names:
                line += (f" — {len(names)} validator"
                         f"{'' if len(names) == 1 else 's'} "
                         f"NOT RUN ({', '.join(names)}) — those validators "
                         f"returned no verdict, so the file was NOT checked")
            return line + chr(10)
        if not names:
            return ""
        return (f"[result] {len(names)} validator{'' if len(names) == 1 else 's'} "
                f"NOT RUN ({', '.join(names)}) — those validators returned no "
                f"verdict, so the file was NOT checked" + chr(10))
    line = (f"[result] {ops} {'op' if ops == 1 else 'ops'} run, "
            f"{writes} {'write' if writes == 1 else 'writes'}")
    if skipped > 0:
        line += f", {skipped} skipped"
    if rolled_back > 0:
        line += f", {rolled_back} rolled back"
    if reapplied > 0:
        line += f", {reapplied} re-applied"
    files = list(validated or ())
    if files:
        # A `validate:` op inside a batch that also mutated something. Its
        # counts have nowhere else to go — an inner op is at dispatch depth > 1
        # and never renders a footer of its own — so #990's guarantee would
        # have a hole exactly where a reader is least likely to notice it.
        line += (f", validated {len(files)} file"
                 f"{'' if len(files) == 1 else 's'} "
                 f"({sum(1 for _p, f, _nv in files if f)} with findings, "
                 f"{sum(1 for _p, _f, nv in files if nv)} not checked)")
    names = list(dict.fromkeys(not_checked or ()))
    if names:
        line += (f", {len(names)} validator{'' if len(names) == 1 else 's'} "
                 f"NOT RUN ({', '.join(names)})")
    tails = []
    if writes == 0:
        # A rolled-back write is not a second application of anything, so this
        # clause wins: the bytes complained about are no longer on disk.
        tails.append("nothing changed on disk")
    elif reapplied > 0:
        tails.append("an edit already present in the file was applied again")
    if rolled_back > 0:
        tails.append(
            f"{rolled_back} edit{'' if rolled_back == 1 else 's'} "
            f"{'was' if rolled_back == 1 else 'were'} reverted after "
            f"validation and did NOT land")
    if names:
        tails.append("those validators returned no verdict, so the file was "
                     "NOT checked")
    if tails:
        line += " — " + "; ".join(tails)
    return line + "\n"


def _normalise_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _edit_miss_diagnostic(old: str, content: str, new: str = "") -> str:
    """Why `old` didn't match (#380).

    `ERROR: old string not found` was the whole message, so the natural next
    move was a `read` round-trip — the one the payload route exists to save.
    These are the ways a payload comes back close but not exact, ranked by how
    often each was the real cause.
    """
    hints: List[str] = []

    # 0. The replacement is ALREADY in the file — a re-run of a payload that
    #    landed. #701 covers the case where `new` contains `old` (the anchor
    #    survives and the edit applies a second time); this is the other half,
    #    where it does not, and the second run reports a bare no-match that is
    #    character-for-character what a genuinely wrong anchor prints (#984).
    #    The two have opposite remedies — one is done, the other needs a new
    #    anchor — and the reader could not recover which from the message.
    #
    #    A located fact, not a verdict: the ERROR stands, the exit code stands,
    #    the op is still counted as skipped. Downgrading a failure to a note
    #    because it is probably benign is how a loud bug becomes a quiet one.
    if new and new.strip() and new in content:
        at = content.count("\n", 0, content.index(new)) + 1
        hints.append(
            f"the replacement text is ALREADY present at line {at} — this "
            f"looks like a re-run of an edit that already applied, not a "
            f"broken anchor"
        )

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

# How the running dispatch frame separated its fields — ':::', ':', or '' when
# the fields arrived structured through a payload and nothing was tokenized.
# Read by `_resolve_custom_op`, which is several frames down and sees only the
# already-split `parts`, and exported to the preset subprocess as
# SUPERTOOL_ARG_SEP. A preset that reconstructs the caller's input for a
# refusal has to know which separator to rejoin on: git-commit rejoined on ':'
# whatever the route, so a ':::' inside a message came back as a ':' and the
# suggested repair, pasted, committed bytes the caller never wrote (#946).
_ARG_SEP: List[str] = [":"]

# Exit status of the last custom/preset op run in the current frame, or None
# when none ran. A preset writes no file through `_atomic_write`, so the write
# counter below cannot answer "did it succeed"; this is recorded at the
# subprocess rather than parsed back out of its receipt, for the same reason
# `_result_line`'s counts are.
_CUSTOM_OP_OK: List[Optional[bool]] = [None]

# Bumped by _atomic_write. Lets dispatch ask 'did this op actually write?'
# instead of sniffing the receipt for an ERROR prefix — receipts are prose
# and not every no-op failure says ERROR (op_replace's zero-match returns
# "(0 occurrences of 'x' found)").
_WRITE_COUNT: List[int] = [0]

# Bumped where a mutating op DECLINES: it ran, it could have written, and it
# deliberately left the disk alone (#680). The third state docs/validators.md
# already defines for validators — `ok`, a finding, `skipped` — applied to the
# ops themselves.
#
# Declared at the decline, never inferred from `attempts - writes`. That
# subtraction looks equivalent and is not: a multi-file `replace` writes more
# times than it was attempted, `replace_dry` writes nothing by design, and a
# validator rollback retracts a write that was genuinely made. Each would be
# mis-reported as a decline, and a skip count that is sometimes wrong is worse
# than none — it is the same absence-read-as-fact this counter exists to stop.
_SKIP_COUNT: List[int] = [0]

# Bumped where a mutating op wrote text the file ALREADY contained at that spot
# (#701) — the fourth state, and the only one that is not a decline. See
# `_edit_already_applied` for why the test is positional rather than
# `new in content`, and `_result_line` for why it is not folded into
# `_SKIP_COUNT`. Never decremented on rollback: `_retract_write` is per-path and
# this counter is not, so a batch rollback could retract the wrong op's
# disclosure. The `writes == 0` clause in `_result_line` covers that case
# honestly instead — "nothing changed on disk" is the stronger statement.
_REAPPLY_COUNT: List[int] = [0]

# Bumped where a write is REVERTED after the fact (#952): the op matched, the
# bytes landed, a validator with `rollback_on_fail` regressed, and the previous
# content was restored. The sixth state, and the one that had no name — see
# `_result_line` for why it is neither `skipped` nor covered by `writes`.
#
# Bumped inside `_retract_write`, which is the single chokepoint both rollback
# paths (formatter and validator) already pass through. Bumping at the two call
# sites instead would make a third rollback path added later silently invisible,
# which is the shape of defect this counter exists to close.
_ROLLBACK_COUNT: List[int] = [0]

# Validators that ran and returned no verdict about the file (#665). The state
# `skipped` covers a checker that declined before running; this covers one that
# was asked to run, could not, and had only an `adapter` error to say so with.
#
# Names, not a count, because the reader's next action is installing a tool:
# `2 validators NOT RUN` alone sends them back up to the block this footer
# exists to save them from re-reading.
#
# Appended through `_acc_not_checked()`, which routes to the running op's own
# dispatch frame and reaches this list only when the frame unwinds (#1109). So
# what accumulates here is the whole CALL, which is exactly the scope the exit
# code below wants: the warm daemon reuses the process, so `main` reads a
# per-call delta and truncates back, or one ungated edit would poison the exit
# code of every later call in the same worker (#680). The FOOTER's scope is one
# op, and it is built from the frame instead — a `len()` snapshot here was only
# ever per-op while a single op was appending, which stopped being true the day
# `validate` joined `_PARALLEL_SAFE_OPS`.
_NOT_CHECKED: List[str] = []

# One entry per file a `validate:` op rendered a block for, as
# `(path, had_finding, had_non_verdict)` — the material for the whole-run
# verdict `validate:` had no line for at all on the clean path (#990).
#
# Recorded here rather than re-derived from the rendered rows, for the reason
# `_result_line`'s counts already are: a footer parsed back out of the prose it
# is summarising can only ever agree with the prose, which is exactly the thing
# under suspicion when a reader reaches for it.
#
# Per-call, and reached through the dispatch frame, like `_NOT_CHECKED` above
# and for the same two reasons — the warm daemon reuses the process, and the
# footer's scope is one op rather than one call (#1109).
_VALIDATED_FILES: List[Tuple[str, bool, bool]] = []


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
    _ROLLBACK_COUNT[0] += 1
    if _WRITE_COUNT[0] > 0:
        _WRITE_COUNT[0] -= 1


def _rollback_action(pre_existed: bool, pre_content: Optional[bytes]) -> str:
    """`restore` | `unlink` | `refuse` — what undoing this write actually means.

    Three states, because the two the rollback loop used to have were "we have
    prior bytes" and "we do not", and it read the second as "there is nothing to
    do" (#1088). A created file has no prior bytes and still has an undo: unlink.
    So a `paste` of a new file whose content did not parse printed the red row,
    printed no retraction, and left the artifact — the receipt saying the write
    did not survive validation while the filesystem said it had.

    `unlink` is a delete, so it is gated on provenance rather than on the
    absence of a baseline. `pre_existed` is sampled before the op runs and is
    the only thing that can tell "a path this call brought into being" from "a
    path that was here and got overwritten". Deleting the second would turn a
    rollback into destruction of work the call never wrote, which is the one
    outcome this repository ranks below misreporting.

    The third state is the file that existed and whose bytes could not be read.
    Neither undo is available and there is no safe guess, so the caller is told
    rather than left with a silent skip that reads as "nothing needed doing".
    """
    if not pre_existed:
        return "unlink"
    if pre_content is not None:
        return "restore"
    return "refuse"


def _retraction_line(name: str, verb: str, path: str, body: object,
                     created: bool = False, target: str = "") -> str:
    """Retract a receipt's own success line where a FILTERED read will see it.

    `[rolled back] <tool> regressed; file restored` shares no token with the
    `edited <file> (line N)` printed above it, so `grep -E 'edited|ERROR'` —
    the reported way this output is read — returned the claim and not the undo
    (#952). Quoting the retracted line back means any filter that caught the
    claim catches the retraction, adjacent to it and in the same stream
    position.

    Deliberately NOT by suppressing the claim. An absent line makes "written,
    then reverted" indistinguishable from "never ran", which is the same
    absence-read-as-fact defect wearing different clothes.

    Separator-agnostic: the path is echoed as the op received it, so a Windows
    `pkg\\x.py` renders as the caller typed it. Both interpolated strings go
    through `_flat_cell` because this is a column-0 marker line — the rule
    docs/validators.md states for `[validators]` rows applies verbatim here,
    and a path is attacker-influenceable input.

    Plain double quotes rather than `repr()` around the retracted line: repr
    doubles every backslash, so a Windows receipt would retract
    `'edited pkg\\\\x.py (line 2)'` — text that no longer matches the line it
    is retracting, which defeats the whole point of quoting it.
    """
    head = ""
    if isinstance(body, str):
        for ln in body.splitlines():
            if ln.strip():
                head = _flat_cell(ln.strip())
                break
    quoted = f' — retracts "{head}"' if head else ""
    # `removed` / `NOT created` on the create path (#1088). "restored" names an
    # earlier state, and a file this call brought into being has none — a reader
    # deciding what to do next needs to know the path is now absent, not that it
    # went back to something.
    undo = "removed" if created else "restored"
    tail = "created" if created else "edited"
    # A write through a symlink lands on the target, so the undo does too
    # (#1136). Saying "link.py removed" was true only while the rollback was
    # deleting the wrong object; now that it deletes the right one, the same
    # sentence would tell a reader their symlink is gone when it is intact.
    subject = _flat_cell(path)
    via = ""
    if target and os.path.abspath(target) != os.path.abspath(path):
        subject = _flat_cell(target)
        via = (f" ({_flat_cell(path)} is a symlink, so the write landed on its "
               f"target and that is what was undone; the link is intact)")
    return (f"[rolled back] {name} {verb}; {subject} {undo}{via}{quoted}"
            f"; the file was NOT {tail}")


def _elide(s: str, limit: int) -> str:
    """One-line, length-capped rendering of an op argument for a header.

    Reports the elided character count rather than trailing off — a silent
    truncation reads as "that was the whole argument".
    """
    s = s.replace("\r\n", "⏎").replace("\n", "⏎")
    if len(s) <= limit:
        return s
    return f"{s[:limit]}… (+{len(s) - limit} chars)"


def _commit_header_arg(parts: List[str]) -> str:
    """`git-commit`'s header, for a commit that LANDED (#946, #1235).

    Here the argument is also the artifact: after a successful commit
    `git log -1` hands the message back, so replaying it above a receipt
    whose whole job is to prove the commit landed is a second copy of
    something already retrievable. Commit messages in this repo are long by
    convention, so that echo routinely outweighs the receipt it introduces.

    Success only, and that is the point rather than a detail. On a refusal
    nothing was committed and this header is the ONLY surviving copy of a
    message the caller composed — eliding it there would cause precisely the
    loss #1235 was filed about. The swap in `_dispatch_impl` is gated on the
    op having succeeded for that reason.

    Subject plus a body line COUNT, never a body sample. A sample reads as
    the message and is not; the count is what tells the reader the tool
    received the lines they sent, which is the whole job of an echo.
    """
    msg = parts[1] if len(parts) > 1 else ""
    paths = [p for p in parts[2:] if p]
    # CRLF normalised first: `split` on the newline alone leaves a trailing
    # CR on every line, and `_elide` only collapses the pair — so a message
    # composed on Windows would put a stray CR inside the quoted subject.
    lines = msg.replace(chr(13) + chr(10), chr(10)).split(chr(10))
    out = 'git-commit: "' + _elide(lines[0], _HEADER_ANCHOR_MAX) + '"'
    if len(lines) > 1:
        out += f" +{len(lines) - 1} more message lines"
    if paths:
        out += (f" → {len(paths)} path(s): "
                + _elide(", ".join(paths), _HEADER_ANCHOR_MAX))
    else:
        out += " → no paths (commits the index)"
    return out


def _compact_header_arg(op: str, parts: List[str], sep: str = ":") -> str:
    """Identifying header for a content-heavy mutating op, or "" to keep the
    verbatim one. Each op keeps whatever identifies the *target* — path, line
    range, anchor — and drops the content the diff is about to show anyway.

    *sep* is how this call's fields were split. It gates `git-commit` only:
    under single-colon tokenization `parts[1]` is a fragment of the message
    rather than the message, so a summary built from it would state a subject
    the caller never wrote.
    """
    def _p(i: int) -> str:
        return parts[i] if len(parts) > i else ""

    if op == "git-commit":
        return _commit_header_arg(parts) if sep == ":::" else ""
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


def _payload_header_arg(op: str, target: str) -> str:
    """Header for a sub-op that arrived through an @payload, not the colon CLI.

    A batch sub-op used to be echoed back as its fields joined on ':'. The
    payload route exists *because* the content contains ':', so that join does
    not merely lose information — it produces a string that parses as a
    DIFFERENT op. `replace` on `time: 10:30` rendered as

        --- replace:time: 10:30:time: 11:45:/tmp/h.txt ---

    which, pasted back, sends the dispatcher looking for a file named `30`.
    A header is the thing a reader trusts to reconstruct what happened, and in
    a bug report it is often the only surviving record. Inviting them to run an
    op that touches a path nobody named is worse than telling them nothing.

    So this does not attempt a faithful one-line colon rendering — for a
    payload op there isn't one. It names the ROUTE and the TARGET, which is
    what identifies the step, and the `@payload` reference does not resolve to
    a file, so pasting it fails loudly instead of quietly doing something else.
    That is the invariant: a header must never be a runnable string that runs
    something other than what ran; if it cannot be re-runnable it must not look
    re-runnable. Same family as #621 — output presenting itself as a faithful
    account of an operation and not being one. #644.
    """
    return f"{op}:@payload" + (f" → {target}" if target else "")


# Positional colon-argument order for batch sub-ops that have no @payload
# route of their own. Only ops whose colon syntax takes more than one argument
# need an entry; single-argument ops are unambiguous without one. Ordering here
# mirrors the `syntax` strings in .supertool.json.
_BATCH_POSITIONAL_FIELDS: Dict[str, Tuple[str, ...]] = {
    "head":        ("path", "n"),
    "tail":        ("path", "n"),
    "tree":        ("path", "depth"),
    "around_line": ("path", "line", "n"),
    "diff":        ("path1", "path2"),
}


def _ordered_batch_fields(op: str, item: Dict[str, Any]) -> Tuple[List[str], str]:
    """Positional colon fields for a batch sub-op with no @payload route.

    Returns (fields, error) — exactly one is non-empty.

    This replaces `sorted(item)`, which ordered the payload's fields
    ALPHABETICALLY and is not any op's argument order. That was not a header
    defect: it is the arg that gets dispatched. `{"op": "tree", "path": "src/",
    "depth": 2}` ran as `tree:2:src/`, and `between` with `symbol` + `path` ran
    as `between:<path>:<symbol>` — silently searching for the file inside the
    symbol name.

    Where the order is declared it is used; where it is not, this declines
    rather than inventing one. Guessing is how the original defect happened.
    #644.
    """
    lower = {str(k).lower(): v for k, v in item.items() if str(k).lower() != "op"}
    if not lower:
        return [], ""
    order = _BATCH_POSITIONAL_FIELDS.get(op)
    if order is None:
        if len(lower) == 1:
            return [str(next(iter(lower.values())))], ""
        return [], (
            f"ERROR: batch sub-op '{op}' takes its arguments positionally and has "
            f"no declared payload field order, so {', '.join(sorted(lower))} "
            f"cannot be placed. Ordering them alphabetically is a guess, and a "
            f"wrong guess dispatches a different op — so this declines instead. "
            f"Use the colon form for '{op}', or an op with an @payload route.\n"
        )
    unknown = sorted(k for k in lower if k not in order)
    if unknown:
        return [], (
            f"ERROR: unknown field(s) {', '.join(unknown)} in batch '{op}' "
            f"— accepted: {', '.join(order)}\n"
        )
    fields: List[str] = []
    for name in order:
        if name not in lower:
            break
        fields.append(str(lower[name]))
    skipped = [n for n in order[len(fields):] if n in lower]
    if skipped:
        return [], (
            f"ERROR: batch '{op}' payload sets {', '.join(skipped)} without the "
            f"earlier positional field(s) {', '.join(order[len(fields):order.index(skipped[0])] or order[:1])} "
            f"— colon arguments cannot be sparse.\n"
        )
    return fields, ""


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
        f"runs differently. This is a note and not a refusal: these bytes "
        f"have no second spelling here, so blocking them would strand the "
        f"caller who meant them. A literal payload block refuses the same "
        f"pattern, because there a second spelling exists (#835).\n"
    )


def _edit_already_applied(content: str, old: str, new: str, idx: int) -> bool:
    """Is the `old` at `idx` sitting INSIDE text this same edit already made?

    The re-run case (#701): `new` contains `old`, so the anchor survives its own
    edit and matches again on a second run. `content[idx:idx+len(old)]` is then
    literally a substring of an occurrence of `new` that a previous run wrote.

    Positional containment, not `new in content`. The issue floated the simpler
    test and it has a real failure mode: `new` may legitimately pre-exist
    somewhere unrelated (an edit inserting `return None` into a file that
    already has a `return None` elsewhere), and a signal that fires on a first
    application is noise — which is how a footer count stops being read. Asking
    instead whether the anchor being replaced is bracketed by an existing copy
    of `new` is the literal statement "this edit's result is already here".

    Deliberately says nothing about intent. An append of a second repeated
    element is indistinguishable from an accidental re-run and must stay
    allowed; the caller decides, the tool discloses.
    """
    if len(new) <= len(old) or old not in new:
        return False
    end = idx + len(old)
    j = content.find(new)
    while j != -1 and j <= idx:
        if end <= j + len(new):
            return True
        j = content.find(new, j + 1)
    return False


def _newline_census(text: str) -> Tuple[int, int, int]:
    """`(crlf, lf, cr)` — how many of each line ending the text actually has.

    Bare counts, not a verdict. Callers that need one say in their own receipt
    what they did with it; nothing here decides on their behalf (#1049).
    """
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    return crlf, lf, cr


def _newline_used(text: str) -> str:
    """The ending convention a block carries, CRLF-first, "" if it carries none.

    CRLF-first because a block holding both is a caller's own mixture written
    verbatim, and naming the two-byte ending is the one that cannot be read as
    a bare LF.
    """
    for nl in ("\r\n", "\r", "\n"):
        if nl in text:
            return nl
    return ""


def _newline_note(content: str, wrote: str = "", retermed: bool = False) -> str:
    """One receipt line, emitted only where the ending used had more than one
    defensible answer.

    `content` is the file **as it will be on disk** — not as it was found.
    Asking the pre-write bytes meant the mixed branch below could only fire on
    a file that was *already* mixed, so a write that CREATED the mixedness
    reached nothing and shipped in silence (#1075). The census printed with it
    describes the file that now exists, which is the one the reader is about to
    open.

    `wrote` is the convention the op used for text it supplied, "" when it
    supplied none. `retermed` says that text was the *caller's own*, rewritten
    to match the file.

    The first cut of this fired on every successful edit of a CRLF file — where
    nothing had been decided, every byte outside the match was unchanged, and
    the line said so at length. On Windows every file is CRLF, so a marker
    meaning "I made a choice you did not" appeared on every call ever made,
    which is how a disclosure becomes noise and then becomes ignored. It also
    collided with `test_successful_edit_has_no_diagnostic`, which reads `↳` as
    "a diagnostic was emitted" and was right to.

    So: a mixed file has no single answer and is always disclosed; re-writing
    the caller's own endings is always disclosed; supplying a trailing newline
    in the one convention a uniform file uses is not a choice and is silent.
    """
    if not wrote:
        return ""
    names = {"\r\n": "CRLF", "\n": "LF", "\r": "CR"}
    name = names.get(wrote, repr(wrote))
    crlf, lf, cr = _newline_census(content)
    if len([n for n in (crlf, lf, cr) if n]) > 1:
        return (f"  {mark('↳')} line endings: file is mixed ({crlf} CRLF / "
                f"{lf} LF / {cr} CR) — every line this op did not touch kept "
                f"its own; text this op supplied uses {name}\n")
    if not retermed:
        return ""
    return (f"  {mark('↳')} line endings: file is {name}, so the text you "
            f"wrote with LF was re-terminated to {name} to match — every "
            f"untouched line is unchanged\n")


def _newline_variants(text: str) -> List[Tuple[str, str]]:
    """`(newline, text)` candidates for a match, most faithful first.

    A caller composing a payload types LF, so an `old` string describing lines
    of a CRLF file arrives LF-terminated and matches nothing once the read
    stops flattening the file. Refusing there would trade a silent whole-file
    rewrite for a hard `old string not found` — a different bug, not a fix. The
    literal text is therefore tried first and the re-terminated forms only
    after it, so a file that matches exactly is never reinterpreted.
    """
    flat = text.replace("\r\n", "\n").replace("\r", "\n")
    out: List[Tuple[str, str]] = [("", text)]
    for nl in ("\r\n", "\r", "\n"):
        cand = flat.replace("\n", nl)
        if cand != text:
            out.append((nl, cand))
    return out


def _retermed(text: str, nl: str) -> str:
    """`text` with every line ending replaced by `nl`."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", nl)


def _line_number_at(text: str, end: Optional[int] = None) -> int:
    """1-based line number of position `end`, counting CR, LF and CRLF alike.

    `text.count("\\n")` is zero at *every* position in a CR-only file, so a
    receipt built on it named line 1 wherever the match actually was. That
    arithmetic was correct only because the read used to translate `\\r` to
    `\\n` before it ran; `newline=""` removed the translation and left the
    count behind (PR #1057 review).

    Counted rather than sliced: `count` takes a range, so this stays
    allocation-free on the dry-run path that calls it once per match.
    """
    return (text.count("\n", 0, end)
            + text.count("\r", 0, end)
            - text.count("\r\n", 0, end)) + 1


def _local_newline(lines: List[str], idx: int) -> str:
    """The line ending in force at `lines[idx]`, scanning backwards for one.

    A mixed file has no single answer, so `replace_lines` does not vote on the
    whole file: the block it writes takes the ending of the line it replaces
    (or, for an insert, of the line above it). A file-wide majority would
    rewrite the caller's own line to the other convention, which is the same
    silent normalisation one line wide.

    The backwards scan is the answer whenever it finds one. When it does not —
    `idx` is at or past the only terminated line, or every line above it is
    unterminated — the search continues *forwards* over the whole file, so a
    file whose endings all sit below `idx` still gets its own convention.
    Falls back to LF only when no line anywhere in the file is terminated.
    """
    for i in range(min(idx, len(lines) - 1), -1, -1):
        line = lines[i]
        for nl in ("\r\n", "\r", "\n"):
            if line.endswith(nl):
                return nl
    for line in lines:
        for nl in ("\r\n", "\r", "\n"):
            if line.endswith(nl):
                return nl
    return "\n"


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
        # newline="": no universal-newline translation, so a CRLF file is not
        # silently rewritten to LF throughout — the contract op_append already
        # states. Without it a one-line edit changed every line in the file,
        # under a receipt that named one (#1049).
        with open(path, "r", encoding="utf-8", errors="surrogateescape",
                  newline="") as f:
            content = f.read()
    except OSError as e:
        return f"ERROR: failed to read {path}: {e}\n"

    wrote = ""
    count = content.count(old)
    if count == 0:
        for nl, cand in _newline_variants(old)[1:]:
            n = content.count(cand)
            if n:
                old, new, wrote, count = cand, _retermed(new, nl), nl, n
                break
    if count == 0:
        _SKIP_COUNT[0] += 1
        return (f"ERROR: old string not found in {path}\n"
                + _edit_miss_diagnostic(old, content, new))
    if count > 1:
        _SKIP_COUNT[0] += 1
        return (
            f"ERROR: old string found {count} times in {path} — ambiguous. "
            f"Use a larger snippet to make it unique, or use replace for "
            f"replace_all semantics.\n"
        )

    retermed = bool(wrote)
    if not wrote and ("\n" in new or "\r" in new):
        # `old` matched literally, so nothing above resolved a convention --
        # but `new` carries one of its own, and writing it verbatim splices LF
        # into a CRLF file and leaves it mixed, silently.
        #
        # The defect this closes is an inconsistency, not just a silence: an
        # `old` that spanned a line boundary went through `_newline_variants`
        # and had `new` re-terminated with whatever matched, while a
        # single-line `old` did not. Two edits expressing the same intent
        # produced different bytes depending on that accident (PR #1057
        # review). Only reachable since this branch stopped flattening every
        # write to LF, which is why it is this branch's regression to fix.
        crlf, lf, cr = _newline_census(content)
        used = [n for n, c in (("\r\n", crlf), ("\n", lf), ("\r", cr)) if c]
        if len(used) == 1 and _retermed(new, used[0]) != new:
            # One convention, and `new` disagrees: match the file and say so.
            # This is the caller's own text rewritten, which `_newline_note`
            # promises is never silent.
            new, wrote, retermed = _retermed(new, used[0]), used[0], True
        elif len(used) > 1:
            # Mixed: no single convention to match, so the caller's bytes stand
            # as typed -- the same answer `replace_lines` gives. Still a
            # decision, and `_newline_note`'s mixed branch discloses it.
            wrote = _newline_used(new)

    idx = content.index(old)
    reapplied = _edit_already_applied(content, old, new, idx)

    new_content = content.replace(old, new, 1)
    try:
        _atomic_write(path, new_content)
    except OSError as e:
        return f"ERROR: failed to write {path}: {e}\n"

    if reapplied:
        _REAPPLY_COUNT[0] += 1

    # Receipt — locate the change and show ±2 lines context
    start_line = _line_number_at(content, idx)
    # `_line_number_at` counts LF/CR/CRLF; the receipt's own line list has to
    # count the same ones or the context block is indexed against a numbering
    # the `line N` above it was never built from (#1060).
    new_lines = _split_lines(new_content)
    new_block_line_count = _line_number_at(new)
    end_line = start_line + new_block_line_count - 1
    ctx_start = max(1, start_line - 2)
    ctx_end = min(len(new_lines), end_line + 2)

    out = [f"edited {path} (line {start_line}"]
    if end_line != start_line:
        out.append(f"-{end_line}")
    out.append(")\n")
    _nl_note = _newline_note(new_content, wrote, retermed=retermed)
    if _nl_note:
        out.append(_nl_note)
    # Attached to the claim it qualifies, not only to the footer: `edited a.py
    # (line 2-3)` is a true sentence that reads as a first application, and the
    # reader who is about to trust it is looking here. The footer carries the
    # same signal for the reader who is piping to `tail` (#621).
    if reapplied:
        out.append(
            f"  {mark('↳')} re-applied: the text this edit produces was already "
            f"present around the anchor — this is a SECOND application, not a "
            f"repeat of the first\n"
        )
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

    all_lines = _split_lines(new_content)
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
        # newline="": the lines this op does not name must come back out byte
        # for byte, CRLF included (#1049).
        with open(path, "r", encoding="utf-8", errors="surrogateescape",
                  newline="") as f:
            orig = f.read()
    except OSError as e:
        return f"ERROR: failed to read {path}: {e}\n"

    # The shared definition (#1060): `str.splitlines` breaks on eight more
    # characters than the byte-level split `read` renders with, so a file
    # holding one of them was numbered one way for the reader and another way
    # for this write. Both sides go through `_split_lines_keepends` now.
    orig_lines = _split_lines_keepends(orig)
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

    # The caller's content goes in verbatim, mixed endings and all: the
    # endings inside the block they typed are their choice, and rewriting an
    # explicit choice is the same silent normalisation #1049 is about pointed
    # the other way. `test_mixed_line_endings_preserved` states this contract
    # and predates the issue.
    #
    # The one ending this op has to *invent* is the trailing one, when the
    # block does not end a line at all. That takes the ending of the line it
    # lands on — not LF and not a file-wide majority, which on a mixed file
    # would rewrite the caller's own line to the other convention.
    block_nl = _local_newline(orig_lines, start - 1)
    new_block = content
    if new_block and not new_block.endswith(("\n", "\r")):
        new_block += block_nl
    new_block_lines = _split_lines_keepends(new_block) if new_block else []

    if insert_only:
        before = orig_lines[: start - 1]
        after = orig_lines[start - 1:]
        removed = 0
    else:
        before = orig_lines[: start - 1]
        after = orig_lines[end:]
        removed = end - start + 1

    new_lines = before + new_block_lines + after
    new_content = "".join(new_lines)
    try:
        _atomic_write(path, new_content)
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
    # The block as written, not merely the ending this op had to invent: a
    # block that already ended a line invented nothing, left `wrote` empty, and
    # short-circuited the note before it ever reached the census — the same
    # mixed file under the same silence (#1075).
    _nl_note = _newline_note(new_content,
                             _newline_used(new_block) if added else "")
    if _nl_note:
        out.append(_nl_note)

    ctx_start = max(1, new_start - 2)
    ctx_end = min(len(new_lines), max(new_end, new_start) + 2)
    for ln in range(ctx_start, ctx_end + 1):
        marker = "→" if added > 0 and new_start <= ln <= new_end else " "
        # rstrip("\r\n"), not rstrip("\n"): on a CRLF file the latter leaves
        # the carriage return inside the receipt, shipping a stray CR into the
        # caller's terminal and logs on every context line (PR #1057 review).
        text = new_lines[ln - 1].rstrip("\r\n")
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

_LINT_DECLINE_PREFIXES = (
    "--- POST-EDIT LINT TIMED OUT",
    "--- POST-EDIT LINT DECLINED",
)


def _lint_timeout() -> int:
    """Post-edit lint subprocess timeout, overridable per environment (#396).

    A slow runner (Windows antivirus scanning a freshly written temp file is
    the usual suspect) needs room without a code change.
    """
    return _env_int("SUPERTOOL_LINT_TIMEOUT", _LINT_TIMEOUT_DEFAULT, minimum=1)


def _lint_declined(tool: str, reason: str) -> str:
    """The checker applies to this file and could not be run (#559).

    Distinct from silence, which says no checker applies, and from FAILED,
    which says one ran and found something. Naming the tool and the reason is
    what makes it actionable; saying the file was NOT checked is what stops it
    being read as a pass.
    """
    return (
        f"--- POST-EDIT LINT DECLINED — {tool} ---\n"
        f"{reason}; the file was NOT checked.\n"
    )


def _vim_render_lint(path: str) -> str:
    """Post-edit syntax lint based on file extension.

    Returns "" when no lint applies — an unknown extension, or a binary absent
    from PATH so nothing was ever going to check this file. That is the one
    silence: it means clean, and only that.

    On success: '--- lint: <tool> ---\\n<output>\\n'.
    On timeout: '--- POST-EDIT LINT TIMED OUT — <tool> (<N>s) ---' (#396) —
    never "", which would read as a file that linted clean.
    On failure: '--- POST-EDIT LINT FAILED — <tool> ---\\n<output>\\n'.
    On a checker that applies but could not be run: '--- POST-EDIT LINT
    DECLINED — <tool> ---' (#559). A file whose linter exists and did not run
    is not the same as a file with no linter, and must not render the same.

    The Python interpreter is `sys.executable`, never a PATH lookup of
    "python3" (#529/#559): on Windows that name resolves to the App Execution
    Alias stub — which blocks rather than errors — or to nothing at all, and
    either way a valid file gets a verdict nobody computed. The running
    interpreter is present by construction, is Python 3 by construction, and
    is never a stray Python 2 or the wrong venv.

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
        tool = "py_compile"
        if not sys.executable:
            return _lint_declined(
                tool,
                "no Python interpreter to run it with (sys.executable is empty)",
            )
        cmd = [sys.executable, "-m", "py_compile", path]
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
            timeout=timeout, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return (
            f"--- POST-EDIT LINT TIMED OUT — {tool} ({timeout}s) ---\n"
            "lint did not run to completion; the file was NOT checked. "
            "Raise SUPERTOOL_LINT_TIMEOUT if this recurs.\n"
        )
    except OSError as e:
        detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        return _lint_declined(tool, f"could not start the checker ({detail})")

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
                raise ValueError(f"bad offset {addr!r}") from None
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
                raise ValueError(f"bad offset {addr[split_idx:]!r}") from None
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
        # Atomic by contract: an errored vim op applied none of its actions, so
        # every one of them is a decline the footer has to carry (#680).
        _SKIP_COUNT[0] += 1
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
        \\\\e        → literal `\\e` (escapes ESC; needed for Windows paths, e.g. `\\emit.py`)

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
        # surrogateescape (not 'replace'): round-trip lone non-UTF-8 bytes via
        # _atomic_write. 'replace' rewrote every one of them to U+FFFD across
        # the WHOLE buffer, and vim writes the whole buffer back — so bytes the
        # script never addressed were destroyed, unrecoverably, under a receipt
        # that named one line and reported nothing (#1059). Same contract
        # op_edit / op_replace / op_replace_lines already carry.
        #
        # Deliberately still no newline="": every motion, o/O and dd below
        # assumes "\n", so adding it here converts one whole-file normalisation
        # into scattered mixed endings. That is a separate design job (#1049);
        # the byte destruction is not blocked by it.
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
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
    #
    # #501: this was a blind, mode-blind text substitution over the WHOLE
    # raw script, including content that ends up inside a greedy capture
    # (insert TEXT, ex `:!cmd`) rather than being an intentional ESC
    # marker. A literal backslash immediately followed by 'e' is
    # unremarkable in real content — most commonly a Windows path segment
    # (`\emit.py`, `\explorer.exe`, `\env`) — and previously had no way
    # to survive: `\e` always became ESC, silently truncating whatever
    # greedy capture it landed inside (e.g. a `:!` shell command cut off
    # mid-string). Mirror the `\\` -> literal `\` two-pass sentinel
    # convention `_decode_escapes` already uses: an escaped-backslash
    # form `\\e` (backslash, backslash, e) now survives as a literal
    # `\e` instead of colliding with the ESC marker.
    _esc_literal_sentinel = "\x00ESCLIT\x00"
    normalized = script.replace("\\\\e", _esc_literal_sentinel)
    normalized = normalized.replace("\\e", ESC).replace("\x1e", ESC).replace("␞", ESC)
    normalized = normalized.replace(_esc_literal_sentinel, "\\e")
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
    _V_MOTION_LINE = re.compile(r"^V(\d*)([jk])(cc|dd|yy|[dyc])(.*)$", re.DOTALL)  # anchored-ok: DOTALL, so the greedy tail already swallows a trailing newline
    # V<N>G<op> — visual-line + goto line N + op = `:.,<N><op>`.
    # E.g. `V145Gd` (line cursor through 145, delete) → `:.,145d`.
    _V_GOTO_LINE_OP = re.compile(r"^V(\d+)G([dyc])(.*)$", re.DOTALL)  # anchored-ok: DOTALL, so the greedy tail already swallows a trailing newline
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
    _V_CHAR_RE = re.compile(  # anchored-ok: DOTALL, so the greedy tail already swallows a trailing newline
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
    _CC_TYPO = re.compile(r"^cc([ia])([wWsp\"'`()\[\]{}<>bBt])(.*)$", re.DOTALL)  # anchored-ok: DOTALL, so the greedy tail already swallows a trailing newline

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
                        cmd, shell=True, capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace"
                    )
                except (OSError, _sp.TimeoutExpired) as e:
                    return f"ERROR: action {i} '{action}': :r !{cmd}: {e}\n"
                if proc.returncode != 0:
                    return (
                        f"ERROR: action {i} '{action}': :r !{cmd}: exit "
                        f"{proc.returncode}: {proc.stderr.strip()}\n"
                    )
                _bad = _undecodable_at(proc.stdout)
                if _bad >= 0:
                    return (
                        f"ERROR: action {i} '{action}': :r !{cmd}: output is not "
                        f"valid UTF-8 (first undecodable byte near offset {_bad}); "
                        "refusing to read mojibake in as file content\n"
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
                    # surrogateescape: `:r` splices this file's text into a
                    # buffer that gets written back, so 'replace' would destroy
                    # the source file's non-UTF-8 bytes on the way in (#1059).
                    with open(path_arg, "r", encoding="utf-8",
                              errors="surrogateescape") as _fh:
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
                        cmd, shell=True, capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace"
                    )
                except (OSError, subprocess.TimeoutExpired) as e:
                    return f"ERROR: action {i} '{action}': :!{cmd}: {e}\n"
                if proc.returncode != 0:
                    return (
                        f"ERROR: action {i} '{action}': :!{cmd}: exit "
                        f"{proc.returncode}: {proc.stderr.strip()}\n"
                    )
                _bad = _undecodable_at(proc.stdout)
                if _bad >= 0:
                    return (
                        f"ERROR: action {i} '{action}': :!{cmd}: output is not "
                        f"valid UTF-8 (first undecodable byte near offset {_bad}); "
                        "file NOT modified\n"
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
                        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace"
                    )
                except (OSError, subprocess.TimeoutExpired) as e:
                    return f"ERROR: action {i} '{action}': :!{cmd}: {e}\n"
                if proc.returncode != 0:
                    return (
                        f"ERROR: action {i} '{action}': :!{cmd}: exit "
                        f"{proc.returncode}: {proc.stderr.strip()}\n"
                    )
                _bad = _undecodable_at(proc.stdout)
                if _bad >= 0:
                    return (
                        f"ERROR: action {i} '{action}': :!{cmd}: output is not "
                        f"valid UTF-8 (first undecodable byte near offset {_bad}); "
                        "the filtered lines were NOT replaced\n"
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
                        raise ValueError(f"bad pattern {addr!r}: {e}") from e
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
                    with open(path, "r", encoding="utf-8",
                              errors="surrogateescape") as _fh:
                        new_content = _fh.read()
                    new_lines_full = new_content.split("\n")
                    new_total = len(new_lines_full) - (1 if new_lines_full and new_lines_full[-1] == "" else 0)
                    line_b_eff = line_b + (new_total - total_lines)
                # Re-read final content for the main loop. surrogateescape,
                # like every other read in this op: a re-read that mangles is
                # the same destruction one recursion later (#1059).
                with open(path, "r", encoding="utf-8",
                          errors="surrogateescape") as _fh:
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
                                            text=True, timeout=30, encoding="utf-8", errors="replace"
                                        )
                                    except (OSError, subprocess.TimeoutExpired) as _dot_e:
                                        log.append(f"  {i}. .(:!{_dot_cmd}) ERROR: {_dot_e}")
                                    else:
                                        if _dot_proc.returncode != 0:
                                            log.append(
                                                f"  {i}. .(:!{_dot_cmd}) ERROR exit "
                                                f"{_dot_proc.returncode}: {_dot_proc.stderr.strip()}"
                                            )
                                        elif _undecodable_at(_dot_proc.stdout) >= 0:
                                            log.append(
                                                f"  {i}. .(:!{_dot_cmd}) ERROR: output is "
                                                "not valid UTF-8 — file NOT modified"
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
                                                capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace"
                                            )
                                        except (OSError, subprocess.TimeoutExpired) as _dot_e:
                                            log.append(f"  {i}. .(:!{_dot_cmd}) ERROR: {_dot_e}")
                                        else:
                                            if _dot_proc.returncode != 0:
                                                log.append(
                                                    f"  {i}. .(:!{_dot_cmd}) ERROR exit "
                                                    f"{_dot_proc.returncode}: {_dot_proc.stderr.strip()}"
                                                )
                                            elif _undecodable_at(_dot_proc.stdout) >= 0:
                                                log.append(
                                                    f"  {i}. .(:!{_dot_cmd}) ERROR: output is "
                                                    "not valid UTF-8 — lines NOT replaced"
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
    elif lint_out.startswith(_LINT_DECLINE_PREFIXES):
        # #560: a decline means the file was written and nothing checked it.
        # Everywhere else the absence of this note means the edit came out
        # clean, so the least-verified state must not be the quietest one.
        # Worded for that state — modified and NOT checked, not modified
        # despite a failure; nothing failed here, nothing ran.
        lint_out += "[note] file modified and NOT checked — the syntax check never returned a verdict; review or restore manually. Configure a validator with rollback_on_fail for auto-rollback.\n"
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
    if op_name in _valid_op_names():
        return (f"ERROR: op '{op_name}' has no documented help in "
                f".supertool.json. It's a valid operation here — run 'ops' for "
                f"the list, or see docs/operations.\n")
    return (f"ERROR: no help for op: {op_name}\n"
            f"Run 'ops' for the full list of operations.\n")


# Threshold above which compact ops output gets a "truncation likely" warning.
# Claude Code's hook-stdout cap appears to be ~7KB; anything over that gets
# saved to disk and only a ~2KB preview is injected into the model's context,
# silently hiding the tail of the ops list. (Empirical: 6.6KB landed full,
# 11KB+ got truncated — threshold sits in between.)
_HOOK_OUTPUT_CAP_BYTES = 7168


def _configured_op_names(config: Dict[str, Any]) -> set:
    """Op names this config has an opinion about — including the ones it hides.

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
            if isinstance(info, dict):
                names.add(name)
    return names


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

    if disclosure and _CONFIG_PATH:
        lines.append(disclosure)
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
    and ``read`` and dispatch as neither.
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


_ROSTER_LEGEND = (
    "Every op loaded here, and nothing else — the complete list, which the "
    "descriptive\n`ops` listing stops being once a project has enough ops to "
    "pass the ~7KB\nSessionStart cap. Class is declared, never guessed.\n\n"
    "- unmarked — read-only. Call it blind; its own error teaches the "
    "signature.\n"
    "- `*` — writes files in this tree.\n"
    "- `!` — changes something outside this tree, or starts something that "
    "outlives\nthe call. Look these up; never probe one.\n\n"
    "An op whose class is not declared is shown `!`, so a gap is never the "
    "quiet\nanswer. Full entry for one op: `help:OP` — more than the listing "
    "row carries.\nEvery entry: `ops`."
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


def _ops_argument_refusal(arg: str, op_name: str = "ops") -> str:
    """`ops:gh-labels` printed the whole 47KB listing and said nothing (#1231).

    An argument dropped without a word, in the op whose job is to say which
    arguments exist, in a tool whose rule is that an unrecognised token is
    refused rather than ignored.

    Refused rather than made a filter. ``help:OP`` already answers the question
    a filter would, and answers it with strictly more — full contract, semantics
    and a worked example, against the listing's one line. A second lookup path
    would also re-create this issue in miniature: ``ops:gh-labl`` matching
    nothing renders identically to an op that does not exist, which is the
    absence-as-answer defect the roster exists to remove.
    """
    if arg in _roster_classes():
        return (f"ERROR: `{op_name}` takes no filter, and '{arg}' is an op "
                f"name.\n"
                f"  Its full entry: `help:{arg}` — more than the listing row "
                f"carries.\n"
                f"  Every name plus its safety class: `ops:roster`. "
                f"Every entry: `ops`.\n")
    return (f"ERROR: unknown argument to `{op_name}`: '{arg}'.\n"
            f"  Accepted: `ops` (every entry), `ops:roster` (every name plus "
            f"its safety class), `ops-compact` (the capped listing).\n"
            f"  '{arg}' is also not an op name loaded here — `ops:roster` "
            f"lists the ones that are.\n")


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


# What a green from the builtin parse check does NOT cover (#1100). `py-syntax`
# answers "does this parse"; it is read as "is this a working module", because
# that is the question a caller who has just written a file actually has.
# Between the two sits everything that only fails at import — a regex compiled
# at module level, an undefined name at class-body scope, a circular import.
# One such write landed in `_supertool.py` itself: it parsed, it could not be
# imported, and every subsequent supertool call in that worktree died behind
# this validator's green.
#
# The validator is not made to import the file. Import EXECUTES module-level
# code, and running arbitrary just-edited bytes is a containment decision, not
# a coverage one — the file that produced the report was this tool's own core,
# being edited by this tool. So the green states its own limit instead, in the
# column that already exists: it costs no line, and it cannot drift out of date
# the way a docs note would.
_PY_SYNTAX_SCOPE = "parsed; not imported"


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
                "errors": [err], "elapsed_s": _elapsed_since(_t0)}
    except (ValueError, MemoryError, RecursionError) as e:
        # Null bytes, absurd nesting: the source is rejected by the compiler but
        # not with a line number. Still a hard "does not compile".
        return {"tool": name, "file": file, "ok": False, "count": 1,
                "errors": [{"line": None, "col": None, "severity": "error",
                            "code": "syntax", "msg": str(e)[:300]}],
                "elapsed_s": _elapsed_since(_t0)}
    return {"tool": name, "file": file, "ok": True, "count": 0, "errors": [],
            "scope": _PY_SYNTAX_SCOPE, "elapsed_s": _elapsed_since(_t0)}


# --- The syntax floor (#478) ------------------------------------------------
#
# This repo supports 3.9-3.12 and, until now, only the CI matrix knew it. PR
# #473 shipped PEP 701 nested quotes inside an f-string — legal on 3.12+, a
# SyntaxError on 3.9/3.10/3.11 — and nine of twelve legs went red on a change
# every local check called clean, because the prescribed check was
# `ast.parse(src, feature_version=(3, 9))`. `feature_version` gates *grammar
# productions* (walrus, `match`, `except*`); it does not touch the tokenizer
# change PEP 701 made, so on a modern host it returns clean both before and
# after the bug. Nothing computed from the running interpreter's AST closes
# that gap — you have to run an older interpreter.
#
# So the ladder below sources a real one, and when it cannot it says so in the
# `skipped` shape (#515) rather than reporting a pass. What it does NOT cover:
#   - a host whose only Python is the one running the suite -> the check does
#     not run at all. The CI floor leg is the backstop, and
#     `test_ci_matrix_covers_the_syntax_floor` fails if that leg disappears.
#   - an interpreter above the floor (`partial`) -> catches PEP 701 and every
#     other syntax newer than it, but not syntax legal on it and illegal on
#     3.9. Better than nothing, honestly labelled, never silent.
SYNTAX_FLOOR: Tuple[int, int] = (3, 9)
SYNTAX_FLOOR_ENV = "PYTHON39"

_SYNTAX_FLOOR_PROBE = "import sys;print('%d.%d' % sys.version_info[:2])"

# Runs under the OLD interpreter, so: no f-strings, no walrus, nothing newer
# than the floor. Reads a JSON path list on stdin, writes a JSON failure list.
_SYNTAX_FLOOR_COMPILE = """
import json, sys
out = []
for p in json.load(sys.stdin):
    try:
        f = open(p, 'rb')
        src = f.read()
        f.close()
        compile(src, p, 'exec', dont_inherit=True)
    except SyntaxError as e:
        out.append({'file': p, 'line': getattr(e, 'lineno', None),
                    'col': getattr(e, 'offset', None),
                    'msg': (getattr(e, 'msg', None) or str(e))[:300]})
    except (OSError, ValueError) as e:
        out.append({'file': p, 'line': None, 'col': None,
                    'msg': 'unreadable: ' + str(e)[:200]})
json.dump(out, sys.stdout)
"""


def _interpreter_version(path: str) -> Optional[Tuple[int, int]]:
    """`(major, minor)` reported by the interpreter at `path`, or None.

    Asked of the binary itself rather than inferred from its name: a
    `python3.9` on PATH that is really a 3.12 shim would otherwise hand back a
    false clean, which is the whole defect this section is about.
    """
    try:
        proc = subprocess.run([path, "-c", _SYNTAX_FLOOR_PROBE],
                              capture_output=True, text=True, timeout=30,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    parts = (proc.stdout or "").strip().split(".")
    if len(parts) != 2:
        return None
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def _syntax_floor_interpreter(env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """An interpreter old enough to be worth compiling under, or None.

    Ladder, lowest useful first:

    1. ``$PYTHON39`` — the explicit escape hatch. **Verified, not trusted**: if
       it points at something no older than the running interpreter it is
       rejected outright rather than falling through, because a declaration
       that silently buys nothing is worse than no declaration at all — it
       restores exactly the false clean this exists to prevent.
    2. The running interpreter, when it *is* at or below the floor. That is the
       CI floor leg, where the check runs for real with nothing extra installed.
    3. The lowest ``pythonX.Y`` on PATH between the floor and the running
       version. Someone with a 3.11 lying around gets a real check locally; the
       result is labelled `partial` so nobody mistakes it for floor fidelity.
    """
    environ = os.environ if env is None else env
    current = sys.version_info[:2]

    declared = (environ.get(SYNTAX_FLOOR_ENV) or "").strip()
    if declared:
        ver = _interpreter_version(declared)
        if ver is None or ver >= current:
            return None
        return declared

    if current <= SYNTAX_FLOOR:
        return sys.executable

    for minor in range(SYNTAX_FLOOR[1], current[1]):
        cand = shutil.which("python%d.%d" % (SYNTAX_FLOOR[0], minor))
        if cand and (_interpreter_version(cand) or current) < current:
            return cand
    return None


def _syntax_floor_check(paths: Iterable[str],
                        env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Compile every path under an older interpreter. Three states, never two.

    Returns a `skipped` result — verdict keys omitted, per SCHEMA.md and #515 —
    whenever no older interpreter could be sourced or the child cannot be
    trusted to have answered. A check that cannot run must say so; rendering an
    absence as a pass is the failure this repo has now filed a dozen times.
    """
    floor = "%d.%d" % SYNTAX_FLOOR
    interp = _syntax_floor_interpreter(env)
    if interp is None:
        return {"tool": "syntax-floor", "skipped": (
            "no interpreter older than this one to compile with (want Python %s): "
            "set $%s to one, or install python%s. This check did NOT run."
            % (floor, SYNTAX_FLOOR_ENV, floor))}
    targets = [str(p) for p in paths]
    t0 = time.monotonic()
    try:
        proc = subprocess.run([interp, "-c", _SYNTAX_FLOOR_COMPILE],
                              input=json.dumps(targets),
                              capture_output=True, text=True, timeout=600,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        return {"tool": "syntax-floor",
                "skipped": "floor interpreter %s did not run: %s" % (interp, e)}
    try:
        found = json.loads(proc.stdout or "[]")
    except ValueError:
        return {"tool": "syntax-floor", "skipped": (
            "unparseable output from %s — treating as no answer, not as clean: %s"
            % (interp, (proc.stderr or proc.stdout or "").strip()[:200]))}
    errors = [{"file": f.get("file"), "line": f.get("line"), "col": f.get("col"),
               "severity": "error", "code": "syntax",
               "msg": str(f.get("msg", ""))[:300]} for f in found]
    result: Dict[str, Any] = {
        "tool": "syntax-floor", "ok": not errors, "count": len(errors),
        "errors": errors, "duration_ms": int(_elapsed_since(t0) * 1000),
        "interpreter": interp, "checked": len(targets),
    }
    ver = _interpreter_version(interp)
    if ver is not None:
        result["interpreter_version"] = "%d.%d" % ver
        if ver > SYNTAX_FLOOR:
            result["partial"] = (
                "compiled under %d.%d, not the supported floor %s — syntax legal "
                "on %d.%d but illegal on %s is NOT covered here. Full floor "
                "fidelity comes from the %s CI leg."
                % (ver[0], ver[1], floor, ver[0], ver[1], floor, floor))
    return result


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
    # `read-elide` (#1329) is one tiny sidecar per (session, file) and the
    # session key holds a PPID, so yesterday's entries can never match again:
    # 1 day, not 7, because the population is per-session garbage the moment
    # the session ends.
    "read-elide": 1,
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

    for _name, spec in specs.items():
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
                           env=_run_env, encoding="utf-8", errors="replace")
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


_VALIDATOR_MEANING_VERSION: Optional[str] = None


def _validator_meaning_version() -> str:
    """Identify what the cached FIELDS MEAN, so a reinterpretation misses (#1048).

    The rest of the key says what was analysed (`content`), by whom (`name`,
    `cmd`) and by which build of the analyser (`_validator_fingerprint`). None
    of it says what the stored fields mean to the core reading them back. So a
    change to the core's own interpretation of a field it already owns — a
    `count` that starts excluding a category, an `ok` that starts implying
    something narrower, a key that becomes core-only — is read out of entries
    written under the previous meaning, for up to `validator_cache_ttl_hours`.
    Nothing is forged and no adapter misbehaves: the bytes were correct when
    written and are wrong when read, which is why no test today notices.

    **Derived, not declared, and that is the judgment call.** A hand-maintained
    revision constant would be cheaper still and is the class of guard this repo
    distrusts on sight — #1042 is a filed instance of exactly it, two copies of
    one contract with nothing comparing them. Putting the release version here
    instead is correct and blunt: it cold-invalidates every validator cache for
    every user on every release, minutes per developer on the phpstan/phpunit
    tiers, whether or not any meaning moved. That trade was refused in #1044 and
    the refusal still holds.

    So the component is hashed out of the two places the meaning actually lives:

    - `validators/SCHEMA.md`, this repo's canonical statement of what each field
      means. A meaning change that does not touch it is already a contract
      violation, and `tests/test_adapter_cannot_forge_core_keys_1036.py` is the
      machine that compares the doc's claims to the code's behaviour — this
      leans on that comparison rather than adding a second copy beside it.
    - the sorted `_VALIDATOR_CORE_ONLY_KEYS`, the meaning-bearing half of the
      contract that lives in code. A key entering that set changes what an
      entry carrying it means, and the doc can lag by a commit.

    **Content, not `stat`.** `_validator_fingerprint` uses size+mtime because it
    is asking "is this the same binary"; this is asking "is this the same
    contract", and a fresh clone or a reinstall rewrites identical bytes at a new
    mtime. Keying on mtime would pay #1044's rejected cost at every checkout.

    **An unreadable SCHEMA.md is its own key space, not a default.** An install
    that cannot read the doc cannot say which meaning its entries were written
    under, and folding that into whatever the readable case hashes to would let
    entries cross the boundary in the one direction this exists to prevent. The
    three-state contract, applied to the key itself.

    Memoised: the file is read once per process, not once per cache lookup.
    """
    global _VALIDATOR_MEANING_VERSION
    if _VALIDATOR_MEANING_VERSION is not None:
        return _VALIDATOR_MEANING_VERSION
    import hashlib
    h = hashlib.sha256()
    try:
        with open(os.path.join(_INSTALL_DIR, "validators", "SCHEMA.md"), "rb") as f:
            h.update(f.read())
    except OSError:
        h.update(b"schema-unreadable")
    h.update(b"\x00" + "\x00".join(
        sorted(_VALIDATOR_CORE_ONLY_KEYS)).encode("utf-8"))
    _VALIDATOR_MEANING_VERSION = h.hexdigest()[:16]
    return _VALIDATOR_MEANING_VERSION


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
    h.update(b"\x00" + _validator_meaning_version().encode("utf-8"))
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
_NONDETERMINISTIC_ERROR_CODES = {"mcp", "orchestrator", "rector.exit", "adapter"}
# `adapter` joined the set with #745. SCHEMA.md and docs/contributing.md already
# reserve it for "the adapter or its tool could not produce a verdict" — a binary
# that is absent, a timeout, output that would not parse, a `php -l` that exited
# without saying anything about the file. None of those are a function of the
# file's content, which is exactly the criterion this set encodes and exactly the
# shape of the 2100-entry incident below: a toolchain broken for ten minutes
# would otherwise freeze a red into a content-hash-keyed cache and replay it
# until someone touched the file. Before #745 those exits reached the cache
# wearing a finding's code (`parse`), so they were cached and this never had a
# chance to fire; naming them correctly is what makes the guard reachable.


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


# How much of a target to read when evaluating `warm_unsafe`. The markers this
# gate looks for (a class-declaration `extends`, a `use` import, an attribute)
# live in the first few hundred lines of any real source file; reading a whole
# generated multi-megabyte file to find one would cost more than the validator.
_WARM_UNSAFE_READ_BYTES = 256 * 1024


def _validator_warm_unsafe_reason(spec: Dict[str, Any], target: str) -> Optional[str]:
    """Why this validator must decline on `target`, or None to run normally.

    WHY THIS EXISTS (#345). `phpunit-mcp` reported two failures on a DVSI test
    extending `SiControllerTestCase`; the cold `phpunit:` op on the same file,
    same commit, same `phpunit.xml`, passed 3/3. The reds were *fabricated*, not
    pre-existing: `mcp-phpunit-warm` runs the project's phpunit.xml bootstrap in
    the long-lived PARENT and forks a child per call, so whatever that bootstrap
    opened — a DB handle, a session, a platform singleton — is shared by every
    child and by the parent. The failure therefore depends on warm-process
    state, not on the file, which is exactly why a cold run cannot reproduce it.
    Same family as #265 (phpunit staleness) and #273 (rector ClassReflection).

    Note what this is NOT. It is not "suppress results the runner calls
    pre-existing": a pre-existing failure is a real failure, and hiding it is
    how a broken file starts looking clean. Regression-only rollback (#406)
    already handles genuinely pre-existing reds correctly — it compares against
    a baseline and refuses to roll back. The problem here is upstream of that:
    the red is not a fact about the file at all.

    So this follows #482 rather than #406 — a tool that cannot answer must say
    so rather than guess. `validators.<name>.warm_unsafe` is a regex (or list of
    regexes) matched against the resolved target's content; a hit turns the run
    into a `skipped`, which the framework already treats as an absence of
    information: never a ✗, never a rollback, never cached.

    Deliberately opt-in and vendor-neutral. Supertool cannot work out on its own
    which of a project's tests touch shared bootstrap state; the project can,
    and says so in config. Absent config, nothing changes.

    Failure modes are biased towards running: an unreadable target, a pattern
    that is not a string, and a pattern that does not compile are all ignored,
    because a config typo must not silently mute a validator. One bad pattern
    does not disarm the good ones beside it.
    """
    patterns = spec.get("warm_unsafe")
    if isinstance(patterns, str):
        patterns = [patterns]
    if not isinstance(patterns, list) or not patterns:
        return None
    try:
        with open(target, "rb") as fh:
            blob = fh.read(_WARM_UNSAFE_READ_BYTES)
    except OSError:
        # Cannot evaluate the gate → leave pre-#345 behaviour in place rather
        # than mute the validator on every file the gate could not read.
        return None
    text = blob.decode("utf-8", errors="replace")
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern.strip():
            continue
        try:
            rx = re.compile(pattern)
        except re.error:
            continue
        if rx.search(text):
            return (f"warm-unsafe: target matches /{pattern}/ — this validator's "
                    f"warm process cannot be trusted here; run the tool directly")
    return None


def _validator_cmd_program(cmd: str) -> str:
    """The program a validator `cmd` tries to spawn, for messages (#634).

    Sourced from the spec rather than from the `OSError` text, because that
    text is not portable: POSIX names the missing binary
    (`No such file or directory: 'jsonlint'`), while Windows raises
    `[WinError 2] The system cannot find the file specified` and names nothing.
    Reading the name from the exception told Windows users a checker could not
    run without telling them which one — the same platform-shaped hole as #627.
    The spec knows the answer on every platform, so it is the one asked.
    """
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()
    return parts[0] if parts else cmd.strip()


def _validator_unusable_reply(name: str, target: str, what: str,
                              elapsed: float) -> Dict[str, Any]:
    """The adapter gave us nothing we can read — a skip, never a finding (#634).

    `validate:presets/gitlab.json` used to print `jsonlint : 1 err (0ms)` with
    `adapter bad json: Expecting value: line 1 column 1 (char 0)` against a file
    stdlib `json.load()` reads happily. That text is what `json.loads("")`
    raises, so it was never about the file: the adapter's own reply failed to
    parse, and the orchestrator rendered its own confusion as a finding about
    the user's code, in the position and colour a real syntax error prints in.

    This is #263's failure inverted, and worse. A missed error costs one bug; an
    invented one costs the credibility of every error the validator prints, and
    this fired on every `.json` edit — which is exactly how the first genuinely
    malformed file gets read as the usual noise and skipped.

    So it takes the third state (`docs/validators.md`, "Declining instead of
    guessing"): no `ok`, no `count`, no `errors` (#515), never a regression,
    never a rollback. That is not suppression — the row still prints, loudly,
    and now says *whose* JSON was bad. `1 err` claimed a fact about the file;
    `skipped` states the truth, which is that nobody checked it.

    Only exits where nothing ran, or ran and said nothing readable, come here.
    A timeout does not: the binary exists and was invoked, and a tool that hangs
    is a validator failure that must stay loud.

    `no_verdict` marks the skip as one the **core** produced by watching the
    adapter fail, as opposed to one the adapter chose (#975). Every skip is an
    absence, but only some are a broken gate: `warm_unsafe` (#345), an
    out-of-scope path (#263) and a resolver that maps a file to nothing are a
    healthy adapter declining, and escalating those under
    `$SUPERTOOL_REQUIRE_VALIDATORS='*'` would fire on edits nobody meant to
    gate. Nothing in the string tells the two apart, so the key does. It is
    core-internal: no adapter sets it, and a skip is never cached.
    """
    return {"tool": name, "file": target, "elapsed_s": elapsed,
            "no_verdict": True,
            "skipped": f"{name} adapter {what} — this file was not checked"}


_VALIDATOR_CORE_ONLY_KEYS = frozenset({
    "no_verdict", "timeout", "elapsed_s", "resolved_to",
})


def _validator_strip_core_keys(data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop every key the *core* owns from an adapter's parsed payload (#1036).

    The core and an adapter both describe the same run, and two of the core's
    words decide things no adapter is entitled to decide. `timeout` is the flag
    `_validator_run_one`'s `TimeoutExpired` arm stamps on the result it
    fabricates; `_validator_no_verdict` reads it, and `_validator_regressed`
    returns False for any non-verdict. So an adapter that printed
    `"timeout": true` beside a real finding switched off `rollback_on_fail`
    entirely: the row said `NOT CHECKED`, the guard never ran, and a bad edit
    stood on the one setting configured to revert it.

    **The core's timeout and an adapter's claim of one are different facts, and
    only the first is evidence.** The adapter is a subprocess reporting on a
    tool; whether it answered inside its budget is something only the process
    holding the budget observed.

    Dropped, not refused. Refusing — turning the whole result into a skip or an
    unusable reply — would give the same adapter the same bypass through the
    other door, because a skip is also a non-verdict and also never rolls back.
    Dropping keeps the adapter's own verdict (`ok`, `count`, `errors`) exactly
    as it was written and removes only the claims it had no standing to make,
    so a forged key costs the adapter nothing and buys it nothing.

    **Two doors, and the count is the part that was wrong.** This was written
    as "the only door" on the line after `json.loads`, and the validator cache
    is the other one: a cache hit returns the same parsed payload, persisted,
    from a `return` that is upstream of that line. An entry left by a build
    from before this function existed carries a forged `timeout` through an
    HMAC that verifies — the machine's own secret signed it — and, until #1048
    added `_validator_meaning_version`, a cache key describing nothing about the
    build that wrote it, so no upgrade retired it (#1044). Both doors are
    in `_validator_run_one`; a third one strips too, and
    `tests/test_cached_result_cannot_forge_core_keys_1044.py` is where the
    cache door is pinned.

    It is a set rather than two `pop` calls because the defect is the class:
    `no_verdict` was already forbidden in prose by `validators/SCHEMA.md` and
    `timeout` was not, and nothing enforced either. Any key a core-only decision
    reads belongs in here, and
    `tests/test_adapter_cannot_forge_core_keys_1036.py` fails if one is added to
    a decision and not to this set.
    """
    for key in _VALIDATOR_CORE_ONLY_KEYS:
        data.pop(key, None)
    return data


def _validator_run_one(name: str, spec: Dict[str, Any], file: str,
                       doc_maybe_stale: bool = False) -> Optional[Dict[str, Any]]:
    """Run one validator adapter on `file`. Returns SCHEMA.md-compliant dict.

    Adapter contract: prints one JSON object on last stdout line. Exit 0 unless
    infra fail. Failures here produce a synthetic error dict so the row still
    renders. Cached by (file content hash, name, cmd, tool fingerprint) at
    ~/.cache/supertool/validators/<sha256>.json — see _validator_fingerprint for
    why the tools themselves are part of the key.

    `doc_maybe_stale` reaches the adapter as SUPERTOOL_LSP_DOC_MAYBE_STALE=1.
    Only this process knows the fact it carries — that a pre-edit baseline pass
    already queried a warm LSP daemon about this path, so the daemon is holding
    the pre-edit document (#482). An adapter that reads a warm cache cannot
    work that out on its own, and must skip rather than answer from it.
    """
    import subprocess
    import json
    target = _validator_resolve(spec, file)
    if target is None:
        return {"tool": name, "skipped": "no target resolved"}
    # #345: some targets this validator's warm process cannot judge — declared
    # per validator as `warm_unsafe` regexes. Checked here, before the adapter
    # is spawned at all: the decision is a property of the target, so paying a
    # daemon round-trip to reach a verdict we would then discard is waste.
    _warm_unsafe = _validator_warm_unsafe_reason(spec, target)
    if _warm_unsafe:
        return {"tool": name, "file": target, "skipped": _warm_unsafe}
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
            import time as _time
            _t_cache = _time.monotonic()
            cached = _validator_cache_read(cache_key)
            if cached is not None:
                # The other door (#1044). A cache entry is an adapter payload
                # that outlived the run which parsed it, and this return is
                # upstream of the strip below — so an entry written by a build
                # from before #1036 hands a decision the adapter's own
                # `timeout` verbatim. It verifies: this machine's secret signed
                # it, and before #1048 the key described nothing about the build
                # that wrote it, so upgrading to the build that fixed #1036 did
                # not retire it. The meaning version retires it only when the
                # contract moves, which is not the same guarantee — the strip
                # below is still the one that makes any vintage safe to read.
                _validator_strip_core_keys(cached)
                # Re-stamped, not preserved: these two describe THIS run. The
                # answer came out of a file, so the elapsed time is the lookup,
                # and the resolved target is the one just resolved above — the
                # cached copy of either is only as trustworthy as the adapter
                # that may have written it.
                cached["elapsed_s"] = _elapsed_since(_t_cache)
                if target != file:
                    cached["resolved_to"] = target
                return cached

    # Use _merged_env (built above) so the prefix env-vars reach the child too.
    # #475: the env is now always explicit, because provenance is stamped into
    # it. A validator runs on a budget measured in seconds; a cold MCP daemon
    # takes 30-60s just to index (docs/mcp-integration.md), so an adapter that
    # auto-spawns one is guaranteed to be killed before it gets an answer while
    # the orphaned daemon holds its index for the full 600s idle window. The
    # flag says "use a warm daemon, do not create one" and is inherited by the
    # adapter's own children (lsp-diag.py shells `supertool diag:FILE`).
    # Opt back in per validator with `"mcp_autospawn": true` when the budget
    # genuinely covers a cold start.
    run_env = dict(_merged_env)
    run_env[_MCP_AUTOSPAWN_ENV] = "1" if spec.get("mcp_autospawn") else "0"
    # #482: the doc the daemon holds may predate this edit, and it has no
    # invalidation of its own. The adapter declines rather than guessing.
    if doc_maybe_stale:
        run_env["SUPERTOOL_LSP_DOC_MAYBE_STALE"] = "1"

    import time
    _t0 = time.monotonic()
    try:
        r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, timeout=timeout,
                           env=run_env, encoding="utf-8", errors="replace")
        _elapsed = _elapsed_since(_t0)
        out = r.stdout.strip()
        if not out:
            return _validator_unusable_reply(
                name, target, "produced no output", _elapsed)
        data = json.loads(out.splitlines()[-1])
        if not isinstance(data, dict) or not ("ok" in data or "skipped" in data):
            return _validator_unusable_reply(
                name, target, "replied without a verdict "
                "(no 'ok' and no 'skipped' key)", _elapsed)
        # Before anything reads it: the payload crosses from the adapter's
        # authority into the core's here. One of the two doors — the other is
        # the cache read above, which returns the same payload persisted
        # (#1036, #1044).
        _validator_strip_core_keys(data)
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
                "duration_ms": timeout * 1000, "elapsed_s": _elapsed_since(_t0),
                "timeout": True}
    except OSError as e:
        # strerror, not str(e): the program name comes from the spec, and the
        # full exception text repeats it on POSIX while omitting it on Windows.
        # Taking only the reason makes the message the same shape everywhere.
        _why = e.strerror or str(e)
        return _validator_unusable_reply(
            name, target,
            f"could not be run: {_validator_cmd_program(cmd)} — {_why}",
            _elapsed_since(_t0))
    except (json.JSONDecodeError, IndexError) as e:
        return _validator_unusable_reply(
            name, target, f"replied with something that is not JSON — {e}",
            _elapsed_since(_t0))


def _flat_cell(value: Any, limit: Optional[int] = None) -> str:
    """An adapter-supplied value, rendered into a line the *tool* owns (#895).

    #886 stated the guarantee for `validate:` output — **one line at column 0,
    one block per file, whatever the files are called** — and implemented it in
    `_flat_field` for the block header. The rows underneath were still written
    against ``.replace(chr(10), " ")``, one separator out of the ten
    `str.splitlines()` splits on, and `resolved_to` had no flattening at all.
    A file named ``a<U+2028>validate: forged.q`` therefore got a correctly
    flattened header and then wrote a second, forged one out of the row below
    it, because the shipped subprocess adapters echo their input: `xmllint`
    reports xmllint's stderr, `tsc-check` reports `output[:300]` raw, `phpstan`
    reports `m["message"]`, `ruff` and `yaml-check` likewise.

    So this is not a second copy of the rule — it is `_flat_field`, the same
    one implementation, plus the two things a row does to a field that a header
    does not: strip it, and bound its width. Applied to *every* adapter-supplied
    string these renderers interpolate into a line of their own, not only to the
    three the report named. `tool` is the leftmost field on the row and `skipped`
    was the only one with no sanitising whatsoever; fixing `msg` and leaving
    those would be this very defect one field over, which is the shape of the
    #876 → #878 → #881 → #886 chain.

    What is deliberately *not* routed here: `raw_stdout`, `raw_stderr` and
    `diff` in verbose mode. Those are blocks, not fields — the reader asked for
    the tool's output verbatim, every line of them is emitted indented, and so
    none can produce a column-0 header. `presets/_untrusted.py` already draws
    that line as `scrub()` versus `flat()`; drawing it differently here would be
    the second copy of a rule this docstring is about.
    """
    text = _flat_field(str(value)).strip()
    if limit and len(text) > limit:
        # A cut with no marker is indistinguishable from a string that ended
        # there — and the fields routed through here include the `skipped`
        # reason and the `adapter` message, whose entire job is to disclose why
        # nothing was checked. `apt install shellche)` and ``(`brew instal)``
        # both shipped, reading as complete sentences. The marker stays inside
        # `limit`, so no column widens.
        return text[:max(limit - 1, 0)] + "…"
    return text


def _validator_render_row(data: Dict[str, Any], verbose: bool = False) -> list:
    """Render a single validator result as a list of display lines.

    verbose=False (default): compact mode — summary header + up to 5 errors,
    then ``... +N more`` if there are additional errors.

    verbose=True: full mode — summary header + ALL errors (no cap), plus the
    adapter's raw stdout/stderr appended verbatim when present in the result
    dict under the ``"raw_stdout"`` / ``"raw_stderr"`` keys.

    The ``"raw_stdout"`` / ``"raw_stderr"`` keys are optional; adapters that
    want verbose output to include their full output should populate them.

    Every field the adapter supplies goes through `_flat_cell`, so a row is one
    line for the same reason the block header is (#895).
    """
    if "skipped" in data:
        return [f"{_flat_cell(data['tool']):12s}: skipped — "
                f"{_flat_cell(data['skipped'])}"]
    tool = _flat_cell(data.get("tool", "?"))
    ok = data.get("ok", False)
    count = data.get("count", 0)
    dur = data.get("duration_ms", 0)
    # `1 err` about a file the adapter never opened reads as a measurement.
    # So does `(timeout)` — on a required gate (#975) and on any other (#969).
    status = ("NOT CHECKED" if (_validator_no_verdict(data) is not None
                                or _validator_gate_did_not_run(data) is not None)
              else ("ok" if ok else f"{count} err"))
    line = f"{tool:12s}: {status:<10}  ({dur}ms)"
    metrics = data.get("metrics")
    if metrics and tool == "git-status":
        added = metrics.get("lines_added", 0)
        removed = metrics.get("lines_removed", 0)
        state = metrics.get("state", "")
        line += f"  +{added} -{removed} {state}"
    if data.get("resolved_to"):
        line += f"  → {_flat_cell(data['resolved_to'])}"
    out = [line]
    errors = data.get("errors") or []
    if verbose:
        for e in errors:
            line_n = f"L{e['line']}" if e.get("line") else "  "
            code = e.get("code") or ""
            msg = _flat_cell(e.get("msg") or "")
            out.append(f"  {line_n} {code}  {msg}")
            for ctx_line in (e.get("source_context") or []):
                out.append(f"    {_flat_cell(ctx_line)}")
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
            msg = _flat_cell(e.get("msg") or "", 120)
            out.append(f"  {line_n} {code}  {msg}")
        if len(errors) > 5:
            out.append(f"  ... +{len(errors) - 5} more")
    return out


def _validator_not_checked(after: Optional[Dict[str, Any]]) -> Optional[str]:
    """The adapter answered and said nothing about the file. Its reason, or None.

    `skipped` is the third state for a checker that declined *before* running.
    This is its twin for one that was asked to run, could not, and had only the
    channel SCHEMA.md gives it to say so with — an error whose `code` is
    `adapter`. Both are absences of information. Neither is a finding.

    The distinction has to exist here because everything downstream of a result
    treats an error as a measurement of the file, and the loudest consumer is
    arithmetic. `_validator_render_diff` subtracts the pre-edit count from the
    post-edit one, and `refusal.required()` emits this same error on BOTH
    passes when the tool is absent — so the counts cancel and the row rendered
    `1 err  (pre-existing — not from this edit)`: a sentence asserting a real
    finding predated the edit, printed about a file nothing opened, above a
    `[result]` line reading `1 op run, 1 write` and an exit code of 0. That is
    the absence-read-as-a-pass the third state exists to end, arriving inside
    the mechanism built to end it.

    The test is `code == "adapter"` on **every** error, not on the first: an
    adapter reporting four real findings plus one adapter row has still
    measured the file, and hiding that would be this defect pointing the other
    way. `orchestrator` codes — the core's own timeout — are deliberately not
    included; those are already rendered as `(timeout)` and are the core's
    statement, not the adapter's.
    """
    if not isinstance(after, dict) or "skipped" in after:
        return None
    if after.get("ok", False):
        return None
    errors = after.get("errors") or []
    if not errors:
        return None
    if not all((e.get("code") or "") == "adapter" for e in errors):
        return None
    return _flat_cell(errors[0].get("msg") or "", 300) or "no reason given"


def _validator_no_verdict(data: Optional[Dict[str, Any]]) -> Optional[str]:
    """No opinion about the file was obtained, by any route. Its reason, or None.

    `_validator_not_checked` is the *adapter* saying it could not answer (#967).
    This adds the *core* saying the same thing: the `TimeoutExpired` arm of
    `_validator_run_one`, which fabricates `ok: false, count: 1` with an
    `orchestrator` code because SCHEMA.md gives an absence no other channel.

    The distinction between the two mattered for rendering — one is the
    adapter's statement, one is the core's — and does not matter at all to the
    consumer that reads a count as a measurement. `_validator_regressed`
    subtracts these fabricated counts and, on a `rollback_on_fail` validator,
    rewrote the file with its pre-edit bytes: an edit deleted by a checker that
    formed no opinion about it, which is the one failure on this tracker that
    destroys work rather than misinforming (#969). A timeout needs no exotic
    config to reach it — a loaded machine and a 10s budget will do.

    Distinct from `_validator_gate_did_not_run`, which answers a narrower
    question — did a gate the operator *required* break down — and is consulted
    only where an exit code is at stake. This one is unconditional, because the
    arithmetic it guards runs whether or not anyone required anything.

    `skipped` is deliberately NOT folded in, though it is the same absence. It
    is the third state for a checker that declined *before* running, every
    consumer already tests for it by key, and routing it here would make an
    optional tool nobody installed report `NOT RUN` and exit 1 — the quiet bug
    traded for a tool nobody can run, which is the trade #665 refused.
    """
    if not isinstance(data, dict) or "skipped" in data:
        return None
    if data.get("timeout"):
        errors = data.get("errors") or []
        msg = _flat_cell((errors[0].get("msg") if errors else "") or "", 300)
        return msg or "timed out"
    return _validator_not_checked(data)


def _validator_baseline(before: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The pre-op result, or None when it holds no verdict to subtract from.

    The `before` side is this defect pointing the other way, and it is the
    quieter half (#969). A baseline that could not run also carries
    `count: 1`, so a real finding introduced by the edit cancels against it:
    the row read `1 err  (pre-existing — not from this edit)` about an error
    this edit had just created, no rollback fired, and the call exited 0.

    A baseline nothing measured is not a clean baseline and it is not a count.
    It is the absence of a baseline, which is the case `before is None` already
    covers — the one #832 taught the renderer to print as `?` rather than `0`.
    So it folds into that one rather than being given a fourth meaning.
    """
    if not isinstance(before, dict):
        return None
    if "skipped" in before or _validator_no_verdict(before) is not None:
        return None
    return before


def _validator_required(name: str) -> bool:
    """Is `name` named by $SUPERTOOL_REQUIRE_VALIDATORS? The core's own read.

    A deliberate second implementation of `validators/common/refusal.required()`,
    and `tests/test_require_validators_core_975.py` pins the two to the same
    answers on the same table because a second copy of a rule is how #895
    happened. It is a copy rather than an import because the twin lives in the
    package the *adapters* import, inside a subprocess with its own interpreter
    and its own sys.path; reaching into it from the core would mean the gate
    stops working whenever that path resolution does.

    The duplication is the price of the fix. #967 could key on the adapter's
    self-report because the adapter was healthy enough to make one. The five
    failures in #975 are the adapter being too broken to run its own Python at
    all — no output, non-JSON output, a crash, a reply with no verdict key, a
    timeout. The core watched every one of them happen and must be able to
    reach the same conclusion without the adapter's cooperation.
    """
    raw = os.environ.get("SUPERTOOL_REQUIRE_VALIDATORS", "")
    if not raw.strip():
        return False
    names = [n.strip().lower()
             for part in raw.split(os.pathsep) for n in part.split(",")]
    return "*" in names or name.lower() in names


def _validator_gate_did_not_run(data: Optional[Dict[str, Any]]) -> Optional[str]:
    """A gate the operator required, that the core watched break down. Reason, or None.

    #966/#967 stopped `$SUPERTOOL_REQUIRE_VALIDATORS` reading as a pass for the
    one case the adapter can report about itself: it ran, found its binary
    missing, and emitted an `adapter` error. `_validator_not_checked` keys on
    that self-report, which requires the adapter to be healthy.

    Five ways it is not lands elsewhere and exited 0 (#975). Four route into
    `_validator_unusable_reply` and become a `skipped`; the fifth is the core's
    own `TimeoutExpired` arm, which renders `1 err (timeout)`. In all five the
    row text was already honest — it says the file was not checked. Only the
    exit code lied, which is the half `supertool 'edit:...' && git commit`
    reads, and that chain is the entire reason the variable exists.

    Scope is deliberately the breakdowns the *core* observed, not every skip.
    An adapter that ran and declined on its own terms has said something true
    about applicability; turning that into a red under `'*'` would make the
    mechanism fire on ordinary edits, and a gate that cries wolf is the quiet
    bug traded for a louder one rather than fixed (#665's refusal, #966's
    judgment call). An adapter that is genuinely absent already escalates
    through `refusal.required()`.
    """
    if not isinstance(data, dict):
        return None
    if data.get("no_verdict"):
        reason = data.get("skipped") or ""
    elif data.get("timeout"):
        errors = data.get("errors") or []
        reason = (errors[0].get("msg") if errors else "") or "timed out"
    else:
        return None
    # `tool` is core-set on both of these dicts, so this is the config name and
    # not something an adapter chose — which matters, because the answer here
    # decides an exit code.
    if not _validator_required(str(data.get("tool") or "")):
        return None
    return _flat_cell(reason, 300) or "no reason given"


def _note_not_checked(results: Dict[str, Any]) -> None:
    """Record every validator in `results` that returned no verdict.

    Called from the render sites, not from `_validator_run_one`: the baseline
    pass runs the same adapters before the edit and produces the same
    non-verdicts, and counting those would double every row. Rendered and
    recorded are the same set by construction this way.
    """
    for name, data in results.items():
        if (_validator_no_verdict(data) is not None
                or _validator_gate_did_not_run(data) is not None):
            _acc_not_checked().append(name)


def _validator_regressed(before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> bool:
    """Did this op make this validator worse? The single definition of ✗ (#406).

    Both the rendered marker and the rollback decision read from here, so the
    red the caller sees and the revert it triggers can never disagree.

    Three states, not two: a `skipped` result is an absence of information, not
    a finding, so it can never regress — and must never roll back an edit.
    A failure that was already there before the op is not a regression either.

    `_validator_no_verdict` extends that to the other two ways a checker can
    fail to form an opinion — an adapter that could not run, and the core's own
    timeout (#969). Both sides are guarded, because both sides are arithmetic:
    a non-verdict *after* the op was read as a new failure and, on a
    `rollback_on_fail` validator, reverted the edit; a non-verdict *before* it
    was read as a pre-existing one and excused a real regression.
    """
    if "skipped" in after:
        return False
    if _validator_no_verdict(after) is not None:
        return False
    if after.get("ok", False):
        return False
    before = _validator_baseline(before)
    b_count = before.get("count", 0) if before else 0
    a_count = after.get("count", 0)
    b_ok = before.get("ok", True) if before else True
    if b_count == a_count and b_ok == after.get("ok", False):
        return False
    return a_count - b_count >= 0


def _validator_scope_col(after: Dict[str, Any]) -> str:
    """`(scope)` for a PASSING validator that declares one, else "" (#1100).

    Only on a pass. On a red row the finding is the line the reader has to act
    on, and a hedge next to it dilutes the one thing that matters; the limit is
    about what a green does not cover, so that is the only place it belongs.

    `_flat_cell` because this lands in a column-0 marker line, same rule as
    every other adapter-supplied string on these rows (#895) — a `scope` can
    come from a configured validator, not only from the builtin.
    """
    if not after.get("ok", False):
        return ""
    scope = after.get("scope")
    if not isinstance(scope, str) or not scope.strip():
        return ""
    return "(" + _flat_cell(scope.strip(), 40) + ")"


def _validator_render_diff(before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> list:
    # Every adapter-supplied field goes through `_flat_cell`, for the reason
    # `_validator_render_row` does (#895). A different renderer, not a different
    # guarantee: these rows also start at column 0, and the reader acting on them
    # is deciding whether the edit that just ran broke something.
    # Skipped path never started a timer, so elapsed_s is absent — `-` rendered in time col.
    elapsed = after.get("elapsed_s")
    time_col = f"{elapsed:.1f}s" if elapsed is not None else "-"
    gate_missed = _validator_gate_did_not_run(after)
    if gate_missed is not None:
        # Checked before the `skipped` branch below, and before the arithmetic:
        # under an escalation the word `skipped` is the wrong one. It is the
        # honest third state for a checker nobody required, and it is what four
        # of these five printed while the run exited 0 (#975). The row now
        # reads the way the exit code does.
        # Same wording as the unrequired path below (#969). Naming a validator
        # in the variable changes the exit code, not what went wrong, and two
        # spellings of one failure is a distinction no reader can act on.
        timed_out = bool(after.get("timeout"))
        why = ("(timed out — no verdict about this file)" if timed_out
               else "(no verdict about this file)")
        code_col = "orchestrator" if timed_out else "adapter"
        return [f"{_flat_cell(after.get('tool', '?')):12s}: {'NOT CHECKED':<10}  "
                f"{why}  {time_col:>5}",
                f"     {code_col}  {gate_missed}"]
    if "skipped" in after:
        # Name the reason. "skipped" alone sends the reader back to the config
        # to work out which of a dozen reasons applied (#406).
        reason = _flat_cell(after["skipped"], 80)
        state_col = f"({reason})" if reason else ""
        return [f"{_flat_cell(after['tool']):12s}: {'skipped':<10}  "
                f"{state_col}  {time_col:>5}"]
    tool = _flat_cell(after["tool"])
    no_verdict = _validator_no_verdict(after)
    if no_verdict is not None:
        # Never diffed. A non-verdict is not a finding about the file, so
        # subtracting one from another is arithmetic over two non-answers — and
        # the label it produced, `pre-existing`, is a claim about the file that
        # nothing measured. The status column says the only true thing instead.
        # A timeout lands here too (#969): it used to render `1 err (timeout)`,
        # which is a count about a file the checker never finished reading.
        timed_out = bool(after.get("timeout"))
        why = ("(timed out — no verdict about this file)" if timed_out
               else "(no verdict about this file)")
        code_col = "orchestrator" if timed_out else "adapter"
        return [f"{tool:12s}: {'NOT CHECKED':<10}  {why}  {time_col:>5}",
                f"     {code_col}  {no_verdict}"]
    # Nothing measured the pre-op state (#832). `before` is None from exactly
    # two callers and both mean that: `_drain_validator_queue`, where the slow
    # tier by design never runs a baseline pass, and the inline site when the
    # baseline produced no result for this validator at all. Falling through
    # `if before else 0` turned that into a literal zero, so `phpunit-mcp`
    # reported `0 → 7  (+7) ✗` about seven tests that were already failing for
    # an environment reason, and the reader nearly reverted a correct edit.
    # Every slow-tier validator did this on every run.
    #
    # #969 folds in the second route to the same absence: a baseline that ran
    # and returned no verdict is not a baseline either, and its fabricated
    # `count: 1` cancelled a real finding this edit had just introduced. That
    # is `before is None` reached by a different road, so it takes the same
    # road out rather than a fourth meaning of its own.
    baseline = _validator_baseline(before)
    b_unknown = baseline is None
    before = baseline
    b_count = before.get("count", 0) if before else 0
    a_count = after.get("count", 0)
    delta = a_count - b_count
    b_ok = before.get("ok", True) if before else True
    a_ok = after.get("ok", False)
    # `not b_unknown` guards the whole equal-counts branch, not just the arrow:
    # a clean unbaselined run took it and printed `(no new errors)`, which is
    # the same fabricated comparison with the sign flipped, and quiet enough to
    # have been left behind by a fix aimed only at the loud one.
    if not b_unknown and b_count == a_count and b_ok == a_ok:
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
            # The third green branch, so it carries the scope too (#1100). A
            # validator declaring one and also reporting metrics would otherwise
            # have its limit dropped on this row alone, and a contract that
            # holds on two rows out of three is the absence this repo keeps
            # filing about.
            scope = _validator_scope_col(after)
            return [f"{tool:12s}: {', '.join(metric_parts)} {marker}"
                    f"{' ' + scope if scope else ''}  {'':<11}  {time_col:>5}"]
        # Truly unchanged — fold the most relevant absolute metric into the row.
        if a_ok and a_metrics:
            primary = None
            for k in ("tests_total", "tests_passed", "changes_count"):
                if k in a_metrics:
                    primary = (k, a_metrics[k]); break
            if primary is not None:
                status = f"ok {primary[0]}={primary[1]}"
                # Same substitution as the branch below (#1100) — one green row
                # carrying `(no new errors)` and another carrying the scope would
                # leave the over-readable reading available on half the rows.
                col = _validator_scope_col(after) or "(no new errors)"
                return [f"{tool:12s}: {status:<10}  {col:<15}  {time_col:>5}"]
        status = "ok" if a_ok else f"{a_count} err"
        if a_ok:
            # Not "(unchanged)" — that reads as "the file is unchanged", which is
            # the opposite of what just happened. This column reports the delta in
            # the validator's own result (#380).
            #
            # A validator that declares a `scope` spends this column on its own
            # limit instead (#1100). `(no new errors)` is the string that got
            # over-read as "this module works", so the scope REPLACES it rather
            # than sitting beside it — leaving both would leave the old reading
            # available on the same row.
            marker_col = _validator_scope_col(after) or "(no new errors)"
        else:
            # No `(timeout)` arm: a timed-out result returned above as a
            # non-verdict (#969), so reaching here with one is impossible, and
            # the arm it used to take printed `1 err` beside it.
            marker_col = "(pre-existing — not from this edit)"
        out = [f"{tool:12s}: {status:<10}  {marker_col}  {time_col:>5}"]
        if not a_ok:
            for e in (after.get("errors") or [])[:5]:
                line_n = f"L{e['line']}" if e.get("line") else "  "
                code = e.get("code") or ""
                msg = _flat_cell(e.get("msg") or "", 120)
                out.append(f"  {line_n} {code}  {msg}")
            if len(after.get("errors") or []) > 5:
                out.append(f"  ... +{len(after['errors']) - 5} more")
        return out
    marker = (mark("✗") if _validator_regressed(before, after)
              else (mark("✓") if a_ok else mark("⚠")))
    scope_col = _validator_scope_col(after)
    if b_unknown:
        # `?`, not `0`. And no `(+N)`: N minus an unmeasured baseline is not N,
        # so fixing the arrow and keeping the delta would move the false number
        # one column rather than remove it. The marker stays as computed — the
        # file does have these errors now, and softening that would trade this
        # bug for the one where a real regression reads as a shrug.
        arrow = f"? → {a_count}"
        # Both, not one: `(baseline not measured)` is a statement about the
        # DELTA and the scope is a statement about the CHECK, and dropping
        # either for the other loses a signal the reader is entitled to (#1100).
        state_col = f"{marker} (baseline not measured)"
        if scope_col:
            state_col += f" {scope_col}"
    else:
        arrow = f"{b_count} → {a_count}"
        sign = f"({'+' if delta >= 0 else ''}{delta})"
        state_col = f"{sign} {marker}{' ' + scope_col if scope_col else ''}"
    out = [f"{tool:12s}: {arrow:<10}  {state_col:<11}  {time_col:>5}"]
    if not a_ok:
        before_msgs = {e.get("msg") for e in (before.get("errors") or [])} if before else set()
        new = [e for e in (after.get("errors") or []) if e.get("msg") not in before_msgs]
        # The `+` prefix means "introduced by this op". With no baseline,
        # `before_msgs` is empty and every finding qualifies — the third place
        # on this line where the absence is read as a measurement.
        bullet = " " if b_unknown else "+"
        for e in new[:5]:
            line_n = f"L{e['line']}" if e.get("line") else "  "
            code = e.get("code") or ""
            msg = _flat_cell(e.get("msg") or "", 120)
            out.append(f"  {bullet} {line_n} {code}  {msg}")
        if len(new) > 5:
            out.append(f"  {bullet} ... +{len(new) - 5} more"
                       f"{'' if b_unknown else ' new'}")
    return out


def _validators_run_batch(
    applicable: Dict[str, Dict[str, Any]], path: str,
    doc_maybe_stale: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Run all validators on path. Parallel if `parallel >= 2` in config.

    `doc_maybe_stale` is forwarded to every adapter — see _validator_run_one.
    The baseline pass passes False (it is the pass that causes the staleness);
    the post-edit pass passes True whenever a warm daemon could still be
    holding the pre-edit document (#482).
    """
    workers = _parallel_workers()
    if workers >= 2 and len(applicable) > 1:
        from concurrent.futures import ThreadPoolExecutor
        max_workers = min(workers, len(applicable))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {name: ex.submit(_validator_run_one, name, spec, path,
                                       doc_maybe_stale)
                       for name, spec in applicable.items()}
            return {name: f.result() for name, f in futures.items()
                    if f.result() is not None}
    out: Dict[str, Dict[str, Any]] = {}
    for name, spec in applicable.items():
        data = _validator_run_one(name, spec, path, doc_maybe_stale)
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
                           env=run_env, encoding="utf-8", errors="replace")
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
        _note_not_checked({name: data})
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
                           env=(_merged_env if _prefix_env else None), encoding="utf-8", errors="replace")
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
    # Identity is decided on the path the WRITER lands on, not the one the
    # caller typed. For `link.py -> target.py` where the target does not exist
    # yet, `isfile(link.py)` follows the link, finds nothing and returns False —
    # which the rollback read as "this call created link.py". It then unlinked a
    # symlink the call never created, left the target it really did write, and
    # printed `nothing changed on disk` over both (#1136).
    #
    # Sampled once here, before the op, and reused by every rollback arm below:
    # resolving again at rollback time would answer a question about a
    # filesystem the write has already changed.
    _target = _write_target(path)
    _pre_existed = os.path.isfile(_target)
    # Every rollback arm reports on the object it acted on, refuse included: a
    # refusal that names the link while the restore beside it names the target
    # would describe two different files as one.
    _target_cell = _flat_cell(_target)
    if os.path.abspath(_target) != os.path.abspath(path):
        _target_cell += f" (which the symlink {_flat_cell(path)} resolves to)"
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
    # Set when the formatter loop below has already undone the write. The
    # validator loop then has nothing left to undo, and on the create path
    # "nothing left" is not a no-op: a second `os.unlink` of a path the first
    # one removed raises FileNotFoundError, which would print `[ROLLBACK
    # FAILED]` under a rollback that had in fact succeeded (#1088). Restoring
    # bytes twice was idempotent, so this only became reachable when unlink
    # joined the set of undos.
    already_undone = False
    if applicable_fmt:
        fmt_results = _formatters_run_batch(applicable_fmt, path)
        for result in fmt_results:
            if not result["ok"]:
                result_name = result.get("name", "")
                if result_name in applicable_fmt and applicable_fmt[result_name].get("rollback_on_fail"):
                    # Same three states as the validator loop below (#1088): a
                    # formatter that fails on a file this op created has an undo
                    # too, and it is unlink rather than "nothing to do".
                    fmt_action = _rollback_action(_pre_existed, pre_content)
                    if fmt_action == "refuse":
                        row = _formatter_render_row(result)
                        if row:
                            fmt_rows.append(row)
                        fmt_rows.append(
                            f"[ROLLBACK NOT POSSIBLE] {result_name} failed on "
                            f"{_target_cell}, whose pre-edit bytes could "
                            f"not be read. The write STANDS (#1088).")
                    else:
                        try:
                            if fmt_action == "unlink":
                                os.unlink(_target)
                            elif pre_content is not None:
                                with open(_target, "wb") as fw:
                                    fw.write(pre_content)
                            _retract_write(path)
                            already_undone = True
                            fmt_rows.append(_retraction_line(
                                result_name, "failed", path, body,
                                created=fmt_action == "unlink",
                                target=_target))
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

    # Stop warm daemons for this new file before the inline validators run, so
    # they cold-start with the file indexed (see _new_file_servers above). Same
    # list covers any deferred slow-tier validators drained later by main().
    for _srv in _new_file_servers:
        _mcp_stop_server(_srv)

    # The baseline pass above is what opened this file in any warm LSP daemon,
    # and cclsp's diagnostics cache is never invalidated for the daemon's life
    # — so a validator querying it now is being answered about the pre-edit
    # bytes (#482). Two conditions clear the flag: a file that did not exist
    # pre-op was never opened by the baseline, and a daemon just SIGTERM'd for
    # a new file (#239) comes back cold with the current bytes indexed.
    _doc_maybe_stale = _pre_existed and not _new_file_servers
    after_results = (_validators_run_batch(applicable, path, _doc_maybe_stale)
                     if applicable else {})
    _note_not_checked(after_results)
    diff_lines: list = []
    for name in applicable:  # stable order from config
        if name in after_results:
            diff_lines.extend(_validator_render_diff(before.get(name), after_results[name]))

    diff_out = "\n".join(diff_lines) + ("\n" if diff_lines else "")

    if needs_rollback:
        # Decided from the result dicts, not from the rendered rows: a scan for
        # a ✗ on a line starting with the validator's name reverted `phpstan`
        # whenever `phpstan-mcp` went red, and could not tell a skip from a
        # finding at all (#406).
        #
        # NOT gated on `pre_content is not None` any more (#1088). That gate
        # conflated "there is something to restore" with "there is something to
        # undo", so a file this op CREATED — which has the second and not the
        # first — skipped the loop entirely and survived its own failed
        # validation, with the red row printed above it.
        for name, spec in applicable.items():
            if not spec.get("rollback_on_fail"):
                continue
            after_data = after_results.get(name)
            if after_data is None or not _validator_regressed(before.get(name), after_data):
                continue
            if already_undone:
                # A formatter already retracted this write. Reporting the
                # validator's finding is still right; undoing a second time is
                # not, and on the create path it would fail loudly against a
                # path that is already gone.
                break
            action = _rollback_action(_pre_existed, pre_content)
            if action == "refuse":
                diff_out += (
                    f"\n[ROLLBACK NOT POSSIBLE] {name} regressed on "
                    f"{_target_cell}, whose pre-edit bytes could not be "
                    f"read. The file existed before this op, so removing it "
                    f"would delete content this call never wrote. The write "
                    f"STANDS and the file is NOT what it was (#1088).\n"
                )
                break
            try:
                if action == "unlink":
                    os.unlink(_target)
                elif pre_content is not None:
                    with open(_target, "wb") as f:
                        f.write(pre_content)
                _retract_write(path)
                diff_out += ("\n" + _retraction_line(
                    name, "regressed", path, body,
                    created=action == "unlink", target=_target) + "\n")
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


_UNTRUSTED_FLAT: Optional[Callable[[str], str]] = None
_UNTRUSTED_FLAT_TRIED = False


def _flat_field(text: str) -> str:
    """A value the tool prints on its own line, kept to one line (#881).

    The guarantee this establishes, stated so a parser can rely on it: **a
    ``validate:`` header is exactly one line, whatever path it was handed.**
    Not "one line for the paths we expected" — a filename is whatever the
    filesystem accepted, and on POSIX that includes newlines. A worktree file
    named ``evil\\nvalidate: forged.py\\nok : ok\\n.py`` used to emit three
    header lines for one file, and the caller that folds blocks back to files
    positionally then attributed a forged clean verdict to a file that does not
    parse (#881). The same defect as #876 with the filename echoed one file
    over.

    Implemented by `presets/_untrusted.flat`, which is the repo's answer to
    this exact question and shipped in this same release for the worktrees
    board — one guarantee with one implementation, because a second copy of a
    rule beside the real one is what these issues are about. Loaded by path,
    the way `presets/mcp/_paths.py` already is.

    The fallback, for an install without `presets/`, is not a second copy of
    that rule: `str.isprintable()` is false for every control character
    including the newline, and `repr()` of any `str` is one line by the
    language's own definition. An ordinary path is printable and passes through
    byte-identical either way, so nothing about normal output moves.

    "One line" is measured against `str.splitlines()`, the ten separators the
    consumer folds on — not against the newline. The preset covered eight of
    them when this consolidation shipped and the fallback covered all ten, so
    the install *without* `presets/` was the safe one for a release (#886).
    Recorded because the argument for consolidating was "one guarantee, one
    implementation", which was right in shape and unverified in fact:
    consolidation is a win only once the survivor is the stronger of the two.
    """
    global _UNTRUSTED_FLAT, _UNTRUSTED_FLAT_TRIED
    if not _UNTRUSTED_FLAT_TRIED:
        _UNTRUSTED_FLAT_TRIED = True
        try:
            import importlib.util
            _u_path = os.path.join(_INSTALL_DIR, "presets", "_untrusted.py")
            _u_spec = importlib.util.spec_from_file_location(
                "_supertool_untrusted", _u_path)
            if _u_spec is not None and _u_spec.loader is not None:
                _u_mod = importlib.util.module_from_spec(_u_spec)
                _u_spec.loader.exec_module(_u_mod)
                _UNTRUSTED_FLAT = getattr(_u_mod, "flat", None)
        except Exception:
            _UNTRUSTED_FLAT = None
    if _UNTRUSTED_FLAT is not None:
        return _UNTRUSTED_FLAT(text)
    return text if text.isprintable() else repr(text)


def _validate_one_block(path: str, validators: dict, verbose: bool = False) -> List[str]:
    """Render the validator rows for a single ``path`` (no trailing newline join).

    Returns the lines for one ``validate: PATH`` block — shared by the
    single-file and multi-file forms so they stay byte-identical per file.

    The header carries the path through `_flat_field`, which is what makes
    "one block per file" a guarantee rather than an expectation (#881). Every
    validator still runs on the real unflattened `path`.

    The header is not the only place the path is echoed, and this docstring
    used to say it was (#895). `_validator_render_row` prints it back through
    `resolved_to`, and every shipped subprocess adapter reproduces it inside
    `msg` — so the rows carry the same guarantee, via `_flat_cell`. Stated
    here because a reader who believes the sentence above stops looking.
    """
    out = [f"validate: {_flat_field(path)}"]
    had_finding = False
    had_non_verdict = False
    ran_any = False
    for name, spec in validators.items():
        glob = spec.get("match", "*")
        if path and glob and not _match_glob(path, glob):
            continue
        data = _validator_run_one(name, spec, path)
        if data is None:
            continue
        # The three states, tallied for the run's own footer (#990). `skipped`
        # counts towards "not checked" here even though #665 refused to
        # ESCALATE it: an optional tool nobody installed still checked nothing,
        # and saying so on a count line does not gate anything on it.
        ran_any = True
        if "skipped" in data or _validator_no_verdict(data) is not None:
            had_non_verdict = True
        elif data.get("ok") is False:
            had_finding = True
        _note_not_checked({name: data})
        out.extend(_validator_render_row(data, verbose=verbose))
    # A file no validator's `match` glob selected is NOT a clean file. It is the
    # emptiest block this function can emit — no rows at all — and counting it
    # towards `0 not checked` would make "we own no checker for this type" and
    # "every checker passed" the same number, which is the absence-read-as-
    # presence defect the footer exists to prevent. `presets/git/resolve.py`
    # already distinguishes the two (an empty block digests to `None`, rendered
    # as nothing); the count has to agree with it.
    _acc_validated().append((path, had_finding, had_non_verdict or not ran_any))
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
    fold each block back to its source file. Exactly one per file, whatever the
    files are called — the header is flattened, so a filename cannot write a
    second one (#881). Config is loaded once for the whole batch — the
    throughput win over shelling ``validate:PATH`` per file.

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
    if re.match(r"^\.+\w*(?:\.\w+)*\Z", symbol) or symbol in (".", ".."):  # \Z — #1188
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
    if re.match(r"^[A-Za-z0-9_-]+\Z", symbol):  # \Z, not $ — #1188
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


# Same signature as its sibling below; `from_file` is the `path` argument.
def _parse_imports(path: str, content: str) -> List[tuple]:
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
            capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
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
                capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
            )
            branch = branch_r.stdout.strip() if branch_r.returncode == 0 else "?"
            # ahead/behind
            ab_r = subprocess.run(
                ["git", "rev-list", "--left-right", "--count", f"{branch}...@{{u}}"],
                capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
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
                capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
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
                capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
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
                capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
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
            colon1 = hit.find(":", _start)
            colon2 = hit.find(":", colon1 + 1) if colon1 != -1 else -1
            if colon1 == -1 or colon2 == -1:
                # Only the heuristic grep path guarantees `file:line:content`.
                # An MCP `refs` server answers in its own shape — cclsp, which
                # this repo's own config routes `*.py` to, leads with a prose
                # header and bullet lines — and `.index()` on those raised
                # ValueError out of op_workspace, surfacing as "ERROR:
                # argument parsing: substring not found". Show the line as the
                # server wrote it: dropping it would trade the loud failure
                # for a quiet one, and inventing a line number for it would be
                # worse than either.
                current_file = ""
                out.append(f"{hit}\n")
                continue
            hit_file = hit[:colon1]
            lineno = hit[colon1 + 1:colon2]
            content = hit[colon2 + 1:]
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


def _undecodable_staged_paths(raw: str) -> str:
    """A warning line naming staged paths that are not valid UTF-8, or ``""``.

    ``git diff -z`` emits raw path bytes — ``-z`` turns off the octal quoting
    that would otherwise keep porcelain ASCII — so a filename in latin-1 comes
    back holding U+FFFD after ``errors="replace"``. That name no longer refers
    to a file, ``os.path.isfile`` says no, and the entry drops out of the list
    with nothing said: a pre-commit gate that silently declines to check one of
    the files being committed. Naming it is the only honest outcome, because
    the mangled name cannot be reopened to check it either.
    """
    bad = [p for p in raw.split("\x00") if p and _undecodable_at(p) >= 0]
    if not bad:
        return ""
    return (
        f"WARNING: {len(bad)} staged path(s) are not valid UTF-8 and were NOT "
        f"checked — rename them or check them by hand: {', '.join(bad)}\n"
    )


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
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            msg = (r.stderr.strip() or "git diff failed")
            return f"ERROR: {msg}\n"
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"ERROR: git unavailable: {e}\n"

    # Split on NUL (git diff -z), reject empty + symlinks + paths outside cwd.
    staged = []
    unreadable = _undecodable_staged_paths(r.stdout)
    for p in r.stdout.split("\x00"):
        if not p or _undecodable_at(p) >= 0:
            continue
        if os.path.islink(p) or not os.path.isfile(p):
            continue
        # Reject paths that resolve outside cwd (symlink-following could leak).
        real = os.path.realpath(p)
        root = os.path.realpath(os.getcwd())
        if real != root and not real.startswith(root + os.sep):
            continue
        staged.append(p)
    if not staged:
        return (unreadable or "") + "no staged files\n"

    parts = []
    if unreadable:
        parts.append(unreadable.rstrip("\n"))
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
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            msg = (r.stderr.strip() or "git diff failed")
            return f"ERROR: {msg}\n"
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"ERROR: git unavailable: {e}\n"

    staged = []
    unreadable = _undecodable_staged_paths(r.stdout)
    for p in r.stdout.split("\x00"):
        if not p or _undecodable_at(p) >= 0:
            continue
        if os.path.islink(p) or not os.path.isfile(p):
            continue
        real = os.path.realpath(p)
        root = os.path.realpath(os.getcwd())
        if real != root and not real.startswith(root + os.sep):
            continue
        staged.append(p)
    if not staged:
        return (unreadable or "") + "no staged files\n"

    parts = []
    if unreadable:
        parts.append(unreadable.rstrip("\n"))
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


_TOML_HEXDIGITS = frozenset("0123456789abcdefABCDEF")

_TOML_ESCAPE_ADVICE = (
    "in a basic string a backslash must be escaped as a double backslash, or "
    "use a triple-single-quote literal block, which keeps backslashes as typed"
)


def _toml_decode_escape(s: str, i: int, key: str, multiline: bool) -> Tuple[str, int]:
    r"""Decode the escape starting at s[i] (a backslash); return (text, offset).

    Raises on everything TOML calls invalid, because stdlib `tomllib` raises
    and this parser stands in for it on Python <3.11 (#684). The default it
    replaces — keep the escaped character, drop the backslash — turned
    `path = "C:\Users\dev"` into `C:Usersdev`, and the op then reported a
    missing file at an address the parser had invented: same payload, same
    tool, a parse error on 3.11+ and a manufactured absence below it.

    `\u` / `\U` are decoded rather than rejected. tomllib accepts them, so
    refusing them would trade a silent divergence for a loud one. Agreement
    with tomllib is what matters here, not severity.
    """
    c = s[i + 1] if i + 1 < len(s) else ""
    if c in _TOML_ESCAPES:
        return _TOML_ESCAPES[c], i + 2
    if c in ("u", "U"):
        width = 4 if c == "u" else 8
        digits = s[i + 2:i + 2 + width]
        if len(digits) == width and all(d in _TOML_HEXDIGITS for d in digits):
            code = int(digits, 16)
            if code < 0x110000 and not 0xD800 <= code <= 0xDFFF:
                return chr(code), i + 2 + width
        raise ValueError(
            f"invalid escape: \\{c} for '{key}' wants {width} hex digits naming "
            f"a Unicode scalar — {_TOML_ESCAPE_ADVICE}"
        )
    if multiline:
        j = i + 1
        while j < len(s) and s[j] in " \t":
            j += 1
        if j < len(s) and s[j] in "\r\n":
            while j < len(s) and s[j] in " \t\r\n":
                j += 1
            return "", j
    raise ValueError(f"invalid escape '\\{c}' for '{key}' — {_TOML_ESCAPE_ADVICE}")


def _toml_basic_unescape(s: str, key: str = "value", multiline: bool = True) -> str:
    out = []
    i = 0
    while i < len(s):
        if s[i] == "\\":
            text, i = _toml_decode_escape(s, i, key, multiline)
            out.append(text)
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


def _toml_multiline_close(
    raw: str, i: int, quote: str, escaped: bool
) -> Tuple[int, int, int]:
    """Locate the closing run of a TOML multi-line string opened at *i*.

    Returns `(content_end, run_start, next_index)`, or `(-1, -1, -1)` when the
    block is never closed.

    The subtlety this exists for: a closing run may be **four or five** quotes,
    and the surplus one or two belong to the content. That is not a curiosity —
    it is the only way a multi-line string can end with its own delimiter
    character, and a payload carrying quoted code meets it immediately. The
    fallback parser used to stop at the first three quotes and then choke on the
    leftovers, so that spelling parsed under stdlib `tomllib` (3.11+) and failed
    below it. #834, and the same rule as #684: the escape hatch has to exist on
    every interpreter, or the advice naming it is wrong exactly where it is
    needed most.

    A run of six or more is capped at five, which leaves a stray quote for the
    caller to trip over — the error `tomllib` raises on the same input.
    """
    n = len(raw)
    j = i
    while j < n:
        if escaped and raw[j] == chr(92):
            j += 2
            continue
        if raw[j] != quote:
            j += 1
            continue
        run = j
        while j < n and raw[j] == quote:
            j += 1
        length = j - run
        if length < 3:
            continue
        if length > 5:
            length = 5
            j = run + 5
        return run + (length - 3), run, j
    return -1, -1, -1


def _toml_parse_value(raw: str, i: int, key: str) -> Tuple[Any, int]:
    """Parse one TOML value at *i*; return (value, offset just past it).

    Split out of `_mini_toml_loads` so inline arrays can recurse into it
    rather than reimplementing every scalar form.
    """
    n = len(raw)
    if raw[i:i + 3] == '"""':
        i += 3
        end, _run, nxt = _toml_multiline_close(raw, i, '"', True)
        if end < 0:
            raise ValueError(f"unterminated \"\"\" for '{key}'")
        val: Any = _toml_basic_unescape(raw[i:end], key, True)
        if val.startswith("\r\n"):
            val = val[2:]
        elif val.startswith("\n"):
            val = val[1:]
        return val, nxt
    if raw[i:i + 3] == "'''":
        i += 3
        end, _run, nxt = _toml_multiline_close(raw, i, "'", False)
        if end < 0:
            raise ValueError(f"unterminated ''' for '{key}'")
        val = raw[i:end]
        if val.startswith("\r\n"):
            val = val[2:]
        elif val.startswith("\n"):
            val = val[1:]
        return val, nxt
    if raw[i] == '"':
        i += 1
        buf = []
        while i < n and raw[i] != '"':
            if raw[i] == "\\":
                text, i = _toml_decode_escape(raw, i, key, False)
                buf.append(text)
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


# Where a relative `@payload` reference resolves from, and where the working
# directory was moved to. Both are set by main() before any chdir, and only
# when a chdir actually happens — so dispatch() called on its own (MCP mode,
# tests) keeps resolving against os.getcwd() exactly as it always did.
_INVOCATION_DIR: Optional[str] = None
_CWD_SHIFT: Optional[str] = None       # label of what moved the cwd, e.g. "cwd:"


def _at_root() -> str:
    """Directory a relative `@payload` reference resolves against.

    A `@reference` is an argument the caller typed, so it belongs to the
    directory the call was made from — not to the repo being operated on.
    `path = ` *inside* the payload is the other kind of path and keeps
    following the working directory, which is what makes `cwd:` useful.
    """
    return _INVOCATION_DIR or os.getcwd()


def _resolve_at_path(rel: str) -> str:
    """Absolute path for a relative `@payload` reference. No existence check."""
    if os.path.isabs(rel):
        return rel
    return os.path.join(_at_root(), rel)


def _at_file_missing_msg(rel: str) -> str:
    """Error for an unresolvable `@payload`, distinguishing absence from a moved root.

    "not found" states an absence in the world. When the working directory has
    moved out from under a relative reference, the absence is one the tool
    produced, and saying so is the difference between a zero-call debug and a
    two-call one. Both roots are named; neither is silently searched — reading
    whichever file happens to exist is how a tool starts opening one the caller
    never meant (#672).
    """
    root = _at_root()
    here = os.getcwd()
    head = f"@file not found: {rel}"
    if os.path.isabs(rel) or os.path.realpath(root) == os.path.realpath(here):
        return head
    label = _CWD_SHIFT or "cwd:"
    alt = os.path.join(here, rel)
    lines = [
        head,
        f"  {mark('↳')} @payload paths resolve against the invocation directory: {root}",
    ]
    if os.path.isfile(alt):
        lines.append(
            f"    It does exist under the {label} target {here}, and is not read from there: "
            "the @reference is an argument you typed, not repo content. Only `path =` inside "
            "the payload follows the working directory."
        )
        lines.append(f"    Pass an absolute path (@{alt}), or write the payload next to the call.")
    else:
        lines.append(
            f"    The {label} target is {here} — moving the working directory does not move "
            "the @reference."
        )
        lines.append("    Present under neither directory: check the path, or pass an absolute one.")
    return chr(10).join(lines)


# How much of the offending block the refusal echoes back, so the suggested
# spelling is recognisably the caller's own line and not a generic example.
_TOML_LITERAL_TAIL_CHARS = 48


def _toml_literal_backslash_message(head: str, run: int) -> str:
    """Name both spellings the refused backslash could have meant (#834)."""
    q = "'"
    tail = head[-_TOML_LITERAL_TAIL_CHARS:]
    lead = "…" if len(head) > len(tail) else ""
    want = run if run > 3 else 4
    quoted = q * 3 + lead + tail + q * want
    basic = chr(34) * 3 + lead + tail + chr(92) * 2 + chr(34) * 3
    arrow = mark("↳")
    return (
        "a " + q * 3 + " literal block ends with a backslash immediately before "
        "its closing quotes. Inside a literal block a backslash is content, "
        "never an escape, so it is written to the file as typed and the line "
        "that reaches the compiler is not the one you meant." + chr(10)
        + "  " + arrow + " to end the content with a quote, drop the backslash "
        "and let the closing run carry it (a literal block may end with 1 or 2 "
        + q + "):" + chr(10) + "      " + quoted + chr(10)
        + "  " + arrow + " to end the content with a backslash, write the block "
        "as a basic one, where it doubles:" + chr(10) + "      " + basic + chr(10)
    )


def _toml_literal_backslash_refusal(raw: str) -> str:
    """Refuse a payload whose literal block ends with an inert backslash (#834).

    The issue was filed as "a string ending in an apostrophe writes broken
    code", and that premise is wrong: a literal block ends with an apostrophe
    perfectly well, by letting the closing run carry it. Refusing on a trailing
    quote would refuse the correct spelling -- the check would fire on its own
    fix.

    What actually broke the reported write is the backslash before the closer.
    The caller typed it out of escape reflex; in a literal block it is inert,
    so the value ended with a stray backslash and the Python written from it
    parsed as something else. The op reported success, the validators agreed,
    and the failure surfaced a language away from its cause.

    This refuses rather than warns for one reason, and it is the reason that
    decides the severity of every guard in this family: **both** readings of
    that backslash have another spelling -- drop it, or move to a basic block
    where it doubles -- so refusing leaves nothing unwritable. A guard whose
    refusal would strand a legitimate intent has to warn instead; see
    `docs/validators.md`, "Declining instead of guessing".
    """
    opener = chr(39) * 3
    i = raw.find(opener)
    while i >= 0:
        _end, run, nxt = _toml_multiline_close(raw, i + 3, chr(39), False)
        if run < 0:
            return ""
        if raw[run - 1:run] == chr(92):
            return _toml_literal_backslash_message(raw[i + 3:run - 1], nxt - run)
        i = raw.find(opener, nxt)
    return ""


# How many fields one double-backslash note names before it stops counting.
# A note that lists nine fields is a wall nobody finishes; the first few locate
# the mistake, and the total is carried in the count.
_PAYLOAD_DBS_MAX_FIELDS = 3

# A run of EXACTLY two backslashes. Longer runs are deliberately not matched:
# three or four were counted, not produced by escape reflex, and firing on the
# deliberate case is how a warning stops being read.
_EXACT_DOUBLE_BACKSLASH = re.compile(r"(?<!\\)\\{2}(?!\\)")

# The bare key immediately preceding a value, read backwards off the source.
_PAYLOAD_KEY_BEFORE_VALUE = re.compile(r"([A-Za-z0-9_.\-]+)[ \t]*=[ \t]*$")

# Notes raised while a payload was parsed, drained by dispatch at depth 1.
# A `batch:@file` parses its payload ONCE, in the outer frame, before any
# sub-op runs -- draining per sub-op would file the note inside an unrelated
# op's receipt, which is the wrong place for the one line that says a write
# may not be what its author wrote.
_PAYLOAD_WARNINGS: List[str] = []


# A `[[ops]]` table header, at the start of a line. Counted before a finding's
# offset to name WHICH op a field belongs to (#1087): a batch of six that
# reported a bare `new` cost the reader a hand re-derivation of which op it
# meant -- the one fact the scanner already had.
_TOML_OPS_HEADER = re.compile(r"(?m)^[ \t]*\[\[[ \t]*ops[ \t]*\]\]")


def _toml_literal_double_backslashes(raw: str) -> List[Tuple[str, str, str, int]]:
    r"""`(key, label, first line, count)` per literal block holding a `\\` pair.

    Provenance is the whole point and it is read off the source, not the parsed
    value: in a basic block `\\` IS one backslash and is the correct spelling,
    so a guard that could not tell the two blocks apart would fire on its own
    remedy. Scanning the literal blocks in `raw` directly answers the question
    exactly rather than heuristically.

    `key` is the bare field name, which is what decides whether the pair is
    write-bound. `label` is what a human is shown -- `ops[2].new` inside a
    batch, and the bare key outside one.
    """
    findings: List[Tuple[str, str, str, int]] = []
    opener = chr(39) * 3
    i = raw.find(opener)
    while i >= 0:
        end, run, nxt = _toml_multiline_close(raw, i + 3, chr(39), False)
        if run < 0:
            break
        content = raw[i + 3:end]
        key_m = _PAYLOAD_KEY_BEFORE_VALUE.search(raw[:i])
        key = key_m.group(1) if key_m else "?"
        hit = _EXACT_DOUBLE_BACKSLASH.search(content)
        if hit and key.lower() != "op":
            total = len(_EXACT_DOUBLE_BACKSLASH.findall(content))
            start = content.rfind(chr(10), 0, hit.start()) + 1
            stop = content.find(chr(10), hit.start())
            line = content[start:] if stop < 0 else content[start:stop]
            seen = len(_TOML_OPS_HEADER.findall(raw[:i]))
            label = key if seen == 0 else "ops[" + str(seen - 1) + "]." + key
            findings.append(
                (key, label, line.strip()[:_TOML_LITERAL_TAIL_CHARS], total))
        i = raw.find(opener, nxt)
    return findings


def _payload_double_backslash_note(raw: str) -> str:
    r"""Name a `\\` inside a triple-single-quoted block -- warn, not rewrite (#1027).

    What is left here after #1087 is the half that never reaches disk. A
    doubled `old` cannot match, so the runner reports the skip -- but only
    AFTER the anchor has missed, and only in the language of a failed match.
    This says the same thing one call earlier and in the words that name the
    cause. The half that DID land bytes -- `new`, `content` -- is refused by
    `_payload_double_backslash_refusal` and never reaches this function.

    **It warns, and it does not rewrite.** Collapsing `\\` to `\` would guess at
    intent, and a wrong guess is strictly worse than the bug it replaces: the
    caller loses even the ability to read back what they asked for. The tool's
    job here is to say "you wrote `\\` inside a block that will not process it"
    and leave the decision where it belongs.

    **It warns only for fields that are NOT written** (#1087). The write-bound
    ones -- `_PAYLOAD_DBS_WRITE_KEYS` -- go to
    `_payload_double_backslash_refusal` instead, because the note fired after
    the bytes had landed and every validator passed them.

    The reason this started as a warning was real: #834 and #835 fire at a
    FIXED position, immediately before the closing quotes and at the end of a
    shell line, where every reading has a second spelling, so a refusal strands
    nothing. This pattern has no position, and refusing it with no way out would
    make a payload that legitimately writes a pair unwritable at every offset.
    What changed is that there is now a way out: `literal_backslashes = true`
    (#1096). A suppressible refusal is strictly better than an unsuppressible
    warning -- the suppression is a decision the author records in the payload
    rather than one the tool makes for them.

    What is left here is the half that was always safe: `old` is an anchor, a
    doubled one cannot match, the runner reports the skip, and nothing reaches
    disk. `vim`'s `script` is also left as a note -- it is an instruction
    language, and the tool cannot say from a payload what bytes the file ends
    up holding.

    The line between signal and noise is drawn at two places and nowhere else:

    * **Literal blocks only.** A basic block spells one backslash with two;
      flagging that would flag the fix.
    * **Runs of exactly two.** Three or more were counted deliberately.

    It is deliberately NOT narrowed to "escape-looking" sequences. The reported
    cases were `\\d`, `\\302` and `\\n`; `\d` and `\302` are not TOML escapes,
    so the reflex being caught is generic, not TOML-specific, and any escape set
    narrow enough to be a filter would miss the report it was written for.
    Measured instead: 710 of 208854 lines in this repository carry a `\\` --
    0.34%, and this is the pathological corpus, since its densest files are
    tests ABOUT backslash handling. A note at that rate is not one an author
    learns to skip.
    """
    findings = [f for f in _toml_literal_double_backslashes(raw)
                if f[0].lower() not in _PAYLOAD_DBS_WRITE_KEYS]
    if not findings:
        return ""
    bs = chr(92)
    arrow = mark("↳")
    lines = [
        mark("⚠") + " payload: a " + chr(39) * 3 + " literal block carries `"
        + bs * 2 + "`. A literal block processes NO escapes, so each pair reaches "
        "the file as TWO backslashes -- if you meant one, write one." + chr(10)
    ]
    for _key, label, line, total in findings[:_PAYLOAD_DBS_MAX_FIELDS]:
        lines.append(
            "  " + arrow + " `" + label + "` (" + str(total) + " occurrence"
            + ("" if total == 1 else "s") + "): " + line + chr(10)
        )
    rest = findings[_PAYLOAD_DBS_MAX_FIELDS:]
    if rest:
        # Named, not counted (#1087). `and 1 further field` withholds exactly
        # the identifier the reader needs and sends them back to re-derive by
        # hand which of six ops it meant -- a warning that costs a manual
        # reconstruction is close to no warning at all.
        lines.append(
            "  " + arrow + " and " + str(len(rest)) + " more: "
            + ", ".join("`" + f[1] + "`" for f in rest) + chr(10)
        )
    lines.append(
        "  " + arrow + " this is a note, NOT a correction -- nothing was "
        "rewritten, because a pair is sometimes exactly what was meant and "
        "guessing here is worse than the bug. None of the fields above is "
        "written to a file: a doubled `old` cannot match and is reported as a "
        "skip. The write-bound fields (" + ", ".join(sorted(_PAYLOAD_DBS_WRITE_KEYS))
        + ") are refused instead. (#1027, #1087)" + chr(10)
    )
    return "".join(lines)


# Fields whose value lands verbatim as file bytes. A doubled backslash here is
# the only half of #1027 that reaches disk -- `old` is an anchor that cannot
# match, and `vim`'s `script` is an instruction language where the tool cannot
# say what the file ends up holding, so neither is refused.
#
# Kept as a set rather than derived from the @file registry: the registry knows
# a field's POSITION, not whether its bytes are written, and a rule that
# refused every non-`old` field would refuse `path` -- where a Windows payload
# spells a separator with exactly two backslashes and is correct.
_PAYLOAD_DBS_WRITE_KEYS = frozenset({"new", "content"})

# The one key that says "I meant two characters" (#1096).
_PAYLOAD_LITERAL_BS_KEY = "literal_backslashes"


def _payload_literal_backslashes_optin(parsed: Any) -> bool:
    """True if the payload declares its doubled backslashes deliberate (#1096).

    Top level only, and payload-wide. A per-field key multiplies with every
    content field an op has; a key inside an `[[ops]]` table READS as scoped to
    that op and could not be -- the detector works off the raw source, where op
    boundaries are a line-counting heuristic rather than a fact. Shipping a flag
    whose apparent scope is not its real scope would be a worse lie than the
    one being fixed, so a payload whose ops genuinely differ in intent is two
    payloads. `_payload_literal_backslashes_misplaced` says so out loud.
    """
    return isinstance(parsed, dict) and parsed.get(_PAYLOAD_LITERAL_BS_KEY) is True


def _payload_literal_backslashes_misplaced(parsed: Any) -> str:
    """Refuse `literal_backslashes` set inside an `[[ops]]` table (#1096).

    An author who set it there stated an intent, and honouring it at a scope the
    tool cannot implement is not an option -- but neither is ignoring it. A
    silently dropped flag refuses the write while the payload says it was
    allowed, which is this tracker's own defect class: the receipt and the
    payload disagreeing about what was asked for.
    """
    if not isinstance(parsed, dict):
        return ""
    ops = parsed.get("ops")
    if not isinstance(ops, list):
        return ""
    for idx, entry in enumerate(ops):
        if isinstance(entry, dict) and _PAYLOAD_LITERAL_BS_KEY in entry:
            return (
                "`" + _PAYLOAD_LITERAL_BS_KEY + "` is set inside `ops[" + str(idx)
                + "]`, where it does nothing. It is read at the TOP LEVEL of the "
                "payload only, and it applies to every op the payload carries -- "
                "the doubled-backslash scan runs once, over the raw source, "
                "before any op does. Move it to the top level if that is what "
                "you meant; split the payload if the ops differ. (#1096)"
            )
    return ""


def _payload_double_backslash_refusal(parsed: Any, raw: str) -> str:
    """Refuse a payload that would WRITE a doubled backslash (#1087).

    #1027 made this a note and gave a good reason: the pattern has no fixed
    position, so refusing it would make a payload that legitimately writes two
    characters unwritable at every offset -- the loud-for-quiet trade this repo
    rules out by name. The reason held only while there was no way to say "I
    meant two". `literal_backslashes` (#1096) is that way, so the refusal is
    suppressible, and a suppressible refusal is strictly better than an
    unsuppressible warning: the suppression is a decision the author records in
    the payload rather than one the tool makes on their behalf.

    Scoped to the fields whose bytes land. `old` keeps the note -- a doubled
    anchor cannot match, the runner reports the skip, and nothing reaches disk,
    so refusing it would cost a round-trip on a call that was already safe.

    Fires at parse time, before any op runs. That is why a `batch` is covered
    by construction and why nothing has to be rolled back: on the reported
    `paste` the file was created, every validator passed (two backslashes are
    legal in every language this repo edits), and the author found out from
    behaviour a CI round later.
    """
    if _payload_literal_backslashes_optin(parsed):
        return ""
    findings = [f for f in _toml_literal_double_backslashes(raw)
                if f[0].lower() in _PAYLOAD_DBS_WRITE_KEYS]
    if not findings:
        return ""
    bs = chr(92)
    named = "; ".join(
        "`" + label + "` (" + str(total) + "x): " + line
        for _key, label, line, total in findings
    )
    return (
        "a " + chr(39) * 3 + " literal block carries `" + bs * 2 + "` in a field "
        "that is WRITTEN to the file, and a literal block processes NO escapes "
        "-- each pair would reach disk as TWO backslashes, pass every validator, "
        "and be wrong only in string contents. " + named + ". If you meant one "
        "backslash, write one. If you meant two, add `"
        + _PAYLOAD_LITERAL_BS_KEY + " = true` at the top level of the payload "
        "and this refusal becomes a decision you recorded. (#1087, #1096)"
    )


def _eol_backslash_pair(text: str) -> Optional[Tuple[str, int]]:
    r"""First line in *text* ending with an even run of backslashes, if any.

    Returns `(line without the run, run length)`. Even is the whole point: bash
    consumes backslashes pairwise from the left, so an even run is all escaped
    backslashes and the line genuinely ends -- an odd run leaves one over and is
    the continuation the caller thought they were writing. A run followed by
    whitespace is skipped here and left to `_sh_backslash_warning`; see
    `_payload_sh_eol_backslash_refusal` for why the two halves part company.
    """
    for m in _TRAILING_BACKSLASH_RUN.finditer(text):
        if m.group(2) or len(m.group(1)) % 2:
            continue
        start = text.rfind(chr(10), 0, m.start()) + 1
        return text[start:m.start()], len(m.group(1))
    return None


def _sh_eol_backslash_message(line: str, run: int) -> str:
    """Name both spellings the refused end-of-line backslashes could have meant."""
    q = chr(39) * 3
    bs = chr(92)
    arrow = mark("↳")
    tail = line[-_TOML_LITERAL_TAIL_CHARS:]
    lead = "…" if len(line) > len(tail) else ""
    return (
        "a " + q + " literal block writes a shell file whose line ends with "
        + str(run) + " backslashes. In a literal block a backslash is content, "
        "never an escape, so all " + str(run) + " reach the file — and in bash "
        "an even run at end of line is an escaped backslash, not a line "
        "continuation. It parses cleanly, `bash -n` and `bash-check` agree, and "
        "the script runs differently." + chr(10)
        + "  " + arrow + " to continue the line, write ONE backslash — a literal "
        "block does not eat it:" + chr(10)
        + "      " + lead + tail + bs + chr(10)
        + "  " + arrow + " to write " + str(run) + " literal backslashes, say so "
        "in a basic block, where each doubles:" + chr(10)
        + "      " + chr(34) * 3 + lead + tail + bs * (run * 2) + chr(34) * 3
        + chr(10)
    )


def _payload_dicts(node: Any) -> List[Dict[str, Any]]:
    """Every mapping in a parsed payload -- the op itself, or each `[[ops]]`."""
    found: List[Dict[str, Any]] = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found.extend(_payload_dicts(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_payload_dicts(value))
    return found


def _payload_sh_eol_backslash_refusal(parsed: Any, raw: str) -> str:
    r"""Refuse a literal block that ends a shell line with `\\` (#835).

    `_sh_backslash_warning` (#380) already reads this pattern out of the bytes
    on their way to disk, and warns. The warning is right, and the write went
    through anyway -- which is the issue: a guard that is certain should stop
    the write, a guard that is heuristic should warn, and mixing the two in one
    channel costs the certain ones their authority.

    The severity is not decided by how certain the pattern is. It is decided by
    #834's rule -- refuse when every intent behind it has another spelling, warn
    when refusing would strand one -- and that rule splits this guard rather
    than promoting it whole:

    * From a triple-single-quoted literal payload block the two backslashes are
      **ambiguous**, and both readings have another spelling. Meant one,
      expecting TOML to eat the other? A literal block eats nothing: write one.
      Meant two? Say it in a basic block, where a wanted pair is spelled with
      four. Refusing leaves nothing unwritable, so it refuses -- at parse time,
      before any op of a batch has run.

    * At the write chokepoint there is no second spelling at all. A block there
      reads whole-file content, so one deliberate `echo \\` on line 400 would
      make every later edit to that script impossible, and no payload field
      fixes that for the colon CLI, which has no fields. That is the
      intent-stranding the rule forbids, so `_sh_backslash_warning` stays a
      warning for every other route into the same bytes.

    The issue proposed `allow_literal_backslash = true`. It is not built: the
    basic block is the opt-out, it already exists, and it says *which* of the
    two intents was meant rather than merely silencing the question.

    Provenance is read off `raw` rather than threaded through the parser. A
    value carrying two backslashes appears in its own source verbatim only if it
    came from a literal block -- a basic block spells the same pair with four,
    so the parsed value is not a substring of the source it was parsed from.

    The backslash-then-whitespace half of #380 is deliberately not refused: a
    basic block writes an escaped space exactly as a literal one does, so that
    reading has no second spelling to be sent to.
    """
    for item in _payload_dicts(parsed):
        path = item.get("path")
        if not isinstance(path, str) or not path.endswith(_SH_SUFFIXES):
            continue
        for key, value in item.items():
            if key in ("path", "op") or not isinstance(value, str):
                continue
            found = _eol_backslash_pair(value)
            if found and value in raw:
                return _sh_eol_backslash_message(*found)
    return ""


def _take_payload_warnings() -> str:
    """Drain the parse-time payload notes, or "" if there are none.

    Every path out of `dispatch` that follows a `_load_at_file` has to call
    this. A note parked in a global and drained only at the bottom of the
    function survives each early `return` in between, and then prints attached
    to whatever op runs next -- a claim about a payload that op never had. That
    is this repository's own defect class, produced by the fix for it, so the
    queue is emptied at the exits rather than at one of them.
    """
    if not _PAYLOAD_WARNINGS:
        return ""
    out = "".join(_PAYLOAD_WARNINGS)
    _PAYLOAD_WARNINGS.clear()
    return out


def _load_at_file(ref: str, note: bool = True) -> Any:
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
    if ref == "@payload" or ref.startswith("@payload "):
        # A batch sub-op that ran from a payload is echoed as
        # `op:@payload → target` (#644). That header is deliberately not
        # re-runnable: the fields it ran from cannot be flattened onto a colon
        # CLI without becoming a different op. Say so, rather than letting it
        # fall through to a bare "@file not found: payload", which reads as a
        # missing file and invites the reader to go looking for one.
        raise ValueError(
            "'@payload' is a header placeholder, not a reference. This op ran "
            "from an @payload whose fields no single-colon header can reproduce "
            "(#644) — re-run it from the original payload file or stdin."
        )
    if ref == "@-":
        raw = sys.stdin.read()
        source = "<stdin>"
    else:
        fpath = ref[1:]  # strip leading @
        resolved = _resolve_at_path(fpath)
        if not os.path.isfile(resolved):
            raise ValueError(_at_file_missing_msg(fpath))
        try:
            with open(resolved, "r", encoding="utf-8") as _f:
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
        parsed = parser(raw)
    except Exception as _e:
        raise ValueError(
            f"@file TOML parse error ({source}): {_e}{_toml_delimiter_hint(raw)}"
        ) from _e
    refusal = _toml_literal_backslash_refusal(raw)
    if refusal:
        raise ValueError(f"@file payload refused ({source}): {refusal}")
    refusal = _payload_sh_eol_backslash_refusal(parsed, raw)
    if refusal:
        raise ValueError(f"@file payload refused ({source}): {refusal}")
    # `note=False` for the read-op route: the note is about the write path, and
    # a `grep` pattern is a regex rather than file content -- nothing lands, the
    # doubled backslash there is a different question with a different answer,
    # and raising it would be noise on an op that cannot misfile a byte. The
    # refusals below are scoped the same way and for the same reason (#1087).
    if note:
        refusal = _payload_literal_backslashes_misplaced(parsed)
        if refusal:
            raise ValueError(f"@file payload refused ({source}): {refusal}")
        refusal = _payload_double_backslash_refusal(parsed, raw)
        if refusal:
            raise ValueError(f"@file payload refused ({source}): {refusal}")
        text = _payload_double_backslash_note(raw)
        if text:
            _PAYLOAD_WARNINGS.append(text)
    return parsed


# Dynamic @file field registry — built lazily from op syntax strings.
# Maps op name → ordered list of JSON field names (positional parts[1..N]).
# Populated on first dispatch call via _build_at_file_registry().
_AT_FILE_REGISTRY: Dict[str, List[Tuple[str, bool, bool]]] = {}
_AT_FILE_REGISTRY_BUILT: bool = False

# Ops whose syntax string contains ':::' (so a @payload route was clearly
# intended) but whose derived field names were discarded by the identifier
# guard in _fields_from_syntax — e.g. inline prose or punctuation that no
# payload key could ever match. Populated alongside _AT_FILE_REGISTRY.
#
# This is NOT the same as an op with no ':::' at all (a read-only op, the
# common and correct case, which never appears here). Conflating the two
# would make this list mostly noise; keeping them apart is what lets a test
# — or a human reading a failure message — ask "was this route dropped on
# purpose, or did a syntax edit just delete it?" (#770). Nothing reads this
# list at runtime; it exists so a test failure can explain itself.
_AT_FILE_DROPPED_ROUTES: List[Tuple[str, str]] = []


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


# Read ops that accept `op:@file` / `op:@-`. Deliberately NOT routed through
# _AT_FILE_BUILTIN_DEFAULTS: that registry rebuilds a positional parts list and
# hands it back to the colon parsers, which would re-split the very pattern the
# payload exists to protect. These are dispatched straight to the op (#625).
_READ_OP_AT_FIELDS: Dict[str, Tuple[str, ...]] = {
    "grep":        ("pattern", "path", "limit", "context", "count", "no_auto_read"),
    "around":      ("pattern", "path", "n"),
    "grep_around": ("pattern", "path", "n", "limit"),
    "between":     ("symbol", "start", "end", "path"),
    "read":        ("path", "offset", "limit", "grep", "full"),
    # `validate` is here for the mirror-image reason (#878). Its problem is not
    # a pattern that may contain ':' but a *path list* that may: the colon form
    # `validate:f1,f2,…:FILTER` joins on both ':' and ',', and a filename may
    # legally contain either — as may every absolute path on Windows, whose
    # drive letter is a colon. There is no escape in that form and no amount of
    # sender-side filtering recovers one; only a channel that never re-splits
    # does.
    "validate":    ("path", "paths", "tools", "verbose"),
}


def _payload_strlist(p: Dict[str, Any], key: str) -> List[str]:
    """A payload field that is either one string, or a list of them.

    A comma-separated string is accepted for `tools` because that is what the
    colon form already means there. It is NOT accepted for a path list: commas
    are legal in filenames, and re-splitting on one is the defect the payload
    route exists to avoid.
    """
    value = p.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v)]
    raise ValueError(
        f"field {key!r} must be a string or a list of strings, "
        f"got {type(value).__name__}"
    )


def _validate_from_payload(p: Dict[str, Any]) -> str:
    """Run the `validate` op from a payload, so a path may contain ':' or ','.

    `paths` (a list) is the form that motivates the route; `path` is accepted
    as the singular spelling every other read op uses. Field semantics are the
    colon form's, unchanged: >1 file dispatches the list form and `tools` scopes
    the validator selection.

    **Containment applies to every path, not to the list form.** The first
    version of this function guarded only the `len > 1` branch, on the stated
    ground that this was parity with dispatch. It was not: dispatch applies
    containment twice — generically at `_PATH_ARG_POSITIONS`, which every op
    gets, and *additionally* in the list branch, where position 1 is a
    comma-joined blob the generic gate cannot read. Replicating only the second
    reproduced the special case and skipped the rule (#882). The gate now sits
    one level up, at the top of `_read_op_from_payload`, where the same call
    covers this op's `path` and `paths` *and* the four read ops that had no
    gate at all (#885) — one call for the whole route, so no door into it can
    disagree with another about a path.
    """
    files = _payload_strlist(p, "paths") or _payload_strlist(p, "path")
    if not files:
        return ("ERROR: @payload for op 'validate' missing required field "
                "'path' (or 'paths' for the list form)\n")
    raw_tools = p.get("tools")
    if isinstance(raw_tools, str):
        tools = [t for t in raw_tools.split(",") if t]
    else:
        tools = _payload_strlist(p, "tools")
    verbose = _payload_bool(p, "verbose")
    if len(files) > 1:
        return op_validate_multi(files, tools or None, verbose=verbose)
    return op_validate(files[0], tools or None, verbose=verbose)


def _payload_int(p: Dict[str, Any], key: str, default: int) -> int:
    value = p.get(key)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"field '{key}' must be an integer, got {value!r}") from None


def _payload_grep_limit(p: Dict[str, Any], default: int) -> int:
    """`limit` for the grep family, where the string `all` is a value (#1328).

    The colon CLI and the payload route have to accept the same LIMIT or the
    token is a feature of one spelling — and the payload route is the one that
    exists for the patterns the CLI cannot express, which is exactly where a
    call-site sweep with an alternation ends up.
    """
    value = p.get("limit")
    # Matched exactly, as the colon CLI matches it and as `count` /
    # `no-auto-read` are matched there. A payload that accepted `All` while the
    # CLI read the same token as a path name would make the spelling a property
    # of the route; `_payload_int` refuses it by name instead.
    if value == _GREP_ALL_TOKEN:
        return GREP_LIMIT_ALL
    return _payload_int(p, "limit", default)


def _payload_bool(p: Dict[str, Any], key: str) -> bool:
    value = p.get(key, False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _read_op_from_payload(op: str, payload: Any, no_exclude: bool = False) -> str:
    """Run a read op from an @file/@- payload — the colon-free route (#625).

    The things worth grepping for contain ':' by nature: PHP `Class::CONST`,
    log prefixes, assertion messages, timestamps, and alternations whose last
    branch ends in one. The colon CLI has to guess where the pattern stops; a
    payload never has to. Same @file/@- shape the mutating ops already use, so
    it is one rule applied consistently rather than two to remember.

    `validate` joins the set for the same reason read from the other end: there
    the ambiguous field is the pattern, here it is the file list (#878).
    """
    if isinstance(payload, list) or (
        isinstance(payload, dict) and isinstance(payload.get("ops"), list)
    ):
        return (f"ERROR: this payload is an ops array — use 'batch:@file' "
                f"instead of '{op}:@file'\n")
    if not isinstance(payload, dict):
        return (f"ERROR: @payload for op '{op}' must be a JSON object / TOML "
                f"table, got {type(payload).__name__}\n")
    p = {str(k).lower(): v for k, v in payload.items()}
    allowed = _READ_OP_AT_FIELDS[op]
    unknown = sorted(k for k in p if k not in allowed)
    if unknown:
        return (f"ERROR: unknown field(s) {', '.join(unknown)} in {op}:@payload "
                f"— accepted: {', '.join(allowed)}\n")
    try:
        # Containment, before any op sees a path (#885). `op_read` is caught by
        # the `render_file` chokepoint and `validate` gated itself, but
        # `op_grep`, `op_around` and both `op_between_*` have no check of their
        # own: they rely on the `_PATH_ARG_POSITIONS` gate in `_dispatch_impl`,
        # which this route returns before ever reaching. A payload therefore
        # returned the contents of any file on disk, with a regex the caller
        # chose — strictly worse than #882, which was an oracle and an
        # argument. One call here rather than a `_safe_path` beside each op:
        # re-implementing the rule locally is what produced #882, and a fourth
        # copy would drift the same way. Both fields are read with
        # `_payload_strlist`, which accepts a string or a list, because
        # `validate` accepts a list under `path` as well as under `paths` — a
        # `str()` here stringified that list into one nonsense name that
        # resolves under cwd, and the op then used the real one.
        contained = _containment_error(
            [*_payload_strlist(p, "path"), *_payload_strlist(p, "paths")])
        if contained:
            return contained
        if op in ("grep", "grep_around", "around"):
            pattern = str(p.get("pattern", "") or "")
            if not pattern:
                return (f"ERROR: @payload for op '{op}' missing required "
                        f"field 'pattern'\n")
            path = str(p.get("path") or ".")
            if op == "grep":
                p_limit = _payload_grep_limit(
                    p, _get_op_int("grep", "max_results", MAX_GREP_RESULTS))
                if p_limit == 0:
                    return _GREP_ZERO_LIMIT
                return op_grep(
                    pattern, path, p_limit,
                    _payload_int(p, "context", 0),
                    _payload_bool(p, "count"),
                    no_exclude=no_exclude,
                    no_auto_read=_payload_bool(p, "no_auto_read"),
                )
            if op == "grep_around":
                return op_grep(pattern, path, _payload_grep_limit(p, 10),
                               _payload_int(p, "n", 3), False,
                               no_exclude=no_exclude)
            return op_around(pattern, path, _payload_int(p, "n", 10))
        if op == "between":
            path = str(p.get("path", "") or "")
            symbol = str(p.get("symbol", "") or "")
            start = str(p.get("start", "") or "")
            end = str(p.get("end", "") or "")
            if symbol and (start or end):
                return ("ERROR: between:@payload takes EITHER 'symbol' (symbol "
                        "mode) OR 'start' + 'end' (pattern mode), not both\n")
            if symbol:
                return op_between_symbol(symbol, path)
            if start and end:
                return op_between_pattern(start, end, path)
            return ("ERROR: @payload for op 'between' needs 'symbol' (symbol "
                    "mode) or 'start' + 'end' (pattern mode)\n")
        if op == "validate":
            return _validate_from_payload(p)
        path = str(p.get("path", "") or "")
        if not path:
            return "ERROR: @payload for op 'read' missing required field 'path'\n"
        offset = _payload_int(p, "offset", 0)
        limit = _payload_int(p, "limit", 0)
        body = op_read(path, offset, limit, str(p.get("grep", "") or ""),
                       _payload_bool(p, "full"))
        return body + _read_range_note(path, offset, limit, body)
    except ValueError as exc:
        return f"ERROR: {exc}\n"


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
    dropped: List[Tuple[str, str]] = []
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
            elif ":::" in re.split(r"\s*\|\s*", syntax)[0]:
                # The guard fired, not a read-only op: syntax carries ':::' —
                # a @payload route was intended — but the derived field
                # names weren't clean identifiers, so it was discarded.
                dropped.append((op_name, syntax))
    _AT_FILE_REGISTRY = registry
    _AT_FILE_DROPPED_ROUTES[:] = dropped
    _AT_FILE_REGISTRY_BUILT = True


def _at_file_specs(op: str) -> List[Tuple[str, bool, bool]]:
    """Return (name, optional, variadic) specs for *op*, or [] if no @file route."""
    _build_at_file_registry()
    return _AT_FILE_REGISTRY.get(op, [])


def _at_file_dropped_routes() -> List[Tuple[str, str]]:
    """Ops whose ':::'-bearing syntax had its @payload route discarded by the
    identifier guard in _fields_from_syntax — see _AT_FILE_DROPPED_ROUTES.
    """
    _build_at_file_registry()
    return list(_AT_FILE_DROPPED_ROUTES)


def _at_file_fields(op: str) -> List[str]:
    """Return the field NAMES for *op*, or [] if the op has no @file route.

    Kept name-only for the truthiness/sub-op callers; field semantics
    (optional, variadic) live in _at_file_specs.
    """
    return [name for name, _opt, _var in _at_file_specs(op)]


def _at_file_payload_hint(op: str) -> str:
    """Name the payload keys *op* wants, and show a call that would work.

    An error that names the fault but not the remedy is on this tracker's own
    list. Three agents in one evening met either a bare TOML line/column or
    "takes the @reference as the only argument", and each of them guessed the
    key names from scratch; one mangled its own commit message to get past the
    shell instead, which is permanent in that history (#1003).

    The keys come from the same registry that drives the route, so this can
    never drift into describing a payload shape the loader would reject.
    Returns "" for an op with no @file route, leaving its error untouched.
    """
    specs = _at_file_specs(op)
    if not specs:
        return ""
    quote = "'" * 3
    names = ", ".join(
        name + ("[]" if variadic else "") + (" (optional)" if optional else "")
        for name, optional, variadic in specs
    )
    lines = [
        f"  {op}:@... reads its fields from the payload. Keys: {names}",
        f"    ./supertool '{op}:@-' <<'EOF'",
    ]
    for name, _optional, variadic in specs:
        if variadic:
            lines.append(f'    {name} = ["path/to/file"]')
        else:
            lines.append(f"    {name} = {quote}...{quote}")
    lines.append("    EOF")
    return chr(10) + chr(10).join(lines)


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
    for _path, indices in by_file.items():
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

# Per-op accumulators live on the dispatch frame, not in the process-global
# (#1109). `validate` is in `_PARALLEL_SAFE_OPS`, so under SUPERTOOL_PARALLEL
# six ops append to `_VALIDATED_FILES` / `_NOT_CHECKED` at once — and the footer
# used to be built by snapshotting `len()` at op entry and slicing `[before:]`
# at op exit. That arithmetic is per-op only while exactly one op is appending;
# with six in flight, every footer but one claimed files its op never opened,
# and `with findings` travelled the same way. A lock around the appends would
# have made them orderly and left the slices exactly as wrong — the defect is
# not a missing lock, it is per-op state kept somewhere per-op does not exist.
#
# Two scopes, deliberately, because two readers want two different answers:
#
#   * the FOOTER describes one op. It reads this frame's own list, which is
#     exact by construction — there is no snapshot left to get wrong.
#   * the EXIT CODE describes the whole call. `main` still reads the
#     process-global, so `$SUPERTOOL_REQUIRE_VALIDATORS` keeps firing under
#     parallel dispatch. Giving each op its own list and stopping there would
#     have traded a miscount for a gate that silently stopped gating, which is
#     the louder-bug-for-quieter-bug trade docs/validators.md warns about.
#
# `_acc_pop` is what joins them: every frame flushes into the frame that
# displaced it, and the outermost frame's parent is the process-global. A
# batch's sub-ops append one frame deeper and roll up into the batch's own
# footer, which is what they did before.
_ACC_FLUSH_LOCK = _threading.Lock()


def _acc_not_checked() -> List[str]:
    """This dispatch frame's not-checked names — the call's, outside one.

    The fallback is not decoration: `_drain_validator_queue` runs in `main`
    after every op has returned, with no frame installed, and what it records
    still belongs to the call's exit code.
    """
    buf = getattr(_DISPATCH_STATE, "acc_not_checked", None)
    return _NOT_CHECKED if buf is None else buf


def _acc_validated() -> List[Tuple[str, bool, bool]]:
    """This dispatch frame's validated-file rows — the call's, outside one."""
    buf = getattr(_DISPATCH_STATE, "acc_validated", None)
    return _VALIDATED_FILES if buf is None else buf


def _acc_push() -> Tuple[Optional[List[str]],
                         Optional[List[Tuple[str, bool, bool]]]]:
    """Install fresh per-op lists, returning the ones they displace."""
    prev = (getattr(_DISPATCH_STATE, "acc_not_checked", None),
            getattr(_DISPATCH_STATE, "acc_validated", None))
    _DISPATCH_STATE.acc_not_checked = []
    _DISPATCH_STATE.acc_validated = []
    return prev


def _acc_pop(prev: Tuple[Optional[List[str]],
                         Optional[List[Tuple[str, bool, bool]]]]) -> None:
    """Restore `prev` and flush this frame's rows into it, or into the globals.

    Called from `dispatch`'s `finally`, so an op that raised still hands its
    rows upward instead of stranding them on a thread a pool will reuse.
    """
    mine_not_checked = getattr(_DISPATCH_STATE, "acc_not_checked", None) or []
    mine_validated = getattr(_DISPATCH_STATE, "acc_validated", None) or []
    prev_not_checked, prev_validated = prev
    _DISPATCH_STATE.acc_not_checked = prev_not_checked
    _DISPATCH_STATE.acc_validated = prev_validated
    # Taken unconditionally, and only the outermost hand-off needs it: a parent
    # frame's list is thread-local and cannot be contended, while the
    # process-global is shared by every worker thread of a parallel dispatch,
    # and `list.extend` is not a promise the free-threaded build makes (3.13t+,
    # the same reason the depth counter above is thread-local). Skipping it on
    # the nested path would save an uncontended acquire — tens of nanoseconds,
    # once per frame — in exchange for a branch deciding whether this list is
    # the shared one, which is the kind of thing a later refactor gets wrong
    # silently. Cheap and unconditional beats clever and conditional here.
    with _ACC_FLUSH_LOCK:
        (_NOT_CHECKED if prev_not_checked is None
         else prev_not_checked).extend(mine_not_checked)
        (_VALIDATED_FILES if prev_validated is None
         else prev_validated).extend(mine_validated)
# Parsed at module scope, so a bad value here used to raise during *import* and
# take down every op in the tool, most of which have nothing to do with dispatch
# depth. The widest blast radius of the #654 class, for the smallest knob.
_DISPATCH_MAX_DEPTH = _env_int("SUPERTOOL_DISPATCH_MAX_DEPTH", 32, minimum=1)


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
    # Cleared by the OUTERMOST frame only: a batch sub-op that refuses has to
    # be able to set a flag its parent still carries when it returns.
    if depth == 0:
        _DISPATCH_STATE.call_failed = False
    if depth >= _DISPATCH_MAX_DEPTH:
        _mark_op_failure()
        return (
            f"ERROR: dispatch recursion limit ({_DISPATCH_MAX_DEPTH}) exceeded "
            f"— check for a self-referencing batch payload\n"
        )
    _DISPATCH_STATE.depth = depth + 1
    _acc_prev = _acc_push()
    try:
        out = _dispatch_impl(arg, pre_parsed)
        # The edit ops read with surrogateescape and echo the buffer in their
        # receipts, so a receipt can hold lone surrogates that no UTF-8 stream
        # can encode. Sanitised once, at the outermost frame, because every
        # consumer (CLI stdout, the MCP server, a batch sub-op's caller) hits
        # the same wall (#1059).
        return _display_safe(out) if depth == 0 else out
    finally:
        _DISPATCH_STATE.depth = depth
        _acc_pop(_acc_prev)
        # An op outside the read-only set may have moved the index without
        # moving any file's mtime — `git-commit` is the everyday case — so the
        # repo-wide status snapshot cannot speak for the next op (#1126).
        # Keyed off the same predicate as parallel dispatch because it asks the
        # same question, and an unrecognised or custom op is unsafe by default.
        if not _is_parallel_safe(arg):
            _path_meta_bulk_drop()
        # _FORMATTER_SKIPS is module-level and drained on the normal return
        # path. An exception escaping _dispatch_impl skips that drain, and the
        # next top-level call would report skips belonging to a call that
        # already died. The outermost frame owns the reset either way.
        if depth == 0:
            _FORMATTER_SKIPS.clear()


def dispatch_verdict(
    arg: str, pre_parsed: "Optional[Tuple[List[str], bool]]" = None
) -> "Tuple[str, bool]":
    """`dispatch`, plus the structural answer to "did this call refuse".

    The verdict is set by whichever frame produced the refusal — this one, or
    a batch sub-op nested under it — and read back off the same thread. It is
    never re-derived from the string returned here (#1291).
    """
    # Establish the flag before reading it, rather than inheriting one.
    #
    # `_call_failed()` is a pure read of a thread-local, and the only clear
    # lived inside `dispatch` at depth 0 — a function the two lines below
    # record as one callers REPLACE. When they do, the clear never runs and
    # this returns whatever last refused on this thread: `master` went red on
    # macOS only at df34db5, and the batch tally said "all 2 refused" in words
    # about two ops that had not (#1359).
    #
    # Before the call, never after: a sub-op refusing at any depth must still
    # reach the parent's verdict, which is the whole reason only depth 0
    # clears it inside `dispatch`. That arrangement is unchanged — this adds
    # the same reset one level out, where the reader of the bit lives.
    #
    # The sibling counters in `_main` — `_SKIP_COUNT`, `_ROLLBACK_COUNT`,
    # `_NOT_CHECKED` — are all read as per-call deltas for exactly this
    # reason (#680). This bit was the one member of the group with neither.
    _DISPATCH_STATE.call_failed = False

    # One positional argument when there is nothing else to pass, because
    # `main` has always called `dispatch(arg)` and both the tests and the MCP
    # layer monkeypatch it with that arity. Widening the call here would have
    # been a compatibility break bought for nothing.
    out = dispatch(arg) if pre_parsed is None else dispatch(arg, pre_parsed)
    return out, _call_failed()


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
    # How this call's fields were separated, for the ops that have to know:
    # ':::', ':', or '' when nothing was tokenized at all because the fields
    # arrived structured. Three states rather than two — a payload's fields
    # were never split, and a refusal that says they were is a claim about a
    # parse that did not run (#946).
    _arg_sep: str = ""
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
            _arg_sep = ":::"
        else:
            parts = _split_arg(arg)
            _arg_sep = ":"
        op = parts[0] if parts else ""

    # Read-op @payload route — 'grep:@file' / 'around:@-' etc. (#625).
    #
    # Gated on the reference actually resolving ('@-', or an existing file)
    # rather than on the leading '@' alone: `grep:@Override:src/` is a real and
    # common search, and must keep meaning what it always meant. A pattern that
    # merely starts with '@' therefore falls through untouched — only a genuine
    # payload reference is intercepted.
    if (
        pre_parsed is None
        and len(parts) >= 2
        and parts[1].startswith("@")
        and op in _READ_OP_AT_FIELDS
        and (
            parts[1] == "@-"
            or os.path.isfile(_resolve_at_path(parts[1][1:]))
            # Resolvable only under the moved-to root: still a payload reference,
            # so route it in and let _load_at_file explain which root was searched
            # rather than falling through to a bare "file not found: @…" (#672).
            or os.path.isfile(parts[1][1:])
            # Resolvable under neither root, but payload-shaped: a lone `@….toml`
            # / `@….json` argument. Routing it in only changes which error is
            # printed — an unresolvable reference reads no file either way — and
            # it is the case that most needs the two roots named. Extension-gated
            # so `grep:@Override:src/` keeps falling through as the search it is.
            or (
                len(parts) == 2
                and parts[1][1:].lower().endswith((".toml", ".json"))
            )
        )
    ):
        if len(parts) > 2:
            return _receipt(header, (
                f"ERROR: {op}:@... takes the @reference as the only argument "
                f"(e.g. {op}:@payload.toml or {op}:@-). Put fields in the "
                f"payload, not on the colon CLI.\n"
            ))
        try:
            _read_payload = _load_at_file(parts[1], note=False)
        except ValueError as _e:
            _mark_op_failure()
            return header + _take_payload_warnings() + f"ERROR: {_e}\n"
        # The warnings lead the body, so the verdict is taken from the op's
        # own answer rather than from whatever ends up first on the line.
        _read_warnings = _take_payload_warnings()
        _read_body = _read_op_from_payload(
            op, _read_payload, no_exclude=no_exclude)
        if _op_body_failed(_read_body):
            _mark_op_failure()
        return header + _read_warnings + _read_body

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
            return _receipt(header, (
                f"ERROR: {op}:@... takes the @reference as the only argument "
                f"(e.g. {op}:@payload.json or {op}:@-). Put fields in the "
                f"JSON/TOML payload, not on the colon CLI."
                + _at_file_payload_hint(op) + chr(10)
            ))
        try:
            payload = _load_at_file(parts[1])
            parts, _at_file_replace_all = _at_file_to_parts(op, payload)
            _at_file_used = True
            # The colon prefix got us here; the FIELDS came from the payload
            # and no separator touched them.
            _arg_sep = ""
        except ValueError as _e:
            # A payload that would not load, or one that loaded without the
            # fields the op needs. Both leave the caller knowing their call was
            # wrong and not what a right one looks like — #1003 for the whole
            # reasoning, and for the commit message that was mangled instead.
            # The payload warnings drain first (#1027): a doubled backslash is
            # a plausible cause of the very failure being reported, so it
            # belongs above the error rather than after the remedy.
            return (header + _take_payload_warnings() + f"ERROR: {_e}"
                    + _at_file_payload_hint(op) + chr(10))

    # When parts come from @file (JSON/TOML payload), they hold literal
    # bytes — backslashes and newlines must NOT be reinterpreted as shell-
    # style escapes. Only colon-CLI input needs `_decode_escapes`.
    _dec = (lambda s: s) if _at_file_used else _decode_escapes

    # Published for the preset subprocess launcher, which is several frames
    # down and receives only `parts`. Set on every frame rather than once per
    # call: a batch sub-op arrives through its own frame with its own route.
    _ARG_SEP[0] = _arg_sep

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
    #
    # Not for a payload-sourced sub-op (`pre_parsed`): its `arg` is already the
    # honest `op:@payload → target` header the batch loop synthesized, and
    # eliding THAT would replace a truthful line with a summary of fields the
    # caller never typed on a colon CLI. The swap below is also gated on the op
    # having written, which for a payload op meant a FAILING one fell back to
    # the flattened lie — at the one moment a reader is reconstructing what
    # happened. #644.
    _compact_header = ""
    if pre_parsed is None and len(arg) > _HEADER_ARG_MAX:
        _compact_header = _compact_header_arg(op, parts, _arg_sep)
    # None until a custom op runs in this frame; then its exit status. Read
    # rather than sniffed off the receipt, for the reason stated at the swap
    # below — a preset writes no file, so `_WRITE_COUNT` cannot speak for it.
    _custom_op_ok: Optional[bool] = None
    _writes_before = _WRITE_COUNT[0]
    _attempts_before = _MUTATION_ATTEMPTS[0]
    _skips_before = _SKIP_COUNT[0]
    _reapplies_before = _REAPPLY_COUNT[0]
    _rollbacks_before = _ROLLBACK_COUNT[0]

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
        # grep_around:PATTERN:PATH — parts[2] and nothing else, because its
        # trailing slots must parse as ints, so a ':' in the pattern fails the
        # call rather than moving the path.
        "grep_around": (2,),
        # `grep` and `around` are deliberately absent: neither keeps its path
        # in a fixed slot. `_parse_grep_args`/`_parse_around_args` peel the
        # trailing ints and take `path = args[-1]`, so a ':' anywhere in the
        # PATTERN pushes the path past slot 2 and the gate saw a pattern
        # fragment instead — arbitrary read, contents and all (#1166). Read
        # from the other side it also over-contained: with a ':' in the
        # pattern, slot 2 IS a pattern fragment, so searching a local file for
        # an absolute-path string was refused while naming a file the caller
        # never asked to open. Both branches gate the path they computed.
        # hover:SYMBOL:FILE, rename:OLD:NEW:FILE, resolve:SYMBOL[:FROM_FILE]
        "hover": (2,), "rename": (3,), "resolve": (2,),
        # diff:PATH1:PATH2
        "diff": (1, 2),
        # `between` is deliberately absent: neither of its readings keeps its
        # path in a fixed slot. Symbol mode takes parts[-1] (a ':' in the
        # symbol pushes the path past slot 2, and nothing gated parts[3] —
        # #1163), `re:` mode joins parts[4:] (a ':' in the path leaves only
        # the first fragment gated). And slot 2 is the START *regex* in the
        # `re:` reading, so gating it refused a legitimate local slice while
        # naming a file the caller never asked for (#1164). Both branches
        # gate the path they computed, at the point they computed it.
        # check:PRESET:PATH — runs a custom op, path forwarded as {file}.
        "check": (2,),
        # mutating ops (also covered by _atomic_write chokepoint):
        "edit": (3,), "replace": (3,), "replace_dry": (3,),
        "replace_lines": (1,), "paste": (1,), "append": (1,), "vim": (1,),
    }
    _containment = _containment_error(
        parts[_pos] for _pos in _PATH_ARG_POSITIONS.get(op, ()) if _pos < len(parts)
    )
    if _containment:
        return _receipt(header, _containment)

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
                        return _receipt(
                            header, "ERROR: read range START must be >= 1\n")
                    if r_end < r_start:
                        return _receipt(header, (
                            f"ERROR: read range END ({r_end}) is before "
                            f"START ({r_start})\n"
                        ))
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
                    return _receipt(header, (
                        f"ERROR: read:PATH:START-END takes no LIMIT "
                        f"(got {parts[3]!r}) — the range already bounds it\n"
                    ))
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
            body = op_read(path, offset, limit, grep_filter, force_full,
                           range_form)
            if not range_form:
                body += _read_range_note(path, offset, limit, body)
        elif op == "grep":
            pattern, path, limit, context, count_only, no_auto_read = \
                _parse_grep_args(parts)
            # Ahead of the hint, not after it: `_colon_split_hint` stats the
            # path to decide whether to fire, and a stat of an outside file is
            # itself the existence oracle this gate exists to close.
            _contained = _containment_error([path])
            if _contained:
                return _receipt(header, _contained)
            if limit == 0:
                return _receipt(header, _GREP_ZERO_LIMIT)
            if limit == GREP_LIMIT_ALL_MISPLACED:
                return _receipt(header, _GREP_ALL_OUTSIDE_LIMIT_SLOT)
            _hint = _colon_split_hint("grep", pattern, path)
            if _hint:
                return _receipt(header, _hint)
            body = op_grep(pattern, path, limit, context, count_only,
                           no_exclude=no_exclude, no_auto_read=no_auto_read)
        elif op == "grep_around":
            # grep_around:PATTERN:PATH[:N[:LIMIT]] — every match with N lines
            # context. Sane defaults for "show me how everyone uses this".
            ga_pattern = parts[1] if len(parts) > 1 else ""
            ga_path = parts[2] if len(parts) > 2 and parts[2] else "."
            if len(parts) > 3 and parts[3] == _GREP_ALL_TOKEN:
                return _receipt(header, _GREP_AROUND_ALL_IN_N_SLOT)
            ga_context = int(parts[3]) if len(parts) > 3 and parts[3] else 3
            ga_limit_tok = parts[4] if len(parts) > 4 and parts[4] else ""
            if ga_limit_tok == _GREP_ALL_TOKEN:
                ga_limit = GREP_LIMIT_ALL
            else:
                ga_limit = int(ga_limit_tok) if ga_limit_tok else 10
            if ga_limit == 0:
                return _receipt(header, _GREP_ZERO_LIMIT)
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
            # Ahead of the delegation and the hint: both stat the path to
            # decide whether to fire, and that stat is the oracle. The #1135
            # guard inside the delegation covers the OTHER slot — parts[1],
            # once promotion has turned it into a filename — and still runs.
            _contained = _containment_error([path])
            if _contained:
                return _receipt(header, _contained)
            _delegated = _around_line_delegation(pattern, path, n)
            if _delegated:
                return _receipt(header, _delegated)
            _hint = _colon_split_hint("around", pattern, path)
            if _hint:
                return _receipt(header, _hint)
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
                    _contained = _containment_error([path])
                    if _contained:
                        return _receipt(header, _contained)
                    # between:re rejoins RIGHTWARD, so a ':' in START or END
                    # steals from the path rather than from the pattern — the
                    # opposite of grep/around, and the reason it needs its own
                    # hint (#625).
                    _hint = _colon_split_hint(
                        "between", f"{start_pat}:{end_pat}", path,
                        keys=("start", "end"),
                        # The default prefix would be `between:START:END`,
                        # dropping the `re:` marker that selects this mode —
                        # a printed repair nobody can run.
                        call_prefix=f"between:re:{start_pat}:{end_pat}",
                    )
                    if _hint:
                        return _receipt(header, _hint)
                    body = op_between_pattern(start_pat, end_pat, path)
                else:
                    body = ("ERROR: between:re: requires START:END:PATH "
                            f"(got {len(parts) - 2} args after 're')\n")
            elif len(parts) >= 3:
                # Symbol mode: between:SYMBOL:PATH
                # Join middle parts on ':' so a qualified name stays ONE
                # symbol instead of re-reading the call as re: mode. It does
                # not make `Foo::bar` resolve: the query is compared literally
                # against a definition's own name node, which is `bar` in PHP,
                # C++ and Ruby alike (measured, #1163).
                symbol = ":".join(parts[1:-1])
                path = parts[-1]
                # Ahead of the hints, not after them: both stat the path to
                # decide whether to fire, and a stat of an outside file is
                # itself the existence oracle this gate exists to close.
                _contained = _containment_error([path])
                if _contained:
                    return _receipt(header, _contained)
                _range_hint = _between_numeric_hint(parts)
                if _range_hint:
                    return _receipt(header, _range_hint)
                _hint = _colon_split_hint("between", symbol, path,
                                          keys=("symbol",))
                if _hint:
                    return _receipt(header, _hint)
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
                                        _mark_op_failure()
                                        if not continue_on_error:
                                            break
                                        continue
                                    _sub_op = _item.get("op", "")
                                    if not _sub_op:
                                        err = "ERROR: batch op missing 'op' field\n"
                                        results.append(err)
                                        _mark_op_failure()
                                        if not continue_on_error:
                                            break
                                        continue
                                    # Build the arg string from the op + its fields,
                                    # using the @file→parts machinery for mutating ops
                                    # (preserves validators) and plain dispatch for others.
                                    _sub_pre_parsed = None
                                    if _sub_op in _READ_OP_AT_FIELDS:
                                        # Read op with its own payload route (#625).
                                        # Dispatch straight to the op, exactly as a
                                        # standalone `grep:@-` does: its pattern or
                                        # symbol is precisely what a colon join cannot
                                        # survive, and re-serializing it here undid the
                                        # reason the payload route was built. #644.
                                        _read_payload_fields = {
                                            str(_k): _v for _k, _v in _item.items()
                                            if str(_k).lower() != "op"
                                        }
                                        _read_target = str(
                                            _read_payload_fields.get("path", "") or ""
                                        )
                                        # Dispatched straight to the op, so
                                        # no frame exists to take the verdict.
                                        # Taken here instead, off the op's own
                                        # return value (#1291).
                                        _read_body = _read_op_from_payload(
                                            _sub_op, _read_payload_fields
                                        )
                                        if _op_body_failed(_read_body):
                                            _mark_op_failure()
                                        _sub_result = (
                                            "--- "
                                            + _payload_header_arg(_sub_op, _read_target)
                                            + " ---\n"
                                            + _read_body
                                        )
                                        results.append(_sub_result)
                                        if not continue_on_error and _sub_result.split("\n")[1:2] and (
                                            _sub_result.split("\n")[1].startswith("ERROR")
                                        ):
                                            break
                                        continue
                                    if _at_file_fields(_sub_op):
                                        try:
                                            _sub_parts, _sub_replace_all = _at_file_to_parts(_sub_op, _item)
                                        except ValueError as _ve:
                                            err = f"ERROR: {_ve}\n"
                                            results.append(err)
                                            _mark_op_failure()
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
                                        # The header gets the same treatment as the
                                        # parts: NOT re-serialized to a colon string.
                                        # `":".join(_sub_parts)` produced a header that
                                        # parsed as a different op — see
                                        # _payload_header_arg. #644.
                                        _sub_field_names = _at_file_fields(_sub_op)
                                        _sub_target = ""
                                        if "path" in _sub_field_names:
                                            _sub_path_idx = _sub_field_names.index("path") + 1
                                            if len(_sub_parts) > _sub_path_idx:
                                                _sub_target = _sub_parts[_sub_path_idx]
                                        _sub_arg = _payload_header_arg(
                                            _sub_parts[0], _sub_target
                                        )
                                    else:
                                        # Op with neither a mutating nor a read payload
                                        # route. Fields are placed by declared order, or
                                        # the op declines — never by alphabetical key
                                        # order, which is not any op's argument order and
                                        # dispatched a different op outright. #644.
                                        _fields, _order_err = _ordered_batch_fields(_sub_op, _item)
                                        if _order_err:
                                            results.append(_order_err)
                                            _mark_op_failure()
                                            if not continue_on_error:
                                                break
                                            continue
                                        _sub_arg = ":".join([_sub_op] + _fields) if _fields else _sub_op
                                    _sub_result = dispatch(_sub_arg, pre_parsed=_sub_pre_parsed)
                                    results.append(_sub_result)
                                    # NOT the call's verdict — that was taken
                                    # inside the frame above and is already in
                                    # `_call_failed()`. This is `continue_on_
                                    # error`'s own question, and it is still
                                    # answered by line-indexing the rendered
                                    # string, so a sub-op argument holding a
                                    # newline puts the header on line 1 and the
                                    # batch runs on. Same mechanism as #1291
                                    # and deliberately not folded into it:
                                    # `_call_failed()` cannot tell `ERROR` from
                                    # `FAIL`, so reusing it here would silently
                                    # widen what stops a batch. Left for its
                                    # own decision.
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
                # Position 1 is the comma-joined blob, which the generic gate
                # above cannot read — so the individual files are checked here,
                # through the same helper.
                _v_contained = _containment_error(v_files)
                if _v_contained:
                    return _receipt(header, _v_contained)
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
            else:
                # `ops:gh-labels` used to discard its argument in silence and
                # print all 47KB — an unrecognised token dropped rather than
                # refused, in the op whose subject is which tokens exist
                # (#1231). `ops` and `ops-compact` share one arm rather than
                # one each: the first pass fixed `ops` and left `ops-compact`
                # swallowing the same token one `elif` over, which is how a
                # refusal that exists in one of two twinned branches reads as
                # a refusal that exists.
                ops_arg = parts[1] if len(parts) > 1 else ""
                if not ops_arg:
                    body = op_ops(compact=(op == "ops-compact"))
                elif ops_arg == "roster" and op == "ops":
                    body = op_ops_roster()
                else:
                    body = _ops_argument_refusal(ops_arg, op)
        else:
            # Fallthrough: try custom ops, then aliases
            custom = _resolve_custom_op(op, parts)
            if custom is not None:
                body = custom
                _custom_op_ok = _CUSTOM_OP_OK[0]
            else:
                alias = _resolve_alias(op, parts)
                if alias is not None:
                    body = alias
                else:
                    body = _unknown_op_message(op)
    except (ValueError, IndexError) as e:
        body = f"ERROR: argument parsing: {e}\n"

    # The verdict, taken here and nowhere else. `body` is what the op RETURNED
    # and the header has not been prepended yet, so the boundary this used to
    # go looking for is not in question. Everything below only decorates the
    # receipt — payload warnings lead it, a batch's `[result]` leads it — and
    # each of those pushes the verdict token off the line a body scan reads.
    #
    # A preset op is not a second population. `_resolve_custom_op` records
    # `result.returncode == 0` in `_CUSTOM_OP_OK` at the subprocess, and the
    # `FAIL (…)` line is rendered FROM that boolean rather than the other way
    # round. Where it stays None the op never reached a child — a timeout, an
    # OSError, a malformed `ops` entry — and the string it returned is then
    # the only statement in existence about what happened.
    if _custom_op_ok is not None:
        if not _custom_op_ok:
            _mark_op_failure()
    elif _op_body_failed(body):
        _mark_op_failure()

    # Fire read-op notifiers (mutating ops already fire inside _run_with_validators)
    try:
        _notify_read_op(op, parts)
    except Exception:
        pass  # observation must never break the call

    # Payload-parse notes lead the body: they are about bytes an op has already
    # written, so they have to be readable without scrolling past the receipt
    # that claims those bytes are fine. Depth-gated because a batch parses its
    # payload once, in this frame, before any sub-op runs.
    if _PAYLOAD_WARNINGS and getattr(_DISPATCH_STATE, "depth", 1) <= 1:
        body = _take_payload_warnings() + body

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
    # A preset op writes no file through `_atomic_write`, so the write counter
    # is silent for it and its own exit status is the only success signal
    # available. Same rule as above rather than a looser one: taken from a
    # status code, never from the prose of the receipt being summarised.
    if _compact_header and (_WRITE_COUNT[0] > _writes_before
                            or _custom_op_ok is True):
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
    #
    # `[result]` goes directly above it, under the same gate: the branch line
    # must stay the last line (#381's tests assert `endswith`), and a footer
    # that only appears when a branch happens to exist would go missing outside
    # a repo — which is the exact shape #621 is about.
    if getattr(_DISPATCH_STATE, "depth", 1) <= 1:
        # A read-only op that ran validators can still carry the one thing the
        # footer exists to carry: a checker that did not check (#969).
        # `validate` is not in `_OP_TARGETS` and mutates nothing, so it was
        # gated out of the summary line entirely.
        # This frame's own rows (#1109), not a slice of the process-global.
        # `dispatch` installed them before `_dispatch_impl` ran, so the lists
        # are always present here; `or ()` declines to fall back to the global,
        # because a footer built from every op's rows is the defect, not a
        # degraded reading of it.
        _not_checked_slice = list(
            getattr(_DISPATCH_STATE, "acc_not_checked", None) or ())
        _validated_slice = list(
            getattr(_DISPATCH_STATE, "acc_validated", None) or ())
        if (op in _OP_TARGETS or _MUTATION_ATTEMPTS[0] > _attempts_before
                or _not_checked_slice or _validated_slice):
            _result = _result_line(_MUTATION_ATTEMPTS[0] - _attempts_before,
                                   _WRITE_COUNT[0] - _writes_before,
                                   _SKIP_COUNT[0] - _skips_before,
                                   _REAPPLY_COUNT[0] - _reapplies_before,
                                   _not_checked_slice,
                                   _ROLLBACK_COUNT[0] - _rollbacks_before,
                                   _validated_slice)
            # A batch says its count twice, and the leading copy is the load-
            # bearing one. The footer is separated from the per-op results by a
            # validators block long enough that `tail` lands on `git-status :
            # ok` and reads as success -- the exact half of #984 that #1018
            # marked `Part of` and did not build. Only `batch`: a single op's
            # receipt is three lines with the footer already adjacent, and a
            # duplicate there is noise rather than a signal.
            if op == "batch":
                body = _result + body
            body += _result
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

# #475: creating a warm daemon is an interactive affordance, not a universal one.
# The daemon double-forks and lives for IDLE_TIMEOUT_SEC (600s) with no tie to the
# caller, so a caller that will be killed long before a cold LSP can answer buys
# nothing and leaves ~1.3 GB of intelephense index resident for ten minutes. The
# validator runner stamps SUPERTOOL_MCP_AUTOSPAWN=0 into its adapters' env; it is
# inherited by the grandchild `supertool diag:` and read here.
#
# Suppression removes *creation*, never *use* — a daemon that is already warm is
# still connected to, which is the whole point of running the validator.
_MCP_AUTOSPAWN_ENV = "SUPERTOOL_MCP_AUTOSPAWN"
_MCP_AUTOSPAWN_FALSEY = frozenset({"0", "false", "no", "off"})


def _mcp_autospawn_allowed() -> bool:
    """False when the caller declared it cannot wait for a cold daemon (#475)."""
    raw = os.environ.get(_MCP_AUTOSPAWN_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in _MCP_AUTOSPAWN_FALSEY

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


class _StopOutcome(NamedTuple):
    """What actually happened when we asked stop.py to kill a warm daemon.

    `ok` answers the only question the invalidation path cares about: is there
    still a daemon that might answer the next validator from a stale index?
    "No daemon was running" is `ok` — nothing stale can come from nothing.
    `code` and `detail` carry the why, for the debug line.
    """

    ok: bool
    code: str
    detail: str


# stop.py's exit codes. Anything else came from a crashing interpreter, not
# from stop.py's own reporting, and must not be guessed into a known bucket.
#
# `1` is the missing key and the point of the table (#574). It is what CPython
# exits with on an uncaught exception, so it is never stop.py reporting; it used
# to sit here as ("no-daemon", True), which made a traceback out of stop.py
# indistinguishable from its most reassuring answer and handed the invalidation
# path an `ok` for a check that never ran — #239 with the safety net claiming it
# held. It falls to the default below instead, and stop.py's EXIT_NO_DAEMON has
# moved to `5`. Nothing new may be assigned to `1`.
_MCP_STOP_CODES = {
    0: ("stopped", True),
    2: ("usage", False),
    3: ("failed", False),
    4: ("refused", False),
    5: ("no-daemon", True),
}

_MCP_STOP_DETAIL_CAP = 500


def _mcp_stop_report(name: str, outcome: _StopOutcome) -> _StopOutcome:
    """Log a failed invalidation once, on stderr, only under SUPERTOOL_DEBUG.

    Deliberately not in the op's output. Invalidation runs behind every `edit:`
    that creates a file; a line there would turn a background optimization into
    user-facing noise on the overwhelmingly common path where nothing is wrong,
    which is a worse trade than the silence this replaces. stderr keeps it out
    of the op body even when the gate is open. This is the same channel the
    tree-sitter fallbacks already use for "something degraded, carry on".

    A successful stop, and the no-daemon case, say nothing at all.
    """
    if not outcome.ok and os.environ.get("SUPERTOOL_DEBUG"):
        suffix = f" — {outcome.detail}" if outcome.detail else ""
        print(f"[supertool debug] mcp stop {name}: {outcome.code}{suffix}",
              file=sys.stderr)
    return outcome


def _mcp_stop_server(name: str) -> _StopOutcome:
    """Best-effort SIGTERM the warm daemon for `name` via stop.py.

    The next op that touches this server cold-starts a fresh daemon, so its LSP
    re-indexes the workspace. Used by the new-file auto-invalidation path (#239):
    a just-created class isn't in the warm reflection cache, so a stale daemon
    reports phantom errors.

    Still non-blocking on every failure — invalidation is an optimization and
    must never fail the op. What it no longer does is discard the *outcome*
    along with the *blocking*, which are separable (#547). Stopped, refused,
    crashed and binary-missing used to share one observable — nothing — so the
    path whose whole job is to prevent a stale daemon could not report that it
    had failed to prevent one. It returns what happened and logs a single
    debug-gated line when the stop did not succeed.

    stdout stays on DEVNULL: stop.py's human-facing chatter has no business in
    an op's output. stderr is captured, capped, and only ever surfaces behind
    the debug gate.
    """
    import subprocess
    try:
        proc = subprocess.run(
            [sys.executable, _MCP_STOP_SCRIPT, name],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired:
        return _mcp_stop_report(
            name, _StopOutcome(False, "timeout", "stop.py did not return within 30s"))
    except (OSError, subprocess.SubprocessError) as exc:
        return _mcp_stop_report(
            name, _StopOutcome(False, "unavailable", f"{type(exc).__name__}: {exc}"))
    code, ok = _MCP_STOP_CODES.get(proc.returncode, ("crashed", False))
    detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
    return _mcp_stop_report(name, _StopOutcome(ok, code, detail[-_MCP_STOP_DETAIL_CAP:]))


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
            if not hasattr(socket, "AF_UNIX"):
                # Same knowledge as spawn() below, one step earlier (#544).
                # Resolving the socket path goes through _paths.runtime_dir(),
                # which cannot verify ownership without os.geteuid and refuses
                # rather than defaulting — so without this the constructor
                # raised before reaching the sentence that explains the
                # platform. _mcp_ensure_server catches MCPServerError and falls
                # back to the non-MCP heuristic path; it catches neither
                # AttributeError nor SystemExit.
                raise MCPServerError(
                    "MCP daemon requires socket.AF_UNIX — not available on this platform"
                )
            cwd = os.path.abspath(os.getcwd())
            # A stated runtime-dir refusal reaches this caller as a recoverable
            # error, not as a dead process (#568).
            #
            # `_paths.runtime_dir()` refuses with `sys.exit("<reason>")` — for a
            # dir owned by another uid, one it cannot create, and now one that
            # is not owner-only and cannot be made so. `SystemExit` derives from
            # `BaseException`, so `_mcp_ensure_server`'s
            # `except (OSError, MCPServerError, MCPTimeout, KeyError)` does not
            # catch it and neither would a bare `except Exception`. That handler
            # returning `None` is the whole mechanism by which `refs`, `resolve`
            # and `workspace` fall back to their heuristic path, so an escaping
            # `SystemExit` does not degrade the op — it kills the invocation.
            #
            # The AF_UNIX hoist above is the same lesson at the same boundary
            # (#544); it stays, because not calling `runtime_dir()` at all beats
            # translating what it raises. This covers the refusals whose cause
            # cannot be known one step earlier: you have to look at the
            # directory to learn its mode. The mode case is the one that makes
            # this urgent rather than tidy — a foreign-uid runtime dir is rare,
            # while an exFAT/FAT32/SMB `SUPERTOOL_RUNTIME_DIR`, where a chmod is
            # expected to be a no-op, is an ordinary setup.
            #
            # Degrading is also the safer answer here, not merely the friendlier
            # one: the cold path binds no socket and writes no pidfile, so there
            # is nothing left for the directory mode to protect. `stop.py` and
            # `status.py` keep the refusal as a refusal, because reporting on the
            # runtime dir is their job (`EXIT_REFUSED`); for a warm-daemon op it
            # is an optimization, and `docs/mcp-integration.md` already states
            # the rule for the sibling case — an optimization never blocks the op.
            #
            # A bare numeric exit is left alone, on `stop.py::_refused`'s rule:
            # it carries no reason, so it is not a refusal anyone worded, and
            # relabelling it would invent a recoverable failure from an exit
            # nobody explained.
            try:
                self._sock_path, _ = _mcp_socket_pid_paths(cwd, name)
            except SystemExit as exc:
                if exc.code is None or isinstance(exc.code, int):
                    raise
                raise MCPServerError(str(exc.code)) from exc
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
            budget = _env_float("SUPERTOOL_MCP_CONNECT_TIMEOUT",
                                float(self._CONNECT_TIMEOUT_SECONDS), minimum=0.0)
            # Explicit socket_path (tests, externally managed daemons) → no one
            # else will spawn it. Single-shot connect, fail fast on miss.
            # Polling the same dead path burns the full 60s budget for nothing.
            #
            # #475 takes the same exit: when auto-spawn is suppressed by
            # provenance, nobody is going to bind this path either, so polling
            # it is the same wasted budget — and the caller (a validator with a
            # seconds-long timeout) has less of it to waste.
            if not self._auto_spawn or not _mcp_autospawn_allowed():
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
                    ) from e
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
                raise MCPTimeout(
                    f"MCP daemon '{self.name}' read timed out after {self.timeout}s") from None
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
    """Entry point. Scopes the @payload root state to this one call.

    `_CWD_SHIFT` outliving main() would let a `cwd:` call poison a later bare
    dispatch() — the MCP server and the test suite both drive dispatch()
    directly in a process where main() has already run, and a stale root there
    resolves payloads against a directory nobody is standing in (#672).
    """
    global _INVOCATION_DIR, _CWD_SHIFT
    try:
        return _main(argv)
    finally:
        _INVOCATION_DIR = None
        _CWD_SHIFT = None


def _main(argv: List[str]) -> int:
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

    # #714 — the process launcher, scrubbed before ANY op dispatches.
    #
    # #692 chose `_resolve_custom_op` and argued for one chokepoint. The
    # argument was right and the level was one too low: that function launches
    # preset ops, and built-ins never pass through it. Core spawns git itself
    # in six places — `_run_git_ignore_query` (the ignore pruning behind glob,
    # grep, tree, map), `_path_meta_suffix` (the ` m`/` ?`/` !` marker on every
    # read), `_branch_probe`, `op_workspace`'s Git section, `op_validate_staged`
    # and `op_format_staged` — none of which passes `env=`, so each inherited
    # whatever `GIT_*` the parent had. Under a leaked GIT_DIR a tracked,
    # modified file read ` ?`, `workspace` reported the other repo's branch,
    # and `validate_staged` — the op `.githooks/pre-commit` exists to run —
    # answered "no staged files" with a file staged.
    #
    # Guarding each spawn instead would be #704's disease: twelve call sites
    # today, and spawn thirteen written without the guard. Scrubbing
    # `os.environ` itself covers all of them at once, including presets, whose
    # `dict(os.environ)` copy is now clean before it is taken — one boundary,
    # moved up, not a second one added.
    #
    # Before the argv checks so a usage error or an early return still leaves
    # the process clean; the notice waits until after any chdir, because it
    # names the cwd the ops actually ran in.
    _LEAKED_GIT_ENV[:] = scrub_git_env(os.environ)

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

    # repo:OWNER/NAME — name the repo this call's repo-scoped ops are about,
    # instead of deriving it from the cwd's git remote (#673). Consumed in the
    # pre-pass like cwd: and --plain: exported as SUPERTOOL_REPO, which every
    # preset subprocess inherits, then stripped before dispatch.
    #
    # A leading op rather than a trailing `…:repo=OWNER/NAME` token, because the
    # suffix grammar in this family is not free: `gh-job:ID:grep:PATTERN` takes
    # an arbitrary regex in that position, so a trailing scan would silently
    # steal a legitimate `grep:repo=x` log search, and `gh-prs` already spells
    # its filters `key=value` inside one comma-separated token — a second,
    # colon-separated `key=` grammar in the same family would be two rules for
    # one idea. Position mirrors cwd:: first, or immediately after it, so the
    # two read in the order they apply ("stand here, ask about that").
    repo_positions = [i for i, a in enumerate(argv)
                      if a.split(":", 1)[0] == "repo"]
    if repo_positions:
        if len(repo_positions) > 1:
            sys.stderr.write("repo: only one repo: op is allowed per call\n")
            return 1
        first_allowed = 1 if argv[0].split(":", 1)[0] == "cwd" else 0
        if repo_positions != [first_allowed]:
            sys.stderr.write(
                "repo: must be the first op, or immediately after cwd: "
                "(repo:OWNER/NAME op1 op2 ...)\n")
            return 1
        spec = argv[first_allowed]
        repo_target = spec.split(":", 1)[1].strip() if ":" in spec else ""
        if repo_target.count("/") != 1 or not all(repo_target.split("/")):
            sys.stderr.write(
                f"repo: expected OWNER/NAME, got {repo_target!r} "
                "(e.g. repo:Digital-Process-Tools/claude-remember)\n")
            return 1
        rest = argv[:first_allowed] + argv[first_allowed + 1:]
        targetable = _repo_target_ops()
        # A leading cwd: survives in `rest` and is another pre-pass op, not a
        # dispatch one — it is where the call stands, never what it is about,
        # so it is exempt from the targetable check rather than refused by it.
        blocked = [a.split(":", 1)[0] for a in rest
                   if a.split(":", 1)[0] not in targetable
                   and a.split(":", 1)[0] != "cwd"]
        if blocked:
            sys.stderr.write(_repo_refusal(blocked[0]))
            return 1
        os.environ["SUPERTOOL_REPO"] = repo_target
        argv = rest
        if not argv:
            return 0

    # cwd:PATH — must be the FIRST op. chdir once before any dispatch so every
    # remaining op resolves against PATH (mirrors `cd PATH && …`), then strip
    # it. Handled here in the pre-pass (like --plain) — never reaches dispatch,
    # so it can't race the parallel read path or force a batch sequential.
    # Required-first keeps the rule unambiguous: appearing later is an error,
    # not a silently-honored mid-call cwd switch.
    global _INVOCATION_DIR, _CWD_SHIFT
    _INVOCATION_DIR = os.getcwd()
    _CWD_SHIFT = None

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
        _CWD_SHIFT = "cwd:"
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
            _CWD_SHIFT = "auto-resolved project root"
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

    # #678 — config ops decline outright, but a built-in-only call under a mix
    # runs to completion using the other tree's validators, formatters and
    # hooks. Say so once, on stderr, so the operator knows what answered.
    _mixed_call = _mixed_tree_pair()
    if _mixed_call is not None:
        sys.stderr.write(f"supertool: {_mixed_tree_note(_mixed_call)}\n")

    # Normal batched-ops mode
    total_out_bytes = 0
    any_failure = False

    # The scrub happened before argv was even parsed; the cwd it acted under is
    # only settled here, after `cwd:`/auto-root. Printed ahead of the op bodies
    # so a caller reading top-down learns their environment leaked before they
    # read an answer that would otherwise look ordinary (#692, #714).
    _leak_notice = _git_env_notice(_LEAKED_GIT_ENV)
    if _leak_notice:
        sys.stdout.write(_leak_notice)
        total_out_bytes += len(_leak_notice.encode("utf-8"))
    # Per-call delta, not the absolute count: the counter is process-global and
    # the daemon reuses the process, so reading `_SKIP_COUNT[0] > 0` would let
    # one declined op in an early call poison the exit code of every later call
    # in the same worker (#680).
    _skips_at_entry = _SKIP_COUNT[0]
    _rollbacks_at_entry = _ROLLBACK_COUNT[0]
    _not_checked_at_entry = len(_NOT_CHECKED)
    _validated_at_entry = len(_VALIDATED_FILES)

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
    # Parallel path is read-only — no formatters fire there anyway. That was
    # false while `format_staged` sat in the safe set, which is one of the two
    # things #1244 fixed; it is a claim about the set, so it stays true only as
    # long as the set does.
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
                answers = list(ex.map(dispatch_verdict, argv))
        else:
            answers = [dispatch_verdict(a) for a in argv]
    finally:
        if defer:
            _DEFER_FORMATTERS = False

    bodies = [_b for _b, _ in answers]
    refused = 0
    # `any_failure` has more than one source, and the tally below must not
    # attribute all of it to the ops it counted. See the three counter checks
    # further down.
    counter_failure = False
    for body, op_failed in answers:
        sys.stdout.write(body)
        total_out_bytes += len(body.encode("utf-8"))
        if op_failed:
            any_failure = True
            refused += 1

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

    # A declined op is a failure even when its receipt never said ERROR (#680).
    # The verdict above is the op's own return token, which catches `edit`'s
    # no-match but not `replace`'s "(0 occurrences of 'x' found)" — so
    # `batch: && git commit` committed a half-applied set and exited 0. The
    # counter is the authority here precisely because it does not read prose.
    if _SKIP_COUNT[0] > _skips_at_entry:
        any_failure = True
        counter_failure = True

    # A reverted write is the same hazard one step later (#952): the op wrote,
    # a validator rejected it, the file was restored — and `batch:@ops &&
    # git commit` committed the set without it and exited 0. Same per-call
    # delta as above, for the same reason: the warm daemon reuses the process,
    # so an absolute read would let one rolled-back edit poison the exit code
    # of every later call in the same worker.
    if _ROLLBACK_COUNT[0] > _rollbacks_at_entry:
        any_failure = True
        counter_failure = True

    # A validator the operator required, that could not run, is a failure of
    # the same kind (#665): the op wrote and the gate did not run. Setting
    # $SUPERTOOL_REQUIRE_VALIDATORS is the operator stating that these checkers
    # must be present *here*, so a missing one is a configuration fault to fix,
    # not a local inconvenience to absorb — and unset, nothing reaches this at
    # all, because an absent tool is then the honest `skipped` it always was.
    # Deliberately an exit code and not a refusal: the edit still lands and is
    # still rolled back on exactly the conditions it was before. Turning "we
    # could not check" into "we will not work" trades the quiet bug for a
    # louder one rather than fixing it.
    if len(_NOT_CHECKED) > _not_checked_at_entry:
        any_failure = True
        counter_failure = True
    del _NOT_CHECKED[_not_checked_at_entry:]
    # Informational only — the count line discloses a skip, it does not gate on
    # one (#990). Truncated for the same warm-daemon reason as the list above.
    del _VALIDATED_FILES[_validated_at_entry:]

    # The exit code is one bit for a call that ran N ops, and from the caller's
    # seat that bit is indistinguishable from "the batch did not run" (#1234).
    # It was filed as a refusal *suppressing* its siblings; reproduced at
    # 0.32.0 it does not — every op runs and every op renders — but a shell
    # `&&`, a pre-commit hook, or an agent harness that reframes any non-zero
    # command as an error block has nothing in the output to tell it otherwise.
    # So this discloses the mismatch rather than changing the behaviour: a
    # batch was never all-or-nothing and must not start being one.
    #
    # Only on a multi-op call, and only when the exit code is about to be 1 —
    # with one op there is no sibling to have lost, and on a clean batch the
    # line is noise on every call forever.
    _other = ("a skipped write, a rolled-back edit or a validator that could "
              "not run")
    if len(bodies) > 1 and any_failure:
        if counter_failure and refused:
            # Both kinds of failure at once, and this is the branch that has to
            # say the least. `refused` counts ops that returned a refusal;
            # the counters catch the failures that render as ordinary prose —
            # `replace`'s `(0 occurrences ...)`, an edit a validator reverted.
            # An op can therefore be outside `refused` and still not have
            # landed, so "the other N answers are complete" would be a positive
            # claim about output this line has not checked. It is withheld.
            tally = (
                f"[batch] {len(bodies)} ops ran — {refused} refused, and "
                f"{_other} also failed this call (above). More than one thing "
                f"went wrong: read the per-op receipts, not these counts."
                + chr(10)
            )
        elif refused == len(bodies):
            # No sibling survived, so there is no "the rest is fine" to make.
            # Still worth the count: it says every op was reached and answered,
            # which is the thing the exit code alone does not settle.
            tally = (
                f"[batch] {len(bodies)} ops ran — all {len(bodies)} refused."
                + chr(10)
            )
        elif refused:
            tally = (
                f"[batch] {len(bodies)} ops ran — {len(bodies) - refused} ok, "
                f"{refused} refused. Exit 1 flags the refusal; the other "
                f"{len(bodies) - refused} answers above are complete." + chr(10)
            )
        else:
            # None of which is an op refusing. Saying "0 refused" beside exit 1
            # would send the reader hunting for an op that did not fail.
            tally = (
                f"[batch] {len(bodies)} ops ran — all {len(bodies)} rendered an "
                f"answer. Exit 1 is {_other} (above), not an op." + chr(10)
            )
        sys.stdout.write(tally)
        total_out_bytes += len(tally.encode("utf-8"))

    log_call(argv, total_out_bytes)
    return 1 if any_failure else 0


# The op's own verdict token, matched at POSITION 0 of the string an op
# returned. Never against a rendered receipt.
#
# The exit code used to be re-derived from `header + body` by locating where
# the header ended — and the header is `--- {arg} ---`, holding whatever the
# caller typed. That made the verdict a function of the argument, and it was
# wrong in both directions (#1291):
#
#  - a `re.search` for a `---` line followed by `FAIL`/`ERROR: ` over the
#    whole body fired on an argument that spanned lines with an error-shaped
#    continuation, so a `grep` that found nothing exited 1 — and #1284's tally
#    then said "1 refused" in words, an explicit false sentence off a false
#    bit;
#  - taking the header's close to be the FIRST `" ---" + newline` put that
#    close inside the argument whenever the argument held such a line, so the
#    verdict was read off the wrong line and a refusal exited 0. Reachable
#    from this repo's own commit convention — a message quoting an op receipt
#    — which is #1279 restored on exactly the messages the convention asks
#    for.
#
# The docstring that shipped with the previous version claimed a trade: a
# narrow false negative in the argument, against a wide false positive in the
# output. It had taken both, and the false negative was not narrow.
#
# There is nothing left to search for. `_dispatch_impl` holds the op's return
# value before it prepends a header to it, so the boundary is a fact rather
# than a guess. The match being anchored also disposes of the quadratic rescan
# a lazy search cost on a diff-shaped body — 4.0s on 255KB of `--- a/path`
# hunk headers against 0.0013s (#1279) — because this reads at most the first
# few characters however large the receipt grows.
_OP_VERDICT_FAIL = re.compile(r"(FAIL\b|ERROR:\s)")


def _op_body_failed(body: str) -> bool:
    """Did the op that returned *body* refuse?

    The op-return convention, and the only channel a builtin has: every op
    returns `str`, and a refusal is a string that starts `ERROR: ` or `FAIL`.
    So this reads the first token of what the op returned — not the receipt
    later built around it, and not any line further in, which is the caller's
    own content.
    """
    return _OP_VERDICT_FAIL.match(body) is not None


def _receipt(header: str, body: str) -> str:
    """Assemble a receipt, taking the call's verdict on the way.

    `_dispatch_impl` returns early at eighteen refusal and redirect gates
    before it reaches the main verdict point, and each one had its refusal
    read back out of the rendered string it had just built. Passing the two
    halves separately is what makes the boundary a fact rather than a search
    — which is the whole of #1291.
    """
    if _op_body_failed(body):
        _mark_op_failure()
    return header + body


def _mark_op_failure() -> None:
    """Record, at the frame that knows it, that this call refused.

    Thread-local rather than a process-global counter, unlike `_SKIP_COUNT`
    and `_ROLLBACK_COUNT` beside it: those are per-call deltas collapsed into
    one bit, and this one has to stay attributable to a single top-level op so
    #1284's tally can say how many of N refused. Batch sub-ops recurse through
    `dispatch` on the calling thread, and parallel dispatch runs each
    top-level op start to finish on one worker, so a frame at any depth may
    set it and no frame above 0 clears it.

    Two sites clear it, and the pair is the invariant: `dispatch` at depth 0,
    and `dispatch_verdict` immediately before the call it is about to judge.
    The second is not redundant with the first. `dispatch` is a module global
    the tests and the MCP layer replace, and when they do the depth-0 clear is
    not in the process at all — so the bit read back was the last refusal on
    the thread, whenever that happened (#1359). The frame that reads the flag
    establishes it.
    """
    _DISPATCH_STATE.call_failed = True


def _call_failed() -> bool:
    """The flag `_mark_op_failure` sets, for the frame that owns this call."""
    return bool(getattr(_DISPATCH_STATE, "call_failed", False))


def _cli() -> int:
    return main(sys.argv[1:])


# `supertool.py` is the entry point and this module is meant to be *imported*:
# an imported module is compiled to `__pycache__` once, a script named on the
# command line is recompiled every run, and that difference is the whole point
# of #931. Running this file directly still works, and deliberately so — it
# re-pays the ~145ms, but the alternative is worse. Anything that spawns
# `[sys.executable, supertool.__file__, "op"]` lands here, and without this
# block such a call would print nothing and exit 0: a silent success that
# executed no op at all. Paying the tax is a cost; answering "fine" without
# having run is a lie.
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
