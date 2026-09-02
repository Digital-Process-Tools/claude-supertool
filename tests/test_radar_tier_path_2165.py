"""A radar tier may live outside the plugin directory (#2165).

`radar._tier_module` resolved a registered tier name two ways, and both landed
inside the installed plugin: `presets/watch/tiers/<name>.py` beside `radar.py`,
or the `.py` a preset's own op declares. A project that declares its op in its
own `.supertool.json`, with the script in its own tree, is reachable by neither
-- so it can poll a population with a private watch source (#2135 gave sources
that escape hatch) and then cannot put a single line about it on the board.

The measured shape, on 0.53.0, with `radar_tiers = {"server-diag": {}}`:

    radar: WARNING -- tier 'server-diag' is registered but exposes no
    radar_report(); it contributes nothing. Check the name.

Nothing was ever opened. The message names the one cause the operator can do
nothing about from where they are standing.

Four decisions are asserted here.

* **The tier lives beside its poller.** `<dir>/<name>/tier.py` on
  `SUPERTOOL_WATCH_SOURCES_PATH` -- the same directory `<dir>/<name>/poller.py`
  is found in. A tier and a source for one population are one concern, and a
  second search path for the second half is a second thing to keep in step.
* **Shipped tiers win, and a shadow is named.** Identical reasoning to
  `sourcepath.shadowed`: an external `gl-mrs` replacing the shipped board with
  arbitrary Python from a config file is a swap the operator must be told about,
  and skipping it silently is the same defect one layer down.
* **A name is one path component.** `../evil` and `a/b` resolve to nothing, so a
  tier name out of a config file cannot walk out of the directory it names.
* **The unresolved-tier message says where it looked.** An absence that does not
  name the directories searched is this tracker's most-filed class, and this one
  lands in the single message an operator hits while wiring their first tier.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import radar  # noqa: E402
import sourcepath  # noqa: E402
from _changelog_findable import assert_change_is_findable  # noqa: E402

TIER_BODY = """RADAR_OPTIONS = {"fleet"}
RADAR_QUIET_DEFAULT = False


def radar_report(options):
    return ["external tier reporting"], True


def radar_state(options):
    return ["  state     : read from the poller, nothing spawned"]
"""


def _make_tier(root: Path, name: str, body: str = TIER_BODY) -> Path:
    """A minimal external tier at `root/name/tier.py`. Returns its directory."""
    directory = root / name
    directory.mkdir(parents=True)
    (directory / sourcepath.TIER_FILE).write_text(body, encoding="utf-8")
    return directory


# --- resolution --------------------------------------------------------------

def test_a_tier_outside_the_plugin_is_found(tmp_path, monkeypatch):
    external = _make_tier(tmp_path / "private", "server-diag").parent
    monkeypatch.setenv(sourcepath.PATH_ENV, str(external))
    module = radar._tier_module("server-diag")
    assert module is not None
    assert module.radar_report({}) == (["external tier reporting"], True)


def test_the_tier_sits_beside_the_poller_of_the_same_name(tmp_path, monkeypatch):
    """One directory per population, holding both halves. `find_tier` and `find`
    answer from the same entry, which is why there is one search path."""
    directory = _make_tier(tmp_path / "private", "server-diag")
    (directory / "poller.py").write_text("INTERVAL = 7\n", encoding="utf-8")
    monkeypatch.setenv(sourcepath.PATH_ENV, str(directory.parent))
    tier, origin = sourcepath.find_tier("server-diag")
    assert tier == directory / "tier.py"
    assert origin == str(directory.parent)


def test_a_directory_without_a_tier_file_is_not_a_tier(tmp_path, monkeypatch):
    external = tmp_path / "private"
    (external / "server-diag").mkdir(parents=True)
    (external / "server-diag" / "poller.py").write_text("INTERVAL = 7\n", encoding="utf-8")
    monkeypatch.setenv(sourcepath.PATH_ENV, str(external))
    assert sourcepath.find_tier("server-diag") == (None, "")
    assert radar._tier_module("server-diag") is None


def test_entries_are_split_on_the_platform_path_separator(tmp_path, monkeypatch):
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    _make_tier(second, "server-diag")
    monkeypatch.setenv(sourcepath.PATH_ENV, os.pathsep.join([str(first), str(second)]))
    assert radar._tier_module("server-diag") is not None


def test_a_relative_entry_carries_no_tier_either(tmp_path, monkeypatch):
    """`resolve` refuses a relative entry for a source; a tier reads the same
    resolution, so it inherits the refusal rather than restating the rule."""
    _make_tier(tmp_path / "private", "server-diag")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(sourcepath.PATH_ENV, "private")
    assert radar._tier_module("server-diag") is None


def test_nothing_configured_resolves_no_external_tier(monkeypatch):
    monkeypatch.delenv(sourcepath.PATH_ENV, raising=False)
    assert sourcepath.find_tier("server-diag") == (None, "")


# --- a name cannot leave its directory ---------------------------------------

def test_a_tier_name_that_is_not_one_component_resolves_to_nothing(tmp_path, monkeypatch):
    _make_tier(tmp_path / "private", "server-diag")
    monkeypatch.setenv(sourcepath.PATH_ENV, str(tmp_path / "private"))
    for name in ("../server-diag", "a/b", "..", ".", ""):
        assert sourcepath.find_tier(name) == (None, ""), name


# --- shipped wins, and the shadow is named -----------------------------------

def test_an_external_tier_may_not_shadow_a_shipped_one(tmp_path, monkeypatch):
    _make_tier(tmp_path / "private", "gl-mrs")
    monkeypatch.setenv(sourcepath.PATH_ENV, str(tmp_path / "private"))
    module = radar._tier_module("gl-mrs")
    assert module is not None
    assert getattr(module, "__file__", "").endswith(os.path.join("tiers", "gl_mrs.py"))


def test_a_shadowing_tier_is_named_rather_than_silently_skipped(tmp_path, monkeypatch):
    external = _make_tier(tmp_path / "private", "gl-mrs").parent
    monkeypatch.setenv(sourcepath.PATH_ENV, str(external))
    lines = radar.tier_shadow_lines(["gl-mrs"])
    assert lines
    joined = "\n".join(lines)
    assert "gl-mrs" in joined
    assert str(external) in joined
    assert "NOT loaded" in joined


def test_a_tier_nobody_ships_produces_no_shadow_line(tmp_path, monkeypatch):
    external = _make_tier(tmp_path / "private", "server-diag").parent
    monkeypatch.setenv(sourcepath.PATH_ENV, str(external))
    assert radar.tier_shadow_lines(["server-diag"]) == []


# --- the board, end to end ---------------------------------------------------

def test_an_external_tier_renders_on_the_board(tmp_path, monkeypatch):
    external = _make_tier(tmp_path / "private", "server-diag").parent
    monkeypatch.setenv(sourcepath.PATH_ENV, str(external))
    monkeypatch.setenv(radar.TIERS_ENV, '{"server-diag": {}}')
    lines, healthy, failures = radar.tier_reports()
    assert failures == []
    assert healthy is True
    assert "external tier reporting" in "\n".join(lines)


def test_an_external_tier_answers_the_read_only_view(tmp_path, monkeypatch):
    external = _make_tier(tmp_path / "private", "server-diag").parent
    monkeypatch.setenv(sourcepath.PATH_ENV, str(external))
    monkeypatch.setenv(radar.TIERS_ENV, '{"server-diag": {}}')
    lines, failures = radar.tier_states()
    assert failures == []
    assert "read from the poller, nothing spawned" in "\n".join(lines)


def test_an_unresolved_tier_names_every_directory_it_searched(tmp_path, monkeypatch):
    external = tmp_path / "private"
    external.mkdir()
    monkeypatch.setenv(sourcepath.PATH_ENV, str(external))
    monkeypatch.setenv(radar.TIERS_ENV, '{"server-diag": {}}')
    _lines, healthy, failures = radar.tier_reports()
    assert healthy is False
    joined = "\n".join(failures)
    assert "server-diag" in joined
    assert str(external) in joined
    assert sourcepath.TIER_FILE in joined


def test_an_unresolved_tier_says_the_path_is_unset_when_it_is(monkeypatch):
    monkeypatch.delenv(sourcepath.PATH_ENV, raising=False)
    monkeypatch.setenv(radar.TIERS_ENV, '{"server-diag": {}}')
    _lines, _healthy, failures = radar.tier_reports()
    joined = "\n".join(failures)
    assert sourcepath.PATH_ENV in joined


def test_the_read_only_view_names_where_it_looked_too(tmp_path, monkeypatch):
    """`radar:--state` is the surface an operator opens *because* the board said
    nothing. It must not be the one that explains least."""
    external = tmp_path / "private"
    external.mkdir()
    monkeypatch.setenv(sourcepath.PATH_ENV, str(external))
    monkeypatch.setenv(radar.TIERS_ENV, '{"server-diag": {}}')
    lines, failures = radar.tier_states()
    joined = "\n".join(lines + failures)
    assert "UNRESOLVED" in joined
    assert str(external) in joined


def test_a_broken_external_tier_is_a_failure_not_a_crash(tmp_path, monkeypatch):
    external = _make_tier(tmp_path / "private", "server-diag",
                          body="raise RuntimeError('boom')\n").parent
    monkeypatch.setenv(sourcepath.PATH_ENV, str(external))
    monkeypatch.setenv(radar.TIERS_ENV, '{"server-diag": {}}')
    _lines, healthy, failures = radar.tier_reports()
    assert healthy is False
    assert any("server-diag" in line for line in failures)


def test_the_change_is_findable():
    assert_change_is_findable(2165, REPO)
