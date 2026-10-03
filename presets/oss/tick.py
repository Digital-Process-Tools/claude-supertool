#!/usr/bin/env python3

























import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import shim  
import _untrusted  










_SUPERTOOL_CORE = Path(__file__).resolve().parent.parent.parent / "supertool.py"





_BOARD_OPS = ("gh-prs", "gh-issues", "gh-branch", "git-worktrees")


def _run(argv, run=None, timeout=60, cwd=None):

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







    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass  

    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode(sys.stdout.encoding or "utf-8", "replace")
              .decode(sys.stdout.encoding or "utf-8", "replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
