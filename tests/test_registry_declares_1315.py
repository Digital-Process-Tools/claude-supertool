"""Regression tests for #1315 -- two registry-vs-code token disagreements.

Both were confirmed by direct read against the code: `gl-pipeline`'s
`_FILTERS` accepts the literal token `full` while its `syntax` and refusal
text never named it, and `mcp_stop:--all` reaches the identical code path as
the dedicated `mcp_stop_all` op while `mcp_stop`'s own registry entry never
said so. Neither is a functional bug -- both tokens already worked -- so the
fix is registry text, and these tests pin that text rather than behavior a
unit test elsewhere already covers.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _load(name: str) -> dict:
    return json.loads((ROOT / "presets" / name).read_text(encoding="utf-8"))


def test_gl_pipeline_syntax_declares_full_token() -> None:
    entry = _load("gitlab.json")["ops"]["gl-pipeline"]
    assert "full" in entry["syntax"]


def test_mcp_stop_description_points_to_mcp_stop_all() -> None:
    entry = _load("mcp.json")["ops"]["mcp_stop"]
    assert "mcp_stop_all" in entry["description"]
