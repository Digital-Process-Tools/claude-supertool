"""One rule for "this change is documented", and a guard against the wrong one.

`.github/scripts/assemble_changelog.py` **consumes** `changelog.d/` fragments:
the release that ships a change deletes the file that described it and writes
the entry into `CHANGELOG.md` instead. So a test asserting that its own
fragment exists is a guard that cannot survive its own release — it passes for
as long as the release does not happen, then fails on every platform at once,
on the one event it should be indifferent to, saying nothing about the release
it is blocking.

That has now happened three times, and the first two were fixed where they were
found and nowhere else:

- **#941**, v0.26.0 — five legs red on the release commit.
- **#953**, v0.27.0 — thirteen of twenty legs red, on every platform.
- **#1053** — filed because a third instance means the per-instance fix is not
  the fix.

Two halves here, and neither is sufficient alone.

`assert_change_is_findable` makes the correct form the *easy* form. What a test
in this class actually claims is that the change is **findable**; a pending
fragment and a released CHANGELOG entry both satisfy that, and exactly one of
them is true at any moment, so accepting either loses no coverage. Writing that
once means the next author writes one call instead of ten lines they have to
get right.

`fragment_existence_assertions` refuses the wrong form at test time, because a
helper nobody knows about does not stop anyone. The wrong form looks completely
reasonable — `assert (root / "changelog.d" / "1053.added.md").is_file()` is what
anyone would write — so the thing that has to change is what happens when they
do: CI, in seconds, instead of a release, in weeks.

**What the detector establishes, which is also all it claims.** Parsed as
Python, no `assert` in the suite tests an expression that both names
`changelog.d` and calls an existence operation on a path — directly, or through
a local name bound to such an expression. It reads the asserted expression and
never the failure message, because the accepted form names
`changelog.d/941.<section>.md` in its own message and refusing that would refuse
the shape this file prescribes. A name is tracked in the scope that binds it —
the module, or the function — and not beyond, because the assembler's own suites
have a module-level `_repo()` helper that binds `frag_dir = root / "changelog.d"`
under `tmp_path`, and seven correct assertions about what a *refused* run leaves
on disk are made against that fixture in other functions.

**What it does not establish** is that no test can be broken by a release by
some other route — an indirection through a helper, a fixture, a path built from
a variable that never spells the directory. It closes the shape that shipped
three times, and it says so rather than implying more.

**A fourth instance proved the syntax was never the class** (#1231, #1293). A
module-level tuple of swept paths held `changelog.d/1231.added.md` and a
`read_text` in a loop resolved it against the checkout — no `assert`, no
existence call, so the detector above is blind to it by construction, and the
v0.33.0 release commit went red on 13 of 22 legs. `pending_fragment_references`
is the answer to that: it asks which fragments are on disk *now* and refuses any
tracked text file that names one, in any language and by any syntax. The two
guards are complementary and deliberately overlap. The AST detector fires on a
PR that ships no fragment of its own; this one fires on any file shape at all,
including a doc example or a workflow comment, but only while the fragment it
names is pending.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Set

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The directory whose contents a release deletes.
FRAGMENT_DIR = "changelog.d"

#: Path operations that answer "is something there". `glob`/`rglob`/`iterdir`
#: are here because `assert list(dir.glob("953.*.md"))` is the same assertion
#: wearing a different call — #953's own fixed version builds exactly that list
#: and pointedly does not assert on it.
_EXISTENCE_CALLS = frozenset(
    {"is_file", "is_dir", "exists", "glob", "rglob", "iterdir"})

_REMEDY = (
    "a fragment is consumed by the release that ships it, so this fails on the "
    "first release after it merges — #941 reddened five legs on v0.26.0 and "
    "#953 thirteen of twenty on v0.27.0. Call "
    "`assert_change_is_findable(<issue>)` from `tests/_changelog_findable.py` "
    "instead: it accepts a pending fragment or a released CHANGELOG.md entry, "
    "exactly one of which is true at any moment.")


def assert_change_is_findable(issue: int, root: Path = REPO_ROOT) -> None:
    """The change for `issue` is documented — as a fragment, or as an entry.

    Both states satisfy the claim a test in this class is making, and the
    release moves the change from the first to the second. Asserting only the
    first is what #941, #953 and #1053 are.

    The CHANGELOG side is a substring test, which is loose: "953" also occurs
    inside "125,953". That looseness is inherited from #941's and #953's own
    fixed versions and is left as it stands — tightening it to a link shape
    would encode one entry format into every future entry, and this guard's
    job is release-survival, not citation style.
    """
    number = str(int(issue))
    if sorted((root / FRAGMENT_DIR).glob(number + ".*.md")):
        return
    changelog = root / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8") if changelog.is_file() else ""
    assert number in text, (
        "#{0} is neither a pending {1}/{0}.<section>.md fragment nor an entry "
        "in CHANGELOG.md — the change is not findable in either place. Exactly "
        "one of those two is true at any moment: the release consumes the "
        "fragment and writes the entry. Looked in {2} and {3}."
        .format(number, FRAGMENT_DIR, (root / FRAGMENT_DIR), changelog))


#: The Keep a Changelog headings a fragment filename may carry.
_SECTIONS = ("added", "changed", "deprecated", "removed", "fixed", "security")

#: `<issue>.<section>[.<slug>].md`, the grammar `changelog.d/README.md`
#: prescribes and `assemble_changelog.py` consumes. `README.md` is the one
#: permanent file in the directory and is deliberately not matched.
_FRAGMENT_NAME = re.compile(
    r"\A[0-9]+\.(?:" + "|".join(_SECTIONS) + r")(?:\.[^.]+)?\.md\Z")

_PENDING_REMEDY = (
    "the tag that ships this change deletes that file, so the reference is "
    "green until the release and red on it and every release after — #941 took "
    "five legs on v0.26.0, #953 thirteen of twenty on v0.27.0, #1231 thirteen "
    "of twenty-two on v0.33.0, and none of the three was visible from inside "
    "the PR that wrote it. Point at CHANGELOG.md instead, where the fragment's "
    "prose lands permanently, or call `assert_change_is_findable(<issue>)` "
    "from tests/_changelog_findable.py, which accepts either state. If this is "
    "a hermetic fixture that happens to reuse this PR's own issue number, give "
    "the fixture a different number.")


def pending_fragments(root: Path = REPO_ROOT) -> List[str]:
    """The fragment filenames the next tag will delete, sorted.

    An absent or unreadable `changelog.d/` is an empty list rather than an
    error: right after a release the directory is genuinely empty, and that is
    a real state, not a failure to look. What must not read as clean is a
    *scan* that saw no files — that is `scan_test_tree`'s and `tracked_files`'
    problem, and both say so separately.
    """
    directory = Path(root) / FRAGMENT_DIR
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return []
    return [path.name for path in entries
            if path.is_file() and _FRAGMENT_NAME.match(path.name)]


def tracked_files(root: Path = REPO_ROOT) -> Optional[List[Path]]:
    """Every path git tracks under `root`, or `None` when git could not be asked.

    `None` and `[]` are different answers and the caller must be able to tell
    them apart: an empty list is "this repository tracks nothing", which would
    let a sweep over it report a clean sheet it never earned. Windows raises
    `FileNotFoundError` when git is not installed where POSIX may not fail the
    same way, so the spawn is caught rather than the exit status alone.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    if done.returncode != 0:
        return None
    names = done.stdout.decode("utf-8", "replace").split("\0")
    return [Path(name) for name in names if name]


def pending_fragment_references(root: Path, files: Sequence[Path],
                                pending: Optional[Sequence[str]] = None
                                ) -> List[str]:
    """Findings for every line of `files` naming a fragment still pending.

    Keyed to the directory's contents rather than to a filename pattern, which
    is what makes it precise. This repo holds 162 references to fragment names
    and every one is correct — a doc example, a comment, a `tmp_path` fixture —
    because each names an issue whose fragment was consumed releases ago. Only
    a name that is on disk right now can be deleted by the next tag.

    `changelog.d/` itself is not scanned: a fragment naming a sibling fragment
    is consumed in the same commit, and the README quotes the grammar three
    times.

    A file that does not decode as UTF-8 is skipped rather than guessed at, and
    so is one git lists but the working tree does not have — a staged deletion
    is an ordinary state and a `FileNotFoundError` out of a guard reads as a
    product failure.
    """
    root = Path(root)
    names = list(pending) if pending is not None else pending_fragments(root)
    if not names:
        return []
    findings: List[str] = []
    for entry in sorted(files, key=lambda path: Path(path).as_posix()):
        rel = Path(entry)
        if rel.parts[:1] == (FRAGMENT_DIR,):
            continue
        try:
            raw = (root / rel).read_bytes()
        except OSError:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name in names:
                if name in line:
                    findings.append(
                        "{0}:{1}: names {2}/{3}, which this checkout still has "
                        "pending — {4}"
                        .format(rel.as_posix(), lineno, FRAGMENT_DIR, name,
                                _PENDING_REMEDY))
    return findings


def _string_constants(node: ast.AST) -> List[str]:
    return [child.value for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)]


def _names_the_fragment_dir(node: ast.AST) -> bool:
    """Whether any string under `node` spells the directory *as a path*.

    A path component (`root / "changelog.d"`) or a path prefix
    (`"changelog.d/906.added.md"`, either separator, because a Windows-spelled
    literal is still a path). Not a mere mention: `tests/test_changelog_
    fragments_906.py` stages a fixture whose *content* is
    `"# changelog.d\\n\\nHow this works.\\n"`, and a substring test read that as
    the test naming the directory and refused a correct assertion two lines
    below it.
    """
    return any(text == FRAGMENT_DIR
               or FRAGMENT_DIR + "/" in text
               or FRAGMENT_DIR + "\\" in text
               for text in _string_constants(node))


def _existence_calls(node: ast.AST) -> List[str]:
    return sorted({child.func.attr for child in ast.walk(node)
                   if isinstance(child, ast.Call)
                   and isinstance(child.func, ast.Attribute)
                   and child.func.attr in _EXISTENCE_CALLS})


def _referenced_names(node: ast.AST) -> Set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


#: A `lambda` binds no name a later statement can assert on, so it is a barrier
#: for the walk but never a scope to descend into.
_BARRIERS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _own_nodes(scope: ast.AST) -> List[ast.AST]:
    """Every node under `scope` that is not inside a nested function."""
    out: List[ast.AST] = []
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        out.append(node)
        if not isinstance(node, _BARRIERS):
            stack.extend(ast.iter_child_nodes(node))
    return out


def _nested_functions(scope: ast.AST) -> List[ast.AST]:
    """The function definitions immediately inside `scope`, at any block depth."""
    return [node for node in _own_nodes(scope)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _scan_scope(scope: ast.AST, filename: str, dir_names: Set[str],
                lookup_names: Set[str]) -> List[str]:
    """Findings in one scope and every scope nested in it.

    Two name sets, because the shape splits across statements in two different
    places. `dir_names` is every local bound to an expression that *names* the
    directory — the assertion may then do the looking (`frag = ROOT /
    "changelog.d"` … `assert frag.glob("1053.*.md")`). `lookup_names` is the
    narrower set that also did the looking, for a bare `assert fragments`.

    The two are deliberately not one set. `readme = (REPO / "changelog.d" /
    "README.md").read_text(...)` binds a name that *names* the directory and
    looks nothing up, and `tests/test_changelog_fragment_whitelist_934.py` then
    asserts a substring of it — a correct, release-proof assertion that a single
    merged set would refuse.
    """
    dir_names = set(dir_names)
    lookup_names = set(lookup_names)
    own = _own_nodes(scope)

    for node in own:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        if not _names_the_fragment_dir(node.value):
            continue
        bound: Set[str] = set()
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
            bound |= _referenced_names(target)
        dir_names |= bound
        if _existence_calls(node.value):
            lookup_names |= bound

    findings: List[str] = []
    for node in own:
        if not isinstance(node, ast.Assert):
            continue
        calls = _existence_calls(node.test)
        names = _referenced_names(node.test)
        if calls and (_names_the_fragment_dir(node.test) or names & dir_names):
            how = "calls " + ", ".join(name + "()" for name in calls)
        elif names & lookup_names:
            how = "asserts a name bound to a " + FRAGMENT_DIR + " lookup"
        else:
            continue
        findings.append(
            "{0}:{1}: this assertion tests that a {2} path exists ({3}) — {4}"
            .format(filename, node.lineno, FRAGMENT_DIR, how, _REMEDY))

    for nested in _nested_functions(scope):
        findings.extend(_scan_scope(nested, filename, dir_names, lookup_names))
    return findings


def fragment_existence_assertions(source: str, filename: str) -> List[str]:
    """Findings for one module's source, each naming the file and the line.

    A finding is an `assert` that calls an existence operation on something
    that names `changelog.d` — spelled inline, or reached through a name bound
    in the same scope or an enclosing one — or that asserts a name bound to a
    `changelog.d` existence lookup.
    """
    tree = ast.parse(source, filename=filename)
    return sorted(_scan_scope(tree, filename, set(), set()),
                  key=lambda line: int(line.split(":")[1]))


def suite_modules(root: Path = REPO_ROOT) -> List[Path]:
    """Every module of the suite, in sorted order.

    `root/tests/test_*.py`, non-recursively: that is the whole suite today, and
    a flat literal beats an `rglob` that would also parse the deliberately
    malformed Python under `tests/fixtures/`.
    """
    return sorted((root / "tests").glob("test_*.py"))


def scan_test_tree(root: Path = REPO_ROOT) -> List[str]:
    """Every finding in the suite. An empty scan is a failure, not a pass."""
    files: Sequence[Path] = suite_modules(root)
    assert files, (
        "scanned no test files under {0} — a guard that looked at nothing "
        "must not read as a clean sheet".format(root / "tests"))
    findings: List[str] = []
    for path in files:
        findings.extend(fragment_existence_assertions(
            path.read_text(encoding="utf-8"),
            path.relative_to(root).as_posix()))
    return findings
