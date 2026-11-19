"""Tests for `gl-runners:host` — the read-only per-host probe of runner boxes.

The probe answers "is this runner box out of RAM / disk / CPU" next to the
GitLab row, without anybody opening a shell. Every remote reading is
three-state: a value, a stated "unreadable", or (for config.toml) "not
readable" — never a blank that reads as zero.

Nothing here touches a real credential store: the resolver is a script
written into tmp_path by these tests.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PRESET_DIR = Path(__file__).parent.parent / "presets" / "gitlab"

_spec = importlib.util.spec_from_file_location("gl_host_probe", PRESET_DIR / "_host_probe.py")
assert _spec is not None and _spec.loader is not None
host_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(host_probe)

_rspec = importlib.util.spec_from_file_location("gl_runners_op_host", PRESET_DIR / "runners.py")
assert _rspec is not None and _rspec.loader is not None
runners_op = importlib.util.module_from_spec(_rspec)
_rspec.loader.exec_module(runners_op)

GIB = 1024 ** 3

PROBE_OUTPUT = f"""@@ free
              total        used        free      shared  buff/cache   available
Mem:    {16 * GIB}  {9 * GIB}  {1 * GIB}  0  {6 * GIB}  {6 * GIB}
Swap:   {2 * GIB}  {GIB // 2}  {GIB + GIB // 2}
@@rc 0
@@ loadavg
1.20 0.95 0.80 2/345 6789
@@rc 0
@@ nproc
8
@@rc 0
@@ dockerroot
/var/lib/docker
@@rc 0
@@ dfroot
Filesystem     1024-blocks      Used Available Capacity Mounted on
/dev/sda1        102400000  46080000  56320000      45% /
@@rc 0
@@ dfdocker
Filesystem     1024-blocks      Used Available Capacity Mounted on
/dev/sdb1        204800000 184320000  20480000      90% /var/lib/docker
@@rc 0
@@ dockervia
docker
@@rc 0
@@ dockerps
a1b2c3d4e5f6
d4e5f6a7b8c9
0a0b0c0d0e0f
@@rc 0
@@ dockerstats
runner-abc-project-1-concurrent-0|150.25%|1.5GiB / 15.6GiB|9.60%
runner-def-project-1-concurrent-1|3.10%|300MiB / 15.6GiB|1.88%
@@rc 0
@@ runnercfg
concurrent = 4
[session_server]
[[runners]]
  name = "dptools-runner-2"
  limit = 3
  [runners.docker]
  [[runners.docker.services]]
    name = "mysql:5.7"
[[runners]]
  name = "dptools-runner-2 V2"
@@path /etc/gitlab-runner/config.toml
@@rc 0
"""


# ---------------------------------------------------------------------------
# parsing the one probe output
# ---------------------------------------------------------------------------

def test_every_reading_is_parsed_from_the_probe_output() -> None:
    parsed = host_probe.parse_probe(PROBE_OUTPUT)
    assert parsed["memory"] == {"total": 16 * GIB, "used": 9 * GIB, "available": 6 * GIB}
    assert parsed["swap"] == {"total": 2 * GIB, "used": GIB // 2}
    assert parsed["load"] == (1.20, 0.95, 0.80)
    assert parsed["cpus"] == 8
    assert parsed["docker_root"] == "/var/lib/docker"
    root, docker = parsed["disk_root"], parsed["disk_docker"]
    assert (root["mount"], root["percent"]) == ("/", 45)
    assert (docker["mount"], docker["percent"]) == ("/var/lib/docker", 90)
    assert docker["available"] == 20480000 * 1024
    assert parsed["containers"] == 3
    assert [row[0] for row in parsed["stats"]] == [
        "runner-abc-project-1-concurrent-0", "runner-def-project-1-concurrent-1"]
    assert parsed["runner_config"] == {
        "concurrent": 4,
        "limits": [("dptools-runner-2", 3), ("dptools-runner-2 V2", None)],
        "path": "/etc/gitlab-runner/config.toml",
    }
    assert parsed["docker_via"] == "docker"


def test_docker_and_config_read_through_sudo_say_so() -> None:
    out = (PROBE_OUTPUT.replace("@@ dockervia\ndocker\n", "@@ dockervia\nsudo -n docker\n")
           .replace("@@path /etc/gitlab-runner/config.toml",
                    "@@path /srv/gitlab-runner/config/config.toml (sudo)"))
    parsed = host_probe.parse_probe(out)
    assert parsed["runner_config"]["path"] == "/srv/gitlab-runner/config/config.toml (sudo)"
    rendered = "\n".join(host_probe.render(parsed))
    assert "3 running (via sudo -n)" in rendered
    assert "from /srv/gitlab-runner/config/config.toml (sudo)" in rendered


def test_no_config_anywhere_names_every_place_it_looked() -> None:
    out = PROBE_OUTPUT.split("@@ runnercfg")[0] + "@@ runnercfg\nMISSING\n@@rc 0\n"
    config = host_probe.parse_probe(out)["runner_config"]
    assert isinstance(config, host_probe.Unreadable)
    for place in ("/etc/gitlab-runner", "/srv/gitlab-runner", "~/.gitlab-runner", "container mount"):
        assert place in config.reason


def test_sudo_is_only_ever_non_interactive_and_only_for_docker_or_awk() -> None:
    script = host_probe.REMOTE_PROBE
    uses = re.findall(r"\bsudo\s[^;|&)]*", script)
    assert uses, "the fallback is expected to exist"
    for use in uses:
        assert re.match(r"sudo -n (docker|awk)\b", use), use
    # `$D` may be `sudo -n docker`: every verb it runs must be a read.
    assert set(re.findall(r"\$D (\w+)", script)) <= {"ps", "stats", "inspect", "info", "system"}
    assert re.findall(r"\$D system (\w+)", script) == ["df"]


def test_a_service_name_inside_a_runner_is_not_taken_for_the_runner_name() -> None:
    """`[[runners.docker.services]] name = "mysql:5.7"` sits under a runner and
    is not one. Attributing the limit by the last `name =` seen would print a
    limit for `mysql:5.7`."""
    names = [name for name, _ in host_probe.parse_probe(PROBE_OUTPUT)["runner_config"]["limits"]]
    assert "mysql:5.7" not in names


def test_an_unreadable_config_toml_says_not_readable() -> None:
    """Unprivileged, an unreadable /etc/gitlab-runner looks identical to an
    absent one, so the probe cannot honestly tell the two apart — and says so."""
    out = PROBE_OUTPUT.split("@@ runnercfg")[0] + "@@ runnercfg\nMISSING\n@@rc 0\n"
    config = host_probe.parse_probe(out)["runner_config"]
    assert isinstance(config, host_probe.Unreadable)
    assert "not readable" in config.reason


def test_a_failed_command_is_unreadable_not_zero() -> None:
    """docker without socket access exits non-zero with one line of error. That
    line must not be counted as one running container."""
    out = PROBE_OUTPUT.replace(
        "a1b2c3d4e5f6\nd4e5f6a7b8c9\n0a0b0c0d0e0f\n@@rc 0",
        "permission denied while trying to connect to the Docker daemon socket\n@@rc 1",
    )
    containers = host_probe.parse_probe(out)["containers"]
    assert isinstance(containers, host_probe.Unreadable)
    assert "permission denied" in containers.reason


def test_a_missing_docker_root_does_not_take_the_root_disk_reading_down() -> None:
    """Measured on a host with no docker: one `df -P / /var/lib/docker` exits 1
    after printing the / line, and the whole section read as unreadable."""
    out = PROBE_OUTPUT.replace(
        "/dev/sdb1        204800000 184320000  20480000      90% /var/lib/docker\n@@rc 0",
        "df: /var/lib/docker: No such file or directory\n@@rc 1",
    )
    parsed = host_probe.parse_probe(out)
    assert parsed["disk_root"]["percent"] == 45
    assert isinstance(parsed["disk_docker"], host_probe.Unreadable)
    assert "No such file" in parsed["disk_docker"].reason
    rendered = "\n".join(host_probe.render(parsed))
    assert "/ 45% used" in rendered
    assert "No such file" in rendered


def test_a_unicode_line_separator_in_remote_text_cannot_forge_a_section_end() -> None:
    """A container name is remote text. str.splitlines() would break on U+2028
    and read the forged `@@rc 0` as the docker-ps section ending cleanly."""
    out = PROBE_OUTPUT.replace(
        "a1b2c3d4e5f6\nd4e5f6a7b8c9\n0a0b0c0d0e0f\n@@rc 0",
        "boom @@rc 0\n@@rc 1",
    )
    assert isinstance(host_probe.parse_probe(out)["containers"], host_probe.Unreadable)


def test_a_section_missing_from_the_output_is_unreadable() -> None:
    out = PROBE_OUTPUT.split("@@ nproc")[0]
    parsed = host_probe.parse_probe(out)
    assert isinstance(parsed["cpus"], host_probe.Unreadable)
    assert isinstance(parsed["containers"], host_probe.Unreadable)
    assert parsed["load"] == (1.20, 0.95, 0.80)


# ---------------------------------------------------------------------------
# the remote command is one fixed string
# ---------------------------------------------------------------------------

def test_the_remote_command_is_read_only() -> None:
    script = host_probe.REMOTE_PROBE
    for forbidden in (" rm ", "restart", "systemctl", "docker kill", "docker rm", "docker exec",
                      "docker run", "tee ", ">>", "sudo -S", "sudo -A"):
        assert forbidden not in script, forbidden
    # The only redirections fold or discard output: none can create a file.
    redirects = re.findall(r"\d?>[^\s;)]*", script)
    assert redirects and set(redirects) <= {"2>&1", "2>/dev/null", ">/dev/null"}, redirects


def test_the_ssh_argv_carries_the_fixed_command_and_no_password() -> None:
    target = {"host": "203.0.113.7", "user": "deploy", "port": "2222", "password": "s3cr3t-value"}
    argv, env = host_probe.ssh_invocation(target)
    assert argv[-1] == host_probe.REMOTE_PROBE
    assert "s3cr3t-value" not in " ".join(argv)
    assert argv[:2] == ["sshpass", "-e"]
    assert env == {"SSHPASS": "s3cr3t-value"}
    assert "deploy@203.0.113.7" in argv
    assert "2222" in argv
    # BatchMode=yes disables password authentication outright: with sshpass it
    # would fail every connection.
    assert "BatchMode=yes" not in argv


def test_a_key_based_target_runs_in_batch_mode() -> None:
    argv, env = host_probe.ssh_invocation({"host": "runner2.example", "user": "", "port": ""})
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert env == {}
    assert "runner2.example" in argv
    assert argv[-1] == host_probe.REMOTE_PROBE


# ---------------------------------------------------------------------------
# resolver hook — the project supplies the target, the op never sees the store
# ---------------------------------------------------------------------------

def _resolver(tmp_path: Path, body: str) -> dict:
    script = tmp_path / "resolver.py"
    script.write_text("import json, sys\n" + body, encoding="utf-8")
    return {"gl-runners": {"host_resolver": [sys.executable, str(script)]}}


def test_no_resolver_configured_is_skipped_not_empty() -> None:
    resolution = host_probe.resolve_target("dptools-runner-2", {})
    assert resolution.state == "skipped"
    assert "host_resolver" in resolution.message


def test_a_resolver_given_as_a_shell_string_is_refused() -> None:
    resolution = host_probe.resolve_target("x", {"gl-runners": {"host_resolver": "python3 r.py"}})
    assert resolution.state == "error"
    assert "list" in resolution.message


def test_the_resolver_receives_the_runner_description_and_returns_a_target(tmp_path: Path) -> None:
    cfg = _resolver(tmp_path, (
        "assert sys.argv[1] == 'dptools-runner-2'\n"
        "print(json.dumps({'host': '203.0.113.7', 'user': 'deploy', 'port': 22, 'password': 'pw'}))\n"
    ))
    resolution = host_probe.resolve_target("dptools-runner-2", cfg)
    assert resolution.state == "ok", resolution.message
    assert resolution.target == {"host": "203.0.113.7", "user": "deploy", "port": "22", "password": "pw"}


def test_exit_3_means_this_runner_has_no_host_entry(tmp_path: Path) -> None:
    cfg = _resolver(tmp_path, "sys.exit(3)\n")
    assert host_probe.resolve_target("hercule", cfg).state == "no-entry"


def test_a_resolver_failure_relays_stderr_never_stdout(tmp_path: Path) -> None:
    cfg = _resolver(tmp_path, (
        "print('ssh:deploy:hunter2@203.0.113.7')\n"
        "sys.stderr.write('placeholder values for this entry\\n')\n"
        "sys.exit(1)\n"
    ))
    resolution = host_probe.resolve_target("dptools-runner-2", cfg)
    assert resolution.state == "error"
    assert "placeholder values" in resolution.message
    assert "hunter2" not in resolution.message


def test_unparseable_resolver_output_is_never_echoed(tmp_path: Path) -> None:
    cfg = _resolver(tmp_path, "print('host=203.0.113.7 password=hunter2')\n")
    resolution = host_probe.resolve_target("dptools-runner-2", cfg)
    assert resolution.state == "error"
    assert "hunter2" not in resolution.message


@pytest.mark.parametrize("field,value", [
    ("host", "-oProxyCommand=touch x"),
    ("host", "a b"),
    ("user", "-l"),
    ("port", "22; id"),
])
def test_a_target_that_could_become_an_ssh_option_is_refused(tmp_path: Path, field: str, value: str) -> None:
    target = {"host": "203.0.113.7", "user": "deploy", "port": "22"}
    target[field] = value
    cfg = _resolver(tmp_path, f"print(json.dumps({target!r}))\n")
    resolution = host_probe.resolve_target("r", cfg)
    assert resolution.state == "error"
    assert field in resolution.message


# ---------------------------------------------------------------------------
# the op end to end: GitLab row joined with the host readings
# ---------------------------------------------------------------------------

def _fake_api(endpoint: str, paginate: bool = False, timeout: int = 20):
    if endpoint.startswith("projects/:id/runners"):
        return [{"id": 12, "description": "dptools-runner-2", "status": "online", "paused": False},
                {"id": 13, "description": "hercule", "status": "online", "paused": False}], None
    if endpoint == "runners/12":
        return {"tag_list": ["docker", "php"]}, None
    if endpoint == "runners/13":
        return {"tag_list": ["hercule"]}, None
    if "scope[]=running" in endpoint:
        return [{"runner": {"id": 12}}, {"runner": {"id": 12}}], None
    return [], None


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cfg = _resolver(tmp_path, (
        "if sys.argv[1] != 'dptools-runner-2': sys.exit(3)\n"
        "print(json.dumps({'host': '203.0.113.7', 'user': 'deploy', 'port': 22, 'password': 'pw-xyz'}))\n"
    ))
    class Calls(list):
        envs: list

    calls = Calls()
    envs: list[dict] = []
    real_run = subprocess.run

    def fake_run(argv, *args, **kwargs):
        if argv and argv[0] in ("ssh", "sshpass"):
            calls.append(list(argv))
            envs.append(kwargs.get("env") or {})
            return subprocess.CompletedProcess(argv, 0, PROBE_OUTPUT, "")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(runners_op, "_api", _fake_api)
    monkeypatch.setattr(runners_op._host_probe.subprocess, "run", fake_run)
    monkeypatch.setattr(runners_op._maintenance, "load_config", lambda: cfg)
    calls.envs = envs  # type: ignore[attr-defined]
    return calls


def test_host_mode_joins_the_gitlab_row_with_the_host_readings(wired, monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["runners.py", "host"])
    assert runners_op.main() == 0
    out = capsys.readouterr().out
    assert "dptools-runner-2" in out
    assert "online" in out and "docker,php" in out
    assert "running 2" in out
    assert "16.0G total" in out and "6.0G available" in out
    assert "8 CPU" in out
    assert "90%" in out
    assert "3 running" in out
    assert "concurrent 4" in out
    assert "pw-xyz" not in out
    # ...and it did travel: in the environment, never in argv.
    assert wired.envs[0].get("SSHPASS") == "pw-xyz"
    assert not any("pw-xyz" in arg for arg in wired[0])
    # hercule has no host entry: not probed, named once in the footer
    assert len(wired) == 1
    assert "no host entry: hercule" in out


def test_host_mode_for_one_runner_probes_only_that_runner(wired, monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["runners.py", "host", "dptools-runner-2"])
    assert runners_op.main() == 0
    assert len(wired) == 1


def test_host_mode_for_a_runner_without_entry_says_so(wired, monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["runners.py", "host", "hercule"])
    assert runners_op.main() == 1
    assert wired == []
    assert "no host entry" in capsys.readouterr().out


def test_an_unreachable_host_is_a_stated_failure(wired, monkeypatch, capsys) -> None:
    def refuse(argv, *args, **kwargs):
        if argv and argv[0] in ("ssh", "sshpass"):
            return subprocess.CompletedProcess(argv, 255, "", "ssh: connect to host 203.0.113.7 port 22: Connection timed out\n")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"host": "203.0.113.7"}), "")
    monkeypatch.setattr(runners_op._host_probe.subprocess, "run", refuse)
    monkeypatch.setattr(sys, "argv", ["runners.py", "host", "dptools-runner-2"])
    assert runners_op.main() == 1
    out = capsys.readouterr().out
    assert "Connection timed out" in out
    assert "16.0G" not in out


def test_a_missing_ssh_binary_is_reported_not_raised(wired, monkeypatch, capsys) -> None:
    """Windows raises FileNotFoundError for a missing sshpass/ssh (#997's class)."""
    def missing(argv, *args, **kwargs):
        if argv and argv[0] in ("ssh", "sshpass"):
            raise FileNotFoundError(2, "No such file or directory")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"host": "203.0.113.7", "password": "p"}), "")
    monkeypatch.setattr(runners_op._host_probe.subprocess, "run", missing)
    monkeypatch.setattr(sys, "argv", ["runners.py", "host", "dptools-runner-2"])
    assert runners_op.main() == 1
    assert "sshpass" in capsys.readouterr().out


def test_a_description_holding_a_colon_reaches_the_resolver_whole(wired, monkeypatch, capsys) -> None:
    """The core splits op args on ':'; `main` rejoins them."""
    seen: list[str] = []
    real = runners_op._host_probe.resolve_target

    def spy(description, cfg):
        seen.append(description)
        return real(description, cfg)
    monkeypatch.setattr(runners_op._host_probe, "resolve_target", spy)
    monkeypatch.setattr(sys, "argv", ["runners.py", "host", "edge", "runner"])
    runners_op.main()
    assert seen == ["edge:runner"]


def test_a_bare_host_with_an_empty_field_means_the_whole_fleet(wired, monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["runners.py", "host", ""])
    assert runners_op.main() == 0
    assert "no host entry: hercule" in capsys.readouterr().out


def test_an_exception_in_one_host_does_not_lose_the_others(wired, monkeypatch, capsys) -> None:
    real = runners_op._host_probe.resolve_target

    def flaky(description, cfg):
        if description == "hercule":
            raise ValueError("secret-ish detail")
        return real(description, cfg)
    monkeypatch.setattr(runners_op._host_probe, "resolve_target", flaky)
    monkeypatch.setattr(sys, "argv", ["runners.py", "host"])
    assert runners_op.main() == 1
    out = capsys.readouterr().out
    assert "16.0G total" in out
    assert "probe raised ValueError" in out
    assert "secret-ish" not in out


def test_a_missing_resolver_is_an_error_not_a_crash(tmp_path: Path) -> None:
    cfg = {"gl-runners": {"host_resolver": [str(tmp_path / "nope" / "resolver")]}}
    resolution = host_probe.resolve_target("r", cfg)
    assert resolution.state == "error"
    assert "not found" in resolution.message or "could not start" in resolution.message


def test_free_without_an_available_column_is_unreadable_not_cached() -> None:
    """procps < 3.3.10: column 6 is `cached`. Reading it as available is a
    confident wrong number."""
    old_free = (
        "@@ free\n"
        "             total       used       free     shared    buffers     cached\n"
        "Mem:     1000000     900000     100000          0      50000     400000\n"
        "Swap:          0          0          0\n"
        "@@rc 0\n"
    )
    parsed = host_probe.parse_probe(old_free)
    assert isinstance(parsed["memory"], host_probe.Unreadable)
    assert parsed["swap"] == {"total": 0, "used": 0}


def test_a_daemon_warning_on_docker_ps_is_not_a_container() -> None:
    out = PROBE_OUTPUT.replace(
        "a1b2c3d4e5f6\nd4e5f6a7b8c9\n0a0b0c0d0e0f\n",
        "WARNING: daemon is running in debug mode\n" + "a" * 12 + "\n",
    )
    assert host_probe.parse_probe(out)["containers"] == 1


@pytest.mark.skipif(os.name != "posix" or shutil.which("sh") is None, reason="runs REMOTE_PROBE under a local POSIX sh")
def test_the_real_remote_script_frames_each_section_with_its_own_exit_code(tmp_path: Path) -> None:
    """The `@@rc` framing is what the three-state promise rests on: run the
    fixed script under sh with a fake `free` that works and a fake `docker`
    that fails, and read the frames back."""
    gib = 1024 ** 3
    (tmp_path / "free").write_text(
        "#!/bin/sh\n"
        "echo '              total        used        free      shared  buff/cache   available'\n"
        f"echo 'Mem:    {16 * gib}  {9 * gib}  {gib}  0  {6 * gib}  {6 * gib}'\n"
        f"echo 'Swap:   {2 * gib}  0  {2 * gib}'\n",
        encoding="utf-8",
    )
    (tmp_path / "docker").write_text("#!/bin/sh\necho 'permission denied on socket' >&2\nexit 1\n", encoding="utf-8")
    (tmp_path / "nproc").write_text("#!/bin/sh\necho 4\n", encoding="utf-8")
    (tmp_path / "uname").write_text("#!/bin/sh\necho Linux\n", encoding="utf-8")
    for name in ("free", "docker", "nproc", "uname"):
        (tmp_path / name).chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}"}
    out = subprocess.run(["sh", "-c", host_probe.REMOTE_PROBE], capture_output=True,
                         text=True, encoding="utf-8", errors="replace",
                         timeout=30, env=env).stdout
    parsed = host_probe.parse_probe(out)
    assert parsed["memory"] == {"total": 16 * gib, "used": 9 * gib, "available": 6 * gib}
    assert parsed["cpus"] == 4
    assert isinstance(parsed["containers"], host_probe.Unreadable)
    assert "permission denied" in parsed["containers"].reason
    assert not isinstance(parsed["disk_root"], host_probe.Unreadable)


@pytest.mark.skipif(os.name != "posix" or shutil.which("sh") is None, reason="runs REMOTE_PROBE under a local POSIX sh")
def test_the_real_script_falls_back_to_sudo_n_and_finds_config_through_the_runner_container(tmp_path: Path) -> None:
    """Florian's fleet: the SSH user is not in the docker group, sudo -n works,
    and gitlab-runner runs as a container whose /etc/gitlab-runner is a bind
    mount. Fake docker only answers when the fake sudo marked it privileged."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for real in ("/etc/gitlab-runner/config.toml", "/srv/gitlab-runner/config/config.toml"):
        if os.path.exists(real):
            pytest.skip(f"{real} exists on this machine and would be read before the fake mount")
    mount = tmp_path / "runner config"
    mount.mkdir()
    (mount / "config.toml").write_text(
        'concurrent = 6\ncheck_interval = 0\n[[runners]] # glrt-HEADERCOMMENT\n  name = "box"\n'
        '  token = "glrt-SECRET"\n  limit_x = "glrt-LIMITX"\n  token_name = "glrt-NAME"\n  limit = 2\n',
        encoding="utf-8",
    )
    (bin_dir / "docker").write_text(
        "#!/bin/sh\n"
        "if [ -z \"$FAKE_PRIV\" ]; then echo 'permission denied while trying to connect to the Docker daemon socket' >&2; exit 1; fi\n"
        "case \"$*\" in\n"
        "  ps*--format*) echo 'dddddddddddd gitlab/gitlab-runner-helper:x86_64-v17'; echo 'aaaaaaaaaaaa gitlab/gitlab-runner:latest';;\n"
        "  ps*) echo bbbbbbbbbbbb; echo cccccccccccc;;\n"
        "  stats*) echo 'runner-x|5.00%|1GiB / 8GiB|12.5%';;\n"
        f"  inspect*aaaaaaaaaaaa*) echo '{mount}';;\n"
        "  inspect*) echo;;\n"
        "  *) exit 1;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (bin_dir / "sudo").write_text(
        "#!/bin/sh\n[ \"$1\" = -n ] || exit 1\nshift\nFAKE_PRIV=1 exec \"$@\"\n",
        encoding="utf-8",
    )
    (bin_dir / "uname").write_text("#!/bin/sh\necho Linux\n", encoding="utf-8")
    for name in ("docker", "sudo", "uname"):
        (bin_dir / name).chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home),
           "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    out = subprocess.run(["sh", "-c", host_probe.REMOTE_PROBE], capture_output=True,
                         text=True, encoding="utf-8", errors="replace",
                         timeout=30, env=env).stdout
    assert "glrt-" not in out
    parsed = host_probe.parse_probe(out)
    assert parsed["docker_via"] == "sudo -n docker"
    assert parsed["containers"] == 2
    assert [row[0] for row in parsed["stats"]] == ["runner-x"]
    config = parsed["runner_config"]
    assert not isinstance(config, host_probe.Unreadable), config
    assert config["concurrent"] == 6 and config["limits"] == [("box", 2)]
    assert config["path"] == f"{mount}/config.toml"


# ---------------------------------------------------------------------------
# macOS runner hosts (mac-mini): same script, Darwin branch
# ---------------------------------------------------------------------------

DARWIN_OUTPUT = """@@ os
Darwin
@@rc 0
@@ memsize
51539607552
@@rc 0
@@ vmstat
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               100000.
Pages active:                            1000000.
Pages inactive:                           200000.
Pages speculative:                         50000.
Pages wired down:                         300000.
@@rc 0
@@ swapusage
total = 2048.00M  used = 1024.50M  free = 1023.50M  (encrypted)
@@rc 0
@@ loadavg
{ 3.10 2.50 2.00 }
@@rc 0
@@ nproc
14
@@rc 0
@@ dfroot
Filesystem     1024-blocks      Used Available Capacity Mounted on
/dev/disk3s5     971350180 600000000 300000000    67%    /System/Volumes/Data
@@rc 0
@@ dockersysdf
TYPE            TOTAL     ACTIVE    SIZE      RECLAIMABLE
Images          12        3         8.5GB     6.1GB (71%)
Containers      3         2         120MB     10MB (8%)
Local Volumes   4         2         2.1GB     500MB (23%)
Build Cache     50        0         3.2GB     3.2GB
@@rc 0
@@ dockervia
docker
@@rc 0
@@ dockerps
a1b2c3d4e5f6
@@rc 0
@@ dockerstats
runner-mac-concurrent-0|80.00%|2GiB / 7.6GiB|26.3%
@@rc 0
@@ runnercfg
concurrent = 2
[[runners]]
  name = "mac-mini"
  limit = 2
@@path /Users/ci/.gitlab-runner/config.toml
@@rc 0
"""


def test_darwin_readings_come_from_sysctl_vm_stat_and_docker_system_df() -> None:
    parsed = host_probe.parse_probe(DARWIN_OUTPUT)
    available = (100000 + 200000 + 50000) * 16384
    assert parsed["memory"] == {"total": 51539607552, "used": 51539607552 - available,
                                "available": available}
    assert parsed["swap"] == {"total": 2048 * 1024 ** 2, "used": int(1024.5 * 1024 ** 2)}
    assert parsed["load"] == (3.10, 2.50, 2.00)
    assert parsed["cpus"] == 14
    assert parsed["disk_root"]["mount"] == "/System/Volumes/Data"
    assert parsed["docker_df"] == {"size": 13_920_000_000, "reclaimable": 9_810_000_000}
    assert parsed["runner_config"]["path"] == "/Users/ci/.gitlab-runner/config.toml"


def test_darwin_render_shows_the_docker_vm_disk_not_a_missing_linux_path() -> None:
    rendered = "\n".join(host_probe.render(host_probe.parse_probe(DARWIN_OUTPUT)))
    assert "48.0G total" in rendered
    assert "on 14 CPU" in rendered
    assert "/System/Volumes/Data 67% used" in rendered
    assert "docker vm : 13.9GB in images/containers/volumes/cache, 9.8GB reclaimable" in rendered
    assert "/var/lib/docker" not in rendered
    assert "unreadable" not in rendered


def test_a_vm_stat_without_page_size_is_unreadable_not_guessed() -> None:
    out = DARWIN_OUTPUT.replace("Mach Virtual Memory Statistics: (page size of 16384 bytes)",
                                "Mach Virtual Memory Statistics:")
    assert isinstance(host_probe.parse_probe(out)["memory"], host_probe.Unreadable)


def test_the_darwin_branch_looks_where_homebrew_puts_things() -> None:
    script = host_probe.REMOTE_PROBE
    assert "uname -s" in script
    for needle in ("/opt/homebrew/bin", "hw.memsize", "vm_stat", "vm.swapusage", "vm.loadavg",
                   "hw.ncpu", "docker system df", "/opt/homebrew/etc/gitlab-runner/config.toml",
                   "/usr/local/etc/gitlab-runner/config.toml"):
        assert needle in script, needle


def test_an_empty_password_is_key_auth_in_batch_mode_never_sshpass(tmp_path: Path) -> None:
    """mac-mini's entry carries an empty secret: the key comes from ~/.ssh/config."""
    cfg = _resolver(tmp_path, "print(json.dumps({'host': 'mac-mini.lan', 'user': 'ci', 'port': '22', 'password': ''}))\n")
    resolution = host_probe.resolve_target("mac-mini", cfg)
    assert resolution.state == "ok", resolution.message
    argv, env = host_probe.ssh_invocation(resolution.target)
    assert argv[0] == "ssh" and "sshpass" not in argv
    assert "BatchMode=yes" in argv
    assert env == {}


@pytest.mark.skipif(os.name != "posix" or shutil.which("sh") is None, reason="runs REMOTE_PROBE under a local POSIX sh")
def test_the_real_script_takes_the_darwin_branch_on_a_mac(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fakes = {
        "uname": "echo Darwin",
        "sysctl": (
            'case "$2" in hw.memsize) echo 51539607552;; vm.swapusage) echo "total = 2048.00M  used = 0.00M  free = 2048.00M";;'
            ' vm.loadavg) echo "{ 1.00 1.00 1.00 }";; hw.ncpu) echo 14;; *) exit 1;; esac'
        ),
        "vm_stat": (
            'echo "Mach Virtual Memory Statistics: (page size of 16384 bytes)"; echo "Pages free: 1000."; '
            'echo "Pages inactive: 2000."; echo "Pages speculative: 0."'
        ),
        "docker": (
            'case "$*" in "system df"*) echo "TYPE TOTAL ACTIVE SIZE RECLAIMABLE"; echo "Images 1 1 1GB 0B (0%)";;'
            ' "context show"*) echo desktop-linux;; "ps -q"*) echo aaaaaaaaaaaa;;'
            ' ps*) echo "aaaaaaaaaaaa gitlab/gitlab-runner:latest";;'
            ' stats*) echo "x|1.00%|1GiB / 8GiB|12.5%";; *) exit 1;; esac'
        ),
        "colima": "exit 1",
        "free": "echo should-not-run >&2; exit 9",
    }
    for name, body in fakes.items():
        (bin_dir / name).write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    home = tmp_path / "home"
    (home / ".gitlab-runner").mkdir(parents=True)
    (home / ".gitlab-runner" / "config.toml").write_text(
        'concurrent = 2\n[[runners]]\n  name = "mac-mini"\n  token = "glrt-MAC"\n', encoding="utf-8")
    env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    out = subprocess.run(["sh", "-c", host_probe.REMOTE_PROBE], capture_output=True,
                         text=True, encoding="utf-8", errors="replace",
                         timeout=30, env=env).stdout
    assert "glrt-" not in out
    assert "should-not-run" not in out
    parsed = host_probe.parse_probe(out)
    assert parsed["os"] == "Darwin"
    assert parsed["memory"]["available"] == 3000 * 16384
    assert parsed["cpus"] == 14
    assert parsed["docker_df"] == {"size": 1_000_000_000, "reclaimable": 0}
    assert parsed["containers"] == 1
    assert parsed["runner_config"]["path"] == f"{home}/.gitlab-runner/config.toml"
    assert parsed["vm"] == "none"
    assert "vm     : not readable" in "\n".join(host_probe.render(parsed))


# ---------------------------------------------------------------------------
# colima: the Docker VM's own memory/load/disk, read through `colima ssh`
# ---------------------------------------------------------------------------

COLIMA_OUTPUT = """@@ os
Darwin
@@rc 0
@@ memsize
51539607552
@@rc 0
@@ vmstat
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               500000.
Pages inactive:                           300000.
Pages speculative:                         54000.
@@rc 0
@@ vm
colima
@@rc 0
@@ free
               total        used        free      shared  buff/cache   available
Mem:     25100000000  4000000000  1000000000         0 20000000000 21100000000
Swap:              0           0           0
@@rc 0
@@ loadavg
2.00 1.50 1.00 3/400 999
@@rc 0
@@ nproc
8
@@rc 0
@@ dockerroot
/var/lib/docker
@@rc 0
@@ dfroot
Filesystem     1024-blocks      Used Available Capacity Mounted on
/dev/vda1         19000000   9000000  10000000      48% /
@@rc 0
@@ dfdocker
Filesystem     1024-blocks      Used Available Capacity Mounted on
/dev/vdb1        150390625  64667969  85722656      43% /var/lib/docker
@@rc 0
@@ vmssh
exit 0
@@rc 0
@@ dockervia
docker
@@rc 0
@@ dockerps
a1b2c3d4e5f6
@@rc 0
@@ dockerstats
runner-mac-concurrent-0|80.00%|2GiB / 23GiB|8.7%
@@rc 0
@@ runnercfg
concurrent = 2
@@path /Users/ci/.gitlab-runner/config.toml
@@rc 0
"""


def test_colima_memory_load_and_disk_are_the_vms() -> None:
    parsed = host_probe.parse_probe(COLIMA_OUTPUT)
    assert parsed["vm"] == "colima"
    assert parsed["memory"] == {"total": 25100000000, "used": 4000000000, "available": 21100000000}
    assert parsed["cpus"] == 8
    assert parsed["load"] == (2.00, 1.50, 1.00)
    assert parsed["disk_docker"]["mount"] == "/var/lib/docker"
    assert parsed["disk_docker"]["percent"] == 43
    assert parsed["host_memory"]["available"] == (500000 + 300000 + 54000) * 16384


def test_colima_render_labels_the_vm_and_keeps_one_mac_line() -> None:
    rendered = "\n".join(host_probe.render(host_probe.parse_probe(COLIMA_OUTPUT)))
    assert "vm     : colima" in rendered
    assert "mac host : 13.0G available of 48.0G" in rendered
    assert "23.4G total" in rendered
    assert "on 8 CPU" in rendered
    assert "docker /var/lib/docker 43% used" in rendered
    assert "docker vm :" not in rendered
    assert "/System/Volumes/Data" not in rendered


def test_a_failed_colima_ssh_says_so_instead_of_missing_sections() -> None:
    before, _, rest = COLIMA_OUTPUT.partition("@@ free")
    rest = rest.partition("@@ vmssh")[2]
    out = before + "@@ vmssh" + rest.replace("exit 0\n@@rc 0", "exit 255\n@@rc 1", 1)
    parsed = host_probe.parse_probe(out)
    for key in ("memory", "load", "cpus", "disk_docker"):
        assert isinstance(parsed[key], host_probe.Unreadable), key
        assert "colima ssh failed" in parsed[key].reason, key


@pytest.mark.skipif(os.name != "posix" or shutil.which("sh") is None, reason="runs REMOTE_PROBE under a local POSIX sh")
def test_the_real_script_reads_the_colima_vm_through_colima_ssh(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gib = 1024 ** 3
    fakes = {
        "uname": "echo Darwin",
        "sysctl": 'case "$2" in hw.memsize) echo 51539607552;; *) exit 1;; esac',
        "vm_stat": (
            'echo "Mach Virtual Memory Statistics: (page size of 16384 bytes)"; echo "Pages free: 1000."; '
            'echo "Pages inactive: 2000."'
        ),
        "docker": (
            'case "$*" in "context show"*) echo colima;; "ps -q"*) echo aaaaaaaaaaaa;; ps*) echo;;'
            ' stats*) echo "x|1.00%|1GiB / 8GiB|12.5%";; *) exit 1;; esac'
        ),
        "colima": 'case "$*" in "ssh -- "*) shift 2; exec "$@";; *) exit 0;; esac',
        "free": (
            "echo '              total        used        free      shared  buff/cache   available'; "
            f"echo 'Mem:    {24 * gib}  {4 * gib}  {gib}  0  {19 * gib}  {20 * gib}'; "
            "echo 'Swap:   0  0  0'"
        ),
        "nproc": "echo 8",
    }
    for name, body in fakes.items():
        (bin_dir / name).write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    out = subprocess.run(["sh", "-c", host_probe.REMOTE_PROBE], capture_output=True,
                         text=True, encoding="utf-8", errors="replace",
                         timeout=30, env=env).stdout
    parsed = host_probe.parse_probe(out)
    assert parsed["vm"] == "colima"
    assert parsed["memory"] == {"total": 24 * gib, "used": 4 * gib, "available": 20 * gib}
    assert parsed["cpus"] == 8
    assert parsed["host_memory"]["available"] == 3000 * 16384
    assert parsed["docker_df"] is None


def _cpustat(first: str, second: str) -> str:
    return f"@@ cpustat\ncpu  {first}\ncpu  {second}\n@@rc 0\n"


def test_cpu_split_is_the_share_of_the_one_second_delta() -> None:
    out = PROBE_OUTPUT + _cpustat("1000 0 500 8000 500 0 0 0", "1500 0 700 8200 600 0 0 0")
    cpu = host_probe.parse_probe(out)["cpustat"]
    assert cpu["busy"] == pytest.approx(70.0)
    assert cpu["iowait"] == pytest.approx(10.0)
    assert cpu["idle"] == pytest.approx(20.0)
    assert cpu["steal"] == 0


def test_cpu_line_renders_right_after_the_load_line() -> None:
    out = PROBE_OUTPUT + _cpustat("0 0 0 0 0 0 0 0", "62 0 0 20 18 0 0 0")
    lines = host_probe.render(host_probe.parse_probe(out))
    at = next(i for i, line in enumerate(lines) if line.startswith("  load   :"))
    assert lines[at + 1] == "  cpu    : 62% busy, 18% io wait, 20% idle (1s sample)"


def test_steal_is_counted_as_busy_and_shown_only_when_above_zero() -> None:
    out = PROBE_OUTPUT + _cpustat("0 0 0 0 0 0 0 0", "50 0 0 20 10 0 0 3")
    cpu = host_probe.parse_probe(out)["cpustat"]
    assert cpu["steal"] == pytest.approx(100 * 3 / 83)
    assert ", 4% steal" in "\n".join(host_probe.render(host_probe.parse_probe(out)))
    quiet = PROBE_OUTPUT + _cpustat("0 0 0 0 0 0 0 0", "50 0 0 20 10 0 0 0")
    assert "steal" not in "\n".join(host_probe.render(host_probe.parse_probe(quiet)))


@pytest.mark.parametrize("body", [
    "cpu  a b c d e f g h\ncpu  1 2 3 4 5 6 7 8",
    "cpu  1 2 3",
    "cpu  1 2 3 4 5 6 7 8",
    "cpu  1 2 3 4 5 6 7 8\ncpu  1 2 3 4 5 6 7 8",
    "garbage",
])
def test_an_unparseable_cpu_sample_is_unreadable_not_zero(body: str) -> None:
    out = PROBE_OUTPUT + f"@@ cpustat\n{body}\n@@rc 0\n"
    parsed = host_probe.parse_probe(out)
    assert isinstance(parsed["cpustat"], host_probe.Unreadable)
    assert "  cpu    : unreadable (" in "\n".join(host_probe.render(parsed))


def test_plain_macos_has_no_cpu_line() -> None:
    parsed = host_probe.parse_probe(DARWIN_OUTPUT)
    assert parsed["cpustat"] is None
    assert "cpu    :" not in "\n".join(host_probe.render(parsed))


def test_the_remote_script_samples_proc_stat_twice_on_linux_and_colima() -> None:
    assert host_probe.REMOTE_PROBE.count("s cpustat;") == 2
    colima_line = next(line for line in host_probe.REMOTE_PROBE.splitlines() if line.startswith("colima ssh"))
    inner = colima_line.split("sh -c '", 1)[1].rsplit("' 2>/dev/null", 1)[0]
    assert "cpustat" in inner and "'" not in inner


# ---------------------------------------------------------------------------
# tmp, runner config detail, docker info / df
# ---------------------------------------------------------------------------

def _tmp_sections(df_row: str, inode_row: str, fstype: str) -> str:
    return (
        "@@ tmpdf\nFilesystem      Size  Used Avail Use% Mounted on\n" + df_row + "\n@@rc 0\n"
        "@@ tmpinode\nFilesystem       Inodes   IUsed    IFree IUse% Mounted on\n" + inode_row + "\n@@rc 0\n"
        "@@ tmpfstype\n" + fstype + "\n@@rc 0\n"
    )


EXT4_TMP = _tmp_sections("/dev/sda1       410G   64G  327G  17% /",
                         "/dev/sda1      27262976  523001 26739975    2% /", "ext4 410G")


def test_tmp_on_the_root_filesystem_reads_size_used_avail_percent_and_inodes() -> None:
    tmp = host_probe.parse_probe(PROBE_OUTPUT + EXT4_TMP)["tmp"]
    assert tmp["size"] == "410G" and tmp["used"] == "64G" and tmp["available"] == "327G"
    assert tmp["percent"] == 17 and tmp["inode_percent"] == 2
    assert tmp["fstype"] == {"type": "ext4", "size": "410G"}
    rendered = "\n".join(host_probe.render(host_probe.parse_probe(PROBE_OUTPUT + EXT4_TMP)))
    assert ("  tmp    : 410G total, 64G used, 327G free (17% used), inodes 2% used; "
            "ext4; same filesystem as /") in rendered


def test_a_tmpfs_tmp_says_it_is_memory_backed_and_has_its_own_mount() -> None:
    out = PROBE_OUTPUT + _tmp_sections("tmpfs           7.8G  1.2G  6.6G  15% /tmp",
                                       "tmpfs           2000000     90  1999910    1% /tmp", "tmpfs 7.8G")
    rendered = "\n".join(host_probe.render(host_probe.parse_probe(out)))
    assert ("  tmp    : 7.8G total, 1.2G used, 6.6G free (15% used), inodes 1% used; "
            "tmpfs 7.8G (memory-backed); own mount /tmp") in rendered


def test_the_mount_line_fallback_still_tells_tmpfs() -> None:
    out = PROBE_OUTPUT + _tmp_sections("tmpfs           7.8G  1.2G  6.6G  15% /tmp",
                                       "tmpfs           2000000     90  1999910    1% /tmp",
                                       "tmpfs on /tmp type tmpfs (rw,nosuid,nodev)")
    assert host_probe.parse_probe(out)["tmp"]["fstype"] == {"type": "tmpfs", "size": None}
    assert "tmpfs (memory-backed)" in "\n".join(host_probe.render(host_probe.parse_probe(out)))


def test_a_filesystem_without_inode_counts_says_n_a_and_a_missing_type_says_so() -> None:
    out = PROBE_OUTPUT + _tmp_sections("/dev/sda1       410G   64G  327G  17% /",
                                       "/dev/sda1               0       0        0     - /", "")
    out = out.replace("@@ tmpfstype\n\n@@rc 0\n", "@@ tmpfstype\nfindmnt: not found\n@@rc 1\n")
    tmp = host_probe.parse_probe(out)["tmp"]
    assert tmp["inode_percent"] is None
    assert isinstance(tmp["fstype"], host_probe.Unreadable)
    rendered = "\n".join(host_probe.render(host_probe.parse_probe(out)))
    assert "inodes n/a" in rendered and "filesystem type unreadable (findmnt: not found)" in rendered


def test_a_df_without_inode_columns_is_unreadable_not_the_block_percent() -> None:
    """macOS `df -Pi` prints the POSIX block columns: column 5 is Capacity, not IUse%."""
    out = PROBE_OUTPUT + _tmp_sections("/dev/disk3s5  410G  64G  327G  17% /",
                                       "/dev/disk3s5  1000  300  700  30% /", "ext4 410G")
    out = out.replace("Filesystem       Inodes   IUsed    IFree IUse% Mounted on",
                      "Filesystem 512-blocks Used Available Capacity Mounted on")
    assert isinstance(host_probe.parse_probe(out)["tmp"]["inode_percent"], host_probe.Unreadable)


def test_an_unreadable_tmp_is_stated_not_zero() -> None:
    out = PROBE_OUTPUT + "@@ tmpdf\ndf: /tmp: No such file or directory\n@@rc 1\n"
    parsed = host_probe.parse_probe(out)
    assert isinstance(parsed["tmp"], host_probe.Unreadable)
    assert "  tmp    : unreadable (df: /tmp: No such file or directory)" in "\n".join(host_probe.render(parsed))


def _with_config(body: str) -> str:
    return (PROBE_OUTPUT.split("@@ runnercfg")[0] + "@@ runnercfg\n" + body
            + "@@path /etc/gitlab-runner/config.toml\n@@rc 0\n")


RUNNER_5_CONFIG = """concurrent = 4
[[runners]]
  name = "dptools-runner-5"
  limit = 2
  [runners.docker]
    privileged = true
    volumes = ["/cache"]
    shm_size = 0
    disable_cache = false
  [runners.docker.tmpfs]
  [runners.docker.services_tmpfs]
"""


def test_runner_docker_settings_are_read_per_runner_block() -> None:
    parsed = host_probe.parse_probe(_with_config(RUNNER_5_CONFIG))
    assert parsed["runner_detail"] == [{
        "name": "dptools-runner-5", "privileged": "true", "volumes": ["/cache"], "shm_size": "0",
        "disable_cache": "false", "tmpfs": [], "services_tmpfs": []}]
    lines = host_probe.render(parsed)
    at = next(i for i, line in enumerate(lines) if line.startswith("  runner : concurrent 4"))
    assert lines[at + 1] == ("           dptools-runner-5: privileged true, shm_size 0, disable_cache false, "
                             "volumes [/cache]")
    assert lines[at + 2] == "           dptools-runner-5: tmpfs empty; services_tmpfs empty"


def test_tmpfs_tables_and_a_multi_line_volumes_array_are_shown() -> None:
    body = RUNNER_5_CONFIG.replace(
        'volumes = ["/cache"]', 'volumes = [\n      "/cache",\n      "/var/run/docker.sock:/var/run/docker.sock"\n    ]'
    ).replace("[runners.docker.tmpfs]\n", '[runners.docker.tmpfs]\n    "/tmp" = "rw,noexec"\n'
    ).replace("[runners.docker.services_tmpfs]\n", '[runners.docker.services_tmpfs]\n    "/var/lib/mysql" = "rw"\n')
    parsed = host_probe.parse_probe(_with_config(body))
    runner = parsed["runner_detail"][0]
    assert runner["volumes"] == ["/cache", "/var/run/docker.sock:/var/run/docker.sock"]
    assert runner["tmpfs"] == ["/tmp=rw,noexec"] and runner["services_tmpfs"] == ["/var/lib/mysql=rw"]
    rendered = "\n".join(host_probe.render(parsed))
    assert "tmpfs /tmp=rw,noexec; services_tmpfs /var/lib/mysql=rw" in rendered


def test_a_runner_without_docker_keys_says_unset_and_a_service_name_is_not_a_runner() -> None:
    parsed = host_probe.parse_probe(_with_config(
        'concurrent = 1\n[[runners]]\n  name = "a"\n  [[runners.docker.services]]\n    name = "mysql:5.7"\n'))
    assert [r["name"] for r in parsed["runner_detail"]] == ["a"]
    assert "a: privileged unset, shm_size unset, disable_cache unset, volumes [unset]" in "\n".join(
        host_probe.render(parsed))


def test_an_unreadable_config_adds_no_detail_lines_and_does_not_fail() -> None:
    out = PROBE_OUTPUT.split("@@ runnercfg")[0] + "@@ runnercfg\nMISSING\n@@rc 0\n"
    parsed = host_probe.parse_probe(out)
    assert isinstance(parsed["runner_detail"], host_probe.Unreadable)
    rendered = "\n".join(host_probe.render(parsed))
    assert "runner : config.toml not found, or not readable" in rendered
    assert "privileged" not in rendered


DOCKER_INFO_OUTPUT = (
    "@@ dockerinfo\n/var/lib/docker overlay2\n@@rc 0\n"
    "@@ dockerdf\n"
    "TYPE            TOTAL     ACTIVE    SIZE      RECLAIMABLE\n"
    "Images          12        3         3.1GB     1.2GB (38%)\n"
    "Containers      4         2         50MB      0B (0%)\n"
    "Local Volumes   5         2         1GB       200MB (20%)\n"
    "Build Cache     30        0         9GB       9GB\n"
    "@@rc 0\n"
)


def test_docker_info_and_the_three_df_rows_are_parsed_and_shown() -> None:
    parsed = host_probe.parse_probe(PROBE_OUTPUT + DOCKER_INFO_OUTPUT)
    assert parsed["docker_info"] == {"root": "/var/lib/docker", "driver": "overlay2"}
    assert parsed["docker_system_df"] == {
        "images": {"total": 12, "active": 3, "size": "3.1GB", "reclaimable": "1.2GB"},
        "containers": {"total": 4, "active": 2, "size": "50MB", "reclaimable": "0B"},
        "volumes": {"total": 5, "active": 2, "size": "1GB", "reclaimable": "200MB"},
    }
    rendered = "\n".join(host_probe.render(parsed))
    assert "  docker info : root /var/lib/docker, driver overlay2" in rendered
    assert ("  docker df : images 12 (3.1GB, 1.2GB reclaimable); containers 4 (50MB, 0B reclaimable); "
            "volumes 5 (1GB, 200MB reclaimable)") in rendered


def test_a_docker_socket_the_user_cannot_reach_says_no_access() -> None:
    denied = "permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock"
    out = PROBE_OUTPUT + f"@@ dockerinfo\n{denied}\n@@rc 1\n@@ dockerdf\n{denied}\n@@rc 1\n"
    rendered = "\n".join(host_probe.render(host_probe.parse_probe(out)))
    assert "  docker info : no access (permission denied while trying to connect" in rendered
    assert "docker df" not in rendered


def test_a_docker_df_that_fails_alone_is_unreadable_next_to_the_info_line() -> None:
    out = PROBE_OUTPUT + (DOCKER_INFO_OUTPUT.split("@@ dockerdf")[0] + "@@ dockerdf\nboom\n@@rc 1\n")
    rendered = "\n".join(host_probe.render(host_probe.parse_probe(out)))
    assert "docker info : root /var/lib/docker, driver overlay2" in rendered
    assert "  docker df : unreadable (boom)" in rendered


def test_plain_macos_skips_tmp_and_the_docker_info_lines() -> None:
    parsed = host_probe.parse_probe(DARWIN_OUTPUT)
    assert parsed["tmp"] is None and parsed["docker_info"] is None and parsed["docker_system_df"] is None
    rendered = "\n".join(host_probe.render(parsed))
    assert "tmp    :" not in rendered and "docker info" not in rendered and "docker df" not in rendered


def test_the_remote_script_reads_tmp_on_linux_and_in_the_colima_vm() -> None:
    assert host_probe.REMOTE_PROBE.count("s tmpdf;") == 2
    colima_line = next(line for line in host_probe.REMOTE_PROBE.splitlines() if line.startswith("colima ssh"))
    inner = colima_line.split("sh -c '", 1)[1].rsplit("' 2>/dev/null", 1)[0]
    assert "tmpdf" in inner and "tmpinode" in inner and "tmpfstype" in inner


def _fake_bin(tmp_path: Path, docker_body: str) -> tuple[Path, dict]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uname").write_text("#!/bin/sh\necho Linux\n", encoding="utf-8")
    (bin_dir / "docker").write_text("#!/bin/sh\n" + docker_body + "\n", encoding="utf-8")
    (bin_dir / "sudo").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    for name in ("uname", "docker", "sudo"):
        (bin_dir / name).chmod(0o755)
    home = tmp_path / "home"
    (home / ".gitlab-runner").mkdir(parents=True)
    env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    return home, env


def _run_probe(env: dict) -> str:
    return subprocess.run(["sh", "-c", host_probe.REMOTE_PROBE], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=30, env=env).stdout


FULL_CONFIG = """concurrent = 4
check_interval = 0
[[runners]]
  name = "box"
  url = "https://gitlab-secret.example/"
  token = "glrt-SECRETTOKEN"
  executor = "docker"
  [runners.cache]
    [runners.cache.s3]
      AccessKey = "AKIAFAKEKEY"
      SecretKey = "hunter2"
  [runners.docker]
    image = "alpine"
    password = "hunter2-docker"
    privileged = true
    disable_cache = false
    volumes = [
      "/cache",
      "/var/run/docker.sock:/var/run/docker.sock"
    ]
    shm_size = 0
    [runners.docker.tmpfs]
      "/tmp" = "rw,noexec"
    [runners.docker.services_tmpfs]
  [runners.custom_build_dir]
    enabled = true
"""


@pytest.mark.skipif(os.name != "posix" or shutil.which("sh") is None, reason="runs REMOTE_PROBE under a local POSIX sh")
def test_the_real_script_lets_only_the_whitelisted_config_keys_cross_the_wire(tmp_path: Path) -> None:
    for real in ("/etc/gitlab-runner/config.toml", "/srv/gitlab-runner/config/config.toml"):
        if os.path.exists(real):
            pytest.skip(f"{real} exists on this machine and would be read before the fake one")
    home, env = _fake_bin(tmp_path, "echo 'permission denied while trying to connect to the Docker daemon socket' >&2; exit 1")
    (home / ".gitlab-runner" / "config.toml").write_text(FULL_CONFIG, encoding="utf-8")
    out = _run_probe(env)
    for secret in ("glrt-", "gitlab-secret", "AKIA", "hunter2", "alpine", "token", "url", "executor"):
        assert secret not in out, secret
    parsed = host_probe.parse_probe(out)
    assert parsed["runner_detail"] == [{
        "name": "box", "privileged": "true", "disable_cache": "false", "shm_size": "0",
        "volumes": ["/cache", "/var/run/docker.sock:/var/run/docker.sock"],
        "tmpfs": ["/tmp=rw,noexec"], "services_tmpfs": []}]
    assert parsed["runner_config"]["concurrent"] == 4
    assert isinstance(parsed["docker_info"], host_probe.Unreadable)
    assert "docker info : no access" in "\n".join(host_probe.render(parsed))
    assert not isinstance(parsed["tmp"], host_probe.Unreadable) and isinstance(parsed["tmp"]["percent"], int)


@pytest.mark.skipif(os.name != "posix" or shutil.which("sh") is None, reason="runs REMOTE_PROBE under a local POSIX sh")
def test_the_real_script_reads_docker_info_and_system_df_through_the_chosen_docker(tmp_path: Path) -> None:
    _, env = _fake_bin(tmp_path, (
        'case "$*" in\n'
        '  "info --format"*) echo "/var/lib/docker overlay2";;\n'
        '  "system df"*) echo "TYPE TOTAL ACTIVE SIZE RECLAIMABLE"; echo "Images 2 1 1GB 500MB (50%)"; '
        'echo "Containers 1 1 10MB 0B (0%)"; echo "Local Volumes 3 1 2GB 1GB (50%)";;\n'
        '  "ps -q"*) echo aaaaaaaaaaaa;;\n'
        '  *) exit 1;;\n'
        'esac'))
    parsed = host_probe.parse_probe(_run_probe(env))
    assert parsed["docker_info"] == {"root": "/var/lib/docker", "driver": "overlay2"}
    assert parsed["docker_system_df"]["volumes"] == {"total": 3, "active": 1, "size": "2GB", "reclaimable": "1GB"}
    assert parsed["docker_via"] == "docker"
