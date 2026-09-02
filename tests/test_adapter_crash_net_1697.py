"""#1697 -- an exception escaping an adapter's `main` must still publish a verdict.

An adapter writes one JSON object on stdout and the core reads it. When an
exception escapes `main`, stdout is **empty**, and empty stdout is not "no
findings" -- it is "nobody checked this file". This repo's named defect class,
sitting in the layer built to detect it.

**What the filed issue got right, and where the measurement moves.** #1697
counted 31 of 36 adapters with no module-level handler, and that count is
correct as a count of `except Exception` statements. But the contract is not
"does an `except` exist", it is "can an exception reach the process boundary
with stdout empty", and measured that way it is **35 of 36**: the four MCP
adapters wrap only `ensure_daemon` + `ndjson_call`, so the `print(json.dumps(
format_response(...)))` on the line after their handler is outside every net
they have. Only `ruby-check`, whose net is at module level around `main()`
itself, survives the probe below. A handler that exists is not a handler that
covers.

**What the filed issue got wrong.** It says callers `json.loads()` the empty
stdout "as a clean run". The core does not, and has not since #634:
`_validator_run_one` tests `if not out` before parsing and returns
`_validator_unusable_reply(..., "produced no output")`. The cost of a missing
net is therefore not a false clean -- it is a **discarded diagnosis**. The
adapter's traceback goes to stderr, which `_validator_run_one` captures and
never reads, so the operator is told a checker died and never told why.

**Why the net publishes an `adapter` error and not `skipped()`.**
`refusal.skipped()` is the third state for a checker that declined *before*
running, and it is the wrong shape here for a mechanical reason this file
asserts rather than asserts around: an adapter cannot set `no_verdict` (it is
in `_VALIDATOR_CORE_ONLY_KEYS` and is stripped on the way in), and
`_validator_no_verdict` returns `None` the moment `"skipped"` is a key. So a
`skipped` crash net never reaches `_note_not_checked`, `_NOT_CHECKED` never
grows, and the call exits **0** -- quieter than the bare crash it replaced,
which reaches exit 1 through the core's own `no_verdict` reply. A crash net
that lowers the alarm is worse than no crash net. `refusal.tool_fault()` says
the same thing in prose already: "a fault routed to `skipped` is a validator
quietly reporting clean".

An `adapter`-coded error is read by `_validator_not_checked` (its test is
`all(code == "adapter")`), which `_validator_no_verdict` returns, which renders
`NOT CHECKED`, never regresses, never rolls an edit back, and does grow
`_NOT_CHECKED`. It is the third state, delivered through the channel this repo
already built for it. Both halves are pinned below, in the same test.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import supertool  # noqa: E402
from _adapter_budget import adapter_budget  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
VALIDATORS = REPO / "validators"
FORMATTERS = REPO / "formatters"

#: The message the injected exception carries. Asserted on, so a payload that
#: merely exists is not enough -- it has to be *about* the failure that
#: happened. A net that catches and then reports something else is the
#: absence-read-as-presence defect one layer along.
MARKER = "supertool-crash-probe-1697"

#: Formatter adapters mutate the file they are pointed at (#2159) -- unlike
#: every validator this sweep otherwise drives, so pointing one at a real
#: tracked file (`presets/gitlab.json` below) would rewrite it if the real
#: binary is on PATH. Each is redirected at a stub that exits 0 without
#: touching the file, through the same `*_BIN` env var its own test suite
#: already uses to stub it (see `tests/test_formatters_ruff_format.py`), so
#: the sweep exercises the adapter's own crash net without depending on the
#: tool being installed or writing to a file this suite does not own.
_FORMATTER_BIN_ENV = {
    "ruff-format": "RUFF_BIN",
    "php-cs-fixer": "PHPCSFIXER_BIN",
    "prettier-write": "PRETTIER_BIN",
    "phpcbf": "PHPCBF_BIN",
}

_NOOP_STUB_CMD: str | None = None


def _formatter_noop_bin() -> str:
    """A `python <stub>` command line for a binary that exits 0 and never
    touches its argv file -- built once and reused for every formatter case.
    """
    global _NOOP_STUB_CMD
    if _NOOP_STUB_CMD is None:
        import shlex
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".py",
                                     prefix="supertool_formatter_noop_1697_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("import sys" + chr(10) + "sys.exit(0)" + chr(10))
        _NOOP_STUB_CMD = "{0} {1}".format(
            shlex.quote(sys.executable), shlex.quote(path))
    return _NOOP_STUB_CMD


def _has_main_block(path: Path) -> bool:
    """Whether `path` has a runnable `if __name__ == "__main__":` block.

    #2174's census gap: the previous exclusion of `common/` was by
    *directory*, on the stated grounds that it "holds shared helpers with no
    `main` and no `__main__` block" -- true of every file there except
    `ci_lint_resolve_root.py`, which has both and calls `guard_main` from its
    own `__main__` block exactly as a validator or formatter adapter does.
    Excluding by directory made that one file invisible to this sweep;
    excluding by the actual property (does this file have something to run
    as `__main__`?) does not, and does not depend on anyone remembering to
    special-case a filename the next time `common/` grows a second one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(r'(?m)^if __name__ == .__main__.\s*:', text) is not None


def _adapters():
    """Every adapter under `validators/*/*.py` and `formatters/*/*.py`,
    `common/` excluded.

    `guard_main`'s contract is not "every validator" -- SCHEMA.md's one-JSON-
    object-on-stdout promise is the adapter contract, and a formatter is an
    adapter under that contract exactly as a validator is (#2159). Read
    narrowly as "wherever #1697 happened to be found", the population is
    `validators/*/*.py` alone, and that is what the previous glob asserted;
    read from the contract's own reach, it is every directory under either
    root that emits this JSON, `common/` excluded because it holds shared
    helpers with no `main` and no `__main__` block -- see `_has_main_block`
    for the one file under `common/` that does not fit that description and
    is swept separately, as `RESOLVERS` below, rather than folded in here:
    it does not share the JSON-verdict (`ok`/`skipped`) contract this file's
    positive control asserts on an uninjected run, only the crash-net one.
    """
    found = sorted(p for p in VALIDATORS.glob("*/*.py")
                   if p.parent.name != "common")
    found += sorted(p for p in FORMATTERS.glob("*/*.py")
                    if p.parent.name != "common")
    assert len(found) >= 30, (
        "the adapter sweep found {0} files -- a glob that stopped matching "
        "makes every parametrised case below disappear rather than "
        "fail".format(len(found)))
    return found


def _common_helpers_with_main():
    """`common/` files that #2174 says should not be invisible to the
    crash-net sweep: they have their own `__main__` block and route through
    `guard_main`, so an exception escaping them is exactly #1697's failure
    mode. Not merged into `ADAPTERS` -- see `_adapters`'s own docstring for
    why -- but still driven through the crash-net tests below via
    `CRASH_NET_TARGETS`.
    """
    found = []
    for base in (VALIDATORS, FORMATTERS):
        common = base / "common"
        if not common.is_dir():
            continue
        found.extend(p for p in sorted(common.glob("*.py"))
                     if _has_main_block(p) and "guard_main" in p.read_text(
                         encoding="utf-8"))
    return found


def _ids(paths):
    return [p.parent.name for p in paths]


ADAPTERS = _adapters()
RESOLVERS = _common_helpers_with_main()
#: The crash-net tests (#1697's actual subject: does an exception escaping
#: `main` still publish a verdict) are agnostic to whether the file also
#: answers the JSON-verdict adapter contract on a normal run -- a resolver
#: does not, an adapter does -- so they are driven over the union. Only the
#: positive control that asserts an uninjected run carries `"ok"`/`"skipped"`
#: stays `ADAPTERS`-only; `ci_lint_resolve_root.py`'s own normal-run contract
#: (a bare path, or nothing) is pinned by `tests/test_validators_ci_lint_1797.py`
#: instead.
CRASH_NET_TARGETS = ADAPTERS + RESOLVERS


#: Driver run as `python -c`, with the adapter path passed through argv rather
#: than interpolated into the source: a Windows path is full of backslashes and
#: baking one into a Python string literal escapes them (`_adapter_budget` and
#: `test_adapter_duration_is_measured_1683` both take this route, for this).
#:
#: **The injection point is `json.dumps`, and it is chosen to be universal.**
#: Every adapter in this tree publishes through it -- `emit()` wraps it, the MCP
#: adapters call `print(json.dumps(...))` directly -- so making the first call
#: raise puts an exception in front of *whatever* verdict that adapter was
#: about to write, on whichever of its paths this machine happens to reach.
#: Patching `open`/`subprocess.run` instead reaches only 33 of the 36: three
#: adapters legitimately decline before touching either (`cargo-check` with no
#: Cargo.toml above the file, `go-vet` with no go.mod, `yaml-check` with PyYAML
#: absent), and a case that never injected anything would have passed on the
#: silence of a probe that did nothing -- this suite's whole subject.
#:
#: **The failure injected is the adapter's, not the net's**, so a call arriving
#: from `validators/common/` is passed through. Without that exemption the probe
#: breaks the very publish path it is asking about, and it does not fire only in
#: theory: `rector-mcp` raised a genuine `RuntimeError` out of `ensure_daemon`
#: ("did not publish a usable socket within 30s") before reaching any
#: `json.dumps` of its own, so the net's emission WAS the first call, the probe
#: killed it, and a working net was reported as an empty one. Exempting by
#: caller rather than by call ordinal is what makes the assertion about the
#: adapter. It buys reach at the price of one coupling, stated rather than
#: hidden: a net that publishes from inside the adapter's own file is refused
#: here. That is deliberate -- five hand-rolled nets is what #1697 was about --
#: and `test_every_adapter_routes_main_through_the_shared_net` is where it is
#: enforced in the open rather than as a side effect of this driver.
#:
#: The two directory names are compared after `abspath` and `normcase`, not as
#: raw text: the driver is handed whatever path the caller had, and on Windows
#: the two spellings of one directory differ in separator and in case.
_DRIVER = chr(10).join((
    "import json, os, pathlib, runpy, sys",
    "adapter, target, mode = sys.argv[1], sys.argv[2], sys.argv[3]",
    "sys.argv = [adapter, target]",
    # `adapter` is either `validators/NAME/NAME.py` or `formatters/NAME/NAME.py`
    # -- both two directories below the repo root -- and `common/` lives only
    # under `validators/`, so it is addressed from the repo root rather than
    # relative to whichever of the two the adapter came from (#2159).
    "_repo = pathlib.Path(adapter).resolve().parent.parent.parent",
    "_common = str(_repo / 'validators' / 'common')",
    "sys.path.insert(0, _common)",
    "def _same_dir(a, b):",
    "    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(b)",
    "class InjectedAdapterFailure(Exception):",
    "    pass",
    "if mode == 'inject':",
    "    _real = json.dumps",
    "    def _hostile(*a, **k):",
    "        _who = sys._getframe(1).f_globals.get('__file__', '')",
    "        if _who and _same_dir(os.path.dirname(_who), _common):",
    "            return _real(*a, **k)",
    "        raise InjectedAdapterFailure({0!r})".format(MARKER),
    "    json.dumps = _hostile",
    "runpy.run_path(adapter, run_name='__main__')",
))


def _drive(adapter: Path, target: Path, mode: str):
    """Run `adapter` as `__main__` in its own process. Returns the CompletedProcess.

    A subprocess and `run_name='__main__'`, not an import and a `main()` call:
    `ruby-check`'s net lives in its `if __name__ == "__main__"` block, so an
    in-process `mod.main()` walks straight past the one net that exists today
    and would report it broken. The core spawns these adapters; so does this.

    `SUPERTOOL_MCP_AUTOSPAWN=0` because the core sets it for every adapter that
    has not asked for a cold start, and because without it each of the four MCP
    adapters spends its full spawn budget failing to raise a daemon on a machine
    that has none -- two minutes of suite time buying a less faithful call than
    the one the core makes.
    """
    env = dict(os.environ)
    env["SUPERTOOL_MCP_AUTOSPAWN"] = "0"
    bin_env = _FORMATTER_BIN_ENV.get(adapter.parent.name)
    if bin_env:
        env[bin_env] = _formatter_noop_bin()
    return subprocess.run(
        [sys.executable, "-c", _DRIVER, str(adapter), str(target), mode],
        capture_output=True, text=True, timeout=adapter_budget(adapter),
        cwd=str(REPO), env=env, encoding="utf-8", errors="replace")


def _last_json(proc, who: str) -> dict:
    out = (proc.stdout or "").strip()
    assert out, (
        "{0}: stdout was EMPTY. The core reads this as `produced no output` "
        "and the reason the adapter died -- which is on stderr, which the core "
        "captures and discards -- is gone (#1697). stderr tail:{1}{2}".format(
            who, chr(10), chr(10).join((proc.stderr or "").splitlines()[-4:])))
    payload = json.loads(out.splitlines()[-1])
    assert isinstance(payload, dict), "{0}: not a JSON object: {1!r}".format(
        who, payload)
    return payload


# ---------------------------------------------------------------------------
# The positive controls. Without these the silence assertions below pass for
# free on any harness that produces nothing -- which is the bug, not the test.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("adapter", ADAPTERS, ids=_ids(ADAPTERS))
def test_the_probe_runs_the_adapter_and_can_see_what_it_says(adapter) -> None:
    """MUST FIRE: uninjected, every adapter speaks and the probe hears it.

    This is the half that fails loudly when the harness is broken. If a driver
    typo, a path separator or an import error stopped the adapter running at
    all, every case below would see empty stdout -- indistinguishable from the
    defect, and green the moment the defect is "fixed" by anything.
    """
    proc = _drive(adapter, REPO / "presets" / "gitlab.json", "off")
    payload = _last_json(proc, adapter.parent.name + " (uninjected)")
    assert "ok" in payload or "skipped" in payload, (
        "an uninjected run must produce a verdict or a stated skip, so that "
        "the injected run below is compared against something: " + repr(payload))


def test_a_real_finding_still_reaches_stdout(tmp_path: Path) -> None:
    """MUST FIRE: the net must not be bought by swallowing real verdicts.

    The cheapest wrong fix for #1697 is a handler wide enough to eat the
    adapter's own error paths. `jsonlint` on malformed JSON has to keep saying
    `syntax`, with a line number, through whatever net is wrapped around it.
    """
    target = tmp_path / "bad.json"
    target.write_text('{"a": [1, 2,}', encoding="utf-8")
    adapter = VALIDATORS / "jsonlint" / "jsonlint.py"
    payload = _last_json(_drive(adapter, target, "off"), "jsonlint finding")
    assert payload.get("ok") is False, payload
    assert payload["errors"][0]["code"] == "syntax", (
        "a malformed file is a finding about the file, not an adapter "
        "fault: " + repr(payload))
    assert payload["errors"][0]["line"] is not None, payload


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("adapter", ADAPTERS, ids=_ids(ADAPTERS))
def test_an_exception_escaping_main_still_publishes_a_verdict(adapter) -> None:
    """MUST NOT BE SILENT: an adapter that dies says so on stdout, with why.

    `ADAPTERS`, not `CRASH_NET_TARGETS`: `_drive`'s `inject` mode hijacks the
    adapter's *own* first call to `json.dumps` to synthesize a crash, which
    depends on the adapter calling it at all on a normal run -- true of every
    validator and formatter, since SCHEMA.md's contract IS one JSON object on
    stdout. A `RESOLVERS` file has no such call on its happy path (a bare
    path, or nothing) so there is no `json.dumps` for this harness to hijack
    and the driven process would just... resolve normally, proving nothing
    about its crash net. `test_every_adapter_routes_main_through_the_shared_net`
    below is where `RESOLVERS` is actually swept, by reading source rather
    than by injecting a synthetic crash this harness cannot manufacture for
    a non-JSON-emitting `main`.
    """
    name = adapter.parent.name
    proc = _drive(adapter, REPO / "presets" / "gitlab.json", "inject")
    payload = _last_json(proc, name + " (crash injected)")

    assert payload.get("ok") is False, (
        "{0} survived a crash and published `ok: {1!r}` -- a checker that "
        "died reporting on a file it never finished reading".format(
            name, payload.get("ok")))
    errors = payload.get("errors") or []
    assert errors, "{0}: a crash payload with no errors row: {1!r}".format(
        name, payload)
    assert all(e.get("code") == "adapter" for e in errors), (
        "{0}: a crash is an adapter fault, and `code` is what routes it to "
        "`_validator_not_checked`. Any other code makes it a finding about "
        "the user's file: {1!r}".format(name, errors))

    blob = " ".join(str(e.get("msg", "")) for e in errors)
    named = re.search(r"crashed and did NOT check this file: (\w+)", blob)
    assert named, (
        "{0}: the payload does not name the exception class that killed it, "
        "so the diagnosis is still lost -- a net that publishes a generic "
        "sentence has converted a traceback into a shrug: {1!r}".format(
            name, blob))

    # Three states, not two. The injected failure is the usual one, but an
    # adapter can genuinely fall over first. The instance this was written for
    # was `rector-mcp` raising `RuntimeError: daemon 'rector-warm' did not
    # publish a usable socket` on any machine with no warm rector, before it
    # reaches a `json.dumps` for the probe to take -- historical since #1743,
    # because `_drive` sets SUPERTOOL_MCP_AUTOSPAWN=0 and the four MCP adapters
    # now read it, decline in milliseconds and reach the `json.dumps` in their
    # own skip arm. The branch stays: it is about any adapter that dies before
    # the marker, not about that one. That case is NOT skipped and NOT waved
    # through: it is the
    # same contract met by a real crash instead of a synthetic one, so it still
    # had to publish a class-named payload, which is what the assertions above
    # and below require of it. The only claim it cannot also make is the
    # marker's, and stating which of the two happened is the whole point of
    # branching rather than loosening the assertion for everybody.
    if MARKER in blob:
        assert "InjectedAdapterFailure" in blob, (
            "{0}: the payload carries the injected message but not its class, "
            "so `str(exc)` is being published alone -- the shape that made a "
            "`KeyError()` render as a blank reason: {1!r}".format(name, blob))
    else:
        assert named.group(1) != "InjectedAdapterFailure", (
            "{0}: the class is named but its message is gone: {1!r}".format(
                name, blob))

    assert payload.get("tool") == name, (
        "{0}: the crash payload names tool {1!r} -- the row would be "
        "attributed to the wrong checker".format(name, payload.get("tool")))


@pytest.mark.parametrize("adapter", ADAPTERS, ids=_ids(ADAPTERS))
def test_the_core_reads_a_crashed_adapter_as_not_checked(adapter) -> None:
    """The payload is only worth publishing if the core routes it correctly.

    `ADAPTERS`, not `CRASH_NET_TARGETS` -- same reason as the injection test
    above: this depends on `_drive`'s `inject` harness producing a genuine
    crash payload, which it cannot for a `RESOLVERS` file with no normal-path
    `json.dumps` call to hijack.

    `NOT CHECKED`, never a regression, never a rollback -- the promise
    `_validator_measured_count` and `_validator_regressed` make for every
    `code: "adapter"` row.
    """
    proc = _drive(adapter, REPO / "presets" / "gitlab.json", "inject")
    payload = _last_json(proc, adapter.parent.name + " (crash injected)")
    assert supertool._validator_no_verdict(payload) is not None, (
        "the core would render this crash as a finding about the file "
        "instead of `NOT CHECKED`: " + repr(payload))
    assert supertool._validator_regressed({"ok": True, "count": 0}, payload) is False, (
        "a crashed checker was read as a regression -- on a "
        "`rollback_on_fail` validator that reverts the user's edit: "
        + repr(payload))


# ---------------------------------------------------------------------------
# Why `skipped()` was refused. Both directions, in one test.
# ---------------------------------------------------------------------------

def _not_checked_names(payload: dict):
    """The names `_note_not_checked` would record for one result."""
    before = list(supertool._acc_not_checked())
    supertool._note_not_checked({"probe": payload})
    after = list(supertool._acc_not_checked())
    del supertool._acc_not_checked()[len(before):]
    return after[len(before):]


def test_a_skipped_crash_net_would_be_quieter_than_no_net_at_all() -> None:
    """MUST FIRE / MUST NOT FIRE, on the same fixture.

    The argument against spelling the crash net `refusal.skipped()`, run rather
    than asserted in prose. If this ever goes green in both directions the
    choice made in this PR has stopped being load-bearing and the docstring at
    the top of this file is wrong.
    """
    sys.path.insert(0, str(VALIDATORS / "common"))
    import refusal  # noqa: E402

    crashed = refusal.crashed("probe", "f.py", RuntimeError("boom"), 12)
    declined = refusal.skipped("probe", "f.py", "boom", 12)

    # MUST FIRE: the crash reaches the counter that decides the exit code.
    assert _not_checked_names(crashed) == ["probe"], (
        "a crashed adapter did not reach `_NOT_CHECKED`, so `supertool "
        "'edit:...' && git commit` would commit on a checker that died: "
        + repr(crashed))

    # MUST NOT FIRE: the same event spelled `skipped` reaches nothing. This is
    # not a defect in `skipped` -- it is correct for a checker that declined --
    # it is why `skipped` is the wrong word for a checker that died.
    assert _not_checked_names(declined) == [], (
        "`skipped` now escalates; if that is deliberate the reasoning at the "
        "top of this file needs rewriting rather than this assertion "
        "deleting: " + repr(declined))


# ---------------------------------------------------------------------------
# The durable half: a 37th adapter cannot arrive without a net.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("adapter", CRASH_NET_TARGETS, ids=_ids(CRASH_NET_TARGETS))
def test_every_adapter_routes_main_through_the_shared_net(adapter) -> None:
    """One spelling, not thirty-six.

    Before this, five adapters had a net and they disagreed: `ruby-check` wrote
    a module-level `try/except` by hand, the four MCP adapters each wrote the
    same nine-line handler with their own name in it, and the two shapes caught
    different regions of the same function. That is the "two adjacent call
    sites, different strictness" shape #1727 closed. The behavioural cases
    above would pass on thirty-six hand-rolled nets; this one is what stops the
    thirty-seventh being written from memory.
    """
    source = adapter.read_text(encoding="utf-8")
    assert "guard_main" in source, (
        "{0} does not route its `main` through `refusal.guard_main`, so an "
        "exception escaping it writes empty stdout (#1697)".format(
            adapter.relative_to(REPO).as_posix()))

# ---------------------------------------------------------------------------
# The two ways the net could die reporting its own death
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("encoding", ["cp1252", "cp437", "ascii", "utf-8"])
def test_the_crash_net_survives_a_console_that_cannot_spell_the_message(
        encoding: str) -> None:
    """A net that raises `UnicodeEncodeError` at its own `print` leaves the
    empty stdout it exists to prevent -- and does it on Windows only, where
    nobody writing it is looking.

    This is not reasoned, it is driven: `PYTHONIOENCODING` sets
    `sys.stdout.encoding` on every platform, and that is the value `print`
    encodes against. The exception carries an arrow, a check mark, CJK and an
    astral emoji; `json.dumps` defaults to `ensure_ascii=True`, so what reaches
    the console is pure ASCII with escape sequences, whatever the codepage is.
    """
    driver = chr(10).join((
        "import sys",
        "sys.path.insert(0, sys.argv[1])",
        "import refusal",
        "def boom():",
        "    raise ValueError('checker said ' + chr(0x2192) + chr(0x2713)"
        " + chr(0x4E2D) + chr(0x1F600))",
        "sys.exit(refusal.guard_main('probe', boom))",
    ))
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = encoding
    proc = subprocess.run(
        [sys.executable, "-c", driver, str(VALIDATORS / "common")],
        capture_output=True, text=True, timeout=60, env=env,
        encoding="utf-8", errors="replace")
    assert proc.returncode == 0, (
        "the net died publishing its own report on a {0} console; stderr:{1}"
        "{2}".format(encoding, chr(10), proc.stderr))
    out = proc.stdout.strip()
    assert out, "empty stdout on a {0} console".format(encoding)
    assert out.isascii(), (
        "the payload carries non-ASCII bytes, so the `print` that emits it is "
        "at the mercy of the console codepage: " + repr(out))
    payload = json.loads(out.splitlines()[-1])
    assert payload["ok"] is False
    assert chr(0x2192) in payload["errors"][0]["msg"], (
        "escaping must survive the round trip, not replace the message")


@pytest.mark.parametrize("exc,expected", [
    (KeyError(), "KeyError"),
    (RecursionError(), "RecursionError"),
    (ValueError("something went wrong"), "ValueError"),
])
def test_the_class_is_published_even_when_the_exception_says_nothing(
        exc, expected) -> None:
    """`str(KeyError())` is the empty string.

    The net this replaced emitted `str(exc)[:300]` alone, so the two most
    likely crashes in a Python adapter -- a missing dict key and a runaway
    recursion -- produced a row whose reason was blank. A blank reason on a
    `NOT CHECKED` row is the absence-read-as-presence defect wearing the fix's
    clothing.
    """
    sys.path.insert(0, str(VALIDATORS / "common"))
    import refusal  # noqa: E402

    msg = refusal.crashed("probe", "f.py", exc, 7)["errors"][0]["msg"]
    assert expected in msg, msg
    assert chr(10) not in msg and chr(0x2028) not in msg, (
        "the message must be one line: a separator in it writes a second row "
        "at column 0 on the receipt (#1522): " + repr(msg))

    # Constructed here, these three carry no `__traceback__`, so the tail is
    # the exception line alone -- which is the floor, not the product. Raise
    # one so the frame that produced it is in scope, and require it: a class
    # name with no `File "...", line N` is a diagnosis that still does not say
    # where to look.
    try:
        raise type(exc)(*exc.args)
    except Exception as raised:
        with_frame = refusal.crashed(
            "probe", "f.py", raised, 7)["errors"][0]["msg"]
    assert expected in with_frame, with_frame
    assert 'File "' in with_frame and "line " in with_frame, (
        "the tail carries no frame, so the row names the class and not the "
        "site: " + repr(with_frame))
