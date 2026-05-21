# git-status validator

Reports the working-tree delta (lines added/removed) and file state after every mutating op.

## Behavior

- Runs `git diff --numstat` (unstaged) and `git diff --cached --numstat` (staged) against the edited file.
- Runs `git status --porcelain` to determine file state.
- Always emits `ok: true` — git status is informational, never a rollback trigger.
- If `git` is absent or the file is not inside a git repo, emits zero metrics and exits cleanly.

## Output

```json
{
  "tool": "git-status",
  "file": "src/Foo.php",
  "ok": true,
  "count": 0,
  "errors": [],
  "duration_ms": 5,
  "metrics": {
    "lines_added": 5,
    "lines_removed": 4,
    "lines_staged_added": 0,
    "lines_staged_removed": 0,
    "state": "modified"
  }
}
```

## State values

| State       | Meaning                                      |
|-------------|----------------------------------------------|
| `clean`     | No changes vs HEAD                           |
| `modified`  | Unstaged changes in working tree             |
| `staged`    | All changes are staged (index only)          |
| `untracked` | File not tracked by git                      |
| `unknown`   | Porcelain output present but not recognised  |

## Env vars

| Var       | Default | Notes                              |
|-----------|---------|------------------------------------|
| `GIT_BIN` | `git`   | Override if git is not on `PATH`   |

## Registration

Add to `.supertool.json` under `validators`:

```json
"git-status": {
  "cmd": "python3 {supertool_dir}/validators/git-status/git-status.py {file}",
  "match": "*",
  "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
  "rollback_on_fail": false,
  "timeout": 5
}
```
