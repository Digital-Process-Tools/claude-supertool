# Cursor Witness

Watch your supertool agent work in real time. When supertool edits a file in your project, this extension opens it in Cursor and jumps to the changed line.

The agent isn't an opaque process anymore — it's a teammate whose work surfaces in your editor as it happens.

## Install

From source (no marketplace yet):

```bash
cd notifiers/cursor-witness/extension
npm install
npm run compile
# Then: VSCode/Cursor → Extensions → "Install from VSIX..." or symlink to extensions dir
```

## Wire supertool

Add to your project's `.supertool.json`:

```json
{
  "notifiers": {
    "cursor-witness": {
      "cmd": "python3 {supertool_dir}/notifiers/cursor-witness/notify.py {op} {file} {line}",
      "match": "*",
      "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"]
    }
  }
}
```

Add `read`, `grep`, `glob` to `hooks_into` if you also want to follow read ops (noisy).

## Settings

- `cursorWitness.socketPath` — override the auto-derived socket path
- `cursorWitness.openOnRead` — focus files on read ops (default: `true`; set `false` for edit-only)

## How it works

```
supertool edit:foo.php
   │
   ▼
notifier (fire-and-forget)
   │  writes JSON event to UDS:
   │  {"op":"edit","file":"/abs/foo.php","line":42,...}
   ▼
/tmp/supertool-witness-<sha1(cwd)[:12]>.sock
   │
   ▼
Cursor extension (listening)
   │
   ▼
vscode.window.showTextDocument(uri, {selection: line})
```

The notifier doesn't block — if the extension isn't running, the socket isn't there, the notifier exits silently. Supertool keeps working.

## Status bar

Shows `$(eye) Max: idle` when no recent activity, or `$(eye) Max: <op> <filename>` for 3 seconds after each event.

## Troubleshooting

- **Extension doesn't react** — check the socket path matches: derive from `python3 -c "import hashlib; print(hashlib.sha1(b'$(pwd)').hexdigest()[:12])"`. Compare with what the extension logs (Output panel → Cursor Witness).
- **Socket already in use** — leftover from a previous run. Extension auto-cleans on activate, but you can `rm /tmp/supertool-witness-*.sock` to force.
- **Wrong file opens** — check `cwd` in the event JSON matches your workspace folder.
