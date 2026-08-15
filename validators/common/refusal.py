"""Refusal-to-run detection shared by validator adapters (issue #406).

An analyser that declined to run — path outside its configured scope, no files
matched, no config to work from — has produced NO information about the file.
Reporting that as one error makes an unmeasured file indistinguishable from a
measured broken one, adds a `+1` to the before/after delta that no edit caused,
and can revert a good edit through `rollback_on_fail`.

Adapters route such exits through `skipped()` instead. See validators/SCHEMA.md,
"Skipped: the third state".
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import traceback

# Case-insensitive substrings by which an analyser announces it declined to run,
# as opposed to a finding about the file. Deliberately narrow: an exit we cannot
# explain must stay an error, because swallowing an unknown failure is the same
# category mistake in the other direction.
#
# These match the TOOL's own words, never this module's. `outside_roots()` below
# emits `path outside <VAR> allowlist` and is deliberately not one of these: it
# is a local short-circuit whose string goes straight to `skipped()`, so nothing
# ever asks `is_refusal()` about it. The two were read as a drifted pair in
# #1548 and are not one. Provenance for every entry — the measured emission it
# was written for, or an explicit note that nobody has seen it fire — is pinned
# in tests/test_refusal_pattern_provenance_1548.py, because a pattern that
# matches nothing looks exactly like a refusal that never happened.
REFUSAL_PATTERNS = (
    "--paths allowlist",
    "outside the configured",
    "no files found to analyse",
    "no files found to analyze",
    "no files found to check",
)


class DaemonUnavailable(RuntimeError):
    """The analyser's own binary is not installed for this working directory.

    A distinct type rather than a message-substring guess, because the two
    exits an adapter has to tell apart look identical from the outside: a
    daemon that could not be started, and a daemon that started and said the
    file is broken. Only the raise site knows which one happened, so only the
    raise site gets to label it (#531).

    Subclasses `RuntimeError` so every `except RuntimeError` already written
    around `resolve_bin` keeps catching it — the type narrows the handling
    that wants to narrow, and changes nothing for the handling that does not.

    The rule for raising it: the binary is *absent*. Anything that happens
    after a real binary is found — a spawn that dies, a handshake that times
    out, a daemon that answers badly — is a validator failure and must stay
    loud. Guessing towards silence there is how a broken validator starts
    looking clean.
    """


def daemon_transport_reason(has_uds: bool | None = None) -> str | None:
    """Why no warm daemon is reachable on this build, or None if one is (#544).

    The warm analysers are spoken to over a Unix domain socket. GH-hosted
    Windows Python builds do not expose `socket.AF_UNIX` even though the OS
    supports it — `supertool.py`'s MCP client has carried that note since it
    was written, and `tests/test_security_mcp_daemon_148.py` skips its whole
    module for it. So on those builds there is no transport, and every warm
    validator is unreachable before any question of installation arises.

    Until this existed the adapters walked in anyway and published the crash
    as a finding about the file: `_spawn.ensure_daemon` computes
    `socket_pid_paths` before running its preflight, that reaches
    `_paths.runtime_dir`, and `runtime_dir` calls `os.geteuid()` — which
    Windows also lacks. The blanket `except Exception` in `main` turned the
    `AttributeError` into an `adapter` error about a file nothing had opened.

    **Where the check goes matters more than what it says.** It belongs in the
    body of each adapter's `ensure_daemon`, never at the top of `main`. Three
    suites stub the daemon layer by replacing that whole function
    (`monkeypatch.setattr(mod, "ensure_daemon", ...)`), which is why they never
    needed a real socket and passed on Windows all along. A platform check
    placed ahead of that assignment fires before the stub can take effect, and
    those suites keep reporting green while exercising nothing — including the
    one asserting that a real daemon failure stays loud, which the first
    attempt at this fix silenced with the very guard meant to protect it.

    The binary lookup runs first, before this. Both outcomes are `skipped`, so
    neither fabricates anything, and of the two reasons only one is the
    reader's next action: "install it" beats "this Python cannot reach a
    daemon" when it is not installed either way. Once it *is* installed, this
    reason is what they get.

    `has_uds` is injectable so the contract can be asserted by removing the
    attribute on every platform, rather than only on the runners that happen to
    lack it — the absence of exactly such a test is why this shipped unnoticed.
    """
    if has_uds is None:
        has_uds = hasattr(socket, "AF_UNIX")
    if has_uds:
        return None
    return ("warm daemon needs socket.AF_UNIX, which this Python build does not "
            "expose (Windows) — warm validators cannot run here; cold "
            "validators such as phplint are unaffected")


def is_refusal(msg: str, env_var: str = "") -> bool:
    """Does `msg` read as the tool declining to run rather than a finding?

    `env_var` names an optional comma-separated list of extra substrings, so a
    repo can teach an adapter about a house-specific refusal without a release.

    **Give it the tool's own statement, never a whole output blob.** This is a
    substring test, so over a multi-line capture it answers "does a refusal
    phrase appear anywhere in here" — and a phrase inside unrelated noise then
    displaces whatever the tool actually said, which is a non-verdict published
    over a real one (#1527). `phpstan-mcp` and `phpmd-mcp` pass one structured
    error message and are the shape to copy; `phpstan` passed `stdout + stderr`
    and gates the arm on stream position for it.
    """
    patterns = list(REFUSAL_PATTERNS)
    if env_var:
        patterns += [p.strip().lower()
                     for p in os.environ.get(env_var, "").split(",") if p.strip()]
    lowered = (msg or "").lower()
    return any(p in lowered for p in patterns)


def outside_roots(file_path: str, env_var: str) -> str | None:
    """Is `file_path` outside every analysis root named by `$env_var`? (#412)

    Returns a skip reason when the adapter can answer "not analysed" locally,
    and `None` whenever it cannot — which is the answer for an unset or blank
    var. That asymmetry is the whole design: an adapter cannot read the
    analyser's own scope (it lives in `phpstan.neon`, daemon flags, a config
    this module has no parser for and no business duplicating), so the two
    mistakes it could make are not equally bad.

    - Skipping a file that IS in scope loses the analysis silently. The file
      looks handled and is not. Unacceptable.
    - Analysing a file that is NOT in scope wastes a daemon round trip and
      still returns the right answer. Merely slow.

    So the scope is never inferred and never defaulted on. `$env_var` is an
    explicit statement by the repo that it knows the scope, and unset means
    "no local knowledge — the analyser decides", exactly as before.

    Roots are `os.pathsep`- or comma-separated, absolute or relative to the
    working directory. Matching is on `os.path.abspath` with an `os.sep`
    boundary: a bare prefix compare would read `/srcbad/Foo.php` as inside
    `/src`. Empty entries are dropped rather than abspath()ed into the working
    directory, where a stray separator would widen the allowlist to everything.

    The reason names `env_var`, not the analyser: a wrong skip is caused by
    this configuration, and pointing at a tool that never saw the file sends
    the reader to the wrong file to fix it.
    """
    raw = os.environ.get(env_var, "")
    if not raw.strip():
        return None
    entries = [e.strip() for part in raw.split(os.pathsep) for e in part.split(",")]
    roots = [os.path.normcase(os.path.abspath(e)) for e in entries if e]
    if not roots:
        return None
    target = os.path.normcase(os.path.abspath(file_path))
    for root in roots:
        if target == root or target.startswith(root.rstrip(os.sep) + os.sep):
            return None
    return f"path outside {env_var} allowlist"


def tool_fault(tool: str, returncode: int, output: str, limit: int = 300) -> str:
    """Message for a non-zero exit that said nothing about the file (#745).

    The sibling of `skipped()` on the other side of the same distinction. A
    refusal is an analyser that declined *before* running; this is one that ran,
    fell over, and exited non-zero without producing a verdict — a broken
    extension, a fatal startup error, a path it could not open. Neither is a
    finding about the file.

    Unlike a refusal this stays an **error**, not a third state. A refusal is a
    configured, expected non-answer; a tool that crashed on a file it was asked
    about is a fault someone has to fix, and a fault routed to `skipped` is a
    validator quietly reporting clean. So the caller keeps `ok: False` and
    `count: 1` and changes only the `code` and the message.

    The message names the exit code first, because on the failures this exists
    for the output is frequently the least informative part — and sometimes
    empty, which is why the empty case is spelled out rather than rendered as a
    blank tail. A formatter that goes blank on the input it was written for is
    the defect one layer in (see `tests/_adapter_verdict.py`).
    """
    body = " ".join((output or "").split())
    if not body:
        body = "(no output)"
    elif len(body) > limit:
        body = body[:limit] + f"... (+{len(body) - limit} chars)"
    return (f"{tool} exited {returncode} without reporting anything about the "
            f"file - this is a {tool} failure, not a finding about the file: "
            f"{body}")


#: Env var naming the validators whose tool must be installed. Comma- or
#: `os.pathsep`-separated, case-insensitive, `*` for all.
REQUIRE_VAR = "SUPERTOOL_REQUIRE_VALIDATORS"


def required(tool: str) -> bool:
    """Is `tool` named by $SUPERTOOL_REQUIRE_VALIDATORS? (#665/#667/#668)

    The answer to "a `skipped` on every machine that lacks the binary is how a
    validator becomes decorative". A skip is the right *local* verdict — a
    laptop that never installed shellcheck genuinely has no information about
    the file, and escalating that to a red would make every unrelated edit
    fail. In CI the same skip means the gate is not running, and a gate that
    is not running is the absence-read-as-a-pass this whole module exists for,
    one level up: nobody reads a validator row that says `skipped` on every
    run of every job.

    So the escalation is **opt-in and one-directional**. Unset, nothing
    changes. Set, an absent tool becomes an `adapter` error naming this
    variable, so the fix points at the CI image rather than at the file. It
    can only turn quiet into loud; there is deliberately no value that turns a
    finding into a skip, because that would be a mute button and this repo
    declines those.

    It does **not** make a *clean* result louder and does not touch findings.
    The only thing it changes is what an adapter does when it has nothing to
    say.
    """
    raw = os.environ.get(REQUIRE_VAR, "")
    if not raw.strip():
        return False
    names = [n.strip().lower()
             for part in raw.split(os.pathsep) for n in part.split(",")]
    return "*" in names or tool.lower() in names


def required_but_absent(tool: str, reason: str) -> str:
    """The message for a tool that is required by config and not installed."""
    return (f"{tool} is named in ${REQUIRE_VAR} but could not run, so this "
            f"file was NOT checked: {reason}")


def absent(tool: str, file_path: str, reason: str, dur_ms: int) -> dict:
    """The absent-tool arm of every adapter, as one call (#1202).

    `required()` shipped as a helper each adapter had to remember to call, and
    six of them did. The rest spelled the same moment three other ways: a
    `skipped` that could never be escalated (`ruff`, `html-check`, the four MCP
    adapters), or — worse and on the adjacent line — a fabricated
    `{"ok": true, "count": 0, "errors": []}` about a file nothing opened
    (`tsc-check`, `markdownlint`, `ruby-check`, `cargo-check`, `hadolint`,
    `gofmt-check`, `terraform-check`, `pyright`, `git-status`, `yaml-check`).

    An opt-in switch that a caller sets and cannot tell they set is worse than
    no switch, and a clean verdict from a checker that never ran is the failure
    this whole module exists to end. Both were spelled adapter by adapter, so
    both were only ever as good as the last author's memory. They are one call
    now: an adapter states the absence and its reason, and where that lands is
    decided here.

    `tool` is the **validator name** — the key a repo writes in
    `.supertool.json` — never the binary. `tsc-check` runs `tsc` and
    `html-check` runs `node`; escalating on the binary name would ignore the
    only spelling anyone can configure.

    Reserve this for a tool that could not be *reached*. Absent is the usual
    reason; the four MCP adapters also land here when there is no warm daemon
    and `$SUPERTOOL_MCP_AUTOSPAWN` forbids raising a cold one (#1743), which is
    the same sentence — this file was not checked, and the repo that named the
    validator in `$SUPERTOOL_REQUIRE_VALIDATORS` is owed the loud version.

    A tool that ran and fell over is `tool_fault()`, and a scope refusal is
    `skipped()` directly: that one is a decision about which files this
    validator covers, so it does not become louder because someone asked for
    the gate to be installed.
    """
    if required(tool):
        return {"tool": tool, "file": file_path, "ok": False, "count": 1,
                "errors": [{"line": None, "col": None, "severity": "error",
                            "code": "adapter",
                            "msg": required_but_absent(tool, reason)}],
                "duration_ms": dur_ms}
    return skipped(tool, file_path, reason, dur_ms)


def skipped(tool: str, file_path: str, reason: str, dur_ms: int) -> dict:
    """A SCHEMA.md result in the third state: not clean, not broken, not looked at.

    The verdict keys `ok`, `count` and `errors` are **omitted**, not padded (#515).
    This helper padded them for a year while `docs/validators.md` told adapter
    authors to omit them, so a hand-rolled adapter and every helper caller
    published different shapes and nothing caught it.

    Omitting won because the argument for padding — "consumers can read
    `result['ok']` without a branch" — is not true of this codebase. Every core
    consumer tests `"skipped" in result` before touching a verdict key
    (`_validator_regressed`, `_validator_result_is_cacheable`,
    `_validator_render_row`, `_validator_render_diff`), and has to: the reason
    string exists only on a skip. The core's own built-in skips already omitted
    the keys, so padding here made the shared helper the odd one out. And an
    `ok: true` on a receipt that means "never looked at" is precisely the
    absence-read-as-a-pass failure the third state was introduced to end.

    `tool`, `file` and `duration_ms` stay: they describe the attempt, not a
    verdict about the file.
    """
    return {"tool": tool, "file": file_path, "duration_ms": dur_ms,
            "skipped": reason}


#: How much of the crash message survives into the payload. The core truncates
#: again at 300 for display (`_flat_cell`), so this is not the display width —
#: it is the ceiling on what one broken adapter can push into a receipt, a log
#: and a cache entry. The class name is first and the traceback last, so a cut
#: takes frames off the end rather than the diagnosis off the front.
CRASH_MSG_LIMIT = 1000


def crashed(tool: str, file_path: str, exc: BaseException, dur_ms: int) -> dict:
    """An exception escaped the adapter. Say so, in the shape the core reads (#1697).

    **Not `skipped()`, and the difference is mechanical rather than stylistic.**
    An adapter cannot set `no_verdict` — it is in the core's
    `_VALIDATOR_CORE_ONLY_KEYS` and is stripped on the way in — and
    `_validator_no_verdict` returns `None` the moment `skipped` is a key. So a
    crash published as a skip reaches neither `_note_not_checked` nor
    `_NOT_CHECKED`, and the call exits **0**: quieter than the bare crash it
    replaced, which the core itself renders as a `no_verdict` skip and does
    count. A crash net that lowers the alarm is worse than no crash net, and
    that is the whole trap this function exists to not fall into.
    `tool_fault()` already says it in prose one screen up: "a fault routed to
    `skipped` is a validator quietly reporting clean."

    `code: "adapter"` is the channel. `_validator_not_checked` keys on
    `all(code == "adapter")`, which makes this a **non-verdict**: rendered
    `NOT CHECKED`, never subtracted from a baseline, never a regression, never
    a rollback — and still loud, because `_NOT_CHECKED` growing fails the call.
    It is the third state, delivered through the door the core already built.

    **The class name is published even when the exception says nothing.** The
    net this replaced (`ruby-check`'s, the only one in the tree until now)
    emitted `str(exc)` alone, and `str(KeyError())`, `str(RecursionError())`
    and `str(SystemError())` are all empty — so its most likely output was a
    row with a blank reason, which is the absence-as-presence defect wearing
    the fix's clothing. Type first, message if there is one, tail of the
    traceback last.

    **The whole message is flattened onto one line here rather than at the
    renderer.** A traceback carries newlines, and this repo has already been
    bitten by the five *other* separators `str.splitlines()` breaks on writing
    a second row at column 0 (#1522). `str.split()` splits on all of them, so
    the join is the fix and it is applied to the finished string.

    Nothing here can raise a `UnicodeEncodeError` at the `print`: the caller
    emits through `json.dumps` with the default `ensure_ascii=True`, so the
    bytes offered to a cp1252 console are ASCII whatever the exception said.
    That matters because a crash net that dies encoding its own report on
    Windows leaves exactly the empty stdout it was written to prevent.
    """
    frames = traceback.format_exception(type(exc), exc, exc.__traceback__)
    # Python 3.11+ prints `^^^^` / `~~~~` anchor rows under the failing
    # expression and elides long call chains as `...<2 lines>...`. Both are
    # decoration for a terminal showing the frame above them, and this keeps
    # only three lines: on the first crash measured through this helper
    # (`rector-mcp`, a daemon that never came up) two of the three were anchors
    # and the third was a bare `)`, so the tail said nothing at all.
    lines = [ln for ln in "".join(frames).splitlines()
             if ln.strip()
             and set(ln.strip()) - set("^~")
             and not (ln.strip().startswith("...<")
                      and ln.strip().endswith(">..."))]
    tail = " | ".join(lines[-3:])
    detail = str(exc).strip()
    msg = "{0} adapter crashed and did NOT check this file: {1}{2} | trace: {3}".format(
        tool, type(exc).__name__, ": " + detail if detail else "", tail)
    return {"tool": tool, "file": file_path, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter",
                        "msg": " ".join(msg.split())[:CRASH_MSG_LIMIT]}],
            "duration_ms": dur_ms}


def guard_main(tool: str, main, *args) -> int:
    """Run an adapter's `main`, and publish anything that escapes it (#1697).

    An adapter's contract is one JSON object on stdout. An exception escaping
    `main` writes **none**, and the core's fallback
    (`_validator_unusable_reply`, "produced no output") can then report only
    *that* the adapter died — the traceback went to a stderr the core captures
    and never reads. This is that stderr, moved onto the channel somebody
    looks at.

    **It wraps the call, not a region inside `main`.** Every net in the tree
    before this covered a region: `ruby-check`'s sat around `main()` at module
    level and was the only complete one, while the four MCP adapters wrapped
    `ensure_daemon` + `ndjson_call` and left the `print(json.dumps(
    format_response(...)))` on the next line outside every handler they had.
    So the count of adapters that could reach the process boundary with stdout
    empty was 35 of 36, not the 31 that have no `except Exception` at all — a
    handler that exists is not a handler that covers, and only the call site
    covers the whole callee.

    Five spellings for one moment is the drift #1727 closed elsewhere; this is
    one spelling, and `tests/test_adapter_crash_net_1697.py` fails for any
    adapter that does not use it.

    `Exception`, not `BaseException`: `KeyboardInterrupt` and `SystemExit` are
    the operator and the adapter respectively asking to stop, and neither is a
    crash to report. An adapter that has already emitted and then dies still
    publishes here, and the core reads the **last** stdout line, so the later
    fact wins — which is correct, because a run that fell over after speaking
    did not finish checking the file.

    Returns `main`'s own value, so `sys.exit(guard_main(...))` and a bare
    `guard_main(...)` both keep the exit code the adapter already had. On a
    crash it returns 0: the receipt is on stdout, the core does not read the
    exit code, and SCHEMA.md reserves a non-zero exit for infrastructure
    failure rather than for a decline the adapter published.
    """
    started = time.monotonic()
    try:
        return main(*args)
    except Exception as exc:  # noqa: BLE001 — the point is that it is blanket
        target = sys.argv[1] if len(sys.argv) > 1 else ""
        print(json.dumps(crashed(tool, target, exc,
                                 int((time.monotonic() - started) * 1000))))
        return 0
