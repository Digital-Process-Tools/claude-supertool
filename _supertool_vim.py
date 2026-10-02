"""Vim implementation split out of _supertool.py (#2706).

Imported lazily on the first `vim` dispatch call by the op_vim() stub
left in _supertool.py. Shared helpers below resolve through `_st` live,
at call time, not at import time, so a test's
`monkeypatch.setattr(supertool, "_which_excluding_cwd", fake)` still
reaches the patched version here (supertool.py's #931 shim makes
`supertool` and `_supertool` the same module object).
"""

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

def _vim_sub_reapplied_count(
    body: str, rx: "re.Pattern", srepl_safe: str, is_global: bool,
    pattern_is_multiline: bool,
) -> int:
    """How many of a `:s` run's substitutions are already sitting in the
    buffer as an earlier run's output (#2358) -- `vim`'s own version of
    #938's `_count_already_applied`.

    #938's test is `old in new`, and both are FIXED strings there -- `edit`
    and `replace` never see a pattern. `:s`'s `old` is a regex, and asking
    whether a regex SOURCE string ("is `(foo)-(bar)` contained in `\\2-\\1`")
    has no meaning once the pattern holds groups, backreferences, anchors or
    classes -- which is the reason #2358 was filed as its own decision rather
    than a mechanical port.

    The reduction: per MATCH rather than per pattern. `m.group(0)` is the
    literal text this run is about to replace and `m.expand(srepl_safe)` is
    the literal text it is about to write in its place -- `.expand` resolves
    any backreference using THAT match, so it is exact even when `srepl`
    holds `\\1`, `\\2`, ... Both are now fixed strings for this one
    occurrence, which is exactly the positional-containment test #938
    already proved: is `m.group(0)` at this position already sitting inside
    a copy of `m.expand(srepl_safe)` a previous run wrote. Detection is
    therefore NOT limited to a backreference-free subset -- it only needs
    the match, never the pattern's own source text.

    Walks the SAME matches `_run_sub` is about to substitute (whole-buffer
    for a pattern that explicitly wants newlines, per-line and
    first-per-line-vs-every-match otherwise), so the count lines up with
    what that call actually changes rather than a re-derived quantity.

    Collects every `(index, old, new)` triple first, THEN decides how to
    check them, rather than calling `_edit_already_applied` once per match
    unconditionally (self-review, #2358 perf): that function's own
    `content.find(new)` is an O(n) scan from scratch, so paying it once per
    match is O(n*m) -- quadratic once match count scales with the file's own
    size, on an ordinary `:s/X/X_/g`-shaped global substitution with nothing
    even wrong to report (measured 7.7s at 24k matches against `replace`'s
    0.56s over the same size, on a clean first application). When `new` is
    the SAME literal text for every match -- true whenever `srepl` holds no
    backreference, so nothing about it varies with what a match captured --
    that reduces to exactly `_count_already_applied`'s own batched shape:
    one O(n) scan for `new`'s occurrences, then O(log k) per match via
    `bisect`. Only a backreference that makes `new` genuinely differ across
    matches falls back to the per-match scan, which is the case that also
    cannot be batched: the search text is not the same address twice.
    """
    matches: List[Tuple[int, str, str]] = []
    if pattern_is_multiline:
        # `_run_sub`'s own multiline branch caps with `rx.subn(..., count=
        # n_max)`, `n_max=1` when `g` is absent -- only the leftmost match
        # in the whole buffer is ever substituted. Walking every match here
        # without the same cap reported MORE re-applied occurrences than
        # substitutions actually made (self-review, #2358).
        for m in rx.finditer(body):
            matches.append((m.start(), m.group(0), m.expand(srepl_safe)))
            if not is_global:
                break
    else:
        has_trailing_nl = body.endswith("\n")
        body_lines = body.split("\n")
        if has_trailing_nl:
            body_lines = body_lines[:-1]
        # A per-line MATCH is checked against the FULL body, not the one
        # line it matched in -- a replacement is free to write a newline of
        # its own (a `:s/PAT/line1\nline2/` appends a whole new line), and
        # that written text is no longer confined to the line the match
        # came from. Checking containment against `ln` alone can never see
        # a `new` that spans a line boundary, so the offset is translated
        # to a body-level index instead.
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
        # Backreference-driven: `new` genuinely differs per match, so each
        # one needs its own containment scan -- the same per-call cost
        # `_edit_already_applied` already pays for a single `edit`/`replace`
        # action, just repeated here across however many matches there are.
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
        # The rightmost `new` occurrence at or before `idx` is the strongest
        # candidate -- the same reasoning `_count_already_applied` uses.
        pos = bisect.bisect_right(new_idxs, idx) - 1
        if pos >= 0 and new_idxs[pos] >= lo:
            reapplied += 1
    return reapplied

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
    elif _is_ascii_int(base):
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
        # Atomic by contract: an errored vim op applied none of its actions, so
        # every one of them is a decline the footer has to carry (#680).
        _bump_counter(_SKIP_COUNT, "cnt_skip")
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

    Re-running `:s` (#2358):
        A `:s/PAT/REPL/` that writes text REPL already produced is disclosed
        the same way `edit`/`replace` disclose it (#938) — `K re-applied` in
        the `[result]` footer, `[N re-applied]` on the action's own log
        line — never refused. Detection compares, per match, the literal
        text a match consumed against the literal text it is about to
        become (any backreference resolved for that match), so it is not
        limited to a literal PAT: `:s/(a)-(b)/\\1-\\2-X/` run twice is caught
        the same way `:s/foo/foo-bar/` run twice is. BEST-EFFORT, not
        exhaustive: a quantified group that re-matches its own PRIOR output
        as one larger capture (`:s/(\\w+)/\\1\\1/` re-applied to
        already-doubled text) is not caught, because that match's own text
        no longer equals what any one run wrote.

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
        return _path_not_found(path, label="file", op="vim", creates=True)
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
            while j < n and _is_ascii_int(normalized[j]):
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
            while j < n and _is_ascii_int(normalized[j]):
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
                if _is_ascii_int(normalized[_sv]) and normalized[_sv] != "0":
                    while _sv < n and _is_ascii_int(normalized[_sv]):
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
        # Parse count: leading digits, but not if `0` alone (BOL verb).
        verb_pos = i
        if _is_ascii_int(normalized[i]) and normalized[i] != "0":
            while verb_pos < n and _is_ascii_int(normalized[verb_pos]):
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
        if _is_ascii_int(action[0]) and action[0] != "0":
            while i < len(action) and _is_ascii_int(action[i]):
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
            while j < len(rest) and (_is_ascii_int(rest[j]) or rest[j] in ".$+-"):
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
                    while j < len(rest) and (_is_ascii_int(rest[j]) or rest[j] in ".$+-"):
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
                if _is_ascii_int(spec) or spec in ("$", "."):
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
            while mi < len(rest) and _is_ascii_int(rest[mi]):
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
                if rest.startswith("%") or (rest and _is_ascii_int(rest[0])) or rest.startswith("."):
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
            if _is_ascii_int(action.lstrip()[:1]):
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
            pat, _gate_refusal, _gate_note = _pattern_gate(
                arg, check_saturation=False)
            if _gate_refusal:
                return f"ERROR: action {i} '{action}': {_gate_refusal[len('ERROR: '):]}"
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
            # check_saturation stays on (the default) here: unlike the search
            # motions above, :s substitutes every match it finds, so a
            # saturating pattern (a bare `|`) is not a harmless single-match
            # position -- it silently rewrites the whole buffer. #2573 review.
            spat, _gate_refusal, _gate_note = _pattern_gate(spat)
            if _gate_refusal:
                return f"ERROR: action {i} '{action}': {_gate_refusal[len('ERROR: '):]}"
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
                        # #2358: same positional-containment test #938 proved
                        # for `edit`/`replace`, against the PRE-write body --
                        # this branch's `old`/`new` are already fixed literal
                        # strings, so no per-match reduction is needed.
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
            # Set once, here, for the #2358 reapply check below -- the two
            # branches further down that also set `is_global` (the early
            # parse-error literal fallback, and this path's own literal
            # fallback) either `continue` first or agree with this value, so
            # neither can leave it unset for a direct regex match.
            is_global = "g" in sflags
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
            # #2358: which comparison the reapply check below uses -- regex
            # per-match (the general case) or a fixed literal pair (the
            # literal-fallback recovery just below, where `rx` no longer
            # describes what actually matched).
            _used_literal_repl = False
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
                        # #2358: `rx` never matched here -- this recovery used
                        # a plain string replace -- so the reapply check below
                        # has to compare the same fixed literal pair rather
                        # than trying to walk `rx` against text it did not
                        # produce.
                        _used_literal_repl = True
                        _literal_repl_pat = literal_pat
                        _literal_repl_new = srepl_dec
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
                # #2358: computed against the PRE-write body, exactly like
                # #938's `_count_already_applied` -- a later occurrence's
                # index would otherwise be read against text this same
                # write already shifted.
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

        # --- ex line goto: bare `:N`, `:$`, `:.` (no command after range) ---
        # Real vim: `:N\n` jumps to line N. Kevin types this instead of `NG`.
        # arg is the address spec (digits | `$` | `.`).
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
                    detail = ""
                    if isinstance(e, FileNotFoundError):
                        detail = _r_missing_file_diagnostic(path_arg)
                    return (
                        f"ERROR: action {i} '{action}': :r failed to read "
                        f"{path_arg!r}: {e}{detail}\n"
                    )
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
                # check_saturation stays on (the default) here: :g/:v delete
                # every matching (or non-matching) line, so a saturating
                # pattern (a bare `|`) is not a harmless single-match
                # position -- it silently deletes the whole buffer. #2573
                # review.
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
            if p >= eol or not _is_ascii_int(content[p]):
                while p < eol and not _is_ascii_int(content[p]):
                    p += 1
            if p >= eol or not _is_ascii_int(content[p]):
                return f"ERROR: action {i} '{action}': no number on line\n"
            # walk back to leading minus if adjacent (vim treats -42 as -42)
            start_d = p
            while start_d > 0 and _is_ascii_int(content[start_d - 1]):
                start_d -= 1
            end_d = p
            while end_d < eol and _is_ascii_int(content[end_d]):
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
