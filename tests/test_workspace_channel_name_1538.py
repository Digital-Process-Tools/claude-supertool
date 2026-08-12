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
import re
import subprocess
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


def test_the_name_reaches_the_consumer_without_being_written_into_the_artifact() -> None:
    """The two ends still have to agree; the route changed in #1541.

    #1538 put the name in `.mcp.json` as well, and that was wrong in a way this
    test pinned in place: `.mcp.json` is the **plugin's** file — its `args` use
    `${CLAUDE_PLUGIN_ROOT}` and `channel:health` reads it at the plugin root — so
    this clone's private name was shipped to every install, binding their consumer
    to our socket while their pollers bound the default.

    The consumer is now told by the environment instead, which the harness passes
    to the stdio servers it spawns (measured, `tests/test_workspace_launcher_1541
    .py`). One source of truth, nothing shipped. The agreement property is pinned
    there; what remains here is the half of it that belongs to this module — the
    producers name exactly one channel, and the artifact names none.
    """
    declared = _declared_names()
    env = _load(".mcp.json")["mcpServers"]["claude-channel"].get("env", {})
    assert NAME_ENV not in env, (
        f".mcp.json ships {env.get(NAME_ENV)!r} to every install of this plugin. "
        f"The producers' name ({sorted(declared)}) belongs in .supertool.json and "
        f"reaches the consumer through bin/supertool-workspace's export (#1541)."
    )
    # A text read, deliberately, and the weaker half of the pair: the behavioural
    # pin runs the launcher against a stub `claude` and is POSIX-only, so on
    # Windows this is the only thing standing between a deleted export and a
    # green suite.
    launcher = LAUNCHER.read_text(encoding="utf-8")
    assert f"export {NAME_ENV}" in launcher and "watch_name" in launcher, (
        f"nothing carries {NAME_ENV} to the consumer any more: the launcher must "
        f"read it from .supertool.json and export it, or the two ends bind "
        f"different sockets and every emitted event is lost at the source."
    )


# ---------------------------------------------------------------------------
# the launcher
# ---------------------------------------------------------------------------

def test_the_launcher_exists_and_ships_executable() -> None:
    """The bit that matters is the one git records, not the one the checkout has.

    `os.stat().st_mode & S_IXUSR` was the first spelling and it reddened all four
    `windows-latest` legs of #1539 at `mode 0o100666`: NTFS has no execute bit, so
    that read was a fact about the runner's filesystem rendered as a verdict about
    the artifact — this repo's own defect class, relocated into the test harness
    (#1541).

    `git ls-files -s` answers the question actually being asked — what mode is
    committed, and therefore what a clone gets — in the same bytes on every
    platform. So there is no skip here: Windows can answer this, and a test that
    stepped aside there would be one nobody ever ran on the platform that broke.
    An unanswerable git is a failure rather than a pass, because "could not
    establish" must never render as `100755`.
    """
    assert LAUNCHER.is_file(), f"missing {LAUNCHER}"
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-s", "--", "bin/supertool-workspace"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace")
    except OSError as err:  # git absent: Windows raises FileNotFoundError here
        raise AssertionError(
            f"git could not be run ({type(err).__name__}), so the committed mode "
            f"of {LAUNCHER} is unknown. That is not the same as executable."
        ) from err
    assert proc.returncode == 0 and proc.stdout.strip(), (
        f"git ls-files gave nothing for bin/supertool-workspace (rc "
        f"{proc.returncode}): either it is untracked or git could not answer. "
        f"Either way its shipped mode is unestablished, not fine. {proc.stderr}")
    mode = proc.stdout.split()[0]
    assert mode == "100755", (
        f"bin/supertool-workspace is committed as {mode}, so every clone gets a "
        f"launcher it cannot run: `git update-index --chmod=+x "
        f"bin/supertool-workspace`")


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
