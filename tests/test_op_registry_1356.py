"""#1356 — the merged op registry has one implementation, and it is the product's.

`dashboard`, `radar` and `git-diff` are defined twice: once in a shipped
`presets/*.json` with `cmd`/`syntax`/`description`, and once in this repo's
`.supertool.json` as a **partial** override carrying one extra config key and
nothing else. `_merge_presets` merges those key-by-key. Every hand-rolled walk
that wrote the obvious `ops[name] = entry` replaced the shipped definition with
the stub instead.

Measured on this repo at the time of filing:

    naive walk, path-naming ops:  23      merged walk:  24
    the one that vanished:        git-diff

`git-diff` names a path and sits in `_UNDECLARED_PATH_OPS`, so the #1350
containment audit ran over a population that was missing exactly the op the
register was about — and printed a pass. The total op count is identical either
way (88 vs 88): the entry does not disappear, it becomes a stub with no
`syntax` and no `cmd`, so it drops out of every *filtered* population
downstream. A short list and a complete one render identically.

So the rule lives in one function that both the loader and the render call, and
the render says out loud when its own population may be short.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent
_BACKSLASH = chr(92)


@pytest.fixture
def fresh_config(monkeypatch: pytest.MonkeyPatch):
    """Load a .supertool.json written into tmp_path, through the real loader."""

    def _load(tmp_path: Path, config: Dict[str, Any]) -> Dict[str, Any]:
        (tmp_path / ".supertool.json").write_text(
            json.dumps(config), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(supertool, "_CONFIG", None)
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", False)
        monkeypatch.setattr(supertool, "_CONFIG_PATH", None)
        return supertool._load_config()

    return _load


def _preset(tmp_path: Path, name: str, ops: Dict[str, Any]) -> None:
    d = tmp_path / "presets"
    d.mkdir(exist_ok=True)
    (d / (name + ".json")).write_text(json.dumps({"ops": ops}), encoding="utf-8")


def _by_name(entries) -> Dict[str, Any]:
    return {e.name: e for e in entries}


class TestOneMergeRule:
    """`_merge_presets` must not carry its own private copy of the rule."""

    def test_the_rule_is_a_named_function(self) -> None:
        assert supertool._merge_op_def(
            {"cmd": "x", "syntax": "a:PATH"}, {"extra": 1}
        ) == {"cmd": "x", "syntax": "a:PATH", "extra": 1}

    def test_a_non_dict_override_replaces_wholesale(self) -> None:
        assert supertool._merge_op_def({"cmd": "x"}, "echo hi") == "echo hi"

    def test_a_non_dict_base_is_replaced_wholesale(self) -> None:
        assert supertool._merge_op_def("echo old", {"cmd": "x"}) == {"cmd": "x"}

    def test_the_loader_produces_what_the_rule_produces(
            self, tmp_path: Path, fresh_config) -> None:
        _preset(tmp_path, "p", {"widget": {"cmd": "c", "syntax": "widget:PATH"}})
        config = fresh_config(tmp_path, {
            "presets": ["p"], "ops": {"widget": {"tint": "blue"}}})
        assert config["ops"]["widget"] == {
            "cmd": "c", "syntax": "widget:PATH", "tint": "blue"}


class TestTheEffectiveRegistry:

    def test_a_partial_override_keeps_the_shipped_definition(
            self, tmp_path: Path, fresh_config) -> None:
        _preset(tmp_path, "p", {"widget": {"cmd": "c", "syntax": "widget:PATH"}})
        fresh_config(tmp_path, {
            "presets": ["p"], "ops": {"widget": {"tint": "blue"}}})
        entries, incomplete = supertool._op_registry()
        assert incomplete == []
        widget = _by_name(entries)["widget"]
        assert widget.definition["syntax"] == "widget:PATH"
        assert widget.definition["tint"] == "blue"

    def test_a_partial_override_is_marked_shadowed(
            self, tmp_path: Path, fresh_config) -> None:
        """The issue's second question: a shadowed shipped definition has to be
        visible, not merely correct."""
        _preset(tmp_path, "p", {"widget": {"cmd": "c", "syntax": "widget:PATH"}})
        fresh_config(tmp_path, {
            "presets": ["p"], "ops": {"widget": {"tint": "blue"}}})
        widget = _by_name(supertool._op_registry()[0])["widget"]
        assert widget.preset == "p"
        assert widget.project is True
        assert widget.overridden == ("tint",)

    def test_a_project_only_op_names_no_preset(
            self, tmp_path: Path, fresh_config) -> None:
        _preset(tmp_path, "p", {"widget": {"cmd": "c"}})
        fresh_config(tmp_path, {
            "presets": ["p"], "ops": {"solo": {"cmd": "s"}}})
        solo = _by_name(supertool._op_registry()[0])["solo"]
        assert solo.preset is None
        assert solo.project is True
        assert solo.overridden == ()

    def test_a_preset_only_op_names_no_project_override(
            self, tmp_path: Path, fresh_config) -> None:
        _preset(tmp_path, "p", {"widget": {"cmd": "c"}})
        fresh_config(tmp_path, {"presets": ["p"], "ops": {}})
        widget = _by_name(supertool._op_registry()[0])["widget"]
        assert widget.preset == "p"
        assert widget.project is False

    def test_a_dict_over_a_string_preset_def_is_not_a_key_merge(
            self, tmp_path: Path, fresh_config) -> None:
        """A preset may ship an op as a bare cmd string. A dict landing on one
        replaces it wholesale — `_merge_op_def` needs BOTH sides to be dicts —
        so there is no per-key answer to give, and claiming one would attribute
        keys to a preset definition that no longer exists."""
        _preset(tmp_path, "p", {"widget": "echo shipped"})
        fresh_config(tmp_path, {
            "presets": ["p"], "ops": {"widget": {"tint": "blue"}}})
        widget = _by_name(supertool._op_registry()[0])["widget"]
        assert widget.definition == {"tint": "blue"}
        assert widget.overridden is None, (
            "reported a key-by-key merge over a preset definition that was "
            "replaced outright")

    def test_an_override_that_changes_nothing_is_not_a_wholesale_replace(
            self, tmp_path: Path, fresh_config) -> None:
        """`()` and `None` are different answers and the render must not fuse
        them: an empty dict override leaves the preset definition entirely
        intact."""
        _preset(tmp_path, "p", {"widget": {"cmd": "c"}})
        fresh_config(tmp_path, {"presets": ["p"], "ops": {"widget": {}}})
        widget = _by_name(supertool._op_registry()[0])["widget"]
        assert widget.overridden == ()
        assert "replaced wholesale" not in supertool.op_registry()


class TestAWalkThatCannotEnumerateSaysSo:
    """The failure this exists to prevent: a short population, no marker."""

    def test_a_missing_preset_makes_the_population_incomplete(
            self, tmp_path: Path, fresh_config) -> None:
        _preset(tmp_path, "p", {"widget": {"cmd": "c"}})
        fresh_config(tmp_path, {"presets": ["p", "gone"], "ops": {}})
        entries, incomplete = supertool._op_registry()
        assert [e.name for e in entries] == ["widget"]
        assert incomplete, (
            "one of two declared presets did not load and the registry "
            "reported a complete-looking list of one")
        assert any("gone" in r for r in incomplete), incomplete

    def test_an_unreadable_preset_makes_the_population_incomplete(
            self, tmp_path: Path, fresh_config) -> None:
        (tmp_path / "presets").mkdir()
        (tmp_path / "presets" / "bad.json").write_text(
            "{not json", encoding="utf-8")
        fresh_config(tmp_path, {"presets": ["bad"], "ops": {}})
        _, incomplete = supertool._op_registry()
        assert any("bad" in r for r in incomplete), incomplete

    def test_a_malformed_presets_value_is_not_silence(
            self, tmp_path: Path, fresh_config) -> None:
        """`"presets": "p"` is truthy and not a list, so no preset op is ever
        merged. Reporting every remaining op as complete project attribution
        turns a config error into a registry that looks whole."""
        _preset(tmp_path, "p", {"widget": {"cmd": "c"}})
        fresh_config(tmp_path, {"presets": "p", "ops": {"solo": {"cmd": "s"}}})
        entries, incomplete = supertool._op_registry()
        assert [e.name for e in entries] == ["solo"]
        assert incomplete, (
            "a presets declaration that merged nothing reported a complete "
            "registry")

    def test_provenance_it_never_computed_is_unknown_not_absent(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A config carrying `presets` whose ops were never merged cannot know
        where anything came from. That is a third state, not project-only."""
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
        monkeypatch.setattr(supertool, "_CONFIG", {
            "presets": ["p"], "ops": {"widget": {"cmd": "c"}}})
        entries, incomplete = supertool._op_registry()
        assert incomplete, "sources were never recorded and nothing said so"
        assert _by_name(entries)["widget"].preset is None
        assert _by_name(entries)["widget"].project is False

    def test_no_presets_declared_is_a_fact_not_an_absence(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
        monkeypatch.setattr(supertool, "_CONFIG", {
            "ops": {"widget": {"cmd": "c"}}})
        entries, incomplete = supertool._op_registry()
        assert incomplete == []
        assert _by_name(entries)["widget"].project is True


class TestTheRender:

    def test_the_render_lists_every_op(
            self, tmp_path: Path, fresh_config) -> None:
        _preset(tmp_path, "p", {"widget": {"cmd": "c", "syntax": "widget:PATH"}})
        fresh_config(tmp_path, {
            "presets": ["p"], "ops": {"solo": {"cmd": "s"}}})
        out = supertool.op_registry()
        assert "widget" in out and "solo" in out

    def test_the_render_names_the_shadowed_ops(
            self, tmp_path: Path, fresh_config) -> None:
        _preset(tmp_path, "p", {"widget": {"cmd": "c"}})
        fresh_config(tmp_path, {
            "presets": ["p"], "ops": {"widget": {"tint": "blue"}}})
        out = supertool.op_registry()
        assert "shadowed" in out.lower()
        assert "tint" in out

    def test_incompleteness_is_in_the_body_not_only_on_stderr(
            self, tmp_path: Path, fresh_config) -> None:
        """`_preset_warnings` reach stderr from `main()`. A render whose own
        population is short must carry the marker itself — in a batched call
        stderr is somewhere else entirely, and a short list reads as complete.
        """
        _preset(tmp_path, "p", {"widget": {"cmd": "c"}})
        fresh_config(tmp_path, {"presets": ["p", "gone"], "ops": {}})
        out = supertool.op_registry()
        assert "INCOMPLETE" in out, out
        assert "gone" in out, out

    def test_a_complete_render_carries_no_incomplete_marker(
            self, tmp_path: Path, fresh_config) -> None:
        _preset(tmp_path, "p", {"widget": {"cmd": "c"}})
        fresh_config(tmp_path, {"presets": ["p"], "ops": {}})
        assert "INCOMPLETE" not in supertool.op_registry()

    def test_one_op_shows_per_key_provenance(
            self, tmp_path: Path, fresh_config) -> None:
        _preset(tmp_path, "p", {"widget": {"cmd": "c", "syntax": "widget:PATH"}})
        fresh_config(tmp_path, {
            "presets": ["p"], "ops": {"widget": {"tint": "blue"}}})
        out = supertool.op_registry("widget")
        # The key name alone proves nothing — the op of this render is the
        # source label beside it.
        rows = {line.split()[1]: " ".join(line.split()[2:])
                for line in out.splitlines() if line.startswith("- ")}
        assert rows == {"cmd": "preset p", "syntax": "preset p",
                        "tint": "project"}, out

    def test_an_unknown_name_is_refused_not_rendered_empty(
            self, tmp_path: Path, fresh_config) -> None:
        _preset(tmp_path, "p", {"widget": {"cmd": "c"}})
        fresh_config(tmp_path, {"presets": ["p"], "ops": {}})
        out = supertool.op_registry("nope")
        assert out.startswith("ERROR:"), out

    def test_the_render_holds_no_host_paths(
            self, tmp_path: Path, fresh_config) -> None:
        """Preset provenance is a name, never a host path — a Windows render
        must not differ from a POSIX one."""
        _preset(tmp_path, "p", {"widget": {"cmd": "c"}})
        fresh_config(tmp_path, {
            "presets": ["p"], "ops": {"widget": {"tint": "blue"}}})
        assert _BACKSLASH not in supertool.op_registry()
        assert _BACKSLASH not in supertool.op_registry("widget")
        assert str(tmp_path) not in supertool.op_registry()


class TestDispatch:

    def test_registry_dispatches(self, tmp_path: Path, fresh_config) -> None:
        _preset(tmp_path, "p", {"widget": {"cmd": "c"}})
        fresh_config(tmp_path, {"presets": ["p"], "ops": {}})
        assert "widget" in supertool.dispatch("registry")

    def test_a_bad_argument_is_refused(
            self, tmp_path: Path, fresh_config) -> None:
        _preset(tmp_path, "p", {"widget": {"cmd": "c"}})
        fresh_config(tmp_path, {"presets": ["p"], "ops": {}})
        assert "ERROR" in supertool.dispatch("registry:nope")

    def test_registry_is_read_only(self) -> None:
        assert supertool._OP_SAFETY_BUILTIN["registry"] == "read-only"

    def test_registry_is_a_known_op_name(self) -> None:
        assert "registry" in supertool._valid_op_names()

    def test_registry_is_documented_in_the_shipped_config(self) -> None:
        config = json.loads(
            (_ROOT / ".supertool.json").read_text(encoding="utf-8"))
        assert "registry" in config["builtin-ops"]


class TestThisRepoIsTheInstance:
    """The three live partial overrides, pinned against the shipped files."""

    def test_the_three_shadowed_ops_are_still_the_three(self) -> None:
        config = json.loads(
            (_ROOT / ".supertool.json").read_text(encoding="utf-8"))
        preset_ops: Dict[str, str] = {}
        for name in config["presets"]:
            data = json.loads(
                (_ROOT / "presets" / (name + ".json")).read_text(
                    encoding="utf-8"))
            for op_name in data.get("ops", {}):
                preset_ops[op_name] = name
        shadowed = sorted(n for n, e in config["ops"].items()
                          if isinstance(e, dict) and n in preset_ops)
        assert shadowed == ["dashboard", "git-diff", "radar"], shadowed

    def test_a_naive_walk_loses_git_diff_from_the_path_naming_set(self) -> None:
        """The measurement in this file's docstring, re-derived. Retire this
        test if the difference ever goes empty because the overrides changed —
        never by relaxing it."""
        config = json.loads(
            (_ROOT / ".supertool.json").read_text(encoding="utf-8"))
        base: Dict[str, Any] = {}
        for manifest in sorted((_ROOT / "presets").glob("*.json")):
            for n, e in json.loads(
                    manifest.read_text(encoding="utf-8")).get("ops", {}).items():
                if isinstance(e, dict):
                    base[n] = e
        naive = dict(base)
        merged = dict(base)
        for n, e in config["ops"].items():
            if not isinstance(e, dict):
                continue
            naive[n] = e
            merged[n] = supertool._merge_op_def(merged.get(n), e)

        def path_named(d: Dict[str, Any]) -> set:
            return {n for n, e in d.items()
                    if supertool._syntax_names_a_path(e.get("syntax", ""))}

        assert path_named(merged) - path_named(naive) == {"git-diff"}
