"""`repo:OWNER/NAME` is a real op that no registry admits exists (#1240).

`repo:` ships in `presets/_repo_target.py`, is resolved by core before the op
loop, sets ``SUPERTOOL_REPO`` for every `gh-*` op in the call and is refused by
any op that cannot honour it. Its whole behaviour is documented in prose and
declared nowhere machine-readable, so:

* `_valid_op_names()` — the list the unknown-op message prints — omits it, which
  is #614's defect one layer in: a caller asking "can I do this?" is told no.
* `ops` renders from `.supertool.json`, so the op is undiscoverable.
* `claims` builds its op registry from the same three config sections, so every
  document citing `repo:OWNER/NAME` is reported *unchecked* rather than holding.

There is no judgment call about whether a leading pseudo-op belongs in the
registry: `cwd` is one, `_MAIN_LEVEL_OPS` exists for exactly that category, and
its own comment says "They belong in any list a caller reads." `repo` was
simply never added to it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((REPO_ROOT / ".supertool.json").read_text(encoding="utf-8"))


@pytest.fixture
def shipped_config(monkeypatch: pytest.MonkeyPatch):
    """conftest hands every test ``_CONFIG = {}`` (#1030).

    Without this the listing takes its no-config fallback and `op_help` calls
    every op undocumented — so the test would go red for the isolation, not for
    the defect, and stay red after the fix.
    """
    cfg = json.loads((REPO_ROOT / ".supertool.json").read_text(encoding="utf-8"))
    supertool._merge_presets(cfg, str(REPO_ROOT))
    monkeypatch.setattr(supertool, "_CONFIG", cfg)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", str(REPO_ROOT / ".supertool.json"))
    return cfg


def test_repo_is_a_main_level_op_like_cwd():
    """Both are stripped by main() before dispatch; both are still real ops."""
    assert "cwd" in supertool._MAIN_LEVEL_OPS
    assert "repo" in supertool._MAIN_LEVEL_OPS


def test_valid_op_names_lists_repo():
    """The list the unknown-op message prints must not deny a shipped op."""
    assert "repo" in supertool._valid_op_names()


def test_unknown_op_message_offers_repo_as_a_valid_operation():
    tail = supertool._unknown_op_message("raed").split("Valid operations:", 1)[1]
    listed = {n.strip() for n in tail.splitlines()[0].split(",")}
    assert "repo" in listed


def test_config_declares_repo_next_to_cwd():
    entry = CONFIG["builtin-ops"].get("repo")
    assert entry is not None, "repo has no builtin-ops entry"
    assert entry["syntax"].startswith("repo:")
    assert entry.get("description")
    assert entry.get("example")


def test_ops_listing_renders_the_repo_op(shipped_config):
    """`ops` is the discovery surface; an op absent from it does not exist."""
    assert "repo:OWNER/NAME" in supertool.op_ops()


def test_roster_marks_repo_read_only_not_acts(shipped_config):
    """An undeclared safety class falls back to `acts` and renders `!`."""
    assert supertool._OP_SAFETY_BUILTIN.get("repo") == "read-only"


def test_help_repo_returns_a_reference_not_an_unknown_op(shipped_config):
    out = supertool.op_help("repo")
    assert "unknown" not in out.lower()
    assert "repo:OWNER/NAME" in out


def test_claims_registry_resolves_repo():
    """A doc citing `repo:OWNER/NAME` must read as a checked reference."""
    registry = {}
    for section in ("builtin-ops", "ops", "aliases"):
        entries = CONFIG.get(section)
        if isinstance(entries, dict):
            for name, spec in entries.items():
                if isinstance(spec, dict) and spec.get("syntax"):
                    registry[name] = spec["syntax"]
    assert "repo" in registry
