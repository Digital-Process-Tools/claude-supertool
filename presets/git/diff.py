#!/usr/bin/env python3
"""Git diff with review intelligence — the questions you ask the diff, pre-answered.

Raw `git diff` hands back a blob you then scan by eye or pipe through grep.
This op answers the follow-ups in the same call:

  - classified file list + +/- totals (what kind of change, how heavy)
  - red-flag scan of ADDED lines (debug code, conflict markers) with file:line
  - forbidden-path guard (project rules: generated files, secrets, install-only)
  - missing-test pairing (a new source class with no test file)
  - next-move hints (new class added → regenerate caches, etc.)

The engine is generic; project policy lives in `.supertool.json` under
`ops.git-diff.*` and arrives here as SUPERTOOL_* env vars (same mechanism as
gl-job's job_patterns). Generic defaults are universal — var_dump, console.log,
conflict markers — so the op is useful out of the box with no config.

Modes (first arg):
  (none)   working tree vs HEAD  — staged + unstaged tracked changes
  staged   index vs HEAD         — what a commit would capture
  branch   merge-base(base)..HEAD — the reviewer's diff (base: master|main|$2)
  PATH     working tree vs HEAD, scoped to PATH
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

MAX_FILES = 60
MAX_FLAGS = 40

# Generic, universal red flags — debug leftovers + conflict markers. Project
# config (red_flags_extra) adds language/house-rule patterns on top.
DEFAULT_RED_FLAGS = [
    {"pattern": r"\bvar_dump\s*\(", "label": "var_dump"},
    {"pattern": r"\bprint_r\s*\(", "label": "print_r"},
    {"pattern": r"\bvar_export\s*\(", "label": "var_export"},
    {"pattern": r"\bconsole\.(log|debug|warn)\s*\(", "label": "console.*"},
    {"pattern": r"\bdebugger\s*;", "label": "debugger"},
    {"pattern": r"<<<<<<<", "label": "conflict marker"},
    {"pattern": r">>>>>>>", "label": "conflict marker"},
    {"pattern": r"\|\|\|\|\|\|\|", "label": "conflict marker"},
]


def _git(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git"] + args, capture_output=True, text=True, timeout=timeout)


def _json_env(key: str) -> list:
    """Read a JSON-list config value from SUPERTOOL_<KEY>; [] on absent/malformed."""
    raw = os.environ.get(key, "")
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


def _resolve_base(arg: str) -> str:
    if arg:
        return arg
    for c in ("master", "main"):
        if _git(["rev-parse", "--verify", "--quiet", c]).returncode == 0:
            return c
    return "master"


def _classify(path: str) -> str:
    """Generic file classification — informational grouping, not policy."""
    low = path.lower()
    base = path.rsplit("/", 1)[-1]
    ext = "." + base.rsplit(".", 1)[-1] if "." in base else ""
    if "/generated/" in low:
        return "generated"
    if re.search(r"(?:^|/)migrations?/", low):
        return "migration"
    is_test = (
        "/test/" in low or "/tests/" in low
        or base.endswith(("Test.php", "Test.class.php", "_test.py"))
        or re.search(r"\.(test|spec)\.[jt]sx?$", base) is not None
    )
    if is_test:
        return "test"
    if "i18n" in low and ext == ".xml":
        return "i18n"
    if ext in (".scss", ".css", ".less"):
        return "styles"
    if ext in (".js", ".ts", ".jsx", ".tsx", ".vue"):
        return "js"
    if ext in (".php", ".py", ".rb", ".go", ".java", ".rs"):
        return "src"
    if ext in (".json", ".ini", ".yaml", ".yml", ".neon", ".toml", ".xml"):
        return "config"
    return "other"


def _changed_files(diff_args: list[str]) -> list[tuple[str, str]]:
    """[(status, path)] from name-status. R/C entries report the new path."""
    res = _git(["-c", "core.quotepath=false", "diff", "--name-status"] + diff_args)
    out: list[tuple[str, str]] = []
    if res.returncode != 0:
        return out
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0][0]  # R100/C75 → R/C
        path = parts[-1]      # new path for renames/copies
        out.append((status, path))
    return out


def _scan_red_flags(diff_args: list[str], patterns: list[dict]) -> list[str]:
    """Scan ADDED lines (unified=0) for red-flag patterns. Returns 'path:line label'."""
    res = _git(["-c", "core.quotepath=false", "diff", "--unified=0", "--no-color"] + diff_args)
    if res.returncode != 0:
        return []
    compiled = []
    for p in patterns:
        pat = p.get("pattern")
        if not pat:
            continue
        try:
            compiled.append((re.compile(pat), p.get("label", pat), p.get("ext")))
        except re.error:
            continue
    hits: list[str] = []
    cur_path = ""
    new_line = 0
    for line in res.stdout.splitlines():
        if line.startswith('+++ "b/'):
            cur_path = line[7:].rstrip('"')
            continue
        if line.startswith("+++ b/"):
            cur_path = line[6:]
            continue
        if line.startswith("+++ "):
            cur_path = line[4:]
            continue
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
        if m:
            new_line = int(m.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++ "):
            content = line[1:]
            for rx, label, ext in compiled:
                if ext and not cur_path.endswith(ext):
                    continue
                if rx.search(content):
                    hits.append(f"{cur_path}:{new_line}  {label}  →  {content.strip()[:80]}")
            new_line += 1
    return hits


def _check_forbidden(paths: list[str], rules: list[dict]) -> list[str]:
    out = []
    for p in paths:
        for rule in rules:
            pat = rule.get("pattern", "")
            if not pat:
                continue
            try:
                hit = re.search(pat, p) is not None
            except re.error:
                hit = pat in p
            if hit:
                out.append(f"{p}  —  {rule.get('reason', 'forbidden path')}")
                break
    return out


def _check_test_pairing(changed: list[tuple[str, str]], rules: list[dict]) -> list[str]:
    """Flag ADDED source files whose expected test exists neither in diff nor on disk."""
    if not rules:
        return []
    changed_set = {p for _, p in changed}
    out = []
    for status, path in changed:
        if status != "A" or _classify(path) != "src":
            continue
        for rule in rules:
            src_re = rule.get("src", "")
            tmpl = rule.get("test", "")
            if not src_re or not tmpl:
                continue
            try:
                m = re.search(src_re, path)
            except re.error:
                m = None
            if not m:
                continue
            try:
                expected = tmpl.format(**m.groupdict())
            except (KeyError, IndexError):
                continue
            on_disk = os.path.exists(expected)
            if expected not in changed_set and not on_disk:
                out.append(f"{path}  —  no test ({expected})")
            break
    return out


def _path_hints(changed: list[tuple[str, str]], rules: list[dict]) -> list[str]:
    """Emit a message once if any ADDED path matches a hint trigger."""
    out = []
    added = [p for s, p in changed if s == "A"]
    for rule in rules:
        pat = rule.get("added", "")
        msg = rule.get("message", "")
        if not pat or not msg:
            continue
        try:
            if any(re.search(pat, p) for p in added):
                out.append(msg)
        except re.error:
            continue
    return out


def main() -> int:
    arg1 = sys.argv[1] if len(sys.argv) > 1 else ""
    arg2 = sys.argv[2] if len(sys.argv) > 2 else ""

    if _git(["rev-parse", "--is-inside-work-tree"]).returncode != 0:
        print("ERROR: not inside a git repository.")
        return 1

    if arg1 == "staged":
        mode, scope, diff_args = "staged", "index vs HEAD", ["--cached"]
    elif arg1 == "branch":
        base = _resolve_base(arg2)
        if _git(["rev-parse", "--verify", "--quiet", base]).returncode != 0:
            print(f"ERROR: base ref {base!r} not found. Did you fetch?")
            return 1
        mode, scope, diff_args = "branch", f"merge-base({base})..HEAD", [f"{base}...HEAD"]
    elif arg1:
        mode, scope, diff_args = "path", f"working vs HEAD — {arg1}", ["HEAD", "--", arg1]
    else:
        mode, scope, diff_args = "working", "working tree vs HEAD", ["HEAD"]

    print(f"# git-diff ({mode})")
    print(f"Scope: {scope}")

    changed = _changed_files(diff_args)
    if not changed:
        print("No changes.")
        return 0

    shortstat = _git(["diff", "--shortstat"] + diff_args)
    if shortstat.returncode == 0 and shortstat.stdout.strip():
        print(shortstat.stdout.strip())

    # Files grouped by classification
    groups: dict[str, list[str]] = {}
    for status, path in changed:
        groups.setdefault(_classify(path), []).append(f"{status}  {path}")
    print(f"\n## Files changed ({len(changed)})")
    shown = 0
    for label in ("src", "test", "i18n", "config", "js", "styles", "migration",
                  "generated", "other"):
        rows = groups.get(label)
        if not rows:
            continue
        print(f"\n### {label} ({len(rows)})")
        for r in rows:
            if shown >= MAX_FILES:
                break
            print(f"  {r}")
            shown += 1
        if shown >= MAX_FILES:
            print(f"  … truncated at {MAX_FILES} files")
            break

    # Policy from .supertool.json (env), generic defaults otherwise
    red_flags = DEFAULT_RED_FLAGS + _json_env("SUPERTOOL_RED_FLAGS_EXTRA")
    forbidden = _json_env("SUPERTOOL_FORBIDDEN_PATHS")
    pairing = _json_env("SUPERTOOL_TEST_PAIRING")
    hints_cfg = _json_env("SUPERTOOL_HINTS")

    paths = [p for _, p in changed]

    forbidden_hits = _check_forbidden(paths, forbidden)
    if forbidden_hits:
        print(f"\n## ⚠ Forbidden paths ({len(forbidden_hits)})")
        for h in forbidden_hits:
            print(f"  {h}")

    flag_hits = _scan_red_flags(diff_args, red_flags)
    if flag_hits:
        print(f"\n## ⚠ Red flags in added lines ({len(flag_hits)})")
        for h in flag_hits[:MAX_FLAGS]:
            print(f"  {h}")
        if len(flag_hits) > MAX_FLAGS:
            print(f"  … {len(flag_hits) - MAX_FLAGS} more")

    pairing_hits = _check_test_pairing(changed, pairing)
    if pairing_hits:
        print(f"\n## ⚠ New source without a test ({len(pairing_hits)})")
        for h in pairing_hits:
            print(f"  {h}")

    hint_msgs = _path_hints(changed, hints_cfg)
    if hint_msgs:
        print("\n## Next")
        for m in hint_msgs:
            print(f"  {m}")

    if not (forbidden_hits or flag_hits or pairing_hits):
        print("\n✓ No red flags, forbidden paths, or missing tests.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
