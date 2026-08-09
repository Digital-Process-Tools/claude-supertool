"""One git PATH shim for the whole suite, keyed on the subcommand (#1206).

Three test modules built the same fake `git` independently, and all three asked
the same wrong question: `[ "$1" = "status" ]`. git takes its global flags
*before* the subcommand, so `git -C <path> status` and
`git --literal-pathspecs status` put `status` in `$2`. The comparison fails, the
`exec` fallthrough runs the real binary, and the shim is silently not in effect.

Silently is the whole problem. It broke visibly once only because the tests it
disabled assert a *refusal*, so a real git succeeding contradicted them. A shim
standing in for a passing expectation fails the other way: real git returns the
answer the fake would have returned, the test still passes, and it is asserting
nothing about the path it was written for -- indistinguishable, forever, from a
test that works.

So the shim describes "a git invoked with this subcommand". It skips the global
flags and reads the first argument that is not one, rather than matching the
subcommand *anywhere* in the arguments: `git status -- stash` is not a stash
call, and a shim that answered it as one would trade a fake that quietly stops
firing for one that quietly fires too often.

**Where this is still blind, stated rather than left to be found.** The list
below is git's global flags that take a *separate* argument. A future flag of
that shape which is not in it would have its value read as the subcommand, and
the shim would go back to silently not firing. The failure is the same one this
module exists for, one flag further out; there is no way to ask git for the
list at runtime, so it is a maintained constant and this paragraph is the
disclosure.
"""
from __future__ import annotations

#: git global flags whose value is a separate argv entry, not `--flag=value`.
TAKES_A_SEPARATE_ARGUMENT = (
    "-C", "-c", "--git-dir", "--work-tree", "--namespace",
    "--super-prefix", "--attr-source", "--config-env",
)

_SUBCOMMAND_FUNCTION = """_supertool_git_subcommand() {
  while [ $# -gt 0 ]; do
    case "$1" in
      %s)
        if [ $# -ge 2 ]; then shift 2; else shift; fi
        ;;
      -*) shift ;;
      *) printf '%%s' "$1"; return 0 ;;
    esac
  done
  return 0
}
""" % "|".join(TAKES_A_SEPARATE_ARGUMENT)


def dispatch_on_subcommand(subcommand: str, action: str, real_git: str) -> str:
    """`/bin/sh` body: run ACTION when git was invoked with SUBCOMMAND.

    Everything else execs REAL_GIT, so the repo under test is still built and
    read by a real binary and only the one call under examination is faked. The
    `#!/bin/sh` line is the caller's, matching the shim writers that already
    prepend it.
    """
    return (
        _SUBCOMMAND_FUNCTION
        + 'if [ "$(_supertool_git_subcommand "$@")" = "' + subcommand + '" ]; '
        + "then " + action + "; fi\n"
        + "exec " + real_git + ' "$@"\n'
    )
