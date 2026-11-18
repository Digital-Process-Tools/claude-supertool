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
    }


def test_a_service_name_inside_a_runner_is_not_taken_for_the_runner_name() -> None:
    """`[[runners.docker.services]] name = "mysql:5.7"` sits under a runner and
    is not one. Attributing the limit by the last `name =` seen would print a
    limit for `mysql:5.7`."""
    names = [name for name, _ in host_probe.parse_probe(PROBE_OUTPUT)["runner_config"]["limits"]]
    assert "mysql:5.7" not in names


def test_an_unreadable_config_toml_says_not_readable() -> None:
    out = PROBE_OUTPUT.split("@@ runnercfg")[0] + "@@ runnercfg\nNOT_READABLE\n@@rc 0\n"
    assert host_probe.parse_probe(out)["runner_config"] == host_probe.Unreadable("not readable")


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
    for forbidden in ("sudo", " rm ", "restart", "systemctl", "docker kill", "docker rm", "tee ", ">>"):
        assert forbidden not in script, forbidden
    # The only redirections are stderr folds: no output redirect can create a file.
    redirects = re.findall(r"\d?>[^\s;)]*", script)
    assert redirects and set(redirects) <= {"2>&1", "2>/dev/null"}, redirects


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
    for name in ("free", "docker", "nproc"):
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
