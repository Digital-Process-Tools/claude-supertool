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
TARGET_NAME="digital-process-tools.cursor-witness-0.1.1"

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

# 3. Package as .vsix + install via editor CLI.
#
# Symlink-only installs silently fail on profile-aware Cursor builds (≥3.5.x)
# because the authoritative installed-extensions registry moved to
# ~/Library/Application Support/Cursor/User/profiles/<hash>/extensions.json
# and Cursor no longer scans ~/.cursor/extensions/ for unregistered folders.
# Going through the CLI lets the editor write the correct registry entry
# for whichever profile/path layout it uses. Issue #136.
echo "→ vsce package"
# Use npx so dev doesn't have to install vsce globally. --no-dependencies skips
# the (very slow) marketplace dependency audit — we're packaging local code.
VSIX_PATH="$EXT_DIR/$TARGET_NAME.vsix"
rm -f "$VSIX_PATH" "$EXT_DIR"/*.vsix
(cd "$EXT_DIR" && npx --yes @vscode/vsce package --no-dependencies --out "$VSIX_PATH" >/dev/null)
[[ -f "$VSIX_PATH" ]] || { echo "ERROR: vsce produced no $VSIX_PATH"; exit 1; }

install_into() {
  local editor_name="$1" cli="$2"
  if ! command -v "$cli" >/dev/null; then
    echo "  ⚠ $editor_name: '$cli' CLI not on PATH — skip. Install via $editor_name → Cmd+Shift+P → 'Shell Command: Install $editor_name in PATH', then re-run."
    return 1
  fi
  # --force overwrites any prior install (symlink, real folder, older vsix).
  "$cli" --force --install-extension "$VSIX_PATH" >/dev/null
  echo "  ✓ $editor_name: $VSIX_PATH installed via $cli"
}

echo "→ installing"
(( INSTALL_CURSOR )) && install_into "Cursor" "cursor"
(( INSTALL_VSCODE )) && install_into "VSCode" "code"

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
