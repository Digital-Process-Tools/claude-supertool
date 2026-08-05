"""One place decides how long a test waits on a validator adapter (#702, #658).

What can be pinned here and what cannot, stated plainly, because the failure
being fixed is one this machine cannot reproduce:

**Cannot be pinned:** that any particular budget is large enough on a loaded
Windows runner. Nothing runnable here can make CI slow, and a test that
asserts a wall-clock margin holds is the bug, not the fix — it would pass on
every machine that does not have the problem.

**Pinned instead:** the policy. That budgets come from one function rather than
from a literal at each call site; that the function's output always exceeds the
adapter's own internal budget, which is what makes a fired budget mean "hang"
rather than "busy"; that the Windows multiplier applies; that the env override
works and a malformed one does not take the suite down (#654). Those are
properties of the code, so they hold identically on a fast laptop and a
crawling runner.

And one property that is not about timeouts at all but was found by sweeping
for them: an adapter that blows its own internal budget has to survive it.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import _adapter_budget as budget  # noqa: E402
import _adapter_verdict as verdicts  # noqa: E402
from _adapter_verdict import assert_declined  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
VALIDATORS = REPO / "validators"
TESTS = REPO / "tests"


# ---------------------------------------------------------------------------
# The budget function itself
# ---------------------------------------------------------------------------

def test_budget_exceeds_the_adapters_own_internal_timeout() -> None:
    """The property that makes a fired budget mean "hang" and not "busy".

    Below the adapter's own budget, the adapter always declines first and the
    outer timeout can only ever fire because the machine was slow. Every one
    of the three reported incidents was on the wrong side of this line.
    """
    adapters = sorted(VALIDATORS.glob("*/*.py"))
    assert adapters, "no adapters found — the sweep would pass vacuously"
    for adapter in adapters:
        inner = budget.inner_budget(adapter)
        assert budget.adapter_budget(adapter) > inner, (
            f"{adapter.relative_to(REPO)} grants itself {inner}s internally but "
            f"the test budget over it is {budget.adapter_budget(adapter)}s — "
            "the adapter would decline before the test's timeout could fire"
        )


def test_inner_budget_is_read_from_the_adapter_not_tabulated(tmp_path: Path) -> None:
    """Raising an adapter's internal budget raises the test budget over it.

    The point of reading rather than tabulating: no second number to keep in
    step, so the next person to widen an adapter cannot leave a test behind.
    """
    fake = tmp_path / "fake-check.py"
    fake.write_text("subprocess.run(cmd, timeout=17)\nsubprocess.run(x, timeout=999)\n")
    assert budget.inner_budget(fake) == 999
    assert budget.adapter_budget(fake) > 999


def test_a_hoisted_timeout_constant_is_read_the_same_as_a_literal(tmp_path: Path) -> None:
    """Hoisting `timeout=30` into `TIMEOUT_S = 30` must not shrink the budget.

    An adapter that wants to name its budget in its own decline message has to
    hoist the literal to do it — which the three adapters fixed alongside this
    all did. Reading only the call-site spelling would quietly drop every one
    of them back to the default: a good change to an adapter tightening the
    test budget over it, which is this whole class of bug running backwards.
    """
    fake = tmp_path / "hoisted.py"
    fake.write_text("TIMEOUT_S = 77\nsubprocess.run(cmd, timeout=TIMEOUT_S)\n")
    assert budget.inner_budget(fake) == 77
    assert budget.adapter_budget(fake) > 77


def test_an_adapter_without_a_declared_timeout_gets_the_documented_default(tmp_path: Path) -> None:
    fake = tmp_path / "bare.py"
    fake.write_text("print('hi')\n")
    assert budget.inner_budget(fake) == budget.DEFAULT_INNER_S


def test_an_unreadable_adapter_biases_generous_rather_than_tight(tmp_path: Path) -> None:
    """The failure mode of guessing has to be a budget that is too large.

    A too-tight guess reintroduces exactly the defect this module removes.
    """
    assert budget.inner_budget(tmp_path / "does-not-exist.py") == budget.DEFAULT_INNER_S


def test_windows_legs_get_a_larger_budget_than_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform scaling applies, and it applies to the whole budget."""
    fake_inner = 30
    monkeypatch.setattr(budget.sys, "platform", "linux")
    posix = budget.adapter_budget("ignored", inner=fake_inner)
    monkeypatch.setattr(budget.sys, "platform", "win32")
    windows = budget.adapter_budget("ignored", inner=fake_inner)
    assert budget.platform_factor() == budget.WINDOWS_FACTOR
    assert windows == posix * budget.WINDOWS_FACTOR
    assert windows > posix


def test_env_override_wins_outright(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(budget.ENV_OVERRIDE, "600")
    assert budget.adapter_budget("ignored", inner=30) == 600


@pytest.mark.parametrize("bad", ["", "soon", "0", "-5", "12.5"])
def test_a_malformed_override_is_ignored_rather_than_fatal(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """A bad knob must not take collection of the whole suite down (#654)."""
    monkeypatch.setenv(budget.ENV_OVERRIDE, bad)
    monkeypatch.setattr(budget.sys, "platform", "linux")
    assert budget.adapter_budget("ignored", inner=30) == 30 + budget.SPAWN_HEADROOM_S


# ---------------------------------------------------------------------------
# No fourth site writes its own number
# ---------------------------------------------------------------------------

def _adapter_constants(tree: ast.Module) -> set[str]:
    """Module-level names bound to an adapter script under ``validators/``.

    Resolved to a fixed point rather than in one pass, because the constant is
    usually two hops from the literal::

        VALIDATORS  = Path(__file__).parent.parent / "validators"
        GOFMT_CHECK = VALIDATORS / "gofmt-check" / "gofmt-check.py"

    A single-pass version of this function missed exactly that shape — which
    is `tests/test_validators_tier2.py`, the file #702 was filed about. A
    guard that does not see the incident it was written for is worse than no
    guard, so this is the second attempt and the first one is why.
    """
    rooted: set[str] = set()
    scripts: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not targets:
                continue
            literals = [
                n.value for n in ast.walk(node.value)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            ]
            refs = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            if "validators" not in literals and not (refs & rooted):
                continue
            for name in targets:
                if name not in rooted:
                    rooted.add(name)
                    changed = True
                if any(s.endswith(".py") for s in literals):
                    scripts.add(name)
    return scripts


def _literal_timeout_spawns(path: Path) -> list[tuple[int, str]]:
    """(lineno, adapter name) for every adapter spawn with a hardcoded budget."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    adapters = _adapter_constants(tree)
    if not adapters:
        return []
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in {"run", "Popen"}):
            continue
        literal = next(
            (kw for kw in node.keywords
             if kw.arg == "timeout" and isinstance(kw.value, ast.Constant)),
            None,
        )
        if literal is None:
            continue
        referenced = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} & adapters
        for name in sorted(referenced):
            hits.append((node.lineno, name))
    return hits


def test_no_test_spawns_a_validator_adapter_on_a_hardcoded_budget() -> None:
    """The guard that stops a fourth incident from being written.

    #702, #658 and #650 were each one literal, chosen against the machine that
    wrote it. A literal is not wrong because of its value — it is wrong because
    nothing relates it to what the adapter is allowed to take. Route it through
    ``_adapter_budget.adapter_budget`` and the relationship is the definition.
    """
    offenders = []
    for path in sorted(TESTS.glob("test_*.py")):
        for lineno, name in _literal_timeout_spawns(path):
            offenders.append(f"{path.relative_to(REPO)}:{lineno} spawns {name}")
    assert not offenders, (
        "hardcoded subprocess budget on a validator adapter spawn — use "
        "_adapter_budget.adapter_budget(ADAPTER):\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Found by the sweep: an adapter must survive its own budget expiring
# ---------------------------------------------------------------------------

def _spawning_adapters() -> list[Path]:
    out = []
    for adapter in sorted(VALIDATORS.glob("*/*.py")):
        src = adapter.read_text(encoding="utf-8", errors="replace")
        if "subprocess.run(" in src and "timeout=" in src:
            out.append(adapter)
    return out


def test_every_adapter_that_grants_itself_a_budget_survives_blowing_it() -> None:
    """A validator that hangs must decline, not raise.

    Not a timeout-value question — a three-states question, the same one #650
    answered for git. An adapter whose `subprocess.run` raises `TimeoutExpired`
    with nothing to catch it exits on a traceback and prints **nothing** to
    stdout, and every caller `json.loads()` that. The op dies on a
    JSONDecodeError naming neither the validator nor the timeout, which is a
    fact about the machine rendered as a crash in the tool.
    """
    unguarded = [
        a.relative_to(REPO).as_posix() for a in _spawning_adapters()
        if "TimeoutExpired" not in a.read_text(encoding="utf-8", errors="replace")
        and "except Exception" not in a.read_text(encoding="utf-8", errors="replace")
    ]
    assert not unguarded, (
        "adapter spawns a tool on a timeout with no handler for it expiring; "
        "a blown budget leaves stdout empty and the caller crashes on "
        f"json.loads: {unguarded}"
    )


@pytest.mark.parametrize("adapter_dir,tool", [
    ("hadolint", "hadolint"),
    ("markdownlint", "markdownlint"),
    ("tsc-check", "tsc"),
])
def test_a_blown_budget_is_a_stated_decline_with_valid_json(
    adapter_dir: str, tool: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the adapter's own timeout and read what reaches stdout.

    In-process rather than through a PATH shim so it costs milliseconds
    instead of the adapter's real budget, and so it runs on Windows — where
    all three reported incidents happened and where a POSIX `/bin/sh` shim
    (the #650 technique) is skipped.
    """
    import importlib.util

    path = VALIDATORS / adapter_dir / f"{adapter_dir}.py"
    spec = importlib.util.spec_from_file_location(f"{adapter_dir}_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    target = tmp_path / "subject.txt"
    target.write_text("anything\n")

    def _always_times_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=[tool], timeout=kwargs.get("timeout", 30))

    monkeypatch.setattr(mod.subprocess, "run", _always_times_out)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: f"/usr/bin/{tool}")
    monkeypatch.setattr(mod.sys, "argv", [str(path), str(target)])

    emitted: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: emitted.append(" ".join(map(str, a))))

    mod.main()

    payloads = [json.loads(line) for line in emitted if line.strip().startswith("{")]
    assert payloads, f"{adapter_dir} emitted no JSON when its budget expired"
    data = payloads[-1]
    assert_declined(data, context="an adapter whose own internal budget expired")
    assert data["errors"][0]["code"] == "adapter"
    assert "timeout" in data["errors"][0]["msg"].lower(), (
        "the decline has to name what happened; a caller reading this is "
        "deciding whether the file is bad or the machine was"
    )


# ---------------------------------------------------------------------------
# End to end: a real adapter spawn under the shared budget
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("php"), reason="php not installed")
def test_a_real_adapter_spawn_answers_well_inside_the_shared_budget(tmp_path: Path) -> None:
    """Sanity, not a benchmark: it must answer, and the budget must be the shared one."""
    adapter = VALIDATORS / "phplint" / "phplint.py"
    f = tmp_path / "ok.php"
    f.write_text("<?php\n$x = 1;\n")
    r = subprocess.run(
        [sys.executable, str(adapter), str(f)],
        capture_output=True, text=True, timeout=budget.adapter_budget(adapter),
    )
    # This fired once on windows-latest/3.10 as `assert False is True` and named
    # nothing (#725). It is not a blown budget — that raises TimeoutExpired. The
    # adapter answered, and it answered no, and the reason was in a payload the
    # assertion threw away. Now it is in the message.
    #
    # And it fired again on windows-latest/3.9 (#794) as `[adapter] timeout`
    # after 30000ms — the adapter's *own* wall, not this spawn's budget, which
    # was 120s and never came near firing. That is the adapter honouring its
    # contract on a runner too loaded to answer, so there is no verdict here to
    # assert against. See the section below for why that declines rather than
    # fails, and for what still fails.
    verdicts.assert_adapter_ok_or_skip_if_stalled(
        r,
        adapter=adapter.name,
        inner_s=budget.inner_budget(adapter),
        context="a two-line PHP file with nothing wrong with it",
    )


# ---------------------------------------------------------------------------
# A stalled adapter has not found a defect; it has failed to measure (#794)
# ---------------------------------------------------------------------------
#
# #794 reddened a PR about a GraphQL linked-PR lookup. The three shapes proposed
# for it all move the *outer* budget — make it relative, widen it, isolate the
# leg — and not one of them would have changed that run. The outer budget was
# `(30 + 10) * 3 = 120s` and `subprocess.run` never raised: phplint answered, on
# time, with `[adapter] timeout` after 30000ms, which is its own internal 30s
# wall firing exactly as `validators/phplint/phplint.py` says it should.
#
# So the defect is not a number. It is that this end-to-end test asks "is a
# clean PHP file reported clean" and accepts, as an answer to that question, a
# payload whose whole content is "I never got to look". `validators/SCHEMA.md`
# reserves `code: "adapter"` for precisely that — "no verdict was obtained" —
# and `docs/validators.md` §"Declining instead of guessing" says an unmeasured
# subject is a third state, not a failing one.
#
# **What is deliberately NOT widened.** SCHEMA.md is explicit that an adapter
# must never route a tool fault to `skipped`, and `_adapter_budget` is explicit
# that a blown *outer* budget must stay loud because it means an adapter
# ignored its own timeout — a real hang. Neither moves. This is narrower than
# either: a test that spawned an adapter, got a well-formed decline back, and
# has nothing to assert on. Every other route to `ok=False` still fails.

PHPLINT_INNER_S = 30


def _spawn_result(payload: object) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["phplint.py", "subject.php"], returncode=0,
        stdout=json.dumps(payload), stderr="",
    )


def _stall_payload(**over: object) -> dict:
    """The payload that reddened someone else's PR in #794, to the byte."""
    base: dict = {
        "tool": "phplint", "file": "ok.php", "ok": False, "count": 1,
        "errors": [{"line": None, "col": None, "severity": "error",
                    "code": "adapter", "msg": "timeout"}],
        "duration_ms": PHPLINT_INNER_S * 1000,
    }
    base.update(over)
    return base


def test_an_adapter_that_spent_its_whole_budget_declines_rather_than_fails() -> None:
    """The #794 payload, and the one behaviour change in this file."""
    with pytest.raises(pytest.skip.Exception) as caught:
        verdicts.assert_adapter_ok_or_skip_if_stalled(
            _spawn_result(_stall_payload()),
            adapter="phplint.py", inner_s=PHPLINT_INNER_S,
            context="a two-line PHP file with nothing wrong with it",
        )
    assert "30000" in str(caught.value), (
        "the measured duration is the entire signal a skip has left to carry — "
        "a 30s spawn is still worth knowing about, and a skip that drops the "
        "number is a mute button"
    )
    assert "phplint" in str(caught.value)


def test_a_real_finding_about_the_file_still_fails_loudly() -> None:
    """The half that makes this a fix rather than a mute button."""
    broken = _stall_payload(errors=[{
        "line": 2, "col": None, "severity": "error",
        "code": "parse", "msg": "syntax error, unexpected end of file",
    }], duration_ms=40)
    with pytest.raises(AssertionError) as caught:
        verdicts.assert_adapter_ok_or_skip_if_stalled(
            _spawn_result(broken), adapter="phplint.py", inner_s=PHPLINT_INNER_S,
        )
    assert "syntax error" in str(caught.value)


def test_an_instant_adapter_fault_still_fails_loudly() -> None:
    """`code: "adapter"` alone does not buy a skip.

    SCHEMA.md: an `adapter` error "stays a real error ... because the process
    ran and something is broken that someone has to fix". A php that vanished,
    or an argv the adapter could not read, is that. Only a wall the adapter
    actually spent its whole budget hitting is a statement about the machine,
    so the duration is part of the predicate and not decoration.
    """
    for msg, dur in [("php binary not found", 4), ("no file arg", 0)]:
        fault = _stall_payload(errors=[{
            "line": None, "col": None, "severity": "error",
            "code": "adapter", "msg": msg,
        }], duration_ms=dur)
        with pytest.raises(AssertionError) as caught:
            verdicts.assert_adapter_ok_or_skip_if_stalled(
                _spawn_result(fault), adapter="phplint.py", inner_s=PHPLINT_INNER_S,
            )
        assert msg in str(caught.value)


def test_a_timeout_claimed_without_spending_the_budget_still_fails_loudly() -> None:
    """An adapter that reports `timeout` in 12ms did not time out.

    That is the adapter's error routing being wrong — a defect in the thing
    this suite exists to test — and it is exactly what a message-only predicate
    would swallow.
    """
    with pytest.raises(AssertionError):
        verdicts.assert_adapter_ok_or_skip_if_stalled(
            _spawn_result(_stall_payload(duration_ms=12)),
            adapter="phplint.py", inner_s=PHPLINT_INNER_S,
        )


def test_a_stall_reported_beside_a_real_finding_still_fails_loudly() -> None:
    """One error being unmeasurable does not make the other one disappear."""
    mixed = _stall_payload(count=2, errors=[
        {"line": None, "col": None, "severity": "error",
         "code": "adapter", "msg": "timeout"},
        {"line": 2, "col": None, "severity": "error",
         "code": "parse", "msg": "unexpected end of file"},
    ])
    with pytest.raises(AssertionError) as caught:
        verdicts.assert_adapter_ok_or_skip_if_stalled(
            _spawn_result(mixed), adapter="phplint.py", inner_s=PHPLINT_INNER_S,
        )
    assert "unexpected end of file" in str(caught.value)


def test_a_clean_verdict_is_still_a_pass() -> None:
    """The wrapper has to be able to say yes, or it proves nothing."""
    clean = {"tool": "phplint", "file": "ok.php", "ok": True, "count": 0,
             "errors": [], "duration_ms": 41}
    got = verdicts.assert_adapter_ok_or_skip_if_stalled(
        _spawn_result(clean), adapter="phplint.py", inner_s=PHPLINT_INNER_S,
    )
    assert got == clean, "the wrapper returns the parsed verdict unchanged"


def test_a_crashed_adapter_is_not_a_stall() -> None:
    """No stdout at all still fails, naming the exit code and stderr (#725)."""
    crashed = subprocess.CompletedProcess(
        args=["phplint.py"], returncode=1, stdout="", stderr="Traceback ...")
    with pytest.raises(AssertionError) as caught:
        verdicts.assert_adapter_ok_or_skip_if_stalled(
            crashed, adapter="phplint.py", inner_s=PHPLINT_INNER_S,
        )
    assert "no output on stdout" in str(caught.value)


def test_the_payload_a_real_adapter_emits_on_a_stall_is_the_one_that_declines() -> None:
    """Close the seam between the two halves, so they cannot drift apart.

    Everything above works on a payload this file wrote. The predicate only
    protects anyone if the payload `validators/phplint/phplint.py` *actually*
    emits when its own budget expires is one the predicate recognises — and
    that message is a free-text string in another file. Reword it to "budget
    expired" and every test here still passes while #794 quietly returns.

    Driven in-process, the same technique as the parametrized decline test
    above, so it costs milliseconds rather than the adapter's real 30s wall.
    """
    import importlib.util

    path = VALIDATORS / "phplint" / "phplint.py"
    spec = importlib.util.spec_from_file_location("phplint_stalling", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def _always_times_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["php"], timeout=kwargs.get("timeout", 30))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mod.subprocess, "run", _always_times_out)
        mp.setattr(mod.sys, "argv", [str(path), "subject.php"])
        emitted: list[str] = []
        mp.setattr("builtins.print", lambda *a, **k: emitted.append(" ".join(map(str, a))))
        mod.main()

    payload = json.loads(emitted[-1])
    reason = verdicts.stalled_at_its_own_wall(payload, inner_s=budget.inner_budget(path))
    assert reason is not None, (
        "phplint's own stall payload no longer matches the predicate that "
        "declines on it — the two drifted, and the next loaded Windows runner "
        f"reddens a stranger's PR again (#794). Payload: {payload}"
    )
    assert str(budget.inner_budget(path) * 1000) in reason


def test_the_stall_predicate_reads_the_adapters_real_wall_not_a_tabulated_one() -> None:
    """The duration threshold comes from the adapter's source, same as #702.

    A second number written down here would drift from `phplint.py` the moment
    someone widened it, and the drift would land as a red on a stranger's PR —
    which is the entire complaint of #794.
    """
    assert budget.inner_budget(VALIDATORS / "phplint" / "phplint.py") == PHPLINT_INNER_S

