"""_supertool_mcp -- LSP ops (diag, hover, rename, resolve), workspace, and the
MCP client, split out of _supertool.py (#2706).

Loaded by `_load_part("_supertool_mcp")` from inside `_supertool.py`, at the
exact source position the LSP block used to occupy: a plain `exec(code,
globals())` via `_load_part`, not a real `import`. Every function defined
below therefore has `__globals__ is _supertool.__dict__` once loaded, so
every existing `monkeypatch.setattr(supertool, "_mcp_ensure_server", ...)`
(39 patch sites measured across `_mcp_ensure_server`, `_MCP_STOP_SCRIPT`,
`_mcp_stop_server`) keeps reaching the code it patches -- a real module
split would have broken every one of them.

Not importable on its own. `_load_part` is the only legitimate loader: it
puts `_load_part` itself into the globals this file executes against before
running it, which is exactly the marker the guard below checks for. A bare
`import _supertool_mcp` or `python3 _supertool_mcp.py` gets this module's own
fresh globals(), which has no such name, and refuses with a clear ImportError
rather than failing later with a NameError on the first name this file
assumes `_supertool.py` already defined (Any, Dict, List, NamedTuple,
Optional, Tuple, Iterable, os, re, json, sys, time, socket, threading, signal,
subprocess, importlib, `_mcp_specs`, ...).

The two original spans (LSP ops + workspace, then MCP client primitives) are
concatenated into one part, loaded with a single `_load_part()` call at the
first span's original position -- the unrelated code that used to sit
between them in _supertool.py is untouched and stays exactly where it is.
One part rather than two: neither span's top-level code (only function
bodies, resolved lazily at call time) reaches into the other at module-exec
time, and nothing in the code between the two original spans registers an
`atexit`/`signal` handler whose LIFO order this reordering could disturb --
both checked directly against the commit this was cut from (6126fdbb) before
merging them into one file.

`_AUTO_CWD_MARKER`/`_project_root_above_cwd`/`_auto_cwd_root`, which sat
physically between the MCP span and the next function, are core and stay in
_supertool.py -- not part of this file.

`_extract_refs_from_mcp_result`, `_extract_symbols_from_mcp_result` and
`_extract_path_from_mcp_result` lived inside the MCP span in the original
file but are consumed only by `op_resolve`/`op_workspace`, never by MCP-
internal code (each appeared exactly once -- its own `def` -- within the MCP
span). They sit with the LSP/workspace code below for that reason.
"""
from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_mcp.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )

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
