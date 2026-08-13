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

import os
import socket

# Case-insensitive substrings by which an analyser announces it declined to run,
# as opposed to a finding about the file. Deliberately narrow: an exit we cannot
# explain must stay an error, because swallowing an unknown failure is the same
# category mistake in the other direction.
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

    Reserve this for an *absent* tool. A tool that ran and fell over is
    `tool_fault()`, a scope refusal is `skipped()` directly, and neither
    becomes louder because someone asked for the gate to be installed.
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
