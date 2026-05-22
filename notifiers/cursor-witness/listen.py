#!/usr/bin/env python3
"""Reference UDS listener for the cursor-witness notifier.

Stand-in for the eventual VSCode/Cursor extension. Useful for:
  - End-to-end testing
  - Running a tail-style watch in a terminal while Max works
  - Piping events into another tool (jq, awk, etc.)

Usage:
    python3 notifiers/cursor-witness/listen.py
    python3 notifiers/cursor-witness/listen.py --socket /tmp/custom.sock

Output: one JSON event per line on stdout (NDJSON).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import socket
import sys


def default_socket() -> str:
    cwd = os.path.abspath(os.getcwd())
    h = hashlib.sha1(cwd.encode()).hexdigest()[:12]
    return f"/tmp/supertool-witness-{h}.sock"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default=default_socket(),
                    help="UDS path (default: derived from cwd)")
    args = ap.parse_args()

    sock_path = args.socket
    try: os.unlink(sock_path)
    except FileNotFoundError: pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(8)
    sys.stderr.write(f"cursor-witness listening on {sock_path}\n")
    sys.stderr.flush()

    try:
        while True:
            client, _ = srv.accept()
            try:
                buf = b""
                # Bounded read — each notifier sends one short JSON line
                while b"\n" not in buf and len(buf) < 8192:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                line = buf.split(b"\n", 1)[0]
                if line:
                    sys.stdout.write(line.decode("utf-8", errors="replace") + "\n")
                    sys.stdout.flush()
            finally:
                try: client.close()
                except OSError: pass
    except KeyboardInterrupt:
        pass
    finally:
        try: srv.close()
        except OSError: pass
        try: os.unlink(sock_path)
        except FileNotFoundError: pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
