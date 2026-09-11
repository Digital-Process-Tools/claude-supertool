"""#2478 — a session armed for a channel and given none must say so, loudly.

Measured 2026-09-09: a session started by `bin/oss-workspace`, with
`--dangerously-load-development-channels` on its argv and `SUPERTOOL_WATCH_NAME`
exported, got no consumer at all. The harness cached a connect failure, forked
nothing, and said nothing. Four pollers went on emitting into a socket that did
not exist for thirty-two minutes; one of the lost events was `checks_failed` on
an open pull request.

Every instrument was right and none of them was asked. `channel:health` says
`NOT DELIVERING` and `channel:probe` says `expect: nothing`, but both are ops a
session has to think to run, and a session that does not know its channel is
dead has no reason to run them. So the report has to arrive unasked, at the one
moment the session is already listening: session start.

`channel:stranded` is that report. It is deliberately NOT `channel:health`:

* health is `acts`-classed and spawns `claude mcp get` on its bound path, which
  a SessionStart hook must never do -- a hook that starts an MCP server to
  diagnose an MCP server is the shape #1558 was filed for.
* health answers about the socket right now, and at session start the consumer
  may not have bound yet. A missing socket at t=0 is not a finding.

This reads only what the PRODUCERS already wrote down: a poller records
`last_emit.state == "no-listener"` when its own send found nothing there. That
is a fact with a timestamp, not a race, and it needs no socket, no network and
no subprocess.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHANNEL = REPO / "presets" / "watch" / "channel.py"


def _run(args, state_dir, sock):
    """`channel.py` with its channel pointed at a temp directory."""
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "SUPERTOOL_WATCH_SOCK": str(sock),
        "SUPERTOOL_WATCH_STATE_DIR": str(state_dir),
    }
    return subprocess.run(
        [sys.executable, str(CHANNEL), *args],
        capture_output=True, text=True, timeout=30, env=env,
        encoding="utf-8", errors="replace",
    )


def _write_state(state_dir: Path, source: str, wid: str, emit_state: str, sock) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"supertool-watch-{source}__{wid}.state.json").write_text(
        json.dumps({
            "sock_path": str(sock),
            "last_emit": {"ts": "2026-09-09T14:51:41Z", "state": emit_state,
                          "detail": "recorded by the poller"},
            "last_event": {"ts": "2026-09-09T14:51:41Z", "source": source,
                           "id": wid, "event": "checks_failed"},
        }),
        encoding="utf-8",
    )


def test_a_stranded_watcher_is_reported_loudly(tmp_path: Path) -> None:
    """MUST FIRE. The whole point: events are being lost and the session is told."""
    state_dir, sock = tmp_path / "slots", tmp_path / "watch.sock"
    _write_state(state_dir, "github-pr", "2477", "no-listener", sock)

    result = _run(["stranded"], state_dir, sock)
    assert result.returncode != 0, (result.stdout, result.stderr)
    out = result.stdout
    assert "NOT DELIVERING" in out, out
    assert "github-pr" in out, out
    assert "2477" in out, out


def test_a_healthy_channel_says_nothing_at_all(tmp_path: Path) -> None:
    """MUST FIRE, and it is the half that decides whether this can ship.

    This runs on every session start of every user of this plugin, most of whom
    watch nothing. A line printed when there is nothing to say is a byte charged
    to every session forever -- `CLAUDE.md`'s own rule -- so silence has to be
    exact, not merely short.
    """
    state_dir, sock = tmp_path / "slots", tmp_path / "watch.sock"
    _write_state(state_dir, "github-pr", "2477", "accepted", sock)

    result = _run(["stranded"], state_dir, sock)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.strip() == "", repr(result.stdout)
    assert result.stderr.strip() == "", repr(result.stderr)


def test_a_channel_nobody_watches_says_nothing(tmp_path: Path) -> None:
    """MUST FIRE. No state directory at all is the ordinary state of a plugin
    user who has never run `watch`, and it is not a finding about anything."""
    state_dir, sock = tmp_path / "never-created", tmp_path / "watch.sock"

    result = _run(["stranded"], state_dir, sock)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.strip() == "", repr(result.stdout)


def test_the_silence_assertions_would_notice_output(tmp_path: Path) -> None:
    """MUST NOT FIRE -- positive control for the two silence tests above.

    Both rest on stdout being empty, and an op that crashed before printing
    anything would satisfy them just as well. This drives the same entry point
    to a state that MUST speak, so the assertions above are known to be
    distinguishing silence from muteness.
    """
    state_dir, sock = tmp_path / "slots", tmp_path / "watch.sock"
    _write_state(state_dir, "gh-branch", "master", "no-listener", sock)

    result = _run(["stranded"], state_dir, sock)
    assert result.stdout.strip() != "", "the op printed nothing even when stranded"


def test_it_never_reaches_for_claude_or_a_socket(tmp_path: Path) -> None:
    """MUST FIRE. A SessionStart hook must not spawn an MCP server to diagnose
    one (#1558), and must not depend on a socket that may not be bound yet.

    `PATH` in `_run` above holds no `claude`, and the socket path never exists
    in any of these tests. If either were reached for, the stranded arm could
    not answer -- so an answer here IS the assertion.
    """
    state_dir, sock = tmp_path / "slots", tmp_path / "watch.sock"
    _write_state(state_dir, "github-pr", "2477", "no-listener", sock)
    assert not sock.exists()

    result = _run(["stranded"], state_dir, sock)
    assert "github-pr" in result.stdout, (result.stdout, result.stderr)


def test_an_unknown_sub_op_names_stranded_too() -> None:
    """MUST FIRE. The error enumerates the sub-ops, and a fourth that is missing
    from it is a capability nobody reaches for -- the same reason the message
    was widened from two to three."""
    result = subprocess.run(
        [sys.executable, str(CHANNEL), "nonsense"],
        capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
    )
    assert result.returncode == 2, result
    assert "stranded" in result.stderr, result.stderr


def test_an_unreadable_state_file_with_no_other_watcher_stays_silent(tmp_path: Path) -> None:
    """MUST FIRE -- pins a deliberate design choice, not an oversight.

    `stranded_watchers` returns a `Stranded` row carrying `refusal` (not `""`)
    for a state file it could not read (#1191's own shape one layer over:
    corrupt JSON, a symlink, a non-UTF-8 name). `stranded_report` filters
    those rows out before deciding whether it has anything to say -- so a
    channel with only a tampered/corrupt state file on disk renders exactly
    as silent as a genuinely healthy one.

    That fold is intentional, argued in `stranded_report`'s own docstring:
    `channel:health` is where "I could not look" belongs, and shouting about
    an unreadable /tmp entry at every session start would train the reader to
    skip the block that matters. This test exists so that argument is pinned
    by a run rather than resting on the docstring's word alone -- #2478 self
    review, class A.
    """
    state_dir, sock = tmp_path / "slots", tmp_path / "watch.sock"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "supertool-watch-github-pr__2477.state.json").write_text(
        "{not valid json", encoding="utf-8")

    result = _run(["stranded"], state_dir, sock)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.strip() == "", repr(result.stdout)


def test_a_stranded_channel_does_not_falsely_report_the_listing_incomplete(
        tmp_path: Path) -> None:
    """MUST FIRE -- self-review finding (Explore reviewer, #2478).

    `channel:stranded` returns RC_NOT_DELIVERING=1 whenever it has something
    to report -- an answer, not a failure -- but `presets/watch.json`'s shared
    `channel` op declares no `exitStatus`, so the supertool dispatcher cannot
    tell that apart from a real refusal (this is already disclosed in the
    op's own description field). Batched into the same call as the
    `ops:session` listing, a stranded channel's non-zero exit made the WHOLE
    batch non-zero and printed the hook's "op listing is incomplete" line --
    false, since the listing had rendered completely -- at exactly the one
    moment #2478 exists to be noticed: a session with a stranded channel.

    This drives the real hook script (not `channel.py` directly) against a
    stub plugin binary that mimics the split behaviour: `introduction`/
    `output-format`/`ops:session` succeed, and a separate `channel:stranded`
    call reports something and exits 1. Both must be visible, and the false
    "incomplete" line must not be.
    """
    import os
    import subprocess

    hook = REPO / "hooks" / "session-start.sh"
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    (plugin_root / "supertool.py").write_text(
        "import sys\n"
        "argv = sys.argv[1:]\n"
        "if argv == ['introduction', 'output-format', 'ops:session']:\n"
        "    print('ONBOARD-OK')\n"
        "    sys.exit(0)\n"
        "elif argv == ['channel:stranded']:\n"
        "    print('CHANNEL NOT DELIVERING (stub)')\n"
        "    sys.exit(1)\n"
        "else:\n"
        "    print('UNEXPECTED ARGV: %r' % (argv,))\n"
        "    sys.exit(2)\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()

    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    result = subprocess.run(
        ["bash", str(hook)], cwd=str(project), env=env,
        capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
    )

    assert "ONBOARD-OK" in result.stdout, result.stdout
    assert "CHANNEL NOT DELIVERING (stub)" in result.stdout, result.stdout
    assert "op listing is incomplete" not in result.stdout, (
        "the listing rendered completely (ONBOARD-OK) but the hook still "
        "printed the incomplete-listing line, caused by channel:stranded's "
        "own non-failure exit code -- " + result.stdout
    )


def test_the_session_start_hook_asks_for_it() -> None:
    """MUST FIRE. The op existing and the hook calling it are two claims, and
    the first is worth nothing alone -- that is the whole defect this closes."""
    hook = (REPO / "hooks" / "session-start.sh").read_text(encoding="utf-8")
    assert "channel:stranded" in hook, (
        "hooks/session-start.sh does not ask for channel:stranded, so a session "
        "with a dead channel is still told nothing (#2478)"
    )
