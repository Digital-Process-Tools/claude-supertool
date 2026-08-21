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

| Var                     | Default | Notes                                                                 |
|-------------------------|---------|-----------------------------------------------------------------------|
| `GIT_BIN`               | `git`   | Override if git is not on `PATH`                                       |
| `SUPERTOOL_GIT_TIMEOUT` | `15`    | Seconds for the **whole adapter**, not per git call. Values below `1` and anything unparseable fall back to the default, silently — this adapter's stdout is parsed as JSON, so it has no channel to complain on. Same knob every other git call in supertool reads. **Set it in either direction** — see the latency note below. |

**This budget is interactive latency you pay.** Every mutating op blocks on its
slowest validator, so on a repository whose git does not answer, an edit blocks
for **19s measured** (15s budget, plus the SIGTERM grace, plus teardown) before
the decline is printed — bounded above by the registered `timeout` of 30. It was
about 5s before #1882, because the core killed the adapter at its own budget:
5s, and a wrong answer.

So the knob is worth setting in whichever direction fits the repository. Raise
it if you want the measurement and can wait for it. **Lower it — `3` is
reasonable — if you would rather have a fast honest `NOT CHECKED` than a slow
correct one.** That second option is new: before this change the fast answer was
a fabricated `state: "clean"`, so turning the budget down bought silence rather
than speed.

A git call that outlives the budget is **not** reported as a clean working
tree. It declines with `code: "adapter"` — rendered `NOT CHECKED`, never cached,
never able to roll an edit back — and the stalled git is sent `SIGTERM` before
`SIGKILL` so it can remove its own `.git/index.lock` (#1882). Raising
`SUPERTOOL_GIT_TIMEOUT` past the registered `timeout` below makes that decline
unreachable — see **Registration**.

## Registration

Add to `.supertool.json` under `validators`:

```json
"git-status": {
  "cmd": "python3 {supertool_dir}/validators/git-status/git-status.py {file}",
  "match": "*",
  "hooks_into": ["edit", "replace", "replace_lines", "paste", "vim"],
  "rollback_on_fail": false,
  "timeout": 30
}
```

`timeout` is the **core's** budget for this adapter. It must exceed the
adapter's own (`SUPERTOOL_GIT_TIMEOUT`, default 15s) plus its 2s SIGTERM grace,
or the core kills the adapter before it can decline and you get no verdict
instead of a stated one. Both were 5 until #1882, and a tie is a race.
