# worktree

Provision (or undo provisioning of) a git worktree's gitignored local state from the primary checkout, driven by the project's own config — closes [#532](https://github.com/Digital-Process-Tools/claude-supertool/issues/532).

## Why

A fresh `git worktree add` checks out tracked files only. Anything gitignored and needed to run the project's test suite — a vendored binary, a machine-specific DB config, a generated asset cache — is simply absent, and each missing piece tends to fail in its own misleading way: a memory exhaustion that reads as a runaway test, an empty exception that reads as "this test is broken", rather than "your environment is incomplete."

## Requires

`git` installed and a git repository with at least one worktree beyond the primary checkout. No auth, no tokens.

## Config

Nothing project-specific lives in this preset — everything comes from `ops.worktree.setup` in the project's `.supertool.json`:

```json
{
  "ops": {
    "worktree": {
      "setup": {
        "link": ["vendor/libs"],
        "copy": ["conf/dev.ini", "assets/dev-cache/"],
        "exclude": ["vendor/libs"]
      }
    }
  }
}
```

* **`link`** — symlinked from the primary checkout. For large, immutable, shared artefacts only. A write through a symlink is a write to the primary checkout, so nothing environment-specific or mutable belongs here.
* **`copy`** — copied, never linked. For anything machine/environment-specific, or that the worktree might rewrite. The copy is genuinely independent: mutating it never touches the primary checkout's own file.
* **`exclude`** — appended to a worktree-**private** exclude file, so a symlinked artefact showing up as untracked (`?? vendor/libs`) can never be swept into a commit by `git add -A` / `git add .`. This is deliberately **not** `.git/info/exclude` — git shares that file across every worktree of a repository, so appending there from one worktree would silently change what every sibling worktree's `git status` treats as untracked. The mechanism instead is `extensions.worktreeConfig` + a private `core.excludesFile`, pointing at a file under that worktree's own private git directory (`.git/worktrees/<name>/worktree-setup/exclude`).

Getting `link` and `copy` backwards is the hazard this whole preset exists to close: a `link`-declared path that the worktree mutates corrupts the *primary checkout*, silently, from inside a worktree that was supposed to be disposable.

**Every entry is validated before anything touches disk.** `.supertool.json` is read from the worktree *being provisioned* — routinely a fresh checkout of someone else's branch, e.g. a PR under test — so an entry is treated as untrusted text, not the maintainer's own configuration: a newline/carriage-return is refused outright (it could otherwise forge a fake outcome line inside this op's own receipt), and an absolute path or a `..` segment is refused because it would name a source or destination outside the primary checkout / target worktree entirely. A real filesystem failure while linking/copying/excluding (permission denied, a full disk, a Windows host with no symlink privilege) is a `WARNING` and the run continues with the remaining entries — never an unhandled crash.

## Ops

| Op | Syntax | What it does |
|----|--------|--------------|
| `worktree:setup` | `worktree:setup[:PATH]` | Provisions PATH (defaults to cwd) from the primary checkout. No config declared is a clean no-op (`worktree.setup not configured`), never an error and never a guess. Every configured entry reports one of three outcomes: acted on (linked/copied/excluded), already-there (idempotent re-run), or a `WARNING` — never a hard failure — when a configured source is missing from the primary checkout, since some artefacts are legitimately regenerated on demand. A path declared in **both** `link` and `copy` is refused under both headings rather than guessed at. |
| `worktree:teardown` | `worktree:teardown[:PATH]` | Undoes exactly what the matching `setup` run recorded creating — read from a manifest kept in the target worktree's own private git directory, never the working tree, so it cannot itself be swept into a commit. A `link` entry is only removed if it is *still* a symlink resolving to the primary checkout; if the user replaced it with real content, teardown leaves it alone and says so. A file the user made by hand at an unrelated path is never touched, because nothing about it was ever recorded. |

## PATH and the cwd-containment gate

`worktree:setup`/`worktree:teardown`'s `PATH` argument is exempt from supertool's generic cwd/repo path-containment gate (`"paths": {"args": []}` in the registry) because pointing outside cwd **is** the use case — provisioning a sibling worktree from the primary checkout, or reading the primary checkout while standing inside a linked worktree, both routinely reach outside cwd by construction. In its place, the op enforces its own boundary: **PATH must resolve to a worktree of the same repository this call was invoked from** (compared via `git rev-parse --git-common-dir`), or it is refused by name before anything runs. It is not a general-purpose way to reach an arbitrary directory.

## Reproducing a CI failure locally

The worktree runs against whatever environment the `copy`'d config points at. A CI failure on one target cannot be reproduced against a worktree provisioned from a different one, and a local run that "fails the same way" may be failing for an unrelated reason entirely.

The habit that catches this: run the *unmodified* branch too (`git stash`) and compare. Byte-identical output between the stashed and unstashed run means the failure is environmental, not something your change introduced.
