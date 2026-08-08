"""#1124 — `ops` is the tool's own answer to "what can you do?", and it lies by omission.

Six op names dispatch successfully and appear nowhere in the `ops` listing:
batch, check, introduction, ops, ops-compact, output-format. The load-bearing
one is **batch** — the only op that collapses N mutations into a single call,
which matters because #341 caps a call at one stdin payload, so N edits are
otherwise N calls. Measured across 232 agent transcripts: 1.53 ops/call, 70% of
calls carrying exactly one op, and 39% of those single-op calls were payload
mutations that batch is the only escape from. Every agent that did use `batch`
learned it from an external brief; an agent that asks the tool what it can do
never finds it.

Same defect class as #614 one layer out. #614 fixed the *error* path — the
unknown-op message derives its list from `_valid_op_names()` so it cannot rot —
and left the *listing* path deriving nothing. `op_help` inherited the same rot:
its "it's a valid built-in" arm tests `_BUILTIN_OPS`, a shadowing blocklist, so
`help:batch` answers "no help for op: batch / Run 'ops' for the full list" —
denying the op exists, then pointing at a list that omits it.

Three states, not two: documented, accepted-but-undocumented, absent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import supertool


REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture
def shipped_config(monkeypatch: pytest.MonkeyPatch):
    """Load the repo's own .supertool.json.

    conftest deliberately hands every test ``_CONFIG = {}`` (#1030). Without
    this fixture ``op_ops`` takes its no-config fallback and ``op_help`` reports
    every op as undocumented — so a test of the *listing* would go red for the
    isolation rather than for the defect, and stay red after the fix. That is
    the mirror image of a test that passes when the code does nothing, and it
    cost this file one rewrite before the RED output was believable.
    """
    cfg = json.loads((REPO_ROOT / ".supertool.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(supertool, "_CONFIG", cfg)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", str(REPO_ROOT / ".supertool.json"))
    return cfg


def _declared_names(listing: str) -> set:
    """Op names the listing actually accounts for.

    Parsed from the structured surfaces only — the bulleted syntax entries and
    the undocumented-op disclosure line — never from free prose, so an op name
    that merely happens to appear inside somebody's description does not count
    as disclosure.
    """
    names = set()
    for line in listing.splitlines():
        m = re.match(r"- `([A-Za-z0-9_-]+)", line)
        if m:
            names.add(m.group(1))
        m = re.match(r"Also accepted[^:]*: (.+)", line)
        if m:
            names.update(n.strip() for n in m.group(1).split(","))
    return names


class TestOpsAccountsForEveryDispatchableOp:
    def test_shipped_listing_names_every_op_the_dispatcher_accepts(
        self, shipped_config: dict
    ) -> None:
        """The repo's own .supertool.json, through the real renderer."""
        listing = supertool.op_ops()
        missing = sorted(set(supertool._valid_op_names()) - _declared_names(listing))
        assert not missing, (
            f"`ops` omits {missing} — every one dispatches. A listing that "
            f"silently drops an op is an absence the tool produced being read "
            f"as an absence in the world (#614, #1124)."
        )

    def test_disclosure_is_derived_so_it_cannot_rot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config documenting one op must not make the other forty vanish.

        This is the property a hand-maintained list cannot hold: the next op
        added without a .supertool.json entry has to show up here by itself.
        """
        monkeypatch.chdir(tmp_path)
        cfg = tmp_path / ".supertool.json"
        cfg.write_text(
            json.dumps({"builtin-ops": {"read": {"syntax": "read:PATH"}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(supertool, "_CONFIG", json.loads(cfg.read_text(encoding="utf-8")))
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
        monkeypatch.setattr(supertool, "_CONFIG_PATH", str(cfg))
        listing = supertool.op_ops()
        missing = sorted(set(supertool._valid_op_names()) - _declared_names(listing))
        assert not missing, f"undocumented ops vanished from the listing: {missing}"

    def test_status_0_is_a_project_choice_the_disclosure_must_not_undo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An op a project deliberately hid stays hidden.

        The disclosure names what the *tool* omitted, never what the project
        chose to omit — the same line `_preset_disclosure` already draws. A
        first cut of this filtered on `status`, which called `map` straight back
        into a listing that had just suppressed it (test_meta_ops.py:116).
        """
        monkeypatch.chdir(tmp_path)
        cfg = tmp_path / ".supertool.json"
        cfg.write_text(json.dumps({"builtin-ops": {
            "read": {"syntax": "read:PATH", "status": 1},
            "map": {"syntax": "map:PATH", "status": 0},
        }}), encoding="utf-8")
        monkeypatch.setattr(supertool, "_CONFIG", json.loads(cfg.read_text(encoding="utf-8")))
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
        monkeypatch.setattr(supertool, "_CONFIG_PATH", str(cfg))
        assert "map" not in supertool.op_ops()

    def test_disclosure_is_one_line_not_a_second_listing(
        self, shipped_config: dict
    ) -> None:
        """Under the SessionStart cap the accounting must cost a line, not a page."""
        listing = supertool.op_ops(compact=True)
        disclosure = [ln for ln in listing.splitlines() if ln.startswith("Also accepted")]
        assert len(disclosure) == 1, (
            f"expected exactly one disclosure line, got {len(disclosure)}. "
            f"This repo's config leaves five ops undescribed, so zero means the "
            f"disclosure stopped being emitted and the bound `<= 1` would have "
            f"passed on that — the weak form of this assertion."
        )
        head = listing.encode("utf-8")[: supertool._HOOK_OUTPUT_CAP_BYTES]
        assert "Also accepted" in head.decode("utf-8", "ignore"), (
            "compact output is already over the hook cap and truncates from the "
            "tail — a disclosure below the cut is a disclosure nobody reads."
        )


class TestHelpDoesNotDenyADispatchableOp:
    @pytest.mark.parametrize("op", sorted(set(supertool._valid_op_names())))
    def test_help_never_reads_as_unknown_for_an_op_that_dispatches(self, op: str) -> None:
        out = supertool.op_help(op)
        assert "no help for op" not in out, (
            f"help:{op} denies an op the dispatcher accepts. op_help gates its "
            f"'valid built-in' arm on _BUILTIN_OPS, which is a shadowing "
            f"blocklist and not a capability list."
        )


class TestBatchIsReachableFromTheTool:
    def test_batch_has_a_real_reference(self, shipped_config: dict) -> None:
        out = supertool.op_help("batch")
        assert "ERROR" not in out, out
        assert "batch:@" in out, "reference must show the @payload route"
        assert "ops" in out.lower(), (
            "a batch reference that never mentions the ops array does not tell "
            "the caller it collapses N ops into one call — which is its entire "
            "reason to exist (#1124)."
        )

    def test_the_shape_the_reference_teaches_actually_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The claim the listing makes has to be true, or it is worse than silence.

        Relative paths, from inside tmp_path, on purpose: ops are colon-split,
        and an absolute Windows path puts a drive-letter colon into
        ``batch:@C:/...`` — a green here on macOS would say nothing about the
        four Windows legs.
        """
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "sample.txt"
        target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        (tmp_path / "ops.json").write_text(json.dumps([
            {"op": "edit", "path": "sample.txt", "old": "alpha", "new": "ALPHA"},
            {"op": "edit", "path": "sample.txt", "old": "gamma", "new": "GAMMA"},
        ]), encoding="utf-8")
        supertool.dispatch("batch:@ops.json")
        assert target.read_text(encoding="utf-8") == "ALPHA\nbeta\nGAMMA\n", (
            "two mutations in one call is the property the listing will now "
            "advertise; if it does not hold, do not advertise it."
        )


class TestBatchEntryIsInTheShippedConfig:
    def test_supertool_json_carries_a_batch_entry(self) -> None:
        cfg = json.loads((REPO_ROOT / ".supertool.json").read_text(encoding="utf-8"))
        entry = cfg.get("builtin-ops", {}).get("batch")
        assert isinstance(entry, dict), "batch has no builtin-ops entry"
        assert entry.get("syntax"), "batch entry has no syntax"
        assert entry.get("example"), "batch entry has no example"
