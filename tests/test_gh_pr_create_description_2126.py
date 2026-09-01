"""gh-pr-create's own preset description must not claim it reports without
refusing -- #1970 made the closing-reference check a refusal (exit non-zero,
nothing published), but presets/github.json's description was never updated
and still says the opposite (#2126).
"""
from __future__ import annotations

import json
from pathlib import Path

PRESET_PATH = Path(__file__).parent.parent / "presets" / "github.json"


def _gh_pr_create_description() -> str:
    data = json.loads(PRESET_PATH.read_text(encoding="utf-8"))
    return data["ops"]["gh-pr-create"]["description"]


def test_description_does_not_claim_reported_not_refused():
    desc = _gh_pr_create_description()
    assert "Reported, NOT refused" not in desc
    assert "exit code is 0" not in desc


def test_description_says_it_refuses_and_names_no_close():
    """Must-fire control: a description that never mentions refusal at all
    would also pass the negative assertion above, so a positive claim is
    required too."""
    desc = _gh_pr_create_description()
    assert "refus" in desc.lower()
    assert "no_close" in desc
