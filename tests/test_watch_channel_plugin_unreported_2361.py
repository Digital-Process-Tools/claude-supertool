"""`claude mcp get` no longer reports a plugin-provided server at all (#2361).

Measured on Claude Code 2.1.261: a plugin-provided `claude-channel` consumer
was loaded, running, and holding its socket (`lsof`/`ps` confirmed the pid),
while `claude mcp get -- plugin:supertool:claude-channel` exited 1 with
`CLAUDE_UNKNOWN_SERVER` text -- the same exit code and the same text a
genuinely absent server produces. `_configured` read that as the strongest of
its three answers, `False`, for a server that was demonstrably configured.

#2182 built both collision gates on the premise that `claude mcp get` is the
authority on whether the harness loaded a server not on disk. That premise
does not hold for a `plugin:`-qualified name on this harness version: the
harness's own negative surface for that population is not reliable, so a
"not found" response about a `plugin:`-qualified name must not collapse to a
confident `False` -- it is the admission, `None`, same as every other lookup
this file cannot settle.

The positive control matters as much as the fix: a genuinely absent,
non-`plugin:`-qualified name must still read `False`. That is #2182's own
case and nothing here may regress it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import channel  # noqa: E402
from _changelog_findable import assert_change_is_findable  # noqa: E402

# Verbatim from the issue's own repro (Claude Code 2.1.261).
UNKNOWN_PLUGIN_SERVER = (
    'No MCP server named "plugin:supertool:claude-channel". Configured '
    "servers: claude.ai Gmail, claude.ai Google Calendar, claude.ai Google "
    "Drive, oss-channel\n"
)

UNKNOWN_ORDINARY_SERVER = (
    'No MCP server named "definitely-not-a-real-server". Configured servers: '
    "claude.ai Gmail, oss-channel\n"
)


def _mcp_get(monkeypatch, stdout: str, returncode: int = 1):
    def run(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, returncode, stdout=stdout.encode("utf-8"))
    monkeypatch.setattr(channel.subprocess, "run", run)


def test_an_unreported_plugin_qualified_server_is_could_not_tell(monkeypatch):
    """The exact shape #2361 measured: `rc=1`, the recognised unknown-server
    text, and a name that asserts it is plugin-provided. That combination is
    not distinguishable here from a genuinely absent name, so it must not be
    a finding at all -- it is the admission."""
    _mcp_get(monkeypatch, UNKNOWN_PLUGIN_SERVER)
    answer, why = channel._configured("plugin:supertool:claude-channel")
    assert answer is None, (answer, why)
    assert why, "the admission must carry a reason a caller can render"


def test_control_a_genuinely_absent_ordinary_name_is_still_false(monkeypatch):
    """Positive control on the identical response shape: a name that does not
    claim to be plugin-provided keeps #2182's strongest answer."""
    _mcp_get(monkeypatch, UNKNOWN_ORDINARY_SERVER)
    answer, why = channel._configured("definitely-not-a-real-server")
    assert answer is False, (answer, why)


def test_the_change_is_findable_from_the_changelog():
    assert_change_is_findable("2361")
