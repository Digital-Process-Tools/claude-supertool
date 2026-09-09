"""#976 -- widen the refname AST scanner's syntactic coverage for the six
REFNAME_KEYS already in scope (#968 rejected widening which FIELDS are
covered; this is only about which SHAPES of the same six fields are seen).

Written first, red before the fix: every "now caught" assertion below failed
against the scanner as `test_refname_scanner_shape_recount_1089.py` measured
it (4 of 14 -- A, B, G, K), and turns green once `_raw_refname_prints` widens.
Each "now caught" case is paired with a positive control ("still caught")
so a scanner that regressed A/B/G/K would not pass by accident, and the two
genuinely out-of-scope shapes (J: inter-procedural helper call, L: a dict key
read from a variable rather than a literal) are pinned as still-missed --
document what the scanner still cannot catch rather than silently drop them.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).parent.parent


def _load_scanner():
    spec = importlib.util.spec_from_file_location(
        "refname_scanner_976_probe",
        _ROOT / "tests" / "test_forged_branch_line_965.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _caught(scanner, tmp_path: Path, name: str, source: str) -> bool:
    sample = tmp_path / (name + ".py")
    sample.write_text(source, encoding="utf-8")
    found = scanner._raw_refname_prints(sample)
    sample.unlink()
    return bool(found)


# ---------------------------------------------------------------------------
# Positive control: the four shapes #1089 already caught must still be caught
# ---------------------------------------------------------------------------

STILL_CAUGHT = {
    "A_baseline_get_fstring": (
        "def f(d):\n"
        "    print(f\"branch: {d.get('headRefName')}\")\n"),
    "B_subscript": (
        "def f(d):\n"
        "    print(f\"branch: {d['headRefName']}\")\n"),
    "G_marker_elsewhere_in_same_fstring": (
        "def f(d, t):\n"
        "    print(f\"{_untrusted.flat(t)} {d.get('headRefName')}\")\n"),
    "K_walrus": (
        "def f(d):\n"
        "    print(f\"branch: {(x := d.get('headRefName'))}\")\n"),
}


def test_previously_caught_shapes_still_caught(tmp_path: Path) -> None:
    scanner = _load_scanner()
    for name, source in STILL_CAUGHT.items():
        assert _caught(scanner, tmp_path, name, source), (
            f"{name} regressed: the widening must not narrow existing coverage")


def test_clean_baseline_still_never_flagged(tmp_path: Path) -> None:
    scanner = _load_scanner()
    clean = (
        "def f(d):\n"
        "    print(f\"branch: {_untrusted.flat(d.get('headRefName'))}\")\n")
    assert not _caught(scanner, tmp_path, "O_clean_flat", clean)


# ---------------------------------------------------------------------------
# Now caught -- the widened shapes, for the six keys already in REFNAME_KEYS
# ---------------------------------------------------------------------------

NOW_CAUGHT = {
    "C_concat": (
        "def f(d):\n"
        "    print('branch: ' + d.get('headRefName'))\n"),
    "D_print_two_args": (
        "def f(d):\n"
        "    print('branch:', d.get('headRefName'))\n"),
    "E_percent_format": (
        "def f(d):\n"
        "    print('branch: %s' % d.get('headRefName'))\n"),
    "F_str_format": (
        "def f(d):\n"
        "    print('branch: {}'.format(d.get('headRefName')))\n"),
    "H_marker_in_same_expr": (
        "def f(d, t):\n"
        "    print(f\"{_untrusted.flat(t) + d.get('headRefName')}\")\n"),
    "I_tuple_assign": (
        "def f(d):\n"
        "    a, b = 1, d.get('headRefName')\n"
        "    print(f\"branch: {b}\")\n"),
    "M_augassign": (
        "def f(d):\n"
        "    x = ''\n"
        "    x += d.get('headRefName')\n"
        "    print(f\"branch: {x}\")\n"),
    "N_sys_stdout_write": (
        "import sys\n"
        "def f(d):\n"
        "    sys.stdout.write(f\"branch: {d.get('headRefName')}\")\n"),
}


def test_widened_shapes_now_caught(tmp_path: Path) -> None:
    scanner = _load_scanner()
    missed = [name for name, source in NOW_CAUGHT.items()
              if not _caught(scanner, tmp_path, name, source)]
    assert missed == [], f"still missed after widening: {missed}"


def test_join_of_a_container_holding_a_tainted_name_is_a_documented_boundary(
        tmp_path: Path) -> None:
    """The real shape `presets/git/worktrees.py::_pr_detail` had: `base =
    pr.get('baseRefName') or '?'`, `base` embedded in an f-string inside a
    list, then `' . '.join(bits)`. Chasing taint THROUGH a container that is
    later handed to an arbitrary call (`.join()`, `sorted()`, ...) was tried
    and reverted (#976, see `_scan_scope`'s own note): it is exactly the
    shape that turned an unrelated regex match's `.group()` result into a
    false positive elsewhere in this repo (`presets/gitlab/job.py`'s
    `mr_iid = mr_match.group(1)` after `mr_match = re.match(pattern, ref)`).
    Both are "an already-tainted name was referenced somewhere upstream",
    and the scanner cannot tell the display case (bits -> print) from the
    computation case (ref -> regex -> group) without inter-procedural
    reasoning about what `.join`/`.group`/`re.match` DO -- a general taint
    tracker. `_pr_detail` itself is fixed directly at the source instead
    (`_untrusted.flat` on `base`), which is this issue's own fallback for
    exactly this situation: "fix, or file".
    """
    scanner = _load_scanner()
    source = (
        "def pr_detail(pr):\n"
        "    number = pr.get('number', '?')\n"
        "    base = pr.get('baseRefName') or '?'\n"
        "    bits = [f'PR #{number} -> {base}', 'ok']\n"
        "    return ' . '.join(bits)\n"
    )
    assert not _caught(scanner, tmp_path, "transitive_live_instance", source)


# ---------------------------------------------------------------------------
# Still out of scope -- documented, not silently dropped
# ---------------------------------------------------------------------------

STILL_MISSED = {
    "J_via_helper": (
        "def hdr(x):\n"
        "    print(f\"branch: {x}\")\n"
        "def f(d):\n"
        "    hdr(d.get('headRefName'))\n"),
    "L_dict_get_var_key": (
        "def f(d):\n"
        "    k = 'headRefName'\n"
        "    print(f\"branch: {d.get(k)}\")\n"),
}


def test_j_and_l_remain_documented_boundaries(tmp_path: Path) -> None:
    """J needs inter-procedural call-graph tracking; L needs constant
    propagation of a string literal into a *variable*-keyed dict lookup.
    Both are exactly the step from "syntactic widening of one function's
    own local names" to "a general taint tracker" that #968 already refused
    to take for a different axis (which fields). This pins the boundary so
    a future widening that starts catching one of these is a deliberate,
    documented decision rather than an untested side effect."""
    scanner = _load_scanner()
    for name, source in STILL_MISSED.items():
        assert not _caught(scanner, tmp_path, name, source), (
            f"{name} is now caught -- this crosses into inter-procedural or "
            f"constant-propagation territory; update this test AND the "
            f"scanner's own boundary docs deliberately, do not just widen "
            f"this assertion")


# ---------------------------------------------------------------------------
# A third boundary the review pass found: the same C/D/E/F shapes, but
# through `return` instead of a call sink -- not counted in NOW_CAUGHT
# above (whose fixtures are all print()-based, matching #976's own table),
# and not named until this review pass pointed out it reads as an
# oversight rather than a decision.
# ---------------------------------------------------------------------------

RETURN_SHAPES_STILL_MISSED = {
    "C_concat_via_return": (
        "def f(d):\n"
        "    return 'branch: ' + d.get('headRefName')\n"),
    "E_percent_format_via_return": (
        "def f(d):\n"
        "    return 'branch: %s' % d.get('headRefName')\n"),
    "F_str_format_via_return": (
        "def f(d):\n"
        "    return 'branch: {}'.format(d.get('headRefName'))\n"),
}


def test_return_of_a_concat_or_format_is_a_documented_boundary_too(
        tmp_path: Path) -> None:
    """`_scan_scope` deliberately restricts the whole-argument blanket scan
    (the mechanism that catches C/D/E/F for print()) to CALL sinks, and
    keeps `return` on the narrower FormattedValue-scoped check -- because a
    function can legitimately `return` an unflattened structured value for
    its OWN caller to flatten (`presets/github/prs.py::_branches` returns
    `_board.branch_pair(...)` raw for `_board.render_row` to flatten one
    call away; scanning `return` as broadly as `print()` turned that
    pattern into a false positive). One consequence of that restriction:
    the SAME three non-f-string shapes that are caught through print() are
    still missed through return. Found in review (oss:auditor spawn) as an
    undocumented gap rather than a decision -- pinned here, alongside J/L,
    so it reads as the latter from now on."""
    scanner = _load_scanner()
    for name, source in RETURN_SHAPES_STILL_MISSED.items():
        assert not _caught(scanner, tmp_path, name, source), (
            f"{name} is now caught through `return` -- if this is deliberate, "
            f"update this test, `presets/_untrusted.py`'s boundary paragraph, "
            f"and `_scan_scope`'s own comment on why `return` is scanned "
            f"narrower than a call sink")
