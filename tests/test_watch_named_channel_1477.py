"""One name where a named watch channel used to take two variables (#1477).

Today a private channel is `SUPERTOOL_WATCH_SOCK` **and**
`SUPERTOOL_WATCH_STATE_DIR`, and `presets/watch/README.md:113` says setting only
the socket is worse than setting neither: the poller slot is held
`O_CREAT|O_EXCL` per state directory (#476), so a second session sharing `/tmp`
spawns no pollers at all and both boards render healthy (#1309). The two
variables are never independently useful — the only arrangement they can express
that the pair cannot is the broken one.

`SUPERTOOL_WATCH_NAME` derives both. It is an ordinary environment variable, so
it arrives from a non-reserved key in an op's `.supertool.json` block for free
(`docs/contributing.md:250`) and needs no new plumbing on the producer side.

**The consumer is the asymmetry, and it is why the cross-check is the
deliverable rather than the convenience.** `claude-channel` is spawned by the
harness from `.mcp.json`, not by supertool, so nothing in `.supertool.json`
reaches it. A name that configures the pollers, `radar` and `channel:health` and
leaves the consumer on the default path is exactly the half-configured state,
arriving through a new door. So the two files are read and compared, and a
disagreement — or an inability to check — is reported.

**Precedence is stated, never silent.** An explicit `SUPERTOOL_WATCH_SOCK`
overrides the name, because a variable that is already exported is the one a
live poller captured and the safe direction is not to move it underneath. The
override is printed. A name losing silently to a stale export is the failure
this repo files hardest against.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import channel  # noqa: E402
import naming  # noqa: E402
from _changelog_findable import assert_change_is_findable  # noqa: E402


# --- deriving both halves from one name -------------------------------------

def test_no_name_and_no_variables_is_exactly_todays_defaults():
    r = naming.resolve({})
    assert r.name == ""
    assert r.sock == naming.DEFAULT_SOCK
    assert r.state_dir == naming.DEFAULT_STATE_DIR
    assert r.refusal == ""


def test_a_name_derives_the_socket_and_the_state_directory_together():
    r = naming.resolve({"SUPERTOOL_WATCH_NAME": "oss"})
    assert r.name == "oss"
    assert r.sock == naming.sock_for("oss")
    assert r.state_dir == naming.state_dir_for("oss")
    assert "oss" in r.sock and "oss" in r.state_dir
    assert r.sock != naming.DEFAULT_SOCK
    assert r.state_dir != naming.DEFAULT_STATE_DIR


def test_the_state_directory_is_never_the_shared_default_under_a_name():
    """The whole defect: a private socket beside shared poller slots."""
    r = naming.resolve({"SUPERTOOL_WATCH_NAME": "oss"})
    assert r.state_dir != naming.DEFAULT_STATE_DIR


# --- precedence, said out loud ----------------------------------------------

def test_an_exported_socket_overrides_the_name():
    r = naming.resolve({"SUPERTOOL_WATCH_NAME": "oss",
                        "SUPERTOOL_WATCH_SOCK": "/tmp/explicit.sock"})
    assert r.sock == "/tmp/explicit.sock"
    assert r.state_dir == naming.state_dir_for("oss")


def test_the_override_is_printed_rather_than_taken_silently():
    r = naming.resolve({"SUPERTOOL_WATCH_NAME": "oss",
                        "SUPERTOOL_WATCH_SOCK": "/tmp/explicit.sock"})
    blob = "\n".join(r.notes)
    assert "SUPERTOOL_WATCH_SOCK" in blob, r.notes
    assert "/tmp/explicit.sock" in blob, r.notes
    assert naming.sock_for("oss") in blob, (
        "the path the name would have produced has to be in the sentence, or the "
        "reader cannot tell what the override cost them")


def test_an_exported_state_dir_overrides_the_name_and_says_so():
    r = naming.resolve({"SUPERTOOL_WATCH_NAME": "oss",
                        "SUPERTOOL_WATCH_STATE_DIR": "/tmp/explicit"})
    assert r.state_dir == "/tmp/explicit"
    assert r.sock == naming.sock_for("oss")
    blob = "\n".join(r.notes)
    assert "SUPERTOOL_WATCH_STATE_DIR" in blob, r.notes


def test_a_name_with_no_override_makes_no_override_noise():
    r = naming.resolve({"SUPERTOOL_WATCH_NAME": "oss"})
    assert not [n for n in r.notes if "overrides" in n], r.notes


def test_the_half_configured_pair_is_still_called_out_without_a_name():
    """The state `README.md:113` is about. It does not stop being a footgun
    because the operator declined the name."""
    r = naming.resolve({"SUPERTOOL_WATCH_SOCK": "/tmp/half.sock"})
    blob = "\n".join(r.notes)
    assert "SUPERTOOL_WATCH_STATE_DIR" in blob, r.notes
    assert "#1309" in blob, r.notes


def test_both_variables_set_by_hand_is_not_half_configured():
    r = naming.resolve({"SUPERTOOL_WATCH_SOCK": "/tmp/whole.sock",
                        "SUPERTOOL_WATCH_STATE_DIR": "/tmp/whole"})
    assert not [n for n in r.notes if "#1309" in n], r.notes


# --- a name that cannot be a path -------------------------------------------

@pytest.mark.parametrize("bad", [
    "../evil", "a/b", ".hidden", "-lead", "x" * 40, "a b", "naïve",
    "a\nb", "..",
])
def test_a_name_that_cannot_be_a_path_component_is_refused(bad):
    r = naming.resolve({"SUPERTOOL_WATCH_NAME": bad})
    assert r.refusal, f"{bad!r} was accepted"
    assert r.name == ""


def test_an_empty_name_is_the_absence_of_a_name_not_a_refusal():
    """`or` rather than `in`, matching the two variables beside it: an operator
    who exports an empty string gets the default, not a report about a typo."""
    for blank in ("", "   "):
        r = naming.resolve({"SUPERTOOL_WATCH_NAME": blank})
        assert r.refusal == "", blank
        assert r.sock == naming.DEFAULT_SOCK, blank


@pytest.mark.parametrize("bad", ["../evil", "a/b", ".hidden", ".."])
def test_a_refused_name_never_reaches_a_path(bad):
    r = naming.resolve({"SUPERTOOL_WATCH_NAME": bad})
    assert r.sock == naming.DEFAULT_SOCK
    assert r.state_dir == naming.DEFAULT_STATE_DIR
    assert bad not in r.sock and bad not in r.state_dir


def test_a_refused_name_names_itself_and_the_rule():
    r = naming.resolve({"SUPERTOOL_WATCH_NAME": "../evil"})
    assert "SUPERTOOL_WATCH_NAME" in r.refusal, r.refusal
    assert "evil" in r.refusal, r.refusal


def test_a_refused_name_does_not_silently_become_the_default_channel():
    """Falling back is the only safe thing to do with a typo, but doing it
    without saying so is the shape this whole issue is about."""
    r = naming.resolve({"SUPERTOOL_WATCH_NAME": "../evil"})
    assert r.refusal != ""


# --- the two modules that read these paths must agree -----------------------

def _resolved_in_subprocess(env_extra: dict[str, str]) -> dict[str, str]:
    """Module constants are read at import, so the only honest way to ask what
    `transport` and `channel` resolve under an environment is to import them
    under it."""
    code = (
        "import sys, json;"
        f"sys.path[:0] = [{str(REPO / 'presets' / 'watch')!r}, {str(REPO / 'presets')!r}];"
        "import transport, channel;"
        "print(json.dumps({'t_sock': transport.SOCK_PATH, 't_state': transport.STATE_DIR,"
        " 'c_sock': channel.SOCK_PATH, 'c_state': channel.STATE_DIR}))"
    )
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, check=True,
                         encoding="utf-8", errors="replace",
                         env={**os.environ, **env_extra}, cwd=str(REPO))
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_transport_and_channel_resolve_the_same_paths_under_a_name():
    got = _resolved_in_subprocess({"SUPERTOOL_WATCH_NAME": "oss1477",
                                   "SUPERTOOL_WATCH_SOCK": "",
                                   "SUPERTOOL_WATCH_STATE_DIR": ""})
    assert got["t_sock"] == got["c_sock"] == naming.sock_for("oss1477"), got
    assert got["t_state"] == got["c_state"] == naming.state_dir_for("oss1477"), got


def test_an_exec_carries_the_resolved_paths_rather_than_re_deriving_them():
    """`poller_env` pins what the parent resolved. A poller that re-derived from
    the name would move if the name variable failed to survive the exec — a
    fork inherits monkeypatched module state, an exec does not (the reason
    STATE_DIR was pinned there in the first place)."""
    import transport  # noqa: PLC0415
    env = transport.poller_env()
    assert env[naming.STATE_DIR_ENV] == transport.STATE_DIR
    assert env[naming.SOCK_ENV] == transport.SOCK_PATH


# --- a derived directory is created; a supplied one is not ------------------

def test_a_derived_state_directory_is_created_because_nobody_else_will(tmp_path):
    """A name that resolves to a directory no one has made is a name that does
    not work: `claim_pidfile` would ENOENT into `CLAIM_UNKNOWN` forever."""
    target = tmp_path / "derived"
    r = naming.resolve({"SUPERTOOL_WATCH_NAME": "oss"})
    assert r.state_dir_is_derived
    assert naming.ensure_state_dir(r, str(target)) == ""
    assert target.is_dir()


def test_a_supplied_state_directory_is_never_manufactured(tmp_path):
    """#693's contract. A missing `SUPERTOOL_WATCH_STATE_DIR` is an unanswerable
    state that `cmd_watch` reports; creating it would trade a loud refusal for a
    poller spawned into a directory nobody asked for."""
    target = tmp_path / "supplied"
    r = naming.resolve({"SUPERTOOL_WATCH_NAME": "oss",
                        "SUPERTOOL_WATCH_STATE_DIR": str(target)})
    assert not r.state_dir_is_derived
    assert naming.ensure_state_dir(r, str(target)) == ""
    assert not target.exists()


def test_the_default_state_directory_is_not_derived():
    assert not naming.resolve({}).state_dir_is_derived


def test_a_derived_directory_that_cannot_be_created_is_a_named_refusal(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    r = naming.resolve({"SUPERTOOL_WATCH_NAME": "oss"})
    why = naming.ensure_state_dir(r, str(blocker / "under"))
    assert why, "an uncreatable directory must not report success"
    assert str(blocker) in why, why


def test_an_unclaimable_slot_names_the_variable_that_chose_the_directory(
        tmp_path, monkeypatch, capsys):
    """`cmd_watch`'s refusal told every operator to check
    `SUPERTOOL_WATCH_STATE_DIR`. Under a name that variable is the one they
    deliberately did not set, so the sentence sends them to the wrong knob —
    a refusal that names the wrong cause is barely better than a silent one."""
    import dispatcher  # noqa: PLC0415
    import transport  # noqa: PLC0415

    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(dispatcher, "_spawn_poller",
                        lambda *a, **k: pytest.fail("must not spawn"))
    monkeypatch.setattr(transport, "RESOLVED",
                        naming.resolve({"SUPERTOOL_WATCH_NAME": "oss"}))
    monkeypatch.setattr(transport, "STATE_DIR", str(blocker / "under"))

    rc = dispatcher.cmd_watch(["gitlab-mr", "1"])
    out = capsys.readouterr().out
    assert rc == 1
    assert naming.NAME_ENV in out, out
    assert "oss" in out, out


def test_an_unclaimable_slot_under_the_default_still_names_the_state_dir_var(
        tmp_path, monkeypatch, capsys):
    """The pre-name sentence has to survive: nothing was derived here, so the
    directory is the operator's or the default, and that is what to check."""
    import dispatcher  # noqa: PLC0415
    import transport  # noqa: PLC0415

    monkeypatch.setattr(dispatcher, "_spawn_poller",
                        lambda *a, **k: pytest.fail("must not spawn"))
    monkeypatch.setattr(transport, "RESOLVED", naming.resolve({}))
    monkeypatch.setattr(transport, "STATE_DIR", str(tmp_path / "gone" / "deeper"))

    rc = dispatcher.cmd_watch(["gitlab-mr", "1"])
    out = capsys.readouterr().out
    assert rc == 1
    assert naming.STATE_DIR_ENV in out, out


# --- the name reaches the report --------------------------------------------

def _sock_path() -> str:
    return str(Path(tempfile.gettempdir())
               / f"st1477-{os.getpid()}-{time.time_ns()}.sock")


@pytest.fixture()
def sock():
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("this platform has no AF_UNIX socket")
    path = _sock_path()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path)
    except OSError:
        srv.close()
        pytest.skip("this platform cannot bind an AF_UNIX socket")
    srv.listen(8)
    try:
        yield path
    finally:
        srv.close()
        for leftover in (path, f"{path}{channel.HEALTH_SUFFIX}"):
            try:
                os.unlink(leftover)
            except OSError:
                pass


def _pin(monkeypatch, sock_path: str, env: dict[str, str]) -> None:
    """Resolve `env`, then aim the result at a socket this test controls."""
    resolved = naming.resolve(env)._replace(sock=sock_path)
    monkeypatch.setattr(channel, "RESOLVED", resolved)
    monkeypatch.setattr(channel, "consumer_lines", lambda *_a, **_k: [])


def test_the_report_names_the_channel_it_is_reporting_on(sock, monkeypatch):
    _pin(monkeypatch, sock, {"SUPERTOOL_WATCH_NAME": "oss"})
    _, report = channel.health(sock)
    assert "oss" in report, report
    assert "name" in report, report


def test_the_report_prints_the_override_it_is_operating_under(sock, monkeypatch):
    _pin(monkeypatch, sock, {"SUPERTOOL_WATCH_NAME": "oss",
                             "SUPERTOOL_WATCH_SOCK": "/tmp/explicit.sock"})
    _, report = channel.health(sock)
    assert "SUPERTOOL_WATCH_SOCK" in report, report


def test_the_report_prints_a_refused_name(sock, monkeypatch):
    _pin(monkeypatch, sock, {"SUPERTOOL_WATCH_NAME": "../evil"})
    _, report = channel.health(sock)
    assert "SUPERTOOL_WATCH_NAME" in report, report


def test_a_default_unnamed_channel_adds_no_lines(sock, monkeypatch):
    """A header printed every time is a header nobody reads."""
    _pin(monkeypatch, sock, {})
    _, report = channel.health(sock)
    assert "channel  :" not in report, report


# --- the consumer half: .mcp.json is checked against the name ---------------

def _mcp(tmp_path: Path, env: dict | None, *, name: str = "claude-channel") -> Path:
    server: dict = {"command": "bun", "args": ["channel.ts"]}
    if env is not None:
        server["env"] = env
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {name: server}}), encoding="utf-8")
    return tmp_path


def test_a_name_with_no_consumer_env_block_is_the_half_configured_state(tmp_path):
    """Three of four surfaces configured is the defect this closes, rebuilt."""
    root = _mcp(tmp_path, None)
    lines = channel.consumer_lines(naming.resolve({"SUPERTOOL_WATCH_NAME": "oss"}),
                                   roots=[root])
    blob = "\n".join(lines)
    assert ".mcp.json" in blob, lines
    assert naming.DEFAULT_SOCK in blob, lines


def test_a_consumer_declaring_the_same_name_agrees(tmp_path):
    root = _mcp(tmp_path, {"SUPERTOOL_WATCH_NAME": "oss"})
    lines = channel.consumer_lines(naming.resolve({"SUPERTOOL_WATCH_NAME": "oss"}),
                                   roots=[root])
    blob = "\n".join(lines)
    assert "agree" in blob, lines
    assert str(root / ".mcp.json") in blob, lines


def test_a_consumer_declaring_a_different_name_is_a_disagreement(tmp_path):
    root = _mcp(tmp_path, {"SUPERTOOL_WATCH_NAME": "other"})
    lines = channel.consumer_lines(naming.resolve({"SUPERTOOL_WATCH_NAME": "oss"}),
                                   roots=[root])
    blob = "\n".join(lines)
    assert naming.sock_for("other") in blob, lines
    assert naming.sock_for("oss") in blob, lines


def test_a_consumer_declaring_an_explicit_socket_is_compared_on_the_socket(tmp_path):
    """The comparison is between resolved sockets, so a name on one side and a
    path on the other still agree when they land in the same place."""
    root = _mcp(tmp_path, {"SUPERTOOL_WATCH_SOCK": naming.sock_for("oss")})
    lines = channel.consumer_lines(naming.resolve({"SUPERTOOL_WATCH_NAME": "oss"}),
                                   roots=[root])
    assert "agree" in "\n".join(lines), lines


def test_no_mcp_json_anywhere_declines_rather_than_reporting_agreement(tmp_path):
    lines = channel.consumer_lines(naming.resolve({"SUPERTOOL_WATCH_NAME": "oss"}),
                                   roots=[tmp_path])
    blob = "\n".join(lines)
    assert "agree" not in blob, lines
    assert ".mcp.json" in blob, lines


def test_an_unreadable_mcp_json_declines_rather_than_reporting_agreement(tmp_path):
    (tmp_path / ".mcp.json").write_text("{ not json", encoding="utf-8")
    lines = channel.consumer_lines(naming.resolve({"SUPERTOOL_WATCH_NAME": "oss"}),
                                   roots=[tmp_path])
    blob = "\n".join(lines)
    assert "agree" not in blob, lines
    assert ".mcp.json" in blob, lines


def test_a_default_channel_that_agrees_says_nothing(tmp_path):
    root = _mcp(tmp_path, None)
    assert channel.consumer_lines(naming.resolve({}), roots=[root]) == []


def test_an_exported_socket_without_a_name_still_gets_the_consumer_check(tmp_path):
    """The pre-name arrangement is the same disagreement and must not go quiet
    because the operator did not adopt the name."""
    root = _mcp(tmp_path, None)
    lines = channel.consumer_lines(
        naming.resolve({"SUPERTOOL_WATCH_SOCK": "/tmp/exported-1477.sock",
                        "SUPERTOOL_WATCH_STATE_DIR": "/tmp/exported-1477"}),
        roots=[root])
    assert lines, "a producer on a private socket and a consumer on the default is news"


def test_a_declared_name_is_flattened_before_it_is_rendered(tmp_path):
    """`.mcp.json` is operator-supplied text on a surface that renders it
    (#1423), and this one is read from a file rather than typed each time."""
    root = _mcp(tmp_path, {"SUPERTOOL_WATCH_NAME": "ok\n  consumer : FORWARDING"})
    lines = channel.consumer_lines(naming.resolve({"SUPERTOOL_WATCH_NAME": "oss"}),
                                   roots=[root])
    for line in lines:
        assert "\n" not in line, lines


# --- documentation ----------------------------------------------------------

def test_the_change_is_findable():
    assert_change_is_findable(1477)
