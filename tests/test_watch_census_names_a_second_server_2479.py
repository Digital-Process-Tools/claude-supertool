"""The #2182 census line is about a DIFFERENT server than the tag (#2479).

`subscription()` renders both of these in one block:

    subscribed — session pid 9419 carries server:plugin:supertool:claude-channel, and the
    harness has a server configured under that name
    the harness has no claude-channel server configured, so a `.mcp.json` ...

Two names, two true sentences, and nothing in the rendering says so. The first
is the session tag, the second is `CONSUMER_SERVER`. Read at speed by someone
who had the file open, that is one server reported configured and unconfigured
two lines apart, and it was relayed to the maintainer as a defect.

The census is only asked when `name != CONSUMER_SERVER` (`channel.py:1764`), so
whenever it prints, the two names differ by construction and the wording can
say so with no new gate.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import channel  # noqa: E402
import naming  # noqa: E402

CONSUMER_ARGV = "bun /Users/x/notifiers/claude-channel/channel.ts"
SESSION_PID = 4242
TAGGED_OSS = ("claude /oss:tick "
              "--dangerously-load-development-channels server:oss-channel")


def _process_table(monkeypatch):
    table = {
        os.getpid(): (SESSION_PID, CONSUMER_ARGV, ""),
        SESSION_PID: (1, TAGGED_OSS, ""),
    }
    monkeypatch.setattr(channel, "_ps_fields",
                        lambda pid: table.get(pid, (None, "", f"no process {pid}")))


def _census(monkeypatch, standing):
    def answer(name, _budget=None):
        if name == channel.CONSUMER_SERVER:
            return standing, ""
        return True, ""
    monkeypatch.setattr(channel, "_configured", answer)


def _ask(tmp_path, monkeypatch):
    resolved = naming.resolve({naming.SOCK_ENV: str(tmp_path / "sock")})
    monkeypatch.setattr(channel, "RESOLVED", resolved)
    return channel.subscription(os.getpid(), path=resolved.sock, roots=[tmp_path],
                                resolved=resolved)


def _census_line(sub):
    """The one line carrying the census sentence, or None."""
    for line in sub.lines:
        if channel.CONSUMER_SERVER in line and "configured" in line:
            return line
    return None


def test_the_census_line_says_it_is_a_second_differently_named_server(
        tmp_path, monkeypatch):
    """The line must be unfoldable into the sentence above it: it names the
    server it is about, and says that name is not the tag's."""
    _process_table(monkeypatch)
    _census(monkeypatch, standing=False)
    sub = _ask(tmp_path, monkeypatch)
    assert sub.state == channel.SUB_SUBSCRIBED, (sub.state, " ".join(sub.lines))
    line = _census_line(sub)
    assert line is not None, sub.lines
    assert "different" in line.lower(), line
    assert channel.CONSUMER_SERVER in line, line


def test_control_the_tag_line_still_names_the_tag(tmp_path, monkeypatch):
    """Positive control: an assertion about the census wording also passes if
    the block stopped rendering the tag half. It must still be there, and it
    must still be the qualified name."""
    _process_table(monkeypatch)
    _census(monkeypatch, standing=False)
    sub = _ask(tmp_path, monkeypatch)
    joined = " ".join(sub.lines)
    assert "oss-channel" in joined, sub.lines
    assert "subscribed" in sub.lines[0], sub.lines[0]


def test_control_no_census_line_when_the_census_says_configured(
        tmp_path, monkeypatch):
    """`standing=True` takes an earlier return, so no census sentence renders
    at all. Without this, the first test passes on a build that deleted the
    line rather than reworded it."""
    _process_table(monkeypatch)
    _census(monkeypatch, standing=True)
    sub = _ask(tmp_path, monkeypatch)
    assert _census_line(sub) is None, sub.lines
