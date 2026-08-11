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
| `claude-log-cost` | `claude-log-cost[:N or :UUID][:raw]` | Bytes of tool-result text, per tool and per supertool op, plus size distribution, repeat reads, errors and empties. Default: 10 most recent sessions of this project |

## `claude-log-cost` — what a tool result costs (#1252)

`claude-log-summary` counts tool calls. `claude-log-cost` weighs them, because
the number that decides whether a result-trimming hook is worth building is
bytes, not calls.

```bash
./supertool 'claude-log-cost:40'
```

Five blocks, all raw numbers:

- **By tool** — calls, bytes, share of total, p50/p95/max. Nearest-rank
  percentiles, not interpolated: every figure is the size of a result that
  really happened.
- **By supertool op** — the same columns, split on the `--- op ---` markers the
  render already prints. Nested sections (`batch:@-` printing one `edit:` block
  per sub-op) get their own row.
- **Concentration** — bytes in the ten largest single results, as a share. A
  heavy skew means a size cap captures most of the win and nothing clever is
  needed.
- **Repeat reads** — paths read more than once, and the subset whose re-read
  returned byte-identical content. Stated as a count **and** a share of total
  bytes, because those two disagree and only the second one justifies work.
- **Failures and empties** — error results and zero-byte results with their
  byte cost.

### What it declines to claim

- A session that cannot be parsed is listed by name under `Skipped:` with a
  reason. It is never dropped from the totals silently.
- A `tool_result` whose `tool_use_id` matches no `tool_use` is counted under
  tool `?` and disclosed, not discarded.
- Non-text result blocks (an image's base64 payload) are excluded from the byte
  total, because those bytes are not comparable to text bytes. What is reported
  for them is a count per kind (`image x2`), not a size — their byte cost is not
  measured at all, and the report says so rather than implying it is in there.
- A section marker is only believed when the command line it came from named
  that op. Agents write `echo "--- branch ---"` as a shell separator, and
  measured over five live sessions that invented nine ops which do not exist
  and moved real bytes onto them.
- Results from commands that never invoked supertool are reported as a count
  and a byte total under "not attributable to an op", never spread across the
  ops that did run.

Reads are keyed by one path whichever route produced them: `Read` passes an
absolute `file_path`, a `read:` op is usually relative, and the session cwd —
recorded on every transcript event — joins them. Unjoined, one file lands under
two keys and the headline repeat-read figure reads low.

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

No configuration needed. Windows-friendly cwd encoding (handles `\` and drive colons).

**Which store answers, in three states (#1317).** `claude-log-list` and `claude-log-cost` resolve the cwd to a transcript store **upward only**:

| State | What you see |
| --- | --- |
| `direct` | the cwd has its own store — a plain `Project:` header |
| `ancestor` | the cwd is inside a project that has one — the header carries `Source: ancestor store — no sessions recorded for X; showing Y, which is not the same directory` |
| `missing` | nothing at or above the cwd has a store — `no sessions recorded for this directory`, naming the path it looked for and how many stores exist, exit 1 |

A **sibling** store is never used. Until #1317 the fallback picked the store whose encoded name shared the longest common prefix, which from `~/Documents/st-wt/1317` returned `st-wt/1024` and rendered another worktree's sessions as the caller's. Working from a fresh worktree, `missing` is the expected answer, not a fault.

`claude-log-tail` and `claude-log-summary` take a UUID and are unaffected: a session is found by name across every store.

**One ambiguity it cannot close.** The store name is Claude Code's, and it maps `/`, `-` and `:` all to `-`: `/Users/foo/bar` and `/Users/foo-bar` produce the same directory name, and nothing on disk says which one wrote it. A `direct` hit is ambiguous in exactly the same way as an `ancestor` one, so this is a property of the naming scheme rather than of the resolution order. Pinned by `tests/test_claude_log_project_dir_1317.py::TestKnownLimit`.

## Authoring notes

Preset JSON: `presets/claude-log.json`. Helper scripts: `presets/claude-log/`. `claude-log-tail` and `claude-log-summary` require a UUID, so run `claude-log-list` first to get one. `claude-log-cost` does not: with no argument it measures the ten most recent sessions of the current project, and a UUID narrows it to one.
