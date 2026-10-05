#!/usr/bin/env python3


























from __future__ import annotations

import json
import os
import re
import sys



sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _git_common import (  
    NOT_A_REPO, _git, probe_repo, repo_label, unanswered_repo_lines,
    use_utf8_stdout,
)
import _untrusted  

MAX_FILES = 60
MAX_FLAGS = 40





_PLAIN_MARKERS = {
    "⚠": "[WARN]", "✓": "[OK]", "✗": "[FAIL]", "ℹ": "[INFO]",

    "→": "->", "—": "--", "…": "...",
}


def _plain() -> bool:
    return os.environ.get("SUPERTOOL_PLAIN", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def _mark(glyph: str) -> str:

    return _PLAIN_MARKERS.get(glyph, glyph) if _plain() else glyph



DEFAULT_RED_FLAGS = [
    {"pattern": r"\bvar_dump\s*\(", "label": "var_dump"},
    {"pattern": r"\bprint_r\s*\(", "label": "print_r"},
    {"pattern": r"\bvar_export\s*\(", "label": "var_export"},
    {"pattern": r"\bconsole\.(log|debug|warn)\s*\(", "label": "console.*"},
    {"pattern": r"\bdebugger\s*;", "label": "debugger"},



    {"pattern": r"^<<<<<<<", "label": "conflict marker"},
    {"pattern": r"^>>>>>>>", "label": "conflict marker"},
    {"pattern": r"^\|\|\|\|\|\|\|", "label": "conflict marker"},
]





























_FORBIDDEN_STEMS_SPELLED_APART_2734 = {
    "env_kept": ("example", "sample", "template", "dist", "defaults"),
    "ssh_keys": ("rsa", "dsa", "ecdsa", "ed25519"),
    "key_exts": ("pem", "pfx", "p12", "jks", "keystore", "key"),
    "rc_files": ("npmrc", "pypirc", "netrc"),
    "credentials": "credentials",
    "service_account": ("service", "account"),
    "cloud": "aws",
}
_S = _FORBIDDEN_STEMS_SPELLED_APART_2734
DEFAULT_FORBIDDEN_PATHS = [
    {"pattern": r"(^|/)\.env(\.(?!" + "|".join(_S["env_kept"]) + r")[^/]+)*$",
     "reason": "secret-shaped filename — .env files carry credentials"},
    {"pattern": r"(^|/)id_(" + "|".join(_S["ssh_keys"]) + r")$",
     "reason": "secret-shaped filename — private SSH key"},
    {"pattern": r"\.(" + "|".join(_S["key_exts"]) + r")$",
     "reason": "secret-shaped filename — private key or keystore"},
    {"pattern": r"(^|/)\.(" + "|".join(_S["rc_files"]) + r")$",
     "reason": "secret-shaped filename — registry or host credentials"},
    {"pattern": r"(^|/)" + _S["credentials"] + r"(\.json)?$",
     "reason": "secret-shaped filename — credential file"},
    {"pattern": r"(^|/)" + "-".join(_S["service_account"]) + r"[^/]*\.json$",
     "reason": "secret-shaped filename — " + "-".join(_S["service_account"]) + " key"},
    {"pattern": r"(^|/)\." + _S["cloud"] + "/",
     "reason": "secret-shaped path — AWS profile directory"},
]
del _S


def _json_rules(raw: "str | None") -> tuple[list, str]:

















    raw = raw or ""
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












    res = _git(["-c", "core.quotepath=false", "diff", "--name-status"] + diff_args)
    out: list[tuple[str, str]] = []
    if res.returncode != 0:
        return out
    for line in _untrusted.split_lines(res.stdout):
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0][0]  
        path = parts[-1]      
        out.append((status, path))
    return out


def _scan_red_flags(diff_args: list[str], patterns: list[dict]) -> list[str]:















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
    for line in _untrusted.split_lines(res.stdout):
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
                    hits.append(f"{_untrusted.flat(cur_path)}:{new_line}  {label}  "
                                f"{_mark('→')}  {_untrusted.flat(content.strip())[:80]}")
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
                out.append(f"{_untrusted.flat(p)}  {_mark('—')}  "
                           f"{rule.get('reason', 'forbidden path')}")
                break
    return out


def _check_test_pairing(changed: list[tuple[str, str]], rules: list[dict]) -> list[str]:

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
                out.append(f"{_untrusted.flat(path)}  {_mark('—')}  "
                           f"no test ({_untrusted.flat(expected)})")
            break
    return out


def _path_hints(changed: list[tuple[str, str]], rules: list[dict]) -> list[str]:

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




    use_utf8_stdout()





    positional = list(sys.argv[1:])
    full = bool(positional) and positional[-1] == "full"
    if full:
        positional.pop()
    arg1 = positional[0] if len(positional) > 0 else ""
    arg2 = positional[1] if len(positional) > 1 else ""

    inside, why = probe_repo(_git, ["rev-parse", "--is-inside-work-tree"])
    if inside is None:
        for line in unanswered_repo_lines(
                why, probe="git rev-parse --is-inside-work-tree"):
            print(line)
        return 1
    if not inside:
        print(NOT_A_REPO)
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




        if not _git(["ls-files", "--", arg1]).stdout.strip():
            print("# git-diff (path)")





            print(f"Repo: {repo_label()}")
            if not os.path.exists(arg1):
                here = _untrusted.flat(os.getcwd(), disclose_newline=True)
                print(f"{_mark('⚠')} {arg1!r} not found under {here} — wrong CWD?")
                return 1
            print(f"{_mark('⚠')} {arg1!r} is untracked (not in git).")
            return 0
        mode, scope, diff_args = "path", f"working vs HEAD — {arg1}", ["HEAD", "--", arg1]
    else:
        mode, scope, diff_args = "working", "working tree vs HEAD", ["HEAD"]

    print(f"# git-diff ({mode})")



    print(f"Repo: {repo_label()}")
    print(f"Scope: {scope}")

    changed = _changed_files(diff_args)
    if not changed:
        print("No changes.")
        return 0

    shortstat = _git(["diff", "--shortstat"] + diff_args)
    if shortstat.returncode == 0 and shortstat.stdout.strip():
        print(shortstat.stdout.strip())


    groups: dict[str, list[str]] = {}
    for status, path in changed:
        groups.setdefault(_classify(path), []).append(
            f"{status}  {_untrusted.flat(path)}")
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




    red_flags_extra, red_why = _json_rules(os.environ.get("SUPERTOOL_RED_FLAGS_EXTRA"))
    forbidden_extra, forbidden_why = _json_rules(os.environ.get("SUPERTOOL_FORBIDDEN_PATHS"))
    pairing, pairing_why = _json_rules(os.environ.get("SUPERTOOL_TEST_PAIRING"))
    hints_cfg, hints_why = _json_rules(os.environ.get("SUPERTOOL_HINTS"))
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
