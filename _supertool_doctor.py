"""supertool core -- doctor / init (#2706).

Split out of _supertool.py to keep that file under the Anthropic plugin
directory's 256 KiB per-file limit. Imported lazily, inside the function
body, by the op_doctor/op_init stubs still living in _supertool.py (so
dispatch, _dispatch_impl and the op registry are untouched) -- never
imported at _supertool.py module-load time.

Every reference back into _supertool.py below goes through the qualified
`_supertool.NAME` form rather than `from _supertool import NAME`, and is
looked up at call time, not at import time. That is load-bearing: several
tests patch e.g. `monkeypatch.setattr(supertool, "_which_excluding_cwd",
...)` on the staying-behind helper, and `supertool` IS `_supertool` (the
same module object, via supertool.py's own sys.modules shim, #931) -- a
qualified attribute access performed inside a function body re-reads
whatever is currently bound in that dict on every call, so the patch is
seen. A bare `from _supertool import X` would instead bind X once, at
this module's own import time, and silently stop seeing a later patch.

The doctor/init helpers that moved here (_doctor_*, _init_*) are NOT
re-exported back into _supertool.py -- the handful of tests that called
or monkeypatched them directly (as opposed to through op_doctor/op_init)
were migrated in this same change to reference _supertool_doctor
directly, per CLAUDE.md's own rule against a re-export that keeps an old
patch green while it no longer reaches the code.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

import _supertool


def _doctor_interpreter() -> Dict[str, Any]:
    """Facts about the interpreter answering this call (#1857).

    `rosetta` is a tri-state, not a bool: `True` (translated), `False`
    (confirmed native), or `None` — the sysctl node this reads
    (`sysctl.proc_translated`) exists only on Apple Silicon, so it is absent
    by design on an Intel Mac and answers nothing there. Defaulting the
    unreadable case to `False` would assert "native" about a host nobody
    checked, which is the exact failure #1857 was filed to stop happening
    silently — an architecture mismatch is a performance fact, not a fault,
    but only when it is reported rather than assumed away.

    **`sysctl.proc_translated` is not trusted on its own.** Confirmed on real
    hardware: `timeout 5 python3 -c '…sysctl.proc_translated…'` answers `1`
    for a python3 binary `file` reports as `Mach-O 64-bit executable arm64`
    — the flag reads as `1` when a translated ancestor sits anywhere in the
    exec chain (Homebrew's `timeout` is compiled x86_64), not only when THIS
    process is the one being translated. Rosetta translates x86_64 binaries;
    an arm64 process can never itself be the thing being translated, so
    `platform.machine() == "arm64"` forces `rosetta` to `False` regardless of
    what the sysctl answers — the alternative publishes "install a native
    interpreter" about an interpreter that already is one.
    """
    machine = platform.machine()
    info: Dict[str, Any] = {
        "executable": sys.executable,
        "version": platform.python_version(),
        "machine": machine,
        "platform": sys.platform,
        "rosetta": None,
    }
    if sys.platform == "darwin":
        if machine == "arm64":
            info["rosetta"] = False
        else:
            try:
                r = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"],
                                   capture_output=True, text=True, timeout=5,
                                   encoding="utf-8", errors="replace")
                out = r.stdout.strip()
                if r.returncode == 0 and out in ("0", "1"):
                    info["rosetta"] = out == "1"
            except (OSError, subprocess.TimeoutExpired):
                # Deliberately swallowed: `rosetta` stays None, which is this
                # op's third state — "could not tell" — and is rendered as
                # such. A missing or hung `sysctl` is not evidence either way
                # about translation, so neither True nor False may be
                # inferred here, and raising would take the whole report down
                # over one unanswerable line.
                pass
    return info


def _doctor_cpu_topology() -> Dict[str, Any]:
    """Logical CPU count, plus a performance/efficiency split where askable.

    macOS exposes the split via `hw.perflevel{0,1}.logicalcpu`; nothing in
    the standard library answers it elsewhere, and this does not shell out
    to a second tool to guess — `state` is `unknown` there rather than a
    fabricated split. #1857's own case: a worker pool sized off
    `os.cpu_count()` alone asks for one worker per core on a 5+6 chip and
    gets six of them fighting the other five, and only the split shows that.
    """
    topo: Dict[str, Any] = {
        "logical_cpus": os.cpu_count(),
        "performance": None,
        "efficiency": None,
        "state": "unknown",
    }
    if sys.platform == "darwin":
        try:
            p = subprocess.run(["sysctl", "-n", "hw.perflevel0.logicalcpu"],
                               capture_output=True, text=True, timeout=5,
                               encoding="utf-8", errors="replace")
            e = subprocess.run(["sysctl", "-n", "hw.perflevel1.logicalcpu"],
                               capture_output=True, text=True, timeout=5,
                               encoding="utf-8", errors="replace")
            if p.returncode == 0 and p.stdout.strip():
                if e.returncode == 0 and e.stdout.strip():
                    topo["performance"] = int(p.stdout.strip())
                    topo["efficiency"] = int(e.stdout.strip())
                    topo["state"] = "split"
                else:
                    # perflevel0 answers and perflevel1 does not: a
                    # non-hybrid Mac, not an unreadable one.
                    topo["state"] = "uniform"
        except (OSError, subprocess.TimeoutExpired, ValueError):
            topo["state"] = "unknown"
    return topo


def _doctor_symlink() -> Dict[str, Any]:
    """Health of the `supertool` binary on PATH — the dangling-symlink trap.

    `CLAUDE.md`'s own words: a plugin update can leave `~/.local/bin/supertool`
    pointing at nothing, which is `exit 127` mid-session with no other signal.
    Also flags when PATH resolves to a different file than the one answering
    this very call — the ordinary, expected case inside a worktree (`python3
    supertool.py` is run directly, and CLAUDE.md says to), surfaced rather
    than silently assumed innocent, because the other cause of the same
    symptom is a stale symlink target.
    """
    # #2611: the resolved path is spawned below (`[which, "version"]`, no
    # cwd=) to compare its own reported version -- a raw shutil.which()
    # would let a repo-planted "supertool.exe"/".bat"/".cmd" at cwd shadow
    # the real tool on Windows, the same class fixed for _has_rtk()/
    # _has_ctags() in this same change.
    which = _supertool._which_excluding_cwd("supertool")
    result: Dict[str, Any] = {"which": which, "symlink_target": None,
                              "dangling": False}
    if which is None:
        return result
    if os.path.islink(which):
        # TOCTOU: the link can vanish between the check above and the read
        # below (a plugin update mid-session is exactly the scenario this
        # function exists to catch). An unguarded OSError here would crash
        # the whole `doctor` op over one race window instead of degrading —
        # so a link that disappears between the two calls answers `None`,
        # not a crash and not a false `dangling: False`.
        try:
            target = os.readlink(which)
        except OSError:
            result["symlink_target"] = None
            result["dangling"] = None
        else:
            resolved = target if os.path.isabs(target) else os.path.normpath(
                os.path.join(os.path.dirname(which), target))
            result["symlink_target"] = resolved
            result["dangling"] = not os.path.exists(resolved)
    running = os.path.abspath(__file__)
    try:
        result["path_resolves_to_running_module"] = (
            os.path.realpath(which) == os.path.realpath(running))
    except OSError:
        result["path_resolves_to_running_module"] = None
    result["running_module"] = running

    # #2121: a path mismatch alone does not mean stale. A launcher script
    # that resolves the newest install at run time (CLAUDE.md's own
    # documented remedy for #2071's dangling-symlink trap) necessarily
    # fails the path comparison above while being exactly as current as
    # the module answering this call. Ask the cheaper question directly:
    # run the PATH entry with `version` and compare what it answers.
    # Three states, not two -- `current` / `stale` / `unknown` -- because
    # folding "could not run" into either would either mask a genuinely
    # stale build or false-alarm a healthy one.
    result["path_version"] = None
    result["path_version_state"] = None
    if result["path_resolves_to_running_module"] is False and not result["dangling"]:
        try:
            proc = subprocess.run([which, "version"], capture_output=True,
                                  text=True, timeout=5, encoding="utf-8",
                                  errors="replace")
            match = (re.match(r"supertool\s+(\S+)", proc.stdout.strip())
                     if proc.returncode == 0 else None)
        except (OSError, subprocess.TimeoutExpired):
            match = None
        if match:
            result["path_version"] = match.group(1)
            result["path_version_state"] = (
                "current" if match.group(1) == _supertool.VERSION else "stale")
        else:
            result["path_version_state"] = "unknown"
    return result


def _doctor_tracked_files() -> Optional[List[str]]:
    """`git ls-files -z`, or `None` when the answer could not be obtained.

    `None` is load-bearing: a validator's scope cannot be reported as
    "not applicable" off a listing that itself failed, or a real gap reads
    as furniture nobody needs to install.

    `-z` rather than the default rendering, split on NUL rather than
    `str.splitlines()` (#2022). Two defects in one call, both closed by the
    same flag:

    - `splitlines()` folds on ten separators (U+2028/U+2029 among them);
      `git ls-files`' own delimiter is LF alone. A tracked path containing
      U+2028 became two list entries under `core.quotePath=false`, and the
      fragment after the separator reached `_validator_run_one`'s `target`
      argument -- a path handed straight to a subprocess.
    - Git C-quotes an unusual filename (one holding a backslash, for
      instance) in its default rendering -- `"ff\\ff.py"`, literal quotes
      included -- which `_match_glob` can never match against an ordinary
      glob. `-z` disables that quoting entirely rather than requiring a
      second unquoting pass this function would then have to get right.
    """
    try:
        r = subprocess.run(["git", "ls-files", "-z"], capture_output=True,
                           text=True, timeout=15,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return [seg for seg in r.stdout.split("\x00") if seg]


def _doctor_looks_absent(text: str) -> bool:
    """Does this adapter-authored sentence read as "the binary is missing"?

    One heuristic, applied to both routes an adapter uses to say it — the
    `skipped()`/`absent()` third state, and the `errors` list. #1950's own
    `absent()` helper escalates to an `errors`+`code:"adapter"` entry instead
    of a `skipped` one the moment a repo names the tool in
    `$SUPERTOOL_REQUIRE_VALIDATORS`, and several shipped adapters
    (`phpstan`, `xmllint`, `node-check`, `prettier-check`, `bash-check`,
    `phpmd`, `psr`, `lsp-diag`) report a missing binary as an inline `errors`
    entry directly, never through `skipped()` at all. A classifier that only
    read this heuristic off the `skipped` branch reported every one of those
    as "could not tell" for a tool that is, in fact, definitively absent —
    never wrong in the dangerous direction (never "resolves"), but a real
    accuracy gap in the exact three-state count #1950 asks for.
    """
    low = text.lower()
    return ("not found" in low or "not installed" in low
            or "could not be run" in low or "binary not found" in low)


def _doctor_classify_probe(data: Dict[str, Any]) -> "Tuple[str, str]":
    """Sort one `_validator_run_one` verdict into resolves/absent/could-not-tell.

    Reuses the adapter's own vocabulary (`validators/common/refusal.py`)
    rather than re-deriving it: `skipped()`/`absent()` already say "not
    found" in their own words when the binary is missing, and
    `crashed()`/`tool_fault()` already mark themselves `code: "adapter"` when
    the tool ran and fell over without answering. Anything this cannot place
    with confidence — an ambiguous skip reason, a scope-shaped decline this
    probe should not have hit given it was pointed at a matching file — lands
    on "could not tell" rather than "resolves", because laundering an
    unreadable answer into a clean one is exactly the failure #1950 exists to
    end.
    """
    if data.get("timeout"):
        return "could not tell", f"timed out after {data.get('duration_ms', 0)}ms"
    if "skipped" in data:
        reason = str(data["skipped"])
        if _doctor_looks_absent(reason):
            return "absent", reason
        return "could not tell", reason
    errors = data.get("errors") or []
    if any(isinstance(e, dict) and e.get("code") == "adapter" for e in errors):
        msg = str(errors[0].get("msg", "")) if errors else ""
        if _doctor_looks_absent(msg):
            return "absent", msg
        return "could not tell", msg
    if "ok" in data:
        return "resolves", f"ok={data.get('ok')}, count={data.get('count')}"
    return "could not tell", "adapter replied without a verdict"


def _doctor_validators_section(config: Dict[str, Any], probe: bool) -> str:
    """The #1950 half: per configured validator, resolves/absent/could-not-tell.

    Two costs are kept apart on purpose. Scope — does any tracked file match
    this validator's `match` glob — is always computed, because it is free
    (string matching against a `git ls-files` already paid for once). Binary
    resolution is a subprocess per adapter and is gated behind `doctor:probe`
    (issue's own words: "do not run 39 adapters to find out"); the default
    render says "could not tell without probing" for every in-scope
    validator rather than guessing from `shutil.which`, which the issue
    documents as actively wrong (`npx` present, `stylelint` absent).

    Probing invokes the adapter's own resolution path — never a
    `shutil.which` sweep — against a real tracked file that matches its
    `match` glob, which is also what surfaces the config half of #1950 for
    free: `eslint.py`'s `_NO_CONFIG` decline and `stylelint`'s ignore-marker
    skip are both ordinary `skipped()` results the real invocation already
    produces when this tree lacks the config the tool needs, with no second
    per-tool config-detection layer required.

    **Probing always bypasses `_validator_run_one`'s own result cache**
    (`~/.cache/supertool/validators/`, up to a 24h TTL by default). That
    cache exists to make an *edit* fast on a file whose validator answer has
    not changed; `doctor:probe` exists to answer "does this resolve NOW",
    which a cache hit from before a binary was installed or removed would
    silently contradict — the same silently-stale-green failure #1950 was
    filed to end, one layer down through infrastructure reuse.
    `$SUPERTOOL_NO_VALIDATOR_CACHE` is the existing, already-plumbed knob for
    this; it is set for the duration of this function only and always
    restored, so a concurrent `edit`/`validate` call in another process is
    unaffected.
    """
    validators = config.get("validators") or {}
    lines: List[str] = ["## Toolchain validators (#1950)"]
    if not validators:
        lines.append("- no \"validators\" section in .supertool.json")
        return "\n".join(lines)

    files = _doctor_tracked_files()
    scope_unknown = files is None

    resolves = absent = unknown = not_applicable = 0
    rows: List[str] = []
    _prior_cache_env = os.environ.get("SUPERTOOL_NO_VALIDATOR_CACHE")
    if probe:
        os.environ["SUPERTOOL_NO_VALIDATOR_CACHE"] = "1"
    try:
        for name in sorted(validators):
            spec = validators[name]
            if not isinstance(spec, dict):
                continue
            glob = spec.get("match", "*")
            target: Optional[str] = None
            if not scope_unknown:
                for f in files:  # type: ignore[union-attr]
                    if _supertool._match_glob(f, glob):
                        target = f
                        break

            if scope_unknown:
                unknown += 1
                rows.append(f"- {name}: could not tell whether this tree has a "
                            "matching file (`git ls-files` did not answer)")
                continue
            if target is None:
                not_applicable += 1
                rows.append(f"- {name}: not applicable (no tracked file matches "
                            f"`{glob}`)")
                continue
            if not probe:
                unknown += 1
                rows.append(f"- {name}: in scope "
                            f"({_supertool._flat_field(target, disclose_newline=True)}) — "
                            "could not tell without probing; run doctor:probe")
                continue
            try:
                data = _supertool._validator_run_one(name, spec, target)
            except Exception as exc:  # noqa: BLE001 — a crashing probe is itself a finding
                unknown += 1
                # Flattened like every other adapter/filename-derived render
                # in this loop (#2022 finding 3): `target` can legitimately
                # carry a newline post-fix, and an exception commonly echoes
                # its argument verbatim, so an unflattened str(exc) could
                # forge a row here exactly as an unflattened `detail` did.
                rows.append(f"- {name}: could not tell — probing it raised "
                            f"{type(exc).__name__}: {_supertool._flat_field(str(exc))}")
                continue
            if not isinstance(data, dict):
                unknown += 1
                rows.append(f"- {name}: could not tell — probe returned no verdict")
                continue
            state, detail = _doctor_classify_probe(data)
            if state == "resolves":
                resolves += 1
            elif state == "absent":
                absent += 1
            else:
                unknown += 1
            rows.append(
                f"- {name} ({_supertool._flat_field(target, disclose_newline=True)}): "
                f"{state} — {_supertool._flat_field(detail)}")
    finally:
        if probe:
            if _prior_cache_env is None:
                os.environ.pop("SUPERTOOL_NO_VALIDATOR_CACHE", None)
            else:
                os.environ["SUPERTOOL_NO_VALIDATOR_CACHE"] = _prior_cache_env

    lines.append(f"- {len(validators)} configured, {resolves} resolves, "
                 f"{absent} absent, {unknown} could not tell, "
                 f"{not_applicable} not applicable")
    lines.extend(rows)
    return "\n".join(lines)


def _doctor_looks_absent_formatter(text: str) -> bool:
    """`_doctor_looks_absent`, widened for the formatter half's own failure
    shape (#2086) -- kept as a separate function rather than editing the
    validator's shared heuristic, so this addition cannot change what a
    validator probe reports.

    A SCHEMA-emitting formatter phrases an absent binary exactly like a
    validator does ("RUFF_BIN not found: ruff") and the base heuristic
    already catches that. A *legacy*, non-JSON formatter has no adapter
    script standing between it and `subprocess.run(shell=False, ...)`, so
    a missing binary there is Python's own `FileNotFoundError` text --
    "[Errno 2] No such file or directory: 'binary'" -- which contains
    neither "not found" nor "not installed". Left unwidened, every legacy
    formatter's absent-binary case would misreport as "could not tell".
    """
    low = (text or "").lower()
    if _doctor_looks_absent(text):
        return True
    return "no such file or directory" in low


def _doctor_classify_formatter_probe(data: Any) -> "Tuple[str, str]":
    """Sort one `_formatter_run_one` verdict into resolves/absent/could-not-tell.

    Mirrors `_doctor_classify_probe` (#1950) for the formatter twin of the
    same dispatch (#2086) -- the shapes differ enough that reusing the
    validator classifier directly would misread a formatter's own decline.

    A SCHEMA-emitting formatter (`php-cs-fixer`, `phpcbf`, `prettier-write`,
    `ruff-format`) reports an absent binary the same way every validator
    does: an inline `errors` entry with `code: "adapter"`, never a `skipped`
    key -- formatters have no third state of their own, see
    `_formatter_run_one`. A legacy, non-JSON formatter (a bare `cmd` with no
    adapter script) carries no `errors` list at all on failure; its only
    signal is `ok: False` plus a top-level `msg` set by `_formatter_run_one`
    itself (`OSError`, a timeout). Both routes are checked, in that order,
    before falling back to the bare `ok` key -- an unreadable verdict must
    never default to "resolves", the same rule #1950 states for validators.
    """
    if not isinstance(data, dict):
        return "could not tell", "probe returned no verdict"
    errors = data.get("errors") or []
    if any(isinstance(e, dict) and e.get("code") == "adapter" for e in errors):
        msg = str(errors[0].get("msg", "")) if errors else ""
        if _doctor_looks_absent_formatter(msg):
            return "absent", msg
        return "could not tell", msg
    msg = data.get("msg")
    if msg is not None:
        if _doctor_looks_absent_formatter(str(msg)):
            return "absent", str(msg)
        return "could not tell", str(msg)
    raw = data.get("raw")
    if isinstance(raw, str) and not data.get("ok", True) and (
        "Traceback (most recent call last)" in raw
    ):
        # `_formatter_run_one`'s "legacy fallback" branch fires whenever
        # stdout does not parse as SCHEMA JSON -- which is exactly what a
        # crashing formatter SCRIPT (not the underlying tool) produces too:
        # a raw `cmd` that fails to format a file and a custom adapter that
        # raised before calling `emit()` are otherwise indistinguishable
        # here, both landing on `ok: False` with no `errors`/`msg` key. A
        # Python traceback in `raw` is the one signal that tells them apart
        # without guessing, so it is read before the bare `ok` fallback
        # below reports the crash as a working toolchain that merely found
        # something wrong with this file.
        return "could not tell", "adapter raised an exception -- " + raw[:200]
    if "ok" in data:
        return "resolves", f"ok={data.get('ok')}"
    return "could not tell", "adapter replied without a verdict"


def _doctor_formatters_section(config: Dict[str, Any], probe: bool) -> str:
    """The formatter twin of `_doctor_validators_section` (#2086).

    `doctor` reported validator toolchains in full (#1950) and said nothing
    about formatters, so a repo with no working Python formatter -- the
    subject of #2085 -- had no way to learn that from the one op that exists
    to answer "what is this environment able to check/fix". The silence
    read exactly like formatters being fine, which is this repo's own
    defect class (CLAUDE.md, "The defect this codebase keeps having") aimed
    at its own diagnostic.

    Same cost split as the validator half: scope (does any tracked file
    match this formatter's `match` glob) is always computed, because a
    `git ls-files` listing is already paid for by the validator section run
    moments earlier. Binary resolution costs a real subprocess and stays
    behind `doctor:probe`, for the same reason -- up to N adapters is not a
    cost a bare `doctor` should pay by default.

    Unlike validators, `doctor:probe`'s formatter probe does not disable
    `_formatter_run_one`'s cache: formatters have none to disable (no
    caching layer exists for them, unlike `_validator_run_one`'s
    `~/.cache/supertool/validators/`), so there is nothing to bypass.
    """
    formatters = config.get("formatters")
    lines: List[str] = ["## Formatters (#2086)"]
    if formatters is None:
        # No key at all -- nobody has DECLARED anything, so every write this
        # tool makes goes out unformatted and nothing says so at the moment
        # it lands. WARN rather than an unweighted bullet (#2316): unlike a
        # genuine not-applicable ("no tracked file matches"), this is a
        # capability switched off, not a fact about the tree.
        lines.append("- " + _supertool.mark("⚠") + " no \"formatters\" section in "
                     ".supertool.json — no formatter configured, and nothing "
                     "on record says that is intentional")
        return "\n".join(lines)
    if formatters == {}:
        # An explicit empty dict is a DECISION on record -- the repo declared
        # it wants no formatter, the same absent-vs-empty shape claude-oss's
        # own `changelog_untagged` already uses. ok, not a nag (#2316).
        lines.append("- 0 configured — \"formatters\": {} on record, no "
                     "formatter declared")
        return "\n".join(lines)
    if not isinstance(formatters, dict) or not formatters:
        # Present, but neither the dict this key is documented to hold NOR
        # the empty-dict decision above -- a list, a string, 0, False. This
        # is a malformed config, not "declared empty", and must not be
        # laundered into the same ok line real {} gets (oss:auditor review
        # of #2314x2316: `[]`/`""`/`0`/`False` all rendered the {} sentence
        # verbatim, claiming a JSON shape the config did not actually hold).
        lines.append("- " + _supertool.mark("⚠") + " \"formatters\" is set but is not a "
                     "table (got " + type(formatters).__name__ + ") — expected "
                     "either no key, {} for an explicit decision, or a table "
                     "of formatter specs")
        return "\n".join(lines)

    files = _doctor_tracked_files()
    scope_unknown = files is None

    resolves = absent = unknown = not_applicable = 0
    rows: List[str] = []
    for name in sorted(formatters):
        spec = formatters[name]
        if not isinstance(spec, dict):
            continue
        glob = spec.get("match", "*")
        target: Optional[str] = None
        if not scope_unknown:
            for f in files:  # type: ignore[union-attr]
                if _supertool._match_glob(f, glob):
                    target = f
                    break

        if scope_unknown:
            unknown += 1
            rows.append(f"- {name}: could not tell whether this tree has a "
                        "matching file (`git ls-files` did not answer)")
            continue
        if target is None:
            not_applicable += 1
            rows.append(f"- {name}: not applicable (no tracked file matches "
                        f"`{glob}`)")
            continue
        if not probe:
            unknown += 1
            rows.append(f"- {name}: in scope "
                        f"({_supertool._flat_field(target, disclose_newline=True)}) — "
                        "could not tell without probing; run doctor:probe")
            continue
        try:
            data = _supertool._formatter_run_one(name, spec, target)
        except Exception as exc:  # noqa: BLE001 — a crashing probe is itself a finding
            unknown += 1
            rows.append(f"- {name}: could not tell — probing it raised "
                        f"{type(exc).__name__}: {_supertool._flat_field(str(exc))}")
            continue
        if not isinstance(data, dict):
            unknown += 1
            rows.append(f"- {name}: could not tell — probe returned no verdict")
            continue
        state, detail = _doctor_classify_formatter_probe(data)
        if state == "resolves":
            resolves += 1
        elif state == "absent":
            absent += 1
        else:
            unknown += 1
        rows.append(
            f"- {name} ({_supertool._flat_field(target, disclose_newline=True)}): "
            f"{state} — {_supertool._flat_field(detail)}")

    lines.append(f"- {len(formatters)} configured, {resolves} resolves, "
                 f"{absent} absent, {unknown} could not tell, "
                 f"{not_applicable} not applicable")
    lines.extend(rows)
    return "\n".join(lines)


def op_doctor(arg: str = "") -> str:
    """Report the environment supertool runs in, and what it dispatches to.

    Two halves, filed as #1857 and #1950. The interpreter/CPU/symlink half
    always runs — it is in-process and cheap. The validator half always
    reports scope, and only probes binary resolution when called as
    `doctor:probe`, because that half costs a subprocess per adapter (up to
    39 in this tree) and a doctor nobody runs because it takes thirty
    seconds is worse than none.

    A separate op rather than folding into `version`: this is what somebody
    reaches for by name after a morning like the one #1857 describes, and
    `version`'s own one-line contract (`f"supertool {VERSION}\n"`, asserted
    by `tests/test_version.py`) is not the place to grow a multi-section
    report.
    """
    probe = arg.strip().lower() == "probe"
    lines: List[str] = []

    interp = _doctor_interpreter()
    if interp.get("rosetta") is True:
        lines.append(
            f"!! ARCHITECTURE MISMATCH: this interpreter ({interp['machine']}) "
            "is running under Rosetta 2 (or equivalent binary translation) on "
            "Apple Silicon. Results are correct, only slower — measured on "
            "the machine that filed #1857: ~3x slower interpreter start, "
            "~3.4x slower subprocess-spawn CPU time, both worse for a tool "
            "whose whole job is spawning subprocesses. Install a native "
            "arm64 python3.")
        lines.append("")

    lines.append("## Interpreter")
    lines.append(f"- executable: {interp['executable']}")
    lines.append(f"- version: {interp['version']}")
    lines.append(f"- machine: {interp['machine']} ({interp['platform']})")
    if interp["platform"] == "darwin":
        rosetta = interp["rosetta"]
        if rosetta is True:
            lines.append("- rosetta: yes — translated, see warning above")
        elif rosetta is False:
            lines.append("- rosetta: no — native")
        else:
            lines.append("- rosetta: could not tell "
                         "(sysctl.proc_translated did not answer)")
    else:
        lines.append("- rosetta: not applicable (checked on macOS only)")
    lines.append("")

    topo = _doctor_cpu_topology()
    lines.append("## CPU topology")
    lines.append(f"- logical cpus: {topo['logical_cpus']}")
    if topo["state"] == "split":
        lines.append(f"- performance cores: {topo['performance']}")
        lines.append(f"- efficiency cores: {topo['efficiency']}")
        lines.append("- supertool does not size worker pools itself; a "
                     "caller that does should read this rather than "
                     "logical_cpus alone (#1857).")
    elif topo["state"] == "uniform":
        lines.append("- no performance/efficiency split reported by this host")
    else:
        lines.append("- performance/efficiency split: could not tell "
                     "(no portable way to ask on this platform)")
    lines.append("")

    sym = _doctor_symlink()
    lines.append("## supertool on PATH")
    if sym["which"] is None:
        lines.append("- not found on PATH")
    else:
        lines.append(f"- resolves to: {sym['which']}")
        if sym["symlink_target"]:
            state = "DANGLING" if sym["dangling"] else "ok"
            lines.append(f"- symlink target: {sym['symlink_target']} ({state})")
        if sym.get("path_resolves_to_running_module") is False:
            version_state = sym.get("path_version_state")
            if version_state == "stale":
                lines.append(
                    f"- NOTE: PATH entry answers as supertool "
                    f"{sym['path_version']}, this call is running {_supertool.VERSION} "
                    "— stale build, update or reinstall it.")
            elif version_state == "current":
                pass
            elif sym.get("dangling"):
                # The version check is deliberately not attempted for a
                # dangling symlink, so the `unknown` arm below must not be
                # printed here: it would say "running it with `version` did
                # not answer" about a subprocess that was never spawned, and
                # hedge under the DANGLING line above it, which is a
                # stronger and already-correct diagnosis (#2121).
                pass
            else:
                lines.append(
                    "- NOTE: could not tell whether the PATH entry "
                    f"({sym['which']}) is current — it differs by path from "
                    f"the module answering this call ({sym['running_module']}) "
                    "and running it with `version` did not answer, so this "
                    "is neither confirmed current nor confirmed stale "
                    "(CLAUDE.md: run python3 supertool.py inside a worktree, "
                    "not the global symlink, if that is what this is).")
    lines.append("")

    config = _supertool._load_config()
    lines.append("## Project config")
    lines.append(f"- .supertool.json: {_supertool._CONFIG_PATH or 'not found'}")
    watch_name = ((config.get("ops") or {}).get("watch") or {}).get("watch_name")
    lines.append(f"- watch fleet: {watch_name or 'not configured'}")
    lines.append("")

    lines.append(_doctor_validators_section(config, probe))
    lines.append("")
    lines.append(_doctor_formatters_section(config, probe))
    return "\n".join(lines) + "\n"


def _init_run_git(args, cwd):
    """`git` in ``cwd``, trimmed stdout, or ``None`` on any failure.

    Local rather than reusing `_doctor_tracked_files`'s subprocess pattern:
    that helper runs from the process cwd and `op_init` needs an explicit
    ``cwd`` (the repo root, resolved before this is ever called).
    """
    try:
        r = subprocess.run(["git"] + args, capture_output=True, text=True,
                           timeout=15, cwd=cwd, encoding="utf-8",
                           errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _init_parse_remote(url):
    """Parse a git remote URL into (host, 'namespace/repo'), or None.

    Mirrors `presets/_remote_default.py`'s `parse_remote`. Duplicated rather
    than imported: presets are subprocess-invoked scripts with no package
    boundary into core (`presets/` ships no `__init__.py`), and a sys.path
    splice for one 20-line helper is more fragile than keeping the two in
    sync by sharing the same two regexes verbatim.
    """
    url = url.strip()
    if not url:
        return None
    scp = re.match(r"^[\w.+-]+@([^:/]+):(.+)$", url)  # anchored-ok: url is .strip()ed above
    if scp:
        host, path = scp.group(1), scp.group(2)
    else:
        uri = re.match(  # anchored-ok: url is .strip()ed above
            r"^[a-zA-Z][\w+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", url)
        if not uri:
            return None
        host, path = uri.group(1), uri.group(2)
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    if not host or not path:
        return None
    return host, path


def _init_tracked_files(root):
    """`git ls-files` in ``root``, or ``None`` when the call could not be
    trusted -- not the same question as `_init_run_git`'s "" == None fold.

    `_init_run_git` treats empty stdout the same as a failure, which is the
    right read for a remote URL (an empty answer means no remote either
    way) and the wrong one here: a real `git ls-files` failure (timeout, a
    corrupted index, a permission error) must not read the same as "this
    repo genuinely tracks no files yet" (#858 review). Collapsing the two
    would let `init` silently drop `xml`/`ruff`/`shellcheck` out of a
    generated config with no way for the caller to tell a real gap from a
    tool that could not look -- the exact failure `_doctor_tracked_files`
    a few thousand lines away in this same file exists to avoid, and whose
    None-propagation discipline this mirrors rather than reuses (they run
    against different cwds: `_doctor_tracked_files` reads the process cwd,
    `init` must read an already-resolved repo root that can differ from it
    before the root-vs-cwd check below has even run).
    """
    try:
        r = subprocess.run(["git", "ls-files"], capture_output=True,
                           text=True, timeout=15, cwd=root,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return [seg for seg in r.stdout.split("\n") if seg]


def _init_platform(host):
    """github, gitlab, or None for a host `init` does not recognise.

    github: exact host only (`github.com`). A substring match here would
    reopen the exact spoof #1212 fixed for the preset's own host check
    (`endswith("github.com")` matching `evilgithub.com`) -- `init` generates
    the same `defaults.github_repo` that check exists to protect, one caller
    over.

    gitlab: `gitlab.com`, or a self-hosted instance whose host contains
    "gitlab" -- the same substring convention `presets/_remote_default.py`'s
    `origin_slug` already uses for self-hosted GitLab (`gitlab.dp.tools`
    matches `"gitlab"`), kept consistent here rather than tightened
    unilaterally in one of the two callers.
    """
    host = host.lower()
    # Exact only -- see #1212 above. A substring/suffix match here would
    # match "evilgithub.com" the same way `endswith` once did.
    if host == "github.com":
        return "github"
    # Deliberately loose, unlike the exact match above: substring match,
    # same convention as `presets/_remote_default.py`'s `origin_slug`, so a
    # self-hosted instance like "gitlab.dp.tools" still resolves. Not an
    # oversight, and #1212 does not justify tightening this line too -- it
    # only concerns the github branch above. Do not tighten this to match
    # the github branch without re-reading the docstring above.
    if host == "gitlab.com" or "gitlab" in host:
        return "gitlab"
    return None


# Validator specs `init` may declare, keyed by the tracked-file predicate and
# the binary each one's adapter actually shells out to (`shutil.which(TOOL)`
# in the adapter itself -- `validators/ruff/ruff.py:57`,
# `validators/shellcheck/shellcheck.py:56`). jsonlint is not in this table:
# its adapter is stdlib `json.load()`, no external binary, so it is always
# in scope rather than conditional (#858's own "*.json always").
_INIT_CONDITIONAL_VALIDATORS = (
    # (validator name, tracked-file suffixes, binary to resolve, match glob, spec)
    ("ruff", (".py",), "ruff", "*.py", {
        "cmd": "{python} {supertool_dir}/validators/ruff/ruff.py {file}",
        "match": "*.py",
        "hooks_into": ["edit", "replace", "replace_lines", "paste", "append", "vim"],
        "rollback_on_fail": False,
        "timeout": 30,
    }),
    ("shellcheck", (".sh", ".bash"), "shellcheck", "*.{sh,bash}", {
        "cmd": "{python} {supertool_dir}/validators/shellcheck/shellcheck.py {file}",
        "match": "*.{sh,bash}",
        "hooks_into": ["edit", "replace", "replace_lines", "paste", "append", "vim"],
        "rollback_on_fail": False,
        "timeout": 30,
    }),
)

_INIT_JSONLINT_SPEC = {
    "cmd": "{python} {supertool_dir}/validators/jsonlint/jsonlint.py {file}",
    "match": "*.json",
    "hooks_into": ["edit", "replace", "replace_lines", "paste", "append", "vim"],
    "rollback_on_fail": True,
    "timeout": 10,
}


def op_init(mode: str = "") -> str:
    """Derive and write a starter .supertool.json for the current repo (#858).

    Without a config, every preset op (`gh-pr`, `gh-issue`, `git-trail`,
    `gl-mr`, ...) simply does not exist (#614) -- there is no fallback, only
    silence. This derives what it safely can from the repo itself and
    refuses, writing nothing, everywhere it cannot:

    - **defaults.github_repo / defaults.gitlab_project** from the `origin`
      remote. No remote, an unparseable URL, or a host that is neither
      github.com nor a recognised GitLab host: decline rather than emit a
      plausible-but-wrong repo slug (a config naming the wrong repo answers
      confidently about someone else's PRs).
    - **presets**: the matching platform preset, `git` always, `xml` only if
      a tracked file has that extension. `mcp`/`watch` stay opt-in -- this
      op never turns them on, because both need machine-specific setup
      (an MCP server binary, a watch fleet name) `init` cannot infer.
    - **validators**: `jsonlint` always (its adapter is stdlib `json.load`,
      nothing to resolve). `ruff`/`shellcheck` only when BOTH a tracked file
      matches their extension AND the adapter's own binary
      (`shutil.which(...)`) actually resolves -- declaring a validator that
      cannot run is the silent-pass class this repo's own contract forbids
      (`docs/validators.md` "Declining instead of guessing").
    - **allow_outside_cwd / allow_vim_shell**: always `False`. This repo's
      own `.supertool.json` sets `allow_outside_cwd: true` because it is
      supertool's own checkout -- a stranger's repo must not inherit a
      widened capability it never asked for.
    - **rtk**: always `False` too, but for a different reason -- #858 asks
      for it explicitly, alongside the other two, as one of the "safe
      defaults, not copies of whatever the last repo used". Unlike the
      other two this is not a widened *capability* (`rtk` only decides
      whether `read`/`grep`/`wc` delegate to an optional external binary
      for compressed output, on a repo with no config at all `_rtk_enabled`
      already defaults it to on) -- worth keeping distinct in review, but
      the issue is explicit that the field belongs in this list regardless.

    Two more refusals, both about the write itself rather than what to
    write: an existing `.supertool.json` is never overwritten, hand-written
    or not (`allow_vim_shell`/`allow_outside_cwd` widen what supertool may
    do, so a silent rewrite is worse than doing nothing) -- and merging
    presets into an already-hand-edited config is explicitly out of scope
    for this op (#858 calls that the harder "auto change it for you" half
    and leaves it to a follow-up); `init` only ever creates a file that does
    not exist yet.

    Previews by default -- prints the JSON it would write and does not touch
    disk. `init:write` (or `:apply`/`:commit`) commits it through the same
    `_run_with_validators` wrapper the dispatcher's own `paste` branch uses
    (op_paste() called bare, outside that wrapper, was this op's one direct
    call site in the whole module -- #858 review finding 1) rather than a
    bare `op_paste()` call: mutation counting, notifier firing and rollback
    handling all run the same way a caller-typed `paste` would get them.
    One thing it does NOT buy, and the two must not be conflated: a
    validator jsonlint declares can only be *applicable* by way of an
    already-loaded `.supertool.json` -- and by construction the write only
    ever runs where none exists yet (the refusal above), so the config that
    would validate the file `init:write` just created is not loaded until a
    later, separate call reads it back in.
    """
    write = mode.strip().lower() in ("write", "apply", "commit")

    cwd = os.path.abspath(os.getcwd())
    root = _init_run_git(["rev-parse", "--show-toplevel"], cwd)
    if root is None:
        return ("ERROR: not inside a git working tree (or the repo is bare) "
                "-- init needs a real repo root to derive defaults from.\n")
    root = os.path.abspath(root)
    # Windows: `git rev-parse --show-toplevel` can answer with a lowercase
    # drive letter and forward slashes independent of how `os.getcwd()`
    # spells the same directory. `os.path.normcase` is the fix `_safe_path`
    # already applies to this exact problem elsewhere in this file --
    # POSIX-side it is a no-op, so the comparison stays exact there.
    if os.path.normcase(root) != os.path.normcase(cwd):
        return (f"ERROR: init only writes at the repo root -- run it from "
                f"{root}, not {cwd}.\n")

    config_path = os.path.join(root, ".supertool.json")
    if os.path.isfile(config_path):
        try:
            with open(config_path, encoding="utf-8") as fh:
                existing = fh.read(2000)
        except OSError as exc:
            existing = f"(could not read it to show you: {exc})"
        return (f"ERROR: {config_path} already exists -- refusing to "
                "overwrite it. init never touches a config that is already "
                "there, hand-written or not (allow_vim_shell/"
                "allow_outside_cwd both widen what supertool may do, so a "
                "silent rewrite is worse than doing nothing -- and merging "
                "new presets into an edited file is a harder, separate "
                "problem #858 leaves to a follow-up). Existing contents:\n"
                f"{existing}\n")

    remote_url = _init_run_git(["remote", "get-url", "origin"], root)
    if remote_url is None:
        return ("ERROR: no 'origin' remote -- cannot derive "
                "defaults.github_repo/gitlab_project, and a guessed one "
                "would answer confidently about the wrong repo. Write "
                ".supertool.json by hand.\n")

    parsed = _init_parse_remote(remote_url)
    if parsed is None:
        return (f"ERROR: could not parse origin remote URL "
                f"{_supertool._flat_field(remote_url)!r} -- unrecognised form. Write "
                ".supertool.json by hand.\n")
    host, slug = parsed
    platform = _init_platform(host)
    if platform is None:
        return (f"ERROR: origin remote host {_supertool._flat_field(host)!r} is "
                "neither github.com nor a recognised GitLab host -- init "
                "does not know which preset family to enable. Write "
                ".supertool.json by hand.\n")

    tracked = _init_tracked_files(root)
    if tracked is None:
        return ("ERROR: `git ls-files` did not answer -- cannot tell "
                "whether the xml preset or the ruff/shellcheck validators "
                "belong in this config, and guessing 'no' would silently "
                "drop them for a repo that may well need them. Write "
                ".supertool.json by hand, or re-run init once git answers "
                "here.\n")

    presets = [platform, "git"]
    if any(f.endswith(".xml") for f in tracked):
        presets.append("xml")

    validators = {"jsonlint": dict(_INIT_JSONLINT_SPEC)}
    skipped_validators = []
    for name, suffixes, tool, glob, spec in _INIT_CONDITIONAL_VALIDATORS:
        if not any(f.endswith(suffixes) for f in tracked):
            continue
        if shutil.which(tool):
            validators[name] = dict(spec)
        else:
            skipped_validators.append(
                f"{name} ({glob} tracked, but {tool!r} does not resolve on "
                "PATH)")

    defaults_key = "github_repo" if platform == "github" else "gitlab_project"
    doc = {
        "defaults": {defaults_key: slug},
        "rtk": False,
        "allow_outside_cwd": False,
        "allow_vim_shell": False,
        "presets": presets,
        "validators": validators,
    }
    content = json.dumps(doc, indent=2) + "\n"

    out = []
    if write:
        # Through the same wrapper every "paste" dispatch goes through
        # (`_supertool.py`'s "elif op == \"paste\":" branch), not a bare
        # `op_paste()` call -- that was this op's only direct call site in
        # the module and skipped formatter/validator/rollback/notifier
        # handling entirely (#858 review finding 1). `_OP_TARGETS["paste"]`
        # reads `parts[1]` as the path, so a synthetic parts list gets the
        # real target path validated and reported exactly like a caller
        # who typed `paste:::.supertool.json:::...` themselves.
        out.append(_supertool._run_with_validators(
            "paste", ["paste", ".supertool.json", content],
            lambda: _supertool.op_paste(".supertool.json", content)))
    else:
        out.append(f"PREVIEW -- nothing written. Run 'init:write' to create "
                   f"{config_path}.\n")
        out.append(content)
    if skipped_validators:
        out.append("Declined (tool not resolvable on this PATH -- no "
                   "validator was declared for it; add it by hand once "
                   "installed):")
        out.extend(f"  - {s}" for s in skipped_validators)
    return "\n".join(out) + ("" if out[-1].endswith("\n") else "\n")
