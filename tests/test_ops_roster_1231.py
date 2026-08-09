"""#1231 — a names+safety roster that fits the SessionStart cap.

`ops` is 47,254 bytes and `ops-compact` is 9,067 against a ~7,168-byte cap, so
the startup listing is truncated *today* and everything alphabetically after
`grep` is hidden — the whole `gh-*` and `git-*` families, `radar`, `watch`.
What was lost was **existence**, and a reader cannot miss what they do not know
to look for.

A roster of every name fits in ~1KB. But a name alone is only actionable for an
op you may probe: `between:FILE:747:820` teaches its own signature from its
error, while `oss_train` force-pushes a merge train and `gh-pr-merge` merges.
So each name carries a safety class, and the classes fail *loud*: an op with no
declared class renders `!` (acts), never unmarked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import supertool


REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture
def shipped_config(monkeypatch: pytest.MonkeyPatch):
    """The repo's own .supertool.json — conftest hands tests `_CONFIG = {}`."""
    cfg = json.loads((REPO_ROOT / ".supertool.json").read_text(encoding="utf-8"))
    supertool._merge_presets(cfg, str(REPO_ROOT))
    monkeypatch.setattr(supertool, "_CONFIG", cfg)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", str(REPO_ROOT / ".supertool.json"))
    return cfg


def _roster_entries(out: str) -> dict:
    """name -> marker ("", "*", "!") parsed from the roster body lines."""
    entries = {}
    for line in out.splitlines():
        if not line.startswith("  "):
            continue
        for tok in line.split():
            name = tok.rstrip("*!")
            marker = tok[len(name):]
            if name:
                entries[name] = marker
    return entries


# --- the roster exists and is complete -------------------------------------

def test_roster_lists_every_dispatchable_op(shipped_config) -> None:
    """Completeness is the whole point — nothing may be hidden."""
    out = supertool.op_ops_roster()
    entries = _roster_entries(out)
    expected = set(supertool._valid_op_names())
    expected |= {n for n, i in (shipped_config.get("ops") or {}).items()
                 if isinstance(i, dict) and i.get("status", 1)}
    missing = expected - set(entries)
    assert not missing, f"roster hides dispatchable ops: {sorted(missing)}"


def test_roster_fits_well_inside_the_session_start_cap(shipped_config) -> None:
    """The pin. This is the reason the issue exists."""
    size = len(supertool.op_ops_roster().encode("utf-8"))
    budget = supertool._HOOK_OUTPUT_CAP_BYTES // 2
    assert size < budget, f"roster is {size} bytes, budget {budget}"


def test_roster_is_flat_and_alphabetical(shipped_config) -> None:
    """Neighbour-scanning is the discovery affordance: gh-pr-create beside gh-pr."""
    names = list(_roster_entries(supertool.op_ops_roster()))
    assert names == sorted(names)
    assert "gh-pr" in names and "gh-pr-create" in names
    assert abs(names.index("gh-pr") - names.index("gh-pr-create")) <= 2


# --- the safety class ------------------------------------------------------

@pytest.mark.parametrize("name", ["gh-pr-merge", "git-push", "oss_train", "radar",
                                  "watch", "unwatch", "mcp_daemon", "mcp_stop"])
def test_acting_ops_are_marked_acts(shipped_config, name: str) -> None:
    """Rendering an acting op as probe-safe invites someone to probe oss_train."""
    assert _roster_entries(supertool.op_ops_roster()).get(name) == "!"


@pytest.mark.parametrize("name", ["edit", "paste", "append", "replace",
                                  "replace_lines", "vim", "batch", "gc",
                                  "format", "format_staged", "rename"])
def test_writing_builtins_are_marked_writes(shipped_config, name: str) -> None:
    assert _roster_entries(supertool.op_ops_roster()).get(name) == "*"


@pytest.mark.parametrize("name", ["read", "grep", "glob", "between", "map",
                                  "tree", "help", "gh-issue", "git-status"])
def test_reading_ops_are_unmarked(shipped_config, name: str) -> None:
    assert _roster_entries(supertool.op_ops_roster()).get(name) == ""


def test_undeclared_config_op_defaults_to_acts(tmp_path: Path, monkeypatch) -> None:
    """The failure direction is the design: no class means the loudest class."""
    monkeypatch.setattr(supertool, "_CONFIG", {
        "ops": {"mystery": {"cmd": "true", "timeout": 5, "syntax": "mystery"}}})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    assert _roster_entries(supertool.op_ops_roster()).get("mystery") == "!"


def test_config_cannot_downgrade_a_builtin(tmp_path: Path, monkeypatch) -> None:
    """A built-in's class is a fact about the binary, not about a project file."""
    monkeypatch.setattr(supertool, "_CONFIG", {
        "ops": {"edit": {"cmd": "true", "timeout": 5, "safety": "read-only"}}})
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    assert _roster_entries(supertool.op_ops_roster()).get("edit") == "*"


def test_legend_names_all_three_states(shipped_config) -> None:
    out = supertool.op_ops_roster()
    assert "read-only" in out
    assert "help:OP" in out
    for marker in ("`*`", "`!`"):
        assert marker in out


# --- every shipped op declares its class -----------------------------------

def test_every_shipped_preset_op_declares_a_safety_class() -> None:
    """Anti-rot. A new preset op without a class fails here, not in a session."""
    undeclared = []
    for manifest in sorted((REPO_ROOT / "presets").glob("*.json")):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for name, info in (data.get("ops") or {}).items():
            if not isinstance(info, dict):
                continue
            if info.get("safety") not in supertool._SAFETY_CLASSES:
                undeclared.append(f"{manifest.name}:{name}")
    assert not undeclared, f"preset ops with no safety key: {undeclared}"


def test_every_builtin_op_has_a_class_in_the_binary() -> None:
    missing = sorted(set(supertool._valid_op_names()) - set(supertool._OP_SAFETY_BUILTIN))
    assert not missing, f"built-ins with no declared safety class: {missing}"


def test_every_op_this_repo_loads_declares_a_safety_class(shipped_config) -> None:
    """Merged view: preset entries deep-merge under project overrides, so a
    partial override (`radar`, `git-diff`) inherits the preset's class and only
    a genuinely project-only op (`oss_train`) must declare its own."""
    undeclared = [n for n, i in (shipped_config.get("ops") or {}).items()
                  if isinstance(i, dict) and n not in supertool._OP_SAFETY_BUILTIN
                  and i.get("safety") not in supertool._SAFETY_CLASSES]
    assert not undeclared, f"ops loaded here with no safety key: {undeclared}"


# --- ops:ARG is no longer discarded ----------------------------------------

def test_ops_roster_dispatches(shipped_config) -> None:
    out = supertool.dispatch("ops:roster")
    assert "gh-pr-merge!" in out


def test_ops_with_an_op_name_argument_is_refused_and_points_at_help(shipped_config) -> None:
    """`ops:gh-labels` printed the whole 47KB listing and said nothing (#1231)."""
    out = supertool.dispatch("ops:gh-labels")
    assert "ERROR" in out
    assert "help:gh-labels" in out
    assert "## Operations" not in out


def test_ops_with_an_unknown_argument_is_refused(shipped_config) -> None:
    out = supertool.dispatch("ops:zzzz")
    assert "ERROR" in out
    assert "roster" in out
    assert "## Operations" not in out


def test_bare_ops_still_renders_the_full_listing(shipped_config) -> None:
    assert "## Operations" in supertool.dispatch("ops")

# --- the hook the whole issue is about -------------------------------------

def test_session_start_hook_asks_for_the_roster() -> None:
    """The pin that makes the fix real rather than available.

    `ops:roster` existing changes nothing on its own — the defect was what the
    SessionStart hook prints, and it printed `ops-compact` (9,067 bytes against
    a ~7,168 cap) on every session.
    """
    hook = (REPO_ROOT / "hooks" / "session-start.sh").read_text(encoding="utf-8")
    invocations = [ln for ln in hook.splitlines()
                   if ln.strip().startswith("python3 ") and "$BIN" in ln]
    assert invocations, "hook no longer invokes supertool"
    assert all("ops:roster" in ln for ln in invocations), invocations
    assert not any("ops-compact" in ln for ln in invocations), invocations


def test_whole_hook_payload_fits_the_cap(shipped_config) -> None:
    """introduction + output-format + roster, measured together."""
    payload = (supertool.op_introduction()
               + supertool.op_output_format()
               + supertool.op_ops_roster())
    size = len(payload.encode("utf-8"))
    assert size < supertool._HOOK_OUTPUT_CAP_BYTES, f"{size} bytes"

# --- review follow-ups (Sonnet pass over 012e53c) --------------------------

def test_ops_compact_also_refuses_an_argument(shipped_config) -> None:
    """`ops-compact:gh-labels` swallowed its argument exactly as `ops` did.

    The first pass fixed the branch above it and left this one, which is the
    same defect one `elif` over — and it is the form the hook used to call.
    """
    out = supertool.dispatch("ops-compact:gh-labels")
    assert "ERROR" in out
    assert "help:gh-labels" in out
    assert "## Operations" not in out


def test_bare_ops_compact_still_renders_the_compact_listing(shipped_config) -> None:
    assert "## Operations" in supertool.dispatch("ops-compact")


@pytest.mark.parametrize("manifest,name", [
    ("bluesky.json", "bluesky_status_since"),
    ("devto.json", "devto_status_since"),
    ("hashnode.json", "hashnode_status_since"),
])
def test_watermark_briefings_are_acts_not_read_only(manifest: str, name: str) -> None:
    """`*_status_since` writes `~/.config/<service>/last_check` on success.

    Probing one to learn its signature silently advances the watermark, so the
    next real briefing reports nothing for the window that was consumed — an
    absence the probe produced, read as an absence in the world. Outside the
    tree, and the reason the class exists.
    """
    data = json.loads((REPO_ROOT / "presets" / manifest).read_text(encoding="utf-8"))
    assert data["ops"][name]["safety"] == "acts"


_FIGURE_BEARING = (
    "_supertool.py", "README.md", "hooks/session-start.sh",
    "docs/operations/meta.md", "changelog.d/1231.added.md",
    "tests/test_ops_roster_1231.py",
)


def test_quoted_byte_figures_are_this_checkouts(shipped_config) -> None:
    """A rule with its evidence stripped is folklore, and a *wrong* number is
    worse than none. The first pass copied the issue body's figures for `ops`
    and `ops-compact` rather than measuring this checkout, and every doc
    inherited them. This file is itself in the swept set."""
    roster = f"{len(supertool.op_ops_roster().encode('utf-8')):,}"
    for rel in _FIGURE_BEARING:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        # Assembled, not written literally — this file is in the swept set,
        # and a sweep that trips over its own pattern is not a sweep.
        for stale in ("47," + "260", "9," + "073"):
            assert stale not in text, f"{rel} still quotes {stale}"
    for rel in ("README.md", "hooks/session-start.sh", "docs/operations/meta.md"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert roster in text, f"{rel} does not quote the measured roster size"
