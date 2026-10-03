#!/usr/bin/env python3
"""oss_tick -- the mechanical opening of the `claude-oss` maintainer tick,
composed into one receipt whose every row is three-state (#1985).

Runs from the current working directory: the git-excluded `.oss.local.json`
beside `.oss.json` there names the state file the three oss_state.py rows
read (never `.oss.json` itself directly -- this shim's own logic needs
nothing from it, though the plugin it execs into may). No fact about any
particular repository enters this codebase either way.

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
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import shim  # noqa: E402
import _untrusted  # noqa: E402  (a wait's dispatch/observable/why and a
                    # plugin identity's current/prior all come from state-file
                    # entries a lane or a maintainer typed, some of it a
                    # copy-pasted PR title -- flattened before it reaches a
                    # one-line render, same convention as every other preset
                    # (found by review, #2639's self-review round))

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
        text = local_cfg.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "{} not found".format(local_cfg)
    except OSError as exc:
        return None, "{} could not be read -- {}".format(
            local_cfg, exc.strerror or exc.__class__.__name__
        )
    try:
        doc = json.loads(text)
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


def _parse_pending_wait(out):
    """`oss_state.py --pending-wait`'s real stdout, not the prose the shim used
    to assume (#2639): a bare `holds`/`cleared` never leaves that script. It
    prints the literal string ``no pending wait`` for both "nothing was ever
    recorded" and "the most recent wait was cleared" -- those two collapse to
    the same bytes at the source, so this shim cannot and does not try to tell
    them apart -- or a JSON object (``json.dumps(record, indent=2)``) whose
    own ``state`` key is ``holds`` or ``could-not-evaluate`` for anything still
    outstanding.

    Returns ``(state, record)`` where ``state`` is ``"cleared"``, ``"holds"``,
    ``"could-not-evaluate"`` or ``"unrecognised"`` (a shape neither of the two
    known ones -- the script's own output changed under this shim, or the
    process printed something else entirely) with ``record`` set to the raw
    text/value in that last case, never a crash.
    """
    text = (out or "").strip()
    if not text or text == "no pending wait":
        return "cleared", None
    try:
        record = json.loads(text)
    except ValueError:
        return "unrecognised", text
    if not isinstance(record, dict) or record.get("state") not in (
        "holds", "could-not-evaluate",
    ):
        return "unrecognised", record
    return record["state"], record


def _parse_plugin_identity_check(out):
    """`oss_state.py --check-plugin-identity`'s real stdout (#2639's sibling,
    same root cause): `_run` merges stderr into stdout, and the script writes
    its one-line receipt to stderr *before* the JSON record on stdout -- so the
    combined text is that receipt line, then the JSON. This finds the JSON by
    its first `{` rather than assuming the output is bare JSON with nothing
    ahead of it.

    Returns the parsed record dict, or ``None`` when no valid JSON object
    could be found in the output at all.
    """
    text = (out or "").strip()
    brace = text.find("{")
    if brace == -1:
        return None
    try:
        record = json.loads(text[brace:])
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


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
        # `shim.resolve`'s own docstring: this state means an active version
        # was found and a root resolved for it, but that root carries no
        # scripts/ directory -- a broken/partial install, never a version
        # comparison (there is no "declared version" concept in shim.py at
        # all; that is the separate plugin_identity_check row below).
        rows["plugin_identity"] = (
            "resolved, but its install carries no scripts/ directory -- "
            "{}".format(detail)
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
        wait_state, wait_record = _parse_pending_wait(out)
        if wait_state == "cleared":
            rows["pending_wait"] = "cleared"
        elif wait_state == "holds":
            detail = (
                wait_record.get("dispatch") or wait_record.get("observable")
                or "no detail given"
            )
            rows["pending_wait"] = "holds -- {}".format(_untrusted.flat(str(detail)))
        elif wait_state == "could-not-evaluate":
            why = wait_record.get("why") or "no reason recorded"
            rows["pending_wait"] = "could-not-evaluate -- {}".format(
                _untrusted.flat(str(why))
            )
        else:
            # A shape neither `_parse_pending_wait` nor this shim recognises --
            # never rendered as a plain "could-not-evaluate", which `_next_step`
            # treats as non-blocking (found by review, #2639's own self-review
            # round): that would let dispatch proceed on the exact kind of
            # ambiguous answer #2639 was filed over, one level down from the
            # bug this diff fixes. "unresolved" is its own word so `_next_step`
            # can gate on it distinctly from a script-confirmed could-not-
            # evaluate measurement, which this shim still treats as advisory.
            rows["pending_wait"] = (
                "unresolved -- unrecognised --pending-wait output: "
                "{!r}".format(wait_record)
            )

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
        identity_record = _parse_plugin_identity_check(out)
        identity_state = identity_record.get("state") if identity_record else None
        if identity_state == "unchanged":
            rows["plugin_identity_check"] = "unchanged ({})".format(
                _untrusted.flat(str(identity_record.get("current")))
            )
        elif identity_state == "changed":
            rows["plugin_identity_check"] = "changed -- was {}, now {}".format(
                _untrusted.flat(str(identity_record.get("prior"))),
                _untrusted.flat(str(identity_record.get("current"))),
            )
        elif identity_state == "could-not-tell":
            why = identity_record.get("why") or "no reason recorded"
            rows["plugin_identity_check"] = "could-not-tell -- {}".format(
                _untrusted.flat(str(why))
            )
        elif identity_state == "route-mismatch":
            why = identity_record.get("why") or "no reason recorded"
            rows["plugin_identity_check"] = "route-mismatch -- {}".format(
                _untrusted.flat(str(why))
            )
        else:
            rows["plugin_identity_check"] = (
                "could-not-tell -- unrecognised --check-plugin-identity "
                "output: {!r}".format(out.strip())
            )

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
        elif code != 0 and "no tiers configured" in (out or "").lower():
            # `radar`'s own refusal string (presets/watch/radar.py's NO_TIERS)
            # is "no tiers configured", not "not configured" -- matched on
            # the wrong substring here once, and it is the majority-case
            # outcome for any repo with no ops.radar.radar_tiers set (#1985
            # self-review).
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
    if rows["pending_wait"].startswith("unresolved"):
        # An unrecognised --pending-wait shape (#2639's own self-review round)
        # must not fail open the way a plain "could-not-evaluate" measurement
        # does -- a parse failure carries no information about whether a real
        # hold is sitting behind it, so it is refused the same as a hold
        # rather than treated as advisory.
        return "pending wait could not be parsed -- do not dispatch yet"
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
    text = render(rows)
    # Every row here can carry subprocess output verbatim (a GitHub issue
    # title, an oss_state.py state entry), and stdout's encoding is the
    # CONSOLE's codepage, not this file's -- typically cp1252 on Windows,
    # where a non-ASCII byte raises UnicodeEncodeError and kills the
    # process at this print, after git fetch/pull and every other row have
    # already run (#1985 self-review). `errors="replace"` never fails; the
    # worst case is a `?` in place of a glyph the console cannot show.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass  # a stream with no reconfigure (or already detached): fall
              # through and let the encode below carry the same fallback
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode(sys.stdout.encoding or "utf-8", "replace")
              .decode(sys.stdout.encoding or "utf-8", "replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
