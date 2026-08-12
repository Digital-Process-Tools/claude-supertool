"""#1541: the channel name is this workspace's state, not the shipped artifact's,
and the launcher must not silently drop its own prompt.

#1539 put `SUPERTOOL_WATCH_NAME=oss-supertool` into `.mcp.json`. That file is the
**plugin's**: its `args` use `${CLAUDE_PLUGIN_ROOT}` and `channel:health` reads it
at the plugin root. So the value reached every downstream install, binding their
consumer to this clone's socket while their pollers bound the default — the
half-configured state `presets/watch/README.md` calls worse than setting neither.

Measured 2026-08-12 against claude 2.1.219, because both halves of the fix rest
on the behaviour of a binary this repo does not own:

* **A stdio MCP server the harness spawns inherits the launching process's
  environment.** A probe server whose command dumps `env`, registered with
  `claude mcp add` and spawned by `claude mcp list`, saw a `SUPERTOOL_WATCH_NAME`
  exported only by the shell that ran the command. So the launcher can export the
  name and nothing needs to ship.
* **A second positional is accepted and then ignored.** `claude --session-id
  bogus a b` reaches the session-id validator rather than "too many arguments",
  and `claude --print --model bogus-model-xyz "" "probe-second"` answers "Input
  must be provided either through stdin or as a prompt argument" — the *first*
  positional is the prompt, later ones are dropped. Worse, `claude --add-dir
  /nope-1 hello /opensource-manager` consumed both trailing words into the
  variadic option and left no prompt at all.

So `exec claude ... "$@" "/opensource-manager"` delivered its prompt only when
`$@` was empty, and said nothing in every other case.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import supertool

REPO = Path(__file__).resolve().parents[1]
for _dir in (str(REPO / "presets" / "watch"), str(REPO / "presets"), str(REPO / "tests")):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import channel  # noqa: E402
import naming  # noqa: E402
from _changelog_findable import assert_change_is_findable  # noqa: E402

ROOT = Path(supertool.__file__).resolve().parent
LAUNCHER = ROOT / "bin" / "supertool-workspace"

NAME_ENV = "SUPERTOOL_WATCH_NAME"
PROMPT = "/opensource-manager"


def test_the_shipped_mcp_config_carries_no_workspace_local_channel() -> None:
    """`.mcp.json` is installed for every user, so nothing in it may be true of
    exactly one checkout.

    The producers' name lives in `.supertool.json`, which is this clone's own file
    and ships to nobody. A name in `.mcp.json` reconfigures every downstream
    consumer onto a socket their own pollers never bind. This one is a plain file
    read, so it runs on every platform.
    """
    doc = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    env = doc["mcpServers"]["claude-channel"].get("env", {})
    leaked = sorted(k for k in env if k.startswith("SUPERTOOL_WATCH"))
    assert not leaked, (
        f".mcp.json hardcodes {leaked} for every install of this plugin. The name "
        f"is workspace state: export it from bin/supertool-workspace, whose "
        f"environment the harness-spawned consumer inherits."
    )


# ---------------------------------------------------------------------------
# a throwaway clone, a stub `claude`, and the launcher run for real
# ---------------------------------------------------------------------------

posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason="bin/supertool-workspace is a POSIX /bin/sh script; Windows cannot run it",
)

_STUB = r"""#!/bin/sh
for a in "$@"; do printf '%s\n' "$a"; done > "$STUB_ARGV"
env > "$STUB_ENV"
"""


def _clone(tmp_path: Path, watch_name: str | None) -> Path:
    """A minimal project root carrying a copy of the real launcher."""
    root = tmp_path / "clone"
    (root / "bin").mkdir(parents=True)
    block: dict = {"radar_tiers": {}}
    ops: dict = {"radar": block}
    if watch_name:
        block["watch_name"] = watch_name
        ops["watches"] = {"watch_name": watch_name}
    (root / ".supertool.json").write_text(
        json.dumps({"ops": ops}), encoding="utf-8")
    copy = root / "bin" / "supertool-workspace"
    copy.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    copy.chmod(0o755)
    return root


def _stub_claude(tmp_path: Path) -> Path:
    """A `claude` on PATH that records its argv and environment, then exits 0."""
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    stub = bindir / "claude"
    stub.write_text(_STUB, encoding="utf-8")
    stub.chmod(0o755)
    return bindir


class Launch:
    def __init__(self, argv: list[str], env: dict, proc) -> None:
        self.argv = argv
        self.env = env
        self.stderr = proc.stderr
        self.returncode = proc.returncode


def _env_for(tmp_path: Path, bindir: Path) -> dict:
    env = dict(os.environ)
    env.pop(NAME_ENV, None)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    env["STUB_ARGV"] = str(tmp_path / "argv.txt")
    env["STUB_ENV"] = str(tmp_path / "env.txt")
    return env


def _launch(tmp_path: Path, *args: str, watch_name: str | None = "probe-name",
            exported: str | None = None) -> Launch:
    root = _clone(tmp_path, watch_name)
    env = _env_for(tmp_path, _stub_claude(tmp_path))
    if exported is not None:
        env[NAME_ENV] = exported

    proc = subprocess.run([str(root / "bin" / "supertool-workspace"), *args],
                          cwd=str(tmp_path), env=env, capture_output=True,
                          text=True, timeout=60, encoding="utf-8",
                          errors="replace")
    argv_file = tmp_path / "argv.txt"
    assert argv_file.exists(), (
        f"the stub claude never ran (rc={proc.returncode}): {proc.stderr}")
    argv = argv_file.read_text(encoding="utf-8").splitlines()
    seen: dict = {}
    for line in (tmp_path / "env.txt").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        seen[key] = value
    return Launch(argv, seen, proc)


# ---------------------------------------------------------------------------
# the name reaches the consumer without shipping
# ---------------------------------------------------------------------------

@posix_only
def test_the_launcher_exports_the_clones_channel_name(tmp_path: Path) -> None:
    """The one source of truth is that clone's `.supertool.json`.

    `claude` hands its own environment to the stdio servers it spawns, so an
    export here is what puts the consumer on the same socket as the pollers.
    """
    run = _launch(tmp_path, watch_name="probe-name")
    assert run.env.get(NAME_ENV) == "probe-name", (
        f"the launcher did not export {NAME_ENV}, so the consumer it starts binds "
        f"the default socket while this clone's pollers bind "
        f"/tmp/supertool-watch-probe-name.sock. Saw: {run.env.get(NAME_ENV)!r}")


@posix_only
def test_a_clone_that_names_no_channel_exports_nothing(tmp_path: Path) -> None:
    """The default channel is coherent at both ends; inventing a name is not."""
    run = _launch(tmp_path, watch_name=None)
    assert not run.env.get(NAME_ENV), run.env.get(NAME_ENV)


@posix_only
def test_an_operators_own_export_wins_and_is_said_out_loud(tmp_path: Path) -> None:
    """`naming.py`'s precedence: a value a running fleet already captured is not
    moved underneath it, and a name losing silently is what this preset files
    hardest against."""
    run = _launch(tmp_path, watch_name="probe-name", exported="already-set")
    assert run.env.get(NAME_ENV) == "already-set", run.env.get(NAME_ENV)
    assert "already-set" in run.stderr and "probe-name" in run.stderr, run.stderr


# ---------------------------------------------------------------------------
# the prompt: appended, or said to be missing — never silently dropped
# ---------------------------------------------------------------------------

@posix_only
def test_the_prompt_is_appended_when_there_is_nothing_else(tmp_path: Path) -> None:
    run = _launch(tmp_path)
    assert run.argv[-1] == PROMPT, run.argv


@posix_only
def test_the_flag_the_channel_needs_is_still_passed(tmp_path: Path) -> None:
    run = _launch(tmp_path)
    assert "--dangerously-load-development-channels" in run.argv, run.argv
    assert "server:claude-channel" in run.argv, run.argv


@posix_only
@pytest.mark.parametrize("args", [
    ("fix #1500",),
    ("--add-dir", "/tmp"),
    ("--model", "opus", "fix #1500"),
])
def test_the_prompt_is_never_dropped_in_silence(tmp_path: Path, args) -> None:
    """`claude` reads the FIRST positional as the prompt and ignores the rest, and
    a variadic option swallows whatever follows it. So a trailing
    `/opensource-manager` after pass-through arguments is not a prompt — it is a
    word `claude` throws away.

    Either the launcher places it where `claude` will read it, or it says on
    stderr that it did not. Appending it and saying nothing is the one answer
    that is wrong, and it is what #1539 shipped.
    """
    run = _launch(tmp_path, *args)
    if PROMPT in run.argv:
        assert run.argv.index(PROMPT) < run.argv.index(args[0]), (
            f"{PROMPT} sits after the pass-through arguments, where claude reads "
            f"it as an excess positional and drops it: {run.argv}")
    else:
        assert PROMPT in run.stderr, (
            f"the prompt was not passed to claude and stderr never said so: "
            f"{run.stderr!r}")


@posix_only
@pytest.mark.parametrize("args", [
    ("fix #1500",),
    ("--add-dir", "/tmp"),
])
def test_pass_through_arguments_still_reach_claude(tmp_path: Path, args) -> None:
    """The header's other promise. However the prompt is handled, the operator's
    own arguments are not rewritten."""
    run = _launch(tmp_path, *args)
    for arg in args:
        assert arg in run.argv, (arg, run.argv)


@posix_only
def test_the_refusal_still_fires_outside_a_project_root(tmp_path: Path) -> None:
    """The pre-existing guard, kept honest while the script grew a second job."""
    root = _clone(tmp_path, "probe-name")
    (root / ".supertool.json").unlink()
    env = _env_for(tmp_path, _stub_claude(tmp_path))
    proc = subprocess.run([str(root / "bin" / "supertool-workspace")],
                          cwd=str(tmp_path), env=env, capture_output=True,
                          text=True, timeout=60, encoding="utf-8",
                          errors="replace")
    assert proc.returncode != 0, proc.stdout
    assert not (tmp_path / "argv.txt").exists(), "claude was launched anyway"

# ---------------------------------------------------------------------------
# what `channel:health` may now claim about a consumer that declares nothing
# ---------------------------------------------------------------------------

def _mcp(tmp_path: Path, env: dict | None) -> Path:
    server: dict = {"command": "bun", "args": ["channel.ts"]}
    if env is not None:
        server["env"] = env
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"claude-channel": server}}), encoding="utf-8")
    return tmp_path


def test_a_consumer_declaring_no_channel_variable_is_not_claimed_to_disagree(
        tmp_path: Path) -> None:
    """The shipped `.mcp.json` names no channel, which used to read as "the
    consumer is on the default socket".

    That was true while `.mcp.json` was the only route to the consumer. It is not
    true now: the consumer inherits the environment of the session that spawned
    it, and this process cannot read that environment — its own copy carries a
    name supertool injected from `.supertool.json`, which says nothing about what
    the harness was launched with. So the answer is the third state, and the
    report may not say the pollers are shouting into nothing while
    `channel:health` prints FORWARDING two lines above.
    """
    root = _mcp(tmp_path, None)
    lines = channel.consumer_lines(naming.resolve({NAME_ENV: "oss"}), roots=[root])
    blob = chr(10).join(lines)
    assert lines, "silence here would read as agreement"
    assert "inherit" in blob, blob
    assert "is on another channel" not in blob, (
        "a claim about an environment this process never read: " + blob)


def test_an_explicit_declaration_is_still_compared(tmp_path: Path) -> None:
    """The inheritance arm must not swallow the check #1477 exists for: an
    `env` block that names a channel overrides what it inherits, so a wrong one
    is still a disagreement."""
    root = _mcp(tmp_path, {NAME_ENV: "other"})
    lines = channel.consumer_lines(naming.resolve({NAME_ENV: "oss"}), roots=[root])
    blob = chr(10).join(lines)
    assert naming.sock_for("other") in blob, lines
    assert naming.sock_for("oss") in blob, lines


def test_the_change_is_findable() -> None:
    assert_change_is_findable(1541)


def test_a_number_inside_a_longer_number_is_not_a_changelog_entry(
        tmp_path: Path) -> None:
    """This call site is why the guard was tightened, so it pins it.

    `assert_change_is_findable(1541)` passed before a single line of this branch
    existed: `1541` occurs inside `154177` in CHANGELOG.md, and the check was a
    bare `in`. A guard that greens on an unrelated pipeline id is one that reports
    an absence it never established.
    """
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "CHANGELOG.md").write_text(
        "- pipeline 154177 went red, see 21541999 too", encoding="utf-8")
    with pytest.raises(AssertionError):
        assert_change_is_findable(1541, root=tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "- the launcher exports the name (#1541)", encoding="utf-8")
    assert_change_is_findable(1541, root=tmp_path)
