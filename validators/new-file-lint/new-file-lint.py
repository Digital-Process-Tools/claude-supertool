#!/usr/bin/env python3







































from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from refusal import absent, guard_main, skipped, tool_fault  
from spawnable import argv0, spawnable  

TOOL = "new-file-lint"

INSTALL_HINT = "ruff not found on PATH — pip install ruff"





ENV_LINT_SCRIPT = "SUPERTOOL_NEW_FILE_LINT_SCRIPT"








LINT_SCRIPT_LOCATIONS = (
    os.path.join(".github", "scripts", "lint_new_files.py"),
)




TIMEOUT_S = 30

RC_CLEAN = 0
RC_FINDINGS = 1


def emit(d: dict) -> None:
    print(json.dumps(d))


def _adapter_error(file: str, msg: str, dur_ms: int) -> None:
    emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
          "errors": [{"line": None, "col": None, "severity": "error",
                      "code": "adapter", "msg": msg}],
          "duration_ms": dur_ms})


def _locations() -> tuple:





    override = os.environ.get("SUPERTOOL_NEW_FILE_LINT_SCRIPT", "").strip()
    return (override,) if override else LINT_SCRIPT_LOCATIONS















CONFIG_DIR_ENV = "SUPERTOOL_CONFIG_DIR"


def _config_dir() -> "tuple[Path | None, bool, str]":

















    if "SUPERTOOL_CONFIG_DIR" not in os.environ:
        return None, False, ""
    raw = os.environ["SUPERTOOL_CONFIG_DIR"].strip()
    if not raw:
        return None, True, "{0} was set but empty".format(CONFIG_DIR_ENV)
    try:
        return Path(raw).resolve(), True, ""
    except (OSError, ValueError) as exc:



        return None, True, "{0}={1!r} could not be resolved: {2}".format(
            CONFIG_DIR_ENV, raw, exc)


def _config_dir_is_untrusted_ancestor(root: Path, config_dir: Path) -> bool:





















    root_s = os.path.normcase(str(root))
    config_s = os.path.normcase(str(config_dir))
    if config_s == root_s:
        return False
    try:
        common = os.path.commonpath([root_s, config_s])
    except ValueError:  
        return False
    return common == config_s


def _config_dir_may_authorize_execution(root: Path, config_dir: Path) -> bool:































    root_s = os.path.normcase(str(root))
    config_s = os.path.normcase(str(config_dir))
    if config_s == root_s:
        return True
    try:
        common = os.path.commonpath([root_s, config_s])
    except ValueError:  
        return False
    return common == root_s


def _repo_root(start: Path) -> "tuple[Path | None, str | None]":










    try:
        r = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=TIMEOUT_S,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return None, "git binary not found"
    except subprocess.TimeoutExpired:
        return None, "git rev-parse timed out"
    except OSError as exc:
        return None, "git could not be run: {0}".format(exc)
    if r.returncode != 0:
        return None, "not inside a git repository"
    top = r.stdout.strip()
    return (Path(top).resolve(), None) if top else (None, "git reported no toplevel")


def _find_lint_script(target: Path, root: Path) -> "Path | None":








    resolved_start = target.parent.resolve()
    for parent in [resolved_start, *resolved_start.parents]:
        for relative in _locations():
            candidate = parent / relative
            if candidate.is_file():
                return candidate
        if parent == root:
            break
    return None


def _load(script: Path):
    spec = importlib.util.spec_from_file_location("_st_lint_new_files", script)
    if spec is None or spec.loader is None:
        raise ImportError("no import spec for {0}".format(script))
    module = importlib.util.module_from_spec(spec)
    sys.modules["_st_lint_new_files"] = module
    spec.loader.exec_module(module)
    return module


def _is_new_at_head(realpath: str, root: Path) -> "tuple[bool | None, str]":



















    try:
        rel = os.path.relpath(realpath, str(root)).replace(os.sep, "/")
    except ValueError as exc:
        return None, "could not relate {0!r} to repo root {1!r}: {2}".format(
            realpath, root, exc)
    try:
        r = subprocess.run(["git", "-C", str(root), "cat-file", "-e",
                            "HEAD:" + rel], capture_output=True, text=True,
                           timeout=TIMEOUT_S, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "git could not be spawned to probe HEAD: {0}".format(exc)
    if r.returncode == 0:
        return False, ""



    return True, ""


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        _adapter_error("", "no file arg", 0)
        return

    file = sys.argv[1]
    start = time.time()

    if not spawnable("ruff"):
        emit(absent(TOOL, file, INSTALL_HINT, int((time.time() - start) * 1000)))
        return

    path = Path(file)
    root, could_not_look = _repo_root(path.parent)
    if root is None:
        emit(skipped(TOOL, file,
                     "could not determine the git repo root above {0}, so no "
                     "location was tried and no history check was possible: "
                     "{1}. That is a claim about this run, not about the "
                     "project.".format(path.parent, could_not_look),
                     int((time.time() - start) * 1000)))
        return



    override_set = bool(os.environ.get("SUPERTOOL_NEW_FILE_LINT_SCRIPT", "").strip())
    if not override_set:
        config_dir, scope_known, scope_reason = _config_dir()





        if scope_known and config_dir is None:
            emit(skipped(TOOL, file,
                         "a new-file-lint script may exist at the default "
                         "location(s) inside {0}, but this run's "
                         "SUPERTOOL_CONFIG_DIR could not be used ({1}), so "
                         "the convention-based location cannot be checked "
                         "against a project boundary (#2228). Set {2} to an "
                         "exact path if this project's script is meant to "
                         "be trusted here.".format(
                             root, scope_reason, ENV_LINT_SCRIPT),
                         int((time.time() - start) * 1000)))
            return
        if (scope_known and config_dir is not None
                and _config_dir_is_untrusted_ancestor(root, config_dir)):
            emit(skipped(TOOL, file,
                         "a new-file-lint script may exist at the default "
                         "location(s) inside {0}, but the .supertool.json "
                         "that wired this run lives ABOVE that project, at "
                         "{1} -- the convention-based location is not "
                         "trusted across that boundary (#2228), because a "
                         "checkout is not made trustworthy just by sitting "
                         "under a directory that also holds this config. "
                         "Set {2} to an exact path if this project's script "
                         "is meant to be trusted here.".format(
                             root, config_dir, ENV_LINT_SCRIPT),
                         int((time.time() - start) * 1000)))
            return











        if (scope_known and config_dir is not None
                and not _config_dir_may_authorize_execution(root, config_dir)):
            emit(skipped(TOOL, file,
                         "a new-file-lint script may exist at the default "
                         "location(s) inside {0}, but the .supertool.json "
                         "that wired this run lives at {1}, which shares no "
                         "ownership relationship with that project -- "
                         "importing and running a script THAT project "
                         "supplies is not authorized just because supertool "
                         "was pointed at one of its files (#2236). Set {2} "
                         "to an exact path if this project's script is "
                         "meant to be trusted here.".format(
                             root, config_dir, ENV_LINT_SCRIPT),
                         int((time.time() - start) * 1000)))
            return

    script = _find_lint_script(path, root)
    if script is None:
        tried = ", ".join(_locations())
        emit(skipped(TOOL, file,
                     "no new-file-lint script found at or above {0} -- tried "
                     "{1}. That is a claim about where this adapter looked, "
                     "not about the project: set {2} to point at the real "
                     "script if it lives somewhere else, or this project "
                     "has not adopted this convention at all.".format(
                         path.parent, tried, ENV_LINT_SCRIPT),
                     int((time.time() - start) * 1000)))
        return

    try:
        found = _load(script)
    except Exception as exc:  
        _adapter_error(file, "{0} could not be imported, so the file was "
                             "NOT checked: {1}: {2}".format(
                                 script, type(exc).__name__, exc),
                       int((time.time() - start) * 1000))
        return

    extra_rules = getattr(found, "EXTRA_RULES", None)
    if not extra_rules:
        emit(skipped(TOOL, file,
                     "{0} does not define EXTRA_RULES, so this adapter does "
                     "not know which rules a new file should be checked "
                     "against".format(script),
                     int((time.time() - start) * 1000)))
        return




    is_new, reason = _is_new_at_head(os.path.realpath(file), root)
    if is_new is None:
        emit(skipped(TOOL, file,
                     "could not determine whether this path has history at "
                     "HEAD, so whether the tree-wide ignore applies to it is "
                     "unknown: " + reason,
                     int((time.time() - start) * 1000)))
        return
    if not is_new:
        emit(skipped(TOOL, file,
                     "this path already has a commit at HEAD -- the "
                     "project's tree-wide ignore covers it locally the same "
                     "as the shared `ruff` validator; a new finding "
                     "introduced by THIS edit is CI's own added-path leg's "
                     "job, via its own merge-base diff, which this "
                     "validator has no PR base ref to reproduce",
                     int((time.time() - start) * 1000)))
        return

    cmd = [argv0("ruff"), "check", "--output-format", "json", "--no-cache",
           "--force-exclude", "--quiet", "--extend-select",
           ",".join(extra_rules), "--", file]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=TIMEOUT_S, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        emit(absent(TOOL, file, "ruff on PATH but could not be executed",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        _adapter_error(file, "timeout — ruff did not return within "
                             "{0}s; the file was NOT checked".format(TIMEOUT_S),
                       int((time.time() - start) * 1000))
        return

    dur = int((time.time() - start) * 1000)
    body = (r.stdout or "").strip()
    if r.returncode not in (RC_CLEAN, RC_FINDINGS) and not body:
        _adapter_error(file, tool_fault("ruff check", r.returncode,
                                        r.stderr or r.stdout or ""), dur)
        return

    try:
        items = json.loads(body) if body else []
    except ValueError:
        _adapter_error(file, tool_fault("ruff check", r.returncode,
                                        r.stdout or r.stderr or ""), dur)
        return

    if not isinstance(items, list):
        _adapter_error(file, tool_fault("ruff check", r.returncode,
                                        "expected a JSON array, got "
                                        "{0}".format(type(items).__name__)), dur)
        return

    errors = []
    for item in items:
        if not isinstance(item, dict):
            continue
        location = item.get("location") or {}
        errors.append({
            "line": location.get("row"), "col": location.get("column"),
            "severity": "warning", "code": item.get("code"),
            "msg": (item.get("message") or "").strip().replace("\n", " ")[:300],
        })

    emit({"tool": TOOL, "file": file, "ok": not errors, "count": len(errors),
          "errors": errors, "duration_ms": int((time.time() - start) * 1000)})


if __name__ == "__main__":
    guard_main(TOOL, main)
