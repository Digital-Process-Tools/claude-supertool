"""#515 — the shape of a skipped validator result, pinned to the documentation.

`docs/validators.md` and `refusal.skipped()` published two different shapes for
the same thing. These tests pin the *documented* one, because the old tests
pinned the implementation, which is exactly why the drift went unnoticed.

The decision (see CHANGELOG): a skip **omits** `ok`/`count`/`errors`. Not
because uniformity is worthless, but because nothing in this repo consumes
those keys on a skip — every core consumer branches on `"skipped" in result`
first, and must, since the reason string only exists on a skip — while the
core itself already emitted the omitting shape from four places. `ok: true`
on a receipt meaning "never looked at" is the absence-read-as-a-pass defect
this repo keeps filing.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

import supertool  # noqa: E402

_REFUSAL = _ROOT / "validators" / "common" / "refusal.py"
_SCHEMA = _ROOT / "validators" / "SCHEMA.md"
_DOCS = _ROOT / "docs" / "validators.md"

VERDICT_KEYS = ("ok", "count", "errors")


def _refusal_mod():
    spec = importlib.util.spec_from_file_location("refusal_515", _REFUSAL)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _documented_skip(tool: str = "phpstan-mcp", reason: str = "out of scope") -> dict:
    """A skip written by hand from the prose, as an adapter author would."""
    return {"tool": tool, "file": "src/Foo.php", "duration_ms": 3, "skipped": reason}


def test_the_shared_helper_emits_the_documented_shape() -> None:
    """`refusal.skipped()` must produce what an adapter author reading the docs
    would produce. Padded and omitted shapes are not interchangeable: a
    consumer written against one mis-reads the other."""
    ref = _refusal_mod()
    result = ref.skipped("phpstan", "src/Foo.php", "no files found to analyse", 7)
    present = [k for k in VERDICT_KEYS if k in result]
    assert not present, (
        "skipped() padded verdict keys the documentation tells adapter authors "
        f"to omit: {present} — a receipt carrying ok:true reads as a pass"
    )
    assert result["skipped"] == "no files found to analyse"
    assert result["tool"] == "phpstan"
    assert result["file"] == "src/Foo.php"


def test_the_core_emits_the_same_shape_as_the_shared_helper() -> None:
    """The core's own built-in skips (#477) and the adapter helper are two
    producers of one contract. They may not disagree."""
    ref = _refusal_mod()
    core = supertool._builtin_syntax_run("pycheck", "elvish", "src/foo.el")
    helper = ref.skipped("pycheck", "src/foo.el", "unknown builtin", 0)
    assert "skipped" in core
    core_verdict = {k for k in VERDICT_KEYS if k in core}
    helper_verdict = {k for k in VERDICT_KEYS if k in helper}
    assert core_verdict == helper_verdict, (
        "core and helper publish different skip shapes: "
        f"core has {sorted(core_verdict)}, helper has {sorted(helper_verdict)}"
    )


def _section(text: str, heading: str) -> str:
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == heading)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("#"):
            return "\n".join(lines[start:j])
    return "\n".join(lines[start:])


def test_schema_md_does_not_publish_a_third_version() -> None:
    """SCHEMA.md is the normative document adapter authors are pointed at.
    If it still prescribes padding, the contract has two definitions again."""
    section = _section(_SCHEMA.read_text(encoding="utf-8"), "### Skipped: the third state")
    assert "omit" in section.lower(), (
        "SCHEMA.md's skip section does not say the verdict keys are omitted:\n" + section
    )
    assert not re.search(r"alongside\s+`ok:\s*true`", section), (
        "SCHEMA.md still prescribes padding a skip with ok: true:\n" + section
    )


def test_validators_md_still_states_the_shape_it_won_on() -> None:
    """Guard against the drift being 'resolved' later by editing the prose to
    match a padded helper — the direction this issue decided against."""
    text = _DOCS.read_text(encoding="utf-8")
    assert re.search(r"omit\s+`ok`/`count`/`errors`", text), (
        "docs/validators.md no longer states the omitting shape"
    )


def test_no_core_consumer_needs_ok_on_a_skip() -> None:
    """The reason padding was defended — 'consumers can read result[ok] without
    a branch' — is not true here. Every consumer branches on `skipped` first,
    so the documented shape must survive all of them intact."""
    skip = _documented_skip()
    baseline = {"tool": "phpstan-mcp", "file": "src/Foo.php", "ok": True, "count": 0, "errors": []}

    assert supertool._validator_regressed(None, skip) is False
    assert supertool._validator_regressed(baseline, skip) is False
    assert supertool._validator_result_is_cacheable(skip) is False

    row = supertool._validator_render_row(skip)
    assert any("skipped" in ln for ln in row)
    assert any("out of scope" in ln for ln in row)

    diff = supertool._validator_render_diff(baseline, skip)
    assert any("skipped" in ln for ln in diff)
    assert not any("+1" in ln for ln in diff)
