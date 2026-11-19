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
# is filtered on the host by a WHITELIST (section headers, the keys shown, and the
# body of the two tmpfs sections), never a blacklist, so the runner tokens, urls
# and cache credentials it also holds never cross the wire. Paths tried, in order:
# /etc/gitlab-runner, /srv/gitlab-runner/config, ~/.gitlab-runner, then the bind
# mount behind /etc/gitlab-runner of a running gitlab-runner container.
REMOTE_PROBE = r"""LC_ALL=C; export LC_ALL
s() { echo "@@ $1"; }
r() { echo "@@rc $?"; }
s os; uname -s 2>&1; r
PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"; export PATH
if [ "$(uname -s)" = Darwin ]; then
s memsize; sysctl -n hw.memsize 2>&1; r
s vmstat; vm_stat 2>&1; r
V=none; if command -v colima >/dev/null 2>&1; then if [ "$(docker context show 2>/dev/null)" = colima ] || colima status >/dev/null 2>&1; then V=colima; fi; fi
s vm; echo "$V"; r
if [ "$V" = colima ]; then
colima ssh -- sh -c 'LC_ALL=C; export LC_ALL; s() { echo "@@ $1"; }; r() { echo "@@rc $?"; }; s free; free -b 2>&1; r; s loadavg; awk 1 /proc/loadavg 2>&1; r; s nproc; nproc 2>&1; r; s cpustat; { sed -n 1p /proc/stat; sleep 1; sed -n 1p /proc/stat; } 2>&1; r; s dockerroot; echo /var/lib/docker; r; s dfroot; df -P / 2>&1; r; s dfdocker; df -P /var/lib/docker 2>&1; r; s tmpdf; df -Ph /tmp 2>&1; r; s tmpinode; df -Pi /tmp 2>&1; r; s tmpfstype; { findmnt -no FSTYPE,SIZE -T /tmp 2>/dev/null || mount 2>/dev/null | grep " on /tmp type "; }; r' 2>/dev/null; v=$?
s vmssh; echo "exit $v"; [ "$v" = 0 ]; r
else
s swapusage; sysctl -n vm.swapusage 2>&1; r
s loadavg; sysctl -n vm.loadavg 2>&1; r
s nproc; sysctl -n hw.ncpu 2>&1; r
s dfroot; df -P /System/Volumes/Data 2>&1; r
s dockersysdf; docker system df 2>&1; r
fi
set -- "$HOME/.gitlab-runner/config.toml" /opt/homebrew/etc/gitlab-runner/config.toml /usr/local/etc/gitlab-runner/config.toml
else
s free; free -b 2>&1; r
s loadavg; { cat</proc/loadavg; } 2>&1; r
s nproc; nproc 2>&1; r
s cpustat; { awk 'NR==1' /proc/stat; sleep 1; awk 'NR==1' /proc/stat; } 2>&1; r
d=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null); dn=""; [ -n "$d" ] || { d=/var/lib/docker; dn=" (assumed)"; }
s dockerroot; echo "$d$dn"; r
s dfroot; df -P / 2>&1; r
s dfdocker; df -P "$d" 2>&1; r
s tmpdf; df -Ph /tmp 2>&1; r
s tmpinode; df -Pi /tmp 2>&1; r
s tmpfstype; { findmnt -no FSTYPE,SIZE -T /tmp 2>/dev/null || mount 2>/dev/null | grep " on /tmp type "; }; r
set -- /etc/gitlab-runner/config.toml /srv/gitlab-runner/config/config.toml "$HOME/.gitlab-runner/config.toml"
fi
D=docker; docker ps -q >/dev/null 2>&1 || { sudo -n docker ps -q >/dev/null 2>&1 && D="sudo -n docker"; }
s dockervia; echo "$D"; r
s dockerps; $D ps -q 2>&1; r
s dockerstats; timeout 8 $D stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}' 2>&1; r
s dockerinfo; timeout 5 $D info --format '{{.DockerRootDir}} {{.Driver}}' 2>&1; r
s dockerdf; echo "not measured: docker system df takes up to a minute on a busy runner"; r
F='match($0, /^[ \t]*\[\[?[A-Za-z0-9_.-]+\]\]?/) { t = ($0 ~ /^[ \t]*\[runners\.docker\.(services_)?tmpfs\]/); v = 0; print substr($0, RSTART, RLENGTH); next } v { print; if ($0 ~ /\]/ || ++v == 21) v = 0; next } /^[ \t]*volumes[ \t]*=/ { print; if ($0 !~ /\]/) v = 1; next } t && $0 !~ /^[ \t]*(#|$)/ { print; next } /^[ \t]*(concurrent|limit|name|privileged|shm_size|disable_cache)[ \t]*=/'
rd() { o=$(awk "$F" "$1" 2>/dev/null) && [ -n "$o" ] && { printf '%s\n' "$o"; echo "@@path $1"; return 0; }; o=$(sudo -n awk "$F" "$1" 2>/dev/null) && [ -n "$o" ] && { printf '%s\n' "$o"; echo "@@path $1 (sudo)"; return 0; }; return 1; }
s runnercfg; ok=0; for p in "$@"; do rd "$p" && { ok=1; break; }; done
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
    limit = SSH_TIMEOUT * 2 if str(target.get("host", "")).endswith(".local") else SSH_TIMEOUT
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=limit,
            encoding="utf-8", errors="replace", env=env, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None, f"{argv[0]} not found on PATH" + (
            " — password targets need sshpass installed" if argv[0] == "sshpass" else "")
    except subprocess.TimeoutExpired:
        return None, f"ssh timed out after {limit}s"
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
        # Linux /proc/loadavg `1.20 0.95 ...`; macOS vm.loadavg `{ 1.20 0.95 0.80 }`.
        one, five, fifteen = lines[0].replace("{", " ").replace("}", " ").split()[:3]
        return (float(one), float(five), float(fifteen))
    except (IndexError, ValueError):
        return Unreadable("unexpected load average format")


def _parse_cpustat(lines):
    """Two /proc/stat `cpu ...` lines one second apart -> share of the delta."""
    if lines is None or isinstance(lines, Unreadable):
        return lines
    samples = []
    for line in lines:
        fields = line.split()
        if fields and fields[0] == "cpu":
            try:
                values = [int(v) for v in fields[1:9]]
            except ValueError:
                return Unreadable("unexpected /proc/stat format")
            if len(values) < 8:
                return Unreadable("unexpected /proc/stat format")
            samples.append(values)
    if len(samples) != 2:
        return Unreadable("unexpected /proc/stat format")
    user, nice, system, idle, iowait, irq, softirq, steal = (b - a for a, b in zip(*samples))
    total = user + nice + system + idle + iowait + irq + softirq + steal
    if total <= 0 or min(user, nice, system, idle, iowait, irq, softirq, steal) < 0:
        return Unreadable("no CPU time elapsed between the two samples")
    busy = user + nice + system + irq + softirq + steal
    return {"busy": 100 * busy / total, "iowait": 100 * iowait / total,
            "idle": 100 * idle / total, "steal": 100 * steal / total}


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


_RUNNER_KEYS = ("privileged", "shm_size", "disable_cache")
_TOML_STRING_RE = re.compile(r"\"([^\"]*)\"|'([^']*)'")


def _unquote(text: str) -> str:
    return text.strip().strip("\"'")


def _toml_strings(text: str) -> list[str]:
    return [a or b for a, b in _TOML_STRING_RE.findall(text)]


def _parse_runner_detail(lines):
    """Per `[[runners]]` block: the whitelisted docker keys and the two tmpfs
    tables, as the host-side awk let them through."""
    if isinstance(lines, Unreadable):
        return lines
    stripped = [line.strip() for line in lines]
    if "MISSING" in stripped:
        return Unreadable(_CONFIG_MISSING)
    runners: list[dict] = []
    section = ""
    volumes: list[str] | None = None
    for line in stripped:
        if not line or line.startswith("@@path "):
            continue
        if volumes is not None:
            volumes.append(line)
            if "]" in line:
                runners[-1]["volumes"] = _toml_strings(" ".join(volumes))
                volumes = None
            continue
        if line.startswith("["):
            section = line.split("#", 1)[0].strip()
            if section == "[[runners]]":
                runners.append({"name": None, "privileged": None, "volumes": None, "shm_size": None,
                                "disable_cache": None, "tmpfs": [], "services_tmpfs": []})
            continue
        if not runners:
            continue
        current = runners[-1]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.split("#", 1)[0].strip()
        if section == "[[runners]]" and key == "name":
            current["name"] = _unquote(value)
        elif section == "[runners.docker]" and key in _RUNNER_KEYS:
            current[key] = _unquote(value)
        elif section == "[runners.docker]" and key == "volumes":
            if "]" in value:
                current["volumes"] = _toml_strings(value)
            else:
                volumes = [value]
        elif section in ("[runners.docker.tmpfs]", "[runners.docker.services_tmpfs]") and key:
            table = "tmpfs" if section == "[runners.docker.tmpfs]" else "services_tmpfs"
            current[table].append(f"{_unquote(key)}={_unquote(value)}")
    if volumes is not None and runners:
        runners[-1]["volumes"] = _toml_strings(" ".join(volumes))
    return runners


def _parse_tmp(df_lines, inode_lines, fstype_lines):
    """`df -Ph /tmp`, `df -Pi /tmp` and the filesystem type: sizes stay as df
    printed them (human units), percentages become numbers."""
    if isinstance(df_lines, Unreadable):
        return df_lines
    result: dict | None = None
    for line in df_lines[1:]:
        fields = line.split()
        if len(fields) < 6:
            continue
        try:
            result = {"filesystem": fields[0], "size": fields[1], "used": fields[2],
                      "available": fields[3], "percent": int(fields[4].rstrip("%")),
                      "mount": " ".join(fields[5:])}
        except ValueError:
            continue
        break
    if result is None:
        return Unreadable("no filesystem line")
    result["inode_percent"] = _parse_inode_percent(inode_lines)
    result["fstype"] = _parse_fstype(fstype_lines)
    return result


def _parse_inode_percent(lines):
    if isinstance(lines, Unreadable):
        return lines
    if not any("IUsed" in line or "Inodes" in line for line in lines):
        return Unreadable("df prints no inode columns on this host")
    for line in lines:
        fields = line.split()
        if len(fields) >= 6 and "IUse%" not in fields and (fields[4].endswith("%") or fields[4] == "-"):
            return int(fields[4].rstrip("%")) if fields[4].rstrip("%").isdigit() else None
    return Unreadable("no inode line")


_MOUNT_TYPE_RE = re.compile(r" type (\S+)")


def _parse_fstype(lines):
    """`findmnt -no FSTYPE,SIZE` -> `ext4 410G`; the `mount` fallback line
    `tmpfs on /tmp type tmpfs (rw,...)` carries no size."""
    if isinstance(lines, Unreadable):
        return lines
    for line in lines:
        match = _MOUNT_TYPE_RE.search(line)
        if match:
            return {"type": match.group(1), "size": None}
        fields = line.split()
        if fields:
            return {"type": fields[0], "size": fields[1] if len(fields) > 1 else None}
    return Unreadable("no filesystem type")


def _parse_docker_info(lines):
    if isinstance(lines, Unreadable):
        return lines
    for line in lines:
        fields = line.split()
        if len(fields) >= 2:
            return {"root": " ".join(fields[:-1]), "driver": fields[-1]}
    return Unreadable("no DockerRootDir / Driver line")


_DOCKER_DF_RE = re.compile(r"^(Images|Containers|Local Volumes)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)")
_DOCKER_DF_LABEL = {"Images": "images", "Containers": "containers", "Local Volumes": "volumes"}


def _parse_docker_df(lines):
    """The Images, Containers and Local Volumes rows of `docker system df`."""
    if isinstance(lines, Unreadable):
        return lines
    rows: dict[str, dict] = {}
    for line in lines:
        if line.startswith("not measured"):
            return Unreadable(_untrusted.flat(line.strip())[:120])
        match = _DOCKER_DF_RE.match(line.strip())
        if match:
            rows[_DOCKER_DF_LABEL[match.group(1)]] = {
                "total": int(match.group(2)), "active": int(match.group(3)),
                "size": match.group(4), "reclaimable": match.group(5)}
    return rows or Unreadable("no Images / Containers / Volumes rows in docker system df")


_CONFIG_MISSING = ("not found, or not readable even with sudo -n, at /etc/gitlab-runner, "
                   "/srv/gitlab-runner/config, ~/.gitlab-runner, or a gitlab-runner "
                   "container mount (macOS: ~/.gitlab-runner, /opt/homebrew/etc, /usr/local/etc)")


def _parse_root(lines):
    if isinstance(lines, Unreadable):
        return lines
    return lines[0].strip() if lines and lines[0].strip() else Unreadable("empty")


_VM_PAGE_RE = re.compile(r"page size of (\d+) bytes")
_SWAP_RE = re.compile(r"total = ([\d.]+)([KMG])\s+used = ([\d.]+)([KMG])")
_SWAP_UNIT = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
_SYSDF_RE = re.compile(r"^(Images|Containers|Local Volumes|Build Cache)\s+\d+\s+\d+\s+(\S+)\s+(\S+)")
_DOCKER_UNIT = {"B": 1, "kB": 10 ** 3, "KB": 10 ** 3, "MB": 10 ** 6, "GB": 10 ** 9, "TB": 10 ** 12}


def _parse_darwin_memory(memsize, vmstat):
    """Total from hw.memsize; available = (free + inactive + speculative) pages
    x the page size vm_stat itself states. No stated page size: no guess."""
    total = _parse_int(memsize)
    if isinstance(total, Unreadable):
        return total
    if isinstance(vmstat, Unreadable):
        return vmstat
    page = next((int(m.group(1)) for m in map(_VM_PAGE_RE.search, vmstat) if m), None)
    if page is None:
        return Unreadable("vm_stat states no page size")
    pages = {}
    for line in vmstat:
        key, _, value = line.partition(":")
        if value.strip().rstrip(".").isdigit():
            pages[key.strip()] = int(value.strip().rstrip("."))
    if "Pages free" not in pages or "Pages inactive" not in pages:
        return Unreadable("vm_stat has no free/inactive page counts")
    available = (pages["Pages free"] + pages["Pages inactive"] + pages.get("Pages speculative", 0)) * page
    return {"total": total, "used": max(total - available, 0), "available": available}


def _parse_darwin_swap(lines):
    if isinstance(lines, Unreadable):
        return lines
    match = _SWAP_RE.search(" ".join(lines))
    if not match:
        return Unreadable("unexpected vm.swapusage format")
    return {"total": int(float(match.group(1)) * _SWAP_UNIT[match.group(2)]),
            "used": int(float(match.group(3)) * _SWAP_UNIT[match.group(4)])}


def _docker_bytes(text: str) -> int | None:
    match = re.match(r"^([\d.]+)([A-Za-z]+)$", text)
    if not match or match.group(2) not in _DOCKER_UNIT:
        return None
    return round(float(match.group(1)) * _DOCKER_UNIT[match.group(2)])


def _parse_system_df(lines):
    """`docker system df` — on macOS the Docker VM's disk is what fills up, and
    the host's df cannot see inside it."""
    if isinstance(lines, Unreadable):
        return lines
    size = reclaimable = 0
    rows = 0
    for line in lines:
        match = _SYSDF_RE.match(line.strip())
        if not match:
            continue
        used, freeable = _docker_bytes(match.group(2)), _docker_bytes(match.group(3))
        if used is None or freeable is None:
            return Unreadable(f"unexpected docker system df size {_untrusted.flat(line.strip())[:60]}")
        size, reclaimable, rows = size + used, reclaimable + freeable, rows + 1
    return {"size": size, "reclaimable": reclaimable} if rows else Unreadable("no rows in docker system df")


def parse_probe(text: str) -> dict:
    sections = _sections(text)
    os_name = _parse_root(_section(sections, "os"))
    vm = _parse_root(_section(sections, "vm")) if os_name == "Darwin" else None
    host_memory = None
    docker_df = None
    mac_only = os_name == "Darwin" and vm != "colima"
    if os_name == "Darwin":
        host_memory = _parse_darwin_memory(_section(sections, "memsize"), _section(sections, "vmstat"))
    if vm == "colima":
        # The VM's own free/loadavg/nproc/df, framed identically, via colima ssh.
        memory, swap = _parse_free(_section(sections, "free"))
    elif os_name == "Darwin":
        memory, swap = host_memory, _parse_darwin_swap(_section(sections, "swapusage"))
        docker_df = _parse_system_df(_section(sections, "dockersysdf"))
    else:
        memory, swap = _parse_free(_section(sections, "free"))
    parsed = {
        "os": os_name,
        "vm": vm,
        "host_memory": host_memory,
        "docker_df": docker_df,
        "memory": memory,
        "swap": swap,
        "load": _parse_load(_section(sections, "loadavg")),
        "cpus": _parse_int(_section(sections, "nproc")),
        "cpustat": (None if os_name == "Darwin" and vm != "colima"
                    else _parse_cpustat(_section(sections, "cpustat"))),
        "docker_root": _parse_root(_section(sections, "dockerroot")),
        "disk_root": _parse_df(_section(sections, "dfroot")),
        "disk_docker": _parse_df(_section(sections, "dfdocker")),
        "docker_via": _parse_root(_section(sections, "dockervia")),
        "containers": _parse_containers(_section(sections, "dockerps")),
        "stats": _parse_stats(_section(sections, "dockerstats")),
        "runner_config": _parse_runner_config(_section(sections, "runnercfg")),
        "runner_detail": _parse_runner_detail(_section(sections, "runnercfg")),
        "tmp": None if mac_only else _parse_tmp(_section(sections, "tmpdf"), _section(sections, "tmpinode"),
                                                _section(sections, "tmpfstype")),
        "docker_info": None if mac_only else _parse_docker_info(_section(sections, "dockerinfo")),
        "docker_system_df": None if mac_only else _parse_docker_df(_section(sections, "dockerdf")),
    }
    if vm == "colima":
        ssh = _section(sections, "vmssh")
        if isinstance(ssh, Unreadable):
            # The VM's sections never arrived: say why, not "not in the probe output".
            why = Unreadable(f"colima ssh failed ({ssh.reason})")
            for key in ("memory", "swap", "load", "cpus", "cpustat", "disk_root", "disk_docker", "docker_root", "tmp"):
                if isinstance(parsed[key], Unreadable):
                    parsed[key] = why
    return parsed


def _gib(value: int) -> str:
    return f"{value / 1024 ** 3:.1f}G"


def _cell(value) -> str:
    return f"unreadable ({value.reason})" if isinstance(value, Unreadable) else ""


_STATS_MAX = 15


def render(parsed: dict) -> list[str]:
    out: list[str] = []
    if parsed.get("vm") == "colima":
        out.append("  vm     : colima — memory, load and disk below are the Docker VM's")
        host = parsed.get("host_memory")
        out.append("  mac host : " + (_cell(host) or
                   f"{_gib(host['available'])} available of {_gib(host['total'])}"))
    elif parsed.get("os") == "Darwin":
        out.append("  vm     : not readable (only colima is read) — readings below are the Mac's")
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
    cpustat = parsed.get("cpustat")
    if isinstance(cpustat, Unreadable):
        out.append("  cpu    : " + _cell(cpustat))
    elif cpustat:
        line = (f"  cpu    : {cpustat['busy']:.0f}% busy, {cpustat['iowait']:.0f}% io wait, "
                f"{cpustat['idle']:.0f}% idle (1s sample)")
        if cpustat["steal"] > 0:
            line += f", {cpustat['steal']:.0f}% steal"
        out.append(line)
    root, docker = parsed["disk_root"], parsed["disk_docker"]
    docker_root = parsed["docker_root"]
    docker_label = "docker" if isinstance(docker_root, Unreadable) else f"docker {_untrusted.flat(docker_root)}"
    disks = [("/", root), (docker_label, docker)]
    mac_only = parsed.get("os") == "Darwin" and parsed.get("vm") != "colima"
    if mac_only:
        # APFS: / is the sealed system volume; the data volume is what fills.
        # The docker root lives inside the Docker VM, so the host df has no say.
        label = "/" if isinstance(root, Unreadable) else _untrusted.flat(root["mount"])
        disks = [(label, root)]
    for label, disk in disks:
        if isinstance(disk, Unreadable):
            out.append(f"  disk   : {label} {_cell(disk)}")
            continue
        same = ""
        if disk is docker and not isinstance(root, Unreadable) and disk["filesystem"] == root["filesystem"]:
            same = " (same filesystem as /)"
        out.append(f"  disk   : {label} {disk['percent']}% used, {_gib(disk['available'])} free "
                   f"of {_gib(disk['size'])}{same}")
    tmp = parsed.get("tmp")
    if isinstance(tmp, Unreadable):
        out.append("  tmp    : " + _cell(tmp))
    elif tmp:
        fstype = tmp["fstype"]
        if isinstance(fstype, Unreadable):
            kind = "filesystem type " + _cell(fstype)
        else:
            kind = _untrusted.flat(fstype["type"])
            if fstype["type"] == "tmpfs":
                kind += (f" {_untrusted.flat(fstype['size'])}" if fstype["size"] else "") + " (memory-backed)"
        inodes = tmp["inode_percent"]
        inode_text = ("inodes " + _cell(inodes)) if isinstance(inodes, Unreadable) else (
            "inodes n/a" if inodes is None else f"inodes {inodes}% used")
        where = ("same filesystem as /" if not isinstance(root, Unreadable) and tmp["mount"] == root["mount"]
                 else f"own mount {_untrusted.flat(tmp['mount'])}")
        out.append(f"  tmp    : {_untrusted.flat(tmp['size'])} total, {_untrusted.flat(tmp['used'])} used, "
                   f"{_untrusted.flat(tmp['available'])} free ({tmp['percent']}% used), {inode_text}; "
                   f"{kind}; {where}")
    if mac_only:
        vm = parsed.get("docker_df")
        out.append("  docker vm : " + (_cell(vm) or
                   f"{vm['size'] / 1e9:.1f}GB in images/containers/volumes/cache, "
                   f"{vm['reclaimable'] / 1e9:.1f}GB reclaimable"))
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
    info, system_df = parsed.get("docker_info"), parsed.get("docker_system_df")
    if isinstance(info, Unreadable) and isinstance(system_df, Unreadable):
        out.append(f"  docker info : no access ({info.reason})")
    elif info is not None:
        out.append("  docker info : " + (_cell(info) or
                   f"root {_untrusted.flat(info['root'])}, driver {_untrusted.flat(info['driver'])}"))
        if isinstance(system_df, Unreadable) and system_df.reason.startswith("not measured"):
            out.append("  docker df : " + system_df.reason)
        elif isinstance(system_df, Unreadable):
            out.append("  docker df : " + _cell(system_df))
        elif system_df:
            out.append("  docker df : " + "; ".join(
                f"{label} {row['total']} ({_untrusted.flat(row['size'])}, "
                f"{_untrusted.flat(row['reclaimable'])} reclaimable)" for label, row in system_df.items()))
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
        detail = parsed.get("runner_detail")
        for runner in detail if isinstance(detail, list) else []:
            name = _untrusted.flat(runner["name"] or "?")
            volumes = runner["volumes"]
            shown = "unset" if volumes is None else ", ".join(_untrusted.flat(v) for v in volumes) or "none"
            keys = ", ".join(f"{key} {_untrusted.flat(runner[key]) if runner[key] is not None else 'unset'}"
                             for key in _RUNNER_KEYS)
            out.append(f"           {name}: {keys}, volumes [{shown}]")
            out.append(f"           {name}: tmpfs "
                       + (", ".join(_untrusted.flat(e) for e in runner["tmpfs"]) or "empty")
                       + "; services_tmpfs "
                       + (", ".join(_untrusted.flat(e) for e in runner["services_tmpfs"]) or "empty"))
    return out
