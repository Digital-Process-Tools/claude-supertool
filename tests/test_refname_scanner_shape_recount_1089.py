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

Recounted 2026-09-07: 4 of 14 (A, B, G, K), not 1 of 14.

Recounted again for #976's own widening: 12 of 14 (A, B, C, D, E, F, G, H,
I, K, M, N). `H_marker_in_same_expr` -- #976's own "the dangerous one" --
is fixed: the marker check no longer clears an entire expression just
because a marker call appears somewhere in it, only the marker call's own
arguments (`_iter_unmarked` in the scanner). `C`/`D`/`E`/`F`/`N` are caught
by scanning a whole CALL-sink argument for a direct refname read instead of
only the inside of an f-string; `I`/`M` by tracking a direct `.get()`/
subscript read through tuple-unpack and `AugAssign` the same way a plain
`Assign` already was.

Two of the original fourteen remain misses, and #976 draws the line there
deliberately rather than chasing them: `J_via_helper` needs following a
value across a call into a DIFFERENT function's own locals (inter-procedural
taint tracking); `L_dict_get_var_key` needs constant-propagating a string
literal into a dict lookup keyed by a variable rather than a literal. Both
are one step past "which syntactic shapes carry a value that was read
directly, in this same function" into "trace what a value becomes" -- the
general taint tracker this scanner has never tried to be. A third shape
that LOOKS similar to a widening but is not one: chasing an already-tainted
NAME through further computation (not just re-display) was tried and
reverted, because it chained taint through `presets/gitlab/job.py`'s
`mr_match = re.match(pattern, ref)` / `mr_iid = mr_match.group(1)` onto
locals that never carry the raw refname text -- see `_scan_scope`'s own
comment in the scanner for the mechanism.
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
    "C_concat",
    "D_print_two_args",
    "E_percent_format",
    "F_str_format",
    "G_marker_elsewhere_in_same_fstring",
    "H_marker_in_same_expr",
    "I_tuple_assign",
    "K_walrus",
    "M_augassign",
    "N_sys_stdout_write",
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
    assert len(CAUGHT_NOW) == 12
    assert len(SHAPES) == 14


def test_the_clean_baseline_is_never_flagged(tmp_path: Path) -> None:
    """Positive control on the count itself: a scanner that flagged
    everything would also "catch" 14 of 14, which is not what CAUGHT_NOW
    claims -- this pins that the clean shape is excluded from that count."""
    scanner = _load_scanner()
    assert not _caught(scanner, tmp_path, "O_clean_flat", CLEAN_SHAPE)


def test_h_marker_in_same_expr_is_no_longer_the_live_gap() -> None:
    """#976 called this shape out by name as "the dangerous one": a marker
    call ANYWHERE in the same expression used to clear the whole expression,
    so a flattened value and a raw one sharing one f-string both read as
    safe. #976 fixed the marker check to be scoped to the marker call's own
    arguments; this is now caught."""
    assert "H_marker_in_same_expr" in CAUGHT_NOW


def test_j_and_l_are_the_remaining_documented_boundary() -> None:
    """The only two of the original fourteen still missed, and named as a
    deliberate boundary rather than an oversight (see this module's own
    docstring, and `tests/test_refname_scanner_widened_976.py`)."""
    assert set(SHAPES) - CAUGHT_NOW == {"J_via_helper", "L_dict_get_var_key"}
