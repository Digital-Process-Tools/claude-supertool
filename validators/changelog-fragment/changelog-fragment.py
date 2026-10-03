#!/usr/bin/env python3































from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from refusal import guard_main, required, required_but_absent, skipped  
from source_context import context_fields  

TOOL = "changelog-fragment"



ENV_ASSEMBLER = "SUPERTOOL_CHANGELOG_ASSEMBLER"










ASSEMBLER_LOCATIONS = (
    os.path.join(".github", "scripts", "assemble_changelog.py"),
    os.path.join(".oss", "assemble_changelog.py"),
    os.path.join("scripts", "assemble_changelog.py"),
)


def emit(payload: dict) -> None:
    print(json.dumps(payload))


def _ms(start: float) -> int:
    return int((time.time() - start) * 1000)


def _locations() -> tuple:








    override = os.environ.get(ENV_ASSEMBLER, "").strip()
    return (override,) if override else ASSEMBLER_LOCATIONS













CONFIG_DIR_ENV = "SUPERTOOL_CONFIG_DIR"


def _config_dir() -> tuple[Path | None, bool, str]:






    if CONFIG_DIR_ENV not in os.environ:
        return None, False, ""
    raw = os.environ[CONFIG_DIR_ENV].strip()
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


def _repo_root(start: Path) -> tuple[Path | None, str | None]:


















    try:
        r = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
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







SCOPE_REFUSED_PREFIX = "SCOPE-REFUSED: "


def _find_assembler(target: Path) -> tuple[Path | None, Path | None, str | None]:
































    root, reason = _repo_root(target.parent)
    if root is None:
        return None, None, reason
    override_set = bool(os.environ.get(ENV_ASSEMBLER, "").strip())
    if not override_set:
        config_dir, scope_known, scope_reason = _config_dir()
        if scope_known and config_dir is None:
            return None, root, SCOPE_REFUSED_PREFIX + scope_reason
        if (scope_known and config_dir is not None
                and _config_dir_is_untrusted_ancestor(root, config_dir)):
            return None, root, SCOPE_REFUSED_PREFIX + (
                "the .supertool.json that wired this run lives ABOVE this "
                "project, at {0}".format(config_dir))








        if (scope_known and config_dir is not None
                and not _config_dir_may_authorize_execution(root, config_dir)):
            return None, root, SCOPE_REFUSED_PREFIX + (
                "the .supertool.json that wired this run lives at {0}, "
                "which shares no ownership relationship with this project "
                "-- importing and running a script THIS project supplies "
                "is not authorized just because supertool was pointed at "
                "one of its files (#2236)".format(config_dir))
    resolved_start = target.parent.resolve()
    for parent in [resolved_start, *resolved_start.parents]:
        for relative in _locations():
            candidate = parent / relative
            if candidate.is_file():
                return candidate, root, None
        if parent == root:
            break
    return None, root, None


def _load(script: Path):
    spec = importlib.util.spec_from_file_location("_st_assemble_changelog", script)
    if spec is None or spec.loader is None:
        raise ImportError("no import spec for {0}".format(script))
    module = importlib.util.module_from_spec(spec)
    sys.modules["_st_assemble_changelog"] = module
    spec.loader.exec_module(module)
    return module


def _line_of(name: str, finding: str) -> int | None:






    match = re.match(r"^{0}:([0-9]+): ".format(re.escape(name)), finding)
    return int(match.group(1)) if match else None


def _error(target: str, name: str, message: str, code: str) -> dict:
    line = _line_of(name, message)
    err = {"line": line, "col": None, "severity": "error",
           "code": code, "msg": message}
    err.update(context_fields(target, line))
    return err


def _verdict(target: str, errors: list, start: float) -> dict:
    return {"tool": TOOL, "file": target, "ok": not errors, "count": len(errors),
            "errors": errors, "duration_ms": _ms(start)}


def main() -> None:
    start = time.time()
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({"tool": TOOL, "file": "", "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter", "msg": "no file arg"}],
              "duration_ms": _ms(start)})
        return

    target = sys.argv[1]
    path = Path(target)
    name = path.name

    script, root, could_not_look = _find_assembler(path)
    if script is None:
        if could_not_look is not None and could_not_look.startswith(SCOPE_REFUSED_PREFIX):
            detail = could_not_look[len(SCOPE_REFUSED_PREFIX):]
            emit(skipped(TOOL, target,
                         "an assembler may exist at the default location(s) "
                         "inside {0}, but {1} -- the convention-based "
                         "location is not trusted across that boundary "
                         "(#2228), because a checkout is not made "
                         "trustworthy just by sitting under a directory "
                         "that also holds a .supertool.json above it. Set "
                         "{2} to an exact path if this project's assembler "
                         "is meant to be trusted here.".format(
                             root, detail, ENV_ASSEMBLER),
                         _ms(start)))
            return
        if could_not_look is not None:
            emit(skipped(TOOL, target,
                         "could not determine the git repo root above {0}, so "
                         "no assembler location was tried at all ({1}). That is "
                         "a claim about this run, not about the project -- set "
                         "{2} to point at the real script if it lives somewhere "
                         "this cannot look".format(
                             path.parent, could_not_look, ENV_ASSEMBLER),
                         _ms(start)))
            return
        tried = ", ".join(_locations())
        emit(skipped(TOOL, target,
                     "no assembler found at or above {0} -- tried {1}. That is "
                     "a claim about where this adapter looked, not about the "
                     "project: set {2} to point at the real script if it lives "
                     "somewhere else".format(path.parent, tried, ENV_ASSEMBLER),
                     _ms(start)))
        return

    try:
        asm = _load(script)
    except Exception as exc:  
        emit({"tool": TOOL, "file": target, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": "{0} could not be imported, so the fragment was "
                                 "NOT checked: {1}: {2}".format(
                                     script, type(exc).__name__, exc)}],
              "duration_ms": _ms(start)})
        return




    if name in getattr(asm, "_IGNORED", ()) or name.startswith("."):
        emit(skipped(TOOL, target,
                     "{0} is not assembled — the release tool passes over it".format(name),
                     _ms(start)))
        return

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        emit({"tool": TOOL, "file": target, "ok": False, "count": 1,
              "errors": [{"line": None, "col": None, "severity": "error",
                          "code": "adapter",
                          "msg": "could not read the fragment: {0}".format(exc)}],
              "duration_ms": _ms(start)})
        return

    errors: list = []









    try:
        asm.parse_fragment_name(name)
    except asm.BadFragment as exc:
        errors.append(_error(target, name, str(exc), "name"))
        emit(_verdict(target, errors, start))
        return

    if not text.strip():
        errors.append(_error(
            target, name,
            "{0}: fragment is empty — an entry nobody would ever read".format(name),
            "empty"))
        emit(_verdict(target, errors, start))
        return











    self_ref = asm.self_reference_finding(name, text)
    if self_ref:
        errors.append(_error(target, name, self_ref, "self-reference"))

    try:
        findings = asm.scan_fragment_body(name, text)
    except asm.CannotValidate as exc:
        if self_ref:



            emit(_verdict(target, errors, start))
            return
        if required(TOOL):
            emit({"tool": TOOL, "file": target, "ok": False, "count": 1,
                  "errors": [{"line": None, "col": None, "severity": "error",
                              "code": "adapter",
                              "msg": required_but_absent(TOOL, str(exc))}],
                  "duration_ms": _ms(start)})
            return
        emit(skipped(TOOL, target, str(exc), _ms(start)))
        return

    errors.extend(_error(target, name, finding, "shape") for finding in findings)
    emit(_verdict(target, errors, start))


if __name__ == "__main__":
    guard_main(TOOL, main)
