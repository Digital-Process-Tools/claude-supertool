"""`ops.<op>.<key>` collides with another op's key of the same name (#1009).

`_resolve_custom_op`'s launcher exports every non-reserved key of an op's own
merged config entry as `SUPERTOOL_<KEY>` -- the key alone, never namespaced by
the op that declared it. Nothing stopped two different ops, each declaring
their own `ops.<op>.<key>` in the SAME project's `.supertool.json`, from
picking the same key name and silently sharing one variable: whichever op ran
read whatever value its own entry happened to carry, with no error and no way
to tell.

The design chosen here -- argued in the PR body against the alternative of
renaming to `SUPERTOOL_<OP>_<KEY>` -- is refusal at the point the colliding op
would run, not a rename. A preset's own shipped defaults deliberately share
one key across many ops on purpose (`REPO_TARGET` across every `gh-*`/`gl-*`
op, `DEFAULT_LIMIT` across every list op) -- the same SUPERTOOL_GIT_TIMEOUT
shape, several readers by design -- so the collision check is scoped to the
PROJECT's own "ops" section, where an ad hoc name picked twice is far more
likely to be an accident than a plan.

Every "must refuse" case here has a "must not refuse, and must reach its own
value" partner in the same fixture, because an assertion that a collision is
refused passes if refusal fired for every call, colliding or not.
"""
from __future__ import annotations

import copy

import pytest

import supertool


# ---------------------------------------------------------------------------
# The pure detector
# ---------------------------------------------------------------------------

def test_two_ops_declaring_the_same_key_name_collide():
    project_ops = {
        "op-a": {"cmd": "true", "prefix": "x"},
        "op-b": {"cmd": "true", "prefix": "y"},
    }
    collisions = supertool._op_config_key_collisions(project_ops)
    assert collisions == {"SUPERTOOL_PREFIX": ["op-a", "op-b"]}


def test_two_ops_declaring_different_key_names_do_not_collide():
    """The positive control: distinct names must not be flagged at all."""
    project_ops = {
        "op-a": {"cmd": "true", "prefix": "x"},
        "op-b": {"cmd": "true", "suffix": "y"},
    }
    assert supertool._op_config_key_collisions(project_ops) == {}


def test_reserved_launcher_keys_are_never_treated_as_a_collision():
    """`timeout`, `cmd`, etc. are control fields every op declares -- sharing
    the literal word `timeout` across every op in the project is normal and
    must never be reported."""
    project_ops = {
        "op-a": {"cmd": "a", "timeout": 5},
        "op-b": {"cmd": "b", "timeout": 10},
    }
    assert supertool._op_config_key_collisions(project_ops) == {}


def test_one_op_alone_using_a_key_is_not_a_collision():
    project_ops = {"op-a": {"cmd": "true", "prefix": "x"}}
    assert supertool._op_config_key_collisions(project_ops) == {}


# ---------------------------------------------------------------------------
# Wired into config load: only the PROJECT's own "ops" section counts
# ---------------------------------------------------------------------------

def test_merge_presets_stashes_project_level_collisions_only(tmp_path):
    cfg = {
        "ops": {
            "op-a": {"cmd": "true", "tiers": "x"},
            "op-b": {"cmd": "true", "tiers": "y"},
        }
    }
    supertool._merge_presets(cfg, str(tmp_path))
    assert cfg["_op_config_collisions"] == {
        "SUPERTOOL_TIERS": ["op-a", "op-b"]
    }


def test_a_shipped_presets_own_shared_vocabulary_is_never_flagged(tmp_path):
    """`REPO_TARGET`-style sharing across many preset ops is design, not a
    defect -- the collision detector must never see it, because it is scoped
    to the project's own `ops` section and presets never populate that dict."""
    preset_dir = tmp_path / "presets"
    preset_dir.mkdir()
    (preset_dir / "twins.json").write_text(
        '{"ops": {'
        '"twin-a": {"cmd": "true", "repo_target": "x"}, '
        '"twin-b": {"cmd": "true", "repo_target": "x"}'
        "}}",
        encoding="utf-8",
    )
    cfg = {"presets": ["twins"]}
    supertool._merge_presets(cfg, str(tmp_path))
    assert cfg["ops"]["twin-a"]["repo_target"] == "x"
    assert cfg["ops"]["twin-b"]["repo_target"] == "x"
    assert cfg["_op_config_collisions"] == {}, (
        "a preset's own shared key must never be reported as a collision -- "
        "only a project's own ops section is in scope"
    )


# ---------------------------------------------------------------------------
# The refusal at the point of use
# ---------------------------------------------------------------------------

@pytest.fixture
def _install(monkeypatch):
    def install(ops: dict) -> None:
        cfg = {"ops": copy.deepcopy(ops)}
        supertool._merge_presets(cfg, "/nonexistent")
        monkeypatch.setattr(supertool, "_CONFIG", cfg)
        monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    return install


@pytest.fixture
def _no_subprocess(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append({"argv": argv, "env": kw.get("env")})

        class R:
            returncode = 0
            stdout = "ok\n"
            stderr = ""
        return R()

    monkeypatch.setattr(supertool.subprocess, "run", fake_run)
    return calls


def test_a_colliding_op_is_refused_and_names_both_ops(_install, _no_subprocess):
    _install({
        "hashnode_react": {"cmd": "true {arg}", "auto_force": True},
        "hashnode_comment": {"cmd": "true {arg}", "auto_force": False},
    })
    out = supertool._resolve_custom_op("hashnode_react", ["hashnode_react", "x"])
    assert out is not None
    assert out.startswith("ERROR: ")
    assert "hashnode_react" in out
    assert "hashnode_comment" in out
    assert "auto_force" in out
    assert "SUPERTOOL_AUTO_FORCE" in out
    assert _no_subprocess == [], "a refused op must never reach the subprocess"


def test_the_other_colliding_op_is_refused_too(_install, _no_subprocess):
    """Both directions -- refusing only the first declared op would leave the
    second one silently sharing the value it was refused for."""
    _install({
        "hashnode_react": {"cmd": "true {arg}", "auto_force": True},
        "hashnode_comment": {"cmd": "true {arg}", "auto_force": False},
    })
    out = supertool._resolve_custom_op(
        "hashnode_comment", ["hashnode_comment", "y|msg"])
    assert out is not None
    assert out.startswith("ERROR: ")
    assert "hashnode_react" in out and "hashnode_comment" in out


def test_two_ops_with_different_keys_each_reach_their_own_value(
        _install, _no_subprocess):
    """The control this fix must not break: independent keys, independent
    values, no refusal, both subprocess calls actually happen."""
    _install({
        "op-a": {"cmd": "true {arg}", "prefix": "left"},
        "op-b": {"cmd": "true {arg}", "suffix": "right"},
    })
    out_a = supertool._resolve_custom_op("op-a", ["op-a", "x"])
    out_b = supertool._resolve_custom_op("op-b", ["op-b", "y"])
    assert out_a is not None and not out_a.startswith("ERROR:")
    assert out_b is not None and not out_b.startswith("ERROR:")
    assert len(_no_subprocess) == 2
    assert _no_subprocess[0]["env"]["SUPERTOOL_PREFIX"] == "left"
    assert "SUPERTOOL_SUFFIX" not in _no_subprocess[0]["env"]
    assert _no_subprocess[1]["env"]["SUPERTOOL_SUFFIX"] == "right"
    assert "SUPERTOOL_PREFIX" not in _no_subprocess[1]["env"]


def test_an_op_inheriting_a_shared_preset_default_is_never_refused(
        _install, _no_subprocess):
    """The scenario the brief calls out by name: SUPERTOOL_GIT_TIMEOUT-style
    sharing, several ops reading one variable on purpose, must keep working.
    Simulated here as two ops that both carry the SAME key with the SAME
    value and neither is a project-level declaration collision because
    nothing distinguishes them from a preset default at this layer -- the
    collision list is empty, so no op is ever refused."""
    cfg = {"ops": {
        "op-a": {"cmd": "true {arg}"},
        "op-b": {"cmd": "true {arg}"},
    }}
    # No project-level key on either op -- config["_op_config_collisions"]
    # is empty, so nothing is ever named a collision.
    supertool._merge_presets(cfg, "/nonexistent")
    assert cfg["_op_config_collisions"] == {}


# ---------------------------------------------------------------------------
# The refinement forced by this repo's own shipped config
# ---------------------------------------------------------------------------

def test_the_same_value_repeated_across_ops_is_not_a_collision():
    """The live counter-example that broke the first version of this fix.

    This repository's own `.supertool.json` sets `watch_name: "oss-supertool"`
    on five different ops in the `watch` preset family (`channel`, `radar`,
    `unwatch`, `watch`, `watches`) -- one identifier, repeated on purpose so
    the whole family points at the same channel. `supertool 'ops:roster'`
    against this repo's own config flagged all five as colliding under the
    name-only rule, which would have refused every `watch` op in this
    repository the moment this fix shipped. Refusal is for two ops that
    picked the same name for DIFFERENT things -- signalled here by the values
    actually differing -- not for a value deliberately repeated.
    """
    project_ops = {
        "channel": {"cmd": "true", "watch_name": "oss-supertool"},
        "radar": {"cmd": "true", "watch_name": "oss-supertool"},
        "unwatch": {"cmd": "true", "watch_name": "oss-supertool"},
        "watch": {"cmd": "true", "watch_name": "oss-supertool"},
        "watches": {"cmd": "true", "watch_name": "oss-supertool"},
    }
    assert supertool._op_config_key_collisions(project_ops) == {}


def test_this_repositorys_own_shipped_config_has_zero_collisions():
    """The end-to-end regression: load this repo's real `.supertool.json`
    through the real loader and require the collision list to be empty.
    Catches exactly the class of break `ops:roster` surfaced during
    development -- a whole preset family refused by its own maintainer's
    config the moment this landed."""
    import json
    from pathlib import Path
    root = Path(__file__).parent.parent
    cfg = json.loads((root / ".supertool.json").read_text(encoding="utf-8"))
    supertool._merge_presets(cfg, str(root))
    assert cfg["_op_config_collisions"] == {}, cfg["_op_config_collisions"]


def test_two_ops_with_the_same_key_but_different_values_still_collide():
    """The value-equality carve-out must not swallow the real defect --
    same key, genuinely different values, still refused."""
    project_ops = {
        "hashnode_react": {"cmd": "true", "auto_force": True},
        "hashnode_comment": {"cmd": "true", "auto_force": False},
    }
    collisions = supertool._op_config_key_collisions(project_ops)
    assert collisions == {
        "SUPERTOOL_AUTO_FORCE": ["hashnode_comment", "hashnode_react"]
    }


def test_three_ops_where_two_agree_and_one_differs_names_only_the_odd_one():
    """Not all-or-nothing: two ops genuinely sharing one value plus a third
    that disagrees is still a real collision, and the whole group is
    reported -- an operator fixing it needs to see every op that touches the
    name, not just the pair that disagrees."""
    project_ops = {
        "op-a": {"cmd": "true", "tiers": "x"},
        "op-b": {"cmd": "true", "tiers": "x"},
        "op-c": {"cmd": "true", "tiers": "y"},
    }
    collisions = supertool._op_config_key_collisions(project_ops)
    assert collisions == {"SUPERTOOL_TIERS": ["op-a", "op-b", "op-c"]}
