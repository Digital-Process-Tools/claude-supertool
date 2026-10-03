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
import subprocess
import sys
import tempfile
import threading  # noqa: F401 -- only use left in this file is inside _supertool_mcp.py's part (#2706)
import time
from datetime import datetime  # noqa: F401 -- only use left in this file is inside _supertool_dispatch.py's part (#2706)
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, MutableMapping, NamedTuple, Optional, Sequence, Tuple  # noqa: F401 -- MutableMapping has no direct use in this file after #2706 moved its two call sites into _supertool_presets.py, which execs into this module's own globals() and still needs the name bound here; Sequence used only by _supertool_edit.py (#2706), sharing this module's own globals()

VERSION = "0.65.1"

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

    #2734: this is very likely what the Anthropic directory's
    `COMMAND_SCRIPT_NOT_FOLLOWED` hold on `supertool.py` ("runs a further
    file the validator did not read") actually fires on. `supertool.py`
    itself does one plain, statically-followable `import _supertool` --
    not `importlib` against a runtime path, not `os.execv`, not `runpy` --
    so a scanner that follows ordinary `import` statements should have no
    trouble reaching `_supertool.py`. But `_supertool.py`'s own top level
    calls this function roughly a dozen times, once per part, and each
    call reads a SECOND file by a name built from a string argument and
    `exec()`s its compiled code -- not an `import` an AST-walking scanner
    can resolve to a path, but a dynamically-read file whose very existence
    is opaque without actually running this function. The parallel case
    this issue found already confirmed (`build_release_tree.py`'s
    `inline_python_ladder`, #2732): a shipped *shell* script `source`/`.`ing
    a sibling hit the same hold, fixed by inlining the sourced body into
    the consumer at build time so nothing ships that sources anything. The
    same move does not transfer here without cost: inlining every part
    back into `_supertool.py` is exactly the ~17k-line-script shape #931
    moved away from, so `supertool.py` is re-parsed from source on every
    invocation again -- the cost #931 exists to avoid, not merely a style
    preference. No change made here for that reason: a confirmed one-line
    fix for the shell-script instance does not have an equivalent for this
    one without undoing #931, and nothing on this end can run the real
    directory validator to confirm a speculative rewrite of this function
    (e.g. a real per-part `import` plus a `vars()` copy into globals())
    would even satisfy it before paying that risk.
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


_load_part("_supertool_dispatch")


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


# Env vars a `repo:` op's pre-pass may export for the duration of one call
# (`_supertool.py`'s own SUPERTOOL_REPO plus its #1986 from-op marker).
# `main()` snapshots and restores these so the export never outlives the
# call that made it (#1962) — a direct `os.environ` write with nothing to
# restore it, which `monkeypatch` cannot undo because it never performed
# the mutation in the first place. Read and restored by two literal-keyed
# statements inside main() rather than a loop over a names tuple (#2734) --
# no `_REPO_ENV_VARS` constant any more, since nothing else referenced it.


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
    # Two literal-keyed reads rather than a loop over _REPO_ENV_VARS (#2734):
    # os.environ[name] with `name` a loop variable is what the Anthropic
    # directory's scanner was reading as "an environment variable named at
    # run time" (MCP_FORWARDS_CREDENTIAL_ENV, #2732) -- even though both
    # names are this module's own fixed constants, never attacker input.
    # Same restore semantics as before, just unrolled.
    _repo_env_prior_repo = os.environ.get("SUPERTOOL_REPO")
    _repo_env_prior_from_op = os.environ.get("SUPERTOOL_REPO_FROM_OP")
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
        if _repo_env_prior_repo is None:
            os.environ.pop("SUPERTOOL_REPO", None)
        else:
            os.environ["SUPERTOOL_REPO"] = _repo_env_prior_repo
        if _repo_env_prior_from_op is None:
            os.environ.pop("SUPERTOOL_REPO_FROM_OP", None)
        else:
            os.environ["SUPERTOOL_REPO_FROM_OP"] = _repo_env_prior_from_op


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
