"""#1089 -- recounting #976's "1 of 14 print shapes" against the scanner
`tests/test_forged_branch_line_965.py` carries today.

#976 probed `_raw_refname_prints` against a fixed catalog of fourteen
non-trivial print/return shapes (A-N below; O is the clean baseline the
scanner must NOT flag) and found one caught. `#1038` and `#1092` widened the
scanner since then: subscript reads (`d['k']` alongside `d.get('k')`),
`return` sinks, and per-function taint scope. This file re-runs the SAME
fourteen shapes -- transcribed verbatim from #976's own table, not
re-imagined -- against the scanner as it stands now, and pins the actual
count so the next widening (or the next silent regression) is a required
edit here rather than a stale sentence nobody revisits.

Measured method: each shape is written to its own scratch file under
`tmp_path` (never inside this repo's own `tests/`/`presets/git` trees --
`tests/_write_guard.py` forbids that), scanned with the real
`_raw_refname_prints`, and classified caught/missed by whether it produced
any finding at all.

Recounted 2026-09-07: 4 of 14 (A, B, G, K), not 1 of 14. `B_subscript` is the
one #1038 actually fixed since #976 was filed. `H_marker_in_same_expr` is
still a real gap and is the one #976 itself flagged as "the dangerous one":
`_unmarked_refnames` treats ANY marker call anywhere in the same expression
as clearing the whole expression, so `flat(t) + d.get('headRefName')` inside
one f-string still launders the raw access beside it. `C`, `D`, `E`, `F`,
`I`, `J`, `L`, `M`, `N` are unchanged misses, each already named in #1089's
own "Not covered" table (concatenation/percent/`.format`, two-arg `print`,
tuple-unpack, an inter-procedural helper, a variable-keyed `.get`,
`AugAssign`, `sys.stdout.write`).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Dict

_ROOT = Path(__file__).parent.parent


def _load_scanner():
    spec = importlib.util.spec_from_file_location(
        "refname_scanner_1089_probe",
        _ROOT / "tests" / "test_forged_branch_line_965.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Transcribed from #976's own table. Every entry uses a real REFNAME_KEYS
#: member (`headRefName`) so a change to that set cannot silently make an
#: entry irrelevant to the scan it is meant to probe.
SHAPES: Dict[str, str] = {
    "A_baseline_get_fstring": (
        "def f(d):\n"
        "    print(f\"branch: {d.get('headRefName')}\")\n"),
    "B_subscript": (
        "def f(d):\n"
        "    print(f\"branch: {d['headRefName']}\")\n"),
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
    "G_marker_elsewhere_in_same_fstring": (
        "def f(d, t):\n"
        "    print(f\"{_untrusted.flat(t)} {d.get('headRefName')}\")\n"),
    "H_marker_in_same_expr": (
        "def f(d, t):\n"
        "    print(f\"{_untrusted.flat(t) + d.get('headRefName')}\")\n"),
    "I_tuple_assign": (
        "def f(d):\n"
        "    a, b = 1, d.get('headRefName')\n"
        "    print(f\"branch: {b}\")\n"),
    "J_via_helper": (
        "def hdr(x):\n"
        "    print(f\"branch: {x}\")\n"
        "def f(d):\n"
        "    hdr(d.get('headRefName'))\n"),
    "K_walrus": (
        "def f(d):\n"
        "    print(f\"branch: {(x := d.get('headRefName'))}\")\n"),
    "L_dict_get_var_key": (
        "def f(d):\n"
        "    k = 'headRefName'\n"
        "    print(f\"branch: {d.get(k)}\")\n"),
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

#: The clean baseline: not one of the fourteen violations, and must never be
#: flagged by anything below.
CLEAN_SHAPE = (
    "def f(d):\n"
    "    print(f\"branch: {_untrusted.flat(d.get('headRefName'))}\")\n")

#: Recounted, measured against this branch's scanner -- not #976's number.
CAUGHT_NOW = frozenset({
    "A_baseline_get_fstring",
    "B_subscript",
    "G_marker_elsewhere_in_same_fstring",
    "K_walrus",
})


def _caught(scanner, tmp_path: Path, name: str, source: str) -> bool:
    sample = tmp_path / (name + ".py")
    sample.write_text(source, encoding="utf-8")
    found = scanner._raw_refname_prints(sample)
    sample.unlink()
    return bool(found)


def test_the_recount_is_4_of_14_not_1_of_14(tmp_path: Path) -> None:
    scanner = _load_scanner()
    caught = {name for name, src in SHAPES.items()
              if _caught(scanner, tmp_path, name, src)}
    assert caught == CAUGHT_NOW, (
        f"the scanner's actual catch set has moved: now catches {sorted(caught)}, "
        f"this test still expects {sorted(CAUGHT_NOW)}. Update CAUGHT_NOW (and "
        f"the docstring's tally) rather than silently widening this assertion -- "
        f"the whole point of #1089's ask is that this number gets re-derived, "
        f"not re-typed."
    )
    assert len(CAUGHT_NOW) == 4
    assert len(SHAPES) == 14


def test_the_clean_baseline_is_never_flagged(tmp_path: Path) -> None:
    """Positive control on the count itself: a scanner that flagged
    everything would also "catch" 14 of 14, which is not what CAUGHT_NOW
    claims -- this pins that the clean shape is excluded from that count."""
    scanner = _load_scanner()
    assert not _caught(scanner, tmp_path, "O_clean_flat", CLEAN_SHAPE)


def test_h_marker_in_same_expr_is_the_named_live_gap() -> None:
    """#976 called this shape out by name as "the dangerous one": a marker
    call ANYWHERE in the same expression clears the whole expression, so a
    flattened value and a raw one sharing one f-string both read as safe."""
    assert "H_marker_in_same_expr" not in CAUGHT_NOW
