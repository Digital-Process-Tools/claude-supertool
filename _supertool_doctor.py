

























from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

import _supertool


def _doctor_interpreter() -> Dict[str, Any]:






















    machine = platform.machine()
    info: Dict[str, Any] = {
        "executable": sys.executable,
        "version": platform.python_version(),
        "machine": machine,
        "platform": sys.platform,
        "rosetta": None,
    }
    if sys.platform == "darwin":
        if machine == "arm64":
            info["rosetta"] = False
        else:
            try:
                r = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"],
                                   capture_output=True, text=True, timeout=5,
                                   encoding="utf-8", errors="replace")
                out = r.stdout.strip()
                if r.returncode == 0 and out in ("0", "1"):
                    info["rosetta"] = out == "1"
            except (OSError, subprocess.TimeoutExpired):






                pass
    return info


def _doctor_cpu_topology() -> Dict[str, Any]:









    topo: Dict[str, Any] = {
        "logical_cpus": os.cpu_count(),
        "performance": None,
        "efficiency": None,
        "state": "unknown",
    }
    if sys.platform == "darwin":
        try:
            p = subprocess.run(["sysctl", "-n", "hw.perflevel0.logicalcpu"],
                               capture_output=True, text=True, timeout=5,
                               encoding="utf-8", errors="replace")
            e = subprocess.run(["sysctl", "-n", "hw.perflevel1.logicalcpu"],
                               capture_output=True, text=True, timeout=5,
                               encoding="utf-8", errors="replace")
            if p.returncode == 0 and p.stdout.strip():
                if e.returncode == 0 and e.stdout.strip():
                    topo["performance"] = int(p.stdout.strip())
                    topo["efficiency"] = int(e.stdout.strip())
                    topo["state"] = "split"
                else:


                    topo["state"] = "uniform"
        except (OSError, subprocess.TimeoutExpired, ValueError):
            topo["state"] = "unknown"
    return topo


def _doctor_symlink() -> Dict[str, Any]:















    which = _supertool._which_excluding_cwd("supertool")
    result: Dict[str, Any] = {"which": which, "symlink_target": None,
                              "dangling": False}
    if which is None:
        return result
    if os.path.islink(which):






        try:
            target = os.readlink(which)
        except OSError:
            result["symlink_target"] = None
            result["dangling"] = None
        else:
            resolved = target if os.path.isabs(target) else os.path.normpath(
                os.path.join(os.path.dirname(which), target))
            result["symlink_target"] = resolved
            result["dangling"] = not os.path.exists(resolved)
    running = os.path.abspath(__file__)
    try:
        result["path_resolves_to_running_module"] = (
            os.path.realpath(which) == os.path.realpath(running))
    except OSError:
        result["path_resolves_to_running_module"] = None
    result["running_module"] = running










    result["path_version"] = None
    result["path_version_state"] = None
    if result["path_resolves_to_running_module"] is False and not result["dangling"]:
        try:
            proc = subprocess.run([which, "version"], capture_output=True,
                                  text=True, timeout=5, encoding="utf-8",
                                  errors="replace")
            match = (re.match(r"supertool\s+(\S+)", proc.stdout.strip())
                     if proc.returncode == 0 else None)
        except (OSError, subprocess.TimeoutExpired):
            match = None
        if match:
            result["path_version"] = match.group(1)
            result["path_version_state"] = (
                "current" if match.group(1) == _supertool.VERSION else "stale")
        else:
            result["path_version_state"] = "unknown"
    return result


def _doctor_tracked_files() -> Optional[List[str]]:





















    try:
        r = subprocess.run(["git", "ls-files", "-z"], capture_output=True,
                           text=True, timeout=15,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return [seg for seg in r.stdout.split("\x00") if seg]


def _doctor_looks_absent(text: str) -> bool:















    low = text.lower()
    return ("not found" in low or "not installed" in low
            or "could not be run" in low or "binary not found" in low)


def _doctor_classify_probe(data: Dict[str, Any]) -> "Tuple[str, str]":













    if data.get("timeout"):
        return "could not tell", f"timed out after {data.get('duration_ms', 0)}ms"
    if "skipped" in data:
        reason = str(data["skipped"])
        if _doctor_looks_absent(reason):
            return "absent", reason
        return "could not tell", reason
    errors = data.get("errors") or []
    if any(isinstance(e, dict) and e.get("code") == "adapter" for e in errors):
        msg = str(errors[0].get("msg", "")) if errors else ""
        if _doctor_looks_absent(msg):
            return "absent", msg
        return "could not tell", msg
    if "ok" in data:
        return "resolves", f"ok={data.get('ok')}, count={data.get('count')}"
    return "could not tell", "adapter replied without a verdict"


def _doctor_validators_section(config: Dict[str, Any], probe: bool) -> str:































    validators = config.get("validators") or {}
    lines: List[str] = ["## Toolchain validators (#1950)"]
    if not validators:
        lines.append("- no \"validators\" section in .supertool.json")
        return "\n".join(lines)

    files = _doctor_tracked_files()
    scope_unknown = files is None

    resolves = absent = unknown = not_applicable = 0
    rows: List[str] = []
    _prior_cache_env = os.environ.get("SUPERTOOL_NO_VALIDATOR_CACHE")
    if probe:
        os.environ["SUPERTOOL_NO_VALIDATOR_CACHE"] = "1"
    try:
        for name in sorted(validators):
            spec = validators[name]
            if not isinstance(spec, dict):
                continue
            glob = spec.get("match", "*")
            target: Optional[str] = None
            if not scope_unknown:
                for f in files:  
                    if _supertool._match_glob(f, glob):
                        target = f
                        break

            if scope_unknown:
                unknown += 1
                rows.append(f"- {name}: could not tell whether this tree has a "
                            "matching file (`git ls-files` did not answer)")
                continue
            if target is None:
                not_applicable += 1
                rows.append(f"- {name}: not applicable (no tracked file matches "
                            f"`{glob}`)")
                continue
            if not probe:
                unknown += 1
                rows.append(f"- {name}: in scope "
                            f"({_supertool._flat_field(target, disclose_newline=True)}) — "
                            "could not tell without probing; run doctor:probe")
                continue
            try:
                data = _supertool._validator_run_one(name, spec, target)
            except Exception as exc:  
                unknown += 1





                rows.append(f"- {name}: could not tell — probing it raised "
                            f"{type(exc).__name__}: {_supertool._flat_field(str(exc))}")
                continue
            if not isinstance(data, dict):
                unknown += 1
                rows.append(f"- {name}: could not tell — probe returned no verdict")
                continue
            state, detail = _doctor_classify_probe(data)
            if state == "resolves":
                resolves += 1
            elif state == "absent":
                absent += 1
            else:
                unknown += 1
            rows.append(
                f"- {name} ({_supertool._flat_field(target, disclose_newline=True)}): "
                f"{state} — {_supertool._flat_field(detail)}")
    finally:
        if probe:
            if _prior_cache_env is None:
                os.environ.pop("SUPERTOOL_NO_VALIDATOR_CACHE", None)
            else:
                os.environ["SUPERTOOL_NO_VALIDATOR_CACHE"] = _prior_cache_env

    lines.append(f"- {len(validators)} configured, {resolves} resolves, "
                 f"{absent} absent, {unknown} could not tell, "
                 f"{not_applicable} not applicable")
    lines.extend(rows)
    return "\n".join(lines)


def _doctor_looks_absent_formatter(text: str) -> bool:














    low = (text or "").lower()
    if _doctor_looks_absent(text):
        return True
    return "no such file or directory" in low


def _doctor_classify_formatter_probe(data: Any) -> "Tuple[str, str]":

















    if not isinstance(data, dict):
        return "could not tell", "probe returned no verdict"
    errors = data.get("errors") or []
    if any(isinstance(e, dict) and e.get("code") == "adapter" for e in errors):
        msg = str(errors[0].get("msg", "")) if errors else ""
        if _doctor_looks_absent_formatter(msg):
            return "absent", msg
        return "could not tell", msg
    msg = data.get("msg")
    if msg is not None:
        if _doctor_looks_absent_formatter(str(msg)):
            return "absent", str(msg)
        return "could not tell", str(msg)
    raw = data.get("raw")
    if isinstance(raw, str) and not data.get("ok", True) and (
        "Traceback (most recent call last)" in raw
    ):










        return "could not tell", "adapter raised an exception -- " + raw[:200]
    if "ok" in data:
        return "resolves", f"ok={data.get('ok')}"
    return "could not tell", "adapter replied without a verdict"


def _doctor_formatters_section(config: Dict[str, Any], probe: bool) -> str:






















    formatters = config.get("formatters")
    lines: List[str] = ["## Formatters (#2086)"]
    if formatters is None:





        lines.append("- " + _supertool.mark("⚠") + " no \"formatters\" section in "
                     ".supertool.json — no formatter configured, and nothing "
                     "on record says that is intentional")
        return "\n".join(lines)
    if formatters == {}:



        lines.append("- 0 configured — \"formatters\": {} on record, no "
                     "formatter declared")
        return "\n".join(lines)
    if not isinstance(formatters, dict) or not formatters:






        lines.append("- " + _supertool.mark("⚠") + " \"formatters\" is set but is not a "
                     "table (got " + type(formatters).__name__ + ") — expected "
                     "either no key, {} for an explicit decision, or a table "
                     "of formatter specs")
        return "\n".join(lines)

    files = _doctor_tracked_files()
    scope_unknown = files is None

    resolves = absent = unknown = not_applicable = 0
    rows: List[str] = []
    for name in sorted(formatters):
        spec = formatters[name]
        if not isinstance(spec, dict):
            continue
        glob = spec.get("match", "*")
        target: Optional[str] = None
        if not scope_unknown:
            for f in files:  
                if _supertool._match_glob(f, glob):
                    target = f
                    break

        if scope_unknown:
            unknown += 1
            rows.append(f"- {name}: could not tell whether this tree has a "
                        "matching file (`git ls-files` did not answer)")
            continue
        if target is None:
            not_applicable += 1
            rows.append(f"- {name}: not applicable (no tracked file matches "
                        f"`{glob}`)")
            continue
        if not probe:
            unknown += 1
            rows.append(f"- {name}: in scope "
                        f"({_supertool._flat_field(target, disclose_newline=True)}) — "
                        "could not tell without probing; run doctor:probe")
            continue
        try:
            data = _supertool._formatter_run_one(name, spec, target)
        except Exception as exc:  
            unknown += 1
            rows.append(f"- {name}: could not tell — probing it raised "
                        f"{type(exc).__name__}: {_supertool._flat_field(str(exc))}")
            continue
        if not isinstance(data, dict):
            unknown += 1
            rows.append(f"- {name}: could not tell — probe returned no verdict")
            continue
        state, detail = _doctor_classify_formatter_probe(data)
        if state == "resolves":
            resolves += 1
        elif state == "absent":
            absent += 1
        else:
            unknown += 1
        rows.append(
            f"- {name} ({_supertool._flat_field(target, disclose_newline=True)}): "
            f"{state} — {_supertool._flat_field(detail)}")

    lines.append(f"- {len(formatters)} configured, {resolves} resolves, "
                 f"{absent} absent, {unknown} could not tell, "
                 f"{not_applicable} not applicable")
    lines.extend(rows)
    return "\n".join(lines)


def op_doctor(arg: str = "") -> str:















    probe = arg.strip().lower() == "probe"
    lines: List[str] = []

    interp = _doctor_interpreter()
    if interp.get("rosetta") is True:
        lines.append(
            f"!! ARCHITECTURE MISMATCH: this interpreter ({interp['machine']}) "
            "is running under Rosetta 2 (or equivalent binary translation) on "
            "Apple Silicon. Results are correct, only slower — measured on "
            "the machine that filed #1857: ~3x slower interpreter start, "
            "~3.4x slower subprocess-spawn CPU time, both worse for a tool "
            "whose whole job is spawning subprocesses. Install a native "
            "arm64 python3.")
        lines.append("")

    lines.append("## Interpreter")
    lines.append(f"- executable: {interp['executable']}")
    lines.append(f"- version: {interp['version']}")
    lines.append(f"- machine: {interp['machine']} ({interp['platform']})")
    if interp["platform"] == "darwin":
        rosetta = interp["rosetta"]
        if rosetta is True:
            lines.append("- rosetta: yes — translated, see warning above")
        elif rosetta is False:
            lines.append("- rosetta: no — native")
        else:
            lines.append("- rosetta: could not tell "
                         "(sysctl.proc_translated did not answer)")
    else:
        lines.append("- rosetta: not applicable (checked on macOS only)")
    lines.append("")

    topo = _doctor_cpu_topology()
    lines.append("## CPU topology")
    lines.append(f"- logical cpus: {topo['logical_cpus']}")
    if topo["state"] == "split":
        lines.append(f"- performance cores: {topo['performance']}")
        lines.append(f"- efficiency cores: {topo['efficiency']}")
        lines.append("- supertool does not size worker pools itself; a "
                     "caller that does should read this rather than "
                     "logical_cpus alone (#1857).")
    elif topo["state"] == "uniform":
        lines.append("- no performance/efficiency split reported by this host")
    else:
        lines.append("- performance/efficiency split: could not tell "
                     "(no portable way to ask on this platform)")
    lines.append("")

    sym = _doctor_symlink()
    lines.append("## supertool on PATH")
    if sym["which"] is None:
        lines.append("- not found on PATH")
    else:
        lines.append(f"- resolves to: {sym['which']}")
        if sym["symlink_target"]:
            state = "DANGLING" if sym["dangling"] else "ok"
            lines.append(f"- symlink target: {sym['symlink_target']} ({state})")
        if sym.get("path_resolves_to_running_module") is False:
            version_state = sym.get("path_version_state")
            if version_state == "stale":
                lines.append(
                    f"- NOTE: PATH entry answers as supertool "
                    f"{sym['path_version']}, this call is running {_supertool.VERSION} "
                    "— stale build, update or reinstall it.")
            elif version_state == "current":
                pass
            elif sym.get("dangling"):






                pass
            else:
                lines.append(
                    "- NOTE: could not tell whether the PATH entry "
                    f"({sym['which']}) is current — it differs by path from "
                    f"the module answering this call ({sym['running_module']}) "
                    "and running it with `version` did not answer, so this "
                    "is neither confirmed current nor confirmed stale "
                    "(CLAUDE.md: run python3 supertool.py inside a worktree, "
                    "not the global symlink, if that is what this is).")
    lines.append("")

    config = _supertool._load_config()
    lines.append("## Project config")
    lines.append(f"- .supertool.json: {_supertool._CONFIG_PATH or 'not found'}")
    watch_name = ((config.get("ops") or {}).get("watch") or {}).get("watch_name")
    lines.append(f"- watch fleet: {watch_name or 'not configured'}")
    lines.append("")

    lines.append(_doctor_validators_section(config, probe))
    lines.append("")
    lines.append(_doctor_formatters_section(config, probe))
    return "\n".join(lines) + "\n"


def _init_run_git(args, cwd):






    try:
        r = subprocess.run(["git"] + args, capture_output=True, text=True,
                           timeout=15, cwd=cwd, encoding="utf-8",
                           errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _init_parse_remote(url):








    url = url.strip()
    if not url:
        return None
    scp = re.match(r"^[\w.+-]+@([^:/]+):(.+)$", url)  
    if scp:
        host, path = scp.group(1), scp.group(2)
    else:
        uri = re.match(  
            r"^[a-zA-Z][\w+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", url)
        if not uri:
            return None
        host, path = uri.group(1), uri.group(2)
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    if not host or not path:
        return None
    return host, path


def _init_tracked_files(root):

















    try:
        r = subprocess.run(["git", "ls-files"], capture_output=True,
                           text=True, timeout=15, cwd=root,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return [seg for seg in r.stdout.split("\n") if seg]


def _init_platform(host):














    host = host.lower()


    if host == "github.com":
        return "github"






    if host == "gitlab.com" or "gitlab" in host:
        return "gitlab"
    return None








_INIT_CONDITIONAL_VALIDATORS = (

    ("ruff", (".py",), "ruff", "*.py", {
        "cmd": "{python} {supertool_dir}/validators/ruff/ruff.py {file}",
        "match": "*.py",
        "hooks_into": ["edit", "replace", "replace_lines", "paste", "append", "vim"],
        "rollback_on_fail": False,
        "timeout": 30,
    }),
    ("shellcheck", (".sh", ".bash"), "shellcheck", "*.{sh,bash}", {
        "cmd": "{python} {supertool_dir}/validators/shellcheck/shellcheck.py {file}",
        "match": "*.{sh,bash}",
        "hooks_into": ["edit", "replace", "replace_lines", "paste", "append", "vim"],
        "rollback_on_fail": False,
        "timeout": 30,
    }),
)

_INIT_JSONLINT_SPEC = {
    "cmd": "{python} {supertool_dir}/validators/jsonlint/jsonlint.py {file}",
    "match": "*.json",
    "hooks_into": ["edit", "replace", "replace_lines", "paste", "append", "vim"],
    "rollback_on_fail": True,
    "timeout": 10,
}


def op_init(mode: str = "") -> str:


























































    write = mode.strip().lower() in ("write", "apply", "commit")

    cwd = os.path.abspath(os.getcwd())
    root = _init_run_git(["rev-parse", "--show-toplevel"], cwd)
    if root is None:
        return ("ERROR: not inside a git working tree (or the repo is bare) "
                "-- init needs a real repo root to derive defaults from.\n")
    root = os.path.abspath(root)





    if os.path.normcase(root) != os.path.normcase(cwd):
        return (f"ERROR: init only writes at the repo root -- run it from "
                f"{root}, not {cwd}.\n")

    config_path = os.path.join(root, ".supertool.json")
    if os.path.isfile(config_path):
        try:
            with open(config_path, encoding="utf-8") as fh:
                existing = fh.read(2000)
        except OSError as exc:
            existing = f"(could not read it to show you: {exc})"
        return (f"ERROR: {config_path} already exists -- refusing to "
                "overwrite it. init never touches a config that is already "
                "there, hand-written or not (allow_vim_shell/"
                "allow_outside_cwd both widen what supertool may do, so a "
                "silent rewrite is worse than doing nothing -- and merging "
                "new presets into an edited file is a harder, separate "
                "problem #858 leaves to a follow-up). Existing contents:\n"
                f"{existing}\n")

    remote_url = _init_run_git(["remote", "get-url", "origin"], root)
    if remote_url is None:
        return ("ERROR: no 'origin' remote -- cannot derive "
                "defaults.github_repo/gitlab_project, and a guessed one "
                "would answer confidently about the wrong repo. Write "
                ".supertool.json by hand.\n")

    parsed = _init_parse_remote(remote_url)
    if parsed is None:
        return (f"ERROR: could not parse origin remote URL "
                f"{_supertool._flat_field(remote_url)!r} -- unrecognised form. Write "
                ".supertool.json by hand.\n")
    host, slug = parsed
    platform = _init_platform(host)
    if platform is None:
        return (f"ERROR: origin remote host {_supertool._flat_field(host)!r} is "
                "neither github.com nor a recognised GitLab host -- init "
                "does not know which preset family to enable. Write "
                ".supertool.json by hand.\n")

    tracked = _init_tracked_files(root)
    if tracked is None:
        return ("ERROR: `git ls-files` did not answer -- cannot tell "
                "whether the xml preset or the ruff/shellcheck validators "
                "belong in this config, and guessing 'no' would silently "
                "drop them for a repo that may well need them. Write "
                ".supertool.json by hand, or re-run init once git answers "
                "here.\n")

    presets = [platform, "git"]
    if any(f.endswith(".xml") for f in tracked):
        presets.append("xml")

    validators = {"jsonlint": dict(_INIT_JSONLINT_SPEC)}
    skipped_validators = []
    for name, suffixes, tool, glob, spec in _INIT_CONDITIONAL_VALIDATORS:
        if not any(f.endswith(suffixes) for f in tracked):
            continue
        if shutil.which(tool):
            validators[name] = dict(spec)
        else:
            skipped_validators.append(
                f"{name} ({glob} tracked, but {tool!r} does not resolve on "
                "PATH)")

    defaults_key = "github_repo" if platform == "github" else "gitlab_project"
    doc = {
        "defaults": {defaults_key: slug},
        "rtk": False,
        "allow_outside_cwd": False,
        "allow_vim_shell": False,
        "presets": presets,
        "validators": validators,
    }
    content = json.dumps(doc, indent=2) + "\n"

    out = []
    if write:








        out.append(_supertool._run_with_validators(
            "paste", ["paste", ".supertool.json", content],
            lambda: _supertool.op_paste(".supertool.json", content)))
    else:
        out.append(f"PREVIEW -- nothing written. Run 'init:write' to create "
                   f"{config_path}.\n")
        out.append(content)
    if skipped_validators:
        out.append("Declined (tool not resolvable on this PATH -- no "
                   "validator was declared for it; add it by hand once "
                   "installed):")
        out.extend(f"  - {s}" for s in skipped_validators)
    return "\n".join(out) + ("" if out[-1].endswith("\n") else "\n")
