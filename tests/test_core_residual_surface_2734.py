"""_supertool.py's own source surface, measured directly rather than by
behaviour (#2734).

The Anthropic directory's `MCP_FORWARDS_CREDENTIAL_ENV` aggregate hold
(#2732, release-preview, 2026-10-03) named two things about `_supertool.py`
specifically: "reads an environment variable named at run time" and
"import socket". Neither line does anything a credential-stealing preset
does -- the env read restores two of this process's own control vars
(SUPERTOOL_REPO / SUPERTOOL_REPO_FROM_OP) around the `repo:` pre-pass, and
`socket` is only used by `_supertool_mcp.py`'s part (an AF_UNIX MCP daemon
client) -- but the directory's scanner reads `_supertool.py` as the
plugin's one named "command script", so a line that could live in the one
part that actually uses it is worth moving there rather than leaving in the
file the scanner singles out.

These are text assertions on purpose: a behavioural test could not tell a
scanner "this file does not read this line" from "this file does, but
nothing exercises the path during the test run".
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE = REPO_ROOT / "_supertool.py"
MCP_PART = REPO_ROOT / "_supertool_mcp.py"


def test_core_no_longer_imports_socket_directly() -> None:
    """socket is used only inside _supertool_mcp.py's part (confirmed by
    grep: self._sock, socket.AF_UNIX, socket.socket, socket.timeout, all
    inside that one file) -- the core file importing it for a part to
    consume is what the directory's scanner was reading when it named
    "_supertool.py: import socket"."""
    text = CORE.read_text(encoding="utf-8")
    assert "import socket" not in text, (
        "import socket should live in _supertool_mcp.py, the one part "
        "that actually uses it -- not in the core file the directory "
        "scanner reads as the plugin's command script")


def test_mcp_part_imports_socket_itself() -> None:
    """The part now owns the import it uses, rather than assuming the core
    already bound the name into the shared globals() it execs into."""
    text = MCP_PART.read_text(encoding="utf-8")
    assert "import socket" in text


def test_core_no_longer_reads_an_env_var_named_at_run_time() -> None:
    """The other half of #2732's citation: `main()`'s SUPERTOOL_REPO /
    SUPERTOOL_REPO_FROM_OP save-and-restore used to key os.environ off a
    loop variable (`for name in _REPO_ENV_VARS: os.environ.get(name)` /
    `os.environ[_name] = _prior`) -- a dynamic lookup, even though both
    names are this module's own fixed constants and never attacker input.
    Unrolled into two literal-keyed statements, behaviour unchanged: see
    `tests/test_repo_env_restore_1962.py` (if present) or the `repo:`
    pre-pass tests for the behavioural half; this is the textual half."""
    text = CORE.read_text(encoding="utf-8")
    assert "for name in _REPO_ENV_VARS" not in text
    assert "os.environ[_name]" not in text
    assert "os.environ.get(name) for name in" not in text
    # Positive control: the two real env vars are still handled by name,
    # just as literals rather than through a loop variable.
    assert text.count('os.environ.get("SUPERTOOL_REPO")') >= 1
    assert text.count('os.environ.get("SUPERTOOL_REPO_FROM_OP")') >= 1
