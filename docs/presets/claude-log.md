# claude-log

Inspect Claude Code session transcripts without leaving supertool. Replaces manual `ls` + `jq` spelunking through `~/.claude/projects/` JSONL files. Useful for measuring autonomous-run efficiency: spotting wasted round-trips, validating that a skill change reduced tool calls, comparing model performance across runs.

## Requires

No external CLIs or tokens. Pure stdlib Python. Reads `~/.claude/projects/<encoded-cwd>/*.jsonl`.

## Ops

| Op | Syntax | What it returns |
|----|--------|-----------------|
| `claude-log-list` | `claude-log-list[:N][:raw]` | N most recent sessions for the current project (default 10): UUID, mtime, line count, first user message excerpt |
| `claude-log-tail` | `claude-log-tail:UUID[:N][:raw]` | Last N events in compact form (default 30): `[role] TOOL name(input)` / `[result] output` / `[result/ERR] msg` |
| `claude-log-summary` | `claude-log-summary:UUID[:raw]` | Full session digest: model, duration, turn counts, tool calls + errors-by-tool, tokens (input/output/cache read/cache create) + cache hit % |

## Redaction (#760)

All three ops **redact values matching known secret patterns by default**, and say so. A transcript is replayed into the current context when you read it, and onward to the provider — so a key that was fine to type last week gets re-transmitted this week as a side effect of asking what you ran.

Each match is replaced in place by a labelled marker, and a header line names the count and the escape hatch:

```
Session: 84397aff-…
Total events: 412, showing last 30
[!] 2 values matched known secret patterns and were replaced by a labelled marker below.
    Detection is pattern-based, so it can miss secrets it has no pattern for — this is
    not a guarantee the output is clean. Re-run with :raw to see them verbatim.

[assistant] TOOL Bash: {"command": "curl -H 'Authorization: Bearer [REDACTED:github-token]' …"}
```

Append `:raw` in any argument position for the pre-#760 verbatim output:

```bash
./supertool 'claude-log-tail:84397aff-4925-45fd-afc5-3641e28c993c:50:raw'
```

**What it looks for** — vendor prefixes (`sk-ant-`, `sk-proj-`, `ghp_`/`gho_`/`ghu_`, `github_pat_`, `glpat-`, `AKIA`/`ASIA`, `xoxb-`, `AIza`, `sk_live_`, `npm_`, `pypi-`, `hf_`, JWTs), positional shapes (`Bearer <token>`, `Authorization: Basic <blob>`, `user:pass@host` URLs, `-----BEGIN … PRIVATE KEY-----` blocks), and `SECRET_NAME=value` assignments whose value looks like a token rather than an English word.

**What it does not do** — score entropy. Session UUIDs, commit SHAs and base64 payloads are high-entropy and are exactly what these ops exist to show; no entropy heuristic can tell them from a key.

**What it does not promise** — that the output is clean. Pattern matching has false negatives, and the disclosure line is worded to say so. Treat `:raw` output, and redacted output, as transcript content rather than as sanitised content.

Rules live in `presets/_secrets.py`. `_common.trunc` deliberately does not redact — callers redact first and truncate second, because truncating first can leave the first half of a key on screen.

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
