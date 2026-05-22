#!/usr/bin/env bash
# Install the cursor-witness extension into Cursor (and/or VSCode).
#
# Idempotent — safe to re-run. Compiles TypeScript, symlinks into the editor's
# extensions directory, and prints the next steps to wire supertool.
#
# Usage:
#   bash notifiers/cursor-witness/install.sh             # cursor (default)
#   bash notifiers/cursor-witness/install.sh --vscode    # also install in VSCode
#   bash notifiers/cursor-witness/install.sh --vscode-only

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_DIR="$SCRIPT_DIR/extension"
TARGET_NAME="digital-process-tools.cursor-witness-0.1.0"

INSTALL_CURSOR=1
INSTALL_VSCODE=0
for arg in "$@"; do
  case "$arg" in
    --vscode)      INSTALL_VSCODE=1 ;;
    --vscode-only) INSTALL_VSCODE=1; INSTALL_CURSOR=0 ;;
    -h|--help)     sed -n '2,12p' "$0"; exit 0 ;;
    *)             echo "unknown arg: $arg"; exit 2 ;;
  esac
done

# 1. Sanity: node + npm + the extension folder
command -v node >/dev/null || { echo "ERROR: node not found. Install Node ≥18."; exit 1; }
command -v npm  >/dev/null || { echo "ERROR: npm not found."; exit 1; }
[[ -d "$EXT_DIR" ]] || { echo "ERROR: extension folder missing: $EXT_DIR"; exit 1; }

NODE_MAJOR=$(node -v | sed -E 's/^v([0-9]+).*/\1/')
if (( NODE_MAJOR < 18 )); then
  echo "ERROR: Node $NODE_MAJOR detected — need ≥ 18."
  exit 1
fi

# 2. Build
echo "→ npm install"
(cd "$EXT_DIR" && npm install --silent)
echo "→ tsc compile"
(cd "$EXT_DIR" && npm run --silent compile)
[[ -f "$EXT_DIR/out/extension.js" ]] || { echo "ERROR: compile produced no out/extension.js"; exit 1; }

# 3. Symlink into editor extensions dir(s)
link_into() {
  local editor_name="$1" dir="$2"
  mkdir -p "$dir"
  local target="$dir/$TARGET_NAME"
  # Remove any prior install (real folder or stale symlink)
  if [[ -L "$target" || -d "$target" ]]; then
    rm -rf "$target"
  fi
  ln -s "$EXT_DIR" "$target"
  echo "  ✓ $editor_name: $target → $EXT_DIR"
}

echo "→ symlinking"
(( INSTALL_CURSOR )) && link_into "Cursor"  "$HOME/.cursor/extensions"
(( INSTALL_VSCODE )) && link_into "VSCode"  "$HOME/.vscode/extensions"

# 4. Next steps
cat <<EOF

✅ cursor-witness installed.

Next steps:

1. Reload your editor:
     Cmd+Shift+P  →  Developer: Reload Window
   Status bar should show:  👁  Max: idle

2. Wire the notifier in your project's .supertool.json:

   "notifiers": {
     "cursor-witness": {
       "cmd": "python3 $SCRIPT_DIR/notify.py {op} {file} {line} {line_end} {before_file}",
       "match": "*",
       "hooks_into": [
         "edit", "replace", "replace_lines", "paste", "vim",
         "read", "around_line", "between", "map"
       ]
     }
   }

3. Run a supertool op against a file in your workspace — the editor should
   open it. Edits show as side-by-side diffs; reads highlight the range.

Settings (Cmd+,  →  search "cursorWitness"):
  • openOnRead (default true) — set false for edit-only

Docs: $SCRIPT_DIR/../../docs/cursor-witness.md
EOF
