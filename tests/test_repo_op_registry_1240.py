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

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import supertool

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((REPO_ROOT / ".supertool.json").read_text(encoding="utf-8"))


def _load_claims_check():
    """`presets/claims/check.py`, loaded the way the other claims tests do."""
    path = REPO_ROOT / "presets" / "claims" / "check.py"
    spec = importlib.util.spec_from_file_location("_claims_check_1240", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO_ROOT / "presets"))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


_claims_check = _load_claims_check()


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


def test_claims_resolves_a_repo_reference_instead_of_declining_it():
    """A doc citing `repo:OWNER/NAME` must read as a checked reference.

    Through `claims`' own loader and its own op lens, not a re-implementation
    of either: a private copy of the registry walk would stay green if the real
    one broke, which is the shape of a test that passes when the code does
    nothing.
    """
    registry = _claims_check._load_registry(REPO_ROOT)
    assert "repo" in registry

    findings = _claims_check._op_findings(0, "repo:OWNER/NAME", registry)
    states = {f.state for f in findings}
    assert _claims_check.UNCHECKED not in states, (
        "claims still declines to check `repo:` — the op resolves to nothing "
        "in the registry, so every document citing it reads as unverifiable")
    assert states <= {_claims_check.HOLDS}, findings
