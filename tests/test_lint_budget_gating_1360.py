"""#1360 -- a blown lint budget is an environment limit, and the suite must
count it rather than render it as a product verdict.

`test_xml_lint` broke `pytest (windows-latest, 3.10)` on PR #1355: the product
declined correctly (`POST-EDIT LINT TIMED OUT -- xmllint (30s)`, naming the
knob), and the test converted that decline into a red leg. #553/#558 is the
same failure at a 5s budget; the budget is now 30s and a loaded runner still
blew it. Raising it a third time buys an interval of quiet, not a fix.

Three states, not two. A site that asserts a lint **verdict** now:

  * asserts it, when the checker reached one -- unchanged, and the only green;
  * **skips**, carrying ``_lint_budget.TOKEN``, when the checker timed out, so
    `N skipped` in the leg resolves to `N did not reach a lint verdict here`;
  * **fails**, when the receipt declined for any reason other than the budget,
    or timed out against a budget that is not the one configured. A
    `POST-EDIT LINT DECLINED -- could not start the checker` is the #997 class
    -- a Windows-only spawn failure -- and swallowing it into a skip would
    trade the loud bug for the quiet one.

The count is a subset and says so (#1274): only the sites in ``_population()``
produce a token skip, and only the timeout decline does.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

import _lint_budget
import supertool

TESTS = Path(__file__).resolve().parent

#: Verdict shapes. A receipt carrying either of these says a checker ran to
#: completion; a timeout decline carries neither, which is why a site asserting
#: one of them positively is exposed to the budget and a site asserting it
#: negatively is not.
VERDICT_ASSERTIONS = ("--- lint: ", "POST-EDIT LINT FAILED")

#: Extensions whose checker is a subprocess, so a slow runner can blow the
#: budget. `.json` is parsed in-process and has no budget to blow.
SUBPROCESS_LINTED = (".php", ".xml", ".py")

#: A bare file name, as a test writes one under `tmp_path`. Excludes the module
#: paths and op strings that also end in `.py`.
_BARE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

GATE = "require_lint_verdict"


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name + "_1360", str(TESTS / (name + ".py")))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _file_suffixes(fn) -> set:
    """Extensions of the file names this test writes."""
    out = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Constant):
            continue
        if not isinstance(node.value, str):
            continue
        name = node.value
        if not _BARE_NAME.match(name):
            continue
        for ext in SUBPROCESS_LINTED:
            if name.endswith(ext) and len(name) > len(ext):
                out.add(ext)
    return out


def _first_verdict_assertion(fn):
    """Line of the earliest positive ``"<verdict>" in out``, or ``None``.

    Positive only: a site asserting a verdict is *absent* is satisfied by a
    timeout decline and is not exposed to the budget.
    """
    lines = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Compare):
            continue
        if not isinstance(node.left, ast.Constant):
            continue
        if not isinstance(node.left.value, str):
            continue
        if not any(node.left.value.startswith(v) for v in VERDICT_ASSERTIONS):
            continue
        if any(isinstance(op, ast.In) for op in node.ops):
            lines.append(node.lineno)
    return min(lines) if lines else None


def _gated_before(fn, lineno) -> bool:
    """Is there a real CALL to the gate in ``fn``, above ``lineno``?

    Both halves are load-bearing, and a bare name search fails both -- the
    lesson `_called_before` in tests/test_symlink_gating_register_1232.py was
    written for. A mention that is not a call (a `monkeypatch.setattr` target,
    a reference in a message) gates nothing, and a call placed BELOW the
    assertion it is meant to guard gates nothing either: the assertion runs
    first and reddens the leg exactly as #1360 did.
    """
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == GATE and node.lineno < lineno:
            return True
    return False


def _sites_in_source(source: str):
    """``[(test name, gated?), ...]`` for one module's source text."""
    sites = []
    for fn in ast.parse(source).body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not fn.name.startswith("test_"):
            continue
        lineno = _first_verdict_assertion(fn)
        if lineno is None:
            continue
        if not _file_suffixes(fn):
            continue
        sites.append((fn.name, _gated_before(fn, lineno)))
    return sites


def _population():
    """``{file name: [(test name, gated?), ...]}`` over the whole suite.

    Derived from the AST rather than listed, so a new site asserting a lint
    verdict joins the population without anyone remembering to add it here --
    the #1232 lesson, where a hand-listed register is a register of what
    somebody happened to know about.
    """
    found = {}
    for path in sorted(TESTS.glob("test_*.py")):
        sites = _sites_in_source(path.read_text(encoding="utf-8"))
        if sites:
            found[path.name] = sites
    return found


# ---------------------------------------------------------------------------
# The instance #1360 was filed for, and one more from the same class.
# ---------------------------------------------------------------------------

def _force_py_compile_to_time_out(monkeypatch) -> None:
    real_run = subprocess.run

    def timing_out(*a, **k):
        cmd = a[0] if a else k.get("args")
        if cmd and "py_compile" in list(cmd):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=k.get("timeout", 5))
        return real_run(*a, **k)

    monkeypatch.setattr(subprocess, "run", timing_out)


def test_test_xml_lint_skips_rather_than_reddens_when_the_budget_is_blown(
    tmp_path, monkeypatch
) -> None:
    """The exact PR #1355 failure, forced instead of waited for."""
    mod = _load("test_vim_receipt_diff_and_lint")
    mod._force_xmllint_to_time_out(monkeypatch)
    with pytest.raises(pytest.skip.Exception) as e:
        mod.test_xml_lint(tmp_path)
    assert _lint_budget.TOKEN in str(e.value), str(e.value)
    assert "xmllint" in str(e.value), str(e.value)


def test_the_python_lint_sites_are_in_the_same_class_not_just_xml(
    tmp_path, monkeypatch
) -> None:
    """py_compile spawns a fresh interpreter -- the more expensive of the two.

    Fixing xmllint alone would leave five sites reddening on the identical
    mechanism, which is this repo's own "the named pattern shadows the class"
    trap.
    """
    mod = _load("test_vim_receipt_diff_and_lint")
    _force_py_compile_to_time_out(monkeypatch)
    with pytest.raises(pytest.skip.Exception) as e:
        mod.test_py_lint_success(tmp_path)
    assert _lint_budget.TOKEN in str(e.value), str(e.value)


# ---------------------------------------------------------------------------
# The skip must not become the silent pass the issue names as the thing to
# avoid.
# ---------------------------------------------------------------------------

def test_a_decline_that_is_not_a_timeout_is_not_skippable() -> None:
    """`could not start the checker` is #997's Windows spawn failure.

    It has to stay red: a skip here would report an absence of coverage where
    the tool actually found a bug.
    """
    out = supertool._lint_declined(
        "xmllint", "could not start the checker (FileNotFoundError: [WinError 2])")
    _lint_budget.require_lint_verdict(out)  # returns, so the caller still asserts


def test_a_timeout_naming_a_budget_nobody_configured_fails_instead_of_skipping(
    monkeypatch
) -> None:
    """The knob mis-plumbed is a product bug wearing the flake's clothes.

    Distinguishable without guessing: the receipt states the budget it used,
    and `_lint_timeout()` states the budget that was configured.
    """
    monkeypatch.setenv("SUPERTOOL_LINT_TIMEOUT", "30")
    forged = (
        supertool._LINT_TIMEOUT_PREFIX + " -- xmllint (2s) ---\n"
        "lint did not run to completion; the file was NOT checked.\n"
    )
    with pytest.raises(pytest.fail.Exception) as e:
        _lint_budget.require_lint_verdict(forged)
    assert "30" in str(e.value) and "2" in str(e.value), str(e.value)


def test_the_gate_keys_on_the_products_own_prefix_not_a_copied_literal() -> None:
    """A predicate that sniffs prose is one reword away from swallowing a
    finding. The product declares the prefix; the suite imports it."""
    assert supertool._LINT_TIMEOUT_PREFIX in supertool._LINT_DECLINE_PREFIXES
    assert _lint_budget.PREFIX is supertool._LINT_TIMEOUT_PREFIX


def test_a_verdict_receipt_is_never_skipped() -> None:
    """The whole risk of a tolerance: a test that passes when nothing ran."""
    _lint_budget.require_lint_verdict("--- lint: xmllint ---\nok\n")
    _lint_budget.require_lint_verdict(
        "--- POST-EDIT LINT FAILED -- xmllint ---\nboom\n")


# ---------------------------------------------------------------------------
# The register: every exposed site is gated, derived rather than listed.
# ---------------------------------------------------------------------------

def test_every_site_that_demands_a_lint_verdict_is_budget_gated() -> None:
    ungated = dict(
        (path, [name for name, gated in sites if not gated])
        for path, sites in _population().items())
    ungated = dict((k, v) for k, v in ungated.items() if v)
    assert not ungated, (
        "these tests assert a lint verdict for a subprocess-backed checker "
        "without calling `_lint_budget.require_lint_verdict(out)` first, so a "
        "runner that blows the budget reddens them instead of counting them "
        "(#1360): " + repr(ungated))


#: A gate below the assertion it guards, and a gate that is only mentioned.
#: Both are certified by a bare name search, and neither gates anything.
_UNGATED_SHAPES = {
    "below the assertion": """
def test_x(tmp_path):
    f = tmp_path / "x.py"
    out = supertool.op_vim(str(f), "G")
    assert "POST-EDIT LINT FAILED" in out
    _lint_budget.require_lint_verdict(out)
""",
    "mentioned, never called": """
def test_x(tmp_path):
    f = tmp_path / "x.py"
    out = supertool.op_vim(str(f), "G")
    handler = _lint_budget.require_lint_verdict
    assert "POST-EDIT LINT FAILED" in out
""",
}

_GATED_SHAPE = """
def test_x(tmp_path):
    f = tmp_path / "x.py"
    out = supertool.op_vim(str(f), "G")
    _lint_budget.require_lint_verdict(out)
    assert "POST-EDIT LINT FAILED" in out
"""


def test_the_register_rejects_a_gate_that_cannot_gate() -> None:
    """Order and callness are both load-bearing (#1232's `_called_before`).

    A gate below the assertion it guards is the #1360 failure verbatim -- the
    assertion runs first -- and a name that is never called gates nothing at
    all. A classifier blind to either certifies the site, and the register then
    reports a coverage it does not have.
    """
    for label, source in _UNGATED_SHAPES.items():
        sites = _sites_in_source(source)
        assert sites, label + ": the classifier stopped seeing the site at all"
        assert sites == [("test_x", False)], (
            label + ": certified as gated, but the verdict assertion still runs "
            "first / the gate is never called: " + repr(sites))


def test_the_register_accepts_the_gate_placed_correctly() -> None:
    """The counterpart, so the test above cannot pass by rejecting everything."""
    assert _sites_in_source(_GATED_SHAPE) == [("test_x", True)]


def test_the_register_is_not_vacuously_empty() -> None:
    """A classifier that stopped recognising the shape would pass the test
    above by finding nothing at all -- the #1274 failure mode."""
    sites = _population()
    total = sum(len(v) for v in sites.values())
    assert total >= 7, (
        "the population collapsed; the classifier no longer recognises a "
        "lint-verdict assertion: " + repr(sites))


# ---------------------------------------------------------------------------
# The count, and its population (#1143's shape, #1274's correction).
# ---------------------------------------------------------------------------

class _Report:
    def __init__(self, reason):
        self.longrepr = ("f.py", 1, reason)


class _Reporter:
    def __init__(self, reasons):
        self.stats = {"skipped": [_Report(r) for r in reasons]}
        self.lines = []

    def write_line(self, line):
        self.lines.append(line)


REASONS = [
    _lint_budget.TOKEN + ": xmllint timed out",
    "this platform has no O_NOFOLLOW, so the guard cannot be enforced",
    "posix only",
]


def _summary(reasons):
    conftest = _load("conftest")
    reporter = _Reporter(reasons)
    conftest.pytest_terminal_summary(reporter, 0, None)
    return chr(10).join(reporter.lines)


def test_the_line_states_the_denominator_it_counted() -> None:
    """`1` on its own reads as a total. `1 of 3` cannot (#1274)."""
    out = _summary(REASONS)
    assert _lint_budget.TOKEN in out, out
    assert "1 of 3 skipped" in out, out


def test_the_line_is_printed_when_the_count_is_zero() -> None:
    """Silence is indistinguishable from not having looked."""
    out = _summary(["posix only"])
    assert _lint_budget.TOKEN in out, out
    assert "0 of 1 skipped" in out, out


def test_the_line_names_what_is_not_in_the_number() -> None:
    out = _summary(REASONS)
    assert "tests/test_lint_budget_gating_1360.py" in out, out
    assert "DECLINED" in out, out
