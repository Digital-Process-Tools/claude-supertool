









import bisect
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
from typing import List, Optional, Tuple

import _supertool as _st

def _atomic_write(*a, **kw):
    return _st._atomic_write(*a, **kw)


def _bump_counter(*a, **kw):
    return _st._bump_counter(*a, **kw)


def _cache_root(*a, **kw):
    return _st._cache_root(*a, **kw)


def _count_already_applied(*a, **kw):
    return _st._count_already_applied(*a, **kw)


def _edit_already_applied(*a, **kw):
    return _st._edit_already_applied(*a, **kw)


def _env_int(*a, **kw):
    return _st._env_int(*a, **kw)


def _is_ascii_int(*a, **kw):
    return _st._is_ascii_int(*a, **kw)


def _load_config(*a, **kw):
    return _st._load_config(*a, **kw)


def _path_not_found(*a, **kw):
    return _st._path_not_found(*a, **kw)


def _pattern_gate(*a, **kw):
    return _st._pattern_gate(*a, **kw)


def _safe_path(*a, **kw):
    return _st._safe_path(*a, **kw)


def _undecodable_at(*a, **kw):
    return _st._undecodable_at(*a, **kw)


def _which_excluding_cwd(*a, **kw):
    return _st._which_excluding_cwd(*a, **kw)


def mark(*a, **kw):
    return _st.mark(*a, **kw)


def _decode_escapes(*a, **kw):
    return _st._decode_escapes(*a, **kw)


def _lint_timeout(*a, **kw):
    return _st._lint_timeout(*a, **kw)


def _lint_declined(*a, **kw):
    return _st._lint_declined(*a, **kw)


def _verb_token_at(*a, **kw):
    return _st._verb_token_at(*a, **kw)


def _r_missing_file_diagnostic(*a, **kw):
    return _st._r_missing_file_diagnostic(*a, **kw)


SecurityError = _st.SecurityError
_REAPPLY_COUNT = _st._REAPPLY_COUNT
_SKIP_COUNT = _st._SKIP_COUNT
_LINT_TIMEOUT_PREFIX = _st._LINT_TIMEOUT_PREFIX
_LINT_DECLINE_PREFIXES = _st._LINT_DECLINE_PREFIXES

def _check_vim_shell_allowed() -> Optional[str]:











    if os.environ.get("SUPERTOOL_ALLOW_VIM_SHELL") == "1":
        return None
    try:
        if bool(_load_config().get("allow_vim_shell")):
            return None
    except Exception:
        pass
    return (
        "ERROR: vim shell verbs (:!, :%!, :r !) are disabled by default. "
        'To allow: define SUPERTOOL_ALLOW_VIM_SHELL=1 as an environment '
        'variable, or add '
        '`"allow_vim_shell": true` to .supertool.json. '
        "For one-off shell logic, prefer a wrapper script + custom op.\n"
    )

def _vim_sub_reapplied_count(
    body: str, rx: "re.Pattern", srepl_safe: str, is_global: bool,
    pattern_is_multiline: bool,
) -> int:











































    matches: List[Tuple[int, str, str]] = []
    if pattern_is_multiline:





        for m in rx.finditer(body):
            matches.append((m.start(), m.group(0), m.expand(srepl_safe)))
            if not is_global:
                break
    else:
        has_trailing_nl = body.endswith("\n")
        body_lines = body.split("\n")
        if has_trailing_nl:
            body_lines = body_lines[:-1]







        line_start = 0
        for ln in body_lines:
            for m in rx.finditer(ln):
                matches.append(
                    (line_start + m.start(), m.group(0), m.expand(srepl_safe)))
                if not is_global:
                    break
            line_start += len(ln) + 1
    if not matches:
        return 0
    distinct_news = {new for _, _, new in matches}
    if len(distinct_news) > 1:




        return sum(
            1 for idx, old, new in matches
            if _edit_already_applied(body, old, new, idx)
        )
    new_text = next(iter(distinct_news))
    if not new_text:
        return 0
    new_idxs: List[int] = []
    start = 0
    while True:
        j = body.find(new_text, start)
        if j == -1:
            break
        new_idxs.append(j)
        start = j + 1
    if not new_idxs:
        return 0
    reapplied = 0
    for idx, old, _new in matches:
        if len(new_text) <= len(old) or old not in new_text:
            continue
        end = idx + len(old)
        lo = end - len(new_text)


        pos = bisect.bisect_right(new_idxs, idx) - 1
        if pos >= 0 and new_idxs[pos] >= lo:
            reapplied += 1
    return reapplied

def _vim_cursor_state_path(file_path: str) -> str:

    abs_path = os.path.abspath(file_path)
    digest = hashlib.sha1(abs_path.encode("utf-8")).hexdigest()
    return os.path.join(str(_cache_root() / "vim-cursor"), digest)

def _vim_load_state(file_path: str, content_len: int) -> dict:





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

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            cur = int(data.get("cursor", 0))
            marks_raw = data.get("marks", {}) or {}
            marks = {k: int(v) for k, v in marks_raw.items() if isinstance(k, str)}
            le = data.get("last_edit", None)
            le_val = int(le) if le is not None else None

            cur = max(0, min(content_len, cur))
            marks = {k: max(0, min(content_len, v)) for k, v in marks.items()}
            if le_val is not None:
                le_val = max(0, min(content_len, le_val))
            macros_raw = data.get("macros", {}) or {}
            macros = {k: str(v) for k, v in macros_raw.items()
                      if isinstance(k, str) and len(k) == 1 and "a" <= k <= "z"}

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

    return _vim_load_state(file_path, content_len)["cursor"]

def _vim_save_cursor(file_path: str, cursor: int) -> None:

    if os.environ.get("SUPERTOOL_VIM_NO_PERSIST"):
        return

    try:
        existing = _vim_load_state(file_path, 10**9)
    except Exception:
        existing = {"marks": {}, "last_edit": None, "macros": {}}
    _vim_save_state(file_path, cursor, existing.get("marks", {}), existing.get("last_edit"),
                    existing.get("macros", {}), existing.get("last_change"))

def _vim_undo_state_path(file_path: str) -> str:

    abs_path = os.path.abspath(file_path)
    digest = hashlib.sha1(abs_path.encode("utf-8")).hexdigest()
    return os.path.join(str(_cache_root() / "vim-undo"), digest + ".last")

def _vim_load_undo_snapshot(file_path: str) -> "Optional[dict]":



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
    pass

def _resolve_text_object(content: str, cursor: int, kind: str, around: bool) -> tuple:





    n = len(content)

    if kind == "b":
        kind = "("
    elif kind == "B":
        kind = "{"

    pair_close_to_open = {")": "(", "]": "[", "}": "{", ">": "<"}
    if kind in pair_close_to_open:
        kind = pair_close_to_open[kind]


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

            s = cursor
            while s > 0 and not is_word(content[s - 1]) and content[s - 1] not in " \t\n":
                s -= 1
            e = cursor
            while e < n and not is_word(content[e]) and content[e] not in " \t\n":
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


    if kind == "W":
        if cursor >= n:
            raise _TextObjectError("iW/aW at EOF")
        def is_ws(ch: str) -> bool:
            return ch in " \t\n"
        if is_ws(content[cursor]):

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


    if kind == "s":

        s = cursor
        while s > 0:
            prev = content[s - 1]
            if prev in ".!?" and s < n and content[s] in (" ", "\t", "\n"):

                while s < n and content[s] in (" ", "\t"):
                    s += 1
                break
            s -= 1

        e = cursor
        while e < n and content[e] not in ".!?":
            e += 1
        if e < n:
            e += 1  
        if around:
            while e < n and content[e] in (" ", "\t"):
                e += 1
        return (s, e)


    if kind == "p":
        lines = content.split("\n")

        cum = 0
        line_idx = 0
        for idx, ln in enumerate(lines):
            if cum + len(ln) >= cursor:
                line_idx = idx
                break
            cum += len(ln) + 1
        else:
            line_idx = len(lines) - 1


        on_blank = lines[line_idx] == ""
        start_idx = line_idx
        while start_idx > 0 and (lines[start_idx - 1] == "") == on_blank:
            start_idx -= 1
        end_idx = line_idx
        while end_idx + 1 < len(lines) and (lines[end_idx + 1] == "") == on_blank:
            end_idx += 1

        s = sum(len(l) + 1 for l in lines[:start_idx])
        e = sum(len(l) + 1 for l in lines[:end_idx + 1])  
        if not around:

            pass
        else:

            j = end_idx + 1
            while j < len(lines) and lines[j] == "":
                j += 1
            e = sum(len(l) + 1 for l in lines[:j])
        return (s, min(e, n))


    if kind in ('"', "'", "`"):
        q = kind

        bol = content.rfind("\n", 0, cursor) + 1
        eol_pos = content.find("\n", cursor)
        if eol_pos == -1:
            eol_pos = n
        line = content[bol:eol_pos]

        rel_cur = cursor - bol

        positions = [i for i, ch in enumerate(line) if ch == q]
        if len(positions) < 2:
            raise _TextObjectError(f"no matching {q} pair on line")

        pair = None
        for k in range(0, len(positions) - 1, 2):
            p1, p2 = positions[k], positions[k + 1]
            if p1 <= rel_cur <= p2:
                pair = (p1, p2)
                break
        if pair is None:

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


    if kind in ("(", "[", "{", "<"):
        opener = kind
        closer = {"(": ")", "[": "]", "{": "}", "<": ">"}[opener]

        depth = 0
        s = -1
        k = cursor

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

            fwd = content.find(opener, cursor)
            if fwd == -1:
                raise _TextObjectError(f"no opening {opener} found")
            s = fwd

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


    if kind == "t":
        import re as _re
        tag_re = _re.compile(r"<(/?)([A-Za-z][A-Za-z0-9_:-]*)[^<>]*>")



        stack = []  
        pairs = []  
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

                if m.group(0).rstrip(">").endswith("/"):
                    continue
                stack.append((m.start(), m.end(), name))

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






    if before == after:
        return "--- diff: no changes ---\n"
    b_lines = before.splitlines(keepends=True)
    a_lines = after.splitlines(keepends=True)
    raw = list(difflib.unified_diff(b_lines, a_lines, n=2, lineterm=""))
    if not raw:
        return "--- diff: no changes ---\n"

    body = [ln for ln in raw if not ln.startswith("---") and not ln.startswith("+++")]

    hunks: list[list[str]] = []
    current: list[str] = []
    for ln in body:
        if ln.startswith("@@"):
            if current:
                hunks.append(current)

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























    ext = os.path.splitext(path)[1].lower()
    tool = ""
    cmd: list[str] = []
    parse_inline = False

    if ext == ".php":
        php_bin = _which_excluding_cwd("php")
        if not php_bin:
            return ""
        tool = "php -l"
        cmd = [php_bin, "-l", path]
    elif ext == ".json":
        tool = "json"
        parse_inline = True
    elif ext == ".xml":
        xmllint_bin = _which_excluding_cwd("xmllint")
        if not xmllint_bin:
            return ""
        tool = "xmllint"
        cmd = [xmllint_bin, "--noout", path]
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
            f"{_LINT_TIMEOUT_PREFIX} — {tool} ({timeout}s) ---\n"
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







    addr = addr.strip()
    if not addr:
        raise ValueError("empty address")
    base = addr
    offset = 0


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
    elif _is_ascii_int(base):
        line = int(base)
    else:
        raise ValueError(f"bad address {addr!r}")
    return line + offset

def _vim_literal_decode(pat: str) -> str:








    out = pat

    if out.startswith("^"):
        out = out[1:]


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







    if not pat or not content:
        return ""

    chunks = [c for c in re.split(r"[\\.\^\$\*\+\?\(\)\[\]\{\}\|\n]+", pat) if len(c) >= 3]
    if not chunks:
        return ""


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




    out = _op_vim_impl(path, script)
    if out.startswith("ERROR"):


        _bump_counter(_SKIP_COUNT, "cnt_skip")
        suffix = " (file unchanged — vim ops are atomic, no actions applied)\n"

        if out.endswith("\n"):
            out = out[:-1] + suffix
        else:
            out = out + suffix
    return out

def _op_vim_impl(path: str, script: str) -> str:


























































































































    if not path:
        return "ERROR: empty path\n"
    if not os.path.isfile(path):
        return _path_not_found(path, label="file", op="vim", creates=True)
    if not script.strip():
        return "ERROR: empty script\n"

    try:











        with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
            content = f.read()
    except OSError as e:
        return f"ERROR: failed to read {path}: {e}\n"

    _before_content = content











    ESC = "\x1b"


    raw_actions: List[str] = []

















    _esc_literal_sentinel = "\x00ESCLIT\x00"
    normalized = script.replace("\\\\e", _esc_literal_sentinel)
    normalized = normalized.replace("\\e", ESC).replace("\x1e", ESC).replace("␞", ESC)
    normalized = normalized.replace(_esc_literal_sentinel, "\\e")


    normalized = normalized.replace("\\C-a", "\x01").replace("\\C-x", "\x18")



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






    normalized = re.sub(
        r"(^|[" + ESC + r"\n])([%]?[gv]/)",
        lambda m: m.group(1) + ":" + m.group(2),
        normalized,
    )



    normalized = re.sub(r":\%([gv]/)", r":\1", normalized)



    normalized = re.sub(
        r":(\d+|\$|\.)a\n(.*?)\n\.\n?",
        lambda m: (
            ("G" if m.group(1) in ("$", ".") else m.group(1) + "G")
            + "o" + m.group(2) + ESC
        ),
        normalized,
        flags=re.DOTALL,
    )

    normalized = re.sub(
        r":(\d+|\$|\.)i\n(.*?)\n\.\n?",
        lambda m: (
            ("G" if m.group(1) in ("$", ".") else m.group(1) + "G")
            + "O" + m.group(2) + ESC
        ),
        normalized,
        flags=re.DOTALL,
    )




    normalized = re.sub(
        r"(?<![a-zA-Z0-9])(\d+),(?=[" + ESC + r"\n]|$)",
        "",
        normalized,
    )
    i = 0
    n = len(normalized)

    def _verb_token(start: int) -> tuple:






        if start >= n:
            return (start, False)
        c = normalized[start]




        if c == "V":
            j = start + 1
            while j < n and _is_ascii_int(normalized[j]):
                j += 1

            if j < n and normalized[j] in "jkG":
                j += 1
            elif j + 1 < n and normalized[j] == "g" and normalized[j + 1] == "g":
                j += 2

            if j < n and normalized[j] == ":":
                return (j + 1, True)

            if j < n and normalized[j] in "dyc":
                op_char = normalized[j]
                j += 1
                if j < n and normalized[j] == op_char:
                    j += 1
                return (j, op_char == "c")

            return (start + 1, False)






        if c == "v":
            j = start + 1
            while j < n and _is_ascii_int(normalized[j]):
                j += 1
            if j >= n:
                return (start + 1, False)

            _TO_KINDS_V = set('wWsp"\'`()[]{}<>bBt')
            if normalized[j] in "ia" and j + 1 < n and normalized[j + 1] in _TO_KINDS_V:
                motion_end = j + 2
                if motion_end < n and normalized[motion_end] in "dyc":
                    op_char = normalized[motion_end]
                    return (motion_end + 1, op_char == "c")
                return (start + 1, False)

            if j + 1 < n and normalized[j] == "g" and normalized[j + 1] == "g":
                motion_end = j + 2
                if motion_end < n and normalized[motion_end] in "dyc":
                    op_char = normalized[motion_end]
                    return (motion_end + 1, op_char == "c")
                return (start + 1, False)

            if normalized[j] in "fFtT" and j + 1 < n:
                motion_end = j + 2
                if motion_end < n and normalized[motion_end] in "dyc":
                    op_char = normalized[motion_end]
                    return (motion_end + 1, op_char == "c")
                return (start + 1, False)

            _V_SIMPLE_MOTIONS = set("wbeWBEjkhl$0^G{}()%;,")
            if normalized[j] in _V_SIMPLE_MOTIONS:
                motion_end = j + 1
                if motion_end < n and normalized[motion_end] in "dyc":
                    op_char = normalized[motion_end]
                    return (motion_end + 1, op_char == "c")
                return (start + 1, False)

            return (start + 1, False)
        return _verb_token_at(normalized, start)



    macros_pending: dict = {}

    def _greedy_verb(s: str, pos: int) -> tuple:





        return _verb_token_at(s, pos)

    while i < n:



        while i < n and normalized[i] in (" \t\n\r" + ESC):
            i += 1
        if i >= n:
            break
        action_start = i






        if normalized[i] == "q" and i + 1 < n and "a" <= normalized[i + 1] <= "z":
            reg = normalized[i + 1]
            body_start = i + 2




            _scan = body_start
            close_q = -1
            while _scan < n:
                while _scan < n and normalized[_scan] in (" \t\n\r" + ESC):
                    _scan += 1
                if _scan >= n:
                    break

                if normalized[_scan] == "q":
                    close_q = _scan
                    break

                _sv = _scan
                if _is_ascii_int(normalized[_sv]) and normalized[_sv] != "0":
                    while _sv < n and _is_ascii_int(normalized[_sv]):
                        _sv += 1
                if _sv >= n:
                    _scan = n
                    break

                if normalized[_sv] == "@" and _sv + 1 < n and (
                    ("a" <= normalized[_sv + 1] <= "z") or normalized[_sv + 1] == "@"
                ):
                    _scan = _sv + 2
                    continue

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
                if _is_ascii_int(_rec_norm[_ri]) and _rec_norm[_ri] != "0":
                    while _rverb_pos < _rn and _is_ascii_int(_rec_norm[_rverb_pos]):
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

        verb_pos = i
        if _is_ascii_int(normalized[i]) and normalized[i] != "0":
            while verb_pos < n and _is_ascii_int(normalized[verb_pos]):
                verb_pos += 1
        if verb_pos >= n:

            raw_actions.append(normalized[action_start:])
            break


        if normalized[verb_pos] == "@" and verb_pos + 1 < n and (
            ("a" <= normalized[verb_pos + 1] <= "z") or normalized[verb_pos + 1] == "@"
        ):
            raw_actions.append(normalized[action_start: verb_pos + 2])
            i = verb_pos + 2
            continue

        verb_end, enters_text = _verb_token(verb_pos)
        if enters_text:

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

        if not action:
            return (1, "", "")


        if action.startswith(":%%"):

            k = 1
            while k < len(action) and action[k] == "%":
                k += 1
            action = ":%" + action[k:]




        if len(action) >= 3 and action[0] == "g" and action[1] == "/":
            action = ":" + action
        elif len(action) >= 3 and action[0] == "v" and action[1] == "/":
            action = ":" + action
        elif len(action) >= 4 and action[0] == "%" and action[1] in ("g", "v") and action[2] == "/":
            action = ":" + action
        i = 0

        if _is_ascii_int(action[0]) and action[0] != "0":
            while i < len(action) and _is_ascii_int(action[i]):
                i += 1
        count = int(action[:i]) if i > 0 else 1
        rest = action[i:]
        if not rest:
            return (count, "", "")

        if len(rest) >= 3 and rest[:3] == "ciw":
            return (count, "ciw", rest[3:])
        if len(rest) >= 3 and rest[:2] == "ci" and rest[2] in ('"', "'", "(", "[", "{"):
            return (count, "ci" + rest[2], rest[3:])



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

        if len(rest) >= 3 and rest[:3] == ":%!":
            return (count, ":!", "\x1d%\x1d" + rest[3:])

        if len(rest) >= 3 and rest[:3] == ":%s":
            return (count, ":s", rest[3:])

        if len(rest) >= 2 and rest[:2] == "%s":
            return (count, ":s", rest[2:])





        if len(rest) >= 2 and rest[0] == ":" and rest[1] in "0123456789.$+-":
            j = 1


            while j < len(rest) and (_is_ascii_int(rest[j]) or rest[j] in ".$+-"):
                j += 1

            if j < len(rest) and rest[j] == ",":
                j += 1


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
                    while j < len(rest) and (_is_ascii_int(rest[j]) or rest[j] in ".$+-"):
                        j += 1




            if rest[j:j + 4] == "sort":
                range_spec = rest[1:j]
                body = rest[j + 4:]
                return (count, ":sort", f"\x1d{range_spec}\x1d{body}")
            if rest[j:j + 7] == "reverse":
                range_spec = rest[1:j]
                body = rest[j + 7:]
                return (count, ":reverse", f"\x1d{range_spec}\x1d{body}")

            if rest[j:j + 4] == "move":
                range_spec = rest[1:j]
                body = rest[j + 4:]
                return (count, ":move", f"\x1d{range_spec}\x1d{body}")
            if rest[j:j + 1] == "m" and (j + 1 >= len(rest) or not rest[j + 1].isalpha()):
                range_spec = rest[1:j]
                body = rest[j + 1:]
                return (count, ":move", f"\x1d{range_spec}\x1d{body}")

            if rest[j:j + 4] == "copy":
                range_spec = rest[1:j]
                body = rest[j + 4:]
                return (count, ":copy", f"\x1d{range_spec}\x1d{body}")
            if rest[j:j + 1] == "t" and (j + 1 >= len(rest) or not rest[j + 1].isalpha()):
                range_spec = rest[1:j]
                body = rest[j + 1:]
                return (count, ":copy", f"\x1d{range_spec}\x1d{body}")

            if rest[j:j + 4] == "norm":
                range_spec = rest[1:j]
                body = rest[j + 4:]
                return (count, ":norm", f"\x1d{range_spec}\x1d{body}")

            if j < len(rest) and rest[j] == "!":
                range_spec = rest[1:j]
                cmd = rest[j + 1:]
                return (count, ":!", f"\x1d{range_spec}\x1d{cmd}")

            if j < len(rest) and rest[j] == "s":
                range_spec = rest[1:j]
                body = rest[j + 1:]
                return (count, ":s", f"\x1d{range_spec}\x1d{body}")
            if j < len(rest) and rest[j] == "d":
                range_spec = rest[1:j]
                trailing = rest[j + 1:]
                return (count, ":d", f"\x1d{range_spec}\x1d{trailing}")


            if j < len(rest) and rest[j] == "r":
                range_spec = rest[1:j]
                body = rest[j + 1:]
                return (count, ":r", f"\x1d{range_spec}\x1d{body}")




            if j == len(rest) and "," not in rest[1:j]:
                spec = rest[1:j]
                if _is_ascii_int(spec) or spec in ("$", "."):
                    return (count, ":goto", spec)

        if len(rest) >= 3 and rest[:3] == ":%d":
            return (count, ":d", "\x1d%\x1d" + rest[3:])

        if len(rest) >= 6 and rest[:6] == ":%sort":
            return (count, ":sort", "\x1d%\x1d" + rest[6:])
        if len(rest) >= 9 and rest[:9] == ":%reverse":
            return (count, ":reverse", "\x1d%\x1d" + rest[9:])
        if len(rest) >= 6 and rest[:6] == ":%norm":
            return (count, ":norm", "\x1d%\x1d" + rest[6:])


        if len(rest) >= 5 and rest[:5] == ":sort":
            return (count, ":sort", "\x1d%\x1d" + rest[5:])
        if len(rest) >= 8 and rest[:8] == ":reverse":
            return (count, ":reverse", "\x1d%\x1d" + rest[8:])

        if len(rest) >= 6 and rest[:6] == ":retab":
            return (count, ":retab", rest[6:])

        if len(rest) >= 2 and rest[:2] == ":!" and len(rest) > 2:
            return (count, ":!", "\x1d\x1d" + rest[2:])

        if len(rest) >= 5 and rest[:5] == ":norm":
            return (count, ":norm", "\x1d.\x1d" + rest[5:])

        if len(rest) >= 5 and rest[:5] == ":move":
            return (count, ":move", "\x1d.\x1d" + rest[5:])
        if len(rest) >= 5 and rest[:5] == ":copy":
            return (count, ":copy", "\x1d.\x1d" + rest[5:])
        if len(rest) >= 2 and rest[:2] == ":m" and (len(rest) < 3 or not rest[2].isalpha()):
            return (count, ":move", "\x1d.\x1d" + rest[2:])
        if len(rest) >= 2 and rest[:2] == ":t" and (len(rest) < 3 or not rest[2].isalpha()):
            return (count, ":copy", "\x1d.\x1d" + rest[2:])


        if len(rest) >= 4 and (rest[:2] == ":g" or rest[:2] == ":v"):
            mode = "v" if rest[:2] == ":v" else "g"
            k = 2

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

        if len(rest) >= 2 and rest[:2] == ":s":
            return (count, ":s", rest[2:])
        if len(rest) >= 2 and rest[:2] == ":r":
            return (count, ":r", rest[2:])




        _WRITE_NOOP_PREFIXES = (
            ":wq!", ":wq", ":wa!", ":wa", ":write", ":w!", ":w",
            ":x!", ":x", ":xa!", ":xa",
        )
        for _wp in _WRITE_NOOP_PREFIXES:
            if rest == _wp or rest.startswith(_wp) and (
                len(rest) == len(_wp) or rest[len(_wp)] in " \t"
            ):
                return (count, ":noop", rest[len(_wp):])

        if len(rest) >= 3 and rest[:3] in (
            "dgg", "ygg", "cgg",
            "dge", "ygE", "yg_", "ygE", "yge",
            "dgE", "dg_", "cge", "cgE", "cg_",
        ):
            return (count, rest[:3], rest[3:])

        if len(rest) >= 3 and rest[:3] in ("g~~", "guu", "gUU"):
            return (count, rest[:3], rest[3:])


        if len(rest) >= 3 and rest[0] == "g" and rest[1] in ("~", "u", "U"):
            return (count, rest[:2], rest[2:])

        if len(rest) >= 2 and rest[:2] in ("ge", "gE", "g_", "gJ"):
            return (count, rest[:2], rest[2:])

        if len(rest) >= 2 and rest[:2] == "gi":
            return (count, "gi", rest[2:])

        if rest[0] == "R":
            return (count, "R", rest[1:])

        if len(rest) >= 2 and rest[0] == "m" and (
            ("a" <= rest[1] <= "z") or ("A" <= rest[1] <= "Z")
        ):
            return (count, "m", rest[1:2] + rest[2:][:0]) if False else (count, "m" + rest[1], rest[2:])

        if len(rest) >= 2 and rest[0] == "`" and (
            ("a" <= rest[1] <= "z") or ("A" <= rest[1] <= "Z") or rest[1] == "`"
        ):
            return (count, "`" + rest[1], rest[2:])

        if len(rest) >= 2 and rest[0] == "'" and (
            ("a" <= rest[1] <= "z") or ("A" <= rest[1] <= "Z") or rest[1] == "'"
        ):
            return (count, "'" + rest[1], rest[2:])

        if len(rest) >= 2 and rest[:2] in (">>", "<<", "=="):
            return (count, rest[:2], rest[2:])

        if len(rest) >= 2 and rest[0] in "><=" and rest[1] != rest[0]:
            op = rest[0]

            mi = 1
            while mi < len(rest) and _is_ascii_int(rest[mi]):
                mi += 1
            motion_count = int(rest[1:mi]) if mi > 1 else 1
            tail = rest[mi:]  
            _to = set('wWsp"\'`()[]{}<>bBt')

            if mi == 1 and len(tail) >= 2 and tail[0] in "ia" and tail[1] in _to:
                return (count, op + tail[0] + tail[1], tail[2:])

            if mi == 1 and len(tail) >= 2 and tail[0] == "g" and tail[1] == "g":
                return (count, op + "gg", tail[2:])


            if tail and tail[0] in ("j", "k", "h", "l", "G", "{", "}", "(", ")",
                                    "%", "+", "-", "_", "w", "b", "e", "W", "B", "E",
                                    "$", "0", "^"):
                return (count, op + tail[0], str(motion_count) + tail[1:])


        if len(rest) >= 2 and rest[:2] in (
            "gg", "dd", "cc", "cw",
            "yy", "yw", "y$",
            "dw", "d$", "d0",
            "c$", "c0",
            "dG", "d^", "dh", "dj", "dk", "dl",
            "yG", "y^", "yh", "yj", "yk", "yl",
            "d/", "d?", "y/", "y?",

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


        if (
            len(rest) >= 3
            and rest[0] in ("c", "d", "y")
            and rest[1] in ("f", "F", "t", "T")
        ):
            return (count, rest[:2], rest[2:])
        c = rest[0]

        if c in ("/", "?"):
            return (count, c, rest[1:])

        if c in ("i", "a", "I", "A", "o", "O"):
            return (count, c, rest[1:])


        if c in ("s", "S", "C"):
            return (count, c, rest[1:])

        if c == "r":
            return (count, c, rest[1:2])

        if c in ("f", "F", "t", "T") and len(rest) >= 2:
            return (count, c, rest[1])

        if c in (
            "h", "j", "k", "l", "0", "$", "G", "D", "x", "J", "n", "N", "p", "P",
            "w", "b", "e", "^",

            "W", "B", "E", "{", "}", "(", ")", "%", "+", "-", "_", ";", ",",

            "~", "\x01", "\x18",

            "Y", "*", "#",

            "u", "\x12",

            ".",
        ):
            return (count, c, rest[1:])

        if c == "@" and len(rest) >= 2 and (
            ("a" <= rest[1] <= "z") or rest[1] == "@"
        ):
            return (count, rest[:2], rest[2:])
        return (count, "", rest)  

    _state = _vim_load_state(path, len(content))
    cursor = _state["cursor"]
    marks: dict = dict(_state["marks"])  
    last_edit = _state["last_edit"]      
    last_change = _state.get("last_change")  
    macros: dict = dict(_state.get("macros", {}))  
    macros.update(macros_pending)        
    last_replayed_macro: Optional[str] = None  
    _macro_replay_count: int = 0  
    prev_cursor = cursor                 
    log: List[str] = []
    last_search: Optional[tuple] = None  
    last_find: Optional[tuple] = None  
    register: str = ""  
    register_linewise: bool = False  

    undo_stack: List[tuple] = []
    redo_stack: List[tuple] = []


    _xundo_snapshot = _vim_load_undo_snapshot(path)  

    _entry_content = content
    _entry_cursor = cursor
    _entry_marks = dict(marks)






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


    _V_GOTO_LINE_OP = re.compile(r"^V(\d+)G([dyc])(.*)$", re.DOTALL)  




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



                if rest.startswith("%") or (rest and _is_ascii_int(rest[0])) or rest.startswith("."):
                    return ":" + rest
                return repl + rest

        m = _V_MOTION_LINE.match(act)
        if m is not None:
            n = int(m.group(1) or "1")
            op = m.group(3)

            if len(op) == 1:
                op = op + op
            return f"{n + 1}{op}{m.group(4)}"

        m = _V_GOTO_LINE_OP.match(act)
        if m is not None:
            return f":.,{m.group(1)}{m.group(2)}{m.group(3)}"
        return act









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

        return f"{count}{op}{motion}{tail}"




    _CC_TYPO = re.compile(r"^cc([ia])([wWsp\"'`()\[\]{}<>bBt])(.*)$", re.DOTALL)  

    def _rewrite_cc_typo(act: str) -> str:
        m = _CC_TYPO.match(act)
        if m is None:
            return act
        return f"c{m.group(1)}{m.group(2)}{m.group(3)}"

    raw_actions = [_rewrite_cc_typo(_rewrite_v_alias(_rewrite_v_char_alias(a))) for a in raw_actions]

    def _push_undo() -> None:

        undo_stack.append((content, cursor, dict(marks)))
        redo_stack.clear()

    for i, action in enumerate(raw_actions, 1):

        if action.startswith("__macro_def_"):
            reg = action[len("__macro_def_"):]
            log.append(f"  {i}. q{reg}...q (macro recorded, {len(macros.get(reg, ''))} chars)")
            continue

        count, verb, arg = _parse(action)

        if verb == "" and count != 1:
            return f"ERROR: action {i} '{action}': count without verb\n"


        if verb == "gg":
            cursor = 0
            log.append(f"  {i}. gg (BOF)")
        elif verb == "G":
            if _is_ascii_int(action.lstrip()[:1]):

                try:
                    cursor = _goto_line(content, count)
                except ValueError as e:
                    return f"ERROR: action {i} '{action}': {e}\n"
                log.append(f"  {i}. {count}G (line {count})")
            else:





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


            eol = _line_end(content, cursor)
            bol = _line_start(content, cursor)
            cursor = max(bol, eol - 1) if eol > bol else bol
            log.append(f"  {i}. $ (last char, cursor={cursor})")
        elif verb == "/":
            if not arg:
                return f"ERROR: action {i} '{action}': empty / pattern\n"
            pat, _gate_refusal, _gate_note = _pattern_gate(
                arg, check_saturation=False)
            if _gate_refusal:
                return f"ERROR: action {i} '{action}': {_gate_refusal[len('ERROR: '):]}"
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

                        raw_actions.insert(i, trail)
                        continue



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
            pat, _gate_refusal, _gate_note = _pattern_gate(
                arg, check_saturation=False)
            if _gate_refusal:
                return f"ERROR: action {i} '{action}': {_gate_refusal[len('ERROR: '):]}"
            idx = -1
            try:
                rx = re.compile(pat, re.MULTILINE)
                last = None


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


        elif verb in ("i", "a", "I", "A", "o", "O"):
            _push_undo()






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








            if (
                verb in ("o", "O")
                and len(arg) >= 3
                and arg[0] in ("?", "/")
                and "\n" not in arg
                and " " not in arg
                and "\t" not in arg
            ):

                _pat, _gate_refusal, _gate_note = _pattern_gate(
                    arg[1:], check_saturation=False)
                if _gate_refusal:
                    return f"ERROR: action {i} '{action}': {_gate_refusal[len('ERROR: '):]}"
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
            else:  
                bol = _line_start(content, cursor)
                content = content[:bol] + "\n" + content[bol:]
                pos = bol
            content = content[:pos] + text + content[pos:]

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


        elif verb == "x":
            _push_undo()
            end = min(len(content), cursor + count)
            content = content[:cursor] + content[end:]
            last_change = {"verb": "x", "count": count, "arg": ""}
            log.append(f"  {i}. {count}x ({end - cursor} chars)")
        elif verb == "dd":
            _push_undo()

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


        elif verb in ('ci"', "ci'", "ci(", "ci[", "ci{"):
            _push_undo()
            opener = verb[2]
            pairs = {'"': '"', "'": "'", "(": ")", "[": "]", "{": "}"}
            closer = pairs[opener]


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


        elif verb == "cc":
            bol = _line_start(content, cursor)
            end = bol
            for _ in range(count):
                nl = content.find("\n", end)
                if nl == -1:
                    end = len(content)
                    break
                end = nl + 1

            keep_nl = end > bol and content[end - 1] == "\n"
            slice_end = end - 1 if keep_nl else end
            text = _decode_escapes(arg)
            content = content[:bol] + text + content[slice_end:]
            cursor = bol + len(text)
            last_change = {"verb": "cc", "count": count, "arg": arg}
            preview = text if len(text) <= 30 else text[:27] + "..."
            log.append(f"  {i}. {count}cc{preview!r} (cursor={cursor})")


        elif verb == "J":
            _push_undo()
            joined = 0
            for _ in range(count):
                nl = content.find("\n", cursor)
                if nl == -1:
                    break

                k = nl + 1
                while k < len(content) and content[k] in (" ", "\t"):
                    k += 1
                sep = " " if k < len(content) and content[k] != "\n" else ""
                content = content[:nl] + sep + content[k:]
                cursor = nl + (1 if sep else 0)
                joined += 1
            log.append(f"  {i}. {count}J (joined {joined})")


        elif verb == "r":
            _push_undo()
            if not arg:
                return f"ERROR: action {i} '{action}': r needs a char\n"
            if cursor >= len(content):
                return f"ERROR: action {i} '{action}': r at EOF\n"
            content = content[:cursor] + arg[0] + content[cursor + 1:]
            log.append(f"  {i}. r{arg[0]!r}")


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
            else:  
                hit = content.rfind(target, bol, cursor)
                idx = hit + 1 if hit != -1 else -1
            if idx == -1:
                return f"ERROR: action {i} '{action}': {verb}{target!r} not found on line\n"
            cursor = idx
            last_find = (verb, target)
            log.append(f"  {i}. {verb}{target!r} → {cursor}")


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


        elif verb == "W":
            for _ in range(count):
                if cursor >= len(content):
                    break

                while cursor < len(content) and not content[cursor].isspace():
                    cursor += 1

                while cursor < len(content) and content[cursor].isspace():
                    cursor += 1
            log.append(f"  {i}. {count}W (cursor={cursor})")
        elif verb == "B":
            for _ in range(count):
                if cursor == 0:
                    break
                cursor -= 1

                while cursor > 0 and content[cursor].isspace():
                    cursor -= 1

                while cursor > 0 and not content[cursor - 1].isspace():
                    cursor -= 1
            log.append(f"  {i}. {count}B (cursor={cursor})")
        elif verb == "E":
            for _ in range(count):
                if cursor >= len(content):
                    break

                if (
                    cursor + 1 < len(content)
                    and not content[cursor].isspace()
                    and content[cursor + 1].isspace()
                ):
                    cursor += 1

                while cursor < len(content) and content[cursor].isspace():
                    cursor += 1

                while (
                    cursor + 1 < len(content)
                    and not content[cursor + 1].isspace()
                ):
                    cursor += 1
            log.append(f"  {i}. {count}E (cursor={cursor})")


        elif verb == "ge":
            def _is_w(ch: str) -> bool:
                return ch.isalnum() or ch == "_"
            for _ in range(count):
                if cursor == 0:
                    break
                cursor -= 1

                while cursor > 0 and content[cursor].isspace():
                    cursor -= 1


            log.append(f"  {i}. {count}ge (cursor={cursor})")
        elif verb == "gE":
            for _ in range(count):
                if cursor == 0:
                    break
                cursor -= 1
                while cursor > 0 and content[cursor].isspace():
                    cursor -= 1
            log.append(f"  {i}. {count}gE (cursor={cursor})")


        elif verb == "g_":

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


        elif verb == "}":
            for _ in range(count):




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

                    next_bol = nl + 1
                    next_eol = content.find("\n", next_bol)
                    if next_eol == -1:
                        next_eol = len(content)
                    if next_bol == next_eol:

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

                if bol == eol and bol > 0:
                    pos = bol - 1
                else:
                    pos = bol
                while pos > 0:
                    prev_eol = pos - 1  
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


        elif verb == ")":

            for _ in range(count):
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
                cursor = pos
            log.append(f"  {i}. {count}) (cursor={cursor})")
        elif verb == "(":

            for _ in range(count):
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
                cursor = found
            log.append(f"  {i}. {count}( (cursor={cursor})")


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


        elif verb in (";", ","):
            if last_find is None:
                return f"ERROR: action {i} '{action}': no previous f/F/t/T to repeat\n"
            fverb, ftarget = last_find

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

                if hit != -1 and hit == cursor + 1:
                    hit = content.find(ftarget, cursor + 2, eol)
                idx = hit - 1 if hit != -1 else -1
            else:  
                hit = content.rfind(ftarget, bol, cursor)
                if hit != -1 and hit == cursor - 1:
                    hit = content.rfind(ftarget, bol, cursor - 1)
                idx = hit + 1 if hit != -1 else -1
            if idx == -1:
                return f"ERROR: action {i} '{action}': {verb} no match\n"
            cursor = idx
            log.append(f"  {i}. {verb} → {cursor}")


        elif verb in ("n", "N"):
            if last_search is None:
                return f"ERROR: action {i} '{action}': no previous search for {verb}\n"
            spat, sdir = last_search
            spat, _gate_refusal, _gate_note = _pattern_gate(
                spat, check_saturation=False)
            if _gate_refusal:
                return f"ERROR: action {i} '{action}': {_gate_refusal[len('ERROR: '):]}"
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


        elif verb == ":s":
            _push_undo()




            sub_start = 0
            sub_end = len(content)
            if arg.startswith("\x1d"):
                close = arg.find("\x1d", 1)
                if close == -1:
                    return f"ERROR: action {i} '{action}': :s malformed range encoding\n"
                range_spec = arg[1:close]
                arg = arg[close + 1:]
                lines = content.split("\n")

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


                line_starts: List[int] = [0]
                for k, ch in enumerate(content):
                    if ch == "\n":
                        line_starts.append(k + 1)
                sub_start = line_starts[line_a - 1]


                if line_b < len(line_starts):
                    sub_end = line_starts[line_b]  
                else:
                    sub_end = len(content)
            if not arg or arg[0] != "/":
                return f"ERROR: action {i} '{action}': :s needs /PAT/REPL/[flags]\n"

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




            spat, _gate_refusal, _gate_note = _pattern_gate(spat)
            if _gate_refusal:
                return f"ERROR: action {i} '{action}': {_gate_refusal[len('ERROR: '):]}"
            flags_re = re.MULTILINE
            if "i" in sflags:
                flags_re |= re.IGNORECASE
            try:
                rx = re.compile(spat, flags_re)
            except re.error as e:





                literal_pat = _vim_literal_decode(spat)
                if literal_pat and literal_pat in content:
                    is_global = "g" in sflags
                    is_dry = "d" in sflags



                    srepl_dec_early = _vim_literal_decode(srepl) or _decode_escapes(srepl)
                    body = content[sub_start:sub_end]
                    occurrences = body.count(literal_pat)
                    if occurrences > 0 and not is_dry:




                        if is_global:
                            _n_reapplied = _count_already_applied(
                                body, literal_pat, srepl_dec_early)
                        else:
                            _idx0 = body.find(literal_pat)
                            _n_reapplied = (
                                1 if _idx0 != -1 and _edit_already_applied(
                                    body, literal_pat, srepl_dec_early, _idx0)
                                else 0
                            )
                        if _n_reapplied:
                            _bump_counter(_REAPPLY_COUNT, "cnt_reapply", _n_reapplied)
                        new_body = body.replace(
                            literal_pat,
                            srepl_dec_early,
                            -1 if is_global else 1,
                        )
                        content = content[:sub_start] + new_body + content[sub_end:]
                        cursor = min(cursor, len(content))
                        n_done = occurrences if is_global else 1
                        _reapplied_note = (
                            f" [{_n_reapplied} re-applied]" if _n_reapplied else ""
                        )
                        log.append(
                            f"  {i}. :s/{spat!r}/{srepl_dec_early!r}/{sflags} ({n_done} subs)"
                            f" [autocorrect: regex parse failed ({e}); literal mode → {literal_pat!r}]"
                            + _reapplied_note
                        )
                        continue
                return f"ERROR: action {i} '{action}': :s regex: {e}\n"
            is_dry = "d" in sflags
            n_max = 0 if "g" in sflags else 1





            is_global = "g" in sflags
            srepl_dec = _decode_escapes(srepl)



            srepl_safe = re.sub(r"\\(?=\D)", r"\\\\", srepl_dec)




            spat_decoded = _decode_escapes(spat)
            pattern_is_multiline = "\n" in spat_decoded




            _used_literal_repl = False
            def _run_sub(_rx):
                if pattern_is_multiline:

                    head = content[:sub_start]
                    tail = content[sub_end:]
                    body = content[sub_start:sub_end]
                    new_body, _n = _rx.subn(srepl_safe, body, count=n_max)
                    return head + new_body + tail, _n



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




            if n == 0:



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





                        _used_literal_repl = True
                        _literal_repl_pat = literal_pat
                        _literal_repl_new = srepl_dec
            if n == 0:
                near = _vim_nearest_literal_hint(content, spat, original=_before_content)
                return f"ERROR: action {i} '{action}': :s no match for {spat!r}{near}\n"
            if is_dry:


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




                _body_before = content[sub_start:sub_end]
                if _used_literal_repl:
                    if is_global:
                        _n_reapplied = _count_already_applied(
                            _body_before, _literal_repl_pat, _literal_repl_new)
                    else:
                        _idx0 = _body_before.find(_literal_repl_pat)
                        _n_reapplied = (
                            1 if _idx0 != -1 and _edit_already_applied(
                                _body_before, _literal_repl_pat,
                                _literal_repl_new, _idx0)
                            else 0
                        )
                else:
                    _n_reapplied = _vim_sub_reapplied_count(
                        _body_before, rx, srepl_safe, is_global,
                        pattern_is_multiline)
                if _n_reapplied:
                    _bump_counter(_REAPPLY_COUNT, "cnt_reapply", _n_reapplied)
                content = new_content
                cursor = min(cursor, len(content))
                _reapplied_note = (
                    f" [{_n_reapplied} re-applied]" if _n_reapplied else ""
                )
                log.append(
                    f"  {i}. :s/{spat!r}/{srepl_dec!r}/{sflags} ({n} subs)"
                    + autocorrect_hint + _reapplied_note
                )




        elif verb == ":goto":
            spec = arg
            if _is_ascii_int(spec):
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

                    bol = content.rfind("\n", 0, cursor) + 1
                    cursor = bol
                log.append(f"  {i}. :$ (goto last line)")
            else:  
                log.append(f"  {i}. :. (current line, no-op)")


        elif verb == ":noop":
            log.append(f"  {i}. :w (no-op — supertool writes atomically)")


        elif verb == ":r":
            _push_undo()



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

                try:
                    _safe_path(path_arg)
                except SecurityError as _se:
                    return f"ERROR: action {i} '{action}': :r {path_arg!r}: {_se}\n"
                try:



                    with open(path_arg, "r", encoding="utf-8",
                              errors="surrogateescape") as _fh:
                        file_text = _fh.read()
                except OSError as e:
                    detail = ""
                    if isinstance(e, FileNotFoundError):
                        detail = _r_missing_file_diagnostic(path_arg)
                    return (
                        f"ERROR: action {i} '{action}': :r failed to read "
                        f"{path_arg!r}: {e}{detail}\n"
                    )

            eol = _line_end(content, cursor)
            bol = _line_start(content, cursor)
            current_line = content[bol:eol]




            tail_after_eol = content[eol:].strip("\n \t")
            is_last_real_line = tail_after_eol == ""
            is_brace_line = current_line.strip() == "}"
            if is_last_real_line and is_brace_line:
                insert_pos = bol
            elif eol < len(content):

                insert_pos = eol + 1
            else:

                if content and not content.endswith("\n"):
                    content += "\n"
                insert_pos = len(content)
            if file_text and not file_text.endswith("\n"):
                file_text += "\n"
            content = content[:insert_pos] + file_text + content[insert_pos:]
            cursor = insert_pos
            log.append(f"  {i}. :r {path_arg!r} ({len(file_text)} chars inserted)")







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

            _vim_gate = _check_vim_shell_allowed()
            if _vim_gate is not None:
                return f"ERROR: action {i} '{action}': {_vim_gate}"
            if range_spec == "":

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


        elif verb == ":d":
            _push_undo()



            if not arg.startswith("\x1d"):
                return f"ERROR: action {i} '{action}': :d malformed encoding\n"
            close = arg.find("\x1d", 1)
            if close == -1:
                return f"ERROR: action {i} '{action}': :d malformed encoding\n"
            spec = arg[1:close]

            lines = content.split("\n")
            has_trailing_nl = lines and lines[-1] == ""
            total_lines = len(lines) - (1 if has_trailing_nl else 0)
            if total_lines == 0:
                return f"ERROR: action {i} '{action}': :d on empty buffer\n"


            if spec.startswith("g:") or spec.startswith("v:"):
                mode = spec[0]
                pat = spec[2:]
                if not pat:
                    return f"ERROR: action {i} '{action}': :{mode}/PAT/d needs non-empty PAT\n"





                pat, _gate_refusal, _gate_note = _pattern_gate(pat)
                if _gate_refusal:
                    return f"ERROR: action {i} '{action}': {_gate_refusal[len('ERROR: '):]}"
                try:
                    rx = re.compile(pat)
                except re.error as e:
                    return f"ERROR: action {i} '{action}': :{mode}/PAT/d regex: {e}\n"
                body_lines = lines[:-1] if has_trailing_nl else lines
                if mode == "g":
                    kept = [ln for ln in body_lines if rx.search(ln) is None]
                    n_deleted = len(body_lines) - len(kept)
                else:  
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


            cursor_line, _ = _offset_to_line_col(content, cursor)

            body_lines_for_pat = lines[:-1] if has_trailing_nl else lines

            def _resolve_d(addr: str) -> int:


                if addr.startswith("/") and addr.endswith("/") and len(addr) >= 2:
                    pat, _gate_refusal, _gate_note = _pattern_gate(
                        addr[1:-1], check_saturation=False)
                    if _gate_refusal:
                        raise ValueError(
                            f"bad pattern {addr!r}: "
                            f"{_gate_refusal[len('ERROR: '):].rstrip(chr(10))}"
                        )
                    try:
                        rxp = re.compile(pat)
                    except re.error as e:
                        raise ValueError(f"bad pattern {addr!r}: {e}") from e

                    for ln_idx in range(cursor_line - 1, len(body_lines_for_pat)):
                        if rxp.search(body_lines_for_pat[ln_idx]):
                            return ln_idx + 1

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

            line_starts: List[int] = [0]
            for k, ch in enumerate(content):
                if ch == "\n":
                    line_starts.append(k + 1)
            del_start = line_starts[line_a - 1]
            if line_b < len(line_starts):
                del_end = line_starts[line_b]  
            else:
                del_end = len(content)
            content = content[:del_start] + content[del_end:]
            cursor = min(del_start, len(content))
            log.append(f"  {i}. :{spec}d ({line_b - line_a + 1} lines deleted)")


        elif verb == "Y":
            eol = _line_end(content, cursor)
            register = content[cursor:eol]
            register_linewise = False
            log.append(f"  {i}. Y ({len(register)} chars)")


        elif verb == "gJ":
            joined = 0
            for _ in range(count):
                nl = content.find("\n", cursor)
                if nl == -1:
                    break

                content = content[:nl] + content[nl + 1:]
                cursor = nl
                joined += 1
            log.append(f"  {i}. {count}gJ (joined {joined})")


        elif verb in ("*", "#"):

            if cursor >= len(content) or not (
                content[cursor].isalnum() or content[cursor] == "_"
            ):

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
            else:  
                last = None
                for m in rx.finditer(content[:ws]):
                    last = m
                if last is None:
                    return f"ERROR: action {i} '{action}': # no earlier match for {word!r}\n"
                cursor = last.start()
                last_search = (pat, "?")
            log.append(f"  {i}. {verb} ({word!r} → {cursor})")


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

                if target < 0 or target > total_lines:
                    return (
                        f"ERROR: action {i} '{action}': {verb} target {target} out of "
                        f"bounds (0..{total_lines})\n"
                    )
                segment = body_lines[line_a - 1:line_b]
                if verb == ":move":

                    if line_a - 1 <= target <= line_b:
                        return (
                            f"ERROR: action {i} '{action}': :move target {target} "
                            f"inside source range {line_a}..{line_b}\n"
                        )
                    remaining = body_lines[:line_a - 1] + body_lines[line_b:]

                    adj_target = target - len(segment) if target > line_b else target
                    new_body = remaining[:adj_target] + segment + remaining[adj_target:]
                else:  
                    new_body = body_lines[:target] + segment + body_lines[target:]
                content = "\n".join(new_body) + ("\n" if has_trailing_nl else "")
                cursor = min(cursor, len(content))
                log.append(
                    f"  {i}. :{range_spec}{verb[1:]} {target} ({len(segment)} lines)"
                )

            elif verb == ":norm":
                _push_undo()

                cmds = body
                if not cmds:
                    return f"ERROR: action {i} '{action}': :norm needs commands\n"










                try:
                    _atomic_write(path, content)
                except OSError as e:
                    return f"ERROR: failed to write {path}: {e}\n"
                total_run = 0
                cur_line_iter = line_a


                line_b_eff = line_b
                while cur_line_iter <= line_b_eff:

                    sub_script = f"{cur_line_iter}G\x1b{cmds}"
                    sub_out = op_vim(path, sub_script)
                    if sub_out.startswith("ERROR"):
                        return f"ERROR: action {i} '{action}': :norm at line {cur_line_iter}: {sub_out}"
                    total_run += 1
                    cur_line_iter += 1

                    with open(path, "r", encoding="utf-8",
                              errors="surrogateescape") as _fh:
                        new_content = _fh.read()
                    new_lines_full = new_content.split("\n")
                    new_total = len(new_lines_full) - (1 if new_lines_full and new_lines_full[-1] == "" else 0)
                    line_b_eff = line_b + (new_total - total_lines)



                with open(path, "r", encoding="utf-8",
                          errors="surrogateescape") as _fh:
                    content = _fh.read()
                cursor = min(cursor, len(content))
                log.append(f"  {i}. :{range_spec}norm {cmds!r} ({total_run} lines)")


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

                k = 0
                while k < len(ln) and ln[k] == "\t":
                    k += 1
                new_lines.append(spaces * k + ln[k:])
            content = "\n".join(new_lines)
            cursor = min(cursor, len(content))
            log.append(f"  {i}. :retab {width}")


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

            pass


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

                arg = arg[1:]
            else:
                op = verb[0]
                motion = verb[1:]
            linewise = False
            if motion == "G":

                start = _line_start(content, cursor)
                end = len(content)
                linewise = True
            elif motion == "gg":

                start = 0
                line_eol = _line_end(content, cursor)
                end = line_eol + 1 if line_eol < len(content) else len(content)
                linewise = True
            elif motion == "^":

                bol = _line_start(content, cursor)
                eol = _line_end(content, cursor)
                first_nb = bol
                while first_nb < eol and content[first_nb] in (" ", "\t"):
                    first_nb += 1
                if first_nb <= cursor:
                    start, end = first_nb, cursor
                else:

                    start, end = cursor, cursor
            elif motion == "h":
                start = max(0, cursor - 1)
                end = cursor
            elif motion == "l":
                start = cursor
                end = min(len(content), cursor + 1)
            elif motion == "$":

                start = cursor
                end = _line_end(content, cursor)
            elif motion == "0":

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
                else:  
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

                start = _line_start(content, cursor)
                first_nl = content.find("\n", cursor)
                if first_nl == -1:
                    end = len(content)
                else:
                    second_nl = content.find("\n", first_nl + 1)
                    end = second_nl + 1 if second_nl != -1 else len(content)
                linewise = True
            elif motion == "k":

                bol = _line_start(content, cursor)
                if bol == 0:

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
                pat, _gate_refusal, _gate_note = _pattern_gate(
                    arg, check_saturation=False)
                if _gate_refusal:
                    return f"ERROR: action {i} '{action}': {_gate_refusal[len('ERROR: '):]}"
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
                else:  
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
                    start, end = cursor, pos + 1  
            elif motion in ("ge", "gE"):



                pos = cursor
                if pos > 0:
                    pos -= 1
                    while pos > 0 and content[pos].isspace():
                        pos -= 1
                start, end = pos + 1, cursor
                if end < start:
                    start, end = end, start
            elif motion == "g_":

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

                start = _line_start(content, cursor)
                first_nl = content.find("\n", cursor)
                if first_nl == -1:
                    end = len(content)
                else:
                    second_nl = content.find("\n", first_nl + 1)
                    end = second_nl + 1 if second_nl != -1 else len(content)
                linewise = True
            elif motion == "-":

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
                    start, end = cursor, target + 1  
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
                    start, end = target, cursor + 1  
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
                    start, end = cursor, hit + 1  
                elif fverb == "F":
                    hit = content.rfind(ftarget, bol, cursor)
                    if hit == -1:
                        return f"ERROR: action {i} '{action}': {motion} no match\n"
                    start, end = hit, cursor
                elif fverb == "t":
                    hit = content.find(ftarget, cursor + 1, eol)
                    if hit == -1:
                        return f"ERROR: action {i} '{action}': {motion} no match\n"
                    start, end = cursor, hit  
                else:  
                    hit = content.rfind(ftarget, bol, cursor)
                    if hit == -1:
                        return f"ERROR: action {i} '{action}': {motion} no match\n"
                    start, end = hit + 1, cursor
            elif motion == "?":
                if not arg:
                    return f"ERROR: action {i} '{action}': {verb} empty pattern\n"
                pat, _gate_refusal, _gate_note = _pattern_gate(
                    arg, check_saturation=False)
                if _gate_refusal:
                    return f"ERROR: action {i} '{action}': {_gate_refusal[len('ERROR: '):]}"
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

                cursor = start
            else:
                register = slice_
                register_linewise = linewise
                if op == "d":
                    content = content[:start] + content[end:]
                    cursor = min(start, len(content))
                elif op == "c":



                    if motion in ("/", "?"):
                        text = ""
                    else:
                        text = _decode_escapes(arg) if arg else ""
                    content = content[:start] + text + content[end:]
                    cursor = start + len(text)
            log.append(f"  {i}. {verb} ({len(slice_)} chars{', linewise' if linewise else ''})")


        elif verb == "~":
            _push_undo()
            end_pos = min(len(content), cursor + count)
            seg = content[cursor:end_pos]
            content = content[:cursor] + seg.swapcase() + content[end_pos:]
            cursor = end_pos
            log.append(f"  {i}. {count}~ ({len(seg)} chars toggled)")


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


        elif verb in ("\x01", "\x18"):
            _push_undo()


            eol = _line_end(content, cursor)
            p = cursor
            if p >= eol or not _is_ascii_int(content[p]):
                while p < eol and not _is_ascii_int(content[p]):
                    p += 1
            if p >= eol or not _is_ascii_int(content[p]):
                return f"ERROR: action {i} '{action}': no number on line\n"

            start_d = p
            while start_d > 0 and _is_ascii_int(content[start_d - 1]):
                start_d -= 1
            end_d = p
            while end_d < eol and _is_ascii_int(content[end_d]):
                end_d += 1

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


        elif verb == "p":
            _push_undo()
            if not register:
                return f"ERROR: action {i} '{action}': p with empty register\n"
            if register_linewise:
                eol = _line_end(content, cursor)

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
            else:  
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

                register = slice_
                register_linewise = False
                content = content[:start] + text + content[end:]
                cursor = start + len(text)
                log.append(f"  {i}. {verb}{target!r} ({len(slice_)} chars → {len(text)})")


        elif verb in (">>", "<<", "==") or (
            len(verb) >= 2 and verb[0] in "><=" and verb not in (">>", "<<", "==")
        ):
            _push_undo()
            op = verb[0]

            cur_line, _ = _offset_to_line_col(content, cursor)
            total_lines = content.count("\n") + 1



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

                        la, _ = _offset_to_line_col(content, ts)
                        lb, _ = _offset_to_line_col(content, max(ts, te - 1))
                        line_a, line_b = la, lb
                    except _TextObjectError as e:
                        return f"ERROR: action {i} '{action}': {e}\n"
                else:

                    if motion == "G":
                        target = len(content) - (1 if content.endswith("\n") else 0)
                    elif motion == "gg":
                        target = 0
                    elif motion == "j":


                        _mc_str = arg.lstrip()
                        _mc = int(_mc_str) if _is_ascii_int(_mc_str) else 1
                        pos = cursor
                        for _ in range(_mc):
                            nl = content.find("\n", pos)
                            if nl == -1:
                                break
                            pos = nl + 1
                        target = pos
                    elif motion == "k":
                        _mc_str = arg.lstrip()
                        _mc = int(_mc_str) if _is_ascii_int(_mc_str) else 1
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

                        target = cursor
                    la, _ = _offset_to_line_col(content, min(cursor, target))
                    lb, _ = _offset_to_line_col(content, max(cursor, target))
                    line_a, line_b = la, lb

            line_a = max(1, line_a)
            line_b = min(total_lines, line_b)
            if line_a > line_b:
                line_a, line_b = line_b, line_a

            lines = content.split("\n")

            trailing_empty = lines and lines[-1] == ""
            real_lines = lines[:-1] if trailing_empty else lines
            shift = "    "
            for ln_idx in range(line_a - 1, min(line_b, len(real_lines))):
                if op == ">":

                    if real_lines[ln_idx] == "":
                        continue

                    real_lines[ln_idx] = shift * indent_repeat + real_lines[ln_idx]
                elif op == "=":



                    ref_depth = 0  
                    for ref_idx in range(ln_idx - 1, -1, -1):
                        ref = real_lines[ref_idx]
                        if ref.strip():
                            raw_indent = ref[: len(ref) - len(ref.lstrip(" \t"))]
                            if "\t" in raw_indent:
                                ref_depth = raw_indent.count("\t")
                            else:
                                ref_depth = len(raw_indent) // 4
                            break

                    target_raw = real_lines[ln_idx]
                    target_prefix = target_raw[: len(target_raw) - len(target_raw.lstrip(" \t"))]
                    if "\t" in target_prefix:
                        new_indent = "\t" * ref_depth
                    else:
                        new_indent = "    " * ref_depth
                    new_line = new_indent + target_raw.lstrip(" \t")
                    if new_line == target_raw:
                        continue  
                    real_lines[ln_idx] = new_line
                else:
                    s = real_lines[ln_idx]
                    if s.startswith("\t"):
                        real_lines[ln_idx] = s[1:]
                    else:

                        k = 0
                        while k < 4 and k < len(s) and s[k] == " ":
                            k += 1
                        real_lines[ln_idx] = s[k:]
            new_lines2 = real_lines + ([""] if trailing_empty else [])
            content = "\n".join(new_lines2)

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


        elif verb == "R":
            _push_undo()
            text = _decode_escapes(arg)

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


        elif len(verb) == 2 and verb[0] == "m" and (
            ("a" <= verb[1] <= "z") or ("A" <= verb[1] <= "Z")
        ):
            mark_char = verb[1].lower()  
            marks[mark_char] = cursor
            log.append(f"  {i}. m{verb[1]} (mark={cursor})")


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
                        else:  
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


        elif action.startswith("__macro_def_"):
            reg = action[len("__macro_def_"):]


            log.append(f"  {i}. q{reg}...q (macro recorded, {len(macros.get(reg, ''))} chars)")


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
                    if _is_ascii_int(_body_norm[_bi]) and _body_norm[_bi] != "0":
                        while _bverb_pos < _bn and _is_ascii_int(_body_norm[_bverb_pos]):
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



                _macro_replay_count += count
                if _macro_replay_count > 100:
                    return f"ERROR: action {i} '@{reg_ch}': macro recursion depth limit 100 reached (likely infinite loop)\n"
                splice = _body_actions * count
                for _s in reversed(splice):
                    raw_actions.insert(i, _s)
                last_replayed_macro = reg_ch
                log.append(f"  {i}. @{reg_ch} x{count} ({len(splice)} actions spliced)")

        elif verb == "u":

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

        elif verb == "\x12":  
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



            _COMMON_VERBS = [
                "gg", "G", "0", "^", "$", "h", "j", "k", "l", "w", "b", "e",
                "W", "B", "E", "{", "}", "(", ")", "%", "/", "?", "n", "N",
                "i", "a", "I", "A", "o", "O", "s", "S", "C", "r", "x", "X",
                "J", "p", "P", "~",
                "dd", "D", "dw", "yy", "Y", "yw", "cc", "cw", "ciw",
                "ci\"", "ci'", "ci(", "ci[", "ci{",
                ":s", ":%s", ":d", ":r", ":g",
            ]

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




        lint_out += "[note] file modified despite syntax fail — review or restore manually. Configure a validator with rollback_on_fail for auto-rollback.\n"
    elif lint_out.startswith(_LINT_DECLINE_PREFIXES):





        lint_out += "[note] file modified and NOT checked — the syntax check never returned a verdict; review or restore manually. Configure a validator with rollback_on_fail for auto-rollback.\n"
    out.append(lint_out)
    return "".join(out)
