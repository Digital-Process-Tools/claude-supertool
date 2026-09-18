#!/usr/bin/env python3
"""oss_tick -- the mechanical opening of the `claude-oss` maintainer tick,
composed into one receipt whose every row is three-state (#1985).

Reads `.oss.json` from the current working directory, the same way that
file's other readers already do, so no fact about any particular repository
ever enters this codebase.

The reason to compose the seven checks below rather than leave them as
prose-ordered commands: a step that is *skipped* and a step that *found
nothing* currently render identically to the reader. This op always runs
every one of them and prints why, wherever it could not, so a missing check
is visible rather than silent.

This is deliberately the mechanical half only. The maintainer loop's
judgment -- the ranking table, the review classes, what ends a tick -- stays
in the `claude-oss` skill; moving it into this op's stdout would reprint it
on every call instead of loading it once per session.

Nothing here is a boundary. The raw-command guard hooks `Bash` only, a
harness `Edit`/`Write` bypasses every op, and the `Bash` grant handed to
agents is total -- this composes read calls (and one `git pull`) into one
round-trip; it enforces nothing.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shim  # noqa: E402

#: This preset's own `supertool.py`, three directories up from
#: `presets/oss/tick.py` -- the same core the call reached this op through,
#: never the bare `supertool` name on PATH, which can resolve to a different
#: checkout's core running against this tree's presets (#1409).
_SUPERTOOL_CORE = Path(__file__).resolve().parent.parent.parent / "supertool.py"

#: The board read this op replaces (issue #1985): `supertool 'gh-prs'
#: 'gh-issues' 'gh-branch' 'git-worktrees'`, one call for four ops -- kept
#: as separate subprocess calls here rather than one batched call so a
#: single op's failure never hides the other three's results.
_BOARD_OPS = ("gh-prs", "gh-issues", "gh-branch", "git-worktrees")


def _run(argv, run=None, timeout=60, cwd=None):
    """`(returncode, output)`, or `(None, reason)` when the process never ran."""
    run = subprocess.run if run is None else run
    try:
        done = run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return None, "timed out after {}s".format(timeout)
    except OSError as exc:
        return None, str(exc.strerror or exc.__class__.__name__)
    output = (
        done.stdout.decode("utf-8", "replace")
        if isinstance(done.stdout, bytes)
        else (done.stdout or "")
    )
    return done.returncode, output


def state_file(cwd):
    """The absolute path oss_state.py's own positional `path` argument
    expects, resolved from `.oss.local.json` beside `.oss.json` (#1985).

    `state_file` there is machine-local and git-excluded -- a worktree cut
    from a clone that has one does not automatically carry a copy -- so a
    missing or unreadable `.oss.local.json` is a real "could not evaluate"
    here, never a crash.

    Returns `(path, None)` on success, `(None, reason)` otherwise.
    """
    local_cfg = Path(cwd) / ".oss.local.json"
    try:
        doc = json.loads(local_cfg.read_text(encoding="utf-8"))
    except OSError:
        return None, "{} not found".format(local_cfg)
    except ValueError as exc:
        return None, "{} is not valid JSON ({})".format(local_cfg, exc)
    rel = doc.get("state_file") if isinstance(doc, dict) else None
    if not rel:
        return None, "{} carries no state_file".format(local_cfg)
    base = Path(doc["clone"]) if doc.get("clone") else Path(cwd)
    return str((base / rel).resolve()), None


def _oss_state(scripts_dir, state_path, args, run=None):
    if scripts_dir is None:
        return None, "plugin not resolved"
    if state_path is None:
        return None, "state file could not be resolved"
    script = scripts_dir / "oss_state.py"
    if not script.is_file():
        return None, "oss_state.py not found at the resolved install path"
    return _run(
        [sys.executable, str(script)] + list(args) + [state_path], run=run
    )


def compose(cwd=None, record=None, cache_root=None, run=None, resolve_fn=None):
    """Every row, always attempted, never silently skipped. Returns a dict."""
    cwd = cwd or os.getcwd()
    resolve_fn = resolve_fn or shim.resolve
    rows = {}

    state, detail = resolve_fn(record=record, cache_root=cache_root)
    scripts_dir = None
    if state == "resolved":
        version, scripts_dir = detail
        rows["plugin_identity"] = "resolved {}".format(version)
    elif state == "resolved-but-different":
        rows["plugin_identity"] = (
            "resolved, but the tree here declares a different version -- {}".format(
                detail
            )
        )
    else:
        rows["plugin_identity"] = "could-not-resolve -- {}".format(detail)

    state_path, state_reason = state_file(cwd)

    code, out = _oss_state(scripts_dir, state_path, ["--last"], run=run)
    if code is None:
        rows["last_state_entry"] = "FAIL -- {}".format(out or state_reason)
    elif code != 0:
        rows["last_state_entry"] = "FAIL -- exit {}: {}".format(code, out.strip())
    else:
        rows["last_state_entry"] = out.strip() or "no entries yet"

    code, out = _oss_state(scripts_dir, state_path, ["--pending-wait"], run=run)
    if code is None or code != 0:
        rows["pending_wait"] = "could-not-evaluate -- {}".format(
            (out or state_reason) if code is None
            else "exit {}: {}".format(code, out.strip())
        )
    else:
        rows["pending_wait"] = out.strip() or "could-not-evaluate"

    if scripts_dir is not None:
        identity = "oss {}".format(version)
        code, out = _oss_state(
            scripts_dir, state_path, ["--check-plugin-identity", identity], run=run
        )
    else:
        code, out = None, "plugin not resolved"
    if code is None or code != 0:
        rows["plugin_identity_check"] = "could-not-tell -- {}".format(
            out if code is None else "exit {}: {}".format(code, out.strip())
        )
    else:
        rows["plugin_identity_check"] = out.strip() or "could-not-tell"

    code, out = _run(["git", "fetch"], run=run, cwd=cwd)
    if code != 0:
        rows["git_sync"] = "could-not-run -- fetch: {}".format(
            out.strip() if out else "exit {}".format(code)
        )
    else:
        code2, out2 = _run(["git", "pull", "--ff-only"], run=run, cwd=cwd)
        if code2 != 0:
            rows["git_sync"] = "could-not-run -- pull --ff-only: {}".format(
                out2.strip() if out2 else "exit {}".format(code2)
            )
        else:
            rows["git_sync"] = out2.strip() or "up to date"

    board = {}
    core_present = _SUPERTOOL_CORE.is_file()
    for op in _BOARD_OPS:
        if not core_present:
            board[op] = "unread -- {} not found".format(_SUPERTOOL_CORE)
            continue
        code, out = _run(
            [sys.executable, str(_SUPERTOOL_CORE), op], run=run, cwd=cwd, timeout=120
        )
        board[op] = "read" if code == 0 else "unread -- {}".format(
            out.strip() if out else "exit {}".format(code)
        )
    rows["board"] = board

    if not core_present:
        rows["radar_tier"] = "probe-did-not-answer -- {} not found".format(
            _SUPERTOOL_CORE
        )
    else:
        code, out = _run(
            [sys.executable, str(_SUPERTOOL_CORE), "radar:--state"],
            run=run,
            cwd=cwd,
            timeout=60,
        )
        if code is None:
            rows["radar_tier"] = "probe-did-not-answer -- {}".format(out)
        elif code != 0 and "not configured" in (out or "").lower():
            rows["radar_tier"] = "not-configured"
        elif code == 0:
            rows["radar_tier"] = "registered"
        else:
            rows["radar_tier"] = "probe-did-not-answer -- exit {}: {}".format(
                code, (out or "").strip()
            )

    rows["next"] = _next_step(rows)
    return rows


def _next_step(rows):
    if rows["plugin_identity"].startswith("could-not-resolve"):
        return "resolve the oss plugin install before proceeding -- {}".format(
            rows["plugin_identity"]
        )
    if rows["pending_wait"].startswith("holds"):
        return "pending wait holds -- do not dispatch yet"
    if rows["git_sync"].startswith("could-not-run"):
        return "resolve the git sync failure before proceeding -- {}".format(
            rows["git_sync"]
        )
    unread = [op for op, v in rows["board"].items() if v.startswith("unread")]
    if unread:
        return "re-read the board before dispatching -- unread: {}".format(
            ", ".join(unread)
        )
    return "proceed to dispatch"


def render(rows):
    lines = ["--- oss_tick ---"]
    lines.append("plugin identity: {}".format(rows["plugin_identity"]))
    lines.append("last state entry: {}".format(rows["last_state_entry"]))
    lines.append("pending wait: {}".format(rows["pending_wait"]))
    lines.append(
        "plugin identity vs last recorded: {}".format(rows["plugin_identity_check"])
    )
    lines.append("git fetch && pull --ff-only: {}".format(rows["git_sync"]))
    for op in _BOARD_OPS:
        lines.append("board {}: {}".format(op, rows["board"].get(op, "unread")))
    lines.append("radar tier: {}".format(rows["radar_tier"]))
    lines.append("NEXT: {}".format(rows["next"]))
    return "\n".join(lines)


def main(argv=None):
    rows = compose()
    print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
