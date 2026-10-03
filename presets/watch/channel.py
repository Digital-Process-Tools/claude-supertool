







































































































from __future__ import annotations

import calendar
import errno
import json
import os
import re
import shlex
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple




sys.path.insert(0, str(Path(__file__).parent.parent))  
sys.path.insert(0, str(Path(__file__).parent))  

import _proc  
import _untrusted  
import naming  
import sourcepath  
import transport  





_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)






RESOLVED = naming.resolve()
SOCK_PATH = RESOLVED.sock
STATE_DIR = RESOLVED.state_dir





MCP_FILENAME = ".mcp.json"
CONSUMER_SERVER = "claude-channel"





CHANNEL_VARS = (naming.NAME_ENV, naming.SOCK_ENV, naming.STATE_DIR_ENV)






HEALTH_SUFFIX = ".health.json"









REFUSAL_SUFFIX = ".refused.json"










_REFUSAL_ABSENT = "no rival consumer has recorded losing this socket"






RECEIVED_SUFFIX = ".received.json"











STALE_AFTER_SECS = 45


CONNECT_TIMEOUT = 0.5

RC_FORWARDING = 0
RC_NOT_DELIVERING = 1
RC_UNKNOWN = 3




RC_CONTRADICTED = 4





RC_NOT_SUBSCRIBED = 5







RC_PROBE_NOT_FORWARDED = 6




RC_PROBE_DISCARDED = 7






RC_UNPROVEN = 8


SUB_SUBSCRIBED = "subscribed"
SUB_NOT_SUBSCRIBED = "not-subscribed"
SUB_UNKNOWN = "unknown"








CHANNEL_FLAG = "--dangerously-load-development-channels"
TAG_PREFIX = "server:"









PLUGIN_TAG_PREFIX = "plugin:"




















CLAUDE_BIN = "claude"
CLAUDE_UNKNOWN_SERVER = "No MCP server named"




















CLAUDE_NOT_LOADED_STATUS_RE = re.compile(
    r"^[ \t]*Status:.*\b(?:Rejected|Pending approval)\b", re.MULTILINE)




CLAUDE_TIMEOUT = 8




PS_TIMEOUT = 5












MCP_LOOKUP_BUDGET = 12.0
SUBSCRIPTION_WORST_CASE = PS_TIMEOUT * 2 + MCP_LOOKUP_BUDGET














PROBE_WAIT_SECS = 3.0
PROBE_POLL_SECS = 0.1







PROBE_WORST_CASE = CONNECT_TIMEOUT * 2 + PROBE_WAIT_SECS





_UCRED = "3i"









_SOL_LOCAL = 0
_LOCAL_PEERPID = 2




CEILING = (
    "`forwarded` counts events handed to the MCP transport by the consumer.\n"
    "Whether they appeared in a Claude session is not observable from here, or\n"
    "from any process except that session: the bridge sends a JSON-RPC\n"
    "notification, which has no response to wait on."
)














PROBE_CEILING = (
    "`forwarded` counts events the consumer handed to the MCP transport. It is\n"
    "not a receipt. Whether this event appeared in a Claude session is\n"
    "observable only from inside that session — the bridge sends a JSON-RPC\n"
    "notification, which has no response to wait on — so no process outside it\n"
    "can see the last leg, this one included.\n"
    "The other half of the answer is yours, and it is the half no process here\n"
    "can reach: the `expect` line above says whether a tag should now appear in\n"
    "a session, and only a session can see whether it did. If it does not\n"
    "appear under a report that says it should, the producer half is exonerated\n"
    "and what is left is the subscription and the session — `channel:health`\n"
    "reports on the first of those (BOUND, NOT SUBSCRIBED).\n"
    "Nor is the increment attributable. A poller emitting in the same window\n"
    "advances the same counter and this op cannot tell the two apart. What is\n"
    "established is that the read-and-forward path moved at least one event in\n"
    "the window this emit opened."
)


def _parse_iso(value: Any) -> float | None:













    if not isinstance(value, str):
        return None
    text = value
    if "." in text and text.endswith("Z"):
        text = text[:text.index(".")] + "Z"
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, OverflowError):
        return None


def probe_socket(path: str) -> tuple[str, str]:










    if not os.path.exists(path):
        return "no-listener", f"no socket at {path}"
    if not hasattr(socket, "AF_UNIX"):




        return "unknown", "this platform has no AF_UNIX socket, so nothing here can probe the path"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(CONNECT_TIMEOUT)
        s.connect(path)
        return "accepted", f"{path} accepted a connection"
    except ConnectionRefusedError:
        return "no-listener", f"{path} exists but refused the connection (ConnectionRefusedError)"
    except FileNotFoundError:





        return "no-listener", f"{path} vanished between the check and the connect"
    except OSError as err:
        return "unknown", f"{type(err).__name__} connecting to {path}"
    finally:
        try:
            s.close()
        except OSError:
            pass


def peer_credentials_supported() -> bool:








    if not hasattr(socket, "AF_UNIX"):
        return False
    if sys.platform.startswith("linux"):
        return hasattr(socket, "SO_PEERCRED")
    return sys.platform == "darwin"


def peer_pid(path: str) -> tuple[int | None, str]:















    if not peer_credentials_supported():
        return None, (
            f"peer credentials for an AF_UNIX socket are not available on "
            f"{sys.platform}, so the process holding it cannot be named from here"
        )
    shown = _untrusted.flat(path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(CONNECT_TIMEOUT)
        s.connect(path)
        if sys.platform == "darwin":
            raw = s.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, struct.calcsize("i"))
            (pid,) = struct.unpack("i", raw)
        else:
            raw = s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                               struct.calcsize(_UCRED))
            pid, _uid, _gid = struct.unpack(_UCRED, raw)
    except OSError as err:
        return None, f"{type(err).__name__} asking who holds {shown}"
    except struct.error as err:


        return None, f"the peer credentials for {shown} were unreadable ({err})"
    finally:
        try:
            s.close()
        except OSError:
            pass
    if pid <= 0:


        return None, f"the kernel reported no pid for the process holding {shown}"
    return pid, ""


def read_health(path: str) -> tuple[dict | None, str]:

























    return _read_json_sidecar(
        path + HEALTH_SUFFIX,
        absent=(
            "the bound consumer publishes no counters — it predates this field, or it is "
            "not claude-channel"
        ))


def _read_json_sidecar(sidecar_path: str, *, absent: str) -> tuple[dict | None, str]:








    if not os.path.lexists(sidecar_path):
        return None, absent
    try:
        fd = os.open(sidecar_path, os.O_RDONLY | _NOFOLLOW)
    except OSError as err:


        if err.errno in (errno.ELOOP, errno.EMLINK):
            return None, (
                f"{sidecar_path} is a symlink and was not followed — this file is "
                "written in place by the consumer, so this is somebody redirecting the "
                "read at another file"
            )
        return None, f"{sidecar_path} could not be read ({type(err).__name__})"







    try:
        handle = os.fdopen(fd, "r", encoding="utf-8")
    except OSError as err:
        os.close(fd)
        return None, f"{sidecar_path} could not be read ({type(err).__name__})"
    try:
        with handle as f:
            record = json.load(f)
    except (OSError, ValueError) as err:







        return None, f"{sidecar_path} could not be read ({type(err).__name__})"
    if not isinstance(record, dict):
        return None, f"{sidecar_path} is not a JSON object"
    return record, ""


def read_refusal(path: str) -> tuple[dict | None, str]:












    return _read_json_sidecar(path + REFUSAL_SUFFIX, absent=_REFUSAL_ABSENT)






_RECEIVED_ABSENT = "no receipt has been recorded for this socket yet"


def read_received_receipt(path: str) -> tuple[dict | None, str]:






    return _read_json_sidecar(path + RECEIVED_SUFFIX, absent=_RECEIVED_ABSENT)


def record_received(path: str, count: int) -> tuple[int, str]:





























    prior, _prior_refusal = read_received_receipt(path)
    record, health_refusal = read_health(path)
    now_forwarded = _counter(record, "forwarded") if record else None
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_err = transport.write_json_contained(
        path + RECEIVED_SUFFIX,
        {"received": count, "forwarded_at_report": now_forwarded, "ts": stamp})
    lines = [f"channel: received report for {path}",
             f"  this session reports {count} received so far"]
    if write_err:
        lines.append(f"  WARNING -- could not persist this report ({write_err}); "
                      f"the next call will have no baseline from it")
    if now_forwarded is None:
        reason = health_refusal or "its health file publishes no readable `forwarded` count"
        lines.append(f"  UNSETTLED -- forwarded is not readable right now ({reason}); "
                      f"nothing to compare this report against")
        return 3, "\n".join(lines)
    if prior is None:
        lines.append(f"  UNSETTLED -- no prior receipt for this socket, so this is "
                      f"the first report; forwarded stands at {now_forwarded}. Call "
                      f"again later to compare drift over a window.")
        return 3, "\n".join(lines)
    prior_received = prior.get("received")
    prior_forwarded = prior.get("forwarded_at_report")
    if isinstance(prior_received, bool) or not isinstance(prior_received, int):
        lines.append("  UNSETTLED -- the prior receipt's `received` count is not "
                      "readable, so no window can be diffed")
        return 3, "\n".join(lines)
    if isinstance(prior_forwarded, bool) or not isinstance(prior_forwarded, int):
        lines.append("  UNSETTLED -- the prior receipt recorded no readable "
                      "forwarded baseline, so no window can be diffed")
        return 3, "\n".join(lines)
    delta_forwarded = now_forwarded - prior_forwarded
    if delta_forwarded < 0:







        lines.append(f"  UNSETTLED -- forwarded went backwards ({prior_forwarded} -> "
                      f"{now_forwarded}), which means the consumer restarted between "
                      f"reports; nothing forwarded across that boundary can be "
                      f"compared. This report is the new baseline.")
        return 3, "\n".join(lines)
    delta_received = count - prior_received
    lines.append(f"  {delta_received} received since the last report, "
                 f"{delta_forwarded} forwarded over the same window")
    if delta_received == delta_forwarded:
        lines.append("  AGREE")
        return 0, "\n".join(lines)
    lines.append(f"  DISAGREE by {delta_forwarded - delta_received}")
    return 1, "\n".join(lines)


def _health_objection(record: dict, *, allow_stale: bool = False) -> str:






















    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return "its health file names no process"
    if not _proc.pid_alive(pid):
        return (
            f"its health file was written by pid {pid}, which is gone — something else "
            "is bound to this socket"
        )
    updated = _parse_iso(record.get("updated"))
    if updated is None:
        return "its health file carries no readable `updated` stamp"
    age = time.time() - updated
    if age > STALE_AFTER_SECS and not allow_stale:
        return (
            f"pid {pid} is alive but has not refreshed its counters in {int(age)}s "
            f"(heartbeat is every 10s, stale after {STALE_AFTER_SECS}s) — it may be wedged"
        )
    if _counter(record, "forwarded") is None:
        return (
            "its health file publishes no readable `forwarded` count, which is the "
            "number a FORWARDING verdict would be reporting"
        )
    return ""


def _counter(record: dict, key: str) -> int | None:










    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _num(value: int | None) -> str:
    return "?" if value is None else str(value)


def _stamp(record: dict, key: str, default: str = "?") -> str:
















    value = record.get(key)
    if value is None or value == "":
        return default
    return _untrusted.flat(value if isinstance(value, str) else str(value))


def _health_note() -> str:



    return "  " + _untrusted.flat_note(
        "the stamps", "the consumer's health file")





_ROW_CAP = 10


class Stranded(NamedTuple):










    source: str
    watcher_id: str
    last: dict
    refusal: str


def _read_state_file(name: str) -> tuple[dict | None, str]:



















    path = os.path.join(STATE_DIR, name)
    shown = _untrusted.flat(name)
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError as err:


        if err.errno in (errno.ELOOP, errno.EMLINK):
            return None, (
                f"{shown} is a symlink and was not followed — a state file is written "
                "in place by its own poller, so this is somebody redirecting the read "
                "at another file"
            )
        return None, f"{shown} could not be read ({type(err).__name__})"
    try:
        handle = os.fdopen(fd, "r", encoding="utf-8")
    except OSError as err:
        os.close(fd)
        return None, f"{shown} could not be read ({type(err).__name__})"
    try:
        with handle as f:
            state = json.load(f)
    except (OSError, ValueError) as err:



        return None, f"{shown} could not be read ({type(err).__name__})"
    if not isinstance(state, dict):
        return None, f"{shown} is not a JSON object"
    return state, ""


def stranded_watchers(path: str) -> list[Stranded]:














    rows: list[Stranded] = []




    names, _dir_state, _dir_why = naming.state_dir_listing(STATE_DIR)
    for name in names:
        if not (name.startswith("supertool-watch-") and name.endswith(".state.json")):
            continue
        stem = name[len("supertool-watch-"):-len(".state.json")]
        source, _, watcher_id = stem.partition("__")
        if not watcher_id:
            continue
        state, refusal = _read_state_file(name)
        if state is None:
            rows.append(Stranded(source, watcher_id, {}, refusal))
            continue









        if state.get("sock_path") not in (None, path):
            continue
        last = state.get("last_emit")
        if isinstance(last, dict) and last.get("state") == "no-listener":
            rows.append(Stranded(source, watcher_id, last, ""))
    return rows


def _render_stranded(path: str) -> list[str]:















    _names, dir_state, dir_why = naming.state_dir_listing(STATE_DIR)
    declined = naming.state_dir_absence_note(STATE_DIR, dir_state, dir_why)
    rows = stranded_watchers(path)
    found = [row for row in rows if not row.refusal]
    refused = [row for row in rows if row.refusal]
    if not found and not refused:
        if declined:
            return [f"  watchers : not established — {declined}"]
        return ["  watchers : none recorded an emit into this socket"]

    if found:
        head = f"{len(found)} found nobody listening on their last emit"
    else:



        head = "none of the readable state files recorded an emit into this socket"
    lines = [f"  watchers : {head}",
             "             " + _untrusted.flat_note(
                 "the watcher rows", "the pollers' own state files")]
    for row in found[:_ROW_CAP]:
        lines.append(
            f"             {_untrusted.flat(row.source)} "
            f"{_untrusted.flat(row.watcher_id)} — last emit {_stamp(row.last, 'ts')}")
    if len(found) > _ROW_CAP:
        lines.append(f"             ... and {len(found) - _ROW_CAP} more")
    if refused:





        one = len(refused) == 1
        noun = "state file was" if one else "state files were"
        whose = "it belongs" if one else "they belong"
        subject = "its watcher is" if one else "their watchers are"
        lines.append(
            f"             {len(refused)} {noun} not read, so neither which socket "
            f"{whose} to nor whether {subject} stranded is known")
        for row in refused[:_ROW_CAP]:
            lines.append(f"             {row.refusal}")
        if len(refused) > _ROW_CAP:
            lines.append(f"             ... and {len(refused) - _ROW_CAP} more unread")
    return lines







_PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def _mcp_roots() -> list[Path]:












    return [_PLUGIN_ROOT, Path.cwd()]







_ROOT_LABEL_PLUGIN = "this plugin's own copy"
_ROOT_LABEL_CALLER = "the caller's project"


def _root_label(root: Path) -> str:

















    try:
        is_plugin = Path(root).resolve() == _PLUGIN_ROOT
    except OSError:



        is_plugin = False
    return _ROOT_LABEL_PLUGIN if is_plugin else _ROOT_LABEL_CALLER


def _declared_env(mcp_path: Path) -> tuple[dict[str, str] | None, str]:







    if not mcp_path.exists():
        return None, f"no {MCP_FILENAME} at {mcp_path}"
    try:
        doc = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        return None, f"{mcp_path} could not be read ({type(err).__name__})"
    if not isinstance(doc, dict):
        return None, f"{mcp_path} is not a JSON object"
    servers = doc.get("mcpServers")
    if not isinstance(servers, dict) or not isinstance(servers.get(CONSUMER_SERVER), dict):
        return None, f"{mcp_path} declares no {CONSUMER_SERVER} server"
    env = servers[CONSUMER_SERVER].get("env")
    if env is None:
        return {}, ""
    if not isinstance(env, dict):
        return None, f"{mcp_path} gives {CONSUMER_SERVER} an `env` that is not an object"
    return {str(k): str(v) for k, v in env.items()}, ""


def _declaration(env: dict[str, str]) -> str:


    bits = [f"{key}={_untrusted.flat(env[key])}"
            for key in (naming.NAME_ENV, naming.SOCK_ENV, naming.STATE_DIR_ENV)
            if env.get(key)]
    return ", ".join(bits) if bits else "no watch variables at all"


def consumer_lines(resolved: naming.Resolved,
                   roots: list[Path] | None = None) -> list[str]:















































    roots = _mcp_roots() if roots is None else roots
    agreed: list[str] = []
    differed: list[str] = []
    inherits: list[str] = []
    unread: list[str] = []
    seen: set[str] = set()
    for root in roots:
        mcp_path = Path(root) / MCP_FILENAME
        key = str(mcp_path)
        if key in seen:
            continue
        seen.add(key)
        label = _root_label(root)
        env, why = _declared_env(mcp_path)
        if env is None:
            unread.append((label, why))
            continue
        if not any(var in env for var in CHANNEL_VARS):











            inherits.append(
                f"consumer config {key} ({label}) names no channel variable, so "
                f"the consumer inherits it from the session that spawned it")
            inherits.append(
                f"that environment is not readable from here: "
                f"`bin/oss-workspace` exports {naming.NAME_ENV}, and a "
                f"session started any other way leaves the consumer on "
                f"{naming.DEFAULT_SOCK} while this process reads "
                f"{naming.flat_path(resolved.sock)}")
            continue
        theirs = naming.resolve(env)
        if theirs.sock == resolved.sock:
            agreed.append(
                f"consumer config {key} ({label}) agrees: {_declaration(env)}")
        else:
            differed.append(
                f"consumer config {key} ({label}) declares {_declaration(env)}, "
                f"which binds {naming.flat_path(theirs.sock)} — this process "
                f"reads {naming.flat_path(resolved.sock)}. The consumer is on "
                f"another channel, so nothing a poller emits here reaches it")
    if differed:
        return differed + agreed
    if agreed:
        return agreed if resolved.name else []
    if resolved.name or resolved.sock != naming.DEFAULT_SOCK:
        return inherits + [
            f"consumer config NOT checked ({label}) — {why}"
            for label, why in unread]
    return []


def _channel_lines(path: str, resolved: naming.Resolved) -> list[str]:






    if path != resolved.sock:
        return []
    body: list[str] = []
    if resolved.name:
        body.append(
            f"name {_untrusted.flat(resolved.name)} (from {naming.NAME_ENV}) — "
            f"poller slots in {naming.flat_path(resolved.state_dir)}")
    if resolved.refusal:
        body.append(resolved.refusal)
    body.extend(resolved.notes)





    body.extend(naming.project_notes(resolved, naming.declared_names()))



    body.extend(sourcepath.op_lines("channel"))
    body.extend(consumer_lines(resolved))
    if not body:
        return []
    return [f"  channel  : {body[0]}"] + [f"             {line}" for line in body[1:]]


def _refusal_lines(path: str) -> list[str]:














    record, why = read_refusal(path)
    if record is None:
        if why == _REFUSAL_ABSENT:
            return []
        return [
            "  refused  : a refusal marker exists for this socket but could not be "
            "read —",
            f"             {_untrusted.flat(why)}. Same defensive read `read_health` "
            "uses for the",
            "             health file (#148, #1184/#1187) — this declines rather than",
            "             guessing, so a same-uid symlink at this predictable name "
            "cannot hide",
            "             behind a report that reads exactly like no collision at "
            "all (#2133)",
        ]
    reason = record.get("reason")
    reason_text = (_untrusted.flat(reason) if isinstance(reason, str) and reason
                    else "unknown reason")
    pid = record.get("pid")
    pid_text = str(pid) if isinstance(pid, int) else "?"
    ts_text = _stamp(record, "ts")
    return [
        f"  refused  : pid {pid_text} lost this socket at {ts_text} — {reason_text}",
        "             a second claude-channel server was configured for this",
        "             session and exited without binding it. If this session also",
        "             carries a channel MCP declaration outside the one that bound",
        "             this socket, that collision recurs every time it launches (#2133)",
    ]


def _holder_lines(path: str) -> list[str]:
















    holder, why = peer_pid(path)
    if holder is None:
        return [f"             socket-holder NOT resolved — {why}"]
    if holder == os.getpid():
        return [
            f"             socket-holder: pid {holder} — this process. The report is "
            f"being run by",
            "             the process holding the socket, so no separate consumer was found",
        ]
    return [
        f"             socket-holder: pid {holder} — not this process (this process is "
        f"pid {os.getpid()})",
        "             something IS bound and reading is possible: this is `not my",
        "             listener`, not `no listener`. Those call for opposite actions,",
        "             and this arm used to render them identically",
    ]


class Subscription(NamedTuple):














    state: str
    lines: list[str]
    probe_residue: bool = False


def _sub(state: str, head: str, rest: tuple[str, ...] = (),
         *, probe_residue: bool = False) -> Subscription:
    return Subscription(
        state, [f"  session  : {head}"] + [f"             {line}" for line in rest],
        probe_residue)


def _ps_fields(pid: int) -> tuple[int | None, str, str]:







    try:
        done = subprocess.run(
            ["ps", "-ww", "-o", "ppid=,command=", "-p", str(pid)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=PS_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as err:
        return None, "", f"`ps` could not be run ({type(err).__name__})"
    if done.returncode != 0:
        return None, "", f"`ps` found no process {pid}"
    text = done.stdout.decode("utf-8", "replace").strip()
    if not text:
        return None, "", f"`ps` returned nothing for pid {pid}"
    ppid_text, _, argv = text.splitlines()[0].strip().partition(" ")
    try:
        ppid = int(ppid_text)
    except ValueError:
        return None, "", f"`ps` output for pid {pid} did not begin with a ppid"
    return ppid, argv.strip(), ""


def _now() -> float:

    return time.monotonic()








_CONTROL = frozenset(chr(code) for code in list(range(0x20)) + [0x7f])


def _tag_shape_objection(name: str) -> str:







    if not name:
        return "the tag after `server:` is empty, so it names nothing"
    if name.startswith("-"):
        return ("the tag after `server:` begins with `-`, so it is an option "
                "rather than a server name")
    bad = next((ch for ch in name if ch in _CONTROL), "")
    if bad:
        return (f"the tag after `server:` carries the control character "
                f"{ord(bad):#04x}, which no configured server name has")
    return ""


def _configured(name: str, timeout: float | None = None) -> tuple[bool | None, str]:






































    objection = _tag_shape_objection(name)
    if objection:
        return None, objection
    try:
        done = subprocess.run(
            [CLAUDE_BIN, "mcp", "get", "--", name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=CLAUDE_TIMEOUT if timeout is None else timeout)
    except (OSError, subprocess.SubprocessError) as err:
        return None, f"`{CLAUDE_BIN} mcp get` could not be run ({type(err).__name__})"
    out = done.stdout.decode("utf-8", "replace")
    if done.returncode == 0:







        if CLAUDE_NOT_LOADED_STATUS_RE.search(out):
            return False, ""
        return True, ""
    if CLAUDE_UNKNOWN_SERVER in out:
        if name.startswith(PLUGIN_TAG_PREFIX):









            return None, (
                f"`{CLAUDE_BIN} mcp get` says no server is named "
                f"{_untrusted.flat(name)!r}, but `claude mcp list`/`get` no "
                f"longer reliably report a `{PLUGIN_TAG_PREFIX}`-qualified "
                f"server at all on some harness versions (#2361) -- a loaded, "
                f"bound plugin server and a genuinely absent one both produce "
                f"this same rc=1 text there, so it is not a finding")
        return False, ""
    return None, (f"`{CLAUDE_BIN} mcp get` exited {done.returncode} without saying "
                  f"the name is unknown")


def _channel_tags(argv: str) -> tuple[list[str] | None, str]:









    try:
        tokens = shlex.split(argv)
    except ValueError:
        return None, "the session argv did not tokenise"
    values: list[str] = []
    for index, token in enumerate(tokens):
        if token == CHANNEL_FLAG:
            for value in tokens[index + 1:]:
                if value.startswith("-"):
                    break
                values.append(value)
        elif token.startswith(CHANNEL_FLAG + "="):
            values.append(token[len(CHANNEL_FLAG) + 1:])
    if not values:
        return [], ""
    if not all(value.startswith(TAG_PREFIX) for value in values):
        return None, (f"a value after {CHANNEL_FLAG} is not a `{TAG_PREFIX}` tag, so "
                      f"a server name containing a space cannot be told from a "
                      f"second entry")
    return [value[len(TAG_PREFIX):] for value in values], ""


def _looks_like_a_session(argv: str) -> bool:








    if CHANNEL_FLAG in argv:
        return True
    first = argv.split(" ", 1)[0]
    return first == CLAUDE_BIN or first.endswith("/" + CLAUDE_BIN)


def _dual_declaration_objection(path: str, tag_name: str,
                                resolved: naming.Resolved,
                                roots: list[Path] | None = None) -> str | None:





































    if tag_name == CONSUMER_SERVER:
        return None
    for root in (_mcp_roots() if roots is None else roots):
        mcp_path = Path(root) / MCP_FILENAME
        env, why = _declared_env(mcp_path)
        if env is None:












            if why == f"no {MCP_FILENAME} at {mcp_path}" or (
                    why == f"{mcp_path} declares no {CONSUMER_SERVER} server"):
                continue
            return (f"{mcp_path} could not be checked for a "
                    f"{CONSUMER_SERVER} declaration ({_untrusted.flat(why)}), "
                    f"so whether it collides with {TAG_PREFIX}{tag_name} on "
                    f"this socket cannot be ruled out")
        theirs_sock = (naming.resolve(env).sock
                       if any(var in env for var in CHANNEL_VARS)
                       else resolved.sock)
        if theirs_sock == path:
            return (f"{mcp_path} declares {CONSUMER_SERVER} on this same "
                    f"socket, unconditionally (#1541) — a session carrying "
                    f"both that standing server and {TAG_PREFIX}{tag_name} "
                    f"spawns two channel-capable servers over one socket. "
                    f"One binds; the harness's connection to the other "
                    f"closes (#2133), and there is no marker requirement "
                    f"here for that to be true")
    return None


def subscription(pid: Any, pid_note: str = "", path: str | None = None,
                 resolved: naming.Resolved = None,  
                 roots: list[Path] | None = None) -> Subscription:


























    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return _sub(SUB_UNKNOWN,
                    "NOT established — no consumer pid to ask about",
                    ("nothing here says whether a session is subscribed",))
    origin = f" ({pid_note})" if pid_note else ""
    ppid, _argv, why = _ps_fields(pid)
    if ppid is None:
        return _sub(SUB_UNKNOWN,
                    f"NOT established — the parent of consumer pid {pid}{origin} "
                    f"was not read",
                    (why,))
    if ppid <= 1:
        return _sub(SUB_NOT_SUBSCRIBED,
                    f"none — consumer pid {pid}{origin} has been reparented to pid "
                    f"{ppid}, so the",
                    ("session that spawned it has exited. A consumer outlives its "
                     "session only",
                     "as an orphan, and an orphan has nobody to deliver to"))
    _, parent_argv, parent_why = _ps_fields(ppid)
    if not parent_argv:
        return _sub(SUB_UNKNOWN,
                    f"NOT established — pid {ppid} spawned this consumer and its "
                    f"argv was not read",
                    (parent_why,))
    shown = _untrusted.flat(parent_argv)
    if not _looks_like_a_session(parent_argv):
        return _sub(SUB_UNKNOWN,
                    f"NOT established — pid {ppid} spawned this consumer and is not "
                    f"recognisably",
                    (f"a Claude session: {shown}",
                     "a session launched under another argv reads the same from "
                     "here, so this",
                     "is a declined probe rather than a definite negative"))
    tags, tag_why = _channel_tags(parent_argv)
    if tags is None:
        return _sub(SUB_UNKNOWN,
                    f"NOT established — the argv of session pid {ppid} did not parse",
                    (tag_why, shown))
    if not tags:
        return _sub(SUB_NOT_SUBSCRIBED,
                    f"none — session pid {ppid} spawned this consumer and carries no",
                    (f"{CHANNEL_FLAG} tag, so nothing it",
                     "is handed is surfaced. Every event read here is discarded",
                     f"session argv: {shown}"))








    undecided: list[str] = []
    deadline = _now() + MCP_LOOKUP_BUDGET
    for name in tags:




        objection = _tag_shape_objection(name)
        if objection:
            undecided.append(f"{TAG_PREFIX}{_untrusted.flat(name)}: {objection}")
            continue
        remaining = deadline - _now()
        if remaining <= 0:
            undecided.append(
                f"{TAG_PREFIX}{_untrusted.flat(name)}: the probe's "
                f"{MCP_LOOKUP_BUDGET:g}s lookup budget was spent before this tag "
                f"was reached, so it was never asked about")
            continue
        answer, ask_why = _configured(name, min(CLAUDE_TIMEOUT, remaining))
        if answer:


























            standing = None
            if name != CONSUMER_SERVER:
                left = deadline - _now()
                if left > 0:
                    standing, _ = _configured(CONSUMER_SERVER,
                                              min(CLAUDE_TIMEOUT, left))
            if path is not None and standing is not False:
                dual_why = _dual_declaration_objection(
                    path, name, resolved if resolved is not None else RESOLVED, roots)
                if dual_why:
                    return _sub(
                        SUB_UNKNOWN,
                        f"NOT established — {TAG_PREFIX}{_untrusted.flat(name)} is "
                        f"configured, but",
                        (_untrusted.flat(dual_why),))
                collision, collision_why = read_refusal(path)
                if collision is not None:
                    reason = collision.get("reason")
                    reason_text = (_untrusted.flat(reason)
                                    if isinstance(reason, str) and reason
                                    else "unknown reason")
                    return _sub(
                        SUB_UNKNOWN,
                        f"NOT established — {TAG_PREFIX}{_untrusted.flat(name)} is "
                        f"configured, but a rival",
                        (f"claude-channel server was refused this exact socket "
                         f"during this run ({reason_text}, see `refused` above). "
                         "Two channel",
                         "servers configured for one session (#2133) means a "
                         "configured tag proves",
                         "nothing about which one the harness actually holds a "
                         "connection to —",
                         "`claude mcp get` cannot tell them apart (#1558), so this "
                         "lands in the",
                         "third state rather than the positive one"))
                if collision_why != _REFUSAL_ABSENT:





                    return _sub(
                        SUB_UNKNOWN,
                        f"NOT established — {TAG_PREFIX}{_untrusted.flat(name)} is "
                        f"configured, but a refusal",
                        (f"marker for this socket could not be read "
                         f"({_untrusted.flat(collision_why)}), so a collision",
                         "during this run cannot be ruled out. Same defensive read "
                         "`read_health` uses",
                         "(#148, #1184/#1187) — guessing 'no rival' off an "
                         "unreadable marker would be",
                         "the same defect this state exists to remove, one call "
                         "site over"))










            census = ((f"separately, no server named {CONSUMER_SERVER} is "
                       f"configured -- a different name",
                       "from the tag above, asked because every copy of this "
                       "plugin ships a `.mcp.json`",
                       "declaring it. So that declaration was not loaded, and "
                       "any refusal marker beside",
                       "this socket was left by a short-lived `claude mcp get` "
                       "probe, not by a session (#2182)")
                      if standing is False else ())
            return _sub(SUB_SUBSCRIBED,
                        f"subscribed — session pid {ppid} carries "
                        f"{TAG_PREFIX}{_untrusted.flat(name)}, and the",
                        ("harness has a server configured under that name",
                         *census,
                         "NOT established: that the configured server is the one "
                         "holding this",
                         "socket. Two channel-capable servers would satisfy both "
                         "halves apart"),





                        probe_residue=(standing is False))
        if answer is None:
            undecided.append(f"{TAG_PREFIX}{_untrusted.flat(name)}: {ask_why}")
    if undecided:
        return _sub(SUB_UNKNOWN,
                    f"NOT established — whether the harness has the server(s) "
                    f"session pid {ppid}",
                    ("asked for configured was not settled", *undecided))
    named = ", ".join(TAG_PREFIX + _untrusted.flat(name) for name in tags)
    return _sub(SUB_NOT_SUBSCRIBED,
                f"none — session pid {ppid} asked for {named}, and the harness has",
                ("no MCP server configured with that name. It refuses the tag at "
                 "startup and",
                 "the session subscribes to nothing; a server loaded from "
                 "`--mcp-config` binds",
                 "this socket and reaches that state (#1544)"))


def subscription_for_socket(path: str) -> Subscription:







    holder, why = peer_pid(path)
    if holder is not None:
        return subscription(holder, path=path)
    record, _ = read_health(path)
    claimed = record.get("pid") if isinstance(record, dict) else None
    if isinstance(claimed, int):
        return subscription(claimed, "self-reported by the health file", path)
    return _sub(SUB_UNKNOWN,
                "NOT established — no consumer pid was resolved for this socket",
                (why or "the health file names no pid",))


def _identity_lines(record: dict, holder: int | None, holder_why: str) -> list[str]:









    claimed = record.get("pid")
    if holder is not None:
        return [
            f"  consumer : pid {claimed}, up since {_stamp(record, 'started')}",
            "             socket-holder verified: the process holding this socket is",
            "             the one the health file names. That rules out a stale or",
            "             forged file beside a live consumer; it does not rule out a",
            "             same-uid process that bound the socket and wrote the file,",
            "             which is its own peer and agrees with itself.",
        ]
    return [
        f"  consumer : pid {claimed} (self-reported), up since "
        f"{_stamp(record, 'started')}",
        f"             socket-holder NOT checked — {holder_why}",
        "             the health file names its own writer; pids are reusable, so",
        "             nothing here proves that process is the one holding the socket",
    ]


def _drop_refusal_as_probe_residue(head: list[str]) -> list[str]:











    out: list[str] = []
    in_refusal = False
    for line in head:
        if line.startswith("  refused  :"):
            in_refusal = True
            out.extend([
                "  refused  : a refusal marker exists for this socket, but "
                "it is accounted for below —",
                f"             the session census found no standing "
                f"{CONSUMER_SERVER} server configured, so this report's own",
                "             `claude mcp get` probe wrote it — not a rival "
                "session (#2182).",
                "             Not printed as a live collision finding",
            ])
            continue
        if in_refusal and line.startswith("             "):
            continue
        in_refusal = False
        out.append(line)
    return out


def health(path: str) -> tuple[int, str]:

    state, detail = probe_socket(path)
    head = [f"  socket   : {path}", f"             {detail}"]
    head += _channel_lines(path, RESOLVED)
    head += _refusal_lines(path)

    if state == "no-listener":
        body = ["channel: NOT DELIVERING", *head,
                "  consumer : none — every event emitted right now is lost at the source"]
        body += _render_stranded(path)
        return RC_NOT_DELIVERING, "\n".join([*body, "", CEILING])

    if state == "unknown":
        return RC_UNKNOWN, "\n".join([
            "channel: CANNOT DETERMINE", *head,
            "  consumer : the socket could not be probed, so nothing is known either way",






            f"             socket-holder NOT asked — {detail}. The holder check is",
            "             the same connect, so there is nothing there to ask; this",
            "             is a declined probe, not an absent holder",
            "", CEILING,
        ])

    record, why = read_health(path)
    if record is None:
        return RC_UNKNOWN, "\n".join([
            "channel: CANNOT DETERMINE", *head,
            f"  consumer : bound, but {why}",
            *_holder_lines(path),
            "             bytes are accepted; what happens to them is not visible here",
            "", CEILING,
        ])

    objection = _health_objection(record)
    if objection:
        return RC_UNKNOWN, "\n".join([
            "channel: CANNOT DETERMINE", *head,
            _health_note(),
            f"  consumer : bound, but {objection}",
            *_holder_lines(path),
            f"             last published counters: {_num(_counter(record, 'forwarded'))} forwarded, "
            f"{_num(_counter(record, 'dropped'))} dropped, updated {_stamp(record, 'updated')}",
            "", CEILING,
        ])

    claimed = record.get("pid")
    holder, holder_why = peer_pid(path)
    if holder is not None and holder != claimed:





        return RC_CONTRADICTED, "\n".join([
            "channel: CONTRADICTED", *head,
            _health_note(),
            f"  consumer : the health file names pid {claimed}, but pid {holder} is "
            f"the process holding this socket",
            "             these are the same process on a healthy channel. They are",
            "             not here, so the counters below were published by something",
            "             that is not the consumer — a live impersonation, or a health",
            "             file left behind beside a legitimate socket. Neither is a",
            "             degraded read: check both pids before trusting any of it.",
            f"  counters : {_num(_counter(record, 'lines_read'))} lines read, "
            f"{_num(_counter(record, 'forwarded'))} forwarded, "
            f"{_num(_counter(record, 'dropped'))} dropped",
            "", CEILING,
        ])

    identity = _identity_lines(record, holder, holder_why)







    sub = subscription(holder if holder is not None else claimed,
                       "" if holder is not None else "self-reported by the health file",
                       path)














    if sub.probe_residue:
        refusal_record, _refusal_why = read_refusal(path)
        if refusal_record is not None:
            head = _drop_refusal_as_probe_residue(head)
    counters = [
        f"  counters : {_num(_counter(record, 'lines_read'))} lines read, "
        f"{_num(_counter(record, 'forwarded'))} forwarded, "
        f"{_num(_counter(record, 'dropped'))} dropped",
        f"             last forwarded {_stamp(record, 'last_forwarded', 'never')}"
        f" (counters refreshed {_stamp(record, 'updated')})",
    ]
    if sub.state == SUB_NOT_SUBSCRIBED:



        return RC_NOT_SUBSCRIBED, "\n".join([
            "channel: BOUND, NOT SUBSCRIBED", *head,
            _health_note(), *identity, *sub.lines, *counters, "", CEILING,
        ])
    if sub.state == SUB_UNKNOWN:
        return RC_UNKNOWN, "\n".join([
            "channel: CANNOT DETERMINE", *head,
            _health_note(), *identity, *sub.lines, *counters, "", CEILING,
        ])







    if (_counter(record, 'forwarded') == 0
            and record.get('last_forwarded') is None):
        return RC_UNPROVEN, "\n".join([
            "channel: BOUND, UNPROVEN", *head,
            _health_note(),
            *identity,
            *sub.lines,
            *counters,
            "  delivery : never forwarded anything through this channel — a "
            "bound, subscribed",
            "             consumer that has not yet moved an event looks "
            "identical to a dead",
            "             one from here. Not FORWARDING: that word is "
            "reserved for a channel",
            "             that has demonstrated delivery (#2658)",
            "", CEILING,
        ])
    return RC_FORWARDING, "\n".join([
        "channel: FORWARDING", *head,
        _health_note(),
        *identity,
        *sub.lines,
        *counters,
        "", CEILING,
    ])


def probe(path: str, *, wait: float = PROBE_WAIT_SECS) -> tuple[int, str]:






























    before, before_why = read_health(path)

    watcher_id = transport.probe_id()
    verdict = transport.emit_socket(transport.probe_record(watcher_id), path)
    tag = (f'<channel watcher_source="{transport.PROBE_SOURCE}" '
           f'id="{watcher_id}" event="{transport.PROBE_EVENT}">')

    head = [f"  socket   : {path}", f"             {verdict.detail}"]
    head += _channel_lines(path, RESOLVED)
    head += [
        f"  probe    : one synthetic event written — source "
        f"{transport.PROBE_SOURCE}, id {watcher_id}, event {transport.PROBE_EVENT}",
        "             a reserved source, so no watcher is impersonated; and no",
        "             watcher state file was written or overwritten by this call",
    ]

    def report(headline: str, *body: str) -> str:
        return "\n".join([headline, *head, *body, "", PROBE_CEILING])

    if verdict.state == transport.EMIT_NO_LISTENER:
        return RC_NOT_DELIVERING, report(
            "channel: NOT DELIVERING",
            "  consumer : none — this event was lost at the source, and so is",
            "             every event a poller emits right now",
            "  expect   : nothing. No tag for this probe can appear in any session,",
            "             because nothing took the bytes",
        )
    if verdict.state != transport.EMIT_ACCEPTED:
        return RC_UNKNOWN, report(
            "channel: CANNOT DETERMINE",
            f"  consumer : the write did not complete — {verdict.detail}",
            "             nothing here can tell whether a partial line reached a",
            "             consumer, so this is a declined probe, not a negative",
            f"  expect   : possibly {tag} — unmeasured either way",
        )

    if before is None:
        return RC_UNKNOWN, report(
            "channel: CANNOT DETERMINE",
            f"  consumer : bound and it took the bytes, but {before_why}",
            "             with no counters there is no baseline, so an advance could",
            "             not have been observed however well the path is working",
            *_holder_lines(path),
            f"  expect   : possibly {tag} — this op could not check",
        )




    objection = _health_objection(before, allow_stale=True)
    if objection:
        return RC_UNKNOWN, report(
            "channel: CANNOT DETERMINE", _health_note(),
            f"  consumer : bound and it took the bytes, but {objection}",
            *_holder_lines(path),
            f"  expect   : possibly {tag} — this op could not check",
        )




    cold = _health_objection(before)

    claimed = before.get("pid")
    holder, holder_why = peer_pid(path)
    if holder is not None and holder != claimed:





        return RC_CONTRADICTED, report(
            "channel: CONTRADICTED", _health_note(),
            f"  consumer : the health file names pid {claimed}, but pid {holder} is "
            f"the process holding this socket",
            "             an advance in counters published by something that is not",
            "             the consumer would say nothing about the event just written,",
            "             so this probe is not measured rather than failed",
            f"  expect   : unknown — {tag} may or may not appear",
        )

    identity = _identity_lines(before, holder, holder_why)
    if cold:
        identity += [
            f"             baseline counters were cold — {cold}",
            "             that is not a reason to decline here, it is the question:",
            "             whether they come back advanced after this emit is the",
            "             measurement, and the re-read below waives nothing",
        ]



    base_fwd = _counter(before, "forwarded")
    base_drop = _counter(before, "dropped")
    base_read = _counter(before, "lines_read")

    started = time.monotonic()












    after: dict | None
    while True:
        after, after_why = read_health(path)
        after_objection = "" if after is None else _health_objection(after)
        waited = time.monotonic() - started
        if after is not None and not after_objection:
            now_fwd = _counter(after, "forwarded")
            now_drop = _counter(after, "dropped")
            if now_fwd is not None and now_fwd > base_fwd:
                also = ""
                if base_drop is not None and now_drop is not None and now_drop > base_drop:
                    also = (f", and `dropped` by {now_drop - base_drop} in the same "
                            "window — at least one event was refused, and which of "
                            "them was this one is not on the record")
                return RC_FORWARDING, report(
                    "channel: FORWARDED", _health_note(), *identity,
                    f"  counters : forwarded {base_fwd} -> {now_fwd} "
                    f"(+{now_fwd - base_fwd}) after {waited:.1f}s{also}",
                    "             the consumer read from this socket and handed an",
                    "             event to the MCP transport inside the window this",
                    "             emit opened",
                    f"  expect   : {tag}",
                    "             in whichever session is subscribed to this channel",
                )
            if base_drop is not None and now_drop is not None and now_drop > base_drop:
                return RC_PROBE_DISCARDED, report(
                    "channel: ACCEPTED, DISCARDED", _health_note(), *identity,
                    f"  counters : dropped {base_drop} -> {now_drop} "
                    f"(+{now_drop - base_drop}) after {waited:.1f}s, forwarded "
                    f"unchanged at {base_fwd}",
                    "             the consumer read an event off this socket and",
                    "             refused it — the burst budget, a routing key over",
                    "             the attribute cap, or a handler that threw. Its own",
                    "             stderr names which, and `claude --debug` surfaces it",
                    f"  expect   : nothing for {tag}",
                    "             unless the discard was somebody else's event",
                )
        if waited >= wait:
            break
        time.sleep(min(PROBE_POLL_SECS, max(0.0, wait - waited)))




    if after is None:
        return RC_UNKNOWN, report(
            "channel: CANNOT DETERMINE",
            f"  consumer : it took the bytes, and then {after_why}",
            f"             the counters could not be re-read inside {waited:.1f}s, so",
            "             nothing was measured — this is not a report about the",
            "             consumer, it is a report about this process's eyesight",
            f"  expect   : possibly {tag} — unmeasured",
        )
    if after_objection:
        return RC_UNKNOWN, report(
            "channel: CANNOT DETERMINE", _health_note(),
            f"  consumer : it took the bytes, but {after_objection}",
            *identity,
            f"  expect   : possibly {tag} — unmeasured",
        )

    now_read = _counter(after, "lines_read")
    counters = (
        f"  counters : forwarded {base_fwd} (unchanged), dropped "
        f"{_num(base_drop)} -> {_num(_counter(after, 'dropped'))}, lines read "
        f"{_num(base_read)} -> {_num(now_read)}, over {waited:.1f}s"
    )
    if base_read is None or now_read is None:
        return RC_UNKNOWN, report(
            "channel: CANNOT DETERMINE", _health_note(), *identity, counters,
            "  consumer : it took the bytes and published no readable `lines_read`,",
            "             so `has not read it yet` and `read it and handed it",
            "             nowhere` cannot be told apart from here — and those are a",
            "             slow consumer and a broken one",
            f"  expect   : possibly {tag} — unmeasured",
        )
    if now_read > base_read:
        return RC_PROBE_NOT_FORWARDED, report(
            "channel: ACCEPTED, NOT FORWARDED", _health_note(), *identity, counters,
            "  consumer : it read an event off this socket and neither forwarded nor",
            "             discarded it. Its counters are fresh, so it is alive and",
            "             still publishing — this is a finding about its read loop,",
            "             not an absence of one",
            f"  expect   : nothing for {tag}",
            f"             (a consumer slower than the {wait:.0f}s waited here would",
            "             look identical, and would forward it after this report)",
        )
    return RC_UNKNOWN, report(
        "channel: CANNOT DETERMINE", _health_note(), *identity, counters,
        "  consumer : bound, alive, publishing — and it has not read this line off",
        f"             the wire within {waited:.1f}s. Wedged on its read loop and",
        "             merely slower than this budget are the same picture from here,",
        "             so this is not reported as a finding",
        f"  expect   : possibly {tag} — unmeasured",
    )


def stranded_report(path: str) -> tuple[int, str]:































    rows = [row for row in stranded_watchers(path) if not row.refusal]
    if not rows:







        return 0, ""
    lines = [
        "> CHANNEL NOT DELIVERING -- this session is armed for a watch channel "
        "and no consumer is bound, so events are being LOST, not queued.",
        "> Each watcher below recorded that its own last send found nobody "
        "listening. These are the pollers' own words, not a probe:",
    ]
    for row in rows[:_ROW_CAP]:
        source = _untrusted.flat(row.source)
        watcher_id = _untrusted.flat(row.watcher_id)
        ts = _untrusted.flat(str(row.last.get("ts", "?")))
        lines.append(f">   {source} {watcher_id} -- last emit {ts}")
    if len(rows) > _ROW_CAP:
        lines.append(f">   ... and {len(rows) - _ROW_CAP} more")
    lines.append(
        "> `channel:health` says which of its six states this is; "
        "`channel:probe` writes one synthetic event and reports what took it. "
        "Nothing here is queued for replay -- an event emitted with no listener "
        "is gone (#2478).")
    return RC_NOT_DELIVERING, chr(10).join(lines)


def main(argv: list[str]) -> int:
    sub = argv[1] if len(argv) > 1 else "health"
    if sub == "health":
        code, report = health(SOCK_PATH)
    elif sub == "stranded":
        code, report = stranded_report(SOCK_PATH)



        if report:
            print(report)
        return code
    elif sub == "probe":
        code, report = probe(SOCK_PATH)
    elif sub == "received":
        if len(argv) < 3:
            sys.stderr.write(
                "channel: `received` needs a count -- channel:received:N is "
                "this session's own report of how many channel events it has "
                "received so far (#2150)\n"
            )
            return 2
        try:
            count = int(argv[2])
        except ValueError:
            sys.stderr.write(f"channel: {argv[2]!r} is not an integer count\n")
            return 2
        if count < 0:
            sys.stderr.write("channel: a received count must not be negative\n")
            return 2
        code, report = record_received(SOCK_PATH, count)
    else:




        sys.stderr.write(
            f"channel: unknown sub-op {sub!r} — the four are `channel:health` "
            "(read the consumer's published counters), `channel:probe` "
            "(write one synthetic event and report which counter moved), "
            "`channel:received:N` (this session's own report of how many it "
            "received, compared against `forwarded`'s advance), and "
            "`channel:stranded` (silent unless this channel's own pollers "
            "recorded that their sends found nobody listening -- what "
            "hooks/session-start.sh asks on every session)\n"
        )
        return 2
    print(report)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
