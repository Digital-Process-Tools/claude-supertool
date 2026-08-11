"""#1350 / #1351 — the containment detector read `syntax` and nothing else.

#1287 put one gate in front of every preset and custom op: an op that names a
path and declares no boundary is **refused, not skipped**, because a path
argument reaching no check is an unchecked read rather than a check that could
not run.

Its detector was `_syntax_names_a_path(entry["syntax"])` alone. An op whose
`cmd` template substitutes the core's own `{file}` / `{dir}` placeholders but
whose `syntax` names no path — or which carries no `syntax` key at all — took
the `return None` arm. No declaration demanded, no check run, and the verdict
rendered identically to a declared-clean op. Two states where the rule needs
three, in the rule's own detector.

Measured on `v0.33.0..5f046a1`, driving the gate function directly so the
checkout's own `allow_outside_cwd` (#1353) cannot colour the result::

    gate verdict: None            # ("oss_train", entry, ["oss_train", "/etc/passwd"])
    syntax detector on empty: False

**The detector is now either signal, never one superseding the other.** Walked
over the whole registry: of the 24 shipped ops whose `syntax` names a path,
**zero** carry `{file}` or `{dir}` in their `cmd`. So a `cmd`-supersedes-`syntax`
detector would have disarmed the gate for all 24 — the reason this file pins the
`syntax`-only shape as loudly as the `cmd`-only one.

**`{arg}` and `{args}` are deliberately not signals**, though `{arg}` substitutes
the very same `parts[1]` that `{file}` does. Sixteen shipped ops use `{arg}` for
a handle, a ref, a tag, an ID or a repo slug and none of them takes a path;
promoting it would refuse all sixteen and gate nothing. `{file}` and `{dir}` are
the placeholders whose NAME is the claim.

#1351 rides here because it is the same sentence from the other end: the
detector's docstring held up `gl-api` as the worked example of a declared op,
and `gl-api` was in `_UNDECLARED_PATH_OPS` — the grandfather register the
docstring describes emptying. Declared for real rather than re-cited, so the
register shrinks 20 -> 19 and the example is true instead of accurate.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_optout(monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest sets the env opt-out suite-wide; containment tests must not.

    Nothing asserted below depends on it — the undeclared refusal is emitted
    before any opt-out is consulted, which is the point of it being a refusal
    rather than a containment check. Cleared anyway so a test added here later
    cannot go quietly vacuous.
    """
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)


def _registry() -> Dict[str, Dict[str, Any]]:
    """Every op this repository ships, preset manifests AND its own config.

    `.supertool.json` is included on purpose: #1350's only live instance
    (`oss_train`) lives there, and a register that cannot see the instance it
    was written for is this repo's standing defect wearing a test's clothes.
    """
    ops: Dict[str, Dict[str, Any]] = {}
    manifests = sorted((_ROOT / "presets").glob("*.json")) + [
        _ROOT / ".supertool.json"]
    for manifest in manifests:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for name, entry in data.get("ops", {}).items():
            if isinstance(entry, dict):
                ops[name] = entry
    return ops


class TestTheCmdTemplateIsADetectorSignal:

    @pytest.mark.parametrize("placeholder", ["{file}", "{dir}"])
    def test_a_cmd_placeholder_with_no_syntax_at_all_demands_a_declaration(
            self, placeholder: str) -> None:
        entry = {"cmd": "{python} scripts/x.py " + placeholder}
        assert supertool._entry_names_a_path(entry) is not None
        verdict = supertool._preset_path_containment(
            "probe", entry, ["probe", "/etc/passwd"])
        assert verdict is not None, "an unchecked path read verdicted as clean"
        assert verdict.startswith("ERROR:"), verdict
        assert '"paths"' in verdict, verdict

    def test_a_cmd_placeholder_beside_a_syntax_that_names_no_path(self) -> None:
        """The other half of the arm: `syntax` present and silent about paths."""
        entry = {"cmd": "{python} x.py {file}", "syntax": "probe:NAME[:full]"}
        verdict = supertool._preset_path_containment(
            "probe", entry, ["probe", "/etc/passwd"])
        assert verdict is not None and verdict.startswith("ERROR:"), verdict

    def test_the_refusal_names_the_cmd_template_as_the_signal(self) -> None:
        """An op with no `syntax` cannot be told it "names a path in its
        syntax ()" — the refusal has to name the signal that actually fired,
        or the author reads it as a bug in the guard."""
        verdict = supertool._preset_path_containment(
            "probe", {"cmd": "{python} x.py {file}"}, ["probe", "/etc/passwd"])
        assert verdict is not None
        assert "{file}" in verdict, verdict
        assert "cmd" in verdict, verdict
        assert "in its syntax ()" not in verdict, verdict

    def test_a_path_naming_syntax_still_fires_with_no_cmd_placeholder(
            self) -> None:
        """`cmd` must ADD a signal, never replace one. All 24 currently-gated
        shipped ops are this shape, so a supersede would disarm every one."""
        entry = {"cmd": "{python} x.py {args}", "syntax": "probe:PATH"}
        assert supertool._entry_names_a_path(entry) is not None
        verdict = supertool._preset_path_containment(
            "probe", entry, ["probe", "/etc/passwd"])
        assert verdict is not None and verdict.startswith("ERROR:"), verdict

    @pytest.mark.parametrize("placeholder", ["{arg}", "{args}", "{argjoin}"])
    def test_the_generic_placeholders_are_not_path_signals(
            self, placeholder: str) -> None:
        """Sixteen shipped ops pass a handle/ref/tag/ID through `{arg}`."""
        entry = {"cmd": "{python} x.py " + placeholder,
                 "syntax": "probe:HANDLE"}
        assert supertool._entry_names_a_path(entry) is None
        assert supertool._preset_path_containment(
            "probe", entry, ["probe", "someone"]) is None

    def test_a_declaration_still_wins_over_the_new_signal(self) -> None:
        """A `{file}` op that HAS declared is gated by its declaration, not
        refused for the placeholder."""
        entry = {"cmd": "{python} x.py {file}", "paths": {"args": []}}
        assert supertool._preset_path_containment(
            "probe", entry, ["probe", "/etc/passwd"]) is None


class TestTheShippedRegistryIsFullyDetected:
    """#1350 asks for the count: one is a bug, several is the detector.

    Walked, not grepped — a zero here means the registry was enumerated.
    """

    def test_no_shipped_op_names_a_path_without_declaring_or_grandfathering(
            self) -> None:
        missing = sorted(
            name for name, entry in _registry().items()
            if supertool._entry_names_a_path(entry) is not None
            and "paths" not in entry
            and name not in supertool._UNDECLARED_PATH_OPS
        )
        assert missing == [], (
            "these ops name a path and declare no containment boundary: "
            + ", ".join(missing))

    def test_the_cmd_signal_population_is_recorded(self) -> None:
        """One op in the whole registry is detected by `cmd` and not `syntax`:
        `oss_train` in `.supertool.json`. A count of one is a bug; this
        assertion is what turns a second one into a red suite rather than a
        silent extension of the ungated set."""
        cmd_only = sorted(
            name for name, entry in _registry().items()
            if supertool._cmd_names_a_path(entry.get("cmd", "")) is not None
            and not supertool._syntax_names_a_path(entry.get("syntax", ""))
        )
        assert cmd_only == ["oss_train"], cmd_only

    def test_oss_train_declares_that_its_argument_is_not_a_filesystem_path(
            self) -> None:
        """It takes worktree NAMES under `wt_root`, and #1246 refuses a
        separator or an absolute target inside the script. That boundary is
        neither `cwd` nor `repo`, so the honest declaration is the empty one —
        a claim someone made, not a default nobody noticed."""
        assert _registry()["oss_train"]["paths"] == {"args": []}


class TestGlApiDeclaresRatherThanBeingGrandfathered:
    """#1351 — the docstring's worked example was a member of the register."""

    def test_gl_api_carries_a_real_declaration(self) -> None:
        assert _registry()["gl-api"]["paths"] == {"args": []}

    def test_gl_api_is_no_longer_grandfathered(self) -> None:
        assert "gl-api" not in supertool._UNDECLARED_PATH_OPS

    def test_the_register_shrank_rather_than_the_docstring_being_reworded(
            self) -> None:
        assert len(supertool._UNDECLARED_PATH_OPS) == 19

    def test_every_op_the_detector_docstring_cites_as_declared_is_declared(
            self) -> None:
        """The docstring is what the next author reads to decide whether their
        op needs a declaration. Any op name it holds up as the safe pattern is
        checked against the shipped registry here."""
        doc = supertool._syntax_names_a_path.__doc__ or ""
        registry = _registry()
        cited = [name for name in registry if "`" + name + "`" in doc]
        assert cited, doc
        undeclared = sorted(n for n in cited if "paths" not in registry[n])
        assert undeclared == [], (
            "the detector docstring cites these as declared and they are not: "
            + ", ".join(undeclared))
