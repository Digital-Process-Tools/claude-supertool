














from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, NamedTuple








sys.path.insert(0, str(Path(__file__).parent.parent))  
sys.path.insert(0, str(Path(__file__).parent))  

import _proc  
import _repo_target  
import _untrusted  
import naming  
from _spawnable import which_excluding_cwd  

















RESOLVED = naming.resolve()
SOCK_PATH = RESOLVED.sock




STATE_DIR = RESOLVED.state_dir
STATE_DIR_ENV = naming.STATE_DIR_ENV
SOCK_ENV = naming.SOCK_ENV







POLL_SUBOP = "poll"
DISPATCHER_TAIL = "watch/dispatcher.py"














CHANNEL_PREFIX = "chan="








_CHANNEL_KEY_CHARS = 12







DEATH_RESPAWN_LIMIT = 3








_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
















_STATE_TMP_SUFFIX = ".tmp"








CLAIM_UNKNOWN = -1
















EMIT_NO_LISTENER = "no-listener"
EMIT_ACCEPTED = "accepted"
EMIT_UNKNOWN = "unknown"















DELIVERY_NO_EMIT = "no-emit"



DELIVERY_LABELS = {
    EMIT_ACCEPTED: "accepted",
    EMIT_NO_LISTENER: "NO LISTENER",
    EMIT_UNKNOWN: "unknown",
    DELIVERY_NO_EMIT: "no emit",
}






VERSION_CURRENT = "current"
VERSION_STALE = "stale"





VERSION_RELOADED = "reloaded"
VERSION_UNKNOWN = "unknown"

VERSION_LABELS = {
    VERSION_CURRENT: "current",
    VERSION_STALE: "STALE",
    VERSION_RELOADED: "RELOADED",
    VERSION_UNKNOWN: "unknown",
}


class Emit(NamedTuple):








    state: str
    detail: str


def state_path(source: str, watcher_id: str) -> str:
    return f"{STATE_DIR}/supertool-watch-{source}__{watcher_id}.state.json"


def pid_path(source: str, watcher_id: str) -> str:
    return f"{STATE_DIR}/supertool-watch-{source}__{watcher_id}.pid"


def read_pid_checked(source: str, watcher_id: str) -> tuple[int | None, str]:



































    path = pid_path(source, watcher_id)
    shown = _untrusted.flat(path)
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError:
        return 0, ""
    except OSError as err:


        if err.errno in (errno.ELOOP, errno.EMLINK):
            return None, (
                f"{shown} is a symlink and was not followed — a pid file is written "
                "in place by the process that claims the slot, so this is somebody "
                "redirecting the read at another file"
            )
        return None, f"{shown} could not be read ({type(err).__name__})"
    try:
        handle = os.fdopen(fd, "r", encoding="utf-8")
    except OSError as err:
        os.close(fd)
        return None, f"{shown} could not be read ({type(err).__name__})"
    try:
        with handle as f:
            raw = f.read()
    except (OSError, ValueError) as err:


        return None, f"{shown} could not be read ({type(err).__name__})"
    try:
        return int(raw.strip()), ""
    except ValueError:
        return 0, f"{shown} exists but its content is not a PID"


def read_pid(source: str, watcher_id: str) -> int:







    pid, _ = read_pid_checked(source, watcher_id)
    return pid or 0


def claim_pidfile(source: str, watcher_id: str) -> int:














































































    if naming.ensure_state_dir(RESOLVED, STATE_DIR):
        return CLAIM_UNKNOWN
    path = pid_path(source, watcher_id)
    directory = os.path.dirname(path) or "."
    for _ in range(2):
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=f"{os.path.basename(path)}.", suffix=".claim",
                dir=directory)
        except OSError:


            return CLAIM_UNKNOWN
        try:
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    f.write(f"{os.getpid()}\n")
            except OSError:



                os.close(tmp_fd)
                return CLAIM_UNKNOWN
            try:
                os.link(tmp_path, path)
            except FileExistsError:
                pass
            except OSError:



                return CLAIM_UNKNOWN
            else:
                return 0
        finally:



            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        existing, _refusal = read_pid_checked(source, watcher_id)
        if existing is None:






            return CLAIM_UNKNOWN
        if existing and _pid_alive(existing):
            return existing
        if existing:




            record_death(source, watcher_id, existing)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        continue



    return CLAIM_UNKNOWN


def record_pid(source: str, watcher_id: str, pid: int) -> None:







    try:
        Path(pid_path(source, watcher_id)).write_text(f"{pid}\n", encoding="utf-8")
    except OSError:
        pass


def release_pidfile(source: str, watcher_id: str, pid: int | None = None) -> None:






    if pid is not None:
        owner, _ = read_pid_checked(source, watcher_id)



        if owner != pid:
            return
    try:
        os.unlink(pid_path(source, watcher_id))
    except OSError:
        pass


def emit_socket(payload: dict[str, Any], path: str | None = None) -> Emit:













    sock_path = SOCK_PATH if path is None else path
    if not os.path.exists(sock_path):
        return Emit(EMIT_NO_LISTENER, f"no socket at {naming.flat_path(sock_path)}")
    if not hasattr(socket, "AF_UNIX"):




        return Emit(EMIT_UNKNOWN, "this platform has no AF_UNIX socket, so nothing could be written")
    s: socket.socket | None = None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(sock_path)
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
    except ConnectionRefusedError:




        return Emit(EMIT_NO_LISTENER, f"{naming.flat_path(sock_path)} refused "
                    f"the connection (ConnectionRefusedError)")
    except FileNotFoundError:




        return Emit(EMIT_NO_LISTENER, f"{naming.flat_path(sock_path)} vanished "
                    f"between the check and the connect")
    except OSError as err:


        return Emit(EMIT_UNKNOWN,
                    f"{type(err).__name__} writing to {naming.flat_path(sock_path)}")
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
    return Emit(EMIT_ACCEPTED, f"{naming.flat_path(sock_path)} accepted the bytes")


def write_state(source: str, watcher_id: str, state: dict[str, Any]) -> str:






































































    why = naming.ensure_state_dir(RESOLVED, STATE_DIR)
    if why:
        return why
    return write_json_contained(state_path(source, watcher_id), state)


def write_json_contained(path: str, payload: Any) -> str:



























    shown = _untrusted.flat(path)
    directory, name = os.path.split(path)
    try:
        fd, tmp = tempfile.mkstemp(prefix=f"{name}.", suffix=_STATE_TMP_SUFFIX,
                                   dir=directory)
    except OSError as err:



        return f"{shown} could not be opened for writing ({type(err).__name__})"
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except OSError as err:



        os.close(fd)
        _discard(tmp)
        return f"{shown} could not be opened for writing ({type(err).__name__})"
    try:
        with handle as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except OSError as err:
        _discard(tmp)
        return f"{shown} could not be written ({type(err).__name__})"
    return ""


def _discard(tmp: str) -> None:






    try:
        os.unlink(tmp)
    except OSError:
        pass


def read_state_checked(source: str, watcher_id: str) -> tuple[dict[str, Any] | None, str]:









































    path = state_path(source, watcher_id)
    shown = _untrusted.flat(path)
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError:


        return {}, ""
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


def read_state(source: str, watcher_id: str) -> dict[str, Any]:




















    state, _ = read_state_checked(source, watcher_id)
    return state if state is not None else {}


def clear_state(source: str, watcher_id: str) -> bool:






    try:
        os.unlink(state_path(source, watcher_id))
    except OSError:
        return False
    return True


def deaths(source: str, watcher_id: str) -> list[dict[str, Any]]:








    return _deaths_in(read_state(source, watcher_id))


def _deaths_in(state: dict[str, Any]) -> list[dict[str, Any]]:






    recorded = state.get("deaths")
    return [d for d in recorded if isinstance(d, dict)] if isinstance(recorded, list) else []


def record_death(source: str, watcher_id: str, pid: int) -> bool:









    if not pid:
        return False
    current = read_state(source, watcher_id)
    recorded = current.get("deaths")
    ledger = [d for d in recorded if isinstance(d, dict)] if isinstance(recorded, list) else []
    if any(d.get("pid") == pid for d in ledger):
        return False
    ledger.append({"pid": pid, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    current["deaths"] = ledger



    return not write_state(source, watcher_id, current)


def clear_deaths(source: str, watcher_id: str) -> bool:







    current = read_state(source, watcher_id)
    if "deaths" not in current:
        return False
    current.pop("deaths", None)


    return not write_state(source, watcher_id, current)


def reap_dead_pidfile(source: str, watcher_id: str) -> int:













    pid, _ = read_pid_checked(source, watcher_id)





    if not pid or _pid_alive(pid):
        return 0
    record_death(source, watcher_id, pid)
    release_pidfile(source, watcher_id)
    return pid









NO_DESKTOP_ENV = "SUPERTOOL_WATCH_NO_DESKTOP"








DESKTOP_ENV = "SUPERTOOL_WATCH_DESKTOP"


def _flag_value(raw: str | None) -> bool:











    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def desktop_notify_disabled(overrides: dict[str, str] | None = None) -> bool:














    if overrides is not None:
        return _flag_value(overrides.get("SUPERTOOL_WATCH_NO_DESKTOP"))
    return _flag_value(os.environ.get("SUPERTOOL_WATCH_NO_DESKTOP"))


def desktop_notify_enabled(overrides: dict[str, str] | None = None) -> bool:










    if desktop_notify_disabled(overrides):
        return False
    if overrides is not None:
        return _flag_value(overrides.get("SUPERTOOL_WATCH_DESKTOP"))
    return _flag_value(os.environ.get("SUPERTOOL_WATCH_DESKTOP"))


def desktop_notify(title: str, message: str) -> None:


    if sys.platform != "darwin":
        return
    if not desktop_notify_enabled():
        return
    osascript_bin = which_excluding_cwd("osascript")
    if not osascript_bin:
        return





    try:
        subprocess.run(
            [
                osascript_bin,
                "-e", "on run argv",
                "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
                "-e", "end run",
                "--", message, title,
            ],
            capture_output=True, timeout=3, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return


FLATTEN_MAX_DEPTH = 6

FLATTEN_TOO_DEEP = ("[supertool: value refused — nested deeper than "
                    f"{FLATTEN_MAX_DEPTH} levels, so it could not be flattened]")


def _flatten_value(value: Any, depth: int) -> Any:

    if isinstance(value, str):
        return _untrusted.flat(value)
    if not isinstance(value, (dict, list, tuple)):
        return value
    if depth <= 0:
        return FLATTEN_TOO_DEEP
    if isinstance(value, dict):



        return {k: _flatten_value(v, depth - 1) for k, v in value.items()}
    return [_flatten_value(v, depth - 1) for v in value]


def flatten_remote(payload: dict[str, Any]) -> dict[str, Any]:


































    return {key: _flatten_value(value, FLATTEN_MAX_DEPTH)
            for key, value in payload.items()}







_REMOTE_SLUG_RE = re.compile(
    r"^(?:[\w.+-]+://[^/]+/|[^@/]+@[^:]+:)(.+?)(?:\.git)?/?\Z")


def repo_slug(timeout: int = 5) -> str:
























    target = _repo_target.target()
    if target:
        return target
    try:
        r = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if r.returncode != 0:
        return ""
    m = _REMOTE_SLUG_RE.match((r.stdout or "").strip())
    return m.group(1) if m else ""


def emit_event(
    source: str,
    watcher_id: str,
    event_key: str,
    payload: dict[str, Any],
    *,
    notify_title: str | None = None,
    notify_message: str | None = None,
    first_tick: bool = False,
    repo: str = "",
) -> None:





















    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
        "id": watcher_id,
        "event": event_key,
        "payload": flatten_remote(payload),
        "first_tick": bool(first_tick),
    }
    if repo:
        record["repo"] = repo
    verdict = emit_socket(record)
    current = read_state(source, watcher_id)
    current["last_event"] = record
    current.setdefault("first_seen", record["ts"])







    current["last_emit"] = {
        "ts": record["ts"],
        "state": verdict.state,
        "detail": verdict.detail,
    }








    current["sock_path"] = SOCK_PATH
    write_state(source, watcher_id, current)
    if notify_title and notify_message:



        desktop_notify(_untrusted.flat(notify_title), _untrusted.flat(notify_message))



















PROBE_SOURCE = "channel-probe"
PROBE_EVENT = "probe"


def probe_record(watcher_id: str) -> dict[str, Any]:





















    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": PROBE_SOURCE,
        "id": watcher_id,
        "event": PROBE_EVENT,
        "payload": flatten_remote({
            "title": "synthetic event from `channel:probe` — nothing is wrong; "
                     "somebody is testing whether this path still works",
        }),



        "first_tick": False,
    }


def probe_id() -> str:








    return f"probe-{os.urandom(4).hex()}"





STATE_DIR_OK = naming.STATE_DIR_OK
STATE_DIR_ABSENT = naming.STATE_DIR_ABSENT
STATE_DIR_UNREADABLE = naming.STATE_DIR_UNREADABLE


def _state_dir_names() -> tuple[list[str], str, str]:











    return naming.state_dir_listing(STATE_DIR)


def state_dir_status() -> tuple[str, str]:











    _names, state, why = _state_dir_names()
    return state, why


def channel_disclosure() -> list[str]:













































    lines = naming.disclosure_lines(RESOLVED, naming.declared_names())
    if desktop_notify_disabled():
        lines = lines + [
            f"desktop notifications are OFF ({NO_DESKTOP_ENV} is set) — the "
            f"socket and status-file transports are unaffected"]
    elif desktop_notify_enabled():
        if sys.platform != "darwin":
            lines = lines + [
                f"desktop notifications are opted in ({DESKTOP_ENV} is "
                f"set) but this platform has no desktop notifier — no "
                f"notification will fire; the socket and status-file "
                f"transports are unaffected"]
        elif which_excluding_cwd("osascript"):
            lines = lines + [
                f"desktop notifications are ON ({DESKTOP_ENV} is set) — the "
                f"socket and status-file transports are unaffected"]
        else:
            lines = lines + [
                f"desktop notifications are opted in ({DESKTOP_ENV} is set) "
                f"but osascript did not resolve — no notification will "
                f"fire; the socket and status-file transports are "
                f"unaffected"]
    return lines


def list_active_pids() -> list[dict[str, Any]]:





















    rows: list[dict[str, Any]] = []
    prefix = "supertool-watch-"
    suffix = ".pid"




    names, _state, _why = _state_dir_names()
    for name in names:
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue


        stem = name[len(prefix):-len(suffix)]
        if "__" not in stem:
            continue
        source, watcher_id = stem.split("__", 1)
        path = os.path.join(STATE_DIR, name)
        pid, _refusal = read_pid_checked(source, watcher_id)
        if pid is None:
            continue
        if not pid:



            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        if not _pid_alive(pid):




            record_death(source, watcher_id, pid)
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        started = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(path))
        )
        state, refusal = read_state_checked(source, watcher_id)
        state = state or {}
        rows.append({
            "source": source,
            "id": watcher_id,
            "pid": pid,
            "started": started,
            "last_event": (state.get("last_event") or {}).get("event", ""),
            "last_event_ts": (state.get("last_event") or {}).get("ts", ""),



            "last_emit": state.get("last_emit"),





            "state_refusal": refusal,



            "forked_fingerprint": state.get("forked_fingerprint"),
            "forked_fingerprint_error": state.get("forked_fingerprint_error"),




            "reloaded_at": state.get("reloaded_at"),
            "reloaded_fingerprint": state.get("reloaded_fingerprint"),
            "reloaded_fingerprint_error": state.get("reloaded_fingerprint_error"),
        })
    return rows


def poller_argv(source: str, watcher_id: str, only: list[str] | None = None) -> list[str]:






















    argv = [
        sys.executable,
        str(Path(__file__).parent.resolve() / "dispatcher.py"),
        POLL_SUBOP,
        source,
        watcher_id,
    ]
    if only:
        argv.append("only=" + ",".join(only))
    argv.append(CHANNEL_PREFIX + channel_key())
    return argv


def channel_key(state_dir: str | None = None) -> str:


















    resolved = STATE_DIR if state_dir is None else state_dir
    digest = hashlib.sha256(os.fsencode(os.path.normpath(resolved)))
    return digest.hexdigest()[:_CHANNEL_KEY_CHARS]


def pin_poller_env() -> None:











    os.environ["SUPERTOOL_WATCH_STATE_DIR"] = STATE_DIR





    os.environ["SUPERTOOL_WATCH_SOCK"] = SOCK_PATH


_SCAN_PS_ARGV = ("ps", "-axww", "-o", "pid=,args=")




_ps_scan_verdict: bool | None = None


def _ps_rows() -> list[tuple[int, list[str]]] | None:






    ps_bin = which_excluding_cwd("ps")
    if not ps_bin:
        return None
    try:
        proc = subprocess.run(
            [ps_bin, *_SCAN_PS_ARGV[1:]],
            capture_output=True, timeout=5, check=False,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    rows: list[tuple[int, list[str]]] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        rows.append((pid, parts[1:]))
    return rows


def ps_scan_supported() -> bool:



































    global _ps_scan_verdict
    if _ps_scan_verdict is not None:
        return _ps_scan_verdict
    _ps_scan_verdict = _probe_ps_scan()
    return _ps_scan_verdict


def _ran(argv: tuple[str, ...] | list[str]) -> int | None:

    try:
        proc = subprocess.run(list(argv), capture_output=True, timeout=5,
                              check=False, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode


def _probe_ps_scan() -> bool:
    ps_bin = which_excluding_cwd("ps")
    if ps_bin is None:
        return False
    scan = _ran((ps_bin, *_SCAN_PS_ARGV[1:]))
    if scan == 0:
        return True
    if scan is None:
        return True
    return _ran((ps_bin,)) != 0


def _labelled_with_path(tokens: list[str]) -> tuple[str | None, str, str, str] | None:













    for i, tok in enumerate(tokens):
        if not tok.replace("\\", "/").endswith(DISPATCHER_TAIL):
            continue
        if i + 3 < len(tokens) and tokens[i + 1] == POLL_SUBOP:
            channel = None
            for extra in tokens[i + 4:]:
                if extra.startswith(CHANNEL_PREFIX):
                    channel = extra[len(CHANNEL_PREFIX):]
                    break
            return channel, tokens[i + 2], tokens[i + 3], tok
    return None


def _labelled(tokens: list[str]) -> tuple[str | None, str, str] | None:













    label = _labelled_with_path(tokens)
    if label is None:
        return None
    channel, source, watcher_id, _path = label
    return channel, source, watcher_id








_DISPATCHER_VERSION_RE = re.compile(
    r"[\\/](\d+\.\d+\.\d+)[\\/]presets[\\/]watch[\\/]dispatcher\.py$"
)


def _version_from_dispatcher_path(path: str) -> str | None:







    match = _DISPATCHER_VERSION_RE.search(path.replace("\\", "/"))
    return match.group(1) if match else None


_INSTALLED_VERSION_RE = re.compile(r'^VERSION\s*=\s*"([^"]+)"', re.MULTILINE)


def installed_version() -> tuple[str | None, str]:















    root = Path(__file__).resolve().parent.parent.parent / "_supertool.py"
    try:
        text = root.read_text(encoding="utf-8")
    except (OSError, ValueError) as err:
        return None, f"could not read {root} ({type(err).__name__})"
    match = _INSTALLED_VERSION_RE.search(text)
    if not match:
        return None, f"no VERSION assignment found in {root}"
    return match.group(1), ""


def foreign_version_disclosure(versions: list[str | None], installed: str | None) -> str:














    seen = sorted({v for v in versions if v})
    if not seen:
        return "version not determined from its argv"
    if len(seen) > 1:
        return f"running mixed supertool versions ({', '.join(seen)})"
    version = seen[0]
    if installed is None:
        return (f"running supertool {version} (installed version could not "
                f"be determined)")
    if version == installed:
        return f"running supertool {version} (matches installed)"
    return f"running supertool {version} (installed: {installed})"


def scan_poller_pids() -> tuple[dict[tuple[str, str], list[int]], bool]:








































    census = poller_census()
    return census["mine"], census["scan_ok"]


def empty_census(scan_ok: bool) -> dict[str, Any]:








    return {"mine": {}, "other": {}, "unknown": {}, "other_versions": {},
            "scan_ok": scan_ok}


def poller_census() -> dict[str, Any]:



























    rows = _ps_rows()
    if rows is None:



        return empty_census(False)
    mine_key = channel_key()
    mine: dict[tuple[str, str], list[int]] = {}
    other: dict[str, dict[tuple[str, str], list[int]]] = {}
    other_versions: dict[str, list[str | None]] = {}
    unknown: dict[tuple[str, str], list[int]] = {}
    for pid, tokens in rows:
        label = _labelled_with_path(tokens)
        if label is None:
            continue
        channel, source, watcher_id, path = label
        slot = (source, watcher_id)
        if channel is None:



            unknown.setdefault(slot, []).append(pid)
        elif channel == mine_key:
            mine.setdefault(slot, []).append(pid)
        else:
            other.setdefault(channel, {}).setdefault(slot, []).append(pid)
            other_versions.setdefault(channel, []).append(
                _version_from_dispatcher_path(path))
    for bucket in (mine, unknown):
        for pids in bucket.values():
            pids.sort()
    for slots in other.values():
        for pids in slots.values():
            pids.sort()
    return {"mine": mine, "other": other, "unknown": unknown,
            "other_versions": other_versions, "scan_ok": True}


def channel_dirs() -> tuple[dict[str, str], str, str]:
















    base = naming.BASE_DIR
    names, state, why = naming.state_dir_listing(base)
    found: dict[str, str] = {}
    if state != naming.STATE_DIR_OK:
        return found, state, why




    found[channel_key(base)] = base
    prefix = "supertool-watch-"
    for name in names:
        if not name.startswith(prefix):
            continue
        path = os.path.join(base, name)



        if not os.path.isdir(path):
            continue
        found[channel_key(path)] = path
    return found, state, why


def watcher_pids(
    source: str,
    watcher_id: str,
    scan: tuple[dict[tuple[str, str], list[int]], bool] | None = None,
) -> dict[str, Any]:












    found, scan_ok = scan_poller_pids() if scan is None else scan
    tracked, tracked_refusal = read_pid_checked(source, watcher_id)
    tracked = tracked or 0
    tracked_alive = bool(tracked and _pid_alive(tracked))
    live = [pid for pid in found.get((source, watcher_id), []) if _pid_alive(pid)]
    pids = sorted(set(live) | ({tracked} if tracked_alive else set()))
    return {
        "tracked": tracked,




        "tracked_refusal": tracked_refusal,
        "tracked_alive": tracked_alive,
        "pids": pids,
        "untracked": [pid for pid in pids if pid != tracked],
        "scan_ok": scan_ok,
    }


def live_poller_pids(source: str, watcher_id: str) -> tuple[list[int], bool]:

    found, scan_ok = scan_poller_pids()
    return [pid for pid in found.get((source, watcher_id), []) if _pid_alive(pid)], scan_ok


def list_watchers(census: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], bool]:











    rows = list_active_pids()



    if census is None:
        census = poller_census()
    found, scan_ok = census["mine"], census["scan_ok"]
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (str(row["source"]), str(row["id"]))
        seen.add(key)
        pids = sorted(set(found.get(key, [])) | {int(row["pid"])})
        row["pids"] = pids
        row["extra"] = [pid for pid in pids if pid != int(row["pid"])]
        row["orphan"] = False
        row["dead"] = False
        row["deaths"] = deaths(*key)
    for (source, watcher_id), pids in sorted(found.items()):
        if (source, watcher_id) in seen:
            continue
        live = sorted(pid for pid in pids if _pid_alive(pid))
        if not live:
            continue
        state, refusal = read_state_checked(source, watcher_id)
        state = state or {}
        rows.append({
            "source": source,
            "id": watcher_id,
            "pid": live[0],
            "pids": live,
            "extra": live[1:],
            "orphan": True,
            "dead": False,
            "deaths": _deaths_in(state),
            "started": "",
            "last_event": (state.get("last_event") or {}).get("event", ""),
            "last_event_ts": (state.get("last_event") or {}).get("ts", ""),
            "last_emit": state.get("last_emit"),
            "state_refusal": refusal,
        })
    rows.extend(_lost_rows(seen | set(found)))
    return rows, scan_ok


def _lost_rows(covered: set[tuple[str, str]]) -> list[dict[str, Any]]:










    prefix = "supertool-watch-"
    suffix = ".state.json"
    out: list[dict[str, Any]] = []
    names, _state, _why = _state_dir_names()
    for name in names:
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        stem = name[len(prefix):-len(suffix)]
        if "__" not in stem:
            continue
        source, watcher_id = stem.split("__", 1)
        if (source, watcher_id) in covered:
            continue
        state, refusal = read_state_checked(source, watcher_id)
        if refusal:







            out.append({
                "source": source, "id": watcher_id, "pid": 0, "pids": [], "extra": [],
                "orphan": False, "dead": False, "deaths": [], "started": "",
                "last_event": "", "last_event_ts": "", "last_emit": None,
                "state_refusal": refusal,
            })
            continue
        state = state or {}
        recorded = _deaths_in(state)
        if not recorded:
            continue
        out.append({
            "source": source,
            "id": watcher_id,
            "pid": 0,
            "pids": [],
            "extra": [],
            "orphan": False,
            "dead": True,
            "deaths": recorded,
            "started": "",
            "last_event": (state.get("last_event") or {}).get("event", ""),
            "last_event_ts": (state.get("last_event") or {}).get("ts", ""),
            "last_emit": state.get("last_emit"),
            "state_refusal": "",
        })
    return out


def delivery_of(last_emit: Any, refusal: str = "") -> str:

















    if refusal:
        return EMIT_UNKNOWN
    if last_emit is None:


        return DELIVERY_NO_EMIT
    if not isinstance(last_emit, dict):
        return EMIT_UNKNOWN
    state = last_emit.get("state")
    if state in (EMIT_ACCEPTED, EMIT_NO_LISTENER, EMIT_UNKNOWN):
        return str(state)
    return EMIT_UNKNOWN


def source_fingerprint() -> tuple[str | None, str]:
























    root = Path(__file__).resolve().parent
    newest: float | None = None
    try:
        entries = list(root.rglob("*.py"))
    except OSError as err:
        return None, f"could not walk {root} ({type(err).__name__})"
    for entry in entries:
        if "__pycache__" in entry.parts:
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError as err:








            return None, f"could not stat {entry} ({type(err).__name__})"
        if newest is None or mtime > newest:
            newest = mtime
    if newest is None:
        return None, f"no .py files found under {root}"
    return f"{newest:.6f}", ""


def version_state_of(
    forked_fingerprint: Any,
    forked_fingerprint_error: Any,
    reloaded_at: Any = None,
    reloaded_fingerprint: Any = None,
    reloaded_fingerprint_error: Any = None,
) -> tuple[str, str]:


























    current, why = source_fingerprint()
    if current is None:
        return VERSION_UNKNOWN, f"this render could not read its own source ({why})"
    if reloaded_at:
        if reloaded_fingerprint_error:
            return VERSION_UNKNOWN, str(reloaded_fingerprint_error)
        if not reloaded_fingerprint:
            return (VERSION_UNKNOWN,
                    f"reloaded at {reloaded_at} but no fingerprint was recorded for "
                    f"that reload — whether poller.py itself is now stale cannot be "
                    f"established")
        if reloaded_fingerprint == current:
            return (VERSION_RELOADED,
                    f"reloaded at {reloaded_at}: poller.py is current as of that "
                    f"reload, but dispatcher.py and transport.py in this running "
                    f"process are still the fork-time copies — unwatch + watch is "
                    f"the only way to a fully current process")
        return (VERSION_RELOADED,
                f"reloaded at {reloaded_at} with fingerprint {reloaded_fingerprint}, "
                f"source is now {current} — presets/watch/ changed again since that "
                f"reload, and dispatcher.py/transport.py in this running process are "
                f"still the fork-time copies regardless — unwatch + watch is the "
                f"only way to a fully current process")
    if forked_fingerprint_error:
        return VERSION_UNKNOWN, str(forked_fingerprint_error)
    if not forked_fingerprint:
        return (VERSION_UNKNOWN,
                "no fork-time fingerprint recorded — this poller predates #2179, "
                "or was never labelled")
    if forked_fingerprint == current:
        return VERSION_CURRENT, ""
    return (VERSION_STALE,
            f"forked with fingerprint {forked_fingerprint}, source is now {current} — "
            f"a file under presets/watch/ changed since this poller started")


def _state_file_slots() -> list[tuple[str, str]]:







    prefix = "supertool-watch-"
    suffix = ".state.json"
    slots: list[tuple[str, str]] = []
    names, _state, _why = _state_dir_names()
    for name in names:
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        stem = name[len(prefix):-len(suffix)]
        if "__" not in stem:
            continue
        source, watcher_id = stem.split("__", 1)
        slots.append((source, watcher_id))
    return slots


def delivery_survey() -> list[tuple[str, str, str]]:













    out: list[tuple[str, str, str]] = []
    for source, watcher_id in _state_file_slots():
        state, refusal = read_state_checked(source, watcher_id)
        out.append((source, watcher_id,
                    delivery_of((state or {}).get("last_emit"), refusal)))
    return out


def emit_destinations() -> list[tuple[str, str, str]]:
















    out: list[tuple[str, str, str]] = []
    for source, watcher_id in _state_file_slots():
        state, refusal = read_state_checked(source, watcher_id)
        recorded = "" if refusal else (state or {}).get("sock_path")
        out.append((source, watcher_id,
                    recorded if isinstance(recorded, str) else ""))
    return out





_pid_alive = _proc.pid_alive
