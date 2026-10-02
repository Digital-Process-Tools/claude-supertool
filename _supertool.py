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
import signal
import socket
import subprocess
import sys
import tempfile
import threading
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


def _onboarding_text(config_key: str, env_var: str, default: str) -> str:
    """env var, else .supertool.json[config_key], else `default`.

    Same env-over-config-over-built-in convention as `_DEFAULT_COAUTHOR`
    (presets/git/commit.py). Whichever layer wins, a value in
    `_ONBOARDING_DISABLE_VALUES` (case-insensitive, stripped) renders as ""
    so the caller can still print the old "not configured" line -- a
    default that cannot be turned off is worse than none for a project that
    has deliberately kept its session preamble bare (#2342).
    """
    config = _load_config()
    raw = os.environ.get(env_var)
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
        "introduction", "SUPERTOOL_INTRODUCTION", _DEFAULT_INTRODUCTION)
    if not intro:
        return "No introduction configured in .supertool.json\n"
    return str(intro) + "\n\n"


def op_output_format() -> str:
    """Output format examples: env override, else .supertool.json's
    `output-format` key, else a shipped default (#2342)."""
    fmt = _onboarding_text(
        "output-format", "SUPERTOOL_OUTPUT_FORMAT", _DEFAULT_OUTPUT_FORMAT)
    if not fmt:
        return "No output-format configured in .supertool.json\n"
    return str(fmt) + "\n\n"


def op_version() -> str:
    """Output the supertool version."""
    return f"supertool {VERSION}\n"


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
                + f"  It is a valid operation — `ops:roster` lists every name "
                f"loaded here, and the op's own error teaches its "
                f"signature.\n")
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


#: Shared protocol with any `resolve`-class command in this tree (today only
#: `validators/common/ci_lint_resolve_root.py`, #2177): such a command can
#: decline to look ("git absent", "git timed out", "not a repo") as
#: distinctly as it declines because there is genuinely nothing to resolve.
#: It says so by printing this prefix plus a reason instead of empty stdout,
#: and `_validator_resolve` below hands the whole line back rather than
#: folding it into the same "skip, no target" silence as an empty resolve.
#:
#: A second, independently-typed literal rather than an import of the
#: producer's own `RESOLVE_ERROR_PREFIX` -- the core cannot reach into
#: `validators/common`, same trade as `_validator_required`'s duplication of
#: `refusal.required()` (`tests/test_require_validators_core_975.py`).
#: `#2229`: `tests/test_resolve_error_prefix_pinned_2229.py` pins this
#: literal equal to the producer's and drives the real subprocess end to
#: end, so a spelling drift here goes red there rather than silently
#: turning every resolve error into a bogus resolved path downstream.
_VALIDATOR_RESOLVE_ERROR_PREFIX = "RESOLVE-ERROR: "


def _validator_resolve(spec: Dict[str, Any], file: str) -> Optional[str]:
    """Run optional `resolve` cmd to map source→target (e.g. source→test).

    Returns the resolved path, original file if no resolve cmd, None if the
    resolve cmd succeeded but returned empty (signal: skip this validator),
    or a string prefixed `_VALIDATOR_RESOLVE_ERROR_PREFIX` when the resolve
    cmd could not be trusted at all -- either it printed the shared
    `RESOLVE-ERROR: ` protocol above, or (#2174) it crashed and its
    `guard_main` net published a JSON receipt on stdout with exit 0, which
    this caller -- unlike an adapter's own reader -- would otherwise trust as
    a resolved path verbatim.
    """
    if "resolve" not in spec:
        return file
    import subprocess
    # argv-form (shell=False): shell metachars in spec["resolve"] are literal
    # tokens. {file} is still shlex.quote'd so values with spaces survive
    # shlex.split. {supertool_dir} is a known constant.
    # Shielded (#1734): `{file}` is a path the CALLER named, so a segment
    # reading `$HOME` or `$USER` was expanded here too — and an expansion
    # carrying a space or a quote shatters the `shlex.quote` applied to it
    # below, handing `shlex.split` extra argv tokens and resolving against a
    # path nobody named. Same fix, same reasoning, as the preset dispatch site.
    cmd, _shield = _shield_substitute(spec["resolve"], {
        "supertool_dir": _INSTALL_DIR,
        "python": _python_token(),
        "file": shlex.quote(file),
    })
    _prefix_env, cmd = _extract_env_prefix(cmd)
    _prefix_env = {k: _unshield_env_value(v, _shield) for k, v in _prefix_env.items()}
    _merged_env = {**os.environ, **_prefix_env}
    cmd = _unshield(_expand_env(cmd, _merged_env), _shield)
    # Pass merged env to child so prefix vars actually reach the subprocess.
    _run_env = _merged_env if _prefix_env else None
    try:
        r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, timeout=30,
                           env=_run_env, encoding="utf-8", errors="replace")
        resolved = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    except subprocess.TimeoutExpired:
        # #2177, one call frame up from `ci_lint_resolve_root.py`'s own fix:
        # the resolve COMMAND timing out is a "could not look" case exactly
        # like the ones that command's own `_repo_root` now distinguishes
        # from "looked, found nothing" -- and bare `None` here would fold it
        # back into that same silence one layer higher, undoing the point.
        return _VALIDATOR_RESOLVE_ERROR_PREFIX + "resolve command timed out"
    except OSError as exc:
        # Same reasoning for a resolve command that could not even be spawned
        # (a `.supertool.json` `resolve` entry naming a binary that is not on
        # PATH, most commonly) -- `FileNotFoundError` is an `OSError` subclass
        # and is not distinguished further here because the caller already
        # gets the exception text.
        return _VALIDATOR_RESOLVE_ERROR_PREFIX + "resolve command could not be run: {0}".format(exc)
    if not resolved:
        return None
    if resolved.startswith(_VALIDATOR_RESOLVE_ERROR_PREFIX):
        return resolved
    # #2174: a resolve command need not spell the protocol above to still be
    # untrustworthy -- one that crashes and routes through `guard_main`
    # publishes a JSON receipt (`{"tool": ..., "ok": false, "errors": [...]}`)
    # on stdout with exit 0, and this caller reads only the first stdout
    # line, never `returncode`. A resolved path is a filesystem path and can
    # never parse as a dict carrying both "tool" and "ok", so that shape is
    # enough to recognize the receipt without depending on the returncode
    # this contract does not distinguish on.
    try:
        _parsed = json.loads(resolved)
    except (ValueError, TypeError):
        _parsed = None
    if isinstance(_parsed, dict) and "tool" in _parsed and "ok" in _parsed:
        _errors = _parsed.get("errors") or []
        _reason = "resolve command crashed"
        if _errors and isinstance(_errors[0], dict) and _errors[0].get("msg"):
            _reason = str(_errors[0]["msg"])
        return _VALIDATOR_RESOLVE_ERROR_PREFIX + _reason
    return resolved


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

    fingerprint = hashlib.sha256("\x00".join(sorted(parts)).encode("utf-8")).hexdigest()
    _VALIDATOR_FINGERPRINT_CACHE[cache_key] = fingerprint
    return fingerprint


_VALIDATOR_MEANING_VERSION: Optional[str] = None
#: The stat identity of `validators/SCHEMA.md` at the moment
#: `_VALIDATOR_MEANING_VERSION` was computed. Companion to the memo above, not
#: a cache of its own: it is what lets a reused process notice its own memo has
#: gone stale (#1110) without falling back to the blunt "hash every call" cost
#: #1044 rejected. `None` doubles as "no memo yet" and "the file was
#: unreadable at that time", which `_stat_signature` also returns for a
#: missing file -- both cases recompute below, which is the safe direction.
_VALIDATOR_MEANING_VERSION_STAT: Optional[str] = None


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

    Memoised, and the memo is checked against `SCHEMA.md`'s own `stat` identity
    on every call (#1110) -- so the expensive re-hash only happens the one time
    a call notices the file changed underneath the process, not on every cache
    lookup, and not never. Two `stat` calls per lookup is the same trade
    `_mixed_tree_pair` already makes for the same reason: a cached verdict is
    one more thing to go stale in a reused daemon process (#680).
    """
    global _VALIDATOR_MEANING_VERSION, _VALIDATOR_MEANING_VERSION_STAT
    schema_path = os.path.join(_INSTALL_DIR, "validators", "SCHEMA.md")
    current_stat = _stat_signature(schema_path)
    if (_VALIDATOR_MEANING_VERSION is not None
            and current_stat == _VALIDATOR_MEANING_VERSION_STAT):
        return _VALIDATOR_MEANING_VERSION
    h = hashlib.sha256()
    try:
        with open(schema_path, "rb") as f:
            h.update(f.read())
    except OSError:
        h.update(b"schema-unreadable")
    h.update(b"\x00" + "\x00".join(
        sorted(_VALIDATOR_CORE_ONLY_KEYS)).encode("utf-8"))
    _VALIDATOR_MEANING_VERSION = h.hexdigest()[:16]
    _VALIDATOR_MEANING_VERSION_STAT = current_stat
    return _VALIDATOR_MEANING_VERSION


def _validator_cache_key(file_path: str, name: str, cmd: str,
                         spec: Optional[Dict[str, Any]] = None) -> Optional[str]:
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


#: The two conventions an adapter may use for `count`, per validators/SCHEMA.md.
#: `total` — `count` counts every row `errors` carries, `adapter` stall rows
#: included. `measured` — `count` already excludes them.
_VALIDATOR_COUNT_BASES = frozenset({"total", "measured"})


def _validator_count_contract_fault(data: Any) -> Optional[str]:
    """Does this payload's declared count convention contradict its own rows? (#1728)

    `_validator_measured_count` is `max(count - absences, len(rows) - absences,
    0)`, and the floor exists for the adapter whose `count` already excludes its
    stall rows. Recomputing from `errors` instead would break the adapter that
    caps `errors` — the fix #1717's reviewer proposed, the author refused, and
    `test_a_capped_error_list_is_not_read_as_a_smaller_count` pins.

    Neither arithmetic covers the payload whose **`count` is bounded by the same
    cap that bounds `errors`**. Both terms saturate, before and after compare
    equal, `_validator_regressed` returns False, and a `rollback_on_fail`
    validator does not revert over a genuinely new finding. Measured on master:

        before  count=4  errors=[f1, f2, f3, f4, stall]  -> 4
        after   count=4  errors=[f5, f2, f3, f4, stall]  -> 4

    **No arithmetic can separate those two payloads, because a cap is invisible
    in a single payload unless the adapter declares it.** So this is not an
    arithmetic fix. `count_basis` and `errors_truncated` are the declaration,
    and this is the guard: a declaration that contradicts the rows printed under
    it is a fault about the **adapter**, never a comparison nobody can read.

    Returned as a message rather than raised, and applied at the ingest
    chokepoint as an `adapter`-coded row — the door `refusal.crashed()` already
    uses. That makes the result a non-verdict: rendered `NOT CHECKED`, never
    subtracted from a baseline, never a rollback, and still loud, because
    `_NOT_CHECKED` growing fails the call. `skipped` would be quieter than the
    defect it reports, which is the trade `validators/common/refusal.py`
    declines by name.

    **Undeclared is not a fault**, and the reason is the population rather than
    politeness: any repo may name its own validator in `.supertool.json`, so a
    runtime mandate would break every third-party adapter on upgrade for a
    shape none of them has been shown to have. An undeclared payload keeps the
    heuristic untouched. The mandate over the *shipped* tree is
    `tests/test_count_basis_contract_1728.py::_GRANDFATHERED`, a set that may
    only shrink — the `_UNDECLARED_PATH_OPS` pattern.

    **Declaring changes no number for a conforming payload.** For a complete
    list the floor already produces the declared answer, and for a truncated one
    `count` already dominates it. The declaration buys the guard, not the
    arithmetic, which is why no shipped adapter's counts move when it starts
    declaring — pinned by `test_declaring_changes_no_measurement`.

    What is checkable from one payload, and what is not, stated rather than
    implied: a `measured` declaration that is a **lie** — an adapter that caps
    and says it does not — is indistinguishable here from a well-formed
    `measured` payload, because saturation makes `count` equal the visible
    findings by construction. That residue is why the declaration is a contract
    an author states and not a property the core infers.
    """
    if not isinstance(data, dict) or "skipped" in data:
        return None
    basis = data.get("count_basis")
    trunc = data.get("errors_truncated")
    if basis is None and trunc is None:
        return None
    tool = _flat_cell(str(data.get("tool") or "adapter"), 40)
    if basis is None or trunc is None:
        return (f"{tool} declared half the count contract (count_basis="
                f"{basis!r}, errors_truncated={trunc!r}) - both keys or "
                f"neither, per validators/SCHEMA.md. One alone leaves the "
                f"question the pair exists to force unanswered while looking "
                f"answered")
    if not isinstance(basis, str) or basis not in _VALIDATOR_COUNT_BASES:
        return (f"{tool} declared count_basis={basis!r}, which is not one of "
                f"{sorted(_VALIDATOR_COUNT_BASES)} - see validators/SCHEMA.md")
    if not isinstance(trunc, bool):
        return (f"{tool} declared errors_truncated={trunc!r}, which is not a "
                f"boolean")
    count = data.get("count", 0)
    if isinstance(count, bool) or not isinstance(count, (int, float)):
        return (f"{tool} declared count_basis={basis!r} and published "
                f"count={count!r}, which is not a number - an undeclared "
                f"payload may read as 0 rather than raise mid-edit, a declared "
                f"one is an adapter contradicting its own statement")
    rows = [e for e in (data.get("errors") or []) if isinstance(e, dict)]
    absences = sum(1 for e in rows if (e.get("code") or "") == "adapter")
    if trunc and basis == "measured":
        return (f"{tool} declared count_basis='measured' with "
                f"errors_truncated=true. An adapter may pre-subtract its "
                f"adapter rows from count OR cap errors, never both: both "
                f"terms of the measured count then saturate at the cap, before "
                f"and after compare equal, and rollback_on_fail goes inert "
                f"over a new finding (#1728)")
    if trunc:
        if count <= len(rows):
            return (f"{tool} declared errors_truncated=true and published "
                    f"count={count} over {len(rows)} row(s). A truncated list "
                    f"dropped findings, so count must exceed the rows it "
                    f"printed; a count bounded by the same cap that bounds "
                    f"errors leaves the rollback gate inert (#1728)")
        return None
    visible = len(rows) if basis == "total" else len(rows) - absences
    if count < visible:
        return (f"{tool} declared count_basis={basis!r} with "
                f"errors_truncated=false and published count={count} under "
                f"{visible} row(s) it says it counts. A complete list cannot "
                f"hold more findings than count counted")
    return None


def _validator_count_contract_reply(name: str, target: str, fault: str,
                                    data: Dict[str, Any]) -> Dict[str, Any]:
    """The payload a contract violation is published as (#1728).

    Shaped exactly like `refusal.crashed()`, because it is the same fact one
    layer up: the adapter said something about this file that cannot be read,
    so there is no verdict here and someone has to fix the adapter. The
    adapter's own rows are deliberately **not** carried through — a payload
    whose count contradicts its rows gives no reason to trust either half, and
    printing findings beside a fault would invite exactly the comparison this
    refuses to make. The message names the numbers, so nothing is invisible.

    `duration_ms` survives when it is a number: it describes the attempt, not
    the verdict, and `tests/_adapter_verdict.stalled_at_its_own_wall` reads it.
    """
    dur = data.get("duration_ms", 0)
    if isinstance(dur, bool) or not isinstance(dur, (int, float)):
        dur = 0
    return {"tool": name, "file": target, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": _flat_cell(fault, 1000)}],
            "duration_ms": dur}


def _validator_apply_count_contract(data: Dict[str, Any], name: str,
                                    target: str) -> Dict[str, Any]:
    """Ingest guard: measure a payload, or say why it cannot be measured (#1728).

    Beside `_validator_strip_core_keys` at both doors — the fresh parse and the
    cache read — for the reason #1044 gives: a cache entry is an adapter payload
    that outlived the run which parsed it, and a guard on one door is a guard
    with a door beside it.
    """
    fault = _validator_count_contract_fault(data)
    if fault is None:
        return data
    return _validator_count_contract_reply(name, target, fault, data)


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
    import time
    _t0_resolve = time.monotonic()
    target = _validator_resolve(spec, file)
    if target is None:
        return {"tool": name, "skipped": "no target resolved"}
    if target.startswith(_VALIDATOR_RESOLVE_ERROR_PREFIX):
        # #2177: distinct from "no target resolved" above -- this is "the
        # resolve command could not even look", not "it looked and found
        # nothing".
        #
        # #2185: and distinct from an ordinary skip in a second way -- routed
        # through `_validator_unusable_reply` rather than a bare
        # `{"skipped": ...}` dict, so it picks up the `no_verdict` marker the
        # other four core-detected "this validator is too broken to answer
        # for itself" cases already carry (produced no output, replied
        # without a verdict, could not be spawned, replied with non-JSON --
        # see that function's docstring). Before this it was invisible to
        # `_validator_gate_did_not_run`, so `$SUPERTOOL_REQUIRE_VALIDATORS`
        # exited 0 over an edit whose gate never ran: git absent, git timed
        # out, not a repo, the resolve command itself unspawnable, or its own
        # `guard_main` crash receipt all read as an ordinary "nothing to
        # check here" skip.
        #
        # Deliberately NOT escalated all the way to the unconditional
        # `NOT CHECKED` a self-reported adapter crash gets (`code: "adapter"`,
        # read by `_validator_no_verdict` with no gate check at all) --
        # that channel is for an adapter healthy enough to report on its own
        # crash (#967); this is the core inferring breakage from a subprocess
        # that did not behave, exactly the class `_validator_unusable_reply`
        # already treats more conservatively, gated behind an explicit
        # `$SUPERTOOL_REQUIRE_VALIDATORS` rather than crying wolf on every
        # ordinary edit (`_validator_gate_did_not_run`'s own docstring, and
        # #665/#975).
        return _validator_unusable_reply(
            name, file,
            "could not resolve its target: {0}".format(
                target[len(_VALIDATOR_RESOLVE_ERROR_PREFIX):]),
            _elapsed_since(_t0_resolve))
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
    # Shielded (#1734) — `{file}` is caller-named data. See _shield_substitute.
    cmd, _shield = _shield_substitute(spec["cmd"], {
        "supertool_dir": _INSTALL_DIR,
        "python": _python_token(),
        "file": shlex.quote(target),
    })
    # Lift leading `KEY=VAL` shell env-prefix into env dict (shipped cmd
    # templates use this to set MCP_*_WORKING_DIR before the python invocation).
    _prefix_env, cmd = _extract_env_prefix(cmd)
    _prefix_env = {k: _unshield_env_value(v, _shield) for k, v in _prefix_env.items()}
    # $VAR / ${VAR} expansion + child env both need spec.env + prefix env.
    _spec_env_dict = {**_prefix_env, **(spec.get("env") or {})}
    _merged_env = {**os.environ, **{str(k): str(v) for k, v in _spec_env_dict.items()}}
    cmd = _unshield(_expand_env(cmd, _merged_env), _shield)
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
                # #1728: the same payload, so the same contract. A declaration
                # that contradicts its own rows is a fault about the adapter,
                # and a cached one is a fault replayed until the file changes.
                cached = _validator_apply_count_contract(cached, name, target)
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
    # #2228: always set, never conditionally -- an adapter that reads this to
    # bound what it will import and execute must be able to tell "no config
    # loaded" (empty string) from "not told at all" (var absent, meaning this
    # adapter was invoked outside supertool's own wiring entirely).
    run_env[_VALIDATOR_CONFIG_DIR_ENV] = (
        os.path.dirname(os.path.realpath(_CONFIG_PATH)) if _CONFIG_PATH else ""
    )

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
        # #1728: and the payload's own count convention is held to what it
        # declared, before any arithmetic reads it.
        data = _validator_apply_count_contract(data, name, target)
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


def _quote_open_guess_line(e: Dict[str, Any]) -> Optional[str]:
    """One short receipt line for `quote_open_guess` (#1810), or None.

    Unlike `source_context`/`context_unavailable` -- several lines, shown only
    in `verbose` mode -- this is a single short line, so it is not gated the
    same way: the incident it exists for is read from the receipt an edit's
    own rollback prints, which never carries `verbose` at all
    (`_validator_render_diff` takes no such parameter). Gating it behind an
    explicit `validate:PATH:verbose` re-run would build the hint and then hide
    it from the one moment it is for.

    Adapter-supplied text, `_flat_cell`-ed like every other string these
    renderers put on a line of its own (#895).
    """
    guess = e.get("quote_open_guess")
    if not isinstance(guess, dict):
        return None
    gl = guess.get("line")
    if gl is None:
        return None
    note = _flat_cell(str(guess.get("note") or ""), 100)
    return f"      ↳ maybe opened at L{gl}" + (f": {note}" if note else "")


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
            code = _flat_cell(e.get("code") or "")
            msg = _flat_cell(e.get("msg") or "")
            out.append(f"  {line_n} {code}  {msg}")
            guess_line = _quote_open_guess_line(e)
            if guess_line:
                out.append(guess_line)
            for ctx_line in (e.get("source_context") or []):
                out.append(f"    {_flat_cell(ctx_line)}")
            # An empty `source_context` used to mean either "no lines to show"
            # or "the file could not be opened" (#1446). The finding stands
            # either way — the tool located a defect and that claim does not
            # depend on reprinting the line — so the reason is rendered beside
            # it rather than swallowed, and flattened like every other
            # adapter-supplied string that gets a line of its own.
            unavailable = e.get("context_unavailable")
            if unavailable:
                out.append(f"    [no source context: {_flat_cell(unavailable)}]")
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
            code = _flat_cell(e.get("code") or "")
            msg = _flat_cell(e.get("msg") or "", 120)
            out.append(f"  {line_n} {code}  {msg}")
            guess_line = _quote_open_guess_line(e)
            if guess_line:
                out.append(guess_line)
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


def _validator_measured_count(data: Optional[Dict[str, Any]]) -> int:
    """`count`, minus the rows that measured nothing. The only count to subtract.

    SCHEMA.md gives an `adapter` row `ok: false, count: 1` because something is
    broken that someone has to fix — and then promises, per result, that the
    core "never subtracts it from a baseline in either direction and never
    reverts an edit over it" (#969). Until #1717 that promise was delivered
    through `_validator_not_checked`, whose test is `all(code == "adapter")`,
    so it held only for a payload that was *entirely* absence.

    A mixed payload — real findings beside a row the checker could not finish —
    fails that `all()`, and rightly: the file WAS measured, and rendering it
    `NOT CHECKED` would hide four real findings behind one stall. But the guard
    was also the rollback guard, so the same payload fell through to arithmetic
    that read the absence as a finding: before 1, after 2, delta +1, and on a
    `rollback_on_fail` validator a correct edit was written back to its pre-edit
    bytes. `cargo-check` ships this shape on master — a crate diagnostic naming
    another file keeps its text and takes `code: "adapter"` (#754).

    Two questions were sharing one predicate. This is the second one, and it is
    per row where the first is per payload: *did this row measure the file?*

    `orchestrator` is deliberately not subtracted here, for the same reason
    `_validator_not_checked` excludes it. The core's own timeout arrives as a
    whole fabricated payload carrying `timeout: True`, which `_validator_no_verdict`
    catches before any arithmetic runs, so a core `orchestrator` row can never
    reach this function beside a finding. One that does is an adapter writing
    the core's provenance code into its own payload, and honouring it would hand
    an adapter the rollback bypass #1036 closed at the other door.

    A `count` that is not a number is not a measurement either; it reads as 0
    rather than raising in the middle of an edit. Nothing is excused by that:
    an `ok: false` result against a clean baseline still regresses on `ok`.

    **The subtraction may correct a count and may never contradict the rows
    under it**, so the floor is the number of non-`adapter` rows visible in
    `errors`. `count` and `errors` have independent sources — twenty adapters
    write `count = len(errors)` and cargo-check is one of them, but `phpstan`
    takes `count` from `totals.file_errors` while building `errors` from a
    different key of the same document. An adapter whose `count` already
    excludes its own stall rows would be subtracted from twice, collapse to
    zero on both sides, and the gate would go inert over a real new finding —
    the louder half of this defect, and the one a fix aimed only at the quieter
    half walks straight into. Recomputing from `errors` instead of subtracting
    from `count` fails the other way: an adapter that caps its `errors` list
    would have fifty findings read as five.
    """
    if not isinstance(data, dict):
        return 0
    count = data.get("count", 0)
    if isinstance(count, bool) or not isinstance(count, (int, float)):
        return 0
    rows = [e for e in (data.get("errors") or []) if isinstance(e, dict)]
    absences = sum(1 for e in rows if (e.get("code") or "") == "adapter")
    if not absences:
        return count
    return max(count - absences, len(rows) - absences, 0)


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

    **A crash is no longer one of the five (#1697).** Every adapter now wraps
    its `main` in `refusal.guard_main`, so an escaping exception arrives as the
    adapter's own `code: "adapter"` row — a self-report of the #967 kind, which
    escalates through `_validator_not_checked` whatever this function answers.
    The count is left as it was because it is #975's, not today's; what changed
    is which door that one walks through.
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

    Five ways it is not landed elsewhere and exited 0 when #975 was written.
    Four routed into `_validator_unusable_reply` and became a `skipped`; the
    fifth is the core's own `TimeoutExpired` arm, which renders
    `1 err (timeout)`. **Four remain**: #1697 gave every adapter a crash net,
    so a crash is self-reported as an `adapter` error and never reaches
    `_validator_unusable_reply` at all — it escalates unconditionally now,
    through the row above rather than through this function. In all of them the
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

    That guard is per *payload*, and it has to be — `_validator_not_checked`
    answers a rendering question where `all()` is correct. The rollback question
    is per *row*, so a payload mixing real findings with one stall row walked
    past the guard and reverted a correct edit anyway. The counts subtracted
    below come from `_validator_measured_count`, which is that second question
    (#1717).
    """
    if "skipped" in after:
        return False
    if _validator_no_verdict(after) is not None:
        return False
    if after.get("ok", False):
        return False
    before = _validator_baseline(before)
    b_count = _validator_measured_count(before) if before else 0
    a_count = _validator_measured_count(after)
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
    # The same subtraction the marker is computed from, so the arrow and the
    # marker cannot disagree about the row they share (#1717). Raw counts made
    # a mixed payload print `1 → 2 (+1)` beside a ✓ — one new error, and
    # nothing wrong — and the "new" row was the schema's channel for an
    # absence. The rows themselves are still listed below with their codes, so
    # the stall is on the page; it is only out of the arithmetic.
    b_count = _validator_measured_count(before) if before else 0
    a_count = _validator_measured_count(after)
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
                code = _flat_cell(e.get("code") or "")
                msg = _flat_cell(e.get("msg") or "", 120)
                out.append(f"  {line_n} {code}  {msg}")
                guess_line = _quote_open_guess_line(e)
                if guess_line:
                    out.append(guess_line)
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
        #
        # And a fourth, per row (#1717): a stall row present only in the after
        # payload is `new` by message comparison, so a checker that could not
        # finish printed as something this edit did, beside a delta that
        # deliberately does not count it. It is still listed — hiding it is the
        # thing this whole mechanism refuses — it is just no longer claimed.
        bullet = " " if b_unknown else "+"
        for e in new[:5]:
            line_n = f"L{e['line']}" if e.get("line") else "  "
            code = _flat_cell(e.get("code") or "")
            msg = _flat_cell(e.get("msg") or "", 120)
            out.append(f"  {' ' if code == 'adapter' else bullet} "
                       f"{line_n} {code}  {msg}")
            guess_line = _quote_open_guess_line(e)
            if guess_line:
                out.append(guess_line)
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
    # Shielded (#1734) — `{file}` is caller-named data. See _shield_substitute.
    cmd, _shield = _shield_substitute(spec["cmd"], {
        "supertool_dir": _INSTALL_DIR,
        "python": _python_token(),
        "file": shlex.quote(file),
    })
    _prefix_env, cmd = _extract_env_prefix(cmd)
    _prefix_env = {k: _unshield_env_value(v, _shield) for k, v in _prefix_env.items()}
    _spec_env_dict = {**_prefix_env, **(spec.get("env") or {})}
    _merged_env = {**os.environ, **{str(k): str(v) for k, v in _spec_env_dict.items()}}
    cmd = _unshield(_expand_env(cmd, _merged_env), _shield)
    timeout = int(spec.get("timeout", 30))
    # #2228, self-review (reviewer finding): a `.supertool.json` "formatters"
    # block can name the exact same `cmd` as a "validators" one -- nothing
    # stops it, and `new-file-lint.py` / `changelog-fragment.py` read
    # `SUPERTOOL_CONFIG_DIR` regardless of which block wired them. Stamped
    # unconditionally here too, the same as `_validator_run_one`, so the
    # trust boundary is not something a validator-vs-formatter choice could
    # bypass.
    run_env = dict(_merged_env)
    run_env[_VALIDATOR_CONFIG_DIR_ENV] = (
        os.path.dirname(os.path.realpath(_CONFIG_PATH)) if _CONFIG_PATH else ""
    )
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
    Failures always produce a row. So does a `verify_failed` payload (#2162):
    it carries the same ok=True, 0/0-metrics shape as a genuine no-op, and
    without this exception it would go silent for the wrong reason -- the
    tool ran and may have changed the file, only the post-run re-read that
    would have proven it failed.
    """
    # Every adapter-supplied field on this row goes through `_flat_cell`, for
    # the reason in its docstring: the validator twin has routed `tool`,
    # `skipped` and `msg` since #895 and this formatter twin never adopted it,
    # so a name or a message carrying any of the ten separators
    # `str.splitlines()` breaks on wrote its own row at column 0 (#1522).
    name = _flat_cell(result.get("name") or result.get("tool") or "?")
    ok = result.get("ok", False)
    dur = result.get("duration_ms", 0)
    metrics = result.get("metrics") or {}
    added = metrics.get("lines_added", 0)
    removed = metrics.get("lines_removed", 0)
    # `verify_failed` (#2162): a formatter that ran and then hit an `OSError`
    # re-reading the file to compute the diff reports `metrics.lines_added`
    # and `lines_removed` as 0 either way -- identical to a genuine no-op.
    # Silently dropping this row would say "nothing changed" about a file
    # that may well have. Never silent, regardless of `ok`.
    verify_failed = result.get("verify_failed")

    # Legacy non-JSON adapter: show raw output verbatim (can't compute metrics).
    # Silent only when the formatter ran cleanly AND printed nothing.
    if "raw" in result:
        raw = (result.get("raw") or "").strip()
        if ok and not raw:
            return None  # quiet clean run
        status = "ok" if ok else "fail"
        if raw:
            # `raw` is a *block* — the reader asked for a legacy adapter's
            # output verbatim, and flattening an eslint report into one
            # 2000-character line answers the column-0 problem by destroying
            # the thing it protects. So it keeps its lines and every line after
            # the first is indented under the row, which is the same answer
            # `op_validate_staged` gives a validator block. Only the *fields*
            # (`name`, `msg`) are flattened.
            body = "\n             ".join(
                _flat_field(ln) for ln in _LINE_BREAK_RE_STR.split(raw))
            return f"{name:8s}: {status}       {body}"
        return f"{name:8s}: {status}"

    if verify_failed:
        detail = _flat_cell(str(verify_failed), 120)
        status = "ok" if ok else "fail"
        return (f"{name:8s}: {status}       ({dur}ms)  formatted, but could "
                f"not verify what changed: {detail}")

    if ok and added == 0 and removed == 0:
        return None  # silent no-op

    if ok:
        line = f"{name:8s}: ok         ({dur}ms) +{added} -{removed}"
        # #2405: a same-session `around_line` read taken before this write
        # can be stale the moment the formatter touches lines the caller
        # never asked it to -- unrelated to the edit that triggered the
        # write. Naming the before-file span here (present only on the
        # four SCHEMA adapters that compute it) is the cheapest place a
        # caller (or the harness) can learn to re-read before reusing that
        # earlier read as a later `edit`'s `old` string.
        first = metrics.get("first_changed_line")
        last = metrics.get("last_changed_line")
        if first is not None and last is not None:
            span = f"line {first}" if first == last else f"lines {first}-{last}"
            line += (f"  ({span} touched -- an earlier read of that region "
                     f"is now stale, re-read before reusing it)")
    else:
        errors = result.get("errors") or []
        msg = result.get("msg") or (errors[0].get("msg") if errors else "") or "failed"
        # `_flat_cell(…, 120)` rather than `str(msg)[:120]`: the slice cut with
        # no marker, so a message that ended there and one that was cut read
        # alike — the same pair `_flat_cell` separates for every other row.
        msg = _flat_cell(msg, 120)
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


_ADVICE_DEFAULT_OPS = ("edit", "paste", "append", "replace", "replace_lines", "vim", "json-set")


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
    # Shielded (#1734) — `{file}` is caller-named data. See _shield_substitute.
    cmd, _shield = _shield_substitute(resolve_cmd, {
        "supertool_dir": _INSTALL_DIR,
        "python": _python_token(),
        "file": shlex.quote(path),
    })
    _prefix_env, cmd = _extract_env_prefix(cmd)
    _prefix_env = {k: _unshield_env_value(v, _shield) for k, v in _prefix_env.items()}
    _merged_env = {**os.environ, **_prefix_env}
    cmd = _unshield(_expand_env(cmd, _merged_env), _shield)
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


def _write_target_pinned(path: str, target: str, fn: "Any") -> Any:
    """Run *fn* -- a `do_op` call -- with `path`'s write target pinned.

    #1147: `_run_with_validators` samples `_target = _write_target(path)`
    once, before the op runs, and every rollback arm below reuses that exact
    sample. `_atomic_write` did not -- it re-derived `real_path =
    _write_target(path)` at write time, from `path` alone. #1136 made the two
    resolutions agree in the ordinary case; it did not remove having two of
    them. If `path` is retargeted between the sample and the write (or turns
    into a symlink having not been one when sampled), the writer and the
    rollback can act on different objects again, through the narrower window
    #1136 left open.

    One resolution: `_atomic_write` consults this pin, keyed on the same
    `path` string `_run_with_validators` sampled against, before it asks
    `_write_target` a second time. Cleared in `finally` so a pin from one
    `do_op()` call never leaks into an unrelated later write on the same
    thread -- `op_replace`'s own multi-file loop calls `_atomic_write` on
    each matched file, none of which equals the single `path` pinned here,
    so it falls through to its own per-file resolution unchanged.
    """
    _prev_pin = getattr(_DISPATCH_STATE, "pinned_write_target", None)
    _DISPATCH_STATE.pinned_write_target = (path, target)
    try:
        return fn()
    finally:
        _DISPATCH_STATE.pinned_write_target = _prev_pin


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
    _bump_counter(_MUTATION_ATTEMPTS, "cnt_mutation")
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
    #
    # Displayed via `_write_target_display`, not `_target` itself (#1146):
    # `_write_target`'s `os.path.realpath` canonicalises a symlinked ANCESTOR
    # directory as well as the leaf link, while every OTHER path in this
    # receipt (`path` itself, via `os.path.abspath`) does not — so the same
    # directory was spelled two different ways across one sentence. The
    # functional `_target` below is untouched; only what gets printed changes.
    _target_display = _write_target_display(path)
    _target_cell = _flat_cell(_target_display)
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
        body = _write_target_pinned(path, _target, do_op)
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

    # A file that did not exist has no pre-op state, so there is nothing here
    # for any adapter to measure and the honest baseline is the absence #832
    # already renders as `?` (#1466). Most of the 36 adapters reach that on
    # their own: handed a missing path they emit `code: "adapter"`, which
    # `_validator_not_checked` routes to the same place. Three do not, because
    # "this file does not exist" is a finding in their own vocabulary rather
    # than an adapter fault -- ruff calls it `E902`, `tsc-check` `TS6053`,
    # `prettier-check` `formatting` -- and the core accepted those fabricated
    # `count: 1`s as measurements. `paste` on a new .py file printed
    # `ruff : 1 -> 0 (-1) OK`: an improvement claimed over a file that was not
    # there, next to `py-syntax : ? -> 0` in the same block, the two rows
    # disagreeing about whether the file had a past.
    #
    # Gated here rather than in those three adapters, because the fix in an
    # adapter is one adapter's memory -- the shape #1202 had to undo across
    # sixteen of them -- and the core already knows the answer for all of
    # them: `_pre_existed` is sampled above the op for the rollback arms.
    #
    # The `1` is arithmetic as well as prose. `_validator_regressed` subtracts
    # it, and an invented baseline of 1 against a real post-write finding of 1
    # is equal counts and equal `ok`, so a finding this op introduced into a
    # file it created whole was excused as `(pre-existing -- not from this
    # edit)`. The sign only ever runs that way -- a fabricated baseline is
    # HIGHER than the true zero, so it suppresses regressions and cannot
    # manufacture one -- which is why the DEFECT misreports rather than
    # destroys. Removing it restores the arm, though, which is a behaviour
    # change and not only a display one: for those three a finding on a created
    # file now regresses, so `[left on disk]` fires where it could not before
    # (ruff is the case `_left_on_disk_line` names by name, and was inert for
    # ruff until this) and a `rollback_on_fail` registration would unlink the
    # create. No shipped registration reaches the second -- every
    # `rollback_on_fail` validator in both config files answers a missing file
    # with `code: "adapter"` or `skipped`, so its baseline was already absent
    # -- and `tests/test_new_file_has_no_baseline_1466.py` audits that
    # intersection rather than trusting it.
    before = (_validators_run_batch(applicable, path)
              if applicable and _pre_existed else {})

    body = _write_target_pinned(path, _target, do_op)

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
                                target=_target_display))
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
    # pre-op has no baseline pass at all since #1466 -- and had none that could
    # open it before that -- and a daemon just SIGTERM'd for a new file (#239)
    # comes back cold with the current bytes indexed.
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
                # Not reachable together with #1320's disclosure below —
                # `refuse` implies `_pre_existed` — but set anyway rather than
                # left to a coupling between two functions that a later change
                # to `_rollback_action` would break silently.
                already_undone = True
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
                    created=action == "unlink", target=_target_display) + "\n")
            except OSError as e:
                diff_out += f"\n[ROLLBACK FAILED] {name}: {e}\n"
            already_undone = True
            break

    # A create nothing undid is still in the tree, and until #1320 the only
    # place that said so was the filesystem. Deliberately NOT gated on
    # `needs_rollback`: the case being disclosed is precisely the one where no
    # applicable validator asks for a rollback, so reading the flag would skip
    # every instance of it.
    #
    # `already_undone` covers all three arms above — undone, refused, or an
    # unlink that raised — because each already tells the reader where the file
    # stands, and two markers about one write would contradict each other.
    if applicable and not _pre_existed and not already_undone:
        refused_by = [n for n in applicable
                      if n in after_results
                      and _validator_regressed(before.get(n), after_results[n])]
        # `os.path.exists` asked separately from the finding: the finding is
        # what makes this a refusal, the file being there is what makes it worth
        # saying, and a validator that removed the file itself is neither.
        if refused_by and os.path.exists(_target):
            _note_left_on_disk()
            diff_out += ("\n" + _left_on_disk_line(
                refused_by, path, body, target=_target_display) + "\n")

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
    out = [f"validate: {_flat_field(path, disclose_newline=True)}"]
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
    out.append(f"({line_count} lines, {size} bytes{_read_freshness_note(path)})"
              f"{_path_meta_suffix(path, b''.join(raw_lines[:64]))}\n")
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
            # no-op: show a muted marker in manual mode so the user knows it ran.
            # `_flat_cell` for the same reason `_formatter_render_row` uses it
            # one branch over (#1522): this arm substitutes for that row, and a
            # row that flattens its name on one path and not the other is the
            # inconsistency the fix is about. `result["tool"]` is the only
            # adapter-supplied term here and `_formatter_run_one` overwrites
            # `name` on every arm, so this is the seam, not a live hole.
            name_key = _flat_cell(result.get("name") or result.get("tool") or name)
            dur = result.get("duration_ms", 0)
            row = f"{name_key:8s}: ok (no-op)  ({dur}ms)"
        if verbose:
            row = row + "  [verbose]"
            errors = result.get("errors") or []
            out.append(row)
            for e in errors:
                line_n = f"L{e['line']}" if e.get("line") else "  "
                code = _flat_cell(e.get("code") or "")
                # `_flat_cell`, not `.replace(newline)`: the latter is one
                # separator out of the ten `str.splitlines()` breaks on, and a
                # lone CR returns the cursor to column 0 without making a line
                # at all. Every sibling render already routes through here; this
                # was the outlier, on the verbose path a human reads after a
                # formatter has objected (#1522). No `limit`, because verbose
                # means untruncated.
                msg = _flat_cell(e.get("msg") or "")
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
                            body = _run_with_validators(
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
            # listing this one answers the provenance half of.
            header = ""
            body = op_registry(parts[1] if len(parts) > 1 else "")
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
                elif ops_arg == "session" and op == "ops":
                    body = op_ops_session()
                elif ops_arg == "full" and op == "ops":
                    # What bare `ops` was before #1774 made signatures the
                    # default. Named on the default listing's own footer, with
                    # the byte count it is asking the caller to spend.
                    body = op_ops(full=True)
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
                        body = op_ops_filter(grep_pattern)
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

# #2228: the directory holding the `.supertool.json` this run actually loaded
# (empty when none did), passed to every validator adapter's environment. An
# adapter that imports and executes a script it finds by walking up from the
# edited file (new-file-lint.py, changelog-fragment.py) needs this to tell
# "the project that wired me" from "whatever repo happens to be edited" --
# without it, a maintainer whose own .supertool.json sits above a directory
# of clones has each clone's own conventionally-named CI helper imported (and
# executing an import is executing its top-level code) with the maintainer's
# privileges the moment any .py file inside that clone is edited, well before
# either adapter's own new-file-only check ever runs.
_VALIDATOR_CONFIG_DIR_ENV = "SUPERTOOL_CONFIG_DIR"


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

# CSI sequences, OSC strings (BEL- or ST-terminated) and the two-character
# escapes, stripped out of a child's stderr before it becomes `detail` (#1333).
#
# CPython colourises its own tracebacks from 3.13 on, and `_colorize` consults
# `FORCE_COLOR` before it asks whether the stream is a tty — so a parent that has
# it set (this repo's own agent harness exports `FORCE_COLOR=3`) gets escape
# sequences out of a child whose stderr is a pipe. Two conditions, not a version
# range, which is why the local red here was green on every 3.9-3.12 CI leg.
#
# Stripped here on ingest, in ADDITION to `_disable_force_color_for_children`
# unsetting `FORCE_COLOR` at import (#1429): that one mutation covers every
# `subprocess.run`/`Popen` this process spawns without enumerating layers, so
# the objection this paragraph used to raise against "unsetting the variable"
# no longer applies to a child of THIS process. It stays defence in depth for
# anything this stripped `detail` might still carry -- a child launched by a
# process that never imported this module (a raw `stop.py` run outside
# supertool's own tree), or a `FORCE_COLOR` a caller re-adds deliberately via
# a declared preset's `env:` block, which the import-time strip intentionally
# leaves free to win. It is also not only cosmetic twice over: `detail` is
# printed for a human to read, so an OSC out of a child steers the reader's
# terminal, and the cap below keeps the **last** 500 characters — every
# escape byte is budget spent on something that renders as nothing, so a
# long enough coloured traceback evicts the exception line.
#
# Held equal to `validators/tsc-check/tsc-check.py`'s `ANSI_RE` by a test, the way
# `validators/common/linebreaks.py` is held equal to `_LINE_BREAK_PATTERN` (#1486):
# an adapter runs with only `validators/common` on `sys.path` and cannot import
# the core, so one definition has to be stated twice and pinned rather than
# trusted. Each complete form is followed by its incomplete one — a CSI or OSC
# with no terminator, and last a lone ESC — which is what a stream cut
# mid-sequence leaves behind; without them "no escape survives" is not the
# invariant it reads as. The order carries weight in both directions, and the
# reasoning is on the adapter's copy.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\[[0-?]*[ -/]*"
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)|\][^\x07\x1b]*"
    r"|[@-Z\\-_]|)"
)


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
    raw = (proc.stderr or b"").decode("utf-8", "replace")
    # Stripped before the cap, never after: see _ANSI_ESCAPE_RE.
    detail = _ANSI_ESCAPE_RE.sub("", raw).strip()
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
