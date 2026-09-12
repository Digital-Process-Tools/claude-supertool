"""A foreign poller's argv path names its own version -- `watches` never
said so (#2529).

Carried from #2525 finding 3, scoped out of #2526 on purpose ("left for a
follow-up"). On 2026-09-11 four pollers on the `claude-oss` channel were
still running from `/plugins/cache/dpt-plugins/supertool/0.57.0` while
0.60.0 was installed -- every fix shipped to `presets/watch/` in between
never reached them until an operator in *that* repo's own session ran
`unwatch` then `watch`. `watches` disclosed the foreign fleet
(`_foreign_poller_lines`, #1881/#1893) but said nothing about which code it
was running.

The fix is disclosure only, exactly as #2526 scoped it: a poller's own argv
carries `sys.executable`, then the resolved path to its own `dispatcher.py`
(`poller_argv`) -- for a poller forked from an installed plugin-cache copy
that path is `.../dpt-plugins/supertool/<version>/presets/watch/
dispatcher.py`, so the version is already sitting in `ps` output and needs
no new cross-channel state-directory read.

Three states, not two, same shape as every other reading in this preset:

* a version parsed from every pid's own path, compared against what this
  install answers for itself (`installed_version`) -- differ, match, or
  installed could not be determined either
* pids on one channel disagree on their own version -- reported as a set,
  never averaged into one wrong number
* nothing in any pid's path carried a version at all (a plain checkout, not
  a plugin-cache layout) -- said plainly, never silently absent
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from _changelog_findable import assert_change_is_findable

WATCH_DIR = Path(__file__).parent.parent / "presets" / "watch"
sys.path.insert(0, str(WATCH_DIR))

import naming  # noqa: E402
import transport  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dispatcher = _load("watch_dispatcher_2529", WATCH_DIR / "dispatcher.py")


# ---------------------------------------------------------------------------
# _version_from_dispatcher_path -- reading the version back out of an argv
# ---------------------------------------------------------------------------

def test_extracts_the_version_from_a_plugin_cache_path() -> None:
    path = ("/Users/op/.claude/plugins/cache/dpt-plugins/supertool/0.57.0/"
            "presets/watch/dispatcher.py")
    assert transport._version_from_dispatcher_path(path) == "0.57.0"


def test_a_windows_backslash_path_is_read_the_same_way() -> None:
    """Cross-platform: a poller forked on Windows carries backslashes, and
    the same regex must not need a second code path (observed: this is the
    same normalisation `_labelled` already applies to the dispatcher-tail
    match itself, reasoned rather than run on a Windows host here)."""
    path = ("C:\\Users\\op\\plugins\\cache\\dpt-plugins\\supertool\\0.57.0\\"
            "presets\\watch\\dispatcher.py")
    assert transport._version_from_dispatcher_path(path) == "0.57.0"


def test_a_plain_checkout_path_has_no_version_to_find() -> None:
    """Must-fire control: a real path from this very tree, with no version
    directory in it at all, must answer None rather than a wrong guess."""
    real_path = str((WATCH_DIR / "dispatcher.py").resolve())
    assert transport._version_from_dispatcher_path(real_path) is None


# ---------------------------------------------------------------------------
# installed_version -- read fresh off this tree's own _supertool.py
# ---------------------------------------------------------------------------

def test_installed_version_answers_on_this_real_tree() -> None:
    version, why = transport.installed_version()
    assert why == ""
    assert version
    # Cross-checked against the same file a different route: the running
    # process's own VERSION constant, imported the normal way.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import _supertool
    assert version == _supertool.VERSION


def test_installed_version_declines_rather_than_guessing_when_the_file_is_missing(
        monkeypatch, tmp_path) -> None:
    missing_root = tmp_path / "nowhere" / "presets" / "watch" / "transport.py"
    monkeypatch.setattr(transport, "Path", lambda *a, **k: missing_root)
    version, why = transport.installed_version()
    assert version is None
    assert why


# ---------------------------------------------------------------------------
# foreign_version_disclosure -- the rendered clause
# ---------------------------------------------------------------------------

def test_a_stale_foreign_version_is_named_against_installed() -> None:
    line = transport.foreign_version_disclosure(["0.57.0"], "0.60.0")
    assert "0.57.0" in line
    assert "installed: 0.60.0" in line


def test_a_matching_foreign_version_says_so_rather_than_naming_a_mismatch() -> None:
    """Must-fire control for the line above: same shape, no mismatch to
    report, and the word "installed:" (a comparison clause) must not appear
    for a poller that is not actually stale."""
    line = transport.foreign_version_disclosure(["0.60.0"], "0.60.0")
    assert "0.60.0" in line
    assert "matches installed" in line
    assert "installed:" not in line


def test_mixed_versions_on_one_channel_are_reported_as_a_set_not_averaged() -> None:
    line = transport.foreign_version_disclosure(["0.57.0", "0.58.0"], "0.60.0")
    assert "0.57.0" in line and "0.58.0" in line
    assert "mixed" in line


def test_no_derivable_version_says_so_plainly() -> None:
    line = transport.foreign_version_disclosure([None, None], "0.60.0")
    assert "not determined" in line


def test_installed_being_undeterminable_is_also_said_plainly() -> None:
    line = transport.foreign_version_disclosure(["0.57.0"], None)
    assert "0.57.0" in line
    assert "could not be determined" in line


# ---------------------------------------------------------------------------
# the actual `watches` render -- the paired must-fire / real-assertion cases
# ---------------------------------------------------------------------------

class _Machine:
    def __init__(self, base: Path, mine: Path, theirs: Path) -> None:
        self.base = str(base)
        self.mine = str(mine)
        self.theirs = str(theirs)
        self.rows: list[tuple[int, list[str]]] = []
        self.alive: set[int] = set()

    def add_foreign(self, pid: int, source: str, watcher_id: str,
                     dispatcher_path: str) -> None:
        self.rows.append((pid, [
            sys.executable, dispatcher_path,
            transport.POLL_SUBOP, source, watcher_id,
            transport.CHANNEL_PREFIX + transport.channel_key(self.theirs),
        ]))
        self.alive.add(pid)

    def ps_rows(self):
        return [(pid, argv) for pid, argv in self.rows if pid in self.alive]

    def pid_alive(self, pid: int) -> bool:
        return pid in self.alive


@pytest.fixture
def machine(tmp_path, monkeypatch) -> _Machine:
    base = tmp_path
    mine = base / "supertool-watch-oss-supertool"
    theirs = base / "supertool-watch-fdavid-dvsi-5535f2d5"
    mine.mkdir()
    theirs.mkdir()
    monkeypatch.setattr(transport, "STATE_DIR", str(mine))
    monkeypatch.setattr(dispatcher.transport, "STATE_DIR", str(mine))
    monkeypatch.setattr(naming, "BASE_DIR", str(base))
    m = _Machine(base, mine, theirs)
    monkeypatch.setattr(transport, "_ps_rows", m.ps_rows)
    monkeypatch.setattr(transport, "_pid_alive", m.pid_alive)
    monkeypatch.setattr(transport, "ps_scan_supported", lambda: True)
    monkeypatch.setattr(transport, "channel_disclosure", lambda: [])
    monkeypatch.setattr(dispatcher.transport, "channel_disclosure", lambda: [])
    return m


def test_a_foreign_poller_with_no_derivable_version_does_not_claim_a_comparison(
        machine, capsys) -> None:
    """Must-fire half of the pair below: a foreign poller forked from a plain
    checkout (this tree's own real dispatcher.py path, no version directory
    in it) must render "cannot tell", never a fabricated "(installed: ...)"
    comparison it has no evidence for."""
    real_path = str((WATCH_DIR / "dispatcher.py").resolve())
    machine.add_foreign(501, "gitlab-mr", "19509", real_path)
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "version not determined from its argv" in out, out
    assert "installed:" not in out, out


def test_a_stale_plugin_cache_poller_is_named_against_what_is_installed_now(
        machine, capsys, monkeypatch) -> None:
    """The actual assertion (#2529): a foreign poller whose own argv path
    names an old plugin-cache version is disclosed against what this
    install currently answers, without any new cross-channel state read."""
    monkeypatch.setattr(transport, "installed_version", lambda: ("0.60.0", ""))
    fake_path = ("/Users/op/.claude/plugins/cache/dpt-plugins/supertool/"
                 "0.57.0/presets/watch/dispatcher.py")
    machine.add_foreign(502, "gitlab-mr", "33952", fake_path)
    machine.add_foreign(503, "gitlab-mr", "33952", fake_path)
    machine.add_foreign(504, "gitlab-mr", "33992", fake_path)
    machine.add_foreign(505, "gitlab-mr-feed", "@me", fake_path)
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "running supertool 0.57.0" in out, out
    assert "(installed: 0.60.0)" in out, out
    assert "4 on channel" in out, out


def test_a_foreign_fleet_running_the_current_version_says_it_matches(
        machine, capsys, monkeypatch) -> None:
    monkeypatch.setattr(transport, "installed_version", lambda: ("0.60.0", ""))
    fake_path = ("/Users/op/.claude/plugins/cache/dpt-plugins/supertool/"
                 "0.60.0/presets/watch/dispatcher.py")
    machine.add_foreign(506, "gitlab-mr", "19509", fake_path)
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "running supertool 0.60.0 (matches installed)" in out, out


# --- documentation ---------------------------------------------------------

def test_the_change_is_findable():
    assert_change_is_findable(2529)
