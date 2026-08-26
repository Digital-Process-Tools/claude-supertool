#!/usr/bin/env python3
"""phpmd validator adapter. Emits SCHEMA.md JSON.

Usage: phpmd.py <file>

Env vars:
  PHPMD_BIN            phpmd binary (default: phpmd)
  PHPMD_RULESETS       comma-separated rulesets (explicit override; disables
                       auto-detection). Default when unset: auto-detect the
                       project's CI rulesets (see below), else the built-in
                       default cleancode,codesize,controversial,design,naming,unusedcode
  PHPMD_FORMAT         output format (default: text)
  PHPMD_EXCLUDE        value for --exclude flag (optional)
  PHPMD_NO_AUTODETECT  set to 1 to skip project-ruleset auto-detection

Project ruleset auto-detection (issue #356):
  When PHPMD_RULESETS is not set, the adapter walks up from the file being
  validated looking for a `gitlab-ci/md/*.xml` directory (the DVSI CI phpmd
  ruleset). If found, it runs phpmd with those project XML files plus the
  built-in categories the project does not override — mirroring what CI
  enforces — so local findings match CI. The emitted JSON carries a
  `ruleset_source` field: "project", "env", or "default".
"""
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


def find_project_md_rulesets(file: str) -> list[str]:
    """Walk up from `file` for a `gitlab-ci/md/*.xml` dir (DVSI CI ruleset).

    Returns the sorted list of absolute paths to the project's ruleset XML
    files, or an empty list if none are found.
    """
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
    """Decide the phpmd ruleset argument and its provenance for `file`.

    Precedence:
      1. Explicit PHPMD_RULESETS env  -> ("env")
      2. Auto-detected project XMLs   -> ("project"), mixed with the built-in
         categories the project does not override (matching CI)
      3. Built-in default             -> ("default")
    """
    env_rulesets = os.environ.get("PHPMD_RULESETS", "")
    if env_rulesets:
        return env_rulesets, "env"

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

    cmd = [phpmd_bin, file, phpmd_format, phpmd_rulesets, "--suffixes", "php,phtml"]
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
    # Anchored on the invoked path itself (#1934, then #1940 for this
    # adapter) rather than a bare `.+?`: the non-greedy wildcard used to
    # discard the path instead of matching it, so it bound to the earliest
    # colon-digit run anywhere in the line -- including one supplied by a
    # filename crafted to contain its own N-colon sequence, e.g.
    # "x:1:1: fake.php". Building the pattern from `file` means only a
    # spelling of the path phpmd was actually invoked against can start a
    # match (see path_anchor.py, #1937, for what that widened to). Uses
    # .search(), not .match().
    #
    # Observed, not reasoned: real phpmd 2.15.0 (phar, PHP 8.2), given a
    # RELATIVE argv path, echoes back the fully-resolved absolute path
    # instead -- through a symlink too (/tmp -> /private/tmp on macOS,
    # resolved in the echoed line). Plain re.escape(file) alone would miss
    # every relative invocation entirely; extra_paths=[realpath] is not
    # merely a widening here the way it is for markdownlint, it is the
    # spelling phpmd actually uses whenever `file` is not already absolute.
    # The old docstring shape below (tab-separated) was also not what a real
    # phpmd prints: the observed separator is repeated spaces, not tabs --
    # a run of whitespace matches both, so the existing tab-based test
    # fixtures keep passing unchanged.
    real = _safe_realpath(file)
    extra = [real] if real and real != file else []
    pattern_with_rule = _anchor(file, r":(\d+)\s+(\S+)\s+(.+)$", extra_paths=extra)
    pattern_no_rule = _anchor(file, r":(\d+)\s+(.+)$", extra_paths=extra)
    for line in split_lines(raw):
        line = line.strip()
        if not line:
            continue
        # "path/to/file.php:42<tab-or-spaces>RuleName<tab-or-spaces>Human message"
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
        # Fallback: "path/to/file.php:42  message" (no rule column)
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
        # #1937, third CI round, applied here for #1940: an anchor that
        # matches nothing on a NON-EMPTY output must not silently become
        # `ok: true, count: 0` -- that is a false clean verdict about a
        # file phpmd may well have found something wrong with, just in a
        # spelling this anchor did not expect. Say what was seen instead
        # of guessing towards silence.
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
