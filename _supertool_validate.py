"""_supertool_validate -- validators, formatters, advice, _run_with_validators,
op_validate*, op_format* -- split out of _supertool.py (#2706).

Loaded by `_load_part("_supertool_validate")` from inside `_supertool.py`, at
the exact source position `_applicable_validators` used to occupy: a plain
`exec(code, globals())` via `_load_part`, not a real `import`. Every function
defined below therefore has `__globals__ is _supertool.__dict__` once loaded,
so any existing `monkeypatch.setattr(supertool, ...)` on a validator or
formatter name keeps reaching the code it patches.

Not importable on its own. `_load_part` is the only legitimate loader: it
puts `_load_part` itself into the globals this file executes against before
running it, which is exactly the marker the guard below checks for. A bare
`import _supertool_validate` or `python3 _supertool_validate.py` gets this
module's own fresh globals(), which has no such name, and refuses with a
clear ImportError rather than failing later with a NameError on the first
name this file assumes `_supertool.py` already defined (Dict, Any, os, re,
shlex, subprocess, ...).

Scope note: `_flat_cell`/`_flat_field`/`_flat_keys` (plus the `_UNTRUSTED_FLAT`
module state `_flat_field` rebinds) stay in `_supertool.py` even though they
sit physically inside the span this part moved -- they are generic
string-flattening helpers used all over the dispatch and payload code in
core, not validator/formatter-specific, and moving them would have required
a `# noqa: F821` at roughly 55 unrelated core call sites instead of the
handful genuinely tied to this part's own op_validate*/op_format*/
_run_with_validators dispatch points. The notifier subsystem
(`_applicable_notifiers`, `_run_notifiers`, `_first_changed_line`,
`_sweep_old_notifier_temp_files`) stays in core for the same kind of reason:
a third caller, `_notify_read_op`, is dispatch-level infrastructure with no
validator/formatter involvement. `_run_with_validators` below still calls
`_run_notifiers`/`_applicable_notifiers` and `_flat_cell`/`_flat_field` across
the module boundary under the same shared-globals guarantee `_cache_root()`
already relies on from `_supertool_gc.py`.

Five spans, not three -- the two cut-outs above (`_flat_cell` sits between
two validator-cache spans, `_flat_field`/`_flat_keys` between two more) split
what would otherwise be one contiguous block into five pieces, all loaded by
the single `_load_part` call at the first span's original position: ast over
`_supertool.py` shows no import-time code (no decorators, no module-level
calls, no class bodies) in the gaps this leaves behind, in either direction,
so one splice point is safe regardless of how many physical regions were cut
out of core to build this file.
"""
from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_validate.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )

def _applicable_validators(op: str, path: str) -> Dict[str, Dict[str, Any]]:
    """Return validators that should wrap this op call. Skips opt_in."""
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

    import hashlib
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
