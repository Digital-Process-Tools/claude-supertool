"""The channel name reaches the two boards an operator reads (#1495).

`transport.RESOLVED.notes` carries the override disclosure - *"SUPERTOOL_WATCH_SOCK
is set and overrides the name: the socket is X, not Y"* - and until this change
only `channel:health` printed it. `radar` and `watches` printed `SOCK_PATH` and
`STATE_DIR` as bare paths, so an operator running a named channel with a stale
`SUPERTOOL_WATCH_SOCK` exported saw two healthy boards and no statement anywhere
that the name was not in force. That is #1477's own defect - a knob whose only
wrong setting is half of it - relocated one surface over.

**Why the fix is in the renders and not in `transport`.** The #1476/#1477
reviewer raised `RESOLVED.notes` as computed-and-never-printed and it was argued
down there deliberately: a poller is detached and its stdout has no reader, so
printing from `transport` writes into nothing. One shared *formatter* in
`naming`, consumed by both renders through one `transport` accessor, is what the
argument left open - the same reason `delivery_of` lives in `transport` rather
than in three renders that could disagree about the same field.

Second instance, same class: `channel:health`'s `state == "unknown"` arm is
correct to say nothing about a holder - `peer_pid` connects, and the connect
just failed - but it did not say that it declined to ask. An omitted line and a
`no holder` line are indistinguishable to a reader scanning for one, so the arm
now states the skip and its reason. #1476 pinned that this arm must not *claim* a
holder, which it still must not, and that is asserted here as well.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

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


radar = _module("watch_radar_1495", WATCH_DIR / "radar.py")
dispatcher = radar.dispatcher
transport = radar.transport
channel = _module("watch_channel_1495", WATCH_DIR / "channel.py")

NAMED = {"SUPERTOOL_WATCH_NAME": "oss"}
STALE_OVERRIDE = {"SUPERTOOL_WATCH_NAME": "oss",
                  "SUPERTOOL_WATCH_SOCK": "/tmp/supertool-watch.sock"}
FAKE_TIERS = '{"fake": {}}'


def _blob(resolved) -> str:
    return "\n".join(naming.disclosure_lines(resolved))


# --- the suite's own channel ------------------------------------------------

def test_the_suite_does_not_inherit_the_developers_channel() -> None:
    """Nothing here may resolve against whoever is running it.

    Every module in this preset resolves `RESOLVED` at *import*, so a
    `SUPERTOOL_WATCH_NAME` exported in the shell reaches the suite before any
    fixture can intervene: `radar`'s board then carries a channel banner and
    four files asserting its exact stdout go red, along with two in
    `test_watch_sock_path_581.py` that assert the *default* socket after
    deleting only the override. Measured under
    `SUPERTOOL_WATCH_NAME=oss-supertool` - the value this repo's own
    `.supertool.json` sets since #1477, so it is every maintainer's environment
    and not an exotic one. All six say nothing about the code and CI, which
    exports none of the three, cannot see any of it.

    `conftest.pytest_configure` deletes the three variables, which is the only
    place early enough. This assertion is what stops that being quietly undone.
    """
    for var in (naming.NAME_ENV, naming.SOCK_ENV, naming.STATE_DIR_ENV):
        assert var not in os.environ, (
            f"{var} is set in the environment running this suite, so every "
            f"watch module resolved against it at import and any test asserting "
            f"a board's exact output is testing the developer's shell")


# --- the formatter ----------------------------------------------------------

def test_a_default_unnamed_channel_discloses_nothing() -> None:
    """A banner printed on every board is a banner nobody reads, and on the
    default paths there is no half-set state to disclose."""
    assert naming.disclosure_lines(naming.resolve({})) == []


def test_a_named_channel_names_itself_the_variable_and_both_paths() -> None:
    lines = naming.disclosure_lines(naming.resolve(NAMED))
    assert lines, "a named channel is exactly what the boards did not say"
    blob = "\n".join(lines)
    assert "oss" in blob
    assert naming.NAME_ENV in blob
    assert naming.sock_for("oss") in blob
    assert naming.state_dir_for("oss") in blob


def test_a_stale_socket_override_is_disclosed_rather_than_taken_silently() -> None:
    """The headline case: the name is set, the export wins, and until now the
    only surface that said so was the one the tick does not open with."""
    blob = _blob(naming.resolve(STALE_OVERRIDE))
    assert naming.SOCK_ENV in blob
    assert "overrides the name" in blob


def test_a_refused_name_is_disclosed_too() -> None:
    blob = _blob(naming.resolve({"SUPERTOOL_WATCH_NAME": "../evil"}))
    assert naming.NAME_ENV in blob
    assert "not usable" in blob


def test_the_half_configured_pair_without_a_name_is_disclosed() -> None:
    blob = _blob(naming.resolve({"SUPERTOOL_WATCH_SOCK": "/tmp/half.sock"}))
    assert naming.STATE_DIR_ENV in blob


def test_a_name_is_flattened_before_it_reaches_a_board() -> None:
    """A name arrives from `.supertool.json` and lands on a fixed-width board;
    a newline in one used to be able to print a whole extra line at column 0."""
    resolved = naming.resolve({})._replace(name="oss\nradar: all fine")
    assert "\nradar: all fine" not in _blob(resolved)


def test_one_accessor_so_the_two_boards_cannot_disagree(
        monkeypatch, tmp_path, capsys) -> None:
    """Asserted over the two *renders*, not over the accessor's own body.

    The first version of this compared `transport.channel_disclosure()` with
    `naming.disclosure_lines(transport.RESOLVED)` — which is that function's one
    line, so it held for any content whatsoever, including none. The claim worth
    pinning is that `radar` and `watches` put the same resolution in front of a
    reader, which is what "cannot disagree" means to the operator.
    """
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    # See `_quiet_fleet` below: the census is the seam since #1881.
    monkeypatch.setattr(transport, "poller_census",
                        lambda: transport.empty_census(True))
    monkeypatch.setattr(transport, "ps_scan_supported", lambda: True)
    monkeypatch.setattr(transport, "RESOLVED", naming.resolve(STALE_OVERRIDE))

    body = [line[len("radar: "):] for line in radar.channel_banner()]
    assert body, "the named channel produced no disclosure at all"

    assert dispatcher.cmd_list() == 0
    watches_out = capsys.readouterr().out
    for line in body:
        assert f"watches: {line}" in watches_out, (line, watches_out)


# --- watches ----------------------------------------------------------------

def _quiet_fleet(monkeypatch, tmp_path) -> None:
    """`poller_census`, not `scan_poller_pids` — see the note in #1502's copy.

    The board renders all three of the scan's buckets since #1881; stubbing the
    this-channel one alone leaves the other two reading the real process table,
    and the banner assertions below then depend on whether anything happens to
    be polling on this machine.
    """
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(transport, "poller_census",
                        lambda: transport.empty_census(True))
    monkeypatch.setattr(transport, "ps_scan_supported", lambda: True)


def test_watches_names_the_channel_it_is_a_board_of(
        monkeypatch, tmp_path, capsys) -> None:
    _quiet_fleet(monkeypatch, tmp_path)
    monkeypatch.setattr(transport, "RESOLVED", naming.resolve(NAMED))
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert "oss" in out, out
    assert naming.NAME_ENV in out, out


def test_watches_discloses_a_stale_override_on_a_named_channel(
        monkeypatch, tmp_path, capsys) -> None:
    _quiet_fleet(monkeypatch, tmp_path)
    monkeypatch.setattr(transport, "RESOLVED", naming.resolve(STALE_OVERRIDE))
    assert dispatcher.cmd_list() == 0
    out = capsys.readouterr().out
    assert naming.SOCK_ENV in out, out
    assert "overrides the name" in out, out


def test_watches_on_a_default_channel_gains_no_banner(
        monkeypatch, tmp_path, capsys) -> None:
    _quiet_fleet(monkeypatch, tmp_path)
    monkeypatch.setattr(transport, "RESOLVED", naming.resolve({}))
    assert dispatcher.cmd_list() == 0
    assert naming.NAME_ENV not in capsys.readouterr().out


# --- radar ------------------------------------------------------------------

def _tiers(monkeypatch) -> None:
    """Radar refuses with no tiers configured; give it one that says nothing."""
    tier = types.SimpleNamespace(
        RADAR_OPTIONS=set(), RADAR_QUIET_DEFAULT=True,
        radar_report=lambda opts: ([], True),
        radar_state=lambda opts: [])
    monkeypatch.setenv(radar.TIERS_ENV, FAKE_TIERS)
    monkeypatch.setattr(radar, "_tier_module", lambda n: tier if n == "fake" else None)


def test_radar_state_names_the_channel_the_board_is_about(
        monkeypatch, tmp_path, capsys) -> None:
    """`radar:--state` is the read-only route and the one an operator reaches
    for when they suspect something, so it must carry the name too."""
    _quiet_fleet(monkeypatch, tmp_path)
    _tiers(monkeypatch)
    monkeypatch.setattr(transport, "RESOLVED", naming.resolve(STALE_OVERRIDE))
    assert radar.state_main("") == 0
    out = capsys.readouterr().out
    assert "oss" in out, out
    assert naming.SOCK_ENV in out, out


def test_radar_names_the_channel_on_the_spawning_route_too(
        monkeypatch, tmp_path, capsys) -> None:
    _quiet_fleet(monkeypatch, tmp_path)
    _tiers(monkeypatch)
    monkeypatch.setattr(transport, "RESOLVED", naming.resolve(NAMED))
    assert radar.main(["radar"]) == 0
    out = capsys.readouterr().out
    assert "oss" in out, out
    assert naming.NAME_ENV in out, out


def test_the_radar_banner_speaks_in_radars_own_voice(monkeypatch) -> None:
    """Every line radar writes is prefixed `radar:`, so a reader can tell the
    tool's words from a tier's - and from a state file's."""
    monkeypatch.setattr(transport, "RESOLVED", naming.resolve(NAMED))
    lines = radar.channel_banner()
    assert lines
    assert all(line.startswith("radar: ") for line in lines), lines


def test_radar_on_a_default_channel_gains_no_banner(monkeypatch) -> None:
    monkeypatch.setattr(transport, "RESOLVED", naming.resolve({}))
    assert radar.channel_banner() == []


# --- channel:health, the `unknown` arm --------------------------------------

def _unprobeable(monkeypatch) -> None:
    monkeypatch.setattr(
        channel, "probe_socket",
        lambda _path: ("unknown", "OSError connecting to /nope"))


def test_the_unprobeable_arm_says_it_declined_to_ask_about_a_holder(
        monkeypatch) -> None:
    _unprobeable(monkeypatch)
    rc, report = channel.health("/nope")
    assert rc == channel.RC_UNKNOWN
    assert "socket-holder NOT asked" in report, report


def test_the_unprobeable_arm_gives_the_reason_it_declined(monkeypatch) -> None:
    _unprobeable(monkeypatch)
    _rc, report = channel.health("/nope")
    assert "same connect" in report, report


def test_the_unprobeable_arm_still_claims_no_holder(monkeypatch) -> None:
    """#1476's constraint, kept: `peer_pid` connects, the connect just failed,
    and this arm must not name a holder it never asked for."""
    _unprobeable(monkeypatch)
    _rc, report = channel.health("/nope")
    assert "socket-holder: pid" not in report, report
    assert "socket-holder NOT resolved" not in report, report


def test_the_change_is_findable() -> None:
    assert_change_is_findable(1495)
