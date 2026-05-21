#!/usr/bin/env bash
# phpstan validator adapter. Emits SCHEMA.md JSON.
# Usage: phpstan.sh <file>
#
# Env vars:
#   PHPSTAN_BIN      phpstan binary (default: phpstan)
#   PHPSTAN_CONFIG   path to neon config file (optional, adds -c FILE)
#   PHPSTAN_MEMORY   PHP memory_limit (default: 1G)
#   PHPSTAN_LEVEL    analysis level (optional, adds --level N)

set -u
file="${1:-}"
if [[ -z "$file" ]]; then
  printf '{"tool":"phpstan","file":"","ok":false,"count":1,"errors":[{"line":null,"col":null,"severity":"error","code":"adapter","msg":"no file arg"}],"duration_ms":0}'
  exit 0
fi

PHPSTAN_BIN="${PHPSTAN_BIN:-phpstan}"
PHPSTAN_MEMORY="${PHPSTAN_MEMORY:-1G}"

config_flag=()
if [[ -n "${PHPSTAN_CONFIG:-}" ]]; then
  config_flag=(-c "$PHPSTAN_CONFIG")
fi

level_flag=()
if [[ -n "${PHPSTAN_LEVEL:-}" ]]; then
  level_flag=(--level "$PHPSTAN_LEVEL")
fi

now_ms() { python3 -c 'import time;print(int(time.time()*1000))'; }
raw="/tmp/phpstan-$$.json"
trap 'rm -f "$raw"' EXIT

start=$(now_ms)
php -d memory_limit="$PHPSTAN_MEMORY" "$PHPSTAN_BIN" analyse \
  ${config_flag[@]+"${config_flag[@]}"} \
  ${level_flag[@]+"${level_flag[@]}"} \
  --no-progress --error-format=json "$file" > "$raw" 2>/dev/null || true
end=$(now_ms)
dur=$((end - start))

python3 - "$file" "$raw" "$dur" <<'PY'
import json, sys, pathlib

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
raw = pathlib.Path(raw_path).read_text() if pathlib.Path(raw_path).exists() else ""
try:
    data = json.loads(raw) if raw.strip() else {"totals":{"file_errors":0},"files":{}}
except json.JSONDecodeError:
    print(json.dumps({"tool":"phpstan","file":file,"ok":False,"count":1,
        "errors":[{"line":None,"col":None,"severity":"error","code":"adapter","msg":"phpstan output not json"}],
        "duration_ms":dur}))
    sys.exit(0)

count = int(data.get("totals", {}).get("file_errors", 0))
errors = []
for fdata in data.get("files", {}).values():
    for m in fdata.get("messages", []):
        line = m.get("line")
        errors.append({
            "line": line,
            "col": None,
            "severity": "error",
            "code": m.get("identifier"),
            "msg": m.get("message", ""),
            "source_context": source_context(file, line),
        })
print(json.dumps({
    "tool": "phpstan",
    "file": file,
    "ok": count == 0,
    "count": count,
    "errors": errors,
    "duration_ms": dur,
}))
PY
exit 0
