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
over the whole registry: of the 25 shipped ops whose `syntax` names a path,
**zero** carry `{file}` or `{dir}` in their `cmd`. So a `cmd`-supersedes-`syntax`
detector would have disarmed the gate for all 25 — the reason this file pins the
`syntax`-only shape as loudly as the `cmd`-only one.

**`{arg}` and `{args}` are deliberately not signals**, though `{arg}` substitutes
the very same `parts[1]` that `{file}` does. 20 shipped ops carry `{arg}`; 8 of
those name a path in `syntax` and are already held by the syntax detector,
leaving 12 that use it for a handle, a ref, a tag, an ID or a repo slug and take
no path. Promoting it would refuse those 12 and gate nothing. `{file}` and
`{dir}` are the placeholders whose NAME is the claim. The 20/8/12 split, and the
reason #1357's proposed `{arg}` lint was measured and not built, are in
`tests/test_arg_placeholder_and_paths_env_1357.py`.

Every figure in that paragraph is recomputed from that file's helpers and
checked against this docstring at the foot of this module, by
`TestTheArgSplitInThisDocstringIsPinnedToTheLiveCount` — because prose and a
green assertion one file away disagreed here for two releases (#1761). Do not
restate a superseded figure above: the pin refuses a second `N/N/N` triple in
this docstring, which keeps the paragraph a single current claim rather than a
history. The history is in the file the paragraph names.

#1351 rides here because it is the same sentence from the other end: the
detector's docstring held up `gl-api` as the worked example of a declared op,
and `gl-api` was in `_UNDECLARED_PATH_OPS` — the grandfather register the
docstring describes emptying. Declared for real rather than re-cited, so the
register shrinks 20 -> 19 and the example is true instead of accurate.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

import supertool
from test_arg_placeholder_and_paths_env_1357 import (  # the split's owner
    _arg_ops, _path_shaped_arg_ops)

_ROOT = Path(__file__).resolve().parent.parent

_MODULE_DOC = __doc__ or ""

# Line breaks in a docstring are a wrapping decision, not a claim. The #1761
# pin matches against this flattened copy so re-flowing a paragraph never
# reddens it — only a figure changing does.
_MODULE_DOC_FLAT = re.sub(r"\s+", " ", _MODULE_DOC)


@pytest.fixture(autouse=True)
def _no_optout(monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest sets the env opt-out suite-wide; containment tests must not.

    Nothing asserted below depends on it — the undeclared refusal is emitted
    before any opt-out is consulted, which is the point of it being a refusal
    rather than a containment check. Cleared anyway so a test added here later
    cannot go quietly vacuous.
    """
    monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)


def _preset_registry() -> Dict[str, Dict[str, Any]]:
    """Every op the shipped preset manifests define."""
    ops: Dict[str, Dict[str, Any]] = {}
    for manifest in sorted((_ROOT / "presets").glob("*.json")):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for name, entry in data.get("ops", {}).items():
            if isinstance(entry, dict):
                ops[name] = entry
    return ops


def _registry() -> Dict[str, Dict[str, Any]]:
    """Every op this repository ships, preset manifests AND its own config.

    `.supertool.json` is included on purpose: #1350's only live instance
    (`oss_train`) lived there until #1472 deleted it, and a register that
    cannot see the instance it was written for is this repo's standing defect
    wearing a test's clothes. It is still merged in rather than dropped — the
    scope is the claim, and a project op added tomorrow has to be walked on
    the day it lands.

    **Merged key-by-key for a dict-over-dict collision, exactly as
    `_merge_presets` does it**, not overwritten. Three entries in
    `.supertool.json` — `dashboard`, `radar` and `git-diff` — are partial
    overrides carrying one extra config key and no `cmd` or `syntax` at all.
    A naive `ops[name] = entry` replaced the preset definition with the stub,
    so `git-diff` — which names a path and is in the grandfather register —
    fell out of every count and every audit below while the audit still
    reported a clean pass. That is this file's own subject matter committed
    inside the test written for it.
    """
    ops = _preset_registry()
    data = json.loads((_ROOT / ".supertool.json").read_text(encoding="utf-8"))
    for name, entry in data.get("ops", {}).items():
        if not isinstance(entry, dict):
            continue
        base = ops.get(name)
        if isinstance(base, dict):
            merged = dict(base)
            merged.update(entry)
            ops[name] = merged
        else:
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
        """`cmd` must ADD a signal, never replace one. All 25 currently-gated
        shipped ops are this shape, so a supersede would disarm every one.

        That 25 is the `syntax` detector's population and the assertion below
        counts `_entry_names_a_path`, the OR of syntax and cmd. They agree only
        because the cmd detector currently matches zero of the 87 shipped ops;
        the first op shipped with `{file}` in its `cmd` makes them diverge and
        only the assertion moves. Do not collapse the two."""
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
        """**Zero** ops in the whole registry are detected by `cmd` and not
        `syntax`, since #1472 deleted `oss_train` — which was the one.

        An empty population is still worth asserting: it is what turns the
        *next* cmd-only op into a red suite rather than a silent extension of
        the ungated set. What it is not is coverage. A register whose set is
        empty cannot exercise the arm it counts, so the arm's live instance is
        the fixture in `TestTheCmdArmHasALiveInstanceOfItsOwn` below, which
        drives a real `.supertool.json` through `dispatch()`. Said here rather
        than left implicit: a zero meaning "none ship" must not be read as
        "the arm is covered by this file's registry walk"."""
        cmd_only = sorted(
            name for name, entry in _registry().items()
            if supertool._cmd_names_a_path(entry.get("cmd", "")) is not None
            and not supertool._syntax_names_a_path(entry.get("syntax", ""))
        )
        assert cmd_only == [], cmd_only

    def test_the_register_header_comment_counts_are_the_registry_counts(
            self) -> None:
        """`_UNDECLARED_PATH_OPS`'s header comment states "24 shipped preset
        ops name a path, 5 declare a boundary, these 19 do not".

        Pinned because it drifted the moment the register shrank: the comment
        kept saying 4/20 while the frozenset six lines below it held 19, so one
        block asserted two counts for the same set. A number in prose with no
        test under it is folklore.

        Both scopes are asserted, and since #1472 they agree: the register is
        a statement about `presets/*.json`, and this repo's own
        `.supertool.json` no longer adds a path-naming op of its own. The
        wider scope is still walked rather than dropped — the two numbers
        diverging again is exactly the event worth catching, and a scope
        nobody computes cannot report one.
        """
        presets = _preset_registry()
        named = [n for n, e in presets.items()
                 if supertool._entry_names_a_path(e) is not None]
        declared = [n for n in named if "paths" in presets[n]]
        # #1739 added `gh-pr-edit`, which DECLARES a boundary, so the named
        # and declared counts each moved by one and the register did not.
        # #2031/#2032 add `slack_publish`, which also DECLARES a boundary
        # (`"paths": {"args": []}` — the `gl-api` precedent: a pipe-joined
        # blob syntax where naming an argument position would validate the
        # wrong thing, and the `file://` half is bounded by
        # `_publish_safety.safe_resolve_body_path`'s own allowlist instead).
        # #2046 adds `classify`, the same `{"args": []}` shape again: its
        # syntax names PATH (`TEXT_OR_file://PATH`) and it declares a
        # boundary of its own inside `check.py::resolve_text` rather than
        # through a `parts` index. Named and declared each moved by one, the
        # 19-strong undeclared register did not.
        # #2078 adds `gh-issue-comment`, which DECLARES a boundary
        # (`"paths": {"args": [2], "root": "cwd"}` over its payload argument,
        # the same shape `gh-pr-edit` uses). Named and declared each moved by
        # one again, the 19-strong undeclared register still did not.
        # #1796 adds `:artifact:PATH` to both `gh-job` and `gl-job`'s
        # `syntax`, so both now name a path per the detector; both DECLARE a
        # boundary (`"paths": {"args": []}` — the `gl-api` precedent, since
        # PATH there names an artifact's own path or a GitLab API route
        # rather than anything on this filesystem). Named and declared each
        # moved by two, the 19-strong undeclared register still did not. The
        # register's own header comment in `_supertool.py` now reads "30
        # shipped PRESET ops name a path, 11 declare a boundary, these 19 do
        # not" — these four numbers are the only thing under it.
        assert len(named) == 30, sorted(named)
        assert sorted(declared) == [
            "claims", "classify", "gh-issue-comment", "gh-job", "gh-pr-edit",
            "gl-api", "gl-job", "slack_publish", "xml", "xml_attr",
            "xml_count"], sorted(declared)
        assert len(supertool._UNDECLARED_PATH_OPS) == 19

        whole = _registry()
        named_all = [n for n, e in whole.items()
                     if supertool._entry_names_a_path(e) is not None]
        declared_all = [n for n in named_all if "paths" in whole[n]]
        assert len(named_all) == 30, sorted(named_all)
        assert sorted(declared_all) == [
            "claims", "classify", "gh-issue-comment", "gh-job", "gh-pr-edit",
            "gl-api", "gl-job", "slack_publish", "xml", "xml_attr",
            "xml_count"], sorted(declared_all)

    def test_this_repo_ships_no_project_only_path_naming_op(self) -> None:
        """`oss_train` was the one, and #1472 deleted it.

        Asserted rather than left to the counts above: those compare lengths,
        and a project op that both appeared and displaced a preset op would
        keep them equal. This names the difference between the two scopes and
        pins it at empty."""
        project_only = sorted(set(_registry()) - set(_preset_registry()))
        assert project_only == [], project_only


#: A project op whose only path signal is the `{file}` in its `cmd`. It is
#: written to a real `.supertool.json` and dispatched, so it reaches the
#: detector by the same route a shipped project op does rather than by being
#: handed to `_entry_names_a_path` as a literal.
_FIXTURE_OP = "cmd_arm_probe"
_RAN = "CMD-ARM-FIXTURE-RAN-1472"


def _fixture_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                     **extra: Any) -> Path:
    """A project root declaring `_FIXTURE_OP`, loaded through `_load_config`."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "ran.py").write_text(
        "print('" + _RAN + "')" + chr(10), encoding="utf-8")
    entry: Dict[str, Any] = {"cmd": "{python} ran.py {file}", "timeout": 60}
    entry.update(extra)
    (root / ".supertool.json").write_text(
        json.dumps({"ops": {_FIXTURE_OP: entry}}), encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setattr(supertool, "_CONFIG", None)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
    monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
    return root


class TestTheCmdArmHasALiveInstanceOfItsOwn:
    """#1472 deleted `oss_train`, and it was this arm's only live instance.

    Everything above drives `_entry_names_a_path` and
    `_preset_path_containment` with hand-built dicts. That covers the
    function; it does not establish that a config on disk still *arrives* at
    it with the `cmd` signal intact — the merge, the loader and the dispatch
    all sit in between, and each of them has been the reason a gate did not
    fire before. With the last shipped instance gone, a break anywhere along
    that route would leave every assertion above green.

    So the fixture is a real `.supertool.json`, read by `_load_config()`, run
    through `supertool.dispatch()`. The reach is asserted rather than assumed:
    the same fixture, declared, actually executes its command and prints
    `_RAN`, which is the control that says the refusal below came from the
    detector and not from something earlier declining the op.
    """

    def test_the_fixture_reaches_the_op_when_it_declares(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The control, and it runs first for a reason.

        A fixture that is refused for some unrelated reason — an unparsed
        config, a mixed-tree decline, a name that never resolves — would make
        every refusal below pass while proving nothing about the detector.
        The op running end to end is the only evidence that the route is
        clear.
        """
        root = _fixture_project(tmp_path, monkeypatch,
                                paths={"args": [1], "root": "cwd"})
        (root / "inside.txt").write_text("x", encoding="utf-8")
        out = supertool.dispatch(_FIXTURE_OP + ":inside.txt")
        assert _RAN in out, out

    def test_the_loader_hands_the_detector_a_cmd_only_signal(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`cmd` fires, `syntax` does not — the arm under test, post-merge."""
        _fixture_project(tmp_path, monkeypatch)
        entry = supertool._load_config()["ops"][_FIXTURE_OP]
        assert supertool._cmd_names_a_path(entry.get("cmd", "")) == "{file}"
        assert not supertool._syntax_names_a_path(entry.get("syntax", ""))
        assert supertool._entry_names_a_path(entry) is not None

    def test_an_undeclared_cmd_only_op_is_refused_through_dispatch(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole gate: refused, named, and the command never launched."""
        _fixture_project(tmp_path, monkeypatch)
        out = supertool.dispatch(_FIXTURE_OP + ":/etc/passwd")
        assert "ERROR:" in out, out
        assert "{file}" in out and "cmd" in out, out
        assert '"paths"' in out, out
        assert _RAN not in out, out

    def test_the_declared_fixture_is_still_bounded(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Declaring is not exempting — the boundary is then enforced.

        `_resolve_custom_op` with an argv list rather than `dispatch()` with a
        colon string, and only here: an absolute Windows path carries a drive
        colon, which `dispatch` splits on. #1247 is that bug, and a test that
        walks into it fails on four legs for a reason that has nothing to do
        with containment. The two tests above pass a colon-free argument and
        keep the full route.
        """
        root = _fixture_project(tmp_path, monkeypatch,
                                paths={"args": [1], "root": "cwd"})
        outside = root.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        out = supertool._resolve_custom_op(
            _FIXTURE_OP, [_FIXTURE_OP, str(outside)])
        assert out is not None
        assert "ERROR:" in out and "escapes" in out, out
        assert _RAN not in out, out


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


class TestTheArgSplitInThisDocstringIsPinnedToTheLiveCount:
    """#1761 — the `{arg}` split above is joined to the count it quotes.

    It read `24/8/16` from #873 until #1761, through #1715 moving the figure a
    second time, while `test_arg_placeholder_and_paths_env_1357.py` asserted
    the real one green on every run one file away. Nothing joined the two, so
    the number CI checks and the number a reader consults before deciding
    whether the `{arg}` lint is worth building could disagree indefinitely.
    This is the join, and it recomputes through that file's own helpers rather
    than restating them — a second copy of the measurement is the defect.
    """

    @staticmethod
    def _live() -> Tuple[int, int, int]:
        arg_ops = _arg_ops()
        path_shaped = _path_shaped_arg_ops()
        assert arg_ops, "no shipped op carries {arg} — re-measure, do not pass"
        assert path_shaped, "no {arg} op names a path — re-measure"
        return len(arg_ops), len(path_shaped), len(arg_ops) - len(path_shaped)

    def test_the_docstring_carries_exactly_one_split_and_it_is_current(
            self) -> None:
        total, path_shaped, rest = self._live()
        live = "%d/%d/%d" % (total, path_shaped, rest)
        found = sorted(set(re.findall(r"\b\d+/\d+/\d+\b", _MODULE_DOC)))
        assert found == [live], (
            "this module's docstring quotes %s; the live split computed from "
            "the shipped manifests is %s. Exactly one triple is allowed here, "
            "so a superseded figure belongs in the history the paragraph "
            "points at, not above it (#1761)." % (found or "no split", live))

    def test_every_figure_in_the_sentence_is_the_live_one(self) -> None:
        """The triple is not the only place the numbers are spelled out.

        Each phrase is built from the computed figure, so a docstring that
        merely dropped a number fails here rather than passing quietly.
        """
        total, path_shaped, rest = self._live()
        for phrase in (
                "%d shipped ops carry `{arg}`" % total,
                "%d of those name a path" % path_shaped,
                "leaving %d that use it" % rest,
                "refuse those %d and gate nothing" % rest):
            assert phrase in _MODULE_DOC_FLAT, (
                "this module's docstring no longer contains %r. If you "
                "reworded the sentence, update this pin; if the figure moved, "
                "the docstring is stale and the number here is the live one "
                "(#1761)." % phrase)

    def test_the_contributing_doc_quotes_the_same_live_figures(self) -> None:
        """The third copy, and it had drifted to a third value.

        `docs/contributing.md` restated the split in words rather than as a
        slash triple, so the grep that found the other two could not see it:
        it read 21/8/13 while the docstring read 24/8/16 and the assertion
        read the truth. Both populations it quotes are pinned here — the
        `{arg}` split and the syntax detector's own count — because the doc
        is the copy a contributor reads before writing an op.
        """
        total, path_shaped, rest = self._live()
        syntax_named = [name for name, entry in _registry().items()
                        if supertool._syntax_names_a_path(
                            entry.get("syntax", ""))]
        assert syntax_named, "the syntax detector finds nothing — re-measure"
        doc = (_ROOT / "docs" / "contributing.md").read_text(encoding="utf-8")
        flat = re.sub(r"\s+", " ", doc)
        for phrase in (
                "%d shipped ops carry `{arg}`" % total,
                "%d of them name a path in `syntax`" % path_shaped,
                "leaving %d that pass a handle" % rest,
                "refuse those %d and gate nothing" % rest,
                "of the %d shipped ops the `syntax` detector finds"
                % len(syntax_named)):
            assert phrase in flat, (
                "docs/contributing.md no longer contains %r. If you reworded "
                "the sentence, update this pin; if the figure moved, the doc "
                "is stale and the number here is the live one (#1761)."
                % phrase)
