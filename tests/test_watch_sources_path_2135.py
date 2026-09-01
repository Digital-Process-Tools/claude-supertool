"""A watch source may live outside the plugin directory (#2135).

`dispatcher.SOURCES_DIR` was `Path(__file__).parent / "sources"` with no override
anywhere in the preset, so a source plugin could only exist inside the installed
plugin -- a directory every plugin update overwrites. The consequence is not a
missing convenience: `watch:my-source:scope` answers `unknown source`, and with
it go the pid slots, `unwatch`, the `watches` board and radar's healing.

`SUPERTOOL_WATCH_SOURCES_PATH` is a search path of extra directories. It is an
ordinary environment variable, so a non-reserved key in an op's
`.supertool.json` block reaches it with no plumbing (`docs/contributing.md`,
"Extra config keys as environment variables") -- the same seam
`SUPERTOOL_WATCH_NAME` arrives through, and `naming.py` is the precedent for a
knob resolved once, reported, and given a precedence rule with a stated reason.

Four things are asserted here, and each is a decision rather than a mechanism:

* **Shipped wins, and a shadow is named.** An external directory declaring
  `gitlab-mr` would otherwise replace a shipped poller with arbitrary Python on
  a path that came from a config file. Skipping it *silently* is the same defect
  one layer down -- the operator believes their source is loaded and it is not.
* **One function resolves it, for all five watch ops.** A key declared on
  `watches` alone gives a private board over a fleet that was never spawned:
  the half-configured shape of #1309 and #1732 arriving through the config door
  a third time.
* **The path survives the exec.** A poller re-execs itself to take a labelled
  argv (`dispatcher._exec_labelled`), and a source directory reachable at spawn
  and not after the exec is a watcher that dies on its second breath. Unlike the
  state directory (#1477/#1534) nothing here is *derived*, so `poller_env`'s
  `dict(os.environ)` already carries it -- which is a property to pin, not to
  assume.
* **The unknown-source error says where it looked.** It used to print
  `Available: <shipped>`; an absence that does not name the directories searched
  is this tracker's most-filed class, landing in the one message a user hits
  while setting the feature up.
"""
from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import dispatcher  # noqa: E402
import sourcepath  # noqa: E402
import transport  # noqa: E402
from _changelog_findable import assert_change_is_findable  # noqa: E402

POLLER_BODY = """INTERVAL = 7

def poll(state, ctx):
    return [], {}

def is_terminal(state):
    return False
"""


def _make_source(root: Path, name: str) -> Path:
    """A minimal source plugin at `root/name/poller.py`. Returns its directory."""
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "poller.py").write_text(POLLER_BODY, encoding="utf-8")
    return directory


# --- the search path itself --------------------------------------------------

def test_with_nothing_configured_only_the_shipped_directory_is_searched():
    resolved = sourcepath.resolve({})
    assert resolved.shipped == Path(sourcepath.SHIPPED_DIR)
    assert resolved.external == ()
    assert resolved.refused == ()


def test_a_shipped_source_still_resolves_with_nothing_configured():
    found, origin = sourcepath.find("gitlab-mr", sourcepath.resolve({}))
    assert found == Path(sourcepath.SHIPPED_DIR) / "gitlab-mr" / "poller.py"
    assert origin == sourcepath.SHIPPED


def test_a_source_outside_the_plugin_is_found(tmp_path):
    external = tmp_path / "private"
    _make_source(external, "server-diag")
    resolved = sourcepath.resolve({sourcepath.PATH_ENV: str(external)})
    found, origin = sourcepath.find("server-diag", resolved)
    assert found == external / "server-diag" / "poller.py"
    assert Path(origin) == external


def test_entries_are_split_on_the_platform_path_separator(tmp_path):
    """`os.pathsep`, never a hardcoded ':' -- which splits a Windows entry at
    its drive letter and searches two directories that do not exist."""
    first, second = tmp_path / "a", tmp_path / "b"
    _make_source(first, "alpha")
    _make_source(second, "beta")
    raw = os.pathsep.join([str(first), str(second)])
    resolved = sourcepath.resolve({sourcepath.PATH_ENV: raw})
    assert [Path(p) for p in resolved.external] == [first, second]
    assert sourcepath.find("alpha", resolved)[0] == first / "alpha" / "poller.py"
    assert sourcepath.find("beta", resolved)[0] == second / "beta" / "poller.py"


def test_a_directory_without_a_poller_is_not_a_source(tmp_path):
    external = tmp_path / "private"
    (external / "not-a-source").mkdir(parents=True)
    resolved = sourcepath.resolve({sourcepath.PATH_ENV: str(external)})
    assert sourcepath.find("not-a-source", resolved)[0] is None


# --- shipped wins, and a shadow is named -------------------------------------

def test_an_external_source_may_not_shadow_a_shipped_one(tmp_path):
    external = tmp_path / "private"
    _make_source(external, "gitlab-mr")
    resolved = sourcepath.resolve({sourcepath.PATH_ENV: str(external)})
    found, origin = sourcepath.find("gitlab-mr", resolved)
    assert found == Path(sourcepath.SHIPPED_DIR) / "gitlab-mr" / "poller.py"
    assert origin == sourcepath.SHIPPED


def test_a_shadowing_source_is_named_rather_than_silently_skipped(tmp_path):
    external = tmp_path / "private"
    _make_source(external, "gitlab-mr")
    resolved = sourcepath.resolve({sourcepath.PATH_ENV: str(external)})
    shadowed = sourcepath.shadowed(resolved)
    assert [name for name, _ in shadowed] == ["gitlab-mr"]
    lines = "\n".join(sourcepath.disclosure_lines(resolved))
    assert "gitlab-mr" in lines
    assert str(external) in lines
    assert "not loaded" in lines.lower()


def test_a_non_shadowing_external_source_produces_no_shadow_line(tmp_path):
    external = tmp_path / "private"
    _make_source(external, "server-diag")
    resolved = sourcepath.resolve({sourcepath.PATH_ENV: str(external)})
    assert sourcepath.shadowed(resolved) == ()


def test_nothing_configured_says_nothing():
    """A banner on every board is one nobody reads (#1495)."""
    assert sourcepath.disclosure_lines(sourcepath.resolve({})) == []


# --- entries that cannot be used are refused out loud ------------------------

def test_a_relative_entry_is_refused_and_named():
    entry = os.path.join("relative", "dir")
    resolved = sourcepath.resolve({sourcepath.PATH_ENV: entry})
    assert resolved.external == ()
    assert [r.entry for r in resolved.refused] == [entry]
    assert "relative" in " ".join(r.why for r in resolved.refused).lower()
    assert any("relative" in line for line in sourcepath.disclosure_lines(resolved))


def test_a_missing_entry_is_refused_and_named(tmp_path):
    missing = tmp_path / "nope"
    resolved = sourcepath.resolve({sourcepath.PATH_ENV: str(missing)})
    assert resolved.external == ()
    assert [r.entry for r in resolved.refused] == [str(missing)]


def test_a_file_where_a_directory_was_declared_is_refused(tmp_path):
    plain = tmp_path / "file.txt"
    plain.write_text("not a directory", encoding="utf-8")
    resolved = sourcepath.resolve({sourcepath.PATH_ENV: str(plain)})
    assert resolved.external == ()
    assert [r.entry for r in resolved.refused] == [str(plain)]


def test_one_bad_entry_does_not_take_the_good_ones_with_it(tmp_path):
    good = tmp_path / "good"
    _make_source(good, "server-diag")
    raw = os.pathsep.join([str(tmp_path / "nope"), str(good)])
    resolved = sourcepath.resolve({sourcepath.PATH_ENV: raw})
    assert [Path(p) for p in resolved.external] == [good]
    assert len(resolved.refused) == 1
    assert sourcepath.find("server-diag", resolved)[0] == good / "server-diag" / "poller.py"


def test_the_shipped_directory_named_again_is_not_searched_twice():
    resolved = sourcepath.resolve({sourcepath.PATH_ENV: str(sourcepath.SHIPPED_DIR)})
    assert resolved.external == ()
    assert sourcepath.shadowed(resolved) == ()


# --- the unknown-source error says where it looked ---------------------------

def test_unknown_source_names_every_directory_it_searched(tmp_path, capsys, monkeypatch):
    external = tmp_path / "private"
    _make_source(external, "server-diag")
    missing = tmp_path / "nope"
    monkeypatch.setenv(sourcepath.PATH_ENV,
                       os.pathsep.join([str(external), str(missing)]))
    assert dispatcher.cmd_watch(["absent-source", "1"]) == 1
    out = capsys.readouterr().out
    assert "ERROR: unknown source" in out
    assert str(sourcepath.SHIPPED_DIR) in out
    assert str(external) in out
    assert "server-diag" in out
    assert str(missing) in out
    assert "gitlab-mr" in out


def test_unknown_source_says_the_path_is_unset_when_it_is(capsys, monkeypatch):
    monkeypatch.delenv(sourcepath.PATH_ENV, raising=False)
    assert dispatcher.cmd_watch(["absent-source", "1"]) == 1
    out = capsys.readouterr().out
    assert str(sourcepath.SHIPPED_DIR) in out
    assert sourcepath.PATH_ENV in out


# --- one function, and every watch op goes through it ------------------------

def test_the_dispatcher_loads_through_the_one_resolver(tmp_path, monkeypatch):
    external = tmp_path / "private"
    _make_source(external, "server-diag")
    monkeypatch.setenv(sourcepath.PATH_ENV, str(external))
    module = dispatcher._load_source("server-diag")
    assert module is not None
    assert module.INTERVAL == 7


def test_radar_and_the_tiers_reach_the_same_resolver(monkeypatch):
    """`radar` and `tiers/gl_mrs` call `dispatcher._load_source`, which is the
    only door to `sourcepath.find`. Pinning the delegation pins all four
    callers at once."""
    seen = []

    def _fake_find(name, resolved=None):
        seen.append(name)
        return None, ""

    monkeypatch.setattr(sourcepath, "find", _fake_find)
    assert dispatcher._load_source("anything") is None
    assert seen == ["anything"]


def test_no_other_watch_module_builds_its_own_sources_directory():
    """One path, resolved once. A second `<something> / "sources"` anywhere in
    the preset is the half-configured shape re-entering by the back door.

    Asked of the AST rather than of the text: a docstring that *describes* the
    construct -- this file has two, and so does `dispatcher._load_source` -- is
    inert to a parser and indistinguishable to a grep, and a structural guard
    that its own explanation trips is one somebody deletes.
    """
    watch = REPO / "presets" / "watch"
    offenders = []
    modules = sorted(watch.rglob("*.py"))
    assert modules, "the population is derived from a glob and must not be empty"
    for path in modules:
        if path.name == "sourcepath.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                    and isinstance(node.right, ast.Constant)
                    and node.right.value == "sources"):
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert offenders == []


# --- the path survives the exec ----------------------------------------------

def test_the_search_path_survives_the_poller_re_exec(monkeypatch, tmp_path):
    """`_exec_labelled` replaces the process image with `poller_env()`.

    Nothing here is derived from another variable, so -- unlike the state
    directory (#1534) -- `dict(os.environ)` is already the whole answer. That is
    a property of `poller_env` worth pinning rather than assuming: an env built
    from an allowlist instead would drop this, and a re-exec'd poller would
    answer `unknown source` about the source it was already polling.
    """
    external = tmp_path / "private"
    _make_source(external, "server-diag")
    monkeypatch.setenv(sourcepath.PATH_ENV, str(external))
    env = transport.poller_env()
    assert env[sourcepath.PATH_ENV] == str(external)
    assert sourcepath.find("server-diag", sourcepath.resolve(env))[0] is not None


# --- half-configured across the five ops -------------------------------------

def _write_config(directory: Path, ops: dict) -> None:
    (directory / ".supertool.json").write_text(
        json.dumps({"ops": {op: {sourcepath.CONFIG_KEY: value}
                            for op, value in ops.items()}}),
        encoding="utf-8")


def test_a_path_declared_on_some_ops_only_is_reported(tmp_path):
    _write_config(tmp_path, {"watch": "/opt/src", "watches": "/opt/src"})
    notes = sourcepath.config_notes("radar", start_dir=str(tmp_path))
    joined = " ".join(notes)
    assert notes != []
    assert "radar" in joined
    assert "watch" in joined


def test_a_path_declared_on_every_watch_op_is_silent(tmp_path):
    _write_config(tmp_path, {op: "/opt/src" for op in sourcepath.WATCH_OPS})
    assert sourcepath.config_notes("radar", start_dir=str(tmp_path)) == []


def test_declaring_nothing_anywhere_is_silent(tmp_path):
    (tmp_path / ".supertool.json").write_text(json.dumps({"ops": {}}), encoding="utf-8")
    assert sourcepath.config_notes("radar", start_dir=str(tmp_path)) == []


def test_op_blocks_that_disagree_are_the_core_refusal_not_a_note(tmp_path):
    """Two op blocks declaring the same key with different values never reach
    this preset: supertool refuses the op itself
    (`_supertool.py::_op_config_collision_refusal`, #1009). Reporting it here
    too would be a second verdict about a state nothing can be in."""
    declared = {op: "/opt/src" for op in sourcepath.WATCH_OPS}
    declared["radar"] = "/opt/other"
    _write_config(tmp_path, declared)
    assert sourcepath.config_notes("radar", start_dir=str(tmp_path)) == []


# --- the change is documented ------------------------------------------------

def test_the_change_is_findable():
    assert_change_is_findable(2135)
