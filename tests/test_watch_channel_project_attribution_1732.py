"""Which project a watch channel belongs to, in three states (#1732).

`SUPERTOOL_WATCH_NAME` derives both the socket and the poller state directory, so
two projects running under one name share one socket and one slot directory. Every
surface that renders the channel — `watches`, `radar`, `channel:health` — printed
that state **identically to a private single-project fleet**: the name, the two
paths, and no field saying whose name it is. A shared fleet and a correct one were
the same render, which is this repository's own most-filed defect arriving on the
watch board.

The value is an environment variable and stays one: an export is the value a
*running* poller already captured, and making a config file win would move the
paths underneath a live fleet (`naming.resolve`'s precedence note, #1477). So
nothing here changes what is in force. What changes is that the resolution now
carries **who claims it**, read from the `.supertool.json` above the cwd, in four
states that a reader can tell apart:

* `found` — this project declares a `watch_name` in at least one watch op block.
* `silent` — a config is there and no watch op block declares one, so whatever is
  in force arrived from an environment this project never asked for.
* `no-config` — nothing above the cwd claims anything.
* `unreadable` — a config is there and could not be parsed. **Not `no-config`**:
  a look that did not happen must never render as an absence in the world.

Every case below pairs with a positive control. A resolution that refused to
attribute *anything* would satisfy "must not render a shared name as private" on
its own, so the ordinary single-project path is asserted to still resolve, still
name itself, and still say the project owns it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WATCH_DIR = REPO / "presets" / "watch"
for _dir in (str(WATCH_DIR), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import naming  # noqa: E402
from _changelog_findable import assert_change_is_findable  # noqa: E402


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


radar = _module("watch_radar_1732", WATCH_DIR / "radar.py")
dispatcher = radar.dispatcher
transport = radar.transport
channel = _module("watch_channel_1732", WATCH_DIR / "channel.py")

NAMED = {naming.NAME_ENV: "oss-supertool"}


def _project(root: Path, ops: dict) -> Path:
    """A directory with a `.supertool.json` declaring `ops`, and its cwd."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".supertool.json").write_text(
        json.dumps({"ops": ops}), encoding="utf-8")
    return root


def _declaring(name: str) -> dict:
    return {op: {"watch_name": name} for op in ("radar", "watches", "channel")}


# --- the reading, and its four states ---------------------------------------

def test_a_project_that_declares_one_name_is_read_as_declaring_it(tmp_path) -> None:
    """The positive control for every refusal below: the ordinary
    single-project declaration still resolves, and names the ops it came from."""
    root = _project(tmp_path / "repo", _declaring("oss-supertool"))
    declared = naming.declared_names(str(root))
    assert declared.state == naming.DECLARED_FOUND, declared
    assert declared.names == ("oss-supertool",), declared
    assert set(declared.declaring_ops) == {"radar", "watches", "channel"}, declared
    assert declared.path == str(root / ".supertool.json"), declared


def test_the_config_is_found_from_a_subdirectory_too(tmp_path) -> None:
    root = _project(tmp_path / "repo", _declaring("oss-supertool"))
    deep = root / "presets" / "watch"
    deep.mkdir(parents=True)
    assert naming.declared_names(str(deep)).names == ("oss-supertool",)


def test_a_config_declaring_no_watch_name_is_silent_not_absent(tmp_path) -> None:
    root = _project(tmp_path / "repo", {"git-push": {"budget": 1500}})
    declared = naming.declared_names(str(root))
    assert declared.state == naming.DECLARED_SILENT, declared
    assert declared.names == (), declared


def test_no_config_above_the_cwd_is_its_own_state(tmp_path) -> None:
    bare = tmp_path / "nothing" / "here"
    bare.mkdir(parents=True)
    declared = naming.declared_names(str(bare))
    assert declared.state == naming.DECLARED_NO_CONFIG, declared


def test_an_unparseable_config_is_unreadable_and_never_no_config(tmp_path) -> None:
    """The whole point of the third state. A config that could not be parsed
    tells us nothing about who owns the name; rendering that as `nothing here
    claims it` is a claim about the world built on a read that failed."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".supertool.json").write_text("{not json", encoding="utf-8")
    declared = naming.declared_names(str(root))
    assert declared.state == naming.DECLARED_UNREADABLE, declared
    assert declared.state != naming.DECLARED_NO_CONFIG
    assert declared.why, "an unreadable state with no reason is a bare refusal"


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="needs a directory this uid cannot traverse: Windows does not honour "
           "a 0o000 mode this way, and root traverses it regardless")
def test_a_directory_the_walk_cannot_traverse_is_unreadable_not_absent(
        tmp_path) -> None:
    """The absence the *tool* produced, one level above the parse.

    `os.path.isfile` is `os.stat` inside `except OSError: return False`, so the
    first version of the walk answered "no config here" for a directory it could
    not traverse and kept climbing — reporting `no-config`, which is a claim
    about the world, off a look that failed. Raised in review of #1732."""
    locked = tmp_path / "locked"
    locked.mkdir()
    inner = locked / "repo"
    inner.mkdir()
    locked.chmod(0o000)
    try:
        declared = naming.declared_names(str(inner))
    finally:
        locked.chmod(0o700)
    assert declared.state == naming.DECLARED_UNREADABLE, declared
    assert declared.state != naming.DECLARED_NO_CONFIG
    blob = _blob(naming.resolve(NAMED), declared)
    assert "unknown" in blob, blob
    assert "nothing here claims" not in blob, blob


def test_a_traversable_walk_that_finds_nothing_is_still_absent(tmp_path) -> None:
    """The positive control for the assertion above: the refusal must not have
    swallowed the ordinary "there is genuinely no config" answer."""
    inner = tmp_path / "plain" / "repo"
    inner.mkdir(parents=True)
    assert naming.declared_names(str(inner)).state == naming.DECLARED_NO_CONFIG


def test_a_watch_name_on_a_non_watch_op_counts_and_that_is_deliberate(
        tmp_path) -> None:
    """Reviewed and kept (#1732).

    Restricting the scan to `WATCH_OPS` looks tidier and would be wrong: the
    config-to-env route is **per op**, so `{"dashboard": {"watch_name": "x"}}`
    really does put `dashboard`'s subprocess on channel `x`. Reporting that as a
    disagreement is accurate; hiding it would make the one surface that can see
    a stray declaration decline to mention it. The deleted `bin/supertool-workspace`
    scanned every block for the same reason, and `bin/oss-workspace` still does.
    `silent_ops` is the half that is scoped to `WATCH_OPS`, because "which ops
    resolve from the environment alone" is only a question about this preset's."""
    root = _project(tmp_path / "repo", {
        **{op: {"watch_name": "oss-supertool"} for op in naming.WATCH_OPS},
        "dashboard": {"watch_name": "somewhere-else"},
    })
    declared = naming.declared_names(str(root))
    assert "dashboard" in declared.declaring_ops, declared
    assert declared.names == ("oss-supertool", "somewhere-else"), declared
    assert declared.silent_ops == (), "every WATCH_OP declares one"
    blob = _blob(naming.resolve(NAMED), declared)
    assert "disagree" in blob, blob
    assert "somewhere-else" in blob, blob


def test_op_blocks_that_disagree_are_all_reported(tmp_path) -> None:
    root = _project(tmp_path / "repo", {
        "watches": {"watch_name": "oss-supertool"},
        "radar": {"watch_name": "dvsi"},
    })
    declared = naming.declared_names(str(root))
    assert declared.state == naming.DECLARED_FOUND
    assert declared.names == ("dvsi", "oss-supertool"), declared


def test_watch_ops_that_declare_nothing_are_named(tmp_path) -> None:
    """`watch` and `unwatch` are the ops that SPAWN and KILL pollers. A project
    that declares the name on `watches` alone puts its board on the private
    channel and its pollers on the default one — #1309's half-configured state,
    arriving one door over."""
    root = _project(tmp_path / "repo", {"watches": {"watch_name": "oss-supertool"}})
    declared = naming.declared_names(str(root))
    assert "watch" in declared.silent_ops, declared
    assert "unwatch" in declared.silent_ops, declared
    assert "watches" not in declared.silent_ops, declared


def test_a_project_declaring_every_watch_op_has_no_silent_ops(tmp_path) -> None:
    """The positive control for the assertion above."""
    root = _project(tmp_path / "repo",
                    {op: {"watch_name": "oss-supertool"}
                     for op in naming.WATCH_OPS})
    assert naming.declared_names(str(root)).silent_ops == (), "all five declared"


# --- what a reader is told --------------------------------------------------

def _blob(resolved, declared) -> str:
    return "\n".join(naming.disclosure_lines(resolved, declared))


def test_a_name_this_project_declares_is_rendered_as_this_projects_own(
        tmp_path) -> None:
    """The positive control for the collision renders: a correct private fleet
    still discloses its name, its paths and now its owner."""
    root = _project(tmp_path / "repo", _declaring("oss-supertool"))
    blob = _blob(naming.resolve(NAMED), naming.declared_names(str(root)))
    assert "oss-supertool" in blob
    assert naming.sock_for("oss-supertool") in blob
    assert str(root / ".supertool.json") in blob, blob
    assert "declared by" in blob, blob
    assert "another project" not in blob, blob


def test_a_name_no_project_here_declares_is_rendered_as_possibly_shared(
        tmp_path) -> None:
    """The reported case: four repos with one hand-copied export. The config is
    silent, so the name arrived from an environment this project never asked
    for, and the board has to say so instead of rendering a private fleet."""
    root = _project(tmp_path / "repo", {"git-push": {"budget": 1500}})
    blob = _blob(naming.resolve(NAMED), naming.declared_names(str(root)))
    assert "oss-supertool" in blob
    assert "declares no watch_name" in blob, blob
    assert "another project" in blob, blob


def test_a_name_that_contradicts_this_projects_own_is_loud(tmp_path) -> None:
    root = _project(tmp_path / "repo", _declaring("dvsi"))
    blob = _blob(naming.resolve(NAMED), naming.declared_names(str(root)))
    assert "oss-supertool" in blob, blob
    assert "dvsi" in blob, blob
    assert "not this project" in blob, blob


def test_an_unreadable_config_is_disclosed_as_unknown_not_as_unclaimed(
        tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".supertool.json").write_text("{not json", encoding="utf-8")
    blob = _blob(naming.resolve(NAMED), naming.declared_names(str(root)))
    assert "could not be read" in blob, blob
    assert "unknown" in blob, blob
    assert "declares no watch_name" not in blob, blob


def test_the_default_channel_says_nothing_even_where_a_name_is_declared(
        tmp_path) -> None:
    """A decision, pinned so it is not silently re-taken.

    "This project declares a name and nothing exported it" reads like the case
    worth shouting about, and it is dropped twice over. It is nearly unreachable:
    a `watch_name` in an op's `.supertool.json` block arrives at *that* op as
    `SUPERTOOL_WATCH_NAME` with no launcher involved, so the op whose block
    declares it always has it in force. The reachable half is an op whose own
    block is silent, and `silent_ops` reports that from the declaring side, where
    it can name which ops. Printing it anyway was measured at twelve exact-stdout
    board tests in four unrelated suites gaining a banner — #1495's "a banner on
    every board is a banner nobody reads", arriving as churn."""
    root = _project(tmp_path / "repo", _declaring("oss-supertool"))
    declared = naming.declared_names(str(root))
    assert declared.state == naming.DECLARED_FOUND, "the declaration IS readable"
    assert naming.disclosure_lines(naming.resolve({}), declared) == []


def test_the_default_channel_with_nothing_declared_still_says_nothing(
        tmp_path) -> None:
    """A banner on every board is a banner nobody reads. #1495's rule, kept."""
    bare = tmp_path / "bare"
    bare.mkdir()
    assert naming.disclosure_lines(
        naming.resolve({}), naming.declared_names(str(bare))) == []


def test_the_silent_watch_ops_reach_the_board(tmp_path) -> None:
    root = _project(tmp_path / "repo", {"watches": {"watch_name": "oss-supertool"}})
    blob = _blob(naming.resolve(NAMED), naming.declared_names(str(root)))
    assert "unwatch" in blob, blob
    assert "watch" in blob, blob


def test_a_declared_name_cannot_forge_a_second_board_row(tmp_path) -> None:
    """The name lands on `watches`' fixed-width board and now arrives from a
    file this process did not write (#1423/#1522)."""
    root = _project(tmp_path / "repo",
                    _declaring("oss\nwatches: name dvsi (from SUPERTOOL_WATCH_NAME)"))
    blob = _blob(naming.resolve(NAMED), naming.declared_names(str(root)))
    assert "\nwatches: name dvsi" not in blob, blob


@pytest.mark.skipif(
    os.name == "nt",
    reason="a newline and a ':' are both illegal in a Windows directory name, so "
           "the fixture cannot be built there — the flattening it asserts is "
           "platform-independent and is covered by the declared-name case above")
def test_the_config_path_cannot_forge_a_second_board_row(tmp_path) -> None:
    root = _project(tmp_path / "repo\nwatches: forged", _declaring("dvsi"))
    blob = _blob(naming.resolve(NAMED), naming.declared_names(str(root)))
    assert "\nwatches: forged" not in blob, blob


def test_channel_health_names_the_owner_too(monkeypatch, tmp_path) -> None:
    """`channel:health` renders its own body rather than going through
    `transport.channel_disclosure`, so it is the surface most likely to be left
    behind — and it is the one an operator opens when they suspect a collision."""
    root = _project(tmp_path / "repo", {"git-push": {"budget": 1500}})
    monkeypatch.chdir(root)
    resolved = naming.resolve(NAMED)
    lines = "\n".join(channel._channel_lines(resolved.sock, resolved))
    assert "another project" in lines, lines


# --- back-compatibility and the one accessor --------------------------------

def test_omitting_the_reading_leaves_the_formatter_exactly_as_it_was() -> None:
    """`disclosure_lines(resolved)` is called from two other suites and from
    `channel.py`'s own render. Attribution is an argument, not a filesystem read
    wired into a pure function."""
    assert naming.disclosure_lines(naming.resolve({})) == []
    named = naming.resolve(NAMED)
    assert naming.disclosure_lines(named) == naming.disclosure_lines(named, None)


def test_the_one_accessor_carries_the_attribution_to_the_boards(
        monkeypatch, tmp_path, capsys) -> None:
    """`watches` renders through `transport.channel_disclosure`, so the claim
    worth pinning is that the operator's board carries it — not that the
    accessor returns it."""
    root = _project(tmp_path / "repo", {"git-push": {"budget": 1500}})
    monkeypatch.chdir(root)
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path / "slots"))
    # The census, not `scan_poller_pids` (#1881): the board renders three
    # buckets of the scan and that function is one of them, so the narrow stub
    # left this attribution render reading the machine's real process table.
    monkeypatch.setattr(transport, "poller_census",
                        lambda: transport.empty_census(True))
    monkeypatch.setattr(transport, "ps_scan_supported", lambda: True)
    monkeypatch.setattr(transport, "RESOLVED", naming.resolve(NAMED))

    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "another project" in out, out


def test_the_change_is_findable() -> None:
    assert_change_is_findable(1732)
