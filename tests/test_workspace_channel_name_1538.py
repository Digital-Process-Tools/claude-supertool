"""#1538: this repo's two channel configs must name the same channel, and the
workspace launcher must be the one recipe for opening it.

`6047d98` set `watch_name` in `.supertool.json` and left `.mcp.json` untold, so
the producers bound `/tmp/supertool-watch-oss-supertool.sock` while the consumer
bound `/tmp/supertool-watch.sock`. `channel:health` reported it correctly — the
naming layer works; what was missing is anything holding the two files together.

A one-file change to a two-file contract is invisible until somebody runs the
op, and by then the board reads armed and delivers nothing.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import supertool

ROOT = Path(supertool.__file__).resolve().parent
LAUNCHER = ROOT / "bin" / "supertool-workspace"

NAME_ENV = "SUPERTOOL_WATCH_NAME"


def _load(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def _declared_names() -> set[str]:
    """Every `watch_name` this repo declares for itself, from `.supertool.json`."""
    blob = (ROOT / ".supertool.json").read_text(encoding="utf-8")
    return set(re.findall(r'"watch_name"\s*:\s*"([^"]+)"', blob))


# ---------------------------------------------------------------------------
# the two configs
# ---------------------------------------------------------------------------

def test_this_repo_declares_exactly_one_watch_name() -> None:
    """Three op blocks carry it; they have to agree or the ops disagree."""
    names = _declared_names()
    assert names, (
        ".supertool.json declares no watch_name at all. If the named channel was "
        "withdrawn on purpose, delete this module with it — an empty match must "
        "never read as agreement."
    )
    assert len(names) == 1, f"op blocks disagree about the channel name: {sorted(names)}"


def test_the_consumer_is_told_the_same_name_as_the_producers() -> None:
    """The defect: `.supertool.json` reaches no consumer, ever.

    Nothing in `.supertool.json` is read by `channel.ts` — it is spawned by the
    harness from `.mcp.json`. So the name has to be in both files or the two
    ends bind different sockets and every emitted event is lost at the source.
    """
    declared = _declared_names()
    env = _load(".mcp.json")["mcpServers"]["claude-channel"].get("env", {})
    assert NAME_ENV in env, (
        f".mcp.json's claude-channel block declares no {NAME_ENV}, so the consumer "
        f"binds the default socket while this repo's producers bind "
        f"{sorted(declared)}'s. `channel:health` reports this as NOT DELIVERING."
    )
    assert env[NAME_ENV] in declared, (
        f".mcp.json says {env[NAME_ENV]!r}, .supertool.json says {sorted(declared)}"
    )


# ---------------------------------------------------------------------------
# the launcher
# ---------------------------------------------------------------------------

def test_the_launcher_exists_and_is_executable() -> None:
    assert LAUNCHER.is_file(), f"missing {LAUNCHER}"
    mode = LAUNCHER.stat().st_mode
    assert mode & stat.S_IXUSR, f"{LAUNCHER} is not executable (mode {oct(mode)})"


def _code_lines() -> str:
    """The launcher without its comments.

    Both assertions below read the file as text, and the file documents itself:
    the header says "never from $HOME" and the word `claude` appears inside
    `claude-channel` long before anything runs. Grepping the whole file failed
    on its own prose — a test asserting about documentation while claiming to
    assert about code. Seen red twice for that reason, once per assertion.
    """
    body = LAUNCHER.read_text(encoding="utf-8")
    kept = [ln for ln in body.splitlines() if not ln.lstrip().startswith("#")]
    return chr(10).join(kept)


def test_the_launcher_resolves_its_root_from_itself_not_from_home() -> None:
    """A hardcoded `~/Documents/claude-supertool` works on exactly one machine.

    `supertool` reaches PATH as a symlink into a clone; the launcher is reached
    the same way, so it must resolve the clone from its own location.
    """
    code = _code_lines()
    assert "$HOME" not in code and "~/Documents" not in code, (
        "the launcher hardcodes a home-relative clone path; resolve the root "
        "from the script's own location so any clone works"
    )
    assert "dirname" in code, "no self-location step found in the launcher"


def test_the_launcher_names_the_flag_the_channel_actually_needs() -> None:
    """Without it the pollers still spawn and nothing reads them."""
    body = LAUNCHER.read_text(encoding="utf-8")
    assert "--dangerously-load-development-channels" in body
    assert "server:claude-channel" in body


def test_the_launcher_changes_directory_before_launching() -> None:
    """`radar` reads its tiers from the CWD's project root, so the cd IS the
    board selection. A launcher that starts claude from wherever you stood
    opens a session over some other repo's radar, or none."""
    code = _code_lines()
    cd_at = code.find("\ncd ")
    exec_at = code.find("exec claude")
    assert cd_at != -1, "the launcher never changes directory"
    assert exec_at != -1, "the launcher never execs claude"
    assert cd_at < exec_at, "the launcher starts claude before it cds"
