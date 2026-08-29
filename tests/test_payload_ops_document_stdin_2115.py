"""#2115: a payload op that accepts `@-` must say so in its signature.

`_load_at_payload` resolves `@-` from stdin for every op that takes an
`@reference`, preset script or not. Four ops advertised only `@FILE`, and the
callers followed the signature: measured over 42h of transcripts,
`gh-pr-create` ran 104 times from a file and twice from stdin, while every op
whose signature named `@-` ran through stdin over 95% of the time. The
signature is the only thing most callers read, so one that omits the cheaper
route costs a round-trip on every use.

This pins the general rule rather than the four instances, so a fifth payload
op cannot ship with the same gap.
"""

import json
import os
import glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _preset_ops():
    """Yield (preset_file, op_name, syntax) for every op declaring a syntax."""
    for path in sorted(glob.glob(os.path.join(ROOT, "presets", "*.json"))):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        ops = data.get("ops") or {}
        if not isinstance(ops, dict):
            continue
        for name, spec in ops.items():
            if not isinstance(spec, dict):
                continue
            syntax = spec.get("syntax")
            if isinstance(syntax, str):
                yield os.path.basename(path), name, syntax


def test_every_at_file_op_also_advertises_stdin():
    """An op whose syntax names @FILE must also name @-.

    `batch:@FILE | batch:@-` is the spelling to copy.
    """
    missing = [
        (preset, name, syntax)
        for preset, name, syntax in _preset_ops()
        if "@FILE" in syntax and "@-" not in syntax
    ]
    assert not missing, (
        "these ops accept @- but their signature only names @FILE, so callers "
        "write a temp file first (#2115): "
        + "; ".join(f"{p}:{n} -> {s}" for p, n, s in missing)
    )


def test_the_four_ops_the_issue_names_are_covered():
    """Guard the guard: if these ops are renamed away, the sweep above can
    silently pass on an empty set."""
    seen = {name for _, name, _ in _preset_ops()}
    for op in ("gh-issue-create", "gh-pr-create", "gh-pr-edit"):
        assert op in seen, f"{op} no longer declares a syntax — #2115's sweep may be vacuous"
