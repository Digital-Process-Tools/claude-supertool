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
                                file < 20KB with a match. PATTERN is never
                                unquoted: a surrounding 'x' or "x" pair is
                                searched as literal characters, which is what
                                a caller hunting a quoted phrase means. A ZERO
                                from a quote-wrapped pattern therefore names
                                the quotes and reports whether the unwrapped
                                spelling matches — same note on grep_around,
                                around and read:PATH:::grep=.
    grep:PATTERN:PATH:no-auto-read
                               Suppress the single-file auto-read — only the
                                matching line(s) are emitted (parity with glob).
    grep:PATTERN:PATH:...:full Suppress the 500-char per-line cap for this call
                                only (#1712) — order-independent with count/
                                no-auto-read; the default stays capped.
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
                                not search. Same pattern gate as grep: BRE
                                alternation is rewritten (and disclosed), and a
                                pattern that matches every line is refused.
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
import bisect  # noqa: F401 -- used only by _supertool_edit.py (#2706), which
                # shares this module's globals() rather than importing it
import json
import difflib
import hashlib  # noqa: F401 -- used by _supertool_edit.py and _supertool_read.py (#2706), sharing this module's globals() rather than importing it
import importlib.machinery
import os
import stat  # noqa: F401 -- used by _supertool_config.py and _supertool_edit.py (#2706), sharing this module's globals() rather than importing it
import re
import shlex
import shutil
import signal  # noqa: F401 -- only use left in this file is inside _supertool_mcp.py's part (#2706)
import socket  # noqa: F401 -- only use left in this file is inside _supertool_mcp.py's part (#2706)
import subprocess
import sys
import tempfile
import threading  # noqa: F401 -- only use left in this file is inside _supertool_mcp.py's part (#2706)
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, MutableMapping, NamedTuple, Optional, Sequence, Tuple  # noqa: F401 -- MutableMapping has no direct use in this file after #2706 moved its two call sites into _supertool_presets.py, which execs into this module's own globals() and still needs the name bound here; Sequence used only by _supertool_edit.py (#2706), sharing this module's own globals()

VERSION = "0.64.0"

# ---------------------------------------------------------------------------
# Part loader (#2706)
#
# `_supertool.py` delegates contiguous chunks of itself to `_supertool_<x>.py`
# *part* files. Each part is loaded with `_load_part("_supertool_<x>")` at the
# exact source position its code used to occupy, so import-time order
# (including `atexit` LIFO registration) is unchanged by the split.
#
# A real `import _supertool_guard` would give that code its own module
# namespace, breaking every `monkeypatch.setattr(supertool, "<name>", ...)`
# that reaches it today -- see the #2706 "Decision" comment: 1,301 patch
# sites over 98 names, and 30 globals rebound with `global X` across what
# would become module boundaries, neither of which any test is built to
# catch across a real module split. Instead, `exec(code, globals())` runs the
# part's code with THIS module's own globals() -- the same dict every
# function already defined here shares -- so every function the part defines
# ends up with `__globals__ is _supertool.__dict__`, exactly as if it had
# been typed at this position in this file.
#
# `importlib.machinery.SourceFileLoader(name, path).get_code(name)` rather
# than a bare `exec(compile(open(path).read(), path, "exec"))`: the loader
# form reads and writes `__pycache__` the same way a real `import` does, so
# the #931 bytecode cache still applies to a part -- a bare `compile()` would
# recompile the part's AST on every single invocation, paid by every call.
#
# Loads only from beside `_supertool.py`'s own resolved path: no
# `sys.meta_path` finder can substitute a part from elsewhere (#678). A
# missing part file fails loudly, here, with the #2706 message -- never with
# a later `NameError` on the first name the missing code was supposed to
# define.
# ---------------------------------------------------------------------------
def _load_part(name: str) -> None:
    """Load `<name>.py`, found beside this file, into this module's globals().

    `name` is the part's module name without the `.py` suffix, e.g.
    `"_supertool_guard"`. Not a general-purpose loader: it always targets
    `globals()` of the caller's enclosing module (this one), and always
    resolves the path relative to `_supertool.py`'s own file, never the
    current working directory or `sys.path`.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    if not os.path.isfile(path):
        raise ImportError(
            f"incomplete install: {name}.py missing beside {__file__} (#2706)"
        )
    code = importlib.machinery.SourceFileLoader(name, path).get_code(name)
    exec(code, globals())

# Children never see an operator's ambient `FORCE_COLOR` (#1429). CPython
# 3.13+ colourises its own tracebacks purely because the variable is set --
# even when stderr is a pipe -- so any child this process spawns hands back
# ANSI escapes baked into whatever receipt field captures its output
# (`_mcp_stop_server`'s `detail`, a validator's stdout, a declared preset's
# captured run). Stripped here, once, before anything can spawn a child,
# rather than scrubbed per call site: every `subprocess.run`/`Popen` in this
# file that does not pass its own `env=` inherits the live `os.environ`, and
# every one that DOES build an explicit env does so by copying/merging
# `os.environ` first (`{**os.environ, ...}` or `os.environ.copy()`) -- so one
# mutation, this early, reaches every call site in THIS process, plus every
# standalone validator/formatter subprocess this tool launches (they receive
# their environment as that same merge). `presets/mcp/daemon.py` repeats this
# same pop, rather than relying on inheriting it, because its own docstring
# documents it as directly runnable (`python3 daemon.py SERVER_NAME`) outside
# this process entirely -- a mutation here cannot reach a process that never
# imported this module.
#
# Only `FORCE_COLOR` is removed, never `NO_COLOR` added: CPython's own
# `_colorize.can_colorize()` checks `NO_COLOR` BEFORE `FORCE_COLOR` (self-
# review finding, #1429), so forcing `NO_COLOR=1` here would have silently
# out-ranked a declared preset's own `.supertool.json` `env: {FORCE_COLOR:
# ...}` -- the escape hatch this comment used to claim still worked, and
# didn't. Removing only the ambient value leaves that override live: a
# preset's own `env:` block is applied AFTER this merge and still wins for an
# operator who deliberately wants colour on one declared command.
def _disable_force_color_for_children() -> None:
    os.environ.pop("FORCE_COLOR", None)


_disable_force_color_for_children()

#: The one "is this string a number" test the core shares (#1748), anchored with
#: a capital-Z escape rather than `$` because Python's `$` also matches before a
#: final newline (#1188).
#:
#: `str.isdigit()` is not that test, and it fails in two directions:
#:
#: * U+0662 ARABIC-INDIC DIGIT TWO and its family are `isdecimal()` too, so
#:   `int()` converted them and the op ran against a number nobody typed --
#:   `around:PAT:F:<U+0662>` gave a 2-line window and `vim:F:<U+0662>jx` edited
#:   the file and printed `2j` back in the receipt.
#: * U+00B2 SUPERSCRIPT TWO and its family are not `isdecimal()`, so `int()`
#:   raised and dispatch's catch-all rendered `invalid literal for int()` -- a
#:   refusal that names an interpreter builtin instead of the argument slot.
#:
#: `presets/_digits.py` holds the identical pair for the presets and is
#: deliberately NOT imported here: presets run as standalone subprocesses and
#: put only `presets/` on their own path, so sharing one module would mean
#: either the core importing out of the tree it dispatches into, or every preset
#: paying the core's compile cost that #931 exists to avoid. Two four-line
#: predicates, one recurrence test over each file, was the cheaper duplication.
_ASCII_DIGITS = re.compile(r"^[0-9]+\Z")


def _is_ascii_int(text: str) -> bool:
    """True when `text` is one or more ASCII digits and nothing else.

    No stripping, no sign, no separators -- a caller that wants to allow leading
    whitespace strips it itself, so the allowance is visible at the call site.
    """
    return bool(_ASCII_DIGITS.match(text))


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
FOOTER_ECHO_MIN_LINES = 60  # read:'s OFFSET/LIMIT window note is echoed at the foot
# only above this many printed lines (#1777) — the same "a screen's worth of content"
# figure as MAX_AUTOREAD_LINES above, reused rather than duplicated with a different
# number. Below it, the established "one disclosure, above the body" contract from
# #1489/#382 stays exactly as those tests pin it (48 printed lines in both).
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


_load_part("_supertool_config")

_load_part("_supertool_parse")


_load_part("_supertool_presets")


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

_load_part("_supertool_read")
_load_part("_supertool_grep")




# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


_load_part("_supertool_edit")














# Diagnostic from the last _vim_load_undo_snapshot call that failed with an
# OS-level error (e.g. permission denied).  Surfaced on the next `u` receipt.
_last_undo_diagnostic: "Optional[str]" = None













_LINT_TIMEOUT_DEFAULT = 5

#: The one decline that is an environment limit rather than a finding: the
#: checker was there, it started, and the budget ran out. Named so the suite can
#: key on the product's own declaration instead of a copied literal (#1360).
_LINT_TIMEOUT_PREFIX = "--- POST-EDIT LINT TIMED OUT"

_LINT_DECLINE_PREFIXES = (
    _LINT_TIMEOUT_PREFIX,
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


#: `_supertool.py`'s own copy of `which_excluding_cwd` (#2596). The core
#: cannot reach into `validators/common` (`_VALIDATOR_RESOLVE_ERROR_PREFIX`'s
#: own comment, same trade), so this is a third stated copy of the same
#: algorithm -- `validators/common/spawnable.py`, `presets/_spawnable.py`,
#: and here -- pinned equal to the other two by
#: `tests/test_bare_spawn_cwd_gate_2596.py` rather than trusted to stay in
#: sync by hand. See `validators/common/spawnable.py::which_excluding_cwd`
#: for the full rationale (#2575): `shutil.which()` inserts the current
#: directory ahead of every real PATH entry on Windows, even with an
#: explicit `path=`, so a repository shipping `php.exe`/`xmllint.exe` at its
#: own root would have that file resolved -- and spawned, since the lint
#: subprocess below passes no `cwd=` -- ahead of the real tool.
def _which_excluding_cwd(name: str) -> Optional[str]:
    if os.path.dirname(name):
        return shutil.which(name)
    path_env = os.environ.get("PATH")
    if path_env is None:
        # Mirror shutil.which(): PATH unset (not merely empty, see below)
        # falls back to the platform default search path rather than
        # reporting every tool absent (#2603).
        try:
            path_env = os.confstr("CS_PATH")
        except (AttributeError, ValueError):
            path_env = os.defpath
    if not path_env:
        return None
    here = os.path.normcase(os.path.abspath(os.curdir))
    exts = [""]
    if os.name == "nt":
        raw_pathext = os.getenv("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
        exts = [""] + [e for e in raw_pathext.split(os.pathsep) if e]
    seen = set()
    for entry in path_env.split(os.pathsep):
        if not entry:
            continue
        entry_abs = os.path.abspath(entry)
        norm = os.path.normcase(entry_abs)
        if norm in seen:
            continue
        seen.add(norm)
        if norm == here:
            continue
        for ext in exts:
            candidate = os.path.join(entry, name + ext)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None










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
        while mc < sn and _is_ascii_int(s[mc]):
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
    """Thin dispatch stub (#2706) -- the real implementation lives in
    _supertool_vim, imported lazily here so it is only loaded on the
    first 'vim' call rather than paid by every supertool invocation.
    """
    import _supertool_vim

    return _supertool_vim.op_vim(path, script)


def _r_missing_file_diagnostic(path_arg: str) -> str:
    """Extra context for a `:r` read that failed because the file was not
    there (#1763). A single CI leg went red once with ENOENT on a path the
    test itself had just written and closed three statements earlier, and
    was never reproduced. Bare ENOENT does not say whether the path never
    resolved at all or whether something was there and is now gone --
    the shape a reap-after-close would leave. This adds the one fact that
    tells them apart: does the parent directory exist.

    Deliberately not a stat dump: most `:r` failures are an ordinary
    typo'd path, and those callers do not need more than this one clause.
    """
    parent = os.path.dirname(os.path.abspath(path_arg)) or os.sep
    if os.path.isdir(parent):
        return " (parent directory exists; the file itself does not)"
    return " (parent directory does not exist)"




# Shipped fallback text for the two onboarding ops below, reached only when
# .supertool.json sets neither key (#2342). Every neighbouring onboarding
# surface already falls back to shipped content -- `help:OP`/`ops` fall back
# to the built-in op reference, `git-commit`'s coauthor trailer falls back to
# `_DEFAULT_COAUTHOR` (presets/git/commit.py) -- and these two were the only
# ones that printed a permanent "No ... configured" non-answer instead.
#
# Same convention as `_DEFAULT_COAUTHOR`: env var over .supertool.json over
# this built-in default, and any of `_ONBOARDING_DISABLE_VALUES` at whichever
# layer wins renders as the old "not configured" line -- so a project that
# deliberately wants its session preamble bare still can.
_ONBOARDING_DISABLE_VALUES = {"", "none", "off", "false", "no", "0"}

# Mechanical, universal facts only -- true of every install, not of any one
# project's ops. Deliberately carries NO batching exhortation ("pack 6-7 ops
# per call"): a downstream A/B test on the nearest equivalent (claude-oss
# scripts/batch_hint.py, #490) found that prose counterproductive when it is
# resent every turn rather than read once -- the treatment arm came out 6%
# MORE expensive in single-op rate, not less. A project that wants the
# exhortation sets its own `introduction`, as this repo's own .supertool.json
# does.
_DEFAULT_INTRODUCTION = (
    "supertool batches operations into one Bash call:\n"
    "    ./supertool 'op1:args' 'op2:args' 'op3:args'\n"
    "\n"
    "Mechanical facts, true of every install:\n"
    "- Paths resolve from the project root, not the shell's cwd.\n"
    "- One op per single-quoted argument -- quote each op separately.\n"
    "- Mutating ops (edit, replace, replace_lines, append, paste, vim; also\n"
    "  read/grep/around/between/validate) take an @-payload route instead of\n"
    "  inline args: 'edit:@-' with the payload on stdin, or 'edit:@path.toml'\n"
    "  for one already on disk. Only one '@-' per call -- for several\n"
    "  mutations in one call use 'batch:@-' with an [[ops]] array.\n"
    "- Payload format auto-detects: starts with '{' or '[' -> JSON, else\n"
    "  TOML. A triple-single-quoted TOML string is literal -- backslashes,\n"
    "  quotes and newlines survive byte-for-byte, which is what a code\n"
    "  block needs. A triple-double-quoted one processes escapes instead.\n"
    "- Field names are the syntax tokens lowercased:\n"
    "  edit:::OLD:::NEW:::PATH -> old, new, path.\n"
    "\n"
    "Run 'ops' for what this project's supertool can do.\n"
)

# Measured against a real batched call (a `read` on a small file, a `read`
# on a path that does not exist, `version`) rather than written from memory
# -- #2342's acceptance criteria requires this, and
# tests/test_output_format.py asserts the shapes named below against a live
# call so a future rendering change cannot leave this default stale in
# silence.
_DEFAULT_OUTPUT_FORMAT = (
    "Each op prints its own header, a meta line carrying the verdict, then\n"
    "its body. A batch runs every op named on the command line and reports\n"
    "each one under its own header, in call order.\n"
    "\n"
    "Measured from a real batched call, one deliberate miss included:\n"
    "\n"
    "$ supertool 'read:README.md:1:3' 'read:no-such-file.md' 'version'\n"
    "--- read:README.md:1:3 ---\n"
    "(184 lines, 18973 bytes)\n"
    "window: offset 1 + limit 3 ... nothing was cut ...\n"
    "     2→\n"
    "     3→...\n"
    "... (180 more lines -- lines 2-4 are the whole window asked for,\n"
    "nothing was cut)\n"
    "--- read:no-such-file.md ---\n"
    "ERROR: file not found: no-such-file.md\n"
    "supertool 0.59.0\n"
    "[batch] 3 ops ran -- 2 ok, 1 refused. Exit 1 flags the refusal; the\n"
    "other 2 answers above are complete.\n"
    "EXIT=1\n"
    "\n"
    "Six things this teaches that a happy-path result does not:\n"
    "1. '--- op:args ---' echoes the op verbatim, one per op, in call\n"
    "   order -- the only segment boundary. Piping a call through\n"
    "   head/tail/sed/cut selects against the answer: the header and the\n"
    "   batch footer are exactly what those cut.\n"
    "2. A meta line sits directly under the header and carries the\n"
    "   verdict ('(184 lines, 18973 bytes)', 'PASS (1.33s)', '(2 results,\n"
    "   limit 5)').\n"
    "3. Truncation is announced -- a short body is not a short file. Reads\n"
    "   cap at 300 lines / 20KB and say which window they returned.\n"
    "4. A failing op reports under its own header; the other ops still\n"
    "   ran.\n"
    "5. The exit code is about the batch, not the op: EXIT=1 above does\n"
    "   NOT mean no answers came back. Discarding a batch on a non-zero\n"
    "   exit throws away work already paid for. Exit 0 means no op\n"
    "   refused.\n"
    "6. Many ops append a '↳ to modify:' affordance line that is not\n"
    "   part of the payload -- it names the next call, not an answer.\n"
    "\n"
    "A project's own ops can print bodies this default cannot show (a\n"
    "custom 'qa' composite, a project-specific check) -- set\n"
    "'output-format' in .supertool.json to demonstrate those; this default\n"
    "only covers the envelope every op shares.\n"
)


_load_part("_supertool_catalog")


def op_doctor(arg: str = "") -> str:
    """Report the environment supertool runs in (#1857/#1950) -- body in
    _supertool_doctor.py (#2706), imported lazily so dispatch and the
    core module's own load order are untouched.
    """
    from _supertool_doctor import op_doctor as _op_doctor_impl
    return _op_doctor_impl(arg)


def op_init(mode: str = "") -> str:
    """Derive and write a starter .supertool.json (#858) -- body in
    _supertool_doctor.py (#2706), imported lazily so dispatch and the
    core module's own load order are untouched.
    """
    from _supertool_doctor import op_init as _op_init_impl
    return _op_init_impl(mode)


_load_part("_supertool_guard")


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
    # json-set never reaches the flat @file field-mapping route (#1822): its
    # payload's `set` field is a table, not a scalar, so dispatch builds
    # `parts = ["json-set", path]` itself after loading the payload -- see
    # the "json-set" branch below. This extractor only has to agree with that
    # shape.
    "json-set":      lambda parts: parts[1] if len(parts) > 1 else "",
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
                    'col': getattr(e, 'offset', None), 'kind': 'syntax',
                    'msg': (getattr(e, 'msg', None) or str(e))[:300]})
    except (OSError, ValueError) as e:
        out.append({'file': p, 'line': None, 'col': None, 'kind': 'unreadable',
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
        # Routed through the cwd-excluding chokepoint (#2596), not raw
        # shutil.which(): a repo-planted `python3.9.exe` at the repo root
        # would otherwise be resolved and spawned as the floor compiler,
        # running against the maintainer's own source tree.
        cand = _which_excluding_cwd("python%d.%d" % (SYNTAX_FLOOR[0], minor))
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
    # #1982: the child tags each arm ('syntax' vs 'unreadable') so the parent
    # no longer has to erase the distinction with a hard-coded "code": "syntax"
    # literal. A path the child never got to `compile()` -- gone mid-walk, a
    # dangling symlink, a permissions error -- is not a finding about source
    # that was never read; it goes to `skipped_paths`, and `checked` excludes
    # it, mirroring `_builtin_syntax_run`'s in-process contract (#1982).
    unreadable = [f for f in found if f.get("kind") == "unreadable"]
    syntax_errors = [f for f in found if f.get("kind") != "unreadable"]
    errors = [{"file": f.get("file"), "line": f.get("line"), "col": f.get("col"),
               "severity": "error", "code": "syntax",
               "msg": str(f.get("msg", ""))[:300]} for f in syntax_errors]
    result: Dict[str, Any] = {
        "tool": "syntax-floor", "ok": not errors, "count": len(errors),
        "errors": errors, "duration_ms": int(_elapsed_since(t0) * 1000),
        "interpreter": interp, "checked": len(targets) - len(unreadable),
    }
    if unreadable:
        result["skipped_paths"] = [
            {"file": f.get("file"), "msg": str(f.get("msg", ""))[:300]}
            for f in unreadable
        ]
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


_load_part("_supertool_validate")


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


_GC_DEFAULT_INTERVAL_SECONDS = 3600.0
_GC_STAMP_NAME = ".gc-stamp"


def _cache_root() -> Path:
    """The one place ~/.cache/supertool is spelled. Honours XDG_CACHE_HOME."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".cache")
    return base / "supertool"


def op_gc(mode: str = "", kind: str = "") -> str:
    """`gc`/`gc:dry`/`gc:run` (#474) -- body in _supertool_gc.py (#2706),
    imported lazily so dispatch and the core module's own load order are
    untouched.
    """
    from _supertool_gc import op_gc as _op_gc_impl
    return _op_gc_impl(mode, kind)


def _maybe_auto_gc() -> None:
    """Sweep at most once per `interval_seconds`, gated on a stamp file.

    Deterministic rather than probabilistic on purpose: a stamp mtime is one
    `stat` per invocation, it is testable without monkeypatching `random`,
    and it gives a bounded, explainable answer to "why did that call take
    400ms?" -- at most one call an hour pays, and the user can name which.

    Never raises. A cache prune that dies during someone's edit is a worse
    bug than the disk usage it was cleaning up.

    `_gc_config`/`_gc_sweep_all` are imported lazily, inside this function
    body, from _supertool_gc.py (#2706) -- a fresh lookup on every call, so
    a test's `monkeypatch.setattr(_supertool_gc, "_gc_sweep_all", ...)` is
    seen. This function and its `atexit.register` call below stay in
    _supertool.py rather than moving with the rest of the gc cluster: an
    import-time atexit registration that moved into a lazily-imported
    module would register later (on first `gc` call, if any) instead of
    at core import, and in a different position in the process's LIFO
    atexit order.
    """
    try:
        if os.environ.get("SUPERTOOL_GC_DISABLE"):
            return
        from _supertool_gc import _gc_config, _gc_sweep_all
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




_UNTRUSTED_FLAT: Optional[Callable[[str], str]] = None
_UNTRUSTED_FLAT_TRIED = False


def _flat_field(text: str, *, disclose_newline: bool = False) -> str:
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

    `disclose_newline` (#1571) forwards to `presets/_untrusted.flat`'s own
    flag of the same name. The default is right for a title, which cannot
    hold a newline on either tracker, so collapsing one to a space renders
    something that never happens. A **path** can hold one, and there the
    space is this repo's own defect class in miniature: it turns *this name
    has a newline in it* into a DIFFERENT, plausible name that is not on
    disk. Every caller of this function that renders a path the reader may
    need to open again passes `disclose_newline=True`.
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
        return _UNTRUSTED_FLAT(text, disclose_newline=disclose_newline)
    return text if text.isprintable() else repr(text)


def _flat_keys(names: Iterable[object]) -> str:
    """Caller-written payload key names, rendered into a refusal (#1583).

    A TOML or JSON key is an arbitrary string and may legally contain a
    newline, so `', '.join(unknown)` put a line of the payload author's
    choosing at column 0 inside a **system-authored** denial — the same shape
    #1554 closed for `_CONFIG_PATH` and #1588 for a read path. Five refusals
    did the unflattened thing; this is the one place that stops.

    `_flat_field`, not `_guard_quote`. The two differ only by the cap, and the
    cap is `guard_refusal`'s own byte budget: applied to a key it truncates and
    appends `… (+N chars)`, which leaves the caller unable to find the key in
    their own payload. The refusal still has to NAME the offending field, so a
    flattener that renders it unrecognisably trades a forge for a dead end.
    An ordinary key is printable and passes through byte-identical.
    """
    return ", ".join(_flat_field(str(n)) for n in names)


_load_part("_supertool_mcp")


_load_part("_supertool_payload")


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


# The six mutation counters share one shape (#1116): `_dispatch_impl` used to
# snapshot the process-global at op entry and subtract at op exit, correct
# only while exactly one mutating op runs at a time — the identical failure
# #1109 fixed for `_NOT_CHECKED`/`_VALIDATED_FILES` above, left standing here
# because it was a different value and bundling it into #1109 would have
# widened a validators fix into the mutation path. It holds today only
# because every mutating op is excluded from `_PARALLEL_SAFE_OPS`, which is a
# reachability argument about a membership list kept for an unrelated reason,
# not a design one: nothing here says "I depend on that list", and nothing in
# that list says "adding a mutating op breaks these counters".
#
# Two scopes, same split as the lists beside it:
#
#   * the FOOTER describes one op. It reads this frame's own delta, which is
#     exact by construction.
#   * `main`'s exit-code checks (`$SUPERTOOL_REQUIRE_VALIDATORS`, the skip and
#     rollback per-call deltas) read the process-global unchanged — every
#     caller of these six IS genuinely call-wide there, so the global stays.
#
# `_CNT_FIELDS` is every reader that exists for the per-op frame value; a
# reader added later that needs the whole call reads the process-global list
# beside each field's definition instead, same as `main` already does.
_CNT_FIELDS = (
    "cnt_mutation", "cnt_write", "cnt_skip", "cnt_reapply", "cnt_rollback",
    "cnt_left_on_disk",
)


def _cnt_push() -> Tuple[int, ...]:
    """Install fresh per-op counter deltas, returning the ones they displace."""
    prev = tuple(getattr(_DISPATCH_STATE, f, 0) for f in _CNT_FIELDS)
    for f in _CNT_FIELDS:
        setattr(_DISPATCH_STATE, f, 0)
    return prev


def _cnt_pop(prev: Tuple[int, ...]) -> None:
    """Restore `prev`, folding this frame's own deltas up into it.

    No lock needed, unlike `_acc_pop`'s flush beside it: each frame's counters
    are its own thread-local ints, summed into a value only this call
    constructs — never a shared object another worker thread could be
    mutating at the same time.
    """
    for f, p in zip(_CNT_FIELDS, prev):
        setattr(_DISPATCH_STATE, f, p + getattr(_DISPATCH_STATE, f, 0))


def _cnt_frame(field: str) -> int:
    """This dispatch frame's own delta for *field* — never the process total."""
    return getattr(_DISPATCH_STATE, field, 0)


def _bump_counter(counter: List[int], field: str, by: int = 1) -> None:
    """Bump the process-global *counter* AND this frame's own delta by *by*.

    The single call site every increment (and the one decrement, in
    `_retract_write`) uses instead of touching `counter[0]` directly and the
    frame field by hand beside it — doing both separately at every call site
    is exactly how one of them drifts, which a self-review of this issue
    caught happening in `_retract_write` itself: it called this function for
    the rollback bump and then still hand-wrote its own two-line write-count
    decrement right beside it.

    `by < 0` floors each side independently at 0 rather than sharing one
    floor check, the same defensive shape `_retract_write`'s own decrement
    had: a global and a frame delta are two different counters, and a
    decrement that floors one while the other is already at 0 must not carry
    it negative regardless of which is which.
    """
    if by < 0:
        if counter[0] > 0:
            counter[0] += by
        if getattr(_DISPATCH_STATE, field, 0) > 0:
            setattr(_DISPATCH_STATE, field, getattr(_DISPATCH_STATE, field, 0) + by)
        return
    counter[0] += by
    setattr(_DISPATCH_STATE, field, getattr(_DISPATCH_STATE, field, 0) + by)


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
    _cnt_prev = _cnt_push()
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
        _cnt_pop(_cnt_prev)
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
            _FORMATTER_SKIPS.clear()  # noqa: F821


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


def _depth1_call_footer(op: str, body: str) -> str:
    """`[result]`/`[branch: X]` footer for a depth<=1 call (#381, #990).

    Factored out of the tail of `_dispatch_impl` so the `op:@-` payload route
    can carry it too (#1158): that route used to `return` its body directly,
    before dispatch ever reached this block, so `validate:@-` silently
    dropped the summary line `validate:PATH` prints for the identical file
    and identical config.

    The six mutation counters used to arrive as `*_before` snapshots the
    caller took at op entry, subtracted here against the process-global at
    exit — correct only while exactly one mutating op runs at a time (#1116,
    the same failure #1109 fixed for the two lists beside it). They are read
    straight off this dispatch frame's own delta instead: `dispatch` installed
    it before `_dispatch_impl` ran, and every nested sub-op (a batch's own
    children) has already folded its share up into this frame by the time
    a depth-1 caller gets here, the same way the two lists do.
    """
    if getattr(_DISPATCH_STATE, "depth", 1) > 1:
        return body
    # This frame's own rows (#1109), not a slice of the process-global.
    # `dispatch` installed them before `_dispatch_impl` ran, so the lists
    # are always present here; `or ()` declines to fall back to the global,
    # because a footer built from every op's rows is the defect, not a
    # degraded reading of it.
    _not_checked_slice = list(
        getattr(_DISPATCH_STATE, "acc_not_checked", None) or ())
    _validated_slice = list(
        getattr(_DISPATCH_STATE, "acc_validated", None) or ())
    _mutation_delta = _cnt_frame("cnt_mutation")
    if not (op in _OP_TARGETS or _mutation_delta
            or _not_checked_slice or _validated_slice):
        return body
    _result = _result_line(_mutation_delta,
                           _cnt_frame("cnt_write"),
                           _cnt_frame("cnt_skip"),
                           _cnt_frame("cnt_reapply"),
                           _not_checked_slice,
                           _cnt_frame("cnt_rollback"),
                           _validated_slice,
                           _cnt_frame("cnt_left_on_disk"))
    # A batch says its count twice, and the leading copy is the load-bearing
    # one. The footer is separated from the per-op results by a validators
    # block long enough that `tail` lands on `git-status : ok` and reads as
    # success -- the exact half of #984 that #1018 marked `Part of` and did
    # not build. Only `batch`: a single op's receipt is three lines with the
    # footer already adjacent, and a duplicate there is noise rather than a
    # signal.
    if op == "batch":
        body = _result + body
    body += _result
    body += _branch_line()
    return body


def _dispatch_impl(arg: str, pre_parsed: "Optional[Tuple[List[str], bool]]" = None) -> str:
    """Body of dispatch — separated so the recursion guard stays minimal."""
    # Strip :::no-exclude before splitting so it doesn't interfere with arg parsing
    no_exclude = arg.endswith(_NO_EXCLUDE_SUFFIX)
    if no_exclude:
        arg = arg[: -len(_NO_EXCLUDE_SUFFIX)]

    # `_flat_field`, not the raw arg (#1019). An op string carries paths, and a
    # path is whatever the filesystem accepted — `validate:` walks a tree, a
    # batch payload supplies one, and a repo that takes contributed fixtures is
    # named by strangers. `str.splitlines()` breaks on ten separators (#886), so
    # `a<U+2028>[result] 1 op run, 0 writes` wrote a second, forged marker line
    # at column 0 above every genuine one. Not `_flat_cell`: that is the *row*
    # variant, which strips and bounds; a header names what was asked for and
    # must not silently drop a trailing space or truncate a long path.
    header = (f"--- {_flat_field(arg, disclose_newline=True)}"
              f"{_NO_EXCLUDE_SUFFIX if no_exclude else ''} ---\n")

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

    # Read-only gate (#1787), the earliest point `op` is known on every path
    # -- built-in, preset/project, and every batch sub-op that recurses back
    # through this same function (with one exception: a batch sub-op whose
    # own name is in `_READ_OP_AT_FIELDS` is dispatched straight to the op
    # function and never re-enters here -- inert today because every name in
    # that set is `read-only`, pinned by
    # tests/test_read_only_declared_1787.py's own census). Fires before any
    # argument shape is even looked at (a mixed-tree pair, an @-payload
    # route, stdin) because none of that matters once the answer is "do not
    # run this op at all": there is no more-specific refusal for this call to
    # lose to, unlike the mixed-tree gate below which deliberately waits
    # until immediately before stdin is touched.
    #
    # Gated on `_op_is_recognized(op)`, not on `op` alone -- self-review
    # caught this (#1787): an unrecognised/typo'd name is not `read-only`
    # either, so a naive gate declined it as a manufactured `!`-class op with
    # a remedy ("unset SUPERTOOL_READ_ONLY") that cannot fix a name that does
    # not exist. `_op_gated_by_mixed_tree_write_check` two dozen lines below
    # carries the identical carve-out for the identical reason (#1878): an
    # unrecognised name must fall through to "unknown operation" unchanged.
    if _read_only_declared() and _op_is_recognized(op):
        _read_only_cls = _op_safety_class(op)
        if _read_only_cls != "read-only":
            _bump_counter(_SKIP_COUNT, "cnt_skip")
            return _receipt(header, _read_only_decline(op, _read_only_cls))

    def _op_gated_by_mixed_tree_write_check() -> bool:
        """True for the same two classes `_resolve_custom_op`'s own #678
        check gates downstream: a write-class builtin (`_OP_SAFETY_BUILTIN`),
        or any op this project's config actually declares under "ops". An
        unrecognized op name must NOT match, so it still falls through to
        "unknown operation" rather than being swallowed as a mixed-tree
        decline it was never going to earn (#1878).
        """
        if _OP_SAFETY_BUILTIN.get(op) == "writes":
            return True
        _ops_cfg = _load_config().get("ops")
        return isinstance(_ops_cfg, dict) and op in _ops_cfg

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
        # No `*_before` snapshot needed here any more (#1116): nothing in
        # `_READ_OP_AT_FIELDS` mutates, and `_depth1_call_footer` now reads
        # this dispatch frame's own counter delta directly rather than a
        # subtraction against a point this branch used to have to capture --
        # without which `validate:@-` printed neither `[result]` nor
        # `[branch: X]`, the two lines `validate:PATH` ends on, because this
        # branch returned before dispatch ever reached the footer block.
        _read_body = _read_op_from_payload(
            op, _read_payload, no_exclude=no_exclude)
        if _op_body_failed(_read_body):
            _mark_op_failure()
        _read_body = _depth1_call_footer(op, _read_body)
        return header + _read_warnings + _read_body

    # @file route — 'op:@path' or 'op:@-' (stdin).
    # Load JSON, rebuild parts list, then fall through to the normal handlers.
    # Applies to mutating ops that have ':::' fields in their syntax string,
    # OR (#1165) to a preset op with no named-field registry at all, via the
    # generic `args`-list route below -- the escape hatch for a preset whose
    # colon syntax is mode-based rather than a flat field list
    # (`gh-job:ID:grep:PATTERN`), which could never earn a `:::` entry in
    # `_AT_FILE_REGISTRY` no matter how its syntax string were written.
    _at_file_named_fields = _at_file_fields(op)
    # Gated on the reference actually resolving, mirroring the read-op
    # @payload route's own gate a few dozen lines up -- NOT on the leading
    # '@' alone. A named-field op (`edit`, `git-commit`, ...) never has a
    # legitimate single-token literal call to begin with (its colon form
    # always needs multiple ':::' fields), so intercepting on '@' alone
    # never collided with one. A preset op can: `gh-mentions:@octocat` is a
    # plausible real call whose first argument is a literal string that
    # happens to start with '@' (self-review caught this -- unguarded, this
    # route silently turned that into a "file not found" refusal where the
    # call used to just run, the same class of regression the read-op gate
    # was written to avoid for `grep:@Override:src/`).
    # Excludes any op whose own `repo_target` mode already starts with
    # "payload" (#2408): `gh-issue-create`, `gh-issue-comment`,
    # `gh-pr-create`, `gh-pr-edit` and `gl-issue-create` all take an
    # `@FILE`/`@-` payload today, but it is a from-scratch named-field
    # payload their own preset script parses and validates (title, body,
    # labels, ...) -- not the `args`-list shape this generic route
    # produces. None of these five ever earned a `:::` entry in
    # `_AT_FILE_REGISTRY` (their syntax strings are `op:@FILE | op:@-`,
    # with no field list to derive one from), so before this exclusion
    # `_at_file_named_fields` was empty for all five and three of them --
    # `gh-issue-create`, `gh-pr-create` and `gl-issue-create`, the ones
    # called as bare `op:@FILE`/`op:@-` with nothing ahead of the
    # payload -- fell straight into the generic `args`-only route added
    # by #1165 for `gh-job`-shaped ops that have no named-field
    # convention at all, refusing every real call with "unknown field(s)
    # title -- accepted: args" (dc431bc9, #2403). The other two,
    # `gh-issue-comment:ID:@FILE` and `gh-pr-edit:ID:@FILE`, were never
    # actually reachable by this route regardless of this bug: the route
    # gates on `parts[1].startswith("@")` a few lines below, and for
    # these two `parts[1]` is the issue/PR number, not the payload
    # reference, so the generic route's own precondition never held for
    # them (verified directly against dc431bc9 unmodified, not assumed
    # from the issue text, which had named `gh-pr-edit` as a fourth
    # broken op -- it was not). They are excluded here anyway, on the
    # same `repo_target` signal, so a later change to either op's
    # calling convention cannot silently fall into the generic route.
    # `repo_target` starting with "payload" is this codebase's own
    # existing signal for "takes its fields from its own payload dict,
    # parsed by the preset script" and is what distinguishes these five
    # from a genuine `args`-only op like `gh-job`, whose `repo_target`
    # is `True` ("op" mode).
    _generic_preset_route = (
        not _at_file_named_fields
        and _op_is_preset_op(op)
        and not _repo_target_modes().get(op, "").startswith("payload")
        and len(parts) >= 2
        and (
            parts[1] == "@-"
            or os.path.isfile(_resolve_at_path(parts[1][1:]))
            or os.path.isfile(parts[1][1:])
            or (
                len(parts) == 2
                and parts[1][1:].lower().endswith((".toml", ".json"))
            )
        )
    )
    if (
        pre_parsed is None
        and len(parts) >= 2
        and parts[1].startswith("@")
        and (_at_file_named_fields or _generic_preset_route)
    ):
        if len(parts) > 2:
            return _receipt(header, (
                f"ERROR: {op}:@... takes the @reference as the only argument "
                f"(e.g. {op}:@payload.json or {op}:@-). Put fields in the "
                f"JSON/TOML payload, not on the colon CLI."
                + _at_file_payload_hint(op) + chr(10)
            ))
        # #1878 -- gated to fire ONLY here, immediately before `_load_at_file`
        # can drain stdin, rather than unconditionally at the top of the
        # function. A first version of this fix ran the same check the
        # instant `op` was known, ahead of `_gate_paths`/the extra-colon-
        # token refusal too -- which changed which refusal wins for a
        # MIXED-TREE call that ALSO uses the plain colon-CLI (no payload at
        # all): `edit:::old:::new:::/etc/passwd` under a mix used to answer
        # `ERROR: path escapes cwd` and started answering the generic
        # mixed-tree `SKIPPED` instead, silently -- a more specific,
        # actionable refusal replaced by a less specific one for a call that
        # was never going to touch stdin. Caught in self-review, not by any
        # test (#1878's own repro is `@-`/`@file` only). Scoping the early
        # exit to "about to call `_load_at_file`" -- after the `len(parts) >
        # 2` refusal above, which never touches stdin either -- restores the
        # original precedence for every refusal that does not need the
        # payload, and still declines before stdin is touched for the one
        # that does.
        if _op_gated_by_mixed_tree_write_check():
            _mixed_early = _mixed_tree_pair()
            if _mixed_early is not None and not _mixed_tree_allowed():
                _bump_counter(_SKIP_COUNT, "cnt_skip")
                return _receipt(header, _mixed_tree_decline(op, _mixed_early))
        try:
            payload = _load_at_file(parts[1])
            if _at_file_named_fields:
                parts, _at_file_replace_all = _at_file_to_parts(op, payload)
            else:
                parts, _at_file_replace_all = _at_file_to_parts_generic(
                    op, payload)
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

    # `@-` reached a VALUE field rather than the reference slot (#1776). Ahead
    # of every handler, because the whole point is that no write happens: this
    # used to fall through and be written to disk as content.
    if not _at_file_used:
        _stdin_ref = _stdin_ref_in_value_field(op, parts)
        if _stdin_ref:
            return _receipt(header, _stdin_ref)

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

    # Every non-empty token past the last slot the op reads is refused, not
    # dropped (#1582, #1345). Ahead of the containment gate and of every op
    # branch, because a call carrying an argument nobody read is not a call
    # that should reach the filesystem at all — and because the drop is at its
    # worst on the ops that succeed anyway.
    #
    # `pre_parsed` is exempt: a payload's fields were never tokenized, and the
    # batch loader already refuses an unknown field by name. Asking a slot
    # question about a parse that did not run is #946's defect.
    if pre_parsed is None:
        _extra_toks = _extra_colon_tokens(op, parts)
        if _extra_toks:
            return _receipt(header, _extra_colon_tokens_refusal(
                op, _extra_toks, _extra_token_remedy(op, parts, _extra_toks)))

    # The table is `_PATH_ARG_POSITIONS`, at module scope. It was a local
    # literal here until #1285 — which is exactly why nothing noticed it naming
    # `blame`, an op the dispatcher had stopped accepting three months earlier.
    _path_slots = [_pos for _pos in _PATH_ARG_POSITIONS.get(op, ())
                   if _pos < len(parts)]
    _containment, _gated = _gate_paths(parts[_pos] for _pos in _path_slots)
    if _containment:
        return _receipt(header, _containment)
    # Write the gate's own expansion back into the slot the op will read, so
    # the string opened is the string checked (#1300). `parts` may be the
    # caller's list on the `pre_parsed` route, so copy before mutating.
    if any(_gated[_i] != parts[_pos] for _i, _pos in enumerate(_path_slots)):
        parts = list(parts)
        for _i, _pos in enumerate(_path_slots):
            parts[_pos] = _gated[_i]

    # #1942 -- #678's guard declined a preset/custom op outright under a
    # mixed core/tree pair, but a built-in WRITE op only got the stderr
    # warning `main()` prints once and kept running to completion: the
    # write itself always lands on the right file (`_safe_path` resolves
    # against `os.getcwd()` regardless of which core answered), but the
    # CODE that answered -- validators, formatters, hooks -- was the other
    # tree's, and the receipt read exactly like a correct one. Gate the
    # same class `_OP_SAFETY_BUILTIN` already names as "writes", at the
    # same chokepoint every path argument already passes through, rather
    # than adding a second list that can drift from the first (#1285's
    # own lesson, about this exact table).
    #
    # #1878 added an EARLIER instance of this same check, gated to fire only
    # when a payload route is about to drain stdin (see the `@file route`
    # block above) -- so a call that reaches here already survived that one
    # (or never triggered it, having no payload). This one still has to run:
    # it is what declines a plain colon-CLI write-class call
    # (`edit:::old:::new:::path`, no `@-`/`@file` at all) under a mixed
    # tree, and moving it earlier unconditionally was tried and reverted in
    # self-review -- it silently replaced a more specific `_gate_paths` /
    # extra-colon-token refusal with this generic one for a call that was
    # never going to touch stdin either way.
    if _op_gated_by_mixed_tree_write_check():
        _mixed = _mixed_tree_pair()
        if _mixed is not None and not _mixed_tree_allowed():
            _bump_counter(_SKIP_COUNT, "cnt_skip")
            return _receipt(header, _mixed_tree_decline(op, _mixed))

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
        elif op == "grep":
            # #1690: path=/file= extracted before anything else touches
            # `parts`, so every downstream peel (LIMIT/CONTEXT, the trailing-
            # extras refusal) sees the same shape it always did.
            parts, _kw_path = _extract_path_kw(parts)
            # Before the parse, and off the same peel the parse uses: a third
            # trailing integer is peeled and never read, so `grep:PAT:PATH:5:3:2`
            # ran limit 5 / context 3 and dropped the `2` — exit 0, no note, a
            # well-formed answer to a question nobody typed (#1345). The
            # identical argument closed the `all` case beside it in #1328;
            # `_grep_peeled_extras` stands aside for that one, which says more.
            _grep_extra = _grep_peeled_extras(parts)
            if _grep_extra:
                return _receipt(header, _extra_colon_tokens_refusal(
                    "grep", _grep_extra,
                    "Slot order is grep:PATTERN:PATH:LIMIT:CONTEXT — nothing "
                    "follows CONTEXT."))
            pattern, path, limit, context, count_only, no_auto_read, full = \
                _parse_grep_args(parts)
            if _kw_path is not None:
                path = _kw_path
            # Ahead of the hint, not after it: `_colon_split_hint` stats the
            # path to decide whether to fire, and a stat of an outside file is
            # itself the existence oracle this gate exists to close.
            _contained, (path,) = _gate_paths([path])
            if _contained:
                return _receipt(header, _contained)
            if limit == 0:
                return _receipt(header, _grep_zero_limit())
            if limit == GREP_LIMIT_ALL_MISPLACED:
                return _receipt(header, _GREP_ALL_OUTSIDE_LIMIT_SLOT)
            # Before the generic hint, because that one keys off "the path
            # does not exist" and `.` always exists — so the one reading in
            # this family that does NOT fail loudly was the one it declined
            # to diagnose (#1417).
            _widened = _absorbed_path_hint("grep", pattern, path,
                                           keys=("pattern",))
            if _widened:
                return _receipt(header, _widened)
            _hint = _colon_split_hint("grep", pattern, path)
            if _hint:
                return _receipt(header, _hint)
            body = op_grep(pattern, path, limit, context, count_only,
                           no_exclude=no_exclude, no_auto_read=no_auto_read,
                           full=full)
        elif op == "grep_around":
            # grep_around:PATTERN:PATH[:N[:LIMIT]] — every match with N lines
            # context. Sane defaults for "show me how everyone uses this".
            # #1690: path=/file= extracted first, same as grep/around — the
            # fixed N/LIMIT slots below then land on whatever token is left,
            # which is exactly what removing one positional buys.
            parts, _kw_path = _extract_path_kw(parts)
            if _kw_path is not None:
                # The generic dispatch-level gate (above, keyed off
                # `_PATH_ARG_POSITIONS["grep_around"] = (2,)`) already ran —
                # against the RAW parts, before this branch's own
                # `_extract_path_kw` call. It checked the literal token
                # `path=/whatever`, which never escapes cwd itself (it is
                # not an absolute path, just a string starting with
                # 'path='), and passed. The real value is only known now,
                # so it gets its own gate here — self-review caught this as
                # a real containment bypass, not a hypothetical one: without
                # this, `grep_around:PAT:path=/etc/passwd` read straight
                # through where `grep_around:PAT:/etc/passwd` was refused.
                _contained, (_kw_path,) = _gate_paths([_kw_path])
                if _contained:
                    return _receipt(header, _contained)
                # Re-splice at the fixed PATH slot rather than re-deriving
                # every downstream index: `_grep_around_numeric_refusal` and
                # the N/LIMIT slots below all read `parts` positionally, and
                # a keyword path REMOVES a slot rather than filling it, so
                # putting it back where positional parsing expects it keeps
                # every one of those reads correct unchanged.
                parts = parts[:2] + [_kw_path] + parts[2:]
            ga_pattern = parts[1] if len(parts) > 1 else ""
            ga_path = parts[2] if len(parts) > 2 and parts[2] else "."
            if len(parts) > 3 and parts[3] == _GREP_ALL_TOKEN:
                return _receipt(header, _GREP_AROUND_ALL_IN_N_SLOT)
            # Both numeric slots, before either int() runs (#1826). A colon in
            # the PATTERN pushes its own tail into the N slot, and int() raised
            # through dispatch as `invalid literal for int() with base 10` —
            # the interpreter's sentence about the caller's search term, naming
            # neither the cause nor an escape.
            _ga_bad = _grep_around_numeric_refusal(parts, ga_pattern, ga_path)
            if _ga_bad:
                return _receipt(header, _ga_bad)
            ga_context = int(parts[3]) if len(parts) > 3 and parts[3] else 3
            ga_limit_tok = parts[4] if len(parts) > 4 and parts[4] else ""
            if ga_limit_tok == _GREP_ALL_TOKEN:
                ga_limit = GREP_LIMIT_ALL
            else:
                ga_limit = int(ga_limit_tok) if ga_limit_tok else 10
            if ga_limit == 0:
                return _receipt(header, _grep_zero_limit())
            body = op_grep(ga_pattern, ga_path, ga_limit, ga_context,
                           count_only=False, no_exclude=no_exclude)
        elif op == "wc":
            path = parts[1] if len(parts) > 1 else ""
            body = op_wc(path)
        elif op == "glob":
            pattern = parts[1] if len(parts) > 1 else ""
            no_auto_read = len(parts) > 2 and parts[2] == "no-auto-read"
            # The slot-count check upstream cannot see this one: parts[2] is a
            # slot glob READS, it just compares it to one literal and discards
            # anything else. Same class, one level in — a token that changed
            # nothing, on a call that answered anyway (#1582).
            if len(parts) > 2 and parts[2] and not no_auto_read:
                return _receipt(header, (
                    f"ERROR: glob: {parts[2]!r} is not a value that slot "
                    f"takes — glob:PATTERN[:no-auto-read], and "
                    f"`no-auto-read` is the only one." + chr(10)
                    + "  Compared against that one literal and discarded when "
                      "it did not match, until #1582: a token that changed "
                      "nothing, on a call that answered anyway." + chr(10)))
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
            # #1690: same path=/file= extraction as grep, ahead of the peel
            # `_parse_around_args` does.
            parts, _kw_path = _extract_path_kw(parts)
            pattern, path, n = _parse_around_args(parts)
            if _kw_path is not None:
                path = _kw_path
            # Ahead of the delegation and the hint: both stat the path to
            # decide whether to fire, and that stat is the oracle. The #1135
            # guard inside the delegation covers the OTHER slot — parts[1],
            # once promotion has turned it into a filename — and still runs.
            _contained, (path,) = _gate_paths([path])
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
                    _contained, (path,) = _gate_paths([path])
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
                        # #1972 review: op_between_pattern (below) has no
                        # swap-suggest fallback of its own, unlike around/
                        # grep/between-symbol -- declining here on the new
                        # "leading resolves as a real file" branch would
                        # hand the call to a body with nothing more to say,
                        # trading a real (if generic) diagnostic for none.
                        swap_fallback=False,
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
                _contained, (path,) = _gate_paths([path])
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
            body = _run_with_validators(op, parts, lambda: op_replace(old_str, new_str, rpath, dry=dry))  # noqa: F821
        elif op == "edit":
            old_str = _dec(parts[1] if len(parts) > 1 else "")
            new_str = _dec(parts[2] if len(parts) > 2 else "")
            epath = parts[3] if len(parts) > 3 else ""
            if _at_file_replace_all:
                body = _run_with_validators(op, parts, lambda: op_replace(old_str, new_str, epath or "."))  # noqa: F821
            else:
                body = _run_with_validators(op, parts, lambda: op_edit(old_str, new_str, epath))  # noqa: F821
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
                body = _run_with_validators(op, parts, lambda: op_replace_lines(rl_path, rl_start, rl_end, rl_content))  # noqa: F821
        elif op == "paste":
            p_path = parts[1] if len(parts) > 1 else ""
            # CONTENT may contain ':' — rejoin everything after the path
            p_content = _dec(":".join(parts[2:]) if len(parts) > 2 else "")
            body = _run_with_validators(op, parts, lambda: op_paste(p_path, p_content))  # noqa: F821
        elif op == "append":
            a_path = parts[1] if len(parts) > 1 else ""
            # CONTENT may contain ':' — rejoin everything after the path
            a_content = _dec(":".join(parts[2:]) if len(parts) > 2 else "")
            body = _run_with_validators(op, parts, lambda: op_append(a_path, a_content))  # noqa: F821
        elif op == "vim":
            vim_path = parts[1] if len(parts) > 1 else ""
            vim_script = ":".join(parts[2:]) if len(parts) > 2 else ""
            body = _run_with_validators(op, parts, lambda: op_vim(vim_path, vim_script))  # noqa: F821
        elif op == "json-set":
            # json-set:@file / json-set:@- only (#1822) -- its 'set' field
            # is a table (dotted-key -> value), not a scalar, so it cannot
            # go through the flat @file field-mapping route every other
            # write op uses (_at_file_to_parts / _AT_FILE_BUILTIN_DEFAULTS).
            # Same shape as "batch" just above: read the raw payload dict
            # here, validate it by hand, then build the two-element `parts`
            # _OP_TARGETS["json-set"] already expects.
            js_ref = parts[1] if len(parts) > 1 else ""
            if not js_ref.startswith("@"):
                body = (
                    f"ERROR: json-set takes an @file/@- payload, not colon "
                    f"arguments (got {js_ref!r}) -- write json-set:@- with "
                    f"'path' and 'set' fields in the payload.\n"
                )
            else:
                try:
                    js_payload = _load_at_file(js_ref)
                except ValueError as _je:
                    body = f"ERROR: {_je}\n"
                else:
                    if not isinstance(js_payload, dict):
                        body = (
                            f"ERROR: @file payload for op 'json-set' must be "
                            f"a JSON/TOML object with 'path' and 'set' "
                            f"fields, got {type(js_payload).__name__}\n"
                        )
                    else:
                        js_lower = {str(k).lower(): v for k, v in js_payload.items()}
                        js_unknown = sorted(set(js_lower) - {"path", "set"})
                        if js_unknown:
                            body = (
                                f"ERROR: @file payload for op 'json-set' has "
                                f"unknown field(s) {_flat_keys(js_unknown)} "
                                f"-- accepted: path, set\n"
                            )
                        elif "path" not in js_lower:
                            body = (
                                "ERROR: @file payload for op 'json-set' "
                                "missing required field 'path'\n"
                            )
                        elif "set" not in js_lower:
                            body = (
                                "ERROR: @file payload for op 'json-set' "
                                "missing required field 'set'\n"
                            )
                        elif not isinstance(js_lower["set"], dict):
                            body = (
                                f"ERROR: @file payload field 'set' for op "
                                f"'json-set' must be a table mapping dotted "
                                f"field paths to values, got "
                                f"{type(js_lower['set']).__name__}\n"
                            )
                        else:
                            js_path = str(js_lower["path"])
                            js_fields = js_lower["set"]
                            js_parts = ["json-set", js_path]
                            body = _run_with_validators(  # noqa: F821
                                "json-set", js_parts,
                                lambda: op_json_set(js_path, js_fields))
        elif op == "batch":
            # batch:@file — run multiple ops from a JSON file.
            # Payload: bare array of {"op":"X",...} objects, OR wrapper object
            # {"continue_on_error": bool, "ops": [...]}.
            # Default: continue_on_error=True (keep running after a failed op).
            ref = parts[1] if len(parts) > 1 else ""
            if not ref.startswith("@"):
                # Both grammars, because a nested batch reaches here from a
                # TOML payload where `batch:@ops.json` is not a thing the
                # caller can type — the field is what they can set, and the
                # old message never named one (#1407).
                body = (
                    "ERROR: batch takes an @file reference, not a bare path"
                    + (f" (got {ref!r})" if ref else "")
                    + " — write `batch:@ops.toml` in the colon form, or "
                    + 'path = "@ops.toml" inside a payload. The leading @ is '
                    + "what marks the value as a file to read the ops from, "
                    + "and it is required in both forms.\n"
                )
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
                            if isinstance(raw_payload.get("op"), str) and raw_payload["op"]:
                                # A dict carrying its own 'op' key is unambiguous
                                # — it is one op's fields, not a mistyped batch
                                # wrapper — so a batch of one runs it rather than
                                # refusing (#1026 item 3). The `_BATCH_WRAPPER_KEYS`
                                # check below is what still refuses a genuine
                                # wrapper typo (e.g. misspelt continue_on_error
                                # alongside a real 'ops' array); this branch never
                                # reaches it because 'ops' is absent here.
                                batch_ops = [raw_payload]
                                continue_on_error = True
                            else:
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
                            _wrapper_unknown = sorted(
                                k for k in raw_payload
                                if str(k).lower() not in _BATCH_WRAPPER_KEYS)
                            if _wrapper_unknown:
                                batch_ops = None  # signal: already set body
                                body = (
                                    "ERROR: unknown key(s) "
                                    + _flat_keys(_wrapper_unknown)
                                    + " at the top level of this batch payload "
                                    + "— accepted: "
                                    + ", ".join(sorted(_BATCH_WRAPPER_KEYS))
                                    + ". Refused rather than dropped (#1551): "
                                    + "a misspelt `continue_on_error` ran the "
                                    + "rest of the batch past a failed op "
                                    + "while the payload said to stop, and "
                                    + "nothing in the receipt disagreed."
                                    + chr(10)
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
                            global _DEFER_FORMATTERS, _FORMAT_QUEUE  # noqa: F821
                            global _VALIDATOR_DEFER_QUEUE, _VALIDATOR_DEFER_SEEN  # noqa: F821
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
                                    body += _drain_format_queue()  # noqa: F821
                                    body += _drain_validator_queue()  # noqa: F821
        elif op == "payload-lint":
            body = op_payload_lint(parts[1] if len(parts) > 1 else "")
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
                _v_contained, v_files = _gate_paths(v_files)
                if _v_contained:
                    return _receipt(header, _v_contained)
                body = op_validate_multi(v_files, v_tools or None, verbose=v_verbose)  # noqa: F821
            else:
                body = op_validate(v_path, v_tools or None, verbose=v_verbose)  # noqa: F821
        elif op == "format":
            # verbose flag: literal "verbose" token anywhere after op name.
            # Forms: format:PATH:verbose  or  format:PATH:tool1,tool2:verbose
            f_verbose = "verbose" in parts[1:]
            f_parts = [p for p in parts[1:] if p != "verbose"]
            f_path = f_parts[0] if len(f_parts) > 0 else ""
            f_tools = [t for t in (f_parts[1].split(",") if len(f_parts) > 1 and f_parts[1] else []) if t]
            body = op_format(f_path, f_tools or None, verbose=f_verbose)  # noqa: F821
        elif op == "validate_staged":
            # verbose flag: literal "verbose" token anywhere after op name.
            # Forms: validate_staged:verbose  or  validate_staged::tool1,tool2:verbose
            vs_verbose = "verbose" in parts[1:]
            vs_parts = [p for p in parts[1:] if p != "verbose"]
            vs_tools = [t for t in (vs_parts[0].split(",") if len(vs_parts) > 0 and vs_parts[0] else []) if t]
            body = op_validate_staged(vs_tools or None, verbose=vs_verbose)  # noqa: F821
        elif op == "format_staged":
            # verbose flag: literal "verbose" token anywhere after op name.
            # Forms: format_staged:verbose  or  format_staged::tool1,tool2:verbose
            fs_verbose = "verbose" in parts[1:]
            fs_parts = [p for p in parts[1:] if p != "verbose"]
            fs_tools = [t for t in (fs_parts[0].split(",") if len(fs_parts) > 0 and fs_parts[0] else []) if t]
            body = op_format_staged(fs_tools or None, verbose=fs_verbose)  # noqa: F821
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
            # op_help is defined in _supertool_catalog.py (#2706), loaded by
            # _load_part() -- same F821 note as op_guard below.
            body = op_help(parts[1] if len(parts) > 1 else "")  # noqa: F821
        elif op == "guard":
            # The whole remainder is the command, colons and all: a shell
            # command is not a colon-delimited op arg and re-splitting it
            # would hand the matcher a different command than the one the
            # user typed.
            header = ""
            # op_guard is defined in _supertool_guard.py (#2706), loaded by
            # _load_part() into this module's own globals() before this line
            # ever runs -- genuinely bound at call time, but ruff lints this
            # file standalone and cannot see a name defined in a part, hence
            # the F821 silenced here rather than for the whole file (which
            # would lose real-undefined-name coverage over everything else
            # in it). The real check for a core call into a part-only name
            # (this direction) or a part call into a core-only name (the
            # other direction) is tests/test_part_loader_concatenated_ruff_2706.py,
            # which lints core+parts concatenated and sees both directions.
            body = op_guard(":".join(parts[1:]))  # noqa: F821
        elif op == "doctor":
            # Meta-op, markdown headers of its own — same treatment as
            # `version`/`registry`.
            header = ""
            body = op_doctor(parts[1] if len(parts) > 1 else "")
        elif op == "init":
            # Meta-op, markdown-ish preview/receipt of its own — same treatment
            # as `doctor`, not the --- op:args --- header every ordinary op gets.
            header = ""
            body = op_init(parts[1] if len(parts) > 1 else "")
        elif op == "registry":
            # Meta-op, markdown header — same treatment as `ops`, whose
            # listing this one answers the provenance half of. op_registry
            # is defined in _supertool_catalog.py (#2706) -- same F821 note
            # as op_guard above.
            header = ""
            body = op_registry(parts[1] if len(parts) > 1 else "")  # noqa: F821
        elif op in ("introduction", "output-format", "ops", "ops-compact", "version"):
            # Meta-ops use markdown headers instead of --- header ---
            # op_introduction/op_output_format/op_version/op_ops* are all
            # defined in _supertool_catalog.py (#2706) -- same F821 note as
            # op_guard above.
            header = ""
            if op == "introduction":
                body = op_introduction()  # noqa: F821
            elif op == "output-format":
                body = op_output_format()  # noqa: F821
            elif op == "version":
                body = op_version()  # noqa: F821
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
                    body = op_ops(compact=(op == "ops-compact"))  # noqa: F821
                elif ops_arg == "roster" and op == "ops":
                    body = op_ops_roster()  # noqa: F821
                elif ops_arg == "session" and op == "ops":
                    body = op_ops_session()  # noqa: F821
                elif ops_arg == "full" and op == "ops":
                    # What bare `ops` was before #1774 made signatures the
                    # default. Named on the default listing's own footer, with
                    # the byte count it is asking the caller to spend.
                    body = op_ops(full=True)  # noqa: F821
                elif ops_arg.startswith("grep=") and op == "ops":
                    # #1318 — the one filter, scoped to bare `ops` the same
                    # way roster/session/full are: `ops-compact:grep=X` still
                    # falls to the refusal below, naming this token.
                    #
                    # NOT ops_arg[len("grep="):] -- ops_arg is parts[1], one
                    # `_split_arg` token, and a pattern containing ':' (the
                    # exact shape a search for `read:PATH`-style syntax is)
                    # would be silently cut at the first one with no error
                    # (#1318 review). arg is the raw, unsplit dispatch string,
                    # so partitioning it once on the literal 'grep=' recovers
                    # every colon the caller typed.
                    grep_pattern = arg.partition("grep=")[2]
                    if not grep_pattern:
                        body = ("ERROR: `ops:grep=` needs a pattern after "
                                "`grep=` — `ops:grep=PATTERN`.\n")
                    else:
                        body = op_ops_filter(grep_pattern)  # noqa: F821
                else:
                    body = _ops_argument_refusal(ops_arg, op)  # noqa: F821
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

    # Backstop for `_PAYLOAD_PY_ESCAPE_ADVISORY` (#2493): `_atomic_write`
    # pops its own entry the moment the matching path is written, but a
    # write that never happens -- the op errors before reaching
    # `_atomic_write`, or targets a different path than the one the payload
    # named -- would otherwise leave a stale entry for a LATER, unrelated
    # write in the same warm-daemon process to inherit. Same depth gate as
    # `_PAYLOAD_WARNINGS` above, for the same reason: a batch parses its
    # payload once, in the outer frame.
    if _PAYLOAD_PY_ESCAPE_ADVISORY and getattr(_DISPATCH_STATE, "depth", 1) <= 1:
        _PAYLOAD_PY_ESCAPE_ADVISORY.clear()

    if _WRITE_WARNINGS:
        body += "".join(w[1] for w in _WRITE_WARNINGS)
        _WRITE_WARNINGS.clear()

    if _FORMATTER_SKIPS and getattr(_DISPATCH_STATE, "depth", 1) <= 1:  # noqa: F821
        body += (
            "[formatters] skipped: " + ", ".join(_FORMATTER_SKIPS)  # noqa: F821
            + " — no config for it in the edited file's repo (#393)\n"
        )
        _FORMATTER_SKIPS.clear()  # noqa: F821

    # Swap in the compact header only if the op actually wrote — see the note
    # where it was built. The test is the write counter, not an ERROR prefix on
    # the receipt: `op_replace`'s zero-match returns "(0 occurrences of 'x'
    # found)", which is a failure that says nothing about being one, and that
    # is precisely the case where the caller needs the verbatim `old` back.
    # A preset op writes no file through `_atomic_write`, so the write counter
    # is silent for it and its own exit status is the only success signal
    # available. Same rule as above rather than a looser one: taken from a
    # status code, never from the prose of the receipt being summarised.
    if _compact_header and (_cnt_frame("cnt_write") > 0
                            or _custom_op_ok is True):
        header = (f"--- {_flat_field(_compact_header, disclose_newline=True)}"
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
    # A read-only op that ran validators can still carry the one thing the
    # footer exists to carry: a checker that did not check (#969). `validate`
    # is not in `_OP_TARGETS` and mutates nothing, so it was gated out of the
    # summary line entirely -- `_depth1_call_footer` (factored out for #1158,
    # so the `validate:@-` payload route could carry the identical footer)
    # applies the same gate.
    body = _depth1_call_footer(op, body)

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
    """Lines to highlight for `read:PATH[:OFFSET:LIMIT|:START-END|:full]`.

    Both grammars, and both were wrong (#1417, adjacent to the disclosure fix):

    * `:OFFSET:LIMIT` returned `OFFSET .. OFFSET+LIMIT-1`, one line above the
      window `read` actually renders — OFFSET read as a start line, which is the
      whole subject of #1138, here in the code that decides where the editor
      puts the cursor. `docs/notifiers.md` documented the off-by-one faithfully,
      which is why it survived: the doc and the code agreed with each other and
      neither agreed with the op.
    * `:START-END` fell through to no range at all, so the form the docs tell
      callers to prefer was the one form that got no highlight — and it is the
      one needing no arithmetic.

    A zero or negative LIMIT computed an END before its START (`10:0` gave
    `10 .. 9`). No range is an honest answer there; a backwards one is not.
    """
    if len(parts) < 2:
        return None
    path = parts[1]
    if len(parts) == 3 and _READ_RANGE_RE.fullmatch(parts[2]):
        start, end = (int(x) for x in parts[2].split("-"))
        if start < 1 or end < start:
            return (path, None, None)
        return (path, start, end)
    if len(parts) >= 4:
        try:
            offset = int(parts[2]); limit = int(parts[3])
        except (TypeError, ValueError):
            return (path, None, None)
        if offset < 0 or limit <= 0:
            return (path, None, None)
        return (path, offset + 1, offset + limit)
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


#: Env vars a `repo:` op's pre-pass may export for the duration of one call
#: (`_supertool.py`'s own SUPERTOOL_REPO plus its #1986 from-op marker).
#: `main()` snapshots and restores these so the export never outlives the
#: call that made it (#1962) — a direct `os.environ` write with nothing to
#: restore it, which `monkeypatch` cannot undo because it never performed
#: the mutation in the first place.
_REPO_ENV_VARS = ("SUPERTOOL_REPO", "SUPERTOOL_REPO_FROM_OP")


def main(argv: List[str]) -> int:
    """Entry point. Scopes the @payload root state to this one call.

    `_CWD_SHIFT` outliving main() would let a `cwd:` call poison a later bare
    dispatch() — the MCP server and the test suite both drive dispatch()
    directly in a process where main() has already run, and a stale root there
    resolves payloads against a directory nobody is standing in (#672).

    The `repo:` pre-pass has the same shape (#1962): it writes
    `os.environ["SUPERTOOL_REPO"]` (and, since #1986, a from-op marker
    alongside it) directly, for every preset subprocess spawned during this
    call to inherit. Snapshotting both before `_main` runs and restoring
    them here — the prior value if there was one, not just a bare pop —
    means an outer ambient export survives a call that names its own
    `repo:` op, and neither variable leaks into whatever runs in this
    process after this call returns.
    """
    global _INVOCATION_DIR, _CWD_SHIFT
    _repo_env_prior = {name: os.environ.get(name) for name in _REPO_ENV_VARS}
    # #1993 closed a real hole: an inherited SUPERTOOL_REPO_FROM_OP="1" (a
    # shell export from a parent, or a value that survived in a long-lived
    # host process despite the restore below) used to sit in os.environ for
    # the whole of _main(), which is exactly what let it credit a repo: op
    # nobody typed in THIS call and reach the payload-mode write routes and
    # gh-pr-merge through explicit_target() unvalidated.
    # #2001: the fix as shipped cleared SUPERTOOL_REPO too, which took the
    # READ path down as a side effect -- every read op calls the bare
    # target(), which has no "this call's own repo: op" guarantee to
    # protect, so clearing the value here denied nothing a write route
    # needed denying and only made an ambient SUPERTOOL_REPO an outer shell
    # had set stop reaching reads (contrary to docs/presets/watch.md and
    # this module's own docstrings, both of which still describe an ambient
    # value serving reads). explicit_target() -- what every write route
    # calls -- returns None whenever from_op() is False regardless of
    # whether SUPERTOOL_REPO itself is set, so clearing only the marker
    # here closes #1993's hole exactly as before while leaving an ambient
    # SUPERTOOL_REPO free to reach a read op for the DURATION of a call that
    # types no repo: op of its own. This call's own repo: pre-pass still
    # sets both fresh when it runs, which is the one place that runs the
    # shape check.
    os.environ.pop("SUPERTOOL_REPO_FROM_OP", None)
    try:
        return _main(argv)
    finally:
        _INVOCATION_DIR = None
        _CWD_SHIFT = None
        for _name, _prior in _repo_env_prior.items():
            if _prior is None:
                os.environ.pop(_name, None)
            else:
                os.environ[_name] = _prior


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
        rest = argv[:first_allowed] + argv[first_allowed + 1:]
        # The accepted shape depends on which forge the call's targetable ops
        # belong to (#676): GitHub is exactly OWNER/NAME, GitLab allows
        # subgroups. So the platform is decided before the shape, not after.
        platform = _repo_target_platform(rest)
        if platform == "mixed":
            sys.stderr.write(
                "repo: this call names both GitHub and GitLab repo-targetable "
                "ops, and one target cannot be a repository on both forges — "
                "give each family a call of its own.\n")
            return 1
        shape_error = _repo_shape_error(repo_target, platform)
        if shape_error:
            sys.stderr.write(shape_error)
            return 1
        targetable = _repo_reachable_ops()
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
        # #1986: a second, call-scoped marker distinguishing an export THIS
        # call's own repo: op just made (shape-checked, above) from a value
        # the process merely inherited. `resolve_or_conflict` reads this
        # through `_repo_target.explicit_target()` before directing a write
        # anywhere — a bare `SUPERTOOL_REPO` read cannot make that
        # distinction, since an inherited value looks identical.
        os.environ["SUPERTOOL_REPO_FROM_OP"] = "1"
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

    # #678 — a preset/custom op declines outright under a mix. #1942 widened
    # that to every built-in the "writes" chokepoint above gates too, so what
    # is left running to completion here on a bare warning is read-only:
    # `read`, `grep` and the rest of `_PARALLEL_SAFE_OPS`, which answer from
    # the core that was invoked and are not of unknown origin. Say so once, on
    # stderr, so the operator still knows which tree's config resolved.
    _mixed_call = _mixed_tree_pair()
    if _mixed_call is not None:
        sys.stderr.write(f"supertool: {_mixed_tree_note(_mixed_call)}\n")

    # #2122 — validate every member's op NAME before any of them runs, and
    # refuse the whole call if one does not route. Position is load-bearing
    # in BOTH directions. It sits after the two stderr blocks above, not
    # before them: a config that failed to parse drops every custom op it
    # declared, so refusing here without those warnings having been printed
    # would answer "unknown operation: mycustomop" about an op the caller
    # really did define — the tool's own absence read as an absence in the
    # world, which is the defect class this repository keeps having. It sits
    # before everything below, because the byte after this is op output and
    # the expensive earlier member this exists to stop is the first of them.
    _prevalidation_error = _batch_prevalidation_refusal(argv)
    if _prevalidation_error is not None:
        sys.stderr.write(_prevalidation_error)
        return 1

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
    _unclean_at_entry = len(_UNCLEAN_VALUE_EXITS)

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
    global _DEFER_FORMATTERS, _FORMAT_QUEUE, _VALIDATOR_DEFER_QUEUE, _VALIDATOR_DEFER_SEEN  # noqa: F821
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
        drain_out = _drain_format_queue()  # noqa: F821
        if drain_out:
            sys.stdout.write(drain_out)
            total_out_bytes += len(drain_out.encode("utf-8"))
        validator_drain_out = _drain_validator_queue()  # noqa: F821
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

    # A value-exit answer that is not clear-to-proceed (#1705). Not a refusal —
    # the op rendered, `refused` does not count it and the receipt says PASS —
    # but `0` has to keep meaning "nothing to worry about" or the exit channel
    # carries no fail-safe at all, and `supertool 'git-worktrees:P' && rm -rf P`
    # was a consumer of exactly that (#1282).
    _unclean_values = _UNCLEAN_VALUE_EXITS[_unclean_at_entry:]
    del _UNCLEAN_VALUE_EXITS[_unclean_at_entry:]
    if _unclean_values:
        any_failure = True

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
    _other = _other_causes_phrase(counter_failure, _unclean_values)
    if len(bodies) > 1 and any_failure:
        if (counter_failure or _unclean_values) and refused:
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
                f"answer. Exit 1 is {_other} (above), not an op refusing."
                + chr(10)
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


def _other_causes_phrase(counter_failure: bool, unclean_values: List[str]) -> str:
    """Why exit 1, for the batch footer, naming only what this call actually had.

    Two causes reach the exit code without being a refusal: the counters
    (`_SKIP_COUNT`, `_ROLLBACK_COUNT`, `_NOT_CHECKED`) and a value-exit answer
    the op does not declare clear to proceed (#1705).

    Joined with `; ` and not `and`. The counter cause is itself an `A, B or C`
    list of possibilities, so `… a validator that could not run and an answer
    its op does not declare clear to proceed (…)` leaves a reader unable to tell
    whether the last clause is a fourth alternative inside the `or` or a second,
    separate cause — on the one branch whose whole job is to say the least.

    With no cause at all the counter enumeration is returned unchanged: the
    caller only reads this when the exit code is already 1, and the three
    possibilities are what that sentence has always offered.
    """
    causes = []
    if counter_failure:
        causes.append("a skipped write, a rolled-back edit or a validator that "
                      "could not run")
    if unclean_values:
        # Named, not enumerated as possibilities: this one the call knows.
        # De-duplicated so three calls to one op read as one cause rather than
        # three, while two different ops still both get named.
        causes.append("an answer its op does not declare clear to proceed ("
                      + ", ".join(dict.fromkeys(unclean_values)) + ")")
    if not causes:
        return ("a skipped write, a rolled-back edit or a validator that could "
                "not run")
    return "; ".join(causes)


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
