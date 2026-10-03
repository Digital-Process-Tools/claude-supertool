# gitleaks — post-edit secret scanning

Requires `gitleaks` on PATH (`brew install gitleaks`, or the release binary).

```json
"gitleaks": {
  "cmd": "{python} {supertool_dir}/validators/gitleaks/gitleaks.py {file}",
  "hooks_into": ["edit", "replace", "replace_lines", "paste", "append", "vim"],
  "rollback_on_fail": false,
  "timeout": 60
}
```

No `match`, deliberately: a credential is not a property of a file type, and
the languages this repo does not have a validator for are exactly the ones
where a `.env`-shaped file would slip through.

## The finding never carries the value

This is the whole design, and it has three layers because gitleaks makes the
naive version dangerous: its JSON report holds `Secret` and `Match` in
cleartext, and `--report-path <file>` is the only way to get JSON out of it. So
the obvious adapter writes the credential to disk before it decides not to
print it.

| layer | what it stops |
|---|---|
| `--redact` on the command line | the value never reaches the report file at all |
| report written under a `mkdtemp` at mode `0700`, removed in a `finally` | a report surviving a crash, or readable by a co-tenant |
| no `source_context` on the error | the receipt printing the secret's own source line |

The third is the one that needs saying out loud, because every sibling adapter
in this directory attaches `source_context` and it is the right default
everywhere else. Here the source line *is* the secret, and a validator receipt
is simultaneously a terminal, a scrollback, a CI log and an agent transcript —
four places a credential should not be written down.

What the finding does carry is the rule id, the line, the column and gitleaks'
own description, which is everything needed to act. The wording follows
`presets/_secrets.py:disclosure`: detection is pattern-based, so a clean result
is the absence of a match and never a guarantee.

`tests/test_validators_gitleaks_668.py` asserts this against the whole
serialised payload rather than against named fields, so a future `metrics`
block or a longer message cannot reintroduce it quietly.

## `rollback_on_fail` is `false`, against the issue's suggestion

[#668](https://github.com/Digital-Process-Tools/claude-supertool/issues/668)
argues rollback is defensible here and nowhere else on its list, since the
alternative is the value sitting on disk waiting to be committed. Three reasons
it is not taken:

1. **Reverting does not unpublish anything.** The value reached the edit from
   somewhere — a payload file, a paste, an agent's context window, shell
   history — and every one of those still holds it afterwards. Rollback buys
   the appearance of containment, not containment.
2. **It destroys the rest of the edit.** A `batch:` that fixes three things and
   happens to touch a token-shaped literal loses all three, and the author's
   next move is to re-apply it.
3. **The false-positive rate makes it dangerous.** See the survey below: every
   finding in this repository today is a fixture. Rollback would have reverted
   real edits over fake keys, which is the fastest available route to the
   validator being switched off — and a scanner nobody runs protects nothing.

The finding stays fully loud: `ok: false`, severity `error`, one row per hit.
Only the destructive auto-revert is off. If a repo wants the harder behaviour
it can set `rollback_on_fail: true` in its own entry, knowing the above.

## The false-positive survey

`gitleaks detect --no-git --redact` over both repositories, 2026-08-07,
gitleaks 8.30.1, default ruleset:

| repository | findings | real | notes |
|---|---|---|---|
| `claude-supertool` | 12 | 0 | 8 in `tests/`, 4 in `tests/__pycache__/` |
| `claude-remember` | 0 | 0 | |

Every one of the eight is a deliberately fake credential in a test that
exercises supertool's *own* secret redaction — `test_secret_redaction_760.py`,
`test_secret_files_691.py`, `test_hashnode.py`, `test_security_devto.py`,
`test_security_claude_log.py`. The four `__pycache__` hits are the same
literals compiled into untracked build artifacts.

That is the normal case, not the edge case, and it is why this validator is
registered in `.supertool.example.json` and not switched on in this
repository's own `.supertool.json` yet: adopting it here means annotating those
fixture lines first, which is its own change.

## How a fixture is silenced — and how it is not

**Per line, in the diff.** gitleaks' own trailing comment:

```python
TOKEN = "ghp_notarealtokenatall"  # gitleaks:allow
```

One line, visible to a reviewer, and it says nothing about the next line. A
`.gitleaksignore` of explicit fingerprints is the bulk equivalent and is also
reviewable.

**Not by directory.** The obvious lever — an `exclude` glob over `tests/` or
`docs/` — is refused, and `test_registered_without_rollback_and_without_a_blanket_exclude`
pins its absence. It turns the check off invisibly and permanently for exactly
the paths most likely to grow a real credential next, and nobody ever notices,
because a scanner that has been silenced looks identical to a scanner finding
nothing. That is the same absence-read-as-a-pass this directory exists to
avoid, arriving through the config file instead of through the adapter.

## Absent binary

`skipped`, and the reason says *the file was NOT scanned* rather than merely
naming a missing binary — "gitleaks not found" reads as a tooling note, and the
sentence a reader needs is the one telling them what they do not know. In CI,
set `SUPERTOOL_REQUIRE_VALIDATORS=gitleaks` and the same absence becomes a loud
`adapter` error naming the variable. See `docs/validators.md`, "The tool is not
installed".

## Scope

`--no-git`: this scans the working file, which is what a post-edit validator
wants. History scanning is a different job — it answers "has a secret ever been
committed", takes as long as the repository is old, and belongs in an op or a
CI step rather than on every edit.
