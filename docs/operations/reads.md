# Reads

File reading and directory listing ops. Reach for these when you know the path and want content — a single file, a directory listing, or metadata without searching.

## Ops

| Op | Syntax | What it does |
|----|--------|--------------|
| `read` | `read:PATH` or `read:PATH:OFFSET:LIMIT` | 300 lines / 20KB cap |
| `read` (filter) | `read:PATH:OFFSET:LIMIT:grep=PATTERN` | Only show lines matching PATTERN (original line numbers preserved). Use `read:PATH:::grep=PATTERN` for defaults. |
| `head` | `head:PATH:N` | First N lines (default 20) |
| `tail` | `tail:PATH:N` | Last N lines (default 20) |
| `wc` | `wc:PATH` | Line/word/char count (like unix `wc`). Output: `LINES WORDS CHARS PATH`. |
| `stat` | `stat:PATH` | File/directory metadata: size (bytes), last modified (ISO datetime), type (file/dir). |
| `ls` | `ls:PATH` | Directory listing. Trailing `/` on subdirs. |
| `glob` | `glob:PATTERN` | `**` supported. **Auto-reads** if PATTERN is a concrete file path (no wildcards). |
| `tree` | `tree:PATH` or `tree:PATH:DEPTH` | Directory structure with depth limit (default 3). Hides dotfiles. Files listed before subdirectories. |
| `diff` | `diff:PATH1:PATH2` | Unified diff between two files. |

## Common patterns

Read three files in one round-trip — parallel where safe:

```bash
./supertool 'read:src/Module.py' 'read:src/Config.py' 'read:src/Auth.py'
```

Survey a directory then read the interesting file:

```bash
./supertool 'tree:src/app/' 'glob:src/app/**/*.py' 'read:src/app/Module.py'
```

Read only matching lines from a large file (line numbers preserved):

```bash
./supertool 'read:src/app/Config.py:::grep=DEBUG'
```

Check size before deciding to read:

```bash
./supertool 'stat:src/app/BigFile.py' 'head:src/app/BigFile.py:50'
```

## See also

- [search.md](search.md) — when you don't know the path yet (`grep`, `between`, `around`)
- [map.md](map.md) — when you want the symbol skeleton of a file/directory without reading every line
- [docs/validators.md](../validators.md) — validators run automatically on mutating ops
