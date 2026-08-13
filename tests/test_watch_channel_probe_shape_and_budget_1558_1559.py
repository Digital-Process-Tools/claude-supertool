"""The subscription probe's argv, its budget and its safety class (#1558, #1559).

#1550 added a probe that reads another process's argv and asks `claude mcp get`
about what it finds there. Three declarations did not move with the capability,
and a fourth defect is in the argv the probe builds.

**#1559 - the tag reaches `subprocess.run` unshaped.** `_channel_tags` breaks on
a *token* starting with `-`, which says nothing about the remainder after
`server:`. `server:--help` therefore arrived as the name `--help`, `claude mcp
get` parsed it as its own flag, exited 0, and the probe returned a definite
`subscribed`. Two halves to the repair and neither is sufficient alone: a `--`
terminator, so the callee's option parser cannot claim as a flag a value this
tool did not mean as one; and a shape check, so a token that cannot be a server
name is declined rather than asked about. The shape is deliberately wide -
`claude.ai Gmail` and `plugin:supertool:claude-channel` are both real configured
names on the machine this was measured on, so spaces, dots and colons are
legitimate and an over-narrow charset would turn a working setup into a refusal.

**#1558/2 - the probe could not fit inside its own op timeout.** `presets/watch.json`
left `channel` at 15s while the probe could spend `PS_TIMEOUT * 2` plus one full
`claude` lookup *per tag*. The op timeout always won, and because `health()`
returns one string at the end there was nothing to print: the reader got
supertool's bare `TIMEOUT` instead of the `CANNOT DETERMINE` the probe was
written to produce, in the one case where that is the right answer. The lookups
now share a declared budget, and this file pins that the declared op timeout
exceeds the probe's worst case - the arithmetic, not the number.

**#1558/3 - `read-only` on an op that spawns user-configured servers.** The name
comes from another process's argv, so what `claude mcp get` starts is whatever
the harness has configured under it. `docs/contributing.md` classifies by
consequence, and `read-only` means safe to invoke blind.

**#1558/1 does not hold, and this file pins the refutation.** The issue reads
`Status: X Failed to connect` under exit 0 as a dead server the probe ought to
report. Measured 2026-08-13 on the machine that filed it: `supertool-channel`
was at that moment holding the socket and forwarding 8 of 8 events, and `claude
mcp get supertool-channel` printed exactly that status line. It has to. The
lookup health-checks by spawning a *second* instance, and this repo's consumer
refuses to start a second one rather than unlinking a live incumbent (#550), so
for the only consumer this op exists to report on, `Failed to connect` is what
*healthy* looks like. Consulting it would convert a correct `FORWARDING` into a
false negative - the loud-for-quiet trade, run backwards. The exit code answers
the question the probe actually asks, which is whether the harness will accept
this tag or refuse it at startup (#1543), and that is the whole claim the report
makes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import channel  # noqa: E402
from _changelog_findable import assert_change_is_findable  # noqa: E402


class _Done:
    def __init__(self, returncode: int, stdout: bytes = b""):
        self.returncode = returncode
        self.stdout = stdout


class _Recorder:
    """Stands in for `subprocess.run`, and records what it was actually asked."""

    def __init__(self, returncode: int = 0, stdout: bytes = b""):
        self.calls: list[tuple[list[str], object]] = []
        self._done = _Done(returncode, stdout)

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs.get("timeout")))
        return self._done


@pytest.fixture()
def spawned(monkeypatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(channel.subprocess, "run", rec)
    return rec


# --- #1559: the shape check -------------------------------------------------

@pytest.mark.parametrize("name", ["--help", "-s", "-", "--"])
def test_a_flag_shaped_tag_is_declined_and_never_asked_about(spawned, name):
    """The defect exactly as filed. `claude mcp get --help` exits 0, so asking
    at all is what manufactured a definite `subscribed` off a flag."""
    answer, why = channel._configured(name)
    assert answer is None, (answer, why)
    assert why, "a declined lookup must carry its reason"
    assert spawned.calls == [], spawned.calls


@pytest.mark.parametrize("name", ["", "a\nb", "a\tb", "a\x00b", "a\x7fb"])
def test_a_tag_that_cannot_be_a_server_name_is_declined(spawned, name):
    """Empty, and anything carrying a control character. None of these can name
    a configured server, and all of them come out of somebody else's argv."""
    answer, why = channel._configured(name)
    assert answer is None, (answer, why)
    assert why
    assert spawned.calls == [], spawned.calls


@pytest.mark.parametrize("name", [
    "supertool-channel",
    "claude-channel",
    "claude.ai Gmail",
    "plugin:supertool:claude-channel",
])
def test_every_real_configured_name_is_still_asked_about(spawned, name):
    """The loud-for-quiet guard. All four are names `claude mcp list` printed on
    2026-08-13; a charset narrow enough to exclude any of them would turn a
    working setup into a refusal, which is the trade this repo forbids."""
    answer, why = channel._configured(name)
    assert answer is True, (answer, why)
    assert len(spawned.calls) == 1, spawned.calls


def test_the_lookup_terminates_its_own_options(spawned):
    """`--` before the name, so the callee's option parser cannot reinterpret a
    value this tool built from ambient process state. Necessary, and on its own
    not sufficient: without the shape check above, `--` merely converts the false
    positive into a definite *negative* off the same non-server token."""
    channel._configured("supertool-channel")
    argv, _timeout = spawned.calls[0]
    assert argv == [channel.CLAUDE_BIN, "mcp", "get", "--", "supertool-channel"], argv


def test_a_flag_shaped_tag_in_a_session_argv_is_cannot_determine(monkeypatch):
    """End to end through the verdict, which is where #1559 was visible: a
    definite `FORWARDING` off `server:--help`. Declined, not answered - a tag
    this reader will not ask about is not a tag the harness has refused."""
    tags, why = channel._channel_tags(
        "claude --dangerously-load-development-channels server:--help")
    assert tags == ["--help"], (tags, why)
    answer, ask_why = channel._configured(tags[0])
    assert answer is None, (answer, ask_why)


# --- #1558/1: the refutation ------------------------------------------------

def test_a_failed_connect_under_exit_zero_is_still_configured(monkeypatch):
    """#1558's first finding, refused. This is the output of `claude mcp get
    supertool-channel` on 2026-08-13 while that same server was holding the
    socket and forwarding 8 of 8 events. `Failed to connect` is what a healthy
    singleton consumer reports to a lookup, because the lookup spawns a second
    instance and #550 makes it refuse."""
    live = _Recorder(0, (
        "supertool-channel:\n  Scope: Local config\n"
        "  Status: X Failed to connect\n"
        "  Issue: -32000: MCP error -32000: Connection closed\n").encode())
    monkeypatch.setattr(channel.subprocess, "run", live)
    answer, why = channel._configured("supertool-channel")
    assert answer is True, (answer, why)


# --- #1558/2: the budget ----------------------------------------------------

def test_the_declared_op_timeout_exceeds_the_probes_worst_case():
    """The arithmetic, not the number. A probe that cannot answer inside its own
    op timeout can never reach its third state: `health()` returns one string at
    the end, so the reader gets supertool's bare `TIMEOUT` with an empty body."""
    ops = json.loads((REPO / "presets" / "watch.json").read_text(encoding="utf-8"))
    declared = ops["ops"]["channel"]["timeout"]
    assert declared > channel.SUBSCRIPTION_WORST_CASE, (
        declared, channel.SUBSCRIPTION_WORST_CASE)


def test_the_worst_case_is_the_sum_of_the_probes_own_budgets():
    assert channel.SUBSCRIPTION_WORST_CASE == (
        channel.PS_TIMEOUT * 2 + channel.MCP_LOOKUP_BUDGET)


def test_a_spent_budget_declines_the_remaining_tags_rather_than_asking(monkeypatch):
    """Three tags, a budget one lookup wide. The tags that were never asked
    about land in the third state naming the budget - not dropped, and not
    reported as servers the harness has never heard of."""
    clock = [1000.0]
    monkeypatch.setattr(channel, "_now", lambda: clock[0])
    asked: list[str] = []

    def lookup(name, timeout=None):
        asked.append(name)
        clock[0] += channel.MCP_LOOKUP_BUDGET
        return None, "the lookup failed"

    monkeypatch.setattr(channel, "_configured", lookup)
    monkeypatch.setattr(channel, "_ps_fields", lambda pid: (
        (7, "bun channel.ts", "") if pid == 99 else
        (1, "claude --dangerously-load-development-channels "
            "server:a server:b server:c", "")))
    sub = channel.subscription(99)
    assert sub.state == channel.SUB_UNKNOWN, sub
    assert asked == ["a"], asked
    text = "\n".join(sub.lines)
    assert "server:b" in text and "server:c" in text, text
    assert "budget" in text, text


def test_the_budget_shrinks_each_lookups_own_timeout(monkeypatch):
    """A per-lookup timeout that ignores what is left of the budget puts the
    total back over the op timeout on the second tag."""
    clock = [1000.0]
    monkeypatch.setattr(channel, "_now", lambda: clock[0])
    handed: list[float] = []

    def lookup(name, timeout=None):
        handed.append(timeout)
        clock[0] += channel.MCP_LOOKUP_BUDGET / 2
        return False, ""

    monkeypatch.setattr(channel, "_configured", lookup)
    monkeypatch.setattr(channel, "_ps_fields", lambda pid: (
        (7, "bun channel.ts", "") if pid == 99 else
        (1, "claude --dangerously-load-development-channels "
            "server:a server:b", "")))
    channel.subscription(99)
    assert len(handed) == 2, handed
    assert handed[1] < handed[0], handed
    assert sum(handed) <= channel.MCP_LOOKUP_BUDGET + channel.CLAUDE_TIMEOUT, handed


# --- #1558/3: the safety class ----------------------------------------------

def test_channel_is_not_declared_read_only():
    """It spawns whatever the harness has configured under a name taken from
    another process's argv. `read-only` means safe to invoke blind."""
    ops = json.loads((REPO / "presets" / "watch.json").read_text(encoding="utf-8"))
    assert ops["ops"]["channel"]["safety"] == "acts"


# --- documentation ----------------------------------------------------------

def test_the_change_is_findable():
    assert_change_is_findable(1558)
    assert_change_is_findable(1559)
