"""`mcp.<name>.cmd` missing must say so, not raise NameError (#666).

Found by pointing ruff at this repo for the first time — `F821 Undefined name
`name``, in `presets/mcp/daemon.py`, on the branch that reports a
misconfigured MCP server:

    cmd = spec.get("cmd")
    if not cmd:
        sys.exit(f"daemon: mcp.{name}.cmd missing")

`_serve_owned` never took a `name` parameter. `serve()` has one and did not
pass it, so the f-string referenced a global that does not exist and the
branch raised `NameError: name 'name' is not defined` — from a daemon child
whose stdout nobody reads — instead of printing the one sentence that would
have told the operator which entry in `.supertool.json` was wrong.

This is the failure mode that survives longest: it is on an error path, so
every green run walks past it, and the only person who ever reaches it is
already debugging something else. Nothing else in the suite covers it, which
is precisely why a linter found it and 5,300 tests did not.

The check is on the real function rather than on a stub. `_serve_owned` opens
and binds its socket before it looks at `cmd`, so reaching the branch means
the preceding half actually ran — a mock would have proved only that a mock
returns what it was told to.
"""

from __future__ import annotations

import importlib.util
import os
import socket
from pathlib import Path

import pytest

DAEMON = Path(__file__).resolve().parent.parent / "presets" / "mcp" / "daemon.py"

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="no AF_UNIX on this Python build (Windows) — the daemon cannot bind",
)


def _load():
    spec = importlib.util.spec_from_file_location("mcp_daemon_666", DAEMON)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_missing_cmd_exits_naming_the_server(tmp_path: Path) -> None:
    mod = _load()
    dir_fd = os.open(str(tmp_path), os.O_RDONLY)
    try:
        with pytest.raises(SystemExit) as excinfo:
            mod._serve_owned({}, "py-lsp", "s.sock", "s.pid", dir_fd,
                             str(tmp_path / "s.sock"))
    finally:
        os.close(dir_fd)

    message = str(excinfo.value)
    assert "mcp.py-lsp.cmd" in message, (
        "the exit has to name the entry the operator must fix; got: " + message
    )
