#!/usr/bin/env bash
# phpmd validator adapter. Emits SCHEMA.md JSON.
# Usage: phpmd.sh <file>
#
# Env vars:
#   PHPMD_BIN       phpmd binary (default: phpmd)
#   PHPMD_RULESETS  comma-separated rulesets (default: cleancode,codesize,controversial,design,naming,unusedcode)
#   PHPMD_FORMAT    output format (default: text)
#   PHPMD_EXCLUDE   value for --exclude flag (optional)

set -u
file="${1:-}"
if [[ -z "$file" ]]; then
  printf '{"tool":"phpmd","file":"","ok":false,"count":1,"errors":[{"line":null,"col":null,"severity":"error","code":"adapter","msg":"no file arg"}],"duration_ms":0}'
  exit 0
fi

PHPMD_BIN="${PHPMD_BIN:-phpmd}"
PHPMD_RULESETS="${PHPMD_RULESETS:-cleancode,codesize,controversial,design,naming,unusedcode}"
PHPMD_FORMAT="${PHPMD_FORMAT:-text}"

exclude_flag=()
if [[ -n "${PHPMD_EXCLUDE:-}" ]]; then
  exclude_flag=(--exclude "$PHPMD_EXCLUDE")
fi

now_ms() { python3 -c 'import time;print(int(time.time()*1000))'; }
raw="/tmp/phpmd-$$.txt"
trap 'rm -f "$raw"' EXIT

start=$(now_ms)
"$PHPMD_BIN" "$file" "$PHPMD_FORMAT" "$PHPMD_RULESETS" \
  --suffixes php,phtml \
  ${exclude_flag[@]+"${exclude_flag[@]}"} > "$raw" 2>/dev/null || true
end=$(now_ms)
dur=$((end - start))

python3 - "$file" "$raw" "$dur" <<'PY'
import json, re, sys, pathlib

def source_context(file_path, error_line):
    """Return 5-line context centered on error_line (1-based). Returns [] if line is None."""
    if error_line is None:
        return []
    try:
        lines = pathlib.Path(file_path).read_text(errors="replace").splitlines()
    except OSError:
        return []
    ctx = []
    for offset in range(-2, 3):
        ln = error_line + offset
        if 1 <= ln <= len(lines):
            prefix = f"{ln}→" if offset == 0 else f"{ln}:"
            ctx.append(f"{prefix} {lines[ln - 1]}")
    return ctx

file, raw_path, dur = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    raw = pathlib.Path(raw_path).read_text(errors="replace") if pathlib.Path(raw_path).exists() else ""
except OSError:
    raw = ""

# phpmd text format: "<file>:<line>	<RuleName>	<message>"
# or shorter: "<file>:<line>	<message>"
errors = []
for line in raw.splitlines():
    line = line.strip()
    if not line:
        continue
    # Match "path/to/file.php:42\tRuleName\tHuman message"
    m = re.match(r'^.+?:(\d+)\t([^\t]+)\t(.+)$', line)
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
            "source_context": source_context(file, lineno),
        })
        continue
    # Fallback: "path/to/file.php:42  message" (space-separated, no rule column)
    m2 = re.match(r'^.+?:(\d+)\s+(.+)$', line)
    if m2:
        lineno = int(m2.group(1))
        msg = m2.group(2).strip()
        errors.append({
            "line": lineno,
            "col": None,
            "severity": "warning",
            "code": None,
            "msg": msg,
            "source_context": source_context(file, lineno),
        })

count = len(errors)
print(json.dumps({
    "tool": "phpmd",
    "file": file,
    "ok": count == 0,
    "count": count,
    "errors": errors,
    "duration_ms": dur,
}))
PY
exit 0
