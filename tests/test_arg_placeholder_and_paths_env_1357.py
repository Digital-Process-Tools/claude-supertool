"""#1357's two residues from #1350, decided.

**1. The proposed `{arg}`-beside-a-PATH-syntax lint is not built, and this file
is the measurement that says why.** Walked over every shipped preset manifest
plus this repo's own `.supertool.json` on `b7e1227`::

    ops whose cmd substitutes {arg}                          24  (21 since #873)
      of those, whose syntax names a PATH/FILE component      8
        of those 8, already demanded by _entry_names_a_path   8
        of those 8, already in _UNDECLARED_PATH_OPS           8
        of those 8, the lint would newly reach                0

The proposed lint's population is a strict subset of the population the gate
already holds. `_syntax_names_a_path` fires on exactly the ops whose `syntax`
names a path, so an op carrying `{arg}` beside a PATH-shaped `syntax` is
already refused at dispatch unless it declares — a hard refusal, strictly
stronger than a lint — or is named in the grandfather register, where it is
already recorded as debt. There is no third case, so the lint yields nothing.

**And it is aimed at the wrong half of the residue.** The residue #1357 states
is "an op that means a path and writes `{arg}` gets no gate and no warning".
That op's `syntax` does *not* name a path — if it did, the gate would already
have it. So the shape the lint keys on is the one shape already covered, and
the shape it was proposed for it cannot see. The residue is pinned below so the
proposal is not re-derived from the issue text alone.

The detector is deliberately not widened: 13 shipped ops pass a handle, a ref,
a tag, an ID or a repo slug through `{arg}` and none takes a path. That trade
was made with numbers in #1350; this file re-measures rather than repeats it —
21 ops carry `{arg}`, 8 name a path in `syntax`, leaving those 13. It was 24
and 16 until #873 moved three multi-token ops to `{args}`; the path-shaped 8
did not move, so the shape of the argument is unchanged.

**2. `paths` is reserved.** It was exported to every declaring op's subprocess
as `SUPERTOOL_PATHS`. Nothing reads it — a tree-wide grep for `SUPERTOOL_PATHS`
returns zero — and it is a containment *declaration* travelling into the
environment of the process it constrains, which is the wrong direction. Same
reasoning and same fix as `replaces` in #1347.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import supertool

_ROOT = Path(__file__).resolve().parent.parent


def _registry_entries() -> List[Tuple[str, str, Dict]]:
    """(op name, manifest, entry) for every dict-shaped shipped op.

    Read off the manifests on disk rather than a loaded registry: the counts
    below are claims about what ships, and a registry filtered by this
    checkout's enabled `presets` list would under-count silently.
    """
    out: List[Tuple[str, str, Dict]] = []
    files = sorted((_ROOT / "presets").glob("*.json")) + [
        _ROOT / ".supertool.json"]
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        for name, entry in (data.get("ops") or {}).items():
            if isinstance(entry, dict):
                out.append((name, f.name, entry))
    return out


def _arg_ops() -> List[Tuple[str, str, Dict]]:
    return [r for r in _registry_entries()
            if "{arg}" in str(r[2].get("cmd", ""))]


def _path_shaped_arg_ops() -> List[Tuple[str, str, Dict]]:
    return [r for r in _arg_ops()
            if supertool._syntax_names_a_path(r[2].get("syntax", ""))]


def test_the_arg_lint_would_reach_no_op_the_gate_does_not_already_hold():
    """The measurement that decided #1357.1 — build nothing.

    Every shipped op carrying `{arg}` beside a PATH-shaped `syntax` is already
    either demanded to declare by `_entry_names_a_path` or named in the
    grandfather register. A lint over that population reports what two existing
    mechanisms already report.
    """
    rows = _path_shaped_arg_ops()
    assert rows, "no {arg} op names a path — re-measure before citing this"

    undetected = sorted(n for n, _f, e in rows
                        if supertool._entry_names_a_path(e) is None)
    assert undetected == [], undetected

    unheld = sorted(n for n, _f, e in rows
                    if n not in supertool._UNDECLARED_PATH_OPS
                    and "paths" not in e)
    assert unheld == [], unheld


def test_the_counts_the_decision_rests_on():
    """Pinned so the negative result cannot rot into folklore."""
    arg_ops = _arg_ops()
    path_shaped = _path_shaped_arg_ops()
    # 24 → 21 in #873: `hashnode_list`, `devto_list` and `hashnode_search` each
    # documented a `[:N]` second token that a `{arg}` template cannot receive,
    # so all three moved to `{args}`. The 8 path-shaped ops are unchanged — the
    # three that moved pass a username or a query, not a path — so the trade
    # this file measures (13 ops carrying a handle, ref, tag, ID or slug that a
    # widened `{arg}` signal would refuse) is the same argument at a smaller N.
    assert len(arg_ops) == 21, sorted(n for n, _f, _e in arg_ops)
    assert len(path_shaped) == 8, sorted(n for n, _f, _e in path_shaped)
    assert len(arg_ops) - len(path_shaped) == 13


def test_a_new_arg_op_with_a_path_shaped_syntax_is_refused_not_linted():
    """The stronger-than-a-lint half, driven through the real gate."""
    entry = {"cmd": "cat {arg}", "syntax": "probe:PATH"}
    err = supertool._preset_path_containment(
        "probe", entry, ["probe", "/etc/passwd"])
    assert err is not None
    assert "no containment boundary" in err, err
    assert "names a path in its syntax" in err, err


def test_the_residue_the_lint_does_not_reach():
    """`{arg}` meaning a path behind a `syntax` that does not say so.

    Still ungated, deliberately, and **the proposed lint would not have moved
    this line** — it keys on a PATH-shaped `syntax`, which this entry does not
    have. Recorded rather than fixed: closing it means widening `{arg}` into a
    path signal, which refuses 16 shipped ops that take no path.
    """
    entry = {"cmd": "cat {arg}", "syntax": "probe:TARGET"}
    assert supertool._entry_names_a_path(entry) is None
    assert supertool._preset_path_containment(
        "probe", entry, ["probe", "/etc/passwd"]) is None


def test_paths_is_not_handed_to_the_op_subprocess(tmp_path):
    """A containment declaration must not enter the process it constrains."""
    probe = ("import os, json; print(json.dumps(sorted("
             "k for k in os.environ if k.startswith('SUPERTOOL_'))))")
    (tmp_path / ".supertool.json").write_text(json.dumps({
        "ops": {
            "probe": {
                "safety": "read-only",
                "cmd": "{python} -c " + json.dumps(probe),
                "lines": 80,
                "syntax": "probe:PATH",
                "paths": {"args": [], "root": "cwd"},
            }
        }
    }), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_ROOT / "supertool.py"), "probe"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(tmp_path), timeout=120)
    assert "SUPERTOOL_LINES" in proc.stdout, proc.stdout
    assert "SUPERTOOL_PATHS" not in proc.stdout, proc.stdout
