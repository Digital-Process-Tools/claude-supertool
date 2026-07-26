#!/usr/bin/env python3
"""Cursor Witness notifier — emits supertool op events to a UDS socket.

The companion VSCode/Cursor extension listens on the socket and opens the file
(jumps to line) so the human can watch the agent work in their editor.

Wired via .supertool.json:

    "notifiers": {
      "cursor-witness": {
        "cmd": "python3 {supertool_dir}/notifiers/cursor-witness/notify.py {op} {file} {line}",
        "match": "*",
        "hooks_into": ["edit", "replace", "replace_lines", "paste", "append", "vim"]
      }
    }

Silent when no listener bound — never breaks the parent supertool call.

Socket path: /tmp/supertool-witness-<sha1(cwd)[:12]>.sock
Override with SUPERTOOL_WITNESS_SOCKET env var.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import time


def socket_path() -> str:
    override = os.environ.get("SUPERTOOL_WITNESS_SOCKET")
    if override:
        return override
    cwd = os.path.abspath(os.getcwd())
    h = hashlib.sha1(cwd.encode()).hexdigest()[:12]
    return f"/tmp/supertool-witness-{h}.sock"


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        return 0  # silent — bad invocation just exits clean
    op = argv[1]
    file_path = argv[2]
    line_raw = argv[3] if len(argv) > 3 else ""
    line_end_raw = argv[4] if len(argv) > 4 else ""
    before_file = argv[5] if len(argv) > 5 else ""

    def _maybe_int(s: str) -> int | None:
        return int(s) if s and s.isdigit() else None

    payload = {
        "op": op,
        "file": os.path.abspath(file_path) if file_path else "",
        "line": _maybe_int(line_raw),
        "line_end": _maybe_int(line_end_raw),
        "before_file": before_file if before_file and os.path.exists(before_file) else "",
        "ts": time.time(),
        "cwd": os.path.abspath(os.getcwd()),
    }
    sock_path = socket_path()
    if not os.path.exists(sock_path):
        return 0  # no listener — silent

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)  # don't hang the parent
        s.connect(sock_path)
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        s.close()
    except OSError:
        return 0  # listener crashed mid-write — silent
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
