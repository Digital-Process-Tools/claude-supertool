# claude-log

Inspect Claude Code session transcripts without leaving supertool. Replaces manual `ls` + `jq` spelunking through `~/.claude/projects/` JSONL files. Useful for measuring autonomous-run efficiency: spotting wasted round-trips, validating that a skill change reduced tool calls, comparing model performance across runs.

## Requires

No external CLIs or tokens. Pure stdlib Python. Reads `~/.claude/projects/<encoded-cwd>/*.jsonl`.

## Ops

| Op | Syntax | What it returns |
|----|--------|-----------------|
| `claude-log-list` | `claude-log-list[:N]` | N most recent sessions for the current project (default 10): UUID, mtime, line count, first user message excerpt |
| `claude-log-tail` | `claude-log-tail:UUID[:N]` | Last N events in compact form (default 30): `[role] TOOL name(input)` / `[result] output` / `[result/ERR] msg` |
| `claude-log-summary` | `claude-log-summary:UUID` | Full session digest: model, duration, turn counts, tool calls + errors-by-tool, tokens (input/output/cache read/cache create) + cache hit % |

## Common workflows

**Audit an autonomous run:**
```bash
./supertool 'claude-log-list:5'
# pick the UUID, then:
./supertool 'claude-log-summary:84397aff-4925-45fd-afc5-3641e28c993c'
```
Summary shows cache hit %, error counts per tool, and total turns — enough to spot whether the run was efficient or thrashing.

**Trace what happened at the end of a run:**
```bash
./supertool 'claude-log-tail:84397aff-4925-45fd-afc5-3641e28c993c:50'
```

## Configuration

No configuration needed. Windows-friendly cwd encoding (handles `\` and drive colons). Falls back to closest-prefix sibling directory when the encoded path doesn't exist.

## Authoring notes

Preset JSON: `presets/claude-log.json`. Helper scripts: `presets/claude-log/`. Always run `claude-log-list` first to get a UUID — the UUID is required by the other two ops.
