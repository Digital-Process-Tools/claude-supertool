
























































































































from __future__ import annotations

import atexit
import bisect  

import json
import difflib
import hashlib  
import importlib.machinery
import os
import stat  
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
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, MutableMapping, NamedTuple, Optional, Sequence, Tuple  

VERSION = "0.65.1"
































def _load_part(name: str) -> None:



































    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    if not os.path.isfile(path):
        raise ImportError(
            f"incomplete install: {name}.py missing beside {__file__} (#2706)"
        )
    code = importlib.machinery.SourceFileLoader(name, path).get_code(name)
    exec(code, globals())




























def _disable_force_color_for_children() -> None:
    os.environ.pop("FORCE_COLOR", None)


_disable_force_color_for_children()





















_ASCII_DIGITS = re.compile(r"^[0-9]+\Z")


def _is_ascii_int(text: str) -> bool:





    return bool(_ASCII_DIGITS.match(text))


def _fwd(p: str) -> str:

    return p.replace(os.sep, "/")


DETERMINISTIC_TIME_ENV = "SUPERTOOL_DETERMINISTIC_TIME"


def _deterministic_time() -> bool:










    return os.environ.get("SUPERTOOL_DETERMINISTIC_TIME") == "1"


def _timeout_verdict_line(t0: float, timeout: float) -> str:


























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



















    if _deterministic_time():
        return 0.0
    return time.monotonic() - t0


def _python_token() -> str:












    return shlex.quote(sys.executable.replace(os.sep, "/"))


def _safe_relpath(path: str, start: str = ".") -> str:








    try:
        return os.path.relpath(path, start)
    except ValueError:
        return os.path.abspath(path)


MAX_READ_LINES = 300



MAX_BATCH_OPS = 1000
MAX_READ_BYTES = 20000  
MAX_AUTOREAD_LINES = 60  

FOOTER_ECHO_MIN_LINES = 60  




MAX_AROUND_BYTES = 16000  
MAX_GREP_LINE_CHARS = 500  

CHAR_WINDOW_CHARS = 1000  
MINIFIED_LINE_CHARS = 5000  
MAX_GREP_RESULTS = 10






GREP_LIMIT_ALL = -1
_GREP_ALL_TOKEN = "all"



GREP_LIMIT_ALL_MISPLACED = -2
MAX_GREP_COUNT_CEILING = 1000  






MAX_GLOB_RESULTS = 50
LOG_FILE = os.path.join(tempfile.gettempdir(), "supertool-calls.log")
GREP_FILE_INCLUDES = ("*.php", "*.xml", "*.py", "*.js", "*.ts", "*.md")
_GREP_EXTENSIONS_EFFECTIVE: Tuple[str, ...] | None = None

def _match_glob(path: str, pattern: str) -> bool:






    import fnmatch
    if not pattern:
        return True
    if "{" not in pattern or "}" not in pattern:
        return fnmatch.fnmatch(path, pattern)



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






    if not patterns:
        return False
    if isinstance(patterns, str):
        patterns = [patterns]
    return any(_match_glob(path, p) for p in patterns if p)


def _expand_braces(pattern: str) -> List[str]:






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









_load_part("_supertool_edit")
















_last_undo_diagnostic: "Optional[str]" = None













_LINT_TIMEOUT_DEFAULT = 5




_LINT_TIMEOUT_PREFIX = "--- POST-EDIT LINT TIMED OUT"

_LINT_DECLINE_PREFIXES = (
    _LINT_TIMEOUT_PREFIX,
    "--- POST-EDIT LINT DECLINED",
)


def _lint_timeout() -> int:





    return _env_int("SUPERTOOL_LINT_TIMEOUT", _LINT_TIMEOUT_DEFAULT, minimum=1)


def _lint_declined(tool: str, reason: str) -> str:







    return (
        f"--- POST-EDIT LINT DECLINED — {tool} ---\n"
        f"{reason}; the file was NOT checked.\n"
    )














def _which_excluding_cwd(name: str) -> Optional[str]:
    if os.path.dirname(name):
        return shutil.which(name)
    path_env = os.environ.get("PATH")
    if path_env is None:



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













    sn = len(s)
    if pos >= sn:
        return (pos, False)
    c = s[pos]

    if c in "iaAIoO":
        return (pos + 1, True)

    if c in "sSCR":
        return (pos + 1, True)

    if c in "/?:":
        return (pos + 1, True)

    if c == "%" and pos + 1 < sn and s[pos + 1] == "s":
        return (pos + 2, True)

    if c == "c" and pos + 1 < sn and s[pos + 1] in ("c", "w", "$", "0"):
        return (pos + 2, True)

    if c == "c" and pos + 1 < sn and s[pos + 1] == "i" and pos + 2 < sn and s[pos + 2] in ('w', '"', "'", "(", "[", "{"):
        return (pos + 3, True)

    _TO = set('wWsp"\'`()[]{}<>bBt')
    if c in "cdy" and pos + 2 < sn and s[pos + 1] in "ia" and s[pos + 2] in _TO:
        return (pos + 3, c == "c")

    if c == "g" and pos + 3 < sn and s[pos + 1] in ("~", "u", "U") \
            and s[pos + 2] in "ia" and s[pos + 3] in _TO:
        return (pos + 4, False)

    if c == "c" and pos + 1 < sn and s[pos + 1] in "{})(%+-_WBES;,^":
        return (pos + 2, True)

    if c == "c" and pos + 1 < sn and s[pos + 1] in "fFtT":
        return (min(pos + 3, sn), True)

    if c in "dy" and pos + 1 < sn and s[pos + 1] in "fFtT":
        return (min(pos + 3, sn), False)

    if c in "dy" and pos + 1 < sn and s[pos + 1] in "/?":
        return (pos + 2, True)

    if c == "r":
        return (min(pos + 2, sn), False)

    if c in "fFtT":
        return (min(pos + 2, sn), False)

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

    if c in "dyc" and pos + 2 < sn and s[pos + 1] == "g" and s[pos + 2] in "geE_g":
        return (pos + 3, c == "c")

    if c == "g" and pos + 2 < sn and s[pos + 1] in "~uU" and s[pos + 2] == s[pos + 1]:
        return (pos + 3, False)

    if c == "g" and pos + 2 < sn and s[pos + 1] in "~uU":
        return (pos + 3, False)

    if c == "g" and pos + 1 < sn and s[pos + 1] in ("g", "e", "E", "_", "i", "J"):
        greedy = s[pos + 1] == "i"
        return (pos + 2, greedy)

    if c == "~":
        return (pos + 1, False)

    if c in ("\x01", "\x18"):
        return (pos + 1, False)

    if c == "m" and pos + 1 < sn and (("a" <= s[pos + 1] <= "z") or ("A" <= s[pos + 1] <= "Z")):
        return (pos + 2, False)

    if c == "`" and pos + 1 < sn and (
        ("a" <= s[pos + 1] <= "z") or ("A" <= s[pos + 1] <= "Z") or s[pos + 1] == "`"
    ):
        return (pos + 2, False)

    if c == "'" and pos + 1 < sn and (
        ("a" <= s[pos + 1] <= "z") or ("A" <= s[pos + 1] <= "Z") or s[pos + 1] == "'"
    ):
        return (pos + 2, False)

    if c in "><=" and pos + 1 < sn and s[pos + 1] == c:
        return (pos + 2, False)

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

    if c == "u" or c == "\x12":
        return (pos + 1, False)

    return (pos + 1, False)


def op_vim(path: str, script: str) -> str:




    import _supertool_vim

    return _supertool_vim.op_vim(path, script)


def _r_missing_file_diagnostic(path_arg: str) -> str:











    parent = os.path.dirname(os.path.abspath(path_arg)) or os.sep
    if os.path.isdir(parent):
        return " (parent directory exists; the file itself does not)"
    return " (parent directory does not exist)"
















_ONBOARDING_DISABLE_VALUES = {"", "none", "off", "false", "no", "0"}









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




    from _supertool_doctor import op_doctor as _op_doctor_impl
    return _op_doctor_impl(arg)


def op_init(mode: str = "") -> str:




    from _supertool_doctor import op_init as _op_init_impl
    return _op_init_impl(mode)


_load_part("_supertool_guard")


_NO_EXCLUDE_SUFFIX = ":::no-exclude"





_OP_TARGETS: Dict[str, Any] = {
    "edit":          lambda parts: parts[3] if len(parts) > 3 else "",
    "replace":       lambda parts: parts[3] if len(parts) > 3 else "",
    "replace_lines": lambda parts: parts[1] if len(parts) > 1 else "",
    "paste":         lambda parts: parts[1] if len(parts) > 1 else "",
    "append":        lambda parts: parts[1] if len(parts) > 1 else "",
    "vim":           lambda parts: parts[1] if len(parts) > 1 else "",





    "json-set":      lambda parts: parts[1] if len(parts) > 1 else "",
}














_BUILTIN_SYNTAX_VALIDATORS: Dict[str, Dict[str, Any]] = {
    "py-syntax": {
        "builtin": "python",
        "match": "*.py",
        "syntax": True,
        "rollback_on_fail": True,
    },
}

















_PY_SYNTAX_SCOPE = "parsed; not imported"


def _builtin_syntax_run(name: str, kind: str, file: str) -> Dict[str, Any]:






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


        return {"tool": name, "file": file, "ok": False, "count": 1,
                "errors": [{"line": None, "col": None, "severity": "error",
                            "code": "syntax", "msg": str(e)[:300]}],
                "elapsed_s": _elapsed_since(_t0)}
    return {"tool": name, "file": file, "ok": True, "count": 0, "errors": [],
            "scope": _PY_SYNTAX_SCOPE, "elapsed_s": _elapsed_since(_t0)}






















SYNTAX_FLOOR: Tuple[int, int] = (3, 9)
SYNTAX_FLOOR_ENV = "PYTHON39"

_SYNTAX_FLOOR_PROBE = "import sys;print('%d.%d' % sys.version_info[:2])"



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


def _syntax_floor_interpreter(overrides: Optional[Dict[str, str]] = None) -> Optional[str]:























    current = sys.version_info[:2]


    declared = (
        overrides.get("PYTHON39") if overrides is not None
        else os.environ.get("PYTHON39")
    ) or ""
    declared = declared.strip()
    if declared:
        ver = _interpreter_version(declared)
        if ver is None or ver >= current:
            return None
        return declared

    if current <= SYNTAX_FLOOR:
        return sys.executable

    for minor in range(SYNTAX_FLOOR[1], current[1]):




        cand = _which_excluding_cwd("python%d.%d" % (SYNTAX_FLOOR[0], minor))
        if cand and (_interpreter_version(cand) or current) < current:
            return cand
    return None


def _syntax_floor_check(paths: Iterable[str],
                        overrides: Optional[Dict[str, str]] = None) -> Dict[str, Any]:







    floor = "%d.%d" % SYNTAX_FLOOR
    interp = _syntax_floor_interpreter(overrides)
    if interp is None:
        return {"tool": "syntax-floor", "skipped": (
            "no interpreter older than this one to compile with (want Python %s): "
            "point the %s variable at one, or install python%s. This check did NOT run."
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












    import glob
    now = time.time()
    for p in glob.glob("/tmp/supertool-before-*"):
        try:
            if now - os.path.getmtime(p) > max_age_seconds:
                os.unlink(p)
        except OSError:
            pass





_sweep_old_notifier_temp_files()
atexit.register(_sweep_old_notifier_temp_files)


_GC_DEFAULT_INTERVAL_SECONDS = 3600.0
_GC_STAMP_NAME = ".gc-stamp"


def _cache_root() -> Path:

    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".cache")
    return base / "supertool"


def op_gc(mode: str = "", kind: str = "") -> str:




    from _supertool_gc import op_gc as _op_gc_impl
    return _op_gc_impl(mode, kind)


def _maybe_auto_gc() -> None:




















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

    return n + 1 if (len(pre_lines) != len(post_lines)) else None


def _run_notifiers(op: str, path: str, line: Optional[int] = None,
                   pre_content: Optional[bytes] = None,
                   line_end: Optional[int] = None) -> None:










    specs = _applicable_notifiers(op, path)
    if not specs:
        _notifier_log(f"no notifier applicable for op={op} path={path}")
        return


    if line is None and pre_content is not None and path and os.path.isfile(path):
        line = _first_changed_line(pre_content, path)
    _notifier_log(f"dispatch op={op} path={path} line={line} line_end={line_end} pre_content={len(pre_content) if pre_content else 0}B notifiers={list(specs.keys())}")

    before_file = ""
    if pre_content is not None:
        try:
            ext = os.path.splitext(path)[1] or ".txt"

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





























    text = _flat_field(str(value)).strip()
    if limit and len(text) > limit:






        return text[:max(limit - 1, 0)] + "…"
    return text




_UNTRUSTED_FLAT: Optional[Callable[[str], str]] = None
_UNTRUSTED_FLAT_TRIED = False


def _flat_field(text: str, *, disclose_newline: bool = False) -> str:









































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















    return ", ".join(_flat_field(str(n)) for n in names)


_load_part("_supertool_mcp")


_load_part("_supertool_payload")


_load_part("_supertool_dispatch")


_AUTO_CWD_MARKER = ".supertool.json"


def _project_root_above_cwd() -> Optional[str]:




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













    root = _project_root_above_cwd()
    if root is None:
        return None
    candidates: List[str] = []
    for arg in argv:
        if ":" not in arg:
            continue




        for segment in arg.split(":")[1:]:
            segment = segment.strip()
            if not segment or segment.startswith(("@", "-", "~", "/")):
                continue
            if "/" not in segment and "." not in segment:
                continue
            if WILDCARD_CHARS.search(segment):
                continue
            if os.path.exists(segment):
                return None  
            candidates.append(segment)
    for segment in candidates:
        if os.path.exists(os.path.join(root, segment)):
            return root
    return None













def main(argv: List[str]) -> int:
















    global _INVOCATION_DIR, _CWD_SHIFT






    _repo_env_prior_repo = os.environ.get("SUPERTOOL_REPO")
    _repo_env_prior_from_op = os.environ.get("SUPERTOOL_REPO_FROM_OP")





















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


    _reconfigure_stdout_utf8()










    os.environ["PYTHONIOENCODING"] = "utf-8"




























    _LEAKED_GIT_ENV[:] = scrub_git_env()



    if "--plain" in argv:
        argv = [a for a in argv if a != "--plain"]
        os.environ["SUPERTOOL_PLAIN"] = "1"

    if not argv:
        sys.stderr.write(
            "Usage: supertool [--plain] op:args [op:args ...]\n"
            "       supertool 'read:file.py' 'grep:foo:src/:20' 'glob:**/*.md'\n"
        )
        return 1














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



        blocked = [a.split(":", 1)[0] for a in rest
                   if a.split(":", 1)[0] not in targetable
                   and a.split(":", 1)[0] != "cwd"]
        if blocked:
            sys.stderr.write(_repo_refusal(blocked[0]))
            return 1
        os.environ["SUPERTOOL_REPO"] = repo_target






        os.environ["SUPERTOOL_REPO_FROM_OP"] = "1"
        argv = rest
        if not argv:
            return 0







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



        try:
            auto_root = _auto_cwd_root(argv)
        except OSError:
            auto_root = None
        if auto_root:
            os.chdir(auto_root)
            _CWD_SHIFT = "auto-resolved project root"
            sys.stdout.write(
                f"[cwd auto-resolved to project root: {auto_root}]\n")






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





    _preset_warnings = list(_load_config().get("_preset_warnings") or [])
    for _warning in list(_CONFIG_WARNINGS) + _preset_warnings:
        sys.stderr.write(f"supertool: {_warning}\n")







    _mixed_call = _mixed_tree_pair()
    if _mixed_call is not None:
        sys.stderr.write(f"supertool: {_mixed_tree_note(_mixed_call)}\n")











    _prevalidation_error = _batch_prevalidation_refusal(argv)
    if _prevalidation_error is not None:
        sys.stderr.write(_prevalidation_error)
        return 1


    total_out_bytes = 0
    any_failure = False





    _leak_notice = _git_env_notice(_LEAKED_GIT_ENV)
    if _leak_notice:
        sys.stdout.write(_leak_notice)
        total_out_bytes += len(_leak_notice.encode("utf-8"))




    _skips_at_entry = _SKIP_COUNT[0]
    _rollbacks_at_entry = _ROLLBACK_COUNT[0]
    _not_checked_at_entry = len(_NOT_CHECKED)
    _validated_at_entry = len(_VALIDATED_FILES)
    _unclean_at_entry = len(_UNCLEAN_VALUE_EXITS)




    bodies: List[str]
    workers = _parallel_workers()
    parallel_path = (
        workers >= 2
        and len(argv) > 1
        and all(_is_parallel_safe(a) for a in argv)
    )





    global _DEFER_FORMATTERS, _FORMAT_QUEUE, _VALIDATOR_DEFER_QUEUE, _VALIDATOR_DEFER_SEEN  
    defer = len(argv) > 1 and not parallel_path
    if defer:
        _DEFER_FORMATTERS = True
        _FORMAT_QUEUE = {}
        _VALIDATOR_DEFER_QUEUE = []
        _VALIDATOR_DEFER_SEEN = set()

    try:
        if parallel_path:

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



    counter_failure = False
    for body, op_failed in answers:
        sys.stdout.write(body)
        total_out_bytes += len(body.encode("utf-8"))
        if op_failed:
            any_failure = True
            refused += 1


    if defer:
        drain_out = _drain_format_queue()  
        if drain_out:
            sys.stdout.write(drain_out)
            total_out_bytes += len(drain_out.encode("utf-8"))
        validator_drain_out = _drain_validator_queue()  
        if validator_drain_out:
            sys.stdout.write(validator_drain_out)
            total_out_bytes += len(validator_drain_out.encode("utf-8"))






    if _SKIP_COUNT[0] > _skips_at_entry:
        any_failure = True
        counter_failure = True







    if _ROLLBACK_COUNT[0] > _rollbacks_at_entry:
        any_failure = True
        counter_failure = True











    if len(_NOT_CHECKED) > _not_checked_at_entry:
        any_failure = True
        counter_failure = True
    del _NOT_CHECKED[_not_checked_at_entry:]


    del _VALIDATED_FILES[_validated_at_entry:]






    _unclean_values = _UNCLEAN_VALUE_EXITS[_unclean_at_entry:]
    del _UNCLEAN_VALUE_EXITS[_unclean_at_entry:]
    if _unclean_values:
        any_failure = True













    _other = _other_causes_phrase(counter_failure, _unclean_values)
    if len(bodies) > 1 and any_failure:
        if (counter_failure or _unclean_values) and refused:







            tally = (
                f"[batch] {len(bodies)} ops ran — {refused} refused, and "
                f"{_other} also failed this call (above). More than one thing "
                f"went wrong: read the per-op receipts, not these counts."
                + chr(10)
            )
        elif refused == len(bodies):



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


            tally = (
                f"[batch] {len(bodies)} ops ran — all {len(bodies)} rendered an "
                f"answer. Exit 1 is {_other} (above), not an op refusing."
                + chr(10)
            )
        sys.stdout.write(tally)
        total_out_bytes += len(tally.encode("utf-8"))

    log_call(argv, total_out_bytes)
    return 1 if any_failure else 0
































_OP_VERDICT_FAIL = re.compile(r"(FAIL\b|ERROR:\s)")


def _other_causes_phrase(counter_failure: bool, unclean_values: List[str]) -> str:
















    causes = []
    if counter_failure:
        causes.append("a skipped write, a rolled-back edit or a validator that "
                      "could not run")
    if unclean_values:



        causes.append("an answer its op does not declare clear to proceed ("
                      + ", ".join(dict.fromkeys(unclean_values)) + ")")
    if not causes:
        return ("a skipped write, a rolled-back edit or a validator that could "
                "not run")
    return "; ".join(causes)


def _op_body_failed(body: str) -> bool:








    return _OP_VERDICT_FAIL.match(body) is not None


def _receipt(header: str, body: str) -> str:








    if _op_body_failed(body):
        _mark_op_failure()
    return header + body


def _mark_op_failure() -> None:


















    _DISPATCH_STATE.call_failed = True


def _call_failed() -> bool:

    return bool(getattr(_DISPATCH_STATE, "call_failed", False))


def _cli() -> int:
    return main(sys.argv[1:])











if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
