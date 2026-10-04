











































from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_validate.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )

def _applicable_validators(op: str, path: str) -> Dict[str, Dict[str, Any]]:

    cfg = _load_config()
    validators = cfg.get("validators") or {}
    if not validators:
        return dict(_builtin_syntax_backstop(op, path, {}))
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


















_VALIDATOR_RESOLVE_ERROR_PREFIX = "RESOLVE-ERROR: "


def _validator_resolve(spec: Dict[str, Any], file: str) -> Optional[str]:











    if "resolve" not in spec:
        return file
    import subprocess








    cmd, _shield = _shield_substitute(spec["resolve"], {
        "supertool_dir": _INSTALL_DIR,
        "python": _python_token(),
        "file": shlex.quote(file),
    })
    _prefix_env, cmd = _extract_env_prefix(cmd)
    _prefix_env = {k: _unshield_env_value(v, _shield) for k, v in _prefix_env.items()}



    cmd = _unshield(_expand_env(cmd, _prefix_env), _shield)
    try:

        r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, timeout=30,
                           env=({**os.environ, **_prefix_env} if _prefix_env else None),
                           encoding="utf-8", errors="replace")
        resolved = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    except subprocess.TimeoutExpired:





        return _VALIDATOR_RESOLVE_ERROR_PREFIX + "resolve command timed out"
    except OSError as exc:





        return _VALIDATOR_RESOLVE_ERROR_PREFIX + "resolve command could not be run: {0}".format(exc)
    if not resolved:
        return None
    if resolved.startswith(_VALIDATOR_RESOLVE_ERROR_PREFIX):
        return resolved








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




_VALIDATOR_FINGERPRINT_CACHE: Dict[str, str] = {}


def _stat_signature(path: str) -> Optional[str]:

    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{path}:{st.st_size}:{st.st_mtime_ns}"


def _validator_fingerprint(spec: Dict[str, Any], cmd: str,
                           exclude: Optional[str] = None) -> str:






















    cache_key = repr((cmd, spec.get("fingerprint_paths"), exclude))
    memo = _VALIDATOR_FINGERPRINT_CACHE.get(cache_key)
    if memo is not None:
        return memo

    parts: list = []





    tokens = set(cmd.split())
    try:
        tokens |= set(shlex.split(cmd, posix=(os.name != "nt")))
    except ValueError:
        pass





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







_VALIDATOR_MEANING_VERSION_STAT: Optional[str] = None


def _validator_meaning_version() -> str:


















































    global _VALIDATOR_MEANING_VERSION, _VALIDATOR_MEANING_VERSION_STAT
    schema_path = os.path.join(_INSTALL_DIR, "validators", "SCHEMA.md")
    current_stat = _stat_signature(schema_path)
    if (_VALIDATOR_MEANING_VERSION is not None
            and current_stat == _VALIDATOR_MEANING_VERSION_STAT):
        return _VALIDATOR_MEANING_VERSION
    import hashlib
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





    import hashlib
    import hmac
    import json
    import time
    p = _validator_cache_path(key)
    if not p.exists():
        return None







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
        return None  
    payload = json.dumps(wrapped["data"], sort_keys=True).encode("utf-8")
    expected = hmac.new(_validator_cache_secret(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(wrapped.get("mac", ""))):
        return None  
    return wrapped["data"] if isinstance(wrapped["data"], dict) else None


def _validator_cache_write(key: str, data: Dict[str, Any]) -> None:

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





_NONDETERMINISTIC_ERROR_CODES = {"mcp", "orchestrator", "rector.exit", "adapter"}












def _validator_result_is_cacheable(data: Dict[str, Any]) -> bool:






















    if "skipped" in data:



        return False
    if data.get("ok"):
        return True
    for err in data.get("errors") or []:
        if not isinstance(err, dict):
            continue
        if err.get("code") in _NONDETERMINISTIC_ERROR_CODES:
            return False
    return True






_WARM_UNSAFE_READ_BYTES = 256 * 1024


def _validator_warm_unsafe_reason(spec: Dict[str, Any], target: str) -> Optional[str]:


































    patterns = spec.get("warm_unsafe")
    if isinstance(patterns, str):
        patterns = [patterns]
    if not isinstance(patterns, list) or not patterns:
        return None
    try:
        with open(target, "rb") as fh:
            blob = fh.read(_WARM_UNSAFE_READ_BYTES)
    except OSError:


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










    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()
    return parts[0] if parts else cmd.strip()


def _validator_unusable_reply(name: str, target: str, what: str,
                              elapsed: float) -> Dict[str, Any]:

































    return {"tool": name, "file": target, "elapsed_s": elapsed,
            "no_verdict": True,
            "skipped": f"{name} adapter {what} — this file was not checked"}


_VALIDATOR_CORE_ONLY_KEYS = frozenset({
    "no_verdict", "timeout", "elapsed_s", "resolved_to",
})


def _validator_strip_core_keys(data: Dict[str, Any]) -> Dict[str, Any]:










































    for key in _VALIDATOR_CORE_ONLY_KEYS:
        data.pop(key, None)
    return data





_VALIDATOR_COUNT_BASES = frozenset({"total", "measured"})


def _validator_count_contract_fault(data: Any) -> Optional[str]:



















































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













    dur = data.get("duration_ms", 0)
    if isinstance(dur, bool) or not isinstance(dur, (int, float)):
        dur = 0
    return {"tool": name, "file": target, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": _flat_cell(fault, 1000)}],
            "duration_ms": dur}


def _validator_apply_count_contract(data: Dict[str, Any], name: str,
                                    target: str) -> Dict[str, Any]:







    fault = _validator_count_contract_fault(data)
    if fault is None:
        return data
    return _validator_count_contract_reply(name, target, fault, data)


def _validator_run_one(name: str, spec: Dict[str, Any], file: str,
                       doc_maybe_stale: bool = False) -> Optional[Dict[str, Any]]:














    import subprocess
    import json
    import time
    _t0_resolve = time.monotonic()
    target = _validator_resolve(spec, file)
    if target is None:
        return {"tool": name, "skipped": "no target resolved"}
    if target.startswith(_VALIDATOR_RESOLVE_ERROR_PREFIX):



























        return _validator_unusable_reply(
            name, file,
            "could not resolve its target: {0}".format(
                target[len(_VALIDATOR_RESOLVE_ERROR_PREFIX):]),
            _elapsed_since(_t0_resolve))




    _warm_unsafe = _validator_warm_unsafe_reason(spec, target)
    if _warm_unsafe:
        return {"tool": name, "file": target, "skipped": _warm_unsafe}


    if spec.get("builtin"):
        return _builtin_syntax_run(name, str(spec["builtin"]), target)




    cmd, _shield = _shield_substitute(spec["cmd"], {
        "supertool_dir": _INSTALL_DIR,
        "python": _python_token(),
        "file": shlex.quote(target),
    })


    _prefix_env, cmd = _extract_env_prefix(cmd)
    _prefix_env = {k: _unshield_env_value(v, _shield) for k, v in _prefix_env.items()}

    _spec_env_dict = {**_prefix_env, **(spec.get("env") or {})}



    _extra_env = {str(k): str(v) for k, v in _spec_env_dict.items()}
    cmd = _unshield(_expand_env(cmd, _extra_env), _shield)
    timeout = int(spec.get("timeout", 60))





    spec_cache_enabled = bool(spec.get("cache", True))

    cache_key: Optional[str] = None
    if _validator_cache_enabled() and spec_cache_enabled:
        cache_key = _validator_cache_key(target, name, cmd, spec)
        if cache_key:
            import time as _time
            _t_cache = _time.monotonic()
            cached = _validator_cache_read(cache_key)
            if cached is not None:










                _validator_strip_core_keys(cached)



                cached = _validator_apply_count_contract(cached, name, target)





                cached["elapsed_s"] = _elapsed_since(_t_cache)
                if target != file:
                    cached["resolved_to"] = target
                return cached











    run_env = dict(_extra_env)
    run_env[_MCP_AUTOSPAWN_ENV] = "1" if spec.get("mcp_autospawn") else "0"


    if doc_maybe_stale:
        run_env["SUPERTOOL_LSP_DOC_MAYBE_STALE"] = "1"




    run_env[_VALIDATOR_CONFIG_DIR_ENV] = (
        os.path.dirname(os.path.realpath(_CONFIG_PATH)) if _CONFIG_PATH else ""
    )

    import time
    _t0 = time.monotonic()
    try:
        r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **run_env}, encoding="utf-8", errors="replace")
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




        _validator_strip_core_keys(data)


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



        _why = e.strerror or str(e)
        return _validator_unusable_reply(
            name, target,
            f"could not be run: {_validator_cmd_program(cmd)} — {_why}",
            _elapsed_since(_t0))
    except (json.JSONDecodeError, IndexError) as e:
        return _validator_unusable_reply(
            name, target, f"replied with something that is not JSON — {e}",
            _elapsed_since(_t0))


def _quote_open_guess_line(e: Dict[str, Any]) -> Optional[str]:













    guess = e.get("quote_open_guess")
    if not isinstance(guess, dict):
        return None
    gl = guess.get("line")
    if gl is None:
        return None
    note = _flat_cell(str(guess.get("note") or ""), 100)
    return f"      ↳ maybe opened at L{gl}" + (f": {note}" if note else "")


def _validator_render_row(data: Dict[str, Any], verbose: bool = False) -> list:















    if "skipped" in data:
        return [f"{_flat_cell(data['tool']):12s}: skipped — "
                f"{_flat_cell(data['skipped'])}"]
    tool = _flat_cell(data.get("tool", "?"))
    ok = data.get("ok", False)
    count = data.get("count", 0)
    dur = data.get("duration_ms", 0)


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



























    if not isinstance(data, dict) or "skipped" in data:
        return None
    if data.get("timeout"):
        errors = data.get("errors") or []
        msg = _flat_cell((errors[0].get("msg") if errors else "") or "", 300)
        return msg or "timed out"
    return _validator_not_checked(data)


def _validator_measured_count(data: Optional[Dict[str, Any]]) -> int:














































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













    if not isinstance(before, dict):
        return None
    if "skipped" in before or _validator_no_verdict(before) is not None:
        return None
    return before


def _validator_required(name: str) -> bool:
























    raw = os.environ.get("SUPERTOOL_REQUIRE_VALIDATORS", "")
    if not raw.strip():
        return False
    names = [n.strip().lower()
             for part in raw.split(os.pathsep) for n in part.split(",")]
    return "*" in names or name.lower() in names


def _validator_gate_did_not_run(data: Optional[Dict[str, Any]]) -> Optional[str]:


























    if not isinstance(data, dict):
        return None
    if data.get("no_verdict"):
        reason = data.get("skipped") or ""
    elif data.get("timeout"):
        errors = data.get("errors") or []
        reason = (errors[0].get("msg") if errors else "") or "timed out"
    else:
        return None



    if not _validator_required(str(data.get("tool") or "")):
        return None
    return _flat_cell(reason, 300) or "no reason given"


def _note_not_checked(results: Dict[str, Any]) -> None:







    for name, data in results.items():
        if (_validator_no_verdict(data) is not None
                or _validator_gate_did_not_run(data) is not None):
            _acc_not_checked().append(name)


def _validator_regressed(before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> bool:























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










    if not after.get("ok", False):
        return ""
    scope = after.get("scope")
    if not isinstance(scope, str) or not scope.strip():
        return ""
    return "(" + _flat_cell(scope.strip(), 40) + ")"


def _validator_render_diff(before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> list:





    elapsed = after.get("elapsed_s")
    time_col = f"{elapsed:.1f}s" if elapsed is not None else "-"
    gate_missed = _validator_gate_did_not_run(after)
    if gate_missed is not None:








        timed_out = bool(after.get("timeout"))
        why = ("(timed out — no verdict about this file)" if timed_out
               else "(no verdict about this file)")
        code_col = "orchestrator" if timed_out else "adapter"
        return [f"{_flat_cell(after.get('tool', '?')):12s}: {'NOT CHECKED':<10}  "
                f"{why}  {time_col:>5}",
                f"     {code_col}  {gate_missed}"]
    if "skipped" in after:


        reason = _flat_cell(after["skipped"], 80)
        state_col = f"({reason})" if reason else ""
        return [f"{_flat_cell(after['tool']):12s}: {'skipped':<10}  "
                f"{state_col}  {time_col:>5}"]
    tool = _flat_cell(after["tool"])
    no_verdict = _validator_no_verdict(after)
    if no_verdict is not None:






        timed_out = bool(after.get("timeout"))
        why = ("(timed out — no verdict about this file)" if timed_out
               else "(no verdict about this file)")
        code_col = "orchestrator" if timed_out else "adapter"
        return [f"{tool:12s}: {'NOT CHECKED':<10}  {why}  {time_col:>5}",
                f"     {code_col}  {no_verdict}"]














    baseline = _validator_baseline(before)
    b_unknown = baseline is None
    before = baseline






    b_count = _validator_measured_count(before) if before else 0
    a_count = _validator_measured_count(after)
    delta = a_count - b_count
    b_ok = before.get("ok", True) if before else True
    a_ok = after.get("ok", False)




    if not b_unknown and b_count == a_count and b_ok == a_ok:


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





            scope = _validator_scope_col(after)
            return [f"{tool:12s}: {', '.join(metric_parts)} {marker}"
                    f"{' ' + scope if scope else ''}  {'':<11}  {time_col:>5}"]

        if a_ok and a_metrics:
            primary = None
            for k in ("tests_total", "tests_passed", "changes_count"):
                if k in a_metrics:
                    primary = (k, a_metrics[k]); break
            if primary is not None:
                status = f"ok {primary[0]}={primary[1]}"



                col = _validator_scope_col(after) or "(no new errors)"
                return [f"{tool:12s}: {status:<10}  {col:<15}  {time_col:>5}"]
        status = "ok" if a_ok else f"{a_count} err"
        if a_ok:









            marker_col = _validator_scope_col(after) or "(no new errors)"
        else:



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





        arrow = f"? → {a_count}"



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





_FORMATTER_SKIPS: List[str] = []





_FORMATTER_EXPLICIT_ENV_SUFFIXES = ("_CONFIG", "_STANDARD", "_RULES", "_RULESET")


def _formatter_markers_for(name: str) -> Optional[Any]:







    lowered = name.lower()
    for key, markers in _FORMATTER_CONFIG_MARKERS.items():
        if key in lowered:
            return markers
    return None


def _repo_opts_into_formatter(name: str, spec: Dict[str, Any], path: str) -> bool:















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
    declared = spec.get("env")
    if isinstance(declared, dict):
        for key, value in declared.items():
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


    cfg = _load_config()
    formatters = cfg.get("formatters") or {}
    if not formatters:
        return {}
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







    import subprocess




    cmd, _shield = _shield_substitute(spec["cmd"], {
        "supertool_dir": _INSTALL_DIR,
        "python": _python_token(),
        "file": shlex.quote(file),
    })
    _prefix_env, cmd = _extract_env_prefix(cmd)
    _prefix_env = {k: _unshield_env_value(v, _shield) for k, v in _prefix_env.items()}
    _spec_env_dict = {**_prefix_env, **(spec.get("env") or {})}



    _extra_env = {str(k): str(v) for k, v in _spec_env_dict.items()}
    cmd = _unshield(_expand_env(cmd, _extra_env), _shield)
    timeout = int(spec.get("timeout", 30))







    run_env = dict(_extra_env)
    run_env[_VALIDATOR_CONFIG_DIR_ENV] = (
        os.path.dirname(os.path.realpath(_CONFIG_PATH)) if _CONFIG_PATH else ""
    )
    try:
        r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **run_env}, encoding="utf-8", errors="replace")
        stdout = r.stdout.strip()

        if stdout:
            try:
                data = json.loads(stdout)
                if isinstance(data, dict) and "ok" in data:
                    data["name"] = name
                    return data
            except (json.JSONDecodeError, ValueError):
                pass




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















    name = _flat_cell(result.get("name") or result.get("tool") or "?")
    ok = result.get("ok", False)
    dur = result.get("duration_ms", 0)
    metrics = result.get("metrics") or {}
    added = metrics.get("lines_added", 0)
    removed = metrics.get("lines_removed", 0)





    verify_failed = result.get("verify_failed")



    if "raw" in result:
        raw = (result.get("raw") or "").strip()
        if ok and not raw:
            return None  
        status = "ok" if ok else "fail"
        if raw:







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
        return None  

    if ok:
        line = f"{name:8s}: ok         ({dur}ms) +{added} -{removed}"







        first = metrics.get("first_changed_line")
        last = metrics.get("last_changed_line")
        if first is not None and last is not None:
            span = f"line {first}" if first == last else f"lines {first}-{last}"
            line += (f"  ({span} touched -- an earlier read of that region "
                     f"is now stale, re-read before reusing it)")
    else:
        errors = result.get("errors") or []
        msg = result.get("msg") or (errors[0].get("msg") if errors else "") or "failed"



        msg = _flat_cell(msg, 120)
        line = f"{name:8s}: fail       ({dur}ms)  {msg}"
    return line








_DEFER_FORMATTERS: bool = False
_FORMAT_QUEUE: Dict[str, Dict[str, Dict[str, Any]]] = {}





_VALIDATOR_DEFER_QUEUE: "list[tuple[str, Dict[str, Any], str]]" = []
_VALIDATOR_DEFER_SEEN: "set[tuple[str, str]]" = set()


def _drain_format_queue() -> str:

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





    try:
        with open(path, "rb") as f:
            post = f.read()
    except OSError:
        return ""
    if pre_content is None:
        return post.decode("utf-8", "replace")



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






    cmd, _shield = _shield_substitute(resolve_cmd, {
        "supertool_dir": _INSTALL_DIR,
        "python": _python_token(),
        "file": shlex.quote(path),
    })
    _prefix_env, cmd = _extract_env_prefix(cmd)
    _prefix_env = {k: _unshield_env_value(v, _shield) for k, v in _prefix_env.items()}



    cmd = _unshield(_expand_env(cmd, _prefix_env), _shield)
    try:
        r = subprocess.run(shlex.split(cmd), shell=False, capture_output=True,
                           text=True, timeout=30,
                           env=({**os.environ, **_prefix_env} if _prefix_env else None),
                           encoding="utf-8", errors="replace")
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 3:
        return None
    return r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ""


def _resolve_cmd_from_validators(cfg: Dict[str, Any],
                                 name: Optional[str] = None) -> Optional[str]:




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




















    _prev_pin = getattr(_DISPATCH_STATE, "pinned_write_target", None)
    _DISPATCH_STATE.pinned_write_target = (path, target)
    try:
        return fn()
    finally:
        _DISPATCH_STATE.pinned_write_target = _prev_pin


def _run_with_validators(op: str, parts: Any, do_op: Any) -> str:






    extract = _OP_TARGETS.get(op)
    if not extract:
        return do_op()



    _bump_counter(_MUTATION_ATTEMPTS, "cnt_mutation")
    try:
        path = extract(parts)
    except (IndexError, TypeError):
        return do_op()
    if not path:
        return do_op()










    _target = _write_target(path)
    _pre_existed = os.path.isfile(_target)










    _target_display = _write_target_display(path)
    _target_cell = _flat_cell(_target_display)
    if os.path.abspath(_target) != os.path.abspath(path):
        _target_cell += f" (which the symlink {_flat_cell(path)} resolves to)"
    applicable_fmt = _applicable_formatters(op, path)
    applicable_all = _applicable_validators(op, path)
    applicable_notif = _applicable_notifiers(op, path)







    _new_file_servers = [] if _pre_existed else _mcp_servers_to_stop_on_new_file(path)





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




    if _DEFER_FORMATTERS and applicable_fmt:
        abs_path = os.path.abspath(path)
        bucket = _FORMAT_QUEUE.setdefault(abs_path, {})
        bucket.update(applicable_fmt)
        applicable_fmt = {}
    if not applicable_fmt and not applicable:

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


    pre_content: Optional[bytes] = None
    needs_pre = (needs_rollback or needs_fmt_rollback or bool(applicable_notif)
                or _advice_wants_pre(op, path))
    if needs_pre and os.path.isfile(path):
        try:
            with open(path, "rb") as f:
                pre_content = f.read()
        except OSError:
            pre_content = None




































    before = (_validators_run_batch(applicable, path)
              if applicable and _pre_existed else {})

    body = _write_target_pinned(path, _target, do_op)


    _run_notifiers(op, path, pre_content=pre_content)

    if isinstance(body, str) and body.startswith("ERROR"):
        return body


    fmt_rows: list = []







    already_undone = False
    if applicable_fmt:
        fmt_results = _formatters_run_batch(applicable_fmt, path)
        for result in fmt_results:
            if not result["ok"]:
                result_name = result.get("name", "")
                if result_name in applicable_fmt and applicable_fmt[result_name].get("rollback_on_fail"):



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




    for _srv in _new_file_servers:
        _mcp_stop_server(_srv)








    _doc_maybe_stale = _pre_existed and not _new_file_servers
    after_results = (_validators_run_batch(applicable, path, _doc_maybe_stale)
                     if applicable else {})
    _note_not_checked(after_results)
    diff_lines: list = []
    for name in applicable:  
        if name in after_results:
            diff_lines.extend(_validator_render_diff(before.get(name), after_results[name]))

    diff_out = "\n".join(diff_lines) + ("\n" if diff_lines else "")

    if needs_rollback:










        for name, spec in applicable.items():
            if not spec.get("rollback_on_fail"):
                continue
            after_data = after_results.get(name)
            if after_data is None or not _validator_regressed(before.get(name), after_data):
                continue
            if already_undone:




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










    if applicable and not _pre_existed and not already_undone:
        refused_by = [n for n in applicable
                      if n in after_results
                      and _validator_regressed(before.get(n), after_results[n])]



        if refused_by and os.path.exists(_target):
            _note_left_on_disk()
            diff_out += ("\n" + _left_on_disk_line(
                refused_by, path, body, target=_target_display) + "\n")

    suffix = ""
    if fmt_rows:  
        suffix += "\n[formatters]\n" + "\n".join(fmt_rows) + "\n"
    if applicable:
        suffix += "\n[validators]\n" + diff_out

    return body + suffix + _run_advice(op, path, _pre_existed, pre_content)





_SYNTAX_FILTER_SENTINEL = "@syntax"


def _select_validators(validators: dict, tool_filter: Optional[list]) -> dict:





    if not tool_filter:
        return validators
    if _SYNTAX_FILTER_SENTINEL in tool_filter:
        return {k: v for k, v in validators.items()
                if isinstance(v, dict) and v.get("syntax")}
    return {k: v for k, v in validators.items() if k in tool_filter}


def _validate_one_block(path: str, validators: dict, verbose: bool = False) -> List[str]:















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




        ran_any = True
        if "skipped" in data or _validator_no_verdict(data) is not None:
            had_non_verdict = True
        elif data.get("ok") is False:
            had_finding = True
        _note_not_checked({name: data})
        out.extend(_validator_render_row(data, verbose=verbose))







    _acc_validated().append((path, had_finding, had_non_verdict or not ran_any))
    return out


def op_validate(path: str, tool_filter: Optional[list] = None, verbose: bool = False) -> str:




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


def op_format(path: str, tool_filter: Optional[list] = None, verbose: bool = False,
              gated: bool = False) -> str:












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







                msg = _flat_cell(e.get("msg") or "")
                out.append(f"  {line_n} {code}  {msg}")
        else:
            out.append(row)
    if not matched:
        out.append("(no formatters matched this file)")
    return "\n".join(out) + "\n"


def _undecodable_staged_paths(raw: str) -> str:










    bad = [p for p in raw.split("\x00") if p and _undecodable_at(p) >= 0]
    if not bad:
        return ""
    return (
        f"WARNING: {len(bad)} staged path(s) are not valid UTF-8 and were NOT "
        f"checked — rename them or check them by hand: {', '.join(bad)}\n"
    )


def op_validate_staged(tool_filter: Optional[list] = None, verbose: bool = False) -> str:









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
        parts.append(f"validate_staged: {fpath}")
        block = op_validate(fpath, tool_filter, verbose=verbose)

        for line in block.splitlines():
            parts.append(f"  {line}")
    return "\n".join(parts) + "\n"


def op_format_staged(tool_filter: Optional[list] = None, verbose: bool = False) -> str:









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
