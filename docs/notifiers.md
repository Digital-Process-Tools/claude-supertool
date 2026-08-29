# Notifiers — fire-and-forget observers

Notifiers are validators' read-friendly sibling. Same `hooks_into` / `match` shape, but they **observe instead of validate**:

- Fire on reads as well as writes (validators only fire on mutating ops)
- **Spawn-and-forget** — never block the parent supertool call
- No JSON receipt parsed, no rollback semantics, no output rendered
- Failures swallowed silently

The use case: tap into supertool's op stream to drive side effects — pipe events into your editor (see [cursor-witness](cursor-witness.md)), Slack, an audit log, a desktop notification, anything.

## Config

Same place as validators in `.supertool.json`:

```json
{
  "notifiers": {
    "my-observer": {
      "cmd": "python3 my-observer.py {op} {file} {line} {line_end} {before_file}",
      "match": "*",
      "hooks_into": [
        "edit", "replace", "replace_lines", "paste", "vim",
        "read", "around_line", "between", "map"
      ]
    }
  }
}
```

| Key | Meaning |
|---|---|
| `cmd` | Shell command, with substitution placeholders |
| `match` | fnmatch glob against the file path — `*` for all files |
| `hooks_into` | List of supertool op names that fire this notifier |

### Placeholders

| Token | Value | Set on |
|---|---|---|
| `{op}` | The op name (`edit`, `between`, ...) | Every fire |
| `{file}` | Absolute file path the op targeted | Every fire |
| `{line}` | Start line (1-indexed) | When the op exposes a line: `around_line`, `between` (symbol mode), `read:F:OFFSET:LIMIT` |
| `{line_end}` | End line (1-indexed inclusive) | Same as `{line}` when a range is known |
| `{before_file}` | Path to a temp file holding pre-edit content | Mutating ops only (`edit`/`replace`/`paste`/`append`/`vim`/`replace_lines`) |
| `{supertool_dir}` | Install dir of supertool | Always |

Unset placeholders render as empty strings — your notifier should treat them as optional.

## Supported ops

Mutating ops fire after the file is rewritten (post-validator). Read ops fire after the op returns its output.

| Op | Line range | Notes |
|---|---|---|
| `edit`, `replace`, `replace_lines`, `paste`, `append`, `vim` | — | `{before_file}` set; ideal for diff view |
| `around_line:FILE:LINE:N` | LINE-N to LINE+N | Computed from args |
| `between:SYMBOL:FILE` | Symbol's body | Resolved via tree-sitter |
| `between:re:START:END:FILE` | — | Regex variant, range too dynamic to precompute |
| `read:FILE:OFFSET:LIMIT` | OFFSET+1 to OFFSET+LIMIT | Computed from args — OFFSET is a skip count, so the window starts one line below it. This row said `OFFSET to OFFSET+LIMIT-1` until #1417, matching the code and neither matching the op |
| `read:FILE:START-END` | START to END | Computed from args; before #1417 the range form got no range at all |
| `read:FILE` | — | Whole file |
| `map`, `tail`, `head`, `wc`, `stat` | — | File-only focus |

Other read ops (`grep`, `glob`, `ls`) don't fire notifiers — they target multiple files / paths, and the multi-event protocol isn't wired yet. Patches welcome.

## Properties

- **Spawn cost** — one fork+exec per fire. Typical < 5ms; never blocks because the parent does not `wait()`.
- **Ordering** — notifiers fire **after** the op's output is returned. Observers see what supertool returned, not work-in-progress.
- **Concurrency** — multiple notifiers fire serially within `_run_notifiers`, each as its own detached subprocess. Inside a single notifier, you own concurrency.
- **Idempotence** — notifier scripts should be safe to fire on every op. The same edit may re-fire if the user re-runs the supertool call.
- **Failure isolation** — `subprocess.Popen` errors (`OSError`, `ValueError`) are caught. A broken notifier never raises into supertool.

## When to use a notifier vs a validator

| | Validator | Notifier |
|---|---|---|
| Decides if the edit is OK | ✓ | ✗ |
| Can roll back the file | ✓ | ✗ |
| Returns a structured receipt | ✓ | ✗ |
| Blocks the op until done | ✓ | ✗ |
| Fires on reads | ✗ | ✓ |
| Sees pre-edit content | rollback flow | `{before_file}` |
| Fan-out side effects (editor, Slack, log) | wrong tool | ✓ |

If your hook's answer changes whether the edit lands → validator. If it just observes → notifier.

## Examples

### Slack ping when files in `src/auth/` change

```json
"notifiers": {
  "auth-watch": {
    "cmd": "bash -c 'curl -s -X POST -H content-type:application/json -d \"{\\\"text\\\":\\\":wrench: {op} on {file}\\\"}\" $SLACK_WEBHOOK'",
    "match": "src/auth/**",
    "hooks_into": ["edit", "replace", "paste", "vim"]
  }
}
```

Fire-and-forget: no return path, no `ts`, none of the outbound safety gating a deliberate post runs under. For a call-it-on-purpose post whose response you need back (a thread reply, a `ts` to edit later), use the `slack_publish` op instead — see [docs/presets/slack.md](presets/slack.md).

This `curl` is a raw notifier body, not a call through `presets/_publish_safety.py` — it does not get `slack_publish`'s authorship-disclosure marker (#2042) or its confirmation gate. A message posted this way carries no indication a machine sent it. If that matters for your channel, add the marker to the notifier's own JSON body, or route the post through `slack_publish` instead.

### Append to a per-session audit log

```json
"notifiers": {
  "audit": {
    "cmd": "bash -c 'echo \"$(date -u +%FT%TZ) {op} {file}\" >> /tmp/supertool-audit.log'",
    "match": "*",
    "hooks_into": ["edit", "replace", "paste", "vim", "rename"]
  }
}
```

### Cursor Witness — see Max work in your editor

The flagship notifier consumer. Ships with supertool. See [cursor-witness.md](cursor-witness.md).
