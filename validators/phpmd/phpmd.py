#!/usr/bin/env python3






















from __future__ import annotations

import json
import os
import subprocess
import sys
import pathlib
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from linebreaks import split_lines
from refusal import guard_main
from path_anchor import (anchor as _anchor, safe_realpath as _safe_realpath,
                          anchor_miss_message as _anchor_miss_message)


DEFAULT_RULESETS = "cleancode,codesize,controversial,design,naming,unusedcode"


def emit(obj: dict) -> None:
    print(json.dumps(obj))


def contained_target(file: str) -> str:












    if not file or os.path.isabs(file) or not file.startswith("-"):
        return file
    return os.path.join(os.curdir, file)


def find_project_md_rulesets(file: str) -> list[str]:





    try:
        start = pathlib.Path(file).resolve()
    except OSError:
        return []
    for ancestor in [start, *start.parents]:
        md_dir = ancestor / "gitlab-ci" / "md"
        if md_dir.is_dir():
            xmls = sorted(str(p) for p in md_dir.glob("*.xml") if p.is_file())
            if xmls:
                return xmls
    return []


def resolve_rulesets(file: str) -> tuple[str, str]:








    env_rulesets = os.environ.get("PHPMD_RULESETS", "")
    if env_rulesets:
        return env_rulesets, "variable"

    if os.environ.get("PHPMD_NO_AUTODETECT") != "1":
        project_xmls = find_project_md_rulesets(file)
        if project_xmls:
            overridden = {pathlib.Path(p).stem for p in project_xmls}
            builtins = [c for c in DEFAULT_RULESETS.split(",") if c not in overridden]
            return ",".join(project_xmls + builtins), "project"

    return DEFAULT_RULESETS, "default"


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        emit({
            "tool": "phpmd", "file": "", "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "no file arg"}],
            "duration_ms": 0,
        })
        return

    file = sys.argv[1]

    phpmd_bin = os.environ.get("PHPMD_BIN", "phpmd")
    phpmd_rulesets, ruleset_source = resolve_rulesets(file)
    phpmd_format = os.environ.get("PHPMD_FORMAT", "text")
    phpmd_exclude = os.environ.get("PHPMD_EXCLUDE", "")

    cmd = [phpmd_bin, contained_target(file), phpmd_format, phpmd_rulesets,
           "--suffixes", "php,phtml"]
    if phpmd_exclude:
        cmd += ["--exclude", phpmd_exclude]

    start = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        dur = int((time.time() - start) * 1000)
        emit({
            "tool": "phpmd", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": f"phpmd binary not found: {phpmd_bin}"}],
            "duration_ms": dur,
        })
        return
    except subprocess.TimeoutExpired:
        emit({
            "tool": "phpmd", "file": file, "ok": False, "count": 1,
            "errors": [{"line": None, "col": None, "severity": "error",
                        "code": "adapter", "msg": "timeout"}],
            "duration_ms": int((time.time() - start) * 1000),
        })
        return
    dur = int((time.time() - start) * 1000)

    raw = r.stdout or ""
    errors = []





















    real = _safe_realpath(file)
    extra = [real] if real and real != file else []
    pattern_with_rule = _anchor(file, r":(\d+)\s+(\S+)\s+(.+)$", extra_paths=extra)
    pattern_no_rule = _anchor(file, r":(\d+)\s+(.+)$", extra_paths=extra)
    for line in split_lines(raw):
        line = line.strip()
        if not line:
            continue

        m = pattern_with_rule.search(line)
        if m:
            lineno = int(m.group(1))
            rule = m.group(2).strip()
            msg = m.group(3).strip()
            errors.append({
                "line": lineno,
                "col": None,
                "severity": "warning",
                "code": rule,
                "msg": msg,
                **context_fields(file, lineno),
            })
            continue

        m2 = pattern_no_rule.search(line)
        if m2:
            lineno = int(m2.group(1))
            msg = m2.group(2).strip()
            errors.append({
                "line": lineno,
                "col": None,
                "severity": "warning",
                "code": None,
                "msg": msg,
                **context_fields(file, lineno),
            })

    if not errors and raw.strip():






        errors = [{"line": None, "col": None, "severity": "error",
                   "code": "adapter",
                   "msg": _anchor_miss_message(file, raw, raw.strip()[:300])}]

    count = len(errors)
    emit({
        "tool": "phpmd",
        "file": file,
        "ok": count == 0,
        "count": count,
        "errors": errors,
        "duration_ms": dur,
        "ruleset_source": ruleset_source,
    })


if __name__ == "__main__":
    guard_main("phpmd", main)
