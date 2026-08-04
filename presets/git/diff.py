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

Trailing `full` (last arg, any mode) appends the raw +/- hunks under `## Patch`
below the summary — for reading a change / writing an honest commit message.
  git-diff:full   git-diff:PATH:full   git-diff:staged:full   git-diff:branch:BASE:full
"""
from __future__ import annotations

import json
import os
import re
import sys

# Sibling import: runtime puts this dir on sys.path[0]; the test harness
# loads scripts via importlib (no dir on path), so add it explicitly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _git_common import _git, use_utf8_stdout  # noqa: E402

MAX_FILES = 60
MAX_FLAGS = 40

# ASCII fallbacks for the status glyphs, keyed by the rich glyph. Presets run as
# standalone subprocesses and can't import supertool's helpers, so this mirrors
# supertool.mark() locally. Honoured when SUPERTOOL_PLAIN is set (by the --plain
# flag, which exports it, or directly) — see issue #308.
_PLAIN_MARKERS = {
    "⚠": "[WARN]", "✓": "[OK]", "✗": "[FAIL]", "ℹ": "[INFO]",
    # Decorative separators — folded too so plain output is fully ASCII.
    "→": "->", "—": "--", "…": "...",
}


def _plain() -> bool:
    return os.environ.get("SUPERTOOL_PLAIN", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def _mark(glyph: str) -> str:
    """Rich glyph, or its stable ASCII marker in plain mode. Unknown → unchanged."""
    return _PLAIN_MARKERS.get(glyph, glyph) if _plain() else glyph

# Generic, universal red flags — debug leftovers + conflict markers. Project
# config (red_flags_extra) adds language/house-rule patterns on top.
DEFAULT_RED_FLAGS = [
    {"pattern": r"\bvar_dump\s*\(", "label": "var_dump"},
    {"pattern": r"\bprint_r\s*\(", "label": "print_r"},
    {"pattern": r"\bvar_export\s*\(", "label": "var_export"},
    {"pattern": r"\bconsole\.(log|debug|warn)\s*\(", "label": "console.*"},
    {"pattern": r"\bdebugger\s*;", "label": "debugger"},
    # Anchored to line start: a real git conflict marker is always at column 0.
    # Matching the bare substring would false-positive on any file that merely
    # mentions markers (resolve.py, its tests, these very docs).
    {"pattern": r"^<<<<<<<", "label": "conflict marker"},
    {"pattern": r"^>>>>>>>", "label": "conflict marker"},
    {"pattern": r"^\|\|\|\|\|\|\|", "label": "conflict marker"},
]

# Secret-shaped filenames, shipped on by default. `forbidden_paths` is project
# policy, it appears in neither shipped config, and most repos never write any —
# so the shipped state of this guard was *zero rules*, which cannot produce a
# hit, which rendered as an affirmative "no forbidden paths" on every run. A
# `.env` and an `id_rsa` went through the review op with the file list called
# clean (#693).
#
# Three answers were available and this is the one chosen. Refusing to render a
# verdict while unconfigured is `radar`'s precedent, but `radar` is opt-in
# machinery and `git-diff` is the op you run before every commit: a refusal on
# every unconfigured repo makes it annoying enough to stop using, and an
# abandoned checker is worse than the defect. Disclosing "guard not configured"
# is one line of permanent disclaimer that resolves nothing and flags nothing.
# Shipping defaults is the only one of the three that makes the *shipped* state
# a state that can fail — and it is the state nearly every user is in.
#
# Chosen to be safe on files projects commit on purpose: `.env.example` and its
# siblings are excluded by name, and `id_rsa.pub` is a public key, so only the
# private halves match. A default that cries wolf gets configured away, which
# puts the user back in the always-passing state by a longer route.
DEFAULT_FORBIDDEN_PATHS = [
    {"pattern": r"(^|/)\.env(\.(?!example|sample|template|dist|defaults)[^/]+)*$",
     "reason": "secret-shaped filename — .env files carry credentials"},
    {"pattern": r"(^|/)id_(rsa|dsa|ecdsa|ed25519)$",
     "reason": "secret-shaped filename — private SSH key"},
    {"pattern": r"\.(pem|pfx|p12|jks|keystore|key)$",
     "reason": "secret-shaped filename — private key or keystore"},
    {"pattern": r"(^|/)\.(npmrc|pypirc|netrc)$",
     "reason": "secret-shaped filename — registry or host credentials"},
    {"pattern": r"(^|/)credentials(\.json)?$",
     "reason": "secret-shaped filename — credential file"},
    {"pattern": r"(^|/)service-account[^/]*\.json$",
     "reason": "secret-shaped filename — service-account key"},
    {"pattern": r"(^|/)\.aws/",
     "reason": "secret-shaped path — AWS profile directory"},
]


def _json_env(key: str) -> tuple[list, str]:
    """Read a JSON-list config value from SUPERTOOL_<KEY> — three states, not two.

    Returns `(rules, why_not_loaded)`. An empty `why` means there was nothing to
    load, which is an answer. A non-empty `why` means a value was configured and
    could not be used, which is a finding — and the caller must not let the
    resulting zero rules render as zero hits (#693).

    Absent and malformed both used to yield `[]`. A typo in `.supertool.json`
    therefore disabled a guard silently, and the run it disabled still printed
    the affirmative clean verdict.
    """
    raw = os.environ.get(key, "")
    if not raw.strip():
        return [], ""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        return [], f"not valid JSON ({str(e).split(chr(58))[0]})"
    if not isinstance(data, list):
        return [], f"parsed as {type(data).__name__}, not a list of rules"
    return data, ""


def _join(items: list[str]) -> str:
    """"a", "a or b", "a, b, or c" — the verdict names what actually ran."""
    if len(items) < 2:
        return items[0] if items else ""
    head = ", ".join(items[:-1])
    return f"{head}{',' if len(items) > 2 else ''} or {items[-1]}"


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
                    hits.append(f"{cur_path}:{new_line}  {label}  {_mark('→')}  {content.strip()[:80]}")
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
                out.append(f"{p}  {_mark('—')}  {rule.get('reason', 'forbidden path')}")
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
                out.append(f"{path}  {_mark('—')}  no test ({expected})")
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
    # The #308 copy this used to carry inline. _git_common owns the one
    # definition now, the way _proc owns the one liveness probe (#429): a
    # duplicate is what lets the two drift, and this one is the reason the
    # other eleven git presets looked like they had a reason not to bother.
    use_utf8_stdout()
    # `full` is an opt-in trailing flag (like read:PATH:full): it appends the
    # raw +/- hunks below the review summary. Strip it from the positional args
    # first so it never collides with staged/branch/PATH dispatch — it works as
    # the last token in every mode (git-diff:full, git-diff:PATH:full,
    # git-diff:staged:full, git-diff:branch:BASE:full).
    positional = list(sys.argv[1:])
    full = bool(positional) and positional[-1] == "full"
    if full:
        positional.pop()
    arg1 = positional[0] if len(positional) > 0 else ""
    arg2 = positional[1] if len(positional) > 1 else ""

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
        # Guard: a path absent or untracked in THIS repo produces an empty diff,
        # which would print "No changes." — indistinguishable from a clean file.
        # Surface it as an explicit miss so a wrong-CWD invocation is obvious
        # instead of silently reading as "nothing changed".
        if not _git(["ls-files", "--", arg1]).stdout.strip():
            root = _git(["rev-parse", "--show-toplevel"]).stdout.strip()
            print("# git-diff (path)")
            print(f"Repo: {root}")
            if not os.path.exists(arg1):
                print(f"{_mark('⚠')} {arg1!r} not found under {os.getcwd()} — wrong CWD?")
                return 1
            print(f"{_mark('⚠')} {arg1!r} is untracked (not in git).")
            return 0
        mode, scope, diff_args = "path", f"working vs HEAD — {arg1}", ["HEAD", "--", arg1]
    else:
        mode, scope, diff_args = "working", "working tree vs HEAD", ["HEAD"]

    print(f"# git-diff ({mode})")
    print(f"Repo: {_git(['rev-parse', '--show-toplevel']).stdout.strip()}")
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
            print(f"  {_mark('…')} truncated at {MAX_FILES} files")
            break

    # Policy from .supertool.json (env), on top of the shipped defaults. Each
    # read carries why it did not load, so a broken value is a finding rather
    # than silently zero rules.
    red_flags_extra, red_why = _json_env("SUPERTOOL_RED_FLAGS_EXTRA")
    forbidden_extra, forbidden_why = _json_env("SUPERTOOL_FORBIDDEN_PATHS")
    pairing, pairing_why = _json_env("SUPERTOOL_TEST_PAIRING")
    hints_cfg, hints_why = _json_env("SUPERTOOL_HINTS")
    red_flags = DEFAULT_RED_FLAGS + red_flags_extra
    forbidden = DEFAULT_FORBIDDEN_PATHS + forbidden_extra
    unloaded = [(k, w) for k, w in (
        ("SUPERTOOL_RED_FLAGS_EXTRA", red_why),
        ("SUPERTOOL_FORBIDDEN_PATHS", forbidden_why),
        ("SUPERTOOL_TEST_PAIRING", pairing_why),
        ("SUPERTOOL_HINTS", hints_why),
    ) if w]

    paths = [p for _, p in changed]

    forbidden_hits = _check_forbidden(paths, forbidden)
    if forbidden_hits:
        print(f"\n## {_mark('⚠')} Forbidden paths ({len(forbidden_hits)})")
        for h in forbidden_hits:
            print(f"  {h}")

    flag_hits = _scan_red_flags(diff_args, red_flags)
    if flag_hits:
        print(f"\n## {_mark('⚠')} Red flags in added lines ({len(flag_hits)})")
        for h in flag_hits[:MAX_FLAGS]:
            print(f"  {h}")
        if len(flag_hits) > MAX_FLAGS:
            print(f"  {_mark('…')} {len(flag_hits) - MAX_FLAGS} more")

    pairing_hits = _check_test_pairing(changed, pairing)
    if pairing_hits:
        print(f"\n## {_mark('⚠')} New source without a test ({len(pairing_hits)})")
        for h in pairing_hits:
            print(f"  {h}")

    hint_msgs = _path_hints(changed, hints_cfg)
    if hint_msgs:
        print("\n## Next")
        for m in hint_msgs:
            print(f"  {m}")

    if unloaded:
        print(f"\n## {_mark('⚠')} Policy not loaded ({len(unloaded)})")
        for key, why in unloaded:
            print(f"  {key}  {_mark('—')}  {why} {_mark('—')} "
                  f"its rules were NOT applied")

    if not (forbidden_hits or flag_hits or pairing_hits):
        # Name only the checks that ran. `_check_test_pairing` returns [] both
        # for "every added file has its test" and for "there were no rules to
        # run", and this line used to claim the first on behalf of the second.
        ran = ["red flags", "forbidden paths"]
        if pairing:
            ran.append("missing tests")
        print(f"\n{_mark('✓')} No {_join(ran)}.")

    if full:
        patch = _git(["diff"] + diff_args)
        if patch.returncode == 0 and patch.stdout.strip():
            print("\n## Patch")
            print(patch.stdout.rstrip("\n"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
