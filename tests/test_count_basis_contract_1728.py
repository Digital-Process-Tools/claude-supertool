"""An adapter may pre-subtract its stall rows or cap `errors` — never both (#1728).

`_validator_measured_count` is `max(count - absences, len(rows) - absences, 0)`.
The floor exists for the adapter whose `count` already excludes its own
`adapter`-coded stall rows: without it, subtracting twice drives the measured
count below the findings actually printed, and the rollback gate goes inert in
the quiet direction. Recomputing from `errors` instead would break the *other*
adapter — the one that caps `errors` at some maximum — which is why
`test_a_capped_error_list_is_not_read_as_a_smaller_count` pins the refusal of
that fix in `tests/test_mixed_payload_rollback_1717.py`.

Neither arithmetic covers the payload whose **`count` is bounded by the same cap
that bounds `errors`**. Both terms of the `max()` then saturate, before and after
compare equal, `_validator_regressed` returns False, and a validator with
`rollback_on_fail: true` does not revert over a genuinely new finding.

**Measured, on master, before this file existed** — a five-row cap over a growing
findings list, `count` pre-subtracted:

    before  count=4  errors=[f1, f2, f3, f4, stall]  -> measured 4
    after   count=4  errors=[f5, f2, f3, f4, stall]  -> measured 4
    _validator_regressed(before, after)              -> False

No arithmetic can tell those two payloads apart, because **a cap is invisible in
a single payload unless the adapter declares it**. So the fix is not arithmetic.
It is a declared convention plus a guard: `count_basis` says whether `count`
already excludes the stall rows, `errors_truncated` says whether findings were
dropped from the list, and a payload whose declaration contradicts its own rows
is reported as a fault **against the adapter** rather than measured.

Three states, not two, and this is the third one arriving through the door
`refusal.crashed()` already uses: an `adapter`-coded row, so the result renders
`NOT CHECKED`, is never subtracted from a baseline, never reverts an edit, and
still exits non-zero. Never `skipped` — that would be quieter than the bug.

**Declaring is optional.** An undeclared payload keeps today's heuristic
untouched, because the population is not only the 36 shipped adapters: any repo
can name its own validator in `.supertool.json`, and a runtime mandate would
break every third-party adapter on upgrade. The mandate is a test over the
shipped tree instead — `_GRANDFATHERED` below, a set that may only shrink.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool

ROOT = Path(__file__).parent.parent
BEFORE = chr(123) + chr(34) + "a" + chr(34) + ": 1" + chr(125) + chr(10)
AFTER = chr(123) + chr(34) + "b" + chr(34) + ": 1" + chr(125) + chr(10)


def _finding(n: int = 1, msg: str = "unterminated object") -> dict:
    return {"line": n, "col": 1, "severity": "error", "code": "E999",
            "msg": f"{msg} {n}"}


def _stall(msg: str = "cargo check reported src/other.rs") -> dict:
    """The row SCHEMA.md reserves for "no verdict was obtained about this file"."""
    return {"line": None, "col": None, "severity": "error", "code": "adapter",
            "msg": msg}


def _payload(count, errors, **declared) -> dict:
    out = {"tool": "fake", "file": "s.json", "ok": not errors, "count": count,
           "errors": list(errors), "duration_ms": 1}
    out.update(declared)
    return out


#: The shape the issue is about: a cap that bounds `count` as well as `errors`.
SATURATED_BEFORE = _payload(4, [_finding(1), _finding(2), _finding(3),
                               _finding(4), _stall()])
SATURATED_AFTER = _payload(4, [_finding(5), _finding(2), _finding(3),
                              _finding(4), _stall()])

#: Conforming shapes, one per convention, named after the adapter that ships it.
CARGO_SHAPE = dict(count_basis="total", errors_truncated=False)
PHPSTAN_SHAPE = dict(count_basis="measured", errors_truncated=False)
CAPPING_SHAPE = dict(count_basis="total", errors_truncated=True)

CONFORMING = {
    # cargo-check: `count = len(errors)`, stall rows included, nothing dropped.
    "cargo-check": _payload(2, [_finding(), _stall()], **CARGO_SHAPE),
    # phpstan: `count` from `totals.file_errors`, `errors` from another key of
    # the same document, so `count` never counted a stall row to begin with.
    "phpstan": _payload(3, [_finding(1), _finding(2), _finding(3)],
                        **PHPSTAN_SHAPE),
    # The capping adapter nothing ships today: `errors` is a sample, `count` is
    # the whole total. This is the one the floor could not have served.
    "capping": _payload(50, [_finding(n) for n in range(1, 6)] + [_stall()],
                        **CAPPING_SHAPE),
    # A clean file still declares — `count: 0` is a measurement like any other.
    "clean": _payload(0, [], **CARGO_SHAPE),
}


# ---------------------------------------------------------------------------
# Why this exists: the payload that goes inert, pinned as it behaves today
# ---------------------------------------------------------------------------

def test_the_undeclared_saturated_payload_is_inert_and_stays_inert() -> None:
    """The residue, stated rather than implied.

    An undeclared payload keeps the `max()` heuristic exactly as it was, so this
    assertion is as true after the guard as before it. That is the point: the
    guard does not repair an adapter that says nothing, it gives an adapter a
    way to say something and then holds it to it. A test that went green here
    would mean the heuristic had changed under every third-party adapter.
    """
    assert supertool._validator_measured_count(SATURATED_BEFORE) == 4
    assert supertool._validator_measured_count(SATURATED_AFTER) == 4
    assert supertool._validator_regressed(SATURATED_BEFORE, SATURATED_AFTER) is False


# ---------------------------------------------------------------------------
# The guard: a declaration that contradicts its own rows is a fault
# ---------------------------------------------------------------------------

def test_a_cap_that_saturated_the_count_is_reported_against_the_adapter() -> None:
    """`errors_truncated` means findings were dropped, so `count` must exceed
    the rows printed. When it does not, the cap bounded both and the before/after
    comparison cannot mean anything."""
    payload = dict(SATURATED_BEFORE, **CAPPING_SHAPE)
    fault = supertool._validator_count_contract_fault(payload)
    assert fault, (
        "a payload declaring a truncated list whose count is no larger than the "
        "rows it printed is the shape #1728 is about, and it was accepted")
    assert "errors_truncated" in fault and "4" in fault, fault


def test_pre_subtracting_and_capping_at_once_is_the_forbidden_pair() -> None:
    """The constraint itself. Spellable, therefore reportable — an enum that
    made it unspellable would have had nothing to report and no way to tell an
    adapter it had done the one thing it may not do."""
    payload = _payload(4, [_finding(), _stall()],
                       count_basis="measured", errors_truncated=True)
    fault = supertool._validator_count_contract_fault(payload)
    assert fault, "the forbidden pair was accepted"
    assert "measured" in fault and "errors_truncated" in fault, fault


def test_half_a_declaration_is_not_a_declaration() -> None:
    """Both keys or neither. One alone leaves the question the pair exists to
    force — "which of the two conventions is this?" — unanswered while looking
    answered."""
    for half in (dict(count_basis="total"), dict(errors_truncated=True)):
        payload = _payload(2, [_finding(), _stall()], **half)
        assert supertool._validator_count_contract_fault(payload), sorted(half)


def test_an_unknown_basis_is_a_fault_and_not_a_shrug() -> None:
    payload = _payload(2, [_finding(), _stall()],
                       count_basis="approximate", errors_truncated=False)
    fault = supertool._validator_count_contract_fault(payload)
    assert fault and "approximate" in fault, fault


def test_a_declared_count_that_is_not_a_number_is_a_fault() -> None:
    """Undeclared, a non-numeric `count` reads as 0 rather than raising mid-edit.
    Declared, it is an adapter that stated a convention and then did not follow
    it, which is the thing this guard is for."""
    payload = _payload("many", [_finding()], **CARGO_SHAPE)
    assert supertool._validator_count_contract_fault(payload)


def test_a_count_below_the_rows_it_printed_is_a_fault_when_declared() -> None:
    """The floor, turned from a silent repair into a report.

    `max()` quietly corrected this payload and no one was told. Under a
    declaration the correction is not available: `total` says `count` counts
    every row, so a `count` smaller than the rows is a contradiction.
    """
    payload = _payload(1, [_finding(1), _finding(2), _stall()], **CARGO_SHAPE)
    assert supertool._validator_count_contract_fault(payload)


# ---------------------------------------------------------------------------
# The positive control: a checker that rejects everything passes the half above
# for free
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(CONFORMING))
def test_a_conforming_payload_is_not_faulted(name: str) -> None:
    fault = supertool._validator_count_contract_fault(CONFORMING[name])
    assert fault is None, f"{name}: {fault}"


@pytest.mark.parametrize("name", sorted(CONFORMING))
def test_declaring_changes_no_measurement(name: str) -> None:
    """The safety claim, asserted rather than asserted-in-prose.

    For every conforming shape the declaration reproduces the heuristic's answer
    exactly, so no shipped adapter's numbers move when it starts declaring. The
    floor was right; it just could not know it was.
    """
    declared = CONFORMING[name]
    bare = {k: v for k, v in declared.items()
            if k not in ("count_basis", "errors_truncated")}
    assert (supertool._validator_measured_count(declared)
            == supertool._validator_measured_count(bare))


def test_an_undeclared_payload_is_never_faulted() -> None:
    """Grandfathering, at runtime. Third-party adapters named in a repo's own
    `.supertool.json` are part of this population and cannot be edited from
    here, so a runtime mandate would break them on upgrade."""
    for payload in (SATURATED_BEFORE, SATURATED_AFTER):
        assert supertool._validator_count_contract_fault(payload) is None


def test_a_skipped_result_is_never_faulted() -> None:
    """A skip carries no `count` and no `errors` — there is no arithmetic to
    protect, and faulting one would convert the third state into a red."""
    assert supertool._validator_count_contract_fault(
        {"tool": "fake", "file": "s.json", "duration_ms": 1,
         "skipped": "tool not installed"}) is None


def test_a_non_dict_is_declined_rather_than_crashed() -> None:
    assert supertool._validator_count_contract_fault(None) is None
    assert supertool._validator_count_contract_fault([1, 2]) is None


# ---------------------------------------------------------------------------
# End to end: the post-condition is the file's bytes and the exit code
# ---------------------------------------------------------------------------

def _two_pass_adapter(tmp_path: Path, first: dict, second: dict) -> str:
    """Answers `first` on the baseline spawn and `second` on every later one."""
    state = tmp_path / "_calls.txt"
    script = tmp_path / "_adapter.py"
    script.write_text(
        "import pathlib, sys" + chr(10)
        + f"state = pathlib.Path({str(state)!r})" + chr(10)
        + "n = int(state.read_text()) if state.exists() else 0" + chr(10)
        + "state.write_text(str(n + 1))" + chr(10)
        + f"sys.stdout.write({json.dumps(first)!r} if n == 0 "
          f"else {json.dumps(second)!r})" + chr(10),
        encoding="utf-8",
    )
    return "{python} " + script.as_posix()


def _configure(cmd: str) -> None:
    supertool._CONFIG = {"validators": {
        "fake": {"cmd": cmd, "match": "*.json", "cache": False,
                 "rollback_on_fail": True,
                 "hooks_into": ["edit", "replace", "replace_lines", "paste",
                                "append", "vim"],
                 "timeout": 10},
    }}
    supertool._CONFIG_CHECKED = True


def _edit(tmp_path: Path, capsys):
    f = tmp_path / "s.json"
    f.write_text(BEFORE, encoding="utf-8")
    rc = supertool.main([f"edit:::{chr(34)}a{chr(34)}:::{chr(34)}b{chr(34)}:::{f}"])
    return rc, capsys.readouterr().out, f.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _stable_branch(monkeypatch):
    monkeypatch.setattr(supertool, "_branch_reading", lambda: ("f", ""))


def test_a_violating_adapter_is_loud_and_never_measured(
        tmp_path: Path, capsys) -> None:
    """The whole change, at the boundary a caller sees.

    Both passes are the saturated capping payload, now declaring. Undeclared,
    this call renders a verdict, compares 4 with 4, exits 0 and lets a new
    finding through a `rollback_on_fail` validator. Declared, the payload is a
    fault about the adapter: `NOT CHECKED`, no comparison, non-zero exit.

    The edit **survives** — a non-verdict never reverts, exactly as #969 and
    #1717 hold — because the pre-edit bytes were not checked either.
    """
    _configure(_two_pass_adapter(tmp_path,
                                 dict(SATURATED_BEFORE, **CAPPING_SHAPE),
                                 dict(SATURATED_AFTER, **CAPPING_SHAPE)))
    rc, out, text = _edit(tmp_path, capsys)
    assert "NOT CHECKED" in out, out
    assert "errors_truncated" in out, out
    assert text == AFTER, f"a non-verdict reverted an edit:{chr(10)}{out}"
    assert rc != 0, f"a gate that did not run exited 0:{chr(10)}{out}"


def test_a_conforming_capping_adapter_still_rolls_an_edit_back(
        tmp_path: Path, capsys) -> None:
    """The must-fire half. A guard that faulted every declaring payload would
    pass the test above for free and turn every capping adapter into a
    permanent `NOT CHECKED`."""
    before = _payload(50, [_finding(n) for n in range(1, 6)], **CAPPING_SHAPE)
    after = _payload(51, [_finding(n) for n in range(1, 6)], **CAPPING_SHAPE)
    _configure(_two_pass_adapter(tmp_path, before, after))
    _rc, out, text = _edit(tmp_path, capsys)
    assert "NOT CHECKED" not in out, out
    assert text == BEFORE, f"a real new finding was not gated:{chr(10)}{out}"
    assert "rolled back" in out, out


# ---------------------------------------------------------------------------
# The shipped tree: declaring is mandatory for new adapters, grandfathered for
# the rest, and the set may only shrink
# ---------------------------------------------------------------------------

#: Shipped adapters that do not yet declare their count convention. Every one of
#: them satisfies the constraint today by construction rather than by statement,
#: which is not the same thing — an accident that holds is not a guarantee, and
#: the next adapter is written by copying one of these. **This set may only
#: shrink.** A new adapter joining it reddens the test below, which is the
#: `_UNDECLARED_PATH_OPS` pattern the core already uses for the same purpose.
#:
#: `cargo-check` and `phpstan` are deliberately absent: they are the two the
#: `_validator_measured_count` docstring cites as the divergent conventions, so
#: they are the two most likely to be copied, and they now say which they are.
_GRANDFATHERED = frozenset({
    "bash-check", "changelog-fragment", "eslint", "git-status", "gitleaks",
    "go-vet", "gofmt-check", "hadolint", "html-check", "inilint", "jit-index",
    "jsonlint", "lsp-diag", "markdownlint", "node-check", "phplint", "phpmd",
    "phpmd-mcp", "phpstan-mcp", "phpunit-mcp", "prettier-check", "psr",
    "py-compile", "pyright", "rector-mcp", "ruby-check", "ruff", "shellcheck",
    "stylelint", "terraform-check", "tomllint", "tsc-check", "xmllint",
    "yaml-check",
})


def _adapters() -> dict:
    """Every shipped adapter, by name, mapped to its source text."""
    out = {}
    for path in sorted((ROOT / "validators").glob("*/*.py")):
        if path.parent.name == "common" or path.stem != path.parent.name:
            continue
        out[path.stem] = path.read_text(encoding="utf-8")
    if len(out) < 30:
        raise AssertionError(
            f"the adapter sweep found {len(out)} adapters, which is not a "
            f"tree this assertion can speak about — an empty sweep reads "
            f"exactly like a tree where every adapter declares")
    return out


def test_the_two_cited_adapters_declare_their_convention() -> None:
    """An accident that holds is not a guarantee (#1728)."""
    sources = _adapters()
    for name in ("cargo-check", "phpstan"):
        assert "count_basis" in sources[name], (
            f"{name} is cited as a worked example of one of the two "
            f"conventions and does not say which one it is")


def test_no_adapter_joins_the_grandfathered_set() -> None:
    """The set only shrinks. A new adapter that does not declare arrives as a
    red asking its author the question, rather than as a thirty-fifth silent
    member of a list nobody rereads."""
    undeclared = {n for n, src in _adapters().items() if "count_basis" not in src}
    joined = sorted(undeclared - _GRANDFATHERED)
    assert not joined, (
        f"{len(joined)} adapter(s) {joined} publish a count and do not declare "
        f"whether it excludes their `adapter` rows or whether `errors` is "
        f"capped. Declare both keys (validators/SCHEMA.md) rather than adding "
        f"a name here.")
    stale = sorted(_GRANDFATHERED - set(_adapters()))
    assert not stale, (
        f"_GRANDFATHERED names {stale}, which is not a shipped adapter — a "
        f"stale name makes the set look wider than the tree it guards")


def test_the_grandfathered_set_holds_nothing_that_now_declares() -> None:
    """Shrinking is the only legal direction, so a name that has started
    declaring must leave rather than sit there as headroom."""
    declaring = {n for n, src in _adapters().items() if "count_basis" in src}
    assert not sorted(declaring & _GRANDFATHERED)


def test_a_changelog_fragment_exists() -> None:
    from _changelog_findable import assert_change_is_findable
    assert_change_is_findable(1728)
