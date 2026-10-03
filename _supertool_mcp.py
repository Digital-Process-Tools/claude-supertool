
















































from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_mcp.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )

import socket  



















_MCP_INFRA_DEFAULT_PATTERNS = ("orchestrator timeout", "timed out after")


def _mcp_result_text(result: object) -> str:

    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        texts = [item.get("text", "") for item in content
                 if isinstance(item, dict) and item.get("type") == "text"]
        return "\n".join(t for t in texts if t)
    return ""


def _mcp_result_is_infra(result: object, patterns: Iterable[str]) -> bool:







    if not isinstance(result, dict):
        return False
    if result.get("isError"):
        return True
    text = _mcp_result_text(result).lower()
    return bool(text) and any(p.lower() in text for p in patterns)


def _mcp_call_or_message(op_name: str, file_path: str, args: dict) -> str:







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

    infra_patterns = _mcp_specs.get(server_name, {}).get(
        "infra_patterns", _MCP_INFRA_DEFAULT_PATTERNS)
    if _mcp_result_is_infra(result, infra_patterns):
        text = _mcp_result_text(result).strip() or "infra condition"
        return f"{op_name}: {text}\n"

    text = _mcp_result_text(result)
    if text:
        return text.rstrip("\n") + "\n"
    return json.dumps(result, indent=2) + "\n"


def op_diag(file_path: str) -> str:

    return _mcp_call_or_message("diag", file_path, {"file_path": os.path.abspath(file_path) if file_path else ""})


def op_hover(symbol: str, file_path: str) -> str:









    if not symbol or not file_path:
        return "hover: usage hover:SYMBOL:FILE\n"
    abs_file = os.path.abspath(file_path)



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





    try:
        with open(target_file, "rb") as f:
            src_lines = f.readlines()
        if 0 < line <= len(src_lines):
            src = src_lines[line - 1].decode("utf-8", errors="replace")
            m = re.search(r"\b" + re.escape(symbol) + r"\b", src)
            if m:
                character = m.start() + 1  
    except OSError:
        pass


    return _mcp_call_or_message("hover", file_path, {
        "file_path": os.path.abspath(target_file),
        "line": line, "character": character,
    })


def op_rename(old_symbol: str, new_symbol: str, file_path: str) -> str:





    if not old_symbol or not new_symbol:
        return "rename: usage rename:OLD_SYMBOL:NEW_SYMBOL:FILE\n"
    return _mcp_call_or_message("rename", file_path, {
        "symbol_name": old_symbol, "query": old_symbol, "new_name": new_symbol,
        "file_path": os.path.abspath(file_path) if file_path else "",
    })







_WORKSPACE_COMMON_SYMBOLS = frozenset({
    "index", "main", "init", "__init__", "app", "base", "utils", "helpers",
    "helper", "config", "settings", "common", "core", "util", "test",
    "tests", "setup", "models", "model", "views", "view", "routes",
})




_EXT_FAMILIES: Dict[str, tuple] = {
    ".ts":   (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
    ".tsx":  (".ts", ".tsx", ".js", ".jsx"),
    ".js":   (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"),
    ".jsx":  (".js", ".jsx", ".ts", ".tsx"),
    ".mjs":  (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"),
    ".cjs":  (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"),
    ".php":  (".php",),  
    ".py":   (".py", ".pyi"),
    ".pyi":  (".py", ".pyi"),
}






def op_resolve(symbol: str, from_file: Optional[str] = None, _cache: Optional[dict] = None) -> str:





















    if not symbol:
        return "resolve: empty symbol\n"


    if _cache is not None:
        cache_key = (symbol, from_file)
        if cache_key in _cache:
            return _cache[cache_key]

    result = _op_resolve_inner(symbol, from_file)

    if _cache is not None:
        _cache[cache_key] = result

    return result


def _op_resolve_inner(symbol: str, from_file: Optional[str] = None) -> str:

    excl = _get_exclude_paths("resolve")



    if from_file:
        route = _mcp_route(from_file, "resolve")
        if route:
            server_name, mcp_tool = route
            server = _mcp_ensure_server(server_name)
            if server is not None:
                try:


                    result = _mcp_call(server_name, mcp_tool, {
                        "symbol_name": symbol, "file_path": from_file, "query": symbol,
                    })
                    if result is not None:
                        path = _extract_path_from_mcp_result(result)
                        if path:
                            return f"{symbol} → {path}\n"
                except (MCPServerError, MCPTimeout):
                    pass


    if "\\" in symbol:
        fqn_path = symbol.replace("\\", "/")
        basename = fqn_path.rsplit("/", 1)[-1]


        for ext in (".class.php", ".php"):
            suffix = f"/{fqn_path}{ext}"
            hits = _glob_files(f"**/{basename}{ext}", excl)
            for h in hits:
                norm = os.path.normpath(h).replace(os.sep, "/")
                if norm.endswith(suffix) or norm == f"{fqn_path}{ext}":
                    return f"{symbol} → {_safe_relpath(h)}\n"
        return f"{symbol} → not found\n"




    if re.match(r"^\.+\w*(?:\.\w+)*\Z", symbol) or symbol in (".", ".."):  
        if not from_file:
            return f"{symbol} → external\n"
        base_dir = os.path.dirname(os.path.abspath(from_file))



        m = re.match(r"^(\.+)(.*)", symbol)
        if not m:
            return f"{symbol} → external\n"
        dots, rest = m.group(1), m.group(2)

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

            pkg_init = os.path.join(target_dir, module_path, "__init__.py")
            if os.path.isfile(pkg_init):
                rel = _safe_relpath(pkg_init)
                return f"{symbol} → {rel}\n"
            return f"{symbol} → not found\n"
        else:

            pkg_init = os.path.join(target_dir, "__init__.py")
            if os.path.isfile(pkg_init):
                rel = _safe_relpath(pkg_init)
                return f"{symbol} → {rel}\n"
            return f"{symbol} → not found\n"


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


    if symbol.startswith("./") or symbol.startswith("../"):
        base = symbol

        if not os.path.splitext(base)[1]:
            for ext in (".ts", ".tsx", ".js", ".jsx", ".py", ".php"):
                candidate = base + ext
                if os.path.isfile(candidate):
                    rel = _safe_relpath(candidate)
                    return f"{symbol} → {rel}\n"

            candidate = base + ".class.php"
            if os.path.isfile(candidate):
                rel = _safe_relpath(candidate)
                return f"{symbol} → {rel}\n"
        else:
            if os.path.isfile(base):
                rel = _safe_relpath(base)
                return f"{symbol} → {rel}\n"
        return f"{symbol} → not found\n"


    if re.match(r"^[A-Za-z0-9_-]+\Z", symbol):  
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


    return f"{symbol} → external\n"






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



def _parse_imports(path: str, content: str) -> List[tuple]:

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











    if not os.path.isfile(path):
        return f"workspace: {path} not found\n"

    out: List[str] = []


    out.append(f"## File: {path}\n\n")

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



    try:
        file_content = "".join(
            ln.decode("utf-8", errors="replace") for ln in raw_lines
        )
    except Exception:
        file_content = ""

    _imports = _parse_imports(path, file_content)
    if _imports:
        _imports = _imports[:40]  
        _resolve_cache: dict = {}
        out.append(f"## Imports ({len(_imports)})\n\n")
        for sym, alias in _imports:
            resolved_line = op_resolve(sym, from_file=path, _cache=_resolve_cache).strip()

            arrow_idx = resolved_line.find(" → ")
            resolved_path = resolved_line[arrow_idx + 3:] if arrow_idx != -1 else resolved_line
            label = f"{sym} (as {alias})" if alias else sym
            out.append(f"  {label:<50} → {resolved_path}\n")
        out.append("\n")


    cfg = _load_config()
    validators = cfg.get("validators") or {}
    if validators:
        out.append("## Validators\n\n")
        out.append(op_validate(path, verbose=True))
        out.append("\n")


    dirname = os.path.dirname(os.path.abspath(path))
    cwd = os.path.abspath(os.getcwd())
    if dirname != cwd:
        my_name = os.path.basename(path)
        try:
            entries = [e for e in os.listdir(dirname) if not e.startswith(".")]
        except OSError:
            entries = []


        real_siblings = [e for e in entries if e != my_name]
        if real_siblings:
            out.append("## Siblings\n\n")
            ls_out = op_ls(dirname)


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


        try:
            branch_r = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
            )
            branch = branch_r.stdout.strip() if branch_r.returncode == 0 else "?"

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


        try:
            log_r = subprocess.run(
                ["git", "log", "--oneline", "-5", "--", path],
                capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
            )
            if log_r.returncode == 0 and log_r.stdout.strip():
                out.append("recent commits:\n")
                for line in log_r.stdout.strip().splitlines():


                    if len(line) > 120:
                        line = line[:117] + "..."
                    out.append(f"  {line}\n")
        except (subprocess.TimeoutExpired, OSError):
            pass


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


    basename = os.path.basename(path)
    ext = os.path.splitext(basename)[1]  

    symbol = os.path.splitext(basename)[0]  

    if "." in symbol:
        symbol = os.path.splitext(symbol)[0]

    display_cap = 20
    noisy_note = ""
    if symbol.lower() in _WORKSPACE_COMMON_SYMBOLS:
        display_cap = 10
        noisy_note = "  (common symbol — results may be noisy)\n"




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

            _start = 2 if len(hit) > 2 and hit[1] == ":" and hit[0].isalpha() else 0
            colon1 = hit.find(":", _start)
            colon2 = hit.find(":", colon1 + 1) if colon1 != -1 else -1
            if colon1 == -1 or colon2 == -1:









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


    _ws_test_path: Optional[str] = None

    if ext == ".php":

        test_pattern = f"**/{symbol}Test.php"
        from glob import glob as _glob
        candidates = _glob(test_pattern, recursive=True)
        if candidates:
            _ws_test_path = candidates[0]
    elif ext == ".py":

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







    from urllib.parse import urlparse, unquote

    if not isinstance(result, dict):
        return None

    def _normalize_file_url_or_path(s: str) -> str:
        if s.startswith("file://"):
            parsed = urlparse(s)
            return unquote(parsed.path)
        return s

    def _extract_first_path_from_bullets(text: str) -> Optional[str]:



        m = re.search(r"\sat\s+(/[^\s:]+(?:\:[^\s:]+)*?)(?:\:\d+(?:\:\d+)?)?\s*$",
                      text, flags=re.MULTILINE)
        return m.group(1) if m else None


    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "").strip()
                if not text:
                    continue

                if "• " in text or "\n• " in text or text.startswith("Found "):
                    p = _extract_first_path_from_bullets(text)
                    if p:
                        return p
                return _normalize_file_url_or_path(text)

    uri = result.get("uri")
    if isinstance(uri, str):
        return _normalize_file_url_or_path(uri)

    if isinstance(result.get("path"), str):
        return result["path"]
    return None






class MCPTimeout(Exception):
    pass


class MCPServerError(Exception):


    def __init__(self, message: str, code: int = 0, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data






_MCP_SERVERS: Dict[str, MCPClient] = {}
_MCP_LOCK = threading.Lock()


def _mcp_shutdown_all() -> None:

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

    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


for _sig in (signal.SIGTERM, signal.SIGINT):
    try:
        signal.signal(_sig, _mcp_signal_handler)
    except (OSError, ValueError):
        pass  






def _mcp_route(path: str, op: str) -> Optional[Tuple[str, str]]:

    if not path:
        return None


    for name, spec in _mcp_specs.items():
        glob = spec.get("match")
        if glob and _match_glob(path, glob):
            tool = (spec.get("tools") or {}).get(op)
            if tool:
                return (name, tool)
    return None


_MCP_DAEMON_SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "presets", "mcp", "daemon.py")
_MCP_STOP_SCRIPT = os.path.join(os.path.dirname(_MCP_DAEMON_SCRIPT), "stop.py")










_MCP_AUTOSPAWN_ENV = "SUPERTOOL_MCP_AUTOSPAWN"
_MCP_AUTOSPAWN_FALSEY = frozenset({"0", "false", "no", "off"})











_VALIDATOR_CONFIG_DIR_ENV = "SUPERTOOL_CONFIG_DIR"


def _mcp_autospawn_allowed() -> bool:





    raw = os.environ.get("SUPERTOOL_MCP_AUTOSPAWN")
    if raw is None:
        return True
    return raw.strip().lower() not in _MCP_AUTOSPAWN_FALSEY




_MCP_SOCKET_PID_PATHS_FN = None


def _mcp_socket_pid_paths(cwd: str, name: str) -> Tuple[str, str]:








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








    ok: bool
    code: str
    detail: str












_MCP_STOP_CODES = {
    0: ("stopped", True),
    2: ("usage", False),
    3: ("failed", False),
    4: ("refused", False),
    5: ("no-daemon", True),
}

_MCP_STOP_DETAIL_CAP = 500


































_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\[[0-?]*[ -/]*"
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)|\][^\x07\x1b]*"
    r"|[@-Z\\-_]|)"
)


def _mcp_stop_report(name: str, outcome: _StopOutcome) -> _StopOutcome:











    if not outcome.ok and os.environ.get("SUPERTOOL_DEBUG"):
        suffix = f" — {outcome.detail}" if outcome.detail else ""
        print(f"[supertool debug] mcp stop {name}: {outcome.code}{suffix}",
              file=sys.stderr)
    return outcome


def _mcp_stop_server(name: str) -> _StopOutcome:



















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

    detail = _ANSI_ESCAPE_RE.sub("", raw).strip()
    return _mcp_stop_report(name, _StopOutcome(ok, code, detail[-_MCP_STOP_DETAIL_CAP:]))


def _mcp_servers_to_stop_on_new_file(path: str) -> List[str]:




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








                raise MCPServerError(
                    "MCP daemon requires socket.AF_UNIX — not available on this platform"
                )
            cwd = os.path.abspath(os.getcwd())


































            try:
                self._sock_path, _ = _mcp_socket_pid_paths(cwd, name)
            except SystemExit as exc:
                if exc.code is None or isinstance(exc.code, int):
                    raise
                raise MCPServerError(str(exc.code)) from exc
            self._auto_spawn = True





    _CONNECT_TIMEOUT_SECONDS = 60

    def spawn(self) -> None:

        with self._lock:
            if self._sock is not None:
                return
            if not hasattr(socket, "AF_UNIX"):



                raise MCPServerError(
                    "MCP daemon requires socket.AF_UNIX — not available on this platform"
                )
            budget = _env_float(os.environ.get("SUPERTOOL_MCP_CONNECT_TIMEOUT"), "SUPERTOOL_MCP_CONNECT_TIMEOUT",
                                float(self._CONNECT_TIMEOUT_SECONDS), minimum=0.0)








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

        msg_id = self._next_id()
        payload = {"jsonrpc": "2.0", "method": method, "id": msg_id}
        if params is not None:
            payload["params"] = params
        with self._lock:
            self._send(payload)

            for _ in range(100):
                line = self._recv_line()
                msg = json.loads(line.decode("utf-8"))
                if msg.get("id") != msg_id:
                    continue  
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






def _mcp_get_server(name: str) -> Optional[MCPClient]:






    with _MCP_LOCK:
        if name in _MCP_SERVERS:
            srv = _MCP_SERVERS[name]
            if srv.is_alive():
                return srv

            del _MCP_SERVERS[name]
    return None


def _mcp_register(name: str, server: MCPClient) -> None:





    with _MCP_LOCK:
        _MCP_SERVERS[name] = server


def _mcp_call(server_name: str, tool: str, args: dict) -> Optional[dict]:












    server = _mcp_get_server(server_name)
    if server is None:
        return None
    try:
        return server.call_tool(tool, args)
    except (MCPTimeout, MCPServerError, OSError, EOFError, ValueError):
        return None
