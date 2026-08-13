"""#1548 — the stock refusal patterns and `outside_roots()` are not a pair.

The issue reads `refusal.py`'s `"--paths allowlist"` against what
`outside_roots()` emits (`"path outside <VAR> allowlist"`), finds they do not
match, and concludes one of the two must be wrong. Neither is. They are
different mechanisms that never meet, and the repo's own history says so:

- `REFUSAL_PATTERNS` arrived in d822e93 (#411/#406) to classify **the analyser's
  own message** — `is_refusal()` is only ever handed text the tool produced
  (`phpstan.py:49,87,188`, `phpmd-mcp.py:159`, `phpstan-mcp.py:187`).
- `outside_roots()` arrived later, in #412, as a **local short-circuit** that the
  same commit message explicitly split out as "a performance feature, not part
  of this bug's blast radius". Its return value is handed straight to
  `skipped()` (`phpstan-mcp.py:202-205`) and is never shown to `is_refusal()`.

So `"--paths allowlist"` is not dead. Measured: the daemon's real emission is
`analyse: path is outside the configured --paths allowlist.` (CHANGELOG.md:5034,
re-derived below), and the pattern is a substring of it. And `outside_roots()`
returning a string that `is_refusal()` rejects is not drift: its wording names
the env var rather than `--paths` on purpose, because a wrong skip is caused by
that configuration and pointing at a tool that never saw the file sends the
reader to the wrong file to fix it (CHANGELOG.md:5038).

What this file pins is the thing the issue was right to want: **a pattern that
matches nothing is indistinguishable from a refusal that never happens.** So
every stock pattern is either paired here with an emission somebody measured, or
named as a deliberate unmeasured spelling variant. Adding a pattern without
doing one or the other fails.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
REFUSAL_PY = REPO / "validators" / "common" / "refusal.py"
PHPSTAN_MCP_PY = REPO / "validators" / "phpstan-mcp" / "phpstan-mcp.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ref = _load(REFUSAL_PY, "refusal_1548")

#: Pattern -> a message a tool actually emitted, with where it was measured.
MEASURED_EMISSIONS = {
    # mcp-phpstan-warm, PhpstanRunner::assertPathAllowed() (CHANGELOG.md:5034)
    "--paths allowlist":
        "analyse: path is outside the configured --paths allowlist.",
    "outside the configured":
        "analyse: path is outside the configured --paths allowlist.",
    # phpstan 2.1.55, `analyse --no-progress --error-format=json <empty dir>`,
    # measured 2026-08-13: `[ERROR] No files found to analyse.`, rc 1.
    "no files found to analyse":
        "[ERROR] No files found to analyse.",
}

#: Patterns kept as spelling variants of a measured one, with no emission of
#: their own on record. Listing them is the point: an entry here is a claim that
#: nobody has seen it fire, not an assertion that it works.
UNMEASURED_VARIANTS = {
    "no files found to analyze",   # US spelling of the phpstan message above
    "no files found to check",     # no emitter established since #411
}


def test_every_stock_pattern_is_accounted_for() -> None:
    """A pattern with neither an emission nor an explicit variant note is dead."""
    accounted = set(MEASURED_EMISSIONS) | UNMEASURED_VARIANTS
    stock = set(ref.REFUSAL_PATTERNS)
    assert stock - accounted == set(), (
        "new refusal pattern with no measured emission and no variant note: "
        f"{sorted(stock - accounted)}")
    assert accounted - stock == set(), (
        f"this file names patterns the module no longer has: {sorted(accounted - stock)}")


@pytest.mark.parametrize("pattern,emission", sorted(MEASURED_EMISSIONS.items()))
def test_each_measured_emission_is_classified_as_a_refusal(
        pattern: str, emission: str) -> None:
    assert pattern in emission.lower(), (pattern, emission)
    assert ref.is_refusal(emission), emission


def test_outside_roots_is_not_an_is_refusal_input(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The issue's premise, pinned as the non-defect it is.

    `outside_roots()` answers "did *this adapter* rule the file out", and its
    string goes to `skipped()` directly. It is deliberately worded to name the
    env var, so it does not — and need not — read as one of the analyser's own
    refusal phrases. Asserting the negative keeps the next reader from
    re-deriving it as drift.
    """
    monkeypatch.setenv("SOME_PATHS", str(tmp_path / "src"))
    reason = ref.outside_roots(str(tmp_path / "tests" / "T.php"), "SOME_PATHS")
    assert reason == "path outside SOME_PATHS allowlist", reason
    assert ref.is_refusal(reason) is False, (
        "outside_roots() now reads as a tool refusal — if that is intended, the "
        "two mechanisms have been merged and this test should say so")


def test_the_short_circuit_still_reaches_skipped_without_is_refusal(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    """End to end: out-of-scope is the third state, and no daemon is contacted."""
    mod = _load(PHPSTAN_MCP_PY, "phpstan_mcp_1548")
    monkeypatch.setenv(mod.PATHS_ENV, str(tmp_path / "src"))

    def _no_daemon(*_a, **_k):
        raise AssertionError("the daemon was contacted for an out-of-scope file")

    monkeypatch.setattr(mod, "ensure_daemon", _no_daemon)
    target = tmp_path / "tests" / "T.php"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("<?php" + chr(10), encoding="utf-8")
    assert mod.main(["phpstan-mcp.py", str(target)]) == 0
    data = json.loads(capsys.readouterr().out.strip())
    assert data.get("skipped") == f"path outside {mod.PATHS_ENV} allowlist", data
    assert "ok" not in data and "count" not in data, data
