"""#1743 — the four MCP adapters ignored the flag the core stamps for them.

#475 stamps `SUPERTOOL_MCP_AUTOSPAWN=0` into every validator adapter's
environment and `tests/test_mcp_autospawn_provenance_475.py:219` pins that it
arrives. Nothing pinned that anyone *reads* it: the only reader was the core's
own `MCPClient`, in a different process family, reached only when an adapter
shells `supertool diag:`. The four MCP adapters go through
`presets/mcp/_spawn.ensure_daemon`, which looked at the socket, the pidfile and
the config fingerprint and never at the flag.

Measured on master, on a machine with `mcp-rector-warm` installed and no warm
daemon::

    SUPERTOOL_MCP_AUTOSPAWN=0 python3 validators/rector-mcp/rector-mcp.py x.php
    {"tool": "rector-mcp", ..., "ok": false, "duration_ms": 30194}

30.2s of spawn budget spent raising a daemon it had been told not to create,
and a receipt that says nothing about having been told.

Both halves are pinned here, because the suppression assertions are all
assertions that *nothing happened* and would pass just as well on a harness
that cannot spawn at all:

- MUST FIRE: with the flag unset, `daemon.py --detach` is still launched.
- MUST NOT FIRE: with the flag falsey, no process is launched, the call
  returns in a fraction of the spawn budget, and the adapter publishes a
  `skipped` receipt that names the flag.

And the payoff clause: suppression removes *creation*, never *use* — a daemon
that is already warm is still connected to.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket as _socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not hasattr(_socket, "AF_UNIX"),
    reason="MCP daemon paths require AF_UNIX — not available on Windows runners.",
)

ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = ROOT / "presets" / "mcp"

sys.path.insert(0, str(MCP_DIR))
sys.path.insert(0, str(ROOT / "presets"))

DAEMON_NAME = "phpstan-warm-1743"

# The real Popen, captured before any test replaces it.
_REAL_POPEN = subprocess.Popen

#: (adapter dir, env prefix). All four share one spawn path; all four ignored it.
ADAPTERS = [
    ("rector-mcp", "MCP_RECTOR"),
    ("phpstan-mcp", "MCP_PHPSTAN"),
    ("phpmd-mcp", "MCP_PHPMD"),
    ("phpunit-mcp", "MCP_PHPUNIT"),
]


def _load(mod_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _spawn_module():
    import _spawn  # noqa: PLC0415

    return _spawn


class _FakePopen:
    """Stands in for `daemon.py --detach` — records, launches nothing."""

    def __init__(self, args, **kwargs):
        self.args = args

    def wait(self, timeout=None):  # noqa: ANN001
        return 0


@pytest.fixture
def spawns(monkeypatch):
    """Every `subprocess.Popen` the spawn path attempts, in order."""
    calls: list = []

    def capture(args, **kwargs):  # noqa: ANN001
        calls.append(list(args))
        return _FakePopen(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", capture)
    return calls


@pytest.fixture
def runtime(monkeypatch):
    """A short runtime dir — AF_UNIX paths cap near 104 bytes.

    Neither `tmp_path` nor `$TMPDIR` is short enough on macOS: pytest's is
    `/private/var/folders/ys/qt5zq.../T/pytest-of-.../`, and `$TMPDIR` is the
    `/private/var/folders/...` prefix of it. `bind()` on a path over the cap
    raises `OSError: AF_UNIX path too long`, which is a broken harness wearing
    the costume of a daemon that did not come up. `/tmp` where it exists,
    `$TMPDIR` elsewhere (Windows never reaches here — the module skips).
    """
    parent = "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()
    d = tempfile.mkdtemp(prefix="st1743-", dir=parent)
    monkeypatch.setenv("SUPERTOOL_RUNTIME_DIR", d)
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def project(tmp_path):
    """A project whose mcp spec names a real (inert) binary and config file."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "phpstan.neon").write_text("parameters:\n  level: 8\n", encoding="utf-8")
    binary = proj / "mcp-warm-stub"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    (proj / ".supertool.json").write_text(
        json.dumps({"mcp": {DAEMON_NAME: {"cmd": [str(binary)], "idle_timeout": 1800}}}),
        encoding="utf-8",
    )
    return proj


# ---------------------------------------------------------------------------
# The shared spawn path — presets/mcp/_spawn.ensure_daemon
# ---------------------------------------------------------------------------


class TestSpawnPathHonoursTheFlag:
    def test_the_probe_can_see_a_spawn(self, runtime, project, spawns, monkeypatch):
        """MUST FIRE. Without this every assertion below passes on a harness
        that spawns nothing for reasons that have nothing to do with the flag —
        a bad runtime dir, an unimportable module, a fixture that never ran."""
        monkeypatch.delenv("SUPERTOOL_MCP_AUTOSPAWN", raising=False)
        sp = _spawn_module()

        with pytest.raises(RuntimeError):
            sp.ensure_daemon(str(project), DAEMON_NAME, spawn_timeout=0.2)

        assert spawns, (
            "harness is blind: an unsuppressed caller must still launch "
            "daemon.py, and every suppression assertion in this file is "
            "worthless if it does not"
        )
        assert any("daemon.py" in str(a) for a in spawns[0]), (
            f"expected daemon.py in the spawn argv, got {spawns[0]!r}"
        )

    def test_suppressed_caller_launches_nothing(
        self, runtime, project, spawns, monkeypatch
    ):
        """#1743: the flag is stamped for this process. It must be obeyed here."""
        monkeypatch.setenv("SUPERTOOL_MCP_AUTOSPAWN", "0")
        sp = _spawn_module()

        with pytest.raises(sp.AutospawnSuppressed) as exc:
            sp.ensure_daemon(str(project), DAEMON_NAME, spawn_timeout=0.2)

        assert spawns == [], (
            f"#1743: SUPERTOOL_MCP_AUTOSPAWN=0 forbids creating a daemon, and "
            f"the shared spawn path launched {len(spawns)}: {spawns!r}"
        )
        assert "SUPERTOOL_MCP_AUTOSPAWN" in str(exc.value), (
            f"the refusal must name the flag that caused it — a knob that "
            f"works silently is indistinguishable from one still ignored. "
            f"got: {exc.value!r}"
        )

    def test_suppressed_caller_does_not_burn_the_spawn_budget(
        self, runtime, project, spawns, monkeypatch
    ):
        """The measured cost of #1743: 30.2s of a 30s budget, with the flag at 0.

        A generous budget and a wall-clock ceiling two orders below it: if the
        poll loop runs at all this fails, and it cannot pass by accident.
        """
        monkeypatch.setenv("SUPERTOOL_MCP_AUTOSPAWN", "0")
        sp = _spawn_module()

        t0 = time.monotonic()
        with pytest.raises(sp.AutospawnSuppressed):
            sp.ensure_daemon(str(project), DAEMON_NAME, spawn_timeout=30.0)
        elapsed = time.monotonic() - t0

        assert elapsed < 2.0, (
            f"#1743: a suppressed caller must fail fast, not poll a socket "
            f"nobody was allowed to bind — took {elapsed:.1f}s of a 30s budget"
        )

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", " 0 "])
    def test_every_falsey_spelling_suppresses(
        self, runtime, project, spawns, monkeypatch, value
    ):
        """Same vocabulary as the core's own reader — provenance is a flag, not
        a trivia quiz about spelling, and two readers of one variable that
        disagree about `off` are worse than one reader."""
        monkeypatch.setenv("SUPERTOOL_MCP_AUTOSPAWN", value)
        sp = _spawn_module()
        # The concrete type, not RuntimeError: the spawn timeout is also a
        # RuntimeError, so the looser spelling could not tell "declined" from
        # "tried and failed" — which is the distinction under test.
        with pytest.raises(sp.AutospawnSuppressed):
            sp.ensure_daemon(str(project), DAEMON_NAME, spawn_timeout=0.2)
        assert spawns == [], f"spawned with SUPERTOOL_MCP_AUTOSPAWN={value!r}"

    @pytest.mark.parametrize("value", ["1", "true", "yes"])
    def test_truthy_values_still_spawn(
        self, runtime, project, spawns, monkeypatch, value
    ):
        """The other direction. `mcp_autospawn: true` stamps `1`, and a
        validator whose budget covers a cold start must still get one."""
        monkeypatch.setenv("SUPERTOOL_MCP_AUTOSPAWN", value)
        sp = _spawn_module()
        with pytest.raises(RuntimeError):
            sp.ensure_daemon(str(project), DAEMON_NAME, spawn_timeout=0.2)
        assert spawns, f"opted-in caller must still spawn, AUTOSPAWN={value!r}"

    def test_the_core_and_the_spawn_path_read_the_same_vocabulary(self):
        """One variable, two readers, one set of falsey spellings.

        The core's reader is `_mcp_autospawn_allowed`. If the two drift, a
        caller that suppressed the core's client would still get a daemon out
        of the adapter path — which is #1743 again, one spelling at a time.
        """
        import supertool  # noqa: PLC0415

        sp = _spawn_module()
        assert sp.AUTOSPAWN_ENV == supertool._MCP_AUTOSPAWN_ENV
        assert set(sp.AUTOSPAWN_FALSEY) == set(supertool._MCP_AUTOSPAWN_FALSEY)

    def test_suppression_removes_creation_never_use(
        self, runtime, project, spawns, monkeypatch
    ):
        """The payoff clause, and the guard against fixing this by making the
        MCP validators inert. A warm daemon is still connected to."""
        monkeypatch.setenv("SUPERTOOL_MCP_AUTOSPAWN", "0")
        sp = _spawn_module()
        sock_path, pid_path = sp.socket_pid_paths(str(project), DAEMON_NAME)

        srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        try:
            srv.bind(sock_path)
            srv.listen(8)
            Path(pid_path).write_text(str(os.getpid()), encoding="utf-8")
            sp.write_fingerprint(
                sock_path,
                sp.config_fingerprint(
                    sp.load_spec(DAEMON_NAME, str(project)), str(project)
                ),
            )

            got = sp.ensure_daemon(str(project), DAEMON_NAME, spawn_timeout=0.2)
            assert got == sock_path
        finally:
            srv.close()
        assert spawns == [], "a warm daemon must be used, not duplicated"


# ---------------------------------------------------------------------------
# The adapters — what the receipt says when a spawn is suppressed
# ---------------------------------------------------------------------------


def _adapter(name: str, prefix: str, project: Path, monkeypatch):
    monkeypatch.setenv(prefix + "_DAEMON_NAME", DAEMON_NAME)
    monkeypatch.setenv(prefix + "_BIN", str(project / "mcp-warm-stub"))
    monkeypatch.setenv(prefix + "_WORKING_DIR", str(project))
    mod = _load(
        name.replace("-", "_") + "_1743",
        ROOT / "validators" / name / (name + ".py"),
    )
    mod.SPAWN_TIMEOUT_SEC = 0.2
    return mod


def _drive(mod, name: str, target: Path) -> int:
    """Run the adapter the way its `__main__` block does.

    Through `refusal.guard_main`, not a bare `mod.main(...)`: the crash net
    lives in the `if __name__ == "__main__"` line (#1697), so a bare call lets
    an exception out of the adapter and past the receipt — which is the status
    quo for the unsuppressed control here, and would make that control fail for
    a reason unrelated to the flag.
    """
    return mod._refusal.guard_main(name, mod.main, [name, str(target)])


@pytest.mark.parametrize("name,prefix", ADAPTERS, ids=[a[0] for a in ADAPTERS])
class TestAdapterReceipts:
    def test_the_probe_can_see_this_adapter_spawn(
        self, name, prefix, runtime, project, spawns, monkeypatch, capsys, tmp_path
    ):
        """MUST FIRE, per adapter. Four adapters, four copies of the same call:
        a per-adapter control is the only thing that tells "this one honours
        the flag" from "this one never reached the spawn path at all"."""
        monkeypatch.delenv("SUPERTOOL_MCP_AUTOSPAWN", raising=False)
        mod = _adapter(name, prefix, project, monkeypatch)
        target = tmp_path / "x.php"
        target.write_text("<?php\n", encoding="utf-8")

        _drive(mod, name, target)
        capsys.readouterr()

        assert spawns, (
            f"{name}: harness blind — an unsuppressed run must reach the spawn "
            f"path, or the suppression case below proves nothing"
        )

    def test_suppressed_adapter_spawns_nothing_and_says_so(
        self, name, prefix, runtime, project, spawns, monkeypatch, capsys, tmp_path
    ):
        """#1743, end to end: drive the adapter with the flag at `0` and assert
        no daemon was created — and that the receipt is the third state,
        naming the flag. `ok: true` here would be a clean verdict about a file
        nothing opened; `ok: false` would be a finding about a file whose only
        defect is that nobody was allowed to look at it."""
        monkeypatch.setenv("SUPERTOOL_MCP_AUTOSPAWN", "0")
        monkeypatch.delenv("SUPERTOOL_REQUIRE_VALIDATORS", raising=False)
        mod = _adapter(name, prefix, project, monkeypatch)
        target = tmp_path / "x.php"
        target.write_text("<?php\n", encoding="utf-8")

        rc = _drive(mod, name, target)
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

        assert rc == 0
        assert spawns == [], (
            f"#1743: {name} launched a daemon with SUPERTOOL_MCP_AUTOSPAWN=0: "
            f"{spawns!r}"
        )
        assert "skipped" in payload, (
            f"{name}: a suppressed run looked at nothing, so it owes the third "
            f"state, not a verdict. got: {payload!r}"
        )
        assert "ok" not in payload and "errors" not in payload, (
            f"{name}: a skip omits the verdict keys (validators/SCHEMA.md "
            f"§'Skipped: the third state'). got: {payload!r}"
        )
        assert "SUPERTOOL_MCP_AUTOSPAWN" in payload["skipped"], (
            f"{name}: the receipt must say a spawn was suppressed and name the "
            f"knob that did it — a run that reports nothing about having been "
            f"told is the misreport this issue is filed under. "
            f"got: {payload['skipped']!r}"
        )

    def test_a_missing_binary_still_says_install_it(
        self, name, prefix, runtime, project, spawns, monkeypatch, capsys, tmp_path
    ):
        """The suppressed arm must not swallow the more actionable sentence.

        `_spawn` declines before its own `preflight`, so the binary lookup that
        produces the install hint does not happen there. On the machine `cwd:`
        usually points at — a git worktree where `composer install` never ran —
        the binary is absent AND the daemon is cold, and the useful sentence
        names the package, not the daemon. `docs/validators.md` §#531 documents
        that row, and without the adapter's own lookup this fix made it
        unreachable on the default path.

        It also pins the placement claim the rest of the file cannot: a decline
        that ran `preflight` first, or one placed after the reap, passes every
        `no-Popen` assertion here and fails this one.
        """
        monkeypatch.setenv("SUPERTOOL_MCP_AUTOSPAWN", "0")
        monkeypatch.delenv("SUPERTOOL_REQUIRE_VALIDATORS", raising=False)
        mod = _adapter(name, prefix, project, monkeypatch)
        # A bare name absent from $PATH, which is the shape `resolve_bin`
        # actually verifies: an *absolute* MCP_*_BIN is returned unchecked, so
        # a nonexistent absolute path would prove nothing about this arm.
        mod.DAEMON_PROC = "mcp-warm-definitely-not-installed-1743"
        target = tmp_path / "x.php"
        target.write_text("<?php", encoding="utf-8")

        _drive(mod, name, target)
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

        assert spawns == [], f"#1743: {name} spawned under suppression"
        assert "skipped" in payload, f"{name}: owes the third state: {payload!r}"
        assert "install via" in payload["skipped"], (
            f"{name}: the binary is absent, so the receipt owes the install "
            f"sentence rather than advice to warm a daemon that cannot boot. "
            f"got: {payload['skipped']!r}"
        )

    def test_a_required_validator_says_it_did_not_run(
        self, name, prefix, runtime, project, spawns, monkeypatch, capsys, tmp_path
    ):
        """A suppressed spawn means the gate did not run. Silent on a laptop,
        loud where the repo asked for the gate (#665/#1202) — the same
        one-directional escalation an absent binary gets, for the same reason:
        nobody reads a validator row that says `skipped` on every run.
        """
        monkeypatch.setenv("SUPERTOOL_MCP_AUTOSPAWN", "0")
        monkeypatch.setenv("SUPERTOOL_REQUIRE_VALIDATORS", name)
        mod = _adapter(name, prefix, project, monkeypatch)
        target = tmp_path / "x.php"
        target.write_text("<?php\n", encoding="utf-8")

        _drive(mod, name, target)
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

        assert spawns == [], f"#1743: {name} spawned under suppression"
        assert payload.get("ok") is False, (
            f"{name}: named in $SUPERTOOL_REQUIRE_VALIDATORS and it did not "
            f"check the file — that has to be loud. got: {payload!r}"
        )
        assert "SUPERTOOL_REQUIRE_VALIDATORS" in json.dumps(payload)
