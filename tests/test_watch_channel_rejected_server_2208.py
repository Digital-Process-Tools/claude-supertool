"""A server the harness REJECTED is not a server the harness has (#2208).

`claude mcp get <name>` exits **0** for a server declared in `.mcp.json` that
`disabledMcpjsonServers` has switched off, printing:

    claude-channel:
      Scope: Project config (shared via .mcp.json)
      Status: X Rejected (see disabledMcpjsonServers in settings)

`_configured` is read for its exit code (its own docstring says so), so that
came back `True`, `subscription()`'s `standing` gate from #2182 opened, and
`_dual_declaration_objection` demoted a healthy channel to `CANNOT DETERMINE`
on the strength of a declaration the harness had already thrown away. The
statusline caches the reading, so it rendered `ch?` permanently -- measured on
this repository 2026-09-03, against a consumer that was holding the socket and
had forwarded that session's own events.

The exit-code-only rule stays right for what it was written about. `Status: X
Failed to connect` is what *healthy* looks like for our own consumer, because
`claude mcp get`'s own probe instance refuses a live socket (#550), and reading
connection status would turn a correct FORWARDING into a false negative. That
is a claim about a **connection**. `Rejected (see disabledMcpjsonServers in
settings)` is a claim about a **load**: the harness saying it never started this
server at all, which is the exact question #2182 added the gate to ask.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import channel  # noqa: E402
import naming  # noqa: E402
from _changelog_findable import assert_change_is_findable  # noqa: E402

REJECTED = (
    "claude-channel:\n"
    "  Scope: Project config (shared via .mcp.json)\n"
    "  Status: ✘ Rejected (see disabledMcpjsonServers in settings)\n"
    "\n"
    "To remove this server, run: claude mcp remove claude-channel -s project\n"
)

FAILED_TO_CONNECT = (
    "oss-channel:\n"
    "  Scope: Local config\n"
    "  Status: ✘ Failed to connect\n"
)

CONNECTED = (
    "oss-channel:\n"
    "  Scope: Local config\n"
    "  Status: ✔ Connected\n"
)


def _mcp_get(monkeypatch, stdout: str, returncode: int = 0):
    """Stub the one subprocess `_configured` runs, returning `stdout` verbatim."""
    def run(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, returncode, stdout=stdout.encode("utf-8"))
    monkeypatch.setattr(channel.subprocess, "run", run)


# --- _configured: the third answer, and the two it must not disturb ----------

def test_a_rejected_server_is_not_configured(monkeypatch):
    """Exit 0, and the harness saying in as many words that it did not load it."""
    _mcp_get(monkeypatch, REJECTED)
    answer, why = channel._configured("claude-channel")
    assert answer is False, (answer, why)


def test_failed_to_connect_is_still_configured(monkeypatch):
    """The case the exit-code-only rule was written for. For our own consumer
    this is what healthy looks like -- the lookup's probe instance refusing a
    live socket (#550) -- so it must stay a positive answer."""
    _mcp_get(monkeypatch, FAILED_TO_CONNECT)
    answer, why = channel._configured("oss-channel")
    assert answer is True, (answer, why)


def test_a_connected_server_is_still_configured(monkeypatch):
    _mcp_get(monkeypatch, CONNECTED)
    answer, why = channel._configured("oss-channel")
    assert answer is True, (answer, why)


def test_the_word_rejected_outside_a_status_line_does_not_count(monkeypatch):
    """Narrower than a substring search. A server whose own name or command
    carries the word must not be read as a load the harness refused -- that
    prose is somebody else's config, and this file's neighbours already treat
    it as untrusted."""
    _mcp_get(monkeypatch, "rejected-webhooks:\n"
                          "  Scope: Local config\n"
                          "  Command: /opt/rejected/bin/server\n"
                          "  Status: ✔ Connected\n")
    answer, why = channel._configured("rejected-webhooks")
    assert answer is True, (answer, why)


# --- subscription(): the gate that produced `ch?` ----------------------------

def _process_table(monkeypatch, session_argv: str):
    consumer = "bun /Users/x/notifiers/claude-channel/channel.ts"
    session_pid = 4242
    table = {
        os.getpid(): (session_pid, consumer, ""),
        session_pid: (1, session_argv, ""),
    }
    monkeypatch.setattr(channel, "_ps_fields",
                        lambda pid: table.get(pid, (None, "", f"no process {pid}")))


def _mcp_json(tmp_path: Path) -> Path:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"claude-channel": {
            "command": "bun", "args": ["channel.ts"]}}}),
        encoding="utf-8")
    return tmp_path


TAGGED_OSS = ("claude /oss:tick "
              "--dangerously-load-development-channels server:oss-channel")


def test_a_rejected_standing_server_is_not_a_dual_declaration(tmp_path, monkeypatch):
    """The measured shape. `.mcp.json` declares `claude-channel` inheriting this
    socket, the session also carries `server:oss-channel`, and the harness has
    rejected the first. One declaration is in play, not two, so the collision
    branch must not run and the verdict must not be the third state."""
    _process_table(monkeypatch, TAGGED_OSS)
    answers = {"oss-channel": (True, ""), "claude-channel": (False, "")}
    monkeypatch.setattr(channel, "_configured",
                        lambda name, _budget=None: answers[name])
    _mcp_json(tmp_path)
    resolved = naming.resolve({})
    monkeypatch.setattr(channel, "RESOLVED", resolved)
    sub = channel.subscription(os.getpid(), path=resolved.sock, roots=[tmp_path],
                               resolved=resolved)
    assert sub.state != channel.SUB_UNKNOWN, (sub.state, sub.lines)


def test_a_standing_server_that_was_not_rejected_still_collides(tmp_path, monkeypatch):
    """#2051/#2133's finding, unchanged. A real standing declaration keeps
    demoting -- this narrows the gate, it does not open it."""
    _process_table(monkeypatch, TAGGED_OSS)
    monkeypatch.setattr(channel, "_configured",
                        lambda _name, _budget=None: (True, ""))
    _mcp_json(tmp_path)
    resolved = naming.resolve({})
    monkeypatch.setattr(channel, "RESOLVED", resolved)
    sub = channel.subscription(os.getpid(), path=resolved.sock, roots=[tmp_path],
                               resolved=resolved)
    assert sub.state == channel.SUB_UNKNOWN, (sub.state, sub.lines)


def test_change_is_findable():
    assert_change_is_findable(2208)
