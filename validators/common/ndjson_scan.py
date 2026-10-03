

















































from __future__ import annotations

import json
import socket
import time

from linebreaks import split_lines


def _enclosing_brace(text: str, marker: int) -> int | None:








    depth = 0
    i = marker - 1
    while i >= 0:
        ch = text[i]
        if ch == "}":
            depth += 1
        elif ch == "{":
            if depth == 0:
                return i
            depth -= 1
        i -= 1
    return None


class DesyncDetected(RuntimeError):
    pass

























INITIALIZE_ID = 1


def _scan(buf: bytes, want_id, own_ids: frozenset = frozenset((INITIALIZE_ID,))) -> tuple:





























    text = buf.decode("utf-8", errors="replace")
    decoder = json.JSONDecoder()
    search_from = 0
    saw_foreign_id = False
    while True:
        marker = text.find('"jsonrpc"', search_from)
        if marker == -1:
            return None, saw_foreign_id
        brace = _enclosing_brace(text, marker)
        if brace is None:
            search_from = marker + 1
            continue
        try:
            obj, _end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError:
            search_from = marker + 1
            continue
        if isinstance(obj, dict):
            oid = obj.get("id")
            if oid == want_id:
                return obj, saw_foreign_id
            if "id" in obj and oid not in own_ids:
                saw_foreign_id = True
        search_from = marker + 1


def find_response(buf: bytes, want_id) -> dict | None:





    obj, _saw_foreign_id = _scan(buf, want_id)
    return obj


def describe_buffer(buf: bytes, want_id) -> str:










    if not buf:
        return "no bytes received"
    n_markers = buf.count(b'"jsonrpc"')
    plural = "s" if n_markers != 1 else ""
    return (f"received {len(buf)} bytes ({n_markers} \"jsonrpc\" marker{plural}), "
            f"none decoded to id={want_id!r}")







DEFAULT_IDLE_TIMEOUT_S = 10.0


def read_daemon_log_tail(sock_path: str, n_lines: int = 8, max_chars: int = 400) -> str:










    try:
        with open(sock_path + ".log", "rb") as f:
            data = f.read()
    except OSError:
        return ""






    lines = [ln for ln in split_lines(data.decode("utf-8", errors="replace"))
             if ln.strip()]
    return " | ".join(lines[-n_lines:])[:max_chars]


def describe_timeout(buf: bytes, want_id, elapsed_s: float, sock_path: str) -> str:






    reason = describe_buffer(buf, want_id)
    if buf:




        preview = buf[:200].decode("utf-8", errors="replace")
        reason = f"{reason}; first bytes: {preview!r}"
    msg = f"no id={want_id!r} response after {elapsed_s:.1f}s ({reason})"
    log_tail = read_daemon_log_tail(sock_path)
    if log_tail:
        msg += f"; daemon log tail: {log_tail}"
    return msg


def receive_until(s: socket.socket, want_id, call_timeout: float, sock_path: str,
                   idle_timeout: float = DEFAULT_IDLE_TIMEOUT_S,
                   own_ids: frozenset = frozenset((INITIALIZE_ID,))) -> dict:






























    start = time.monotonic()
    deadline = start + call_timeout
    buf = b""
    last_byte_at = None
    saw_other_id = False
    while True:
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            break
        if last_byte_at is not None:
            remaining = min(remaining, (last_byte_at + idle_timeout) - now)
            if remaining <= 0:
                break
        s.settimeout(remaining)
        try:
            chunk = s.recv(65536)
        except (socket.timeout, TimeoutError):
            break
        if not chunk:
            break
        buf += chunk
        last_byte_at = time.monotonic()
        obj, other = _scan(buf, want_id, own_ids)
        if other:
            saw_other_id = True
        if obj is not None:
            return obj
    msg = describe_timeout(buf, want_id, time.monotonic() - start, sock_path)
    if saw_other_id:




        raise DesyncDetected(msg)
    raise RuntimeError(msg)


def call_with_retry(do_call, respawn, pid_probe=None):






























































    pid_before = pid_probe() if pid_probe is not None else None
    try:
        return do_call()
    except DesyncDetected:
        respawn()
        return do_call()
    except RuntimeError:
        if pid_probe is not None:
            pid_after = pid_probe()
            if pid_before and pid_after and pid_before != pid_after:
                return do_call()
        raise
