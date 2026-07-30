"""Which `.py` files in this repository are *source* — the one answer, shared
by the two guards that walk the whole tree.

Both guards ask the identical question and got different answers, which is the
bug. `test_no_bare_python3_spawn.py` asks it to decide what to AST-scan;
`test_syntax_floor_478.py` asks it to decide what to compile under an old
interpreter. Neither question is about what the guard checks — it is "is this
path repository source, or is it machine state that happens to sit in the
working tree". A file being source does not depend on which rule is about to
be applied to it, so there is no per-caller option here and none should be
added: the moment this grows a `include_fixtures=` or an `extra_names=`
parameter, it has become two rules wearing one name and the copies would have
been better.

What IS caller-specific is *why the scope is right for that rule* — why a
spawn guard scans `tests/fixtures/`, why a floor guard compiles them. That
reasoning stays in each caller's docstring, where it belongs. Only the walk
moved.

The alternative was a third copy of the reasoning below. #555 is what that
costs: six near-identical `_load` helpers drifted apart until they disagreed.
This file exists because #577 is that drift already happening — the floor walk
is the #575 defect with a *narrower* exclusion set, filed two days after #575,
by which time the sibling had been fixed and the copy had not.

## The rule, in order

**Names, unconditionally.** `.git` is never reported by `git ls-files`, so a
git-derived answer alone can never exclude it. `.venv` and `venv` are in no
ignore file in this repo at all (#577: `.gitignore` line 11 mentions venvs only
inside a comment about setuptools output) — an in-repo virtualenv is untracked,
unignored, and thousands of third-party files deep, many of them legitimately
un-compilable at a 3.9 floor because they target newer syntax. `node_modules`
is the same category and was the only non-`.git` name the floor walk had.
Relying on git for these would be relying on every contributor's clone having
the same ignore file, which is not a property anyone controls.

**Then the property: git's own answer to "is this ignored?"** This is #449's
decision for supertool's own walk (`_git_ignored_dirs` in supertool.py) and
#576's for the spawn guard. It is what makes `build/` (#575) exempt without
anyone having had to anticipate `build/`, and it covers `dist/`, `htmlcov/`,
`*.egg-info/` and the next packaging tool's output before they are filed.

Asked of the *ignored* set, never the tracked set. Scanning `git ls-files`
would exempt every file that is untracked because it is being written right
now — exactly when a guard earns its keep — and would degrade to scanning
nothing wherever git is absent (source tarball, vendored copy, Docker build
without `.git`), which is indistinguishable from a clean repo in the output.
Every degraded mode here goes the other way: wider than needed, never
narrower, so the worst outcome is a false alarm someone can read rather than a
silence nobody can.

The no-git fallback below is a denylist, which #564 and #555 are both against.
That objection is about a list deciding what gets *scanned* — where the next
real instance hides in the gap. It holds far less for one that can only fire
when git is unavailable and can only ever over-report: its worst outcome is a
false alarm on an unnamed artifact directory, which someone reads and files.
Keeping it costs three names and covers the one case the git route cannot.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Unconditional, because git cannot be relied on to name them: `.git` is never
# listed by `ls-files --ignored`, and `venv`/`.venv`/`node_modules` are in no
# ignore file in this repo (#577). Dot-prefixed anything covers `.venv`,
# `.git`, `.pytest_cache`, `.tox`; the bare spellings need naming.
_VENDORED_DIR_NAMES = frozenset({"__pycache__", "node_modules", "venv"})

# Only reached when git cannot answer at all (see `git_ignored_dirs`). Named
# build-system output — machine state by the same reasoning as `__pycache__`.
# Never a substitute for git's answer, only a floor under it.
_ARTIFACT_DIR_NAMES = frozenset({"build", "dist", "htmlcov"})
_ARTIFACT_DIR_SUFFIXES = (".egg-info",)


def git_ignored_dirs(root: Path = REPO_ROOT) -> frozenset[str] | None:
    """Every directory git reports as ignored, repo-root-relative, carrying
    the trailing slash git writes — or None when git has no answer.

    Negations, nested ignore files, `.git/info/exclude` and the user's global
    excludes are semantics a test has no business reimplementing, and getting
    any of them wrong hides files. `--directory` collapses an ignored tree to
    its top entry, so the answer costs one subprocess and never descends into
    what it is telling us to skip.

    None means "no opinion", never "nothing is ignored". The caller has to
    fall back rather than read an unanswered question as a clean sheet — that
    reading is #559's defect.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--others", "--ignored",
             "--exclude-standard", "--directory", "--no-empty-directory"],
            cwd=str(root), capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    entries = proc.stdout.decode("utf-8", "surrogateescape").split("\0")
    return frozenset(entry for entry in entries if entry.endswith("/"))


def is_machine_state(rel: str, ignored: frozenset[str] | None) -> bool:
    """True when this repo-root-relative posix path is machine state rather
    than repository source.

    Names first and unconditionally, then git's property. See the module
    docstring for why that order is load-bearing rather than defensive.
    """
    parts = rel.split("/")
    if any(part.startswith(".") or part in _VENDORED_DIR_NAMES
           for part in parts):
        return True
    if ignored is None:
        return any(name in _ARTIFACT_DIR_NAMES
                   or name.endswith(_ARTIFACT_DIR_SUFFIXES)
                   for name in parts[:-1])
    return any(rel.startswith(prefix) for prefix in ignored)


def scanned_with(ignored: frozenset[str] | None,
                 root: Path = REPO_ROOT) -> list[Path]:
    """`repo_python_files()` with the ignore answer and the walk root injected.

    Both are parameters so that #575's and #577's regression tests can stage a
    real build artifact, and a real in-repo virtualenv, in a real throwaway git
    repo instead of in this one. The suite runs under `-n auto`; a test that
    writes into the repository root races every other worker's copy of these
    very walks, which is a worse bug than the one it would be pinning.
    """
    files = [
        path for path in sorted(root.rglob("*.py"))
        if not is_machine_state(path.relative_to(root).as_posix(), ignored)
    ]
    assert files, (
        "the exclusion rule pruned every Python file in the repository. "
        "That is not a clean repo, it is a guard scanning nothing — the "
        "failure that let #559 live for eight months under a green check.")
    return files


def repo_python_files() -> list[Path]:
    """Every Python source file in the repository."""
    return scanned_with(git_ignored_dirs())
