"""Rendered output must not carry a field that varies between identical runs (#643).

THE INCIDENT
------------
While writing the headline invariant test for #621, an agent found its own new
test **passed under xdist and failed serially** — on the same code, with the
bug fully present.

The `[validators]` block renders its own measured duration. Two tails identical
in every way that mattered compared unequal, so an assertion that two outputs
must be *indistinguishable* passed by accident. Read the direction of that
failure: the test existed to assert "a no-match and a real edit look the same,
and that is the bug". Jitter made them look different, so the test went green
and reported the defect fixed while nothing had been fixed.

That is a green-when-it-should-be-red failure, non-deterministic, depending on
scheduling. It is invisible precisely when it matters.

WHAT IS PINNED HERE
-------------------
Not one test's normalisation — the property underneath it: with
`SUPERTOOL_DETERMINISTIC_TIME=1` (which `tests/conftest.py` sets for the whole
suite), two runs of the same op render byte-identically, so no test comparing
two rendered blocks can ever pass for the wrong reason.

Each duration path is exercised with a *deliberately variable* runtime, so the
RED case is real rather than a coincidence of two fast runs rounding equal.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

import supertool

from _render import stable_render


DETERMINISTIC_ENV = "SUPERTOOL_DETERMINISTIC_TIME"


# ---------------------------------------------------------------------------
# Fixtures: adapters and ops whose runtime genuinely varies run to run
# ---------------------------------------------------------------------------

def _set_validators(cfg: dict) -> None:
    supertool._CONFIG = {"validators": cfg}
    supertool._CONFIG_CHECKED = True


def _slow_by_turn_adapter(tmp_path: Path, *sleeps: float) -> str:
    """A passing validator whose Nth invocation sleeps `sleeps[N]` seconds.

    Real jitter, produced on purpose. Runtimes that round to different tenths
    are exactly the condition that made #621's invariant test pass while the
    bug was present — without them, two sub-millisecond runs would both render
    `0.0s` and the RED case would be a coin flip.

    Per invocation, not per dispatch: a mutating op runs each validator twice,
    once before the write and once after, and it is the *after* run whose
    elapsed time reaches the rendered row.
    """
    counter = tmp_path / "_turn"
    script = tmp_path / "_slow_adapter.py"
    payload = json.dumps({"tool": "fake", "ok": True, "count": 0, "errors": []})
    script.write_text(
        "import json, sys, time, pathlib" + chr(10) +
        "c = pathlib.Path(%r)" % str(counter) + chr(10) +
        "n = int(c.read_text()) if c.exists() else 0" + chr(10) +
        "c.write_text(str(n + 1))" + chr(10) +
        "s = %r" % list(sleeps) + chr(10) +
        "time.sleep(s[n] if n < len(s) else s[-1])" + chr(10) +
        "sys.stdout.write(%r)" % payload + chr(10),
        encoding="utf-8",
    )
    cmd = "{python} " + script.as_posix()
    _set_validators({
        "fake": {"cmd": cmd,
                 "hooks_into": ["edit", "replace", "paste", "append",
                                "replace_lines", "vim"],
                 "match": "*", "cache": False},
    })
    return cmd


def _slow_by_turn_op(tmp_path: Path, first: float, second: float) -> None:
    """A custom op whose two runs take measurably different wall-clock time."""
    counter = tmp_path / "_op_turn"
    script = tmp_path / "_slow_op.py"
    script.write_text(
        "import time, pathlib" + chr(10) +
        "c = pathlib.Path(%r)" % str(counter) + chr(10) +
        "n = int(c.read_text()) if c.exists() else 0" + chr(10) +
        "c.write_text(str(n + 1))" + chr(10) +
        "time.sleep(%r if n == 0 else %r)" % (first, second) + chr(10) +
        "print('constant output')" + chr(10),
        encoding="utf-8",
    )
    supertool._CONFIG = {
        "ops": {"slowop": {"cmd": "{python} " + shlex.quote(script.as_posix())}},
    }
    supertool._CONFIG_CHECKED = True


def _replace_twice(f: Path) -> tuple:
    """Run the identical mutating op twice on the identical starting file."""
    f.write_text("alpha\n", encoding="utf-8")
    first = supertool.dispatch(f"replace:::alpha:::beta:::{f}")
    f.write_text("alpha\n", encoding="utf-8")
    second = supertool.dispatch(f"replace:::alpha:::beta:::{f}")
    return first, second


# ---------------------------------------------------------------------------
# The invariant, per duration path
# ---------------------------------------------------------------------------

def test_validators_block_is_identical_across_identical_runs(tmp_path: Path) -> None:
    """The path that produced the incident: `[validators]` renders its own time."""
    _slow_by_turn_adapter(tmp_path, 0.02, 0.05, 0.02, 0.45)
    first, second = _replace_twice(tmp_path / "x.txt")

    assert "[validators]" in first and "[validators]" in second, "precondition"
    assert first == second, (
        "two identical runs rendered differently — a test comparing two "
        "rendered blocks can pass on this difference alone\n"
        f"--- first ---\n{first}\n--- second ---\n{second}"
    )


def test_custom_op_header_is_identical_across_identical_runs(tmp_path: Path) -> None:
    """`PASS (0.02s)` heads every custom-op dispatch and is measured too."""
    _slow_by_turn_op(tmp_path, 0.02, 0.45)
    first = supertool._resolve_custom_op("slowop", ["slowop"])
    second = supertool._resolve_custom_op("slowop", ["slowop"])

    assert first is not None and second is not None
    assert first.startswith("PASS ("), first
    assert first == second, (
        f"custom-op header varies run to run\n--- first ---\n{first}"
        f"\n--- second ---\n{second}"
    )


def test_the_incident_shape_cannot_pass_on_jitter(tmp_path: Path) -> None:
    """The #621 assertion shape, with jitter forced, on unchanged behaviour.

    A no-match and a real edit must be *distinguishable*. Under jitter this
    assertion passed even when the two tails were otherwise word-for-word
    equal. With durations frozen, it can only pass on a real difference.
    """
    _slow_by_turn_adapter(tmp_path, 0.02, 0.05, 0.02, 0.45)
    f = tmp_path / "x.txt"
    f.write_text("alpha\n", encoding="utf-8")

    missed = supertool.dispatch(f"replace:::NOPE_NOT_THERE:::x:::{f}")
    applied = supertool.dispatch(f"replace:::alpha:::gamma:::{f}")

    def tail(out: str, n: int = 4) -> str:
        return "\n".join(out.rstrip().splitlines()[-n:])

    assert f.read_text(encoding="utf-8") == "gamma\n"
    assert "[validators]" in missed and "[validators]" in applied
    assert tail(missed) != tail(applied)
    difference = set(tail(missed).split()) ^ set(tail(applied).split())
    assert not any(tok.endswith("s") and tok[:-1].replace(".", "").isdigit()
                   for tok in difference), (
        f"the two tails differ by a duration token — {sorted(difference)}"
    )


# ---------------------------------------------------------------------------
# The mode is opt-in — it must not reach a real user's output
# ---------------------------------------------------------------------------

def test_real_durations_render_when_the_switch_is_off(tmp_path: Path,
                                                      monkeypatch) -> None:
    """Without the env var, supertool still reports the time it actually took.

    Freezing durations in normal operation would be a regression of its own:
    the number is there so a human can see which validator is slow.
    """
    monkeypatch.delenv(DETERMINISTIC_ENV, raising=False)
    _slow_by_turn_op(tmp_path, 0.30, 0.30)
    out = supertool._resolve_custom_op("slowop", ["slowop"])
    assert out is not None
    header = out.splitlines()[0]
    seconds = float(header.split("(")[1].split("s)")[0])
    assert seconds >= 0.25, f"a 0.30s op reported {header!r}"


SETTER_TOKENS = ("environ[", "setdefault(", "setenv(", "export ", "putenv(")


def test_switch_is_not_enabled_outside_the_test_suite() -> None:
    """Nothing outside tests/ ever *sets* the mode — shipped code only reads it.

    The guard that keeps a test-only switch test-only. If product code, a
    preset, or a shipped config turned it on, every user would see frozen
    durations and lose the one number that says which validator is slow.
    """
    root = Path(__file__).resolve().parent.parent
    candidates = (
        list(root.glob("*.py")) + list(root.glob("*.json")) +
        list(root.glob("*.toml")) + list((root / "presets").rglob("*.py")) +
        list((root / "validators").rglob("*.py")) +
        list((root / "formatters").rglob("*.py")) +
        list((root / "hooks").rglob("*")) +
        list((root / ".github").rglob("*.yml"))
    )
    scanned, hits = 0, []
    for path in candidates:
        if not path.is_file():
            continue
        scanned += 1
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if DETERMINISTIC_ENV in line and any(t in line for t in SETTER_TOKENS):
                hits.append(f"{path.name}: {line.strip()}")
    assert scanned > 5, "the guard scanned almost nothing — it would pass on anything"
    assert not hits, f"the test-only switch is set outside tests: {hits}"


# ---------------------------------------------------------------------------
# The normaliser must not strip so much that its tests pass on anything
# ---------------------------------------------------------------------------

class TestNormaliserIsNotTooBroad:
    """A normaliser that over-strips is a worse version of the bug it fixes."""

    def test_durations_are_neutralised(self) -> None:
        a = "phplint    : ok          0.1s"
        b = "phplint    : ok          0.2s"
        assert a != b
        assert stable_render(a) == stable_render(b)

    def test_a_changed_verdict_still_differs(self) -> None:
        a = "phplint    : ok          0.1s"
        b = "phplint    : 1 err       0.1s"
        assert stable_render(a) != stable_render(b)

    def test_a_changed_count_still_differs(self) -> None:
        a = "phpstan    : 2 err       1.0s"
        b = "phpstan    : 3 err       1.0s"
        assert stable_render(a) != stable_render(b)

    def test_a_changed_tool_still_differs(self) -> None:
        assert stable_render("phplint : ok 0.1s") != stable_render("phpcs : ok 0.1s")

    def test_line_numbers_survive(self) -> None:
        a = "  L12 syntax  unexpected token"
        b = "  L13 syntax  unexpected token"
        assert stable_render(a) != stable_render(b)
        assert "L12" in stable_render(a)

    def test_the_result_footer_survives(self) -> None:
        """#621's own subject must not be normalised away."""
        a = "[result] 1 op run, 0 writes — nothing changed on disk"
        b = "[result] 1 op run, 1 write"
        assert stable_render(a) != stable_render(b)
        assert stable_render(a) == a

    def test_millisecond_columns_are_neutralised(self) -> None:
        a = "prettier: ok         (12ms) +1 -0"
        b = "prettier: ok         (48ms) +1 -0"
        assert stable_render(a) == stable_render(b)
        assert "+1 -0" in stable_render(a)

    def test_a_changed_diff_metric_still_differs(self) -> None:
        a = "prettier: ok         (12ms) +1 -0"
        b = "prettier: ok         (12ms) +9 -0"
        assert stable_render(a) != stable_render(b)


def test_normalised_comparison_still_catches_a_real_regression(tmp_path: Path) -> None:
    """The bar for the normaliser: a test using it must still go red.

    Same shape as the incident test, but the two runs differ in something that
    matters — one validator passes, the other fails. `stable_render` must not
    hide that.
    """
    _slow_by_turn_adapter(tmp_path, 0.02, 0.05, 0.02, 0.45)
    f = tmp_path / "x.txt"
    f.write_text("alpha\n", encoding="utf-8")
    clean = supertool.dispatch(f"replace:::alpha:::beta:::{f}")

    failing = tmp_path / "_fail_adapter.py"
    failing.write_text(
        "import json, sys" + chr(10) +
        "sys.stdout.write(json.dumps({'tool': 'fake', 'ok': False, 'count': 1,"
        " 'errors': [{'line': 1, 'col': None, 'severity': 'error',"
        " 'code': 'x', 'msg': 'boom'}]}))" + chr(10),
        encoding="utf-8",
    )
    _set_validators({
        "fake": {"cmd": "{python} " + failing.as_posix(),
                 "hooks_into": ["replace"], "match": "*", "cache": False},
    })
    f.write_text("alpha\n", encoding="utf-8")
    broken = supertool.dispatch(f"replace:::alpha:::beta:::{f}")

    assert stable_render(clean) != stable_render(broken), (
        "normalisation hid a genuine change in the rendered verdict"
    )
