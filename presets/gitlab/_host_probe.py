#!/usr/bin/env python3
"""`gl-runners:host` — read-only resource readings from the runner boxes.

GitLab can say a runner is online and idle; it cannot say the box behind it
is out of RAM, swapping, at 98% disk on the docker root, or has fewer CPUs
than its `concurrent` promises. This module asks the box, once, over SSH.

Two halves, and the split is the security boundary:

    resolver  (the project's)  runner description -> {host, user, port, password?}
    probe     (this module)    one ssh call, one FIXED command string

The op never knows where targets come from. A project declares a resolver
command in `.supertool.json` (`gl-runners.host_resolver`, an argv list, never
a shell string); it is run with the runner description as its last argument
and answers on stdout with one JSON object. Contract:

    exit 0   stdout = {"host": ..., "user": ..., "port": ..., "password": ...}
             (only `host` is required; no `password` means key auth)
    exit 3   this runner has no host entry — skipped, named in a footer
    other    failure; the LAST LINE OF STDERR is relayed, flattened. stdout is
             never printed on any path, because on success it carries a secret.

The password, when there is one, reaches `sshpass -e` through the SSHPASS
environment variable — never argv, where `ps` would show it.

Every reading is three-state: a value, or an `Unreadable(reason)` that the
render prints as such. A docker socket the SSH user cannot reach is "docker:
unreadable (permission denied ...)", never "0 running".
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _untrusted  # noqa: E402  (everything the box prints is remote text)

# One fixed string, sent verbatim to every host. Nothing is interpolated into
# it — not the runner description, not the target — so no value from GitLab
# or from the resolver can reach a remote shell. Read-only. `sudo -n` (never a
# prompt, never a password) is used in exactly two places, both read-only and
# both only after the unprivileged try failed: docker ps/stats/inspect when the
# SSH user is not in the docker group, and `awk` reading config.toml. config.toml
# is filtered on the host to section headers and the three keys shown, so the
# runner tokens it also holds never cross the wire. Paths tried, in order:
# /etc/gitlab-runner, /srv/gitlab-runner/config, ~/.gitlab-runner, then the bind
# mount behind /etc/gitlab-runner of a running gitlab-runner container.
REMOTE_PROBE = r"""LC_ALL=C; export LC_ALL
s() { echo "@@ $1"; }
r() { echo "@@rc $?"; }
s free; free -b 2>&1; r
s loadavg; { cat</proc/loadavg; } 2>&1; r
s nproc; nproc 2>&1; r
d=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null); dn=""; [ -n "$d" ] || { d=/var/lib/docker; dn=" (assumed)"; }
s dockerroot; echo "$d$dn"; r
s dfroot; df -P / 2>&1; r
s dfdocker; df -P "$d" 2>&1; r
D=docker; docker ps -q >/dev/null 2>&1 || { sudo -n docker ps -q >/dev/null 2>&1 && D="sudo -n docker"; }
s dockervia; echo "$D"; r
s dockerps; $D ps -q 2>&1; r
s dockerstats; $D stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}' 2>&1; r
F='match($0, /^[ \t]*\[\[?[A-Za-z0-9_.-]+\]\]?/) { print substr($0, RSTART, RLENGTH); next } /^[ \t]*(concurrent|limit|name)[ \t]*=/'
rd() { o=$(awk "$F" "$1" 2>/dev/null) && [ -n "$o" ] && { printf '%s\n' "$o"; echo "@@path $1"; return 0; }; o=$(sudo -n awk "$F" "$1" 2>/dev/null) && [ -n "$o" ] && { printf '%s\n' "$o"; echo "@@path $1 (sudo)"; return 0; }; return 1; }
s runnercfg; ok=0; for p in /etc/gitlab-runner/config.toml /srv/gitlab-runner/config/config.toml "$HOME/.gitlab-runner/config.toml"; do rd "$p" && { ok=1; break; }; done
if [ "$ok" = 0 ]; then for c in $($D ps --format '{{.ID}} {{.Image}}' 2>/dev/null | awk '$2 ~ /gitlab-runner/ && $2 !~ /helper/ {print $1}'); do m=$($D inspect --format '{{range .Mounts}}{{if eq .Destination "/etc/gitlab-runner"}}{{.Source}}{{end}}{{end}}' "$c" 2>/dev/null); case $m in ""|*"
"*) continue;; esac; rd "$m/config.toml" && { ok=1; break; }; done; fi
[ "$ok" = 1 ] || echo MISSING; true; r
"""

NO_ENTRY_EXIT = 3
# Sized so two waves of 8 parallel hosts (15 + 30 each) fit inside the op's
# 120s ceiling in presets/gitlab.json.
RESOLVER_TIMEOUT = 15
SSH_CONNECT_TIMEOUT = 10
SSH_TIMEOUT = 30
_REASON_MAX = 160

# Anything the resolver hands back lands in an ssh argv. argv is not a shell,
# but `ssh` parses its own options: a host of `-oProxyCommand=...` is a command.
_HOST_RE = re.compile(r"^[A-Za-z0-9_.:\[\]-]+$")
_USER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_PORT_RE = re.compile(r"^[0-9]{1,5}$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")


class Unreadable:
    """A reading the probe could not take, and why. Never rendered as zero."""

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Unreadable) and other.reason == self.reason

    def __repr__(self) -> str:
        return f"Unreadable({self.reason!r})"


class Resolution:
    """state: ok | no-entry | skipped | error. `target` holds the secret, if any:
    it is handed to `probe` and never rendered."""

    __slots__ = ("state", "message", "target")

    def __init__(self, state: str, message: str = "", target: dict | None = None) -> None:
        self.state = state
        self.message = message
        self.target = target or {}


def _reason(text: str) -> str:
    lines = [line for line in _untrusted.split_lines(text or "") if line.strip()]
    last = lines[-1].strip() if lines else ""
    return _untrusted.flat(last)[:_REASON_MAX]


def resolve_target(description: str, cfg: dict) -> Resolution:
    gl_cfg = cfg.get("gl-runners") if isinstance(cfg, dict) else None
    resolver = gl_cfg.get("host_resolver") if isinstance(gl_cfg, dict) else None
    if resolver is None:
        return Resolution("skipped", "gl-runners:host needs a host resolver: set "
                          "gl-runners.host_resolver in .supertool.json to an argv list "
                          "(see docs/presets/gitlab.md, 'Runner host probe')")
    if not (isinstance(resolver, list) and resolver and all(isinstance(a, str) and a for a in resolver)):
        return Resolution("error", "gl-runners.host_resolver must be a non-empty argv list "
                          "of strings, never a shell string")
    try:
        result = subprocess.run(
            [*resolver, description], capture_output=True, text=True,
            timeout=RESOLVER_TIMEOUT, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return Resolution("error", f"host resolver not found: {_untrusted.flat(resolver[0])}")
    except PermissionError:
        return Resolution("error", f"host resolver not executable: {_untrusted.flat(resolver[0])}")
    except subprocess.TimeoutExpired:
        return Resolution("error", f"host resolver timed out after {RESOLVER_TIMEOUT}s")
    except (OSError, ValueError) as exc:
        # Windows raises a bare OSError (WinError 193) for a non-executable file.
        # The class only: the message can echo argv.
        return Resolution("error", f"host resolver could not start ({type(exc).__name__})")
    if result.returncode == NO_ENTRY_EXIT:
        return Resolution("no-entry")
    if result.returncode != 0:
        reason = _reason(result.stderr) or "no message on stderr"
        return Resolution("error", f"host resolver failed (exit {result.returncode}): {reason}")
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        data = None
    if not isinstance(data, dict):
        # Never echoed: on this path stdout is whatever the resolver printed,
        # and the resolver is the one component that holds secrets.
        return Resolution("error", f"host resolver output is not one JSON object "
                          f"({len(result.stdout)} bytes, not shown)")
    target = {
        "host": str(data.get("host") or ""),
        "user": str(data.get("user") or ""),
        "port": str(data.get("port") or ""),
    }
    if data.get("password"):
        target["password"] = str(data["password"])
    if not target["host"] or not _HOST_RE.match(target["host"]) or target["host"].startswith("-"):
        return Resolution("error", "host resolver returned an unusable host")
    if target["user"] and (not _USER_RE.match(target["user"]) or target["user"].startswith("-")):
        return Resolution("error", "host resolver returned an unusable user")
    if target["port"] and not _PORT_RE.match(target["port"]):
        return Resolution("error", "host resolver returned an unusable port")
    return Resolution("ok", target=target)


def ssh_invocation(target: dict) -> tuple[list[str], dict]:
    """The argv and the extra environment for one probe. Same options as the
    DVSI `server-diag` script (accept-new host keys, short connect timeout);
    BatchMode only for key auth, because BatchMode=yes forbids the password
    prompt that `sshpass` answers."""
    destination = f"{target['user']}@{target['host']}" if target.get("user") else target["host"]
    options = ["-o", "StrictHostKeyChecking=accept-new", "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}"]
    if target.get("port"):
        options = ["-p", target["port"], *options]
    password = target.get("password")
    if password:
        return ["sshpass", "-e", "ssh", *options, destination, REMOTE_PROBE], {"SSHPASS": password}
    return ["ssh", *options, "-o", "BatchMode=yes", destination, REMOTE_PROBE], {}


def probe(target: dict) -> tuple[str | None, str | None]:
    """Run the probe. Returns (output, None) or (None, reason)."""
    argv, extra_env = ssh_invocation(target)
    env = {**os.environ, **extra_env} if extra_env else None
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=SSH_TIMEOUT,
            encoding="utf-8", errors="replace", env=env, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None, f"{argv[0]} not found on PATH" + (
            " — password targets need sshpass installed" if argv[0] == "sshpass" else "")
    except subprocess.TimeoutExpired:
        return None, f"ssh timed out after {SSH_TIMEOUT}s"
    except (OSError, ValueError) as exc:
        # ValueError: a NUL in the resolver's password reaching env=. The class
        # only — never str(exc), which could carry the value.
        return None, f"{argv[0]} could not start ({type(exc).__name__})"
    if "@@ " not in result.stdout:
        reason = _reason(result.stderr) or _reason(result.stdout) or "no output"
        if result.returncode == 0:
            return None, (f"connected, but the probe printed no sections — is the remote "
                          f"login shell POSIX sh-compatible? ({reason})")
        return None, f"ssh failed (exit {result.returncode}): {reason}"
    return result.stdout, None


def _sections(text: str) -> dict[str, tuple[list[str], int | None]]:
    sections: dict[str, tuple[list[str], int | None]] = {}
    name: str | None = None
    body: list[str] = []
    # LF/CR/CRLF only: a U+2028 in a container name must not open a forged
    # `@@rc 0` line that str.splitlines() would honour (#1119's class).
    for line in _untrusted.split_lines(text):
        if line.startswith("@@rc ") and name is not None:
            try:
                rc: int | None = int(line[5:].strip())
            except ValueError:
                rc = None
            sections[name] = (body, rc)
            name, body = None, []
        elif line.startswith("@@ "):
            if name is not None:
                sections[name] = (body, None)
            name, body = line[3:].strip(), []
        elif name is not None:
            body.append(line)
    if name is not None:
        sections[name] = (body, None)
    return sections


def _section(sections: dict, name: str) -> list[str] | Unreadable:
    if name not in sections:
        return Unreadable("not in the probe output")
    body, rc = sections[name]
    if rc is None:
        return Unreadable("probe output cut short")
    if rc != 0:
        return Unreadable(_reason("\n".join(body)) or f"exit {rc}")
    return body


def _parse_free(lines):
    if isinstance(lines, Unreadable):
        return lines, lines
    memory: dict | Unreadable = Unreadable("no Mem: line")
    swap: dict | Unreadable = Unreadable("no Swap: line")
    # Column positions come from the header: procps before 3.3.10 and busybox
    # have no `available` column, and column 6 there is `cached`.
    header = next((line.split() for line in lines if "total" in line.split()), [])
    available_at = header.index("available") + 1 if "available" in header else None
    for line in lines:
        fields = line.split()
        try:
            if fields and fields[0] == "Mem:" and available_at is None:
                memory = Unreadable("free prints no 'available' column on this host")
            elif fields and fields[0] == "Mem:" and len(fields) > available_at:
                memory = {"total": int(fields[1]), "used": int(fields[2]),
                          "available": int(fields[available_at])}
            elif fields and fields[0] == "Swap:" and len(fields) >= 3:
                swap = {"total": int(fields[1]), "used": int(fields[2])}
        except ValueError:
            continue
    return memory, swap


def _parse_load(lines):
    if isinstance(lines, Unreadable):
        return lines
    try:
        one, five, fifteen = lines[0].split()[:3]
        return (float(one), float(five), float(fifteen))
    except (IndexError, ValueError):
        return Unreadable("unexpected /proc/loadavg format")


def _parse_int(lines):
    if isinstance(lines, Unreadable):
        return lines
    try:
        return int(lines[0].strip())
    except (IndexError, ValueError):
        return Unreadable("not a number")


def _parse_df(lines):
    """One `df -P PATH` — a separate call per path, so a docker root that does
    not exist cannot take the / reading down with it."""
    if isinstance(lines, Unreadable):
        return lines
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 6:
            continue
        try:
            return {
                "filesystem": fields[0],
                "size": int(fields[1]) * 1024,
                "available": int(fields[3]) * 1024,
                "percent": int(fields[4].rstrip("%")),
                "mount": " ".join(fields[5:]),
            }
        except ValueError:
            continue
    return Unreadable("no filesystem line")


def _parse_containers(lines):
    if isinstance(lines, Unreadable):
        return lines
    # Container ids only: a daemon WARNING line on stdout is not a container.
    return len([line for line in lines if _CONTAINER_ID_RE.match(line.strip())])


def _parse_stats(lines):
    if isinstance(lines, Unreadable):
        return lines
    return [tuple(cell.strip() for cell in line.split("|")) for line in lines if line.count("|") == 3]


def _parse_runner_config(lines):
    if isinstance(lines, Unreadable):
        return lines
    stripped = [line.strip() for line in lines]
    if "MISSING" in stripped:
        return Unreadable(_CONFIG_MISSING)
    concurrent: int | None = None
    limits: list[list] = []
    section = ""
    path: str | None = None
    for line in stripped:
        if line.startswith("@@path "):
            path = line[len("@@path "):]
            continue
        if line.startswith("["):
            section = line.split("#", 1)[0].strip()
            if section == "[[runners]]":
                limits.append([None, None])
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.split("#", 1)[0].strip()
        if section == "" and key == "concurrent":
            concurrent = int(value) if value.isdigit() else None
        elif section == "[[runners]]" and limits:
            if key == "name":
                limits[-1][0] = value.strip("\"'")
            elif key == "limit":
                limits[-1][1] = int(value) if value.isdigit() else None
    return {"concurrent": concurrent, "limits": [(name or "?", limit) for name, limit in limits],
            "path": path}


_CONFIG_MISSING = ("not found, or not readable even with sudo -n, at /etc/gitlab-runner, "
                   "/srv/gitlab-runner/config, ~/.gitlab-runner, or a gitlab-runner "
                   "container mount")


def _parse_root(lines):
    if isinstance(lines, Unreadable):
        return lines
    return lines[0].strip() if lines and lines[0].strip() else Unreadable("empty")


def parse_probe(text: str) -> dict:
    sections = _sections(text)
    memory, swap = _parse_free(_section(sections, "free"))
    return {
        "memory": memory,
        "swap": swap,
        "load": _parse_load(_section(sections, "loadavg")),
        "cpus": _parse_int(_section(sections, "nproc")),
        "docker_root": _parse_root(_section(sections, "dockerroot")),
        "disk_root": _parse_df(_section(sections, "dfroot")),
        "disk_docker": _parse_df(_section(sections, "dfdocker")),
        "docker_via": _parse_root(_section(sections, "dockervia")),
        "containers": _parse_containers(_section(sections, "dockerps")),
        "stats": _parse_stats(_section(sections, "dockerstats")),
        "runner_config": _parse_runner_config(_section(sections, "runnercfg")),
    }


def _gib(value: int) -> str:
    return f"{value / 1024 ** 3:.1f}G"


def _cell(value) -> str:
    return f"unreadable ({value.reason})" if isinstance(value, Unreadable) else ""


_STATS_MAX = 15


def render(parsed: dict) -> list[str]:
    out: list[str] = []
    memory, swap = parsed["memory"], parsed["swap"]
    out.append("  memory : " + (_cell(memory) or
               f"{_gib(memory['total'])} total, {_gib(memory['used'])} used, "
               f"{_gib(memory['available'])} available"))
    out.append("  swap   : " + (_cell(swap) or f"{_gib(swap['total'])} total, {_gib(swap['used'])} used"))
    load, cpus = parsed["load"], parsed["cpus"]
    if isinstance(load, Unreadable):
        out.append("  load   : " + _cell(load))
    else:
        line = f"  load   : {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}"
        if isinstance(cpus, Unreadable):
            line += f" (CPU count {_cell(cpus)})"
        else:
            line += f" on {cpus} CPU ({load[0] / max(cpus, 1):.2f} per CPU, 1m)"
        out.append(line)
    root, docker = parsed["disk_root"], parsed["disk_docker"]
    docker_root = parsed["docker_root"]
    docker_label = "docker" if isinstance(docker_root, Unreadable) else f"docker {_untrusted.flat(docker_root)}"
    for label, disk in (("/", root), (docker_label, docker)):
        if isinstance(disk, Unreadable):
            out.append(f"  disk   : {label} {_cell(disk)}")
            continue
        same = ""
        if disk is docker and not isinstance(root, Unreadable) and disk["filesystem"] == root["filesystem"]:
            same = " (same filesystem as /)"
        out.append(f"  disk   : {label} {disk['percent']}% used, {_gib(disk['available'])} free "
                   f"of {_gib(disk['size'])}{same}")
    containers = parsed["containers"]
    via = parsed.get("docker_via")
    through = " (via sudo -n)" if isinstance(via, str) and via.startswith("sudo") else ""
    out.append("  docker : " + (_cell(containers) or f"{containers} running{through}"))
    stats = parsed["stats"]
    if isinstance(stats, Unreadable):
        out.append("           stats " + _cell(stats))
    elif stats:
        out.append(f"           {'NAME':<44} {'CPU':>8} {'MEM%':>7}  MEM")
        for name, cpu, mem, mem_pct in stats[:_STATS_MAX]:
            out.append(f"           {_untrusted.flat(name)[:44]:<44} {_untrusted.flat(cpu):>8} "
                       f"{_untrusted.flat(mem_pct):>7}  {_untrusted.flat(mem)}")
        if len(stats) > _STATS_MAX:
            out.append(f"           … +{len(stats) - _STATS_MAX} more")
    config = parsed["runner_config"]
    if isinstance(config, Unreadable):
        out.append(f"  runner : config.toml {config.reason}")
    else:
        concurrent = config["concurrent"] if config["concurrent"] is not None else "unset"
        limits = ", ".join(
            f"{_untrusted.flat(name)}={limit if limit is not None else 'unset'}"
            for name, limit in config["limits"]) or "no [[runners]]"
        source = f"; from {_untrusted.flat(config['path'])}" if config.get("path") else ""
        out.append(f"  runner : concurrent {concurrent}; limit {limits}{source}")
    return out
