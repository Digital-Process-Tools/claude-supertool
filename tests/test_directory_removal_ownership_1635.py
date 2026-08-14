"""#1635 -- every call in this tree that can remove a *directory*, and who owns it.

A full `pytest` run in a fresh `git clone` was once observed to leave the tree
holding only `tests/` -- `.git` and everything else gone, 603 collection errors
in 14.8s. It has never been reproduced, a reviewer agent was running the suite
in a sibling worktree at the same time, and the mechanism was never established.

This file does not chase that event. It answers the question that is answerable
either way, and keeps it answered:

    which code in this repo can remove a directory it did not create?

**Why an AST walk and not a grep.** This tree mentions `rmtree` and `rm -rf`
constantly -- 94 lines across 48 files match a grep, and all but a couple of
dozen are prose, changelog entries, or shell-injection payloads a test asserts
are *never* executed. A count taken from that grep reads as an inventory and is
not one. Only `ast.Call` nodes are sites here, which is also why the synthetic
sources further down are invisible to the sweep: a call inside a string literal
is an `ast.Constant`, not a call.

**The population, at the commit this was written.** 29 directory-removal sites
across 27 files: 26 `shutil.rmtree`, 2 `subprocess.run(["rm", "-rf", ...])`, 1
`git worktree remove`, and zero `os.rmdir` / `os.removedirs` / `Path.rmdir`.
Only two are outside `tests/` -- `validators/gitleaks/gitleaks.py`, which
removes the private directory it made for one scan, and
`presets/github/pr_merge.py`, the only site whose path the caller never
composed. Any total written in prose is a measurement of one commit; the tests
below re-derive, and they are what a reader should believe when a number
disagrees with them.

**The verdict, re-derived on every run.** Every one of the 28 sites that
composes its own target resolves to a directory the same file created --
`tempfile.mkdtemp`, a `TemporaryDirectory`, or pytest's `tmp_path`. Not one is
built from the repository root, from the current working directory, or from an
argument the caller does not control. `_supertool.py`, which every op runs
through, contains no directory removal at all. So nothing here explains a
missing `.git`, and that absence is itself the finding: if there was a
mechanism, it is outside the code this file can see.

**What "owned" means, and what it deliberately refuses.** The question is
#1246's -- which argument names a directory, and who knows what that directory
is allowed to be. So ownership is proved, never assumed: every non-literal part
of the expression must trace back to a call that *made* a directory. A path
joined against a value the function was handed is UNOWNED even when the other
half is a `mkdtemp`, because an absolute right-hand operand discards the left --
which is exactly how #1246 walked out of its intended tree.
"""

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

#: Calls that hand back a directory the caller now owns.
OWNERS = ("mkdtemp", "TemporaryDirectory", "mkstemp", "NamedTemporaryFile")

#: Calls that re-spell a path without changing which directory it names, so
#: ownership passes straight through them.
PASSTHROUGH = ("str", "Path", "dirname", "abspath", "realpath", "resolve",
               "normpath", "fspath")

#: pytest hands these out already made and reaps them itself.
FRAMEWORK = ("tmp_path", "tmp_path_factory", "tmpdir", "tmpdir_factory")

OWNED = "OWNED: made by this file, or handed over by pytest"
GIT = "GIT: the path came from `git worktree list`, and the arm is gated"
UNOWNED = "UNOWNED"

#: Directories this sweep does not enter -- none of them hold source of ours,
#: and a virtualenv checked out in-tree would otherwise put a few hundred
#: third-party `rmtree` calls into the register and turn the gate red for
#: reasons that have nothing to do with this repository.
SKIP_DIRS = (".git", ".max", ".venv", "venv", ".tox", "node_modules",
             "site-packages", "__pycache__", ".pytest_cache", "build", "dist")

#: Callables that actually spawn an argv. Anything else holding the same list
#: is asserting about a command, not running one.
RUNNERS = ("run", "Popen", "call", "check_call", "check_output",
           "_git", "_git_rc", "_run")


def _scopes(module, node):
    """Every def/class enclosing `node`, outermost first."""
    return sorted(
        (n for n in ast.walk(module)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
         and n.lineno <= node.lineno <= (n.end_lineno or n.lineno)),
        key=lambda n: n.lineno)


#: `id(scope)` -> `{name: [bound expression, ...]}`, for the file being read
#: right now and no other. Without it every lookup re-walked the scope and
#: re-`unparse`d every assignment target in it, which cost 13s a sweep.
#:
#: **Cleared per file, and that is not tidiness.** CPython reuses the `id` of a
#: freed object, and the previous file's tree is freed the moment the next one
#: is parsed -- so a cache that outlived one file could hand a scope another
#: file's bindings, and produce a clean-looking verdict about code it had not
#: read. `_sites_in_source` holds its own module alive for as long as any of
#: these keys can be looked up.
_BINDINGS: dict = {}


def _bindings_in(container, target):
    """Every expression `target` is bound to inside one scope, nested defs included.

    A `for` target is special: `for d in made:` binds `d` to whatever went
    INTO `made`, not to `made`. Resolving it to the container is what made the
    one real teardown loop in this tree read as UNOWNED.
    """
    table = _BINDINGS.get(id(container))
    if table is None:
        table = {}
        for node in ast.walk(container):
            if isinstance(node, ast.Assign):
                for one in node.targets:
                    table.setdefault(ast.unparse(one), []).append(node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                table.setdefault(ast.unparse(node.target), []).append(node.value)
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                table.setdefault(
                    ast.unparse(node.optional_vars), []).append(node.context_expr)
            elif isinstance(node, ast.For):
                key = ast.unparse(node.target)
                if isinstance(node.iter, ast.Name):
                    table.setdefault(key, []).extend(
                        _pushed_into(container, node.iter.id))
                else:
                    table.setdefault(key, []).append(node.iter)
        _BINDINGS[id(container)] = table
    return table.get(target, [])


def _pushed_into(container, name):
    """Values appended to the list `name` -- how a teardown loop gets its paths."""
    return [node.args[0] for node in ast.walk(container)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("append", "add")
            and ast.unparse(node.func.value) == name
            and node.args]


def _sources(module, scopes, target):
    """Bindings of `target` from the nearest scope that has any.

    Innermost first, stopping at the first scope that binds it. Falling through
    to the module for a name an enclosing function already binds is not a
    widening but a wrong answer: `tmp` is a local in eight separate functions
    of `test_git_checkout_rebase_state_900.py`, and pooling all eight made a
    teardown that only ever removes its own `mkdtemp` read as UNOWNED.
    """
    for container in list(reversed(list(scopes))) + [module]:
        found = _bindings_in(container, target)
        if found:
            return found
    return []


def _is_owned(expr, module, scopes, depth=0, seen=()):
    """Does `expr` name a directory this file made?

    A string literal is never owned. That is the whole difference between this
    and a classifier that certifies `ROOT = "/somewhere"`: a hardcoded path is
    a claim about the world, not a directory we created. Literals are allowed
    only as the *later* operands of a join under a root that is already ours,
    which is the one place they cannot redirect anything.
    """
    if depth > 10:
        return False
    if isinstance(expr, ast.Constant):
        return False
    if isinstance(expr, (ast.List, ast.Tuple)):
        return bool(expr.elts) and all(
            _is_owned(e, module, scopes, depth + 1, seen) for e in expr.elts)
    if isinstance(expr, ast.JoinedStr):
        # No site composes a removal target with an f-string. Refusing the
        # shape outright is the safe direction: `f"/etc/{ours}"` is not ours,
        # and telling that from `f"{ours}/sub"` needs more than this walk.
        return False
    if isinstance(expr, ast.BinOp):
        # `Path(mkdtemp()) / "sub"` -- the left operand carries the ownership
        # and the right must be a literal, because `Path(a) / b` with an
        # absolute `b` discards `a` exactly as `os.path.join` does (#1246).
        return (_is_owned(expr.left, module, scopes, depth + 1, seen)
                and isinstance(expr.right, ast.Constant))
    if isinstance(expr, ast.Subscript):
        return _is_owned(expr.value, module, scopes, depth + 1, seen)
    if isinstance(expr, ast.Call):
        func = expr.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else "")
        if name in OWNERS:
            return True
        args = list(expr.args) or (
            [func.value] if isinstance(func, ast.Attribute) else [])
        if not args:
            return False
        if name == "join":
            # Only the first operand can be ours; every later one must be a
            # literal. A variable there is UNOWNED however innocent it looks,
            # because one absolute value is all it takes to leave the tree.
            return (_is_owned(args[0], module, scopes, depth + 1, seen)
                    and all(isinstance(a, ast.Constant) for a in args[1:]))
        if name in PASSTHROUGH:
            return _is_owned(args[0], module, scopes, depth + 1, seen)
        return False
    if isinstance(expr, (ast.Name, ast.Attribute)):
        key = ast.unparse(expr)
        if isinstance(expr, ast.Name) and expr.id in FRAMEWORK:
            return True
        sources = _sources(module, scopes, key)
        if isinstance(expr, ast.Attribute):
            # `box.tmp` where `_Repo.__init__` set `self.tmp = mkdtemp(...)`.
            sources = sources + _sources(module, scopes, "self." + expr.attr)
        sources = [s for s in sources
                   if ast.unparse(s) not in seen + (key,)
                   and not (isinstance(s, (ast.List, ast.Tuple)) and not s.elts)]
        if not sources:
            return False
        return all(_is_owned(s, module, scopes, depth + 1, seen + (key,))
                   for s in sources)
    return False


def _removal_target(node):
    """The directory a call would destroy, or `(None, None)` if it destroys none.

    Returns `(expr, kind)`. `kind` is `"git"` for `git worktree remove`, whose
    path git itself reported and the caller never composed.
    """
    func = node.func
    name = (func.attr if isinstance(func, ast.Attribute)
            else func.id if isinstance(func, ast.Name) else None)
    if name == "rmtree" and node.args:
        return node.args[0], "call"
    if name in ("rmdir", "removedirs"):
        if node.args:
            return node.args[0], "call"
        if isinstance(func, ast.Attribute):
            return func.value, "call"
        return None, None
    # Only a call that *runs* an argv removes anything. A test asserting on
    # `["worktree", "remove", path]` is reading a receipt, and a register that
    # counted it would report removals nobody performs.
    if name not in RUNNERS:
        return None, None
    if not node.args or not isinstance(node.args[0], (ast.List, ast.Tuple)):
        return None, None
    elts = node.args[0].elts
    words = [e.value for e in elts
             if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    if not words:
        return None, None
    operands = [e for e in elts
                if not (isinstance(e, ast.Constant)
                        and isinstance(e.value, str)
                        and e.value.startswith("-"))]
    if words[0] == "rm" and any(
            w.startswith("-") and set(w[1:]) & set("rR") for w in words[1:]):
        return (operands[1] if len(operands) > 1 else None), "call"
    if "worktree" in words and "remove" in words:
        return (operands[-1] if operands else None), "git"
    return None, None


def _sites_in_source(rel, source, found=None):
    """One file's sites. Split out so the classifier can be run on strings."""
    found = {} if found is None else found
    _BINDINGS.clear()
    module = ast.parse(source)
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        target, kind = _removal_target(node)
        if target is None:
            continue
        scopes = _scopes(module, node)
        if kind == "git":
            mechanism = GIT
        else:
            mechanism = OWNED if _is_owned(target, module, scopes) else UNOWNED
        key = "{0}::{1}".format(
            rel, ".".join(n.name for n in scopes) or "<module>")
        found.setdefault(key, []).append((node.lineno, mechanism))
    return found


#: One sweep per process. Six tests ask the same question of the same tree,
#: and this file is in the `lane-ci-cost` issue's own lane.
_SWEEP: list = []


def _call_sites():
    """`(sites, unreadable)` for the whole tree.

    A file this walk cannot parse is *returned*, never skipped. Skipping it
    would subtract a file from the population and leave the answer looking
    exactly like a file with no removals in it -- the absence-read-as-absence
    shape this register exists to keep out of the suite.
    """
    if _SWEEP:
        return _SWEEP[0]
    found = {}
    unreadable = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            _sites_in_source(rel, source, found)
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
            unreadable.append((rel, type(exc).__name__))
    _SWEEP.append((found, unreadable))
    return found, unreadable


def _mechanisms():
    """`key` -> the one mechanism covering every removal under it."""
    out = {}
    for key, sites in _call_sites()[0].items():
        kinds = set(mech for _line, mech in sites)
        out[key] = kinds.pop() if len(kinds) == 1 else "MIXED: " + repr(sorted(kinds))
    return out


#: Every call in this tree that can remove a directory, and what proves the
#: directory was its to remove. Keyed by `path::enclosing def`, not by line
#: number, so an unrelated edit above does not make it stale.
REGISTER = {
    # The only directory removal in product code. The path is not composed
    # here at all -- git reported it -- and the arm ahead of it refuses on
    # three separate reads before it fires (#1280, #1290).
    'presets/github/pr_merge.py::_cleanup_worktree': GIT,

    'tests/test_git_checkout_pathspec_756.py::repo': OWNED,
    'tests/test_git_checkout_rebase_state_900.py::repo': OWNED,
    'tests/test_git_checkout_recovery_649.py::remote_factory': OWNED,
    'tests/test_git_mr_lookup_948.py::test_a_git_that_cannot_be_spawned_is_an_unknown_not_a_traceback': OWNED,
    'tests/test_git_push_budget_1530.py::_Sandbox.close': OWNED,
    'tests/test_git_push_budget_deadline_1615_1617.py::_Sandbox.close': OWNED,
    'tests/test_git_push_budget_from_config_1631.py::_Sandbox.close': OWNED,
    'tests/test_git_push_first_upstream_354.py::FirstUpstreamRebaseTest.tearDown': OWNED,
    'tests/test_git_push_force_discard_655.py::_Sandbox.close': OWNED,
    'tests/test_git_push_hazards_640_642_647.py::_Sandbox.close': OWNED,
    'tests/test_git_push_hook_text_nff_641.py::_Sandbox.close': OWNED,
    'tests/test_git_push_mismatched_upstream_787.py::MismatchedUpstreamTest.tearDown': OWNED,
    'tests/test_git_push_post_push_receipt_675.py::_Sandbox.close': OWNED,
    'tests/test_git_push_rebase_route_discloses_hook_1490.py::_Sandbox.close': OWNED,
    'tests/test_git_push_receipt_truthfulness_661_662_663.py::_Sandbox.close': OWNED,
    'tests/test_git_push_relays_hook_output_1448.py::_Sandbox.close': OWNED,
    'tests/test_git_push_remote_resolution_656.py::_Box.close': OWNED,
    'tests/test_git_push_set_upstream_879.py::InheritedUpstreamTest.tearDown': OWNED,
    'tests/test_git_push_set_upstream_879.py::MatchingUpstreamTest.tearDown': OWNED,
    'tests/test_git_worktrees_unpushed_1496.py::_Sandbox.close': OWNED,
    'tests/test_git_worktrees_upstream_remote_1525.py::_Sandbox.close': OWNED,
    'tests/test_kevin_2026_05_17.py::test_paste_op_creates_missing_file_and_parent': OWNED,
    'tests/test_mcp_daemon_dedup_451.py::runtime': OWNED,
    'tests/test_notifiers_claude_channel_550.py::sock_dir': OWNED,
    'tests/test_notifiers_claude_channel_554.py::Channel.close': OWNED,
    'tests/test_vim_kevin_fixes.py::_cleanup_persist': OWNED,
    'tests/test_vim_kevin_fixes.py::test_undo_cross_call': OWNED,
    'validators/gitleaks/gitleaks.py::main': OWNED,
}


def test_every_directory_removal_site_is_registered() -> None:
    """A new removal must be classified here before it can be merged."""
    live = _mechanisms()
    missing = sorted(k for k in live if k not in REGISTER)
    assert not missing, (
        "directory-removal sites with no entry in REGISTER -- add them with "
        "the mechanism that proves each owns what it deletes: "
        + repr({k: live[k] for k in missing}))


def test_no_registered_site_has_disappeared() -> None:
    """An entry with no call site left is a stale claim, which is the shape
    this repo keeps mistaking for a true one."""
    live = _mechanisms()
    stale = sorted(k for k in REGISTER if k not in live)
    assert not stale, "REGISTER entries with no call site left: " + repr(stale)


def test_nothing_removes_a_directory_it_cannot_prove_it_owns() -> None:
    """The load-bearing one, and the answer to #1635's first question.

    Every removal must resolve to a directory the same file made. A site that
    is reclassified UNOWNED by a later edit -- a `mkdtemp` swapped for a path
    from the environment, a teardown that starts joining an argument onto its
    root -- turns this red on the commit that does it, not on the run that
    loses somebody's checkout.
    """
    live = _mechanisms()
    unowned = sorted(k for k, mech in live.items() if UNOWNED in mech)
    assert not unowned, (
        "these can remove a directory whose provenance is not established: "
        + repr({k: live[k] for k in unowned}))


def test_the_recorded_mechanism_is_still_the_one_in_the_code() -> None:
    """The label is re-derived, never trusted."""
    live = _mechanisms()
    drifted = dict((k, (REGISTER[k], live[k])) for k in REGISTER
                   if k in live and REGISTER[k] != live[k])
    assert not drifted, (
        "the mechanism recorded here is no longer the mechanism in the code "
        "(recorded, actual): " + repr(drifted))


def test_every_python_file_in_the_tree_was_actually_read() -> None:
    """The population is only a population if nothing dropped out of it.

    A file the walk could not parse is not a file without removals in it, and
    a register that quietly skipped one would answer `ok` about code it never
    looked at.
    """
    _sites, unreadable = _call_sites()
    assert not unreadable, (
        "these files were counted in no verdict above, so nothing here is "
        "claimed about them: " + repr(unreadable))


def test_the_core_removes_no_directory_at_all() -> None:
    """`_supertool.py` is the file every op runs through, and it must stay
    incapable of removing a directory. It unlinks cache entries and its own
    write-temp files; it has never had a `shutil.rmtree`, and a checker that
    only counted test-side sites would not notice one arriving."""
    sites = _sites_in_source(
        "_supertool.py", (ROOT / "_supertool.py").read_text(encoding="utf-8"))
    assert not sites, "the core gained a directory removal: " + repr(sites)


def test_the_classifier_refuses_a_path_it_cannot_prove_ownership_of() -> None:
    """Without this the register is a list, not a claim.

    Each source below is a string, so none of these calls exists in this
    module's own AST -- which is the same reason the grep this file replaces
    could not tell a payload from a call site.
    """
    nl = chr(10)

    def mech(*body):
        src = nl.join(body) + nl
        sites = _sites_in_source("tests/synthetic.py", src)
        return list(sites.values())[0][0][1]

    assert mech("import shutil, tempfile",
                "def t():",
                "    d = tempfile.mkdtemp()",
                "    shutil.rmtree(d)") == OWNED

    assert mech("import shutil",
                "def t(tmp_path):",
                "    shutil.rmtree(tmp_path)") == OWNED

    assert mech("import shutil",
                "ROOT = '/somewhere'",
                "def t():",
                "    shutil.rmtree(ROOT)") == UNOWNED, (
        "a module constant is not a directory this file made")

    assert mech("import shutil, os, tempfile",
                "def t(name):",
                "    d = tempfile.mkdtemp()",
                "    shutil.rmtree(os.path.join(d, name))") == UNOWNED, (
        "an argument joined onto our own root was certified -- an absolute "
        "right-hand operand discards the left, which is how #1246 escaped")

    assert mech("import shutil, os, tempfile",
                "def t():",
                "    d = tempfile.mkdtemp()",
                "    shutil.rmtree(os.path.join(d, 'sub'))") == OWNED, (
        "a literal component under our own root is still ours")

    assert mech("import shutil, tempfile",
                "def t(where):",
                "    d = tempfile.mkdtemp()",
                "    d = where",
                "    shutil.rmtree(d)") == UNOWNED, (
        "one owning assignment does not certify a name that is also bound to "
        "something we were handed")

    assert not _sites_in_source(
        "tests/synthetic.py",
        "def t():" + nl + "    " + chr(39) * 3 + "mentions shutil.rmtree(ROOT)"
        + chr(39) * 3 + nl + "    return 1" + nl), (
        "a docstring naming a removal was read as a call site")
