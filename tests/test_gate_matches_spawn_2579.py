"""An absent-tool gate must ask the same PATH the spawn will use (#2579).

Follow-up from #2575 / PR #2577's own review. #2575 fixed the *spawn* side of
every adapter listed below -- `argv0(name)` and `resolve_bin_cmd(raw, default)`
both route through `spawnable.which_excluding_cwd()`, so a repo-planted
`ruff.cmd` at the project root can no longer be executed. It left the *gate*
side untouched: `validators/ruff/ruff.py:200` (at the time) still asked
`shutil.which(TOOL)` -- the raw, cwd-including form -- before ever reaching
that spawn.

The two calls then search different PATH lists. A repo-planted shim passes
the gate (raw `which()` still finds it via cwd) and then fails to spawn (the
corrected `argv0()`/`resolve_bin_cmd()` refuses it) -- not a security hole
(the fix still holds), but a correctness gap: the gate says "found" and the
spawn says "not found", for a reason a caller debugging a validator failure
has no way to connect without reading both call sites.

This is the register that keeps the class closed, the same shape as
`tests/test_spawnable_register_2540.py`: an adapter whose actual subprocess
spawn already goes through the cwd-excluding chokepoint (`argv0`, `spawnable`,
or `resolve_bin_cmd`) must not gate on a raw `shutil.which()` call instead.

`validators/phpstan/phpstan.py` was the one adapter deliberately left off this
register, tracked separately (#2581): its resolved `phpstan_bin` value is spliced as
a literal script argument to `php`, not spawned as argv[0], so its own gate
had nothing to be inconsistent WITH until it gated on `spawnable()` too --
matching every other `_BIN` adapter's own convention, `spawnable()` there is
a presence-only check and `argv0(phpstan_bin)` builds the actual argv element
separately. Done, for what THIS register checks (gate vs. spawn agreement) --
the `ALLOWLIST` below is empty rather than removed outright (kept as the
register's own escape hatch for the next adapter shaped this way).

That is not the same claim as "phpstan.py's argv construction has no bare,
unresolved literal": #2605 found one anyway, one argv slot over -- `php`
itself, at argv[0], was never passed to `spawnable()`/`argv0()` at all, so
this register's own gate-vs-spawn check had nothing to disagree with (the
chokepoint WAS imported and used, just not for `php`). That variant needed
a register that inspects argv construction directly,
`tests/test_bare_argv0_construction_2605.py` -- "done now" above is scoped
to this file's own detection mechanism, not to phpstan.py as a subject.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
ADAPTER_DIRS = ("validators", "formatters")

#: The chokepoint modules themselves call `shutil.which()` internally (the
#: dirname-branch delegation in `which_excluding_cwd`) -- that is the
#: implementation, not an instance of the defect.
CHOKEPOINT_FILES = {"spawnable.py", "bin_resolve.py"}

#: `phpstan_bin` was spliced into `php`'s argv as a literal script path, with
#: nothing to check it against -- no chokepoint-routed spawn its gate could
#: disagree with. Fixed by #2581 (gated on `spawnable()` too, argv built
#: separately via `argv0()`, same split every other `_BIN` adapter already
#: uses), so nothing needs allowlisting here today; kept empty rather than
#: removed as the register's own escape hatch for the next adapter shaped
#: this way.
ALLOWLIST: set = set()


def _adapter_sources() -> "list[pathlib.Path]":
    out = []
    for d in ADAPTER_DIRS:
        out.extend(sorted((ROOT / d).rglob("*.py")))
    return [p for p in out if p.name not in CHOKEPOINT_FILES]


def _calls_raw_which(tree: ast.AST) -> bool:
    """Does this file call `shutil.which(...)` anywhere?"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name == "which":
            return True
    return False


CHOKEPOINT_ARG_FUNCS = {"spawnable", "argv0", "which_excluding_cwd", "resolve_bin_cmd", "already_a_path"}


def _parent_map(tree: ast.AST) -> "dict[ast.AST, ast.AST]":
    """Every node's immediate parent, since vanilla `ast` carries no
    back-reference and finding an existence check's *enclosing* and-chain
    (rather than only its direct siblings) needs one (#2606)."""
    parents: "dict[ast.AST, ast.AST]" = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _chokepoint_arg_names(tree: ast.AST) -> "set[str]":
    """The variable name or literal string every
    spawnable()/argv0()/which_excluding_cwd()/resolve_bin_cmd()/
    already_a_path() call resolves -- what an existence check has to be
    testing before it can be "the same gate", rather than an unrelated
    file-existence check elsewhere in the adapter (#2606: widening the
    existence-check walker to also catch a bare-imported spelling or a
    missing and-chain, with no correlation to what is actually being
    resolved, produced five false positives -- `cargo-check.py` checking
    `Cargo.toml`, `go-vet.py` checking `go.mod`, etc.)."""
    names: "set[str]" = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        fn = node.func
        fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if fname not in CHOKEPOINT_ARG_FUNCS:
            continue
        arg0 = node.args[0]
        if isinstance(arg0, ast.Name):
            names.add(arg0.id)
        elif isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
            names.add(arg0.value)
    return names


def _is_existence_call(node: ast.AST) -> bool:
    """`X.exists()`/`X.is_file()`/`os.path.isfile(X)` (attribute style) OR
    a bare `isfile(X)`/`exists(X)` reached via `from os.path import isfile`
    (#2606 bullet 2 -- the old walker only ever recognised the attribute
    spelling)."""
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    if isinstance(fn, ast.Attribute):
        return fn.attr in ("exists", "isfile", "is_file")
    if isinstance(fn, ast.Name):
        return fn.id in ("exists", "isfile", "is_file")
    return False


def _existence_check_subject(node: ast.Call) -> "ast.expr | None":
    """The expression an existence check actually tests: the sole
    argument for `isfile(X)`/`os.path.isfile(X)`, or the receiver for
    `X.exists()`/`X.is_file()` -- a method call, so what is being tested
    is `fn.value`, not an argument (there is none)."""
    fn = node.func
    if isinstance(fn, ast.Attribute) and fn.attr in ("exists", "is_file") and not node.args:
        return fn.value
    if node.args:
        return node.args[0]
    return None


def _names_mentioned(expr: "ast.expr | None", names: "set[str]") -> "set[str]":
    """Which of `names` does this subtree actually reference, as either a
    bare identifier or a matching string literal, anywhere inside it?"""
    found: "set[str]" = set()
    if expr is None:
        return found
    for n in ast.walk(expr):
        if isinstance(n, ast.Name) and n.id in names:
            found.add(n.id)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in names:
            found.add(n.value)
    return found


def _might_be_true_without_dirname_call(node: "ast.expr", subject_names: "set[str]") -> bool:
    """Could this boolean sub-expression evaluate True without a
    `dirname(...)` call ON ONE OF `subject_names` anywhere in it having
    been required true first?

    Pure truth-table reasoning over AND/OR, not a structural search of a
    fixed BoolOp's direct values (#2606 review finding, second round): an
    earlier draft climbed only to the NEAREST enclosing `ast.BoolOp(And)`
    and searched it (and any subtree nested under it) for a `dirname`
    call anywhere at all. That is wrong in both directions the moment an
    `Or` sits between the existence check and its dirname guard --
    `dirname(X) or Path(X).exists()` is exactly as unguarded as no dirname
    check at all (an `or`'s truth needs only ONE operand true, so a bare
    relative name matching in cwd makes the whole expression true on its
    own), and that shape was read as SAFE because the nearest enclosing
    `BoolOp(And)` a level up happened to contain a `dirname` call
    somewhere in it. Conversely `dirname(X) and (Y or (Z and
    Path(X).exists()))` is genuinely safe -- the existence check can only
    be reached once `dirname(X)` has already been required true -- but
    was flagged as an offender because the nearest enclosing And is the
    *inner* one, which does not itself contain `dirname`.

    `subject_names` -- the names `_names_mentioned` found in the existence
    check's own subject -- is what a `dirname(...)` call's OWN argument
    must directly BE (a bare Name or a matching string literal) before it
    counts as guarding anything. Two rounds of review findings shaped
    this:

    - Second round, both reviewers: matching on the `dirname` function
      name alone, with no check on what it was CALLED WITH, let
      `dirname(OTHER) and Path(TOOL).exists()` read as "TOOL is guarded"
      when the dirname call never touches TOOL at all. This gap predates
      the AND/OR rewrite -- the original `_contains_dirname_call` had the
      identical blind spot.
    - Third round, both reviewers: an intermediate fix that required the
      argument to *mention* a subject name anywhere in its own subtree
      (rather than requiring the argument to directly BE that name) still
      accepted `dirname(LOOKUP[TOOL])`, `dirname(A or B)` guarding `B`, and
      a never-taken ternary branch -- none of those calls actually take
      dirname of the checked value at runtime, they merely contain a
      matching `Name`/string node somewhere inside a larger expression.

    An `and`'s truth requires EVERY operand true, so if even one operand
    guarantees dirname (`might` is False for it), the whole and's truth
    entails dirname's truth regardless of how deeply that operand is
    nested. An `or`'s truth needs only ONE operand true, so unless EVERY
    operand individually guarantees dirname, the or's truth does not.
    `bool(dirname(X))` is unwrapped transparently, the same nested
    spelling `spawnable._already_a_path` itself uses.
    """
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_might_be_true_without_dirname_call(v, subject_names) for v in node.values)
        return any(_might_be_true_without_dirname_call(v, subject_names) for v in node.values)
    if isinstance(node, ast.Call):
        fn = node.func
        fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if fname == "dirname":
            arg = node.args[0] if node.args else None
            # Third self-review round, both reviewers independently: a
            # subtree walk over the WHOLE argument (`_mentions_any_name`)
            # matched `dirname(LOOKUP[TOOL])`, `dirname(A or B)` and a
            # never-taken ternary branch just for containing a Name node
            # equal to the checked name somewhere inside them -- none of
            # those calls actually take dirname of the checked value at
            # runtime. Require the argument to BE one of subject_names
            # directly (a bare Name or a matching string literal), not
            # merely to mention one somewhere in a larger expression.
            if isinstance(arg, ast.Name) and arg.id in subject_names:
                return False
            if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                    and arg.value in subject_names):
                return False
            return True
        if fname == "bool" and len(node.args) == 1:
            return _might_be_true_without_dirname_call(node.args[0], subject_names)
    return True


def _boolop_scope(node: ast.AST, parents: "dict[ast.AST, ast.AST]") -> "ast.BoolOp | None":
    """Climb from `node` through nested `ast.BoolOp` ancestors only,
    stopping at the first ancestor that is not itself a BoolOp (an `if`
    test boundary, a `not (...)` wrapper, an assignment, ...). Returns the
    OUTERMOST BoolOp reachable this way, or None when there is no
    enclosing BoolOp at all -- climbing only to the nearest one, rather
    than the outermost, is the mistake `_might_be_true_without_dirname_call`'s
    own docstring explains."""
    scope = None
    cur: ast.AST = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, ast.BoolOp):
            scope = cur
        else:
            break
    return scope


def _bare_existence_check_without_dirname_guard(tree: ast.AST) -> bool:
    """Does this file check `X.exists()`/`X.is_file()`/`os.path.isfile(X)`
    (or the bare-imported spelling of any of those) -- for the SAME name a
    chokepoint call resolves -- in a way that could evaluate true without
    a `dirname(X)` guard having been required true first, or with no
    enclosing boolean expression at all to guard it (#2602, widened by
    #2606)?

    This is the second-disjunct shape #2575/#2579/#2581 never touched:
    `if not spawnable(X) and not (Path(X).exists() and os.access(X,
    os.X_OK)):`. A bare, separator-free name checked this way resolves
    relative to the current directory exactly like the raw `which()` call
    the first disjunct was already fixed to stop -- see
    `spawnable._already_a_path`'s own docstring. Scoped to existence
    checks that mention a name a chokepoint call in the SAME file also
    resolves: an adapter's unrelated file-discovery existence check (does
    `Cargo.toml`/`go.mod` exist?) is not a tool-presence gate at all and
    must not be flagged just for lacking a dirname guard it was never a
    candidate for.
    """
    chokepoint_names = _chokepoint_arg_names(tree)
    if not chokepoint_names:
        return False
    parents = _parent_map(tree)
    for node in ast.walk(tree):
        if not _is_existence_call(node):
            continue
        subject = _existence_check_subject(node)
        subject_names = _names_mentioned(subject, chokepoint_names)
        if not subject_names:
            continue
        scope = _boolop_scope(node, parents)
        if scope is None or _might_be_true_without_dirname_call(scope, subject_names):
            return True
    return False


def _uses_the_chokepoint_at_spawn_time(tree: ast.AST) -> bool:
    """Does this file import the cwd-excluding chokepoint at all?

    `argv0`/`spawnable` (direct import from `spawnable`) or `resolve_bin_cmd`
    (from `bin_resolve`, itself routed through the same guard) -- either one
    means this adapter's actual subprocess spawn is already cwd-safe, so its
    gate is now the only place the old, unsafe `shutil.which()` can survive.
    """
    names = set()
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.asname or alias.name)
    if names & {"argv0", "spawnable", "resolve_bin_cmd"}:
        return True
    # `import spawnable` / `import bin_resolve` (module-style, attribute
    # access at the call site -- `spawnable.argv0(...)`) is a second way to
    # reach the same chokepoint that `from X import Y` does not surface as
    # a bare name. No shipped adapter uses this style today, but the walker
    # must not go blind the day one does (#2579 review).
    if modules & {"spawnable", "bin_resolve"}:
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in (
                    "argv0", "spawnable", "resolve_bin_cmd"):
                return True
    return False


def _offenders() -> "list[str]":
    hits = []
    for path in _adapter_sources():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not _uses_the_chokepoint_at_spawn_time(tree):
            continue
        if _calls_raw_which(tree) or _bare_existence_check_without_dirname_guard(tree):
            hits.append(rel)
    return hits


def test_no_gate_calls_raw_which_when_the_spawn_is_already_cwd_safe() -> None:
    """Scope note (#2606): this register only checks whether a NAMED gate
    expression (a raw `shutil.which()` call, or a bare existence check for
    a name a chokepoint call also resolves) disagrees with a spawn already
    routed through the chokepoint. It says nothing about a file's argv
    construction directly -- a bare, entirely ungated literal at argv[0]
    (no gate of any shape, the #2605 phpstan.py shape) is invisible to
    this walker by design and is `tests/test_bare_argv0_construction_2605.py`'s
    job instead. A green result here is "this adapter's own gate, if it
    has one, agrees with its spawn" -- not "no bare-argv[0] literal exists
    anywhere in this file"."""
    offenders = _offenders()
    assert not offenders, (
        "these adapters spawn through the cwd-excluding chokepoint "
        "(argv0()/spawnable()/resolve_bin_cmd()) but still gate on raw "
        "shutil.which(), or on a bare Path(X).exists()/os.path.isfile(X) "
        "check with no os.path.dirname(X) guard (#2602) -- either one "
        "searches a DIFFERENT path than the spawn (#2579): a repo-planted "
        "shim passes the gate and then either fails to spawn (POSIX) or "
        "is spawned from the current directory (Windows, #2602). Gate on "
        "spawnable(name) and already_a_path(name) instead:\n  " + "\n  ".join(offenders)
    )


def test_the_register_catches_a_bare_existence_gate_with_no_dirname_guard() -> None:
    """Positive control for the #2602 widening.

    This is the exact shape all seven #2602 sites had before the fix:
    a chokepoint-routed spawn (argv0/spawnable), gated on `spawnable()`
    OR a bare `Path(X).exists() and os.access(X, os.X_OK)` check with no
    dirname guard. The old walker (raw `shutil.which()` only) was blind
    to this -- it does not call `which()` at all -- which is exactly why
    #2602 shipped past #2579's own register.
    """
    vulnerable = (
        "import os, pathlib, subprocess\n"
        "from spawnable import argv0, spawnable\n"
        "if not spawnable(TOOL) and not (\n"
        "    pathlib.Path(TOOL).exists() and os.access(TOOL, os.X_OK)\n"
        "):\n"
        "    absent()\n"
        "cmd = [argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(vulnerable)
    assert _uses_the_chokepoint_at_spawn_time(tree)
    assert _bare_existence_check_without_dirname_guard(tree), (
        "the widened walker does not see a bare Path(X).exists() check "
        "with no dirname guard, alongside a chokepoint-routed spawn -- "
        "the exact shape #2602 fixed at seven call sites"
    )
    assert not _calls_raw_which(tree), (
        "this fixture must not also trip the OLD detector -- it is "
        "testing the NEW one in isolation"
    )


def test_the_is_file_spelling_is_also_caught() -> None:
    """`pathlib.Path.is_file()` is a third spelling of the same existence
    check `.exists()`/`os.path.isfile()` already cover -- auditor finding
    on #2602's own review: the walker originally recognised only
    `("exists", "isfile")` and a future adapter reintroducing the pattern
    spelled `Path(X).is_file()` would have gone unseen.
    """
    vulnerable = (
        "import os, pathlib, subprocess\n"
        "from spawnable import argv0, spawnable\n"
        "if not spawnable(TOOL) and not (\n"
        "    pathlib.Path(TOOL).is_file() and os.access(TOOL, os.X_OK)\n"
        "):\n"
        "    absent()\n"
        "cmd = [argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(vulnerable)
    assert _uses_the_chokepoint_at_spawn_time(tree)
    assert _bare_existence_check_without_dirname_guard(tree), (
        "the walker still cannot see the .is_file() spelling of the same "
        "bare existence check"
    )


def test_a_dirname_guarded_existence_gate_is_not_flagged() -> None:
    """Positive control's mirror: the same shape, fixed, must clear."""
    fixed = (
        "import os, pathlib, subprocess\n"
        "from spawnable import already_a_path, argv0, spawnable\n"
        "if not spawnable(TOOL) and not already_a_path(TOOL):\n"
        "    absent()\n"
        "cmd = [argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(fixed)
    assert _uses_the_chokepoint_at_spawn_time(tree)
    assert not _bare_existence_check_without_dirname_guard(tree), (
        "a gate that calls already_a_path() instead of reimplementing the "
        "check inline must not be flagged -- there is no bare exists()/"
        "isfile() call left in this source to see"
    )
    inline_but_guarded = (
        "import os, pathlib, subprocess\n"
        "from spawnable import argv0, spawnable\n"
        "if not spawnable(TOOL) and not (\n"
        "    os.path.dirname(TOOL) and pathlib.Path(TOOL).exists()\n"
        "    and os.access(TOOL, os.X_OK)\n"
        "):\n"
        "    absent()\n"
        "cmd = [argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree2 = ast.parse(inline_but_guarded)
    assert not _bare_existence_check_without_dirname_guard(tree2), (
        "an inline existence check that DOES carry a dirname guard in "
        "the same and-chain must not be flagged"
    )


def test_the_register_can_actually_see_the_defect() -> None:
    """Positive control, same shape as #2540's own register.

    Without this, the assertion above also passes when the walker is
    broken, matches nothing, or is pointed at an empty tree -- which is
    this repository's recurring defect class wearing a test's clothes.
    """
    bad = (
        "import shutil, subprocess\n"
        "from spawnable import argv0\n"
        "if not shutil.which(TOOL):\n"
        "    absent()\n"
        "cmd = [argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(bad)
    assert _calls_raw_which(tree), "the shutil.which() walker is blind"
    assert _uses_the_chokepoint_at_spawn_time(tree), (
        "the chokepoint-import walker is blind"
    )


def test_bare_imported_isfile_with_no_and_chain_is_caught() -> None:
    """#2606 bullet 2: `from os.path import isfile; if isfile(X):`, with
    no surrounding `and`-chain at all, was invisible to the old walker --
    it only recognised an `ast.Attribute` call (`os.path.isfile(X)`)
    inside an `ast.BoolOp(And)`. Neither holds here: `isfile` is a bare
    `ast.Name` (imported directly) and there is no `and`-chain for a
    dirname guard to ever live in.
    """
    vulnerable = (
        "import subprocess\n"
        "from os.path import isfile\n"
        "from spawnable import argv0, spawnable\n"
        "if not spawnable(TOOL):\n"
        "    if not isfile(TOOL):\n"
        "        absent()\n"
        "cmd = [argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(vulnerable)
    assert _uses_the_chokepoint_at_spawn_time(tree)
    assert _bare_existence_check_without_dirname_guard(tree), (
        "a bare, directly-imported isfile(X) with no and-chain at all "
        "must still be caught"
    )


def test_bare_path_exists_with_no_and_chain_is_caught() -> None:
    """#2606 bullet 2's other half: `if Path(X).exists():` alone, with no
    surrounding `and`, was invisible because the old walker only ever
    looked inside an `ast.BoolOp(And)`.
    """
    vulnerable = (
        "import subprocess, pathlib\n"
        "from spawnable import argv0, spawnable\n"
        "if not spawnable(TOOL):\n"
        "    if not pathlib.Path(TOOL).exists():\n"
        "        absent()\n"
        "cmd = [argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(vulnerable)
    assert _uses_the_chokepoint_at_spawn_time(tree)
    assert _bare_existence_check_without_dirname_guard(tree), (
        "a bare Path(X).exists() with no surrounding and-chain at all "
        "must still be caught"
    )


def test_a_dirname_call_nested_inside_bool_is_not_a_false_positive() -> None:
    """#2606 bullet 3: the old walker only recognised a *direct* `.dirname`
    attribute call among a BoolOp's immediate values, so
    `bool(os.path.dirname(X)) and ...` -- the exact spelling
    `spawnable._already_a_path` itself uses -- registered as an OFFENDER
    even though the expression IS guarded.
    """
    fine = (
        "import os, pathlib, subprocess\n"
        "from spawnable import argv0, spawnable\n"
        "if not spawnable(TOOL) and not (\n"
        "    bool(os.path.dirname(TOOL)) and pathlib.Path(TOOL).exists()\n"
        "    and os.access(TOOL, os.X_OK)\n"
        "):\n"
        "    absent()\n"
        "cmd = [argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(fine)
    assert _uses_the_chokepoint_at_spawn_time(tree)
    assert not _bare_existence_check_without_dirname_guard(tree), (
        "a dirname() call nested inside bool(...) still guards the "
        "expression -- this must not be flagged as an offender"
    )


def test_a_dirname_call_on_an_unrelated_name_does_not_guard_anything() -> None:
    """Second self-review round, both spawned reviewers independently: a
    `dirname(...)` call only guarantees the guard for the SAME name the
    existence check is testing -- `dirname(OTHER) and Path(TOOL).exists()`
    must not read as "TOOL's existence check is guarded" just because
    some unrelated dirname() call sits in the same expression. This gap
    predates this file's #2606 rewrite (the pre-diff `_contains_dirname_call`
    had the identical shape: it matched on the dirname *function name*
    only, never on its argument), and neither the earlier widening nor
    the AND/OR truth-table rewrite closed it until now.
    """
    vulnerable = (
        "import os, pathlib, subprocess\n"
        "from spawnable import argv0, spawnable\n"
        "OTHER = 'some/other/thing'\n"
        "if not spawnable(TOOL) and not (\n"
        "    os.path.dirname(OTHER) and pathlib.Path(TOOL).exists()\n"
        "):\n"
        "    absent()\n"
        "cmd = [argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(vulnerable)
    assert _uses_the_chokepoint_at_spawn_time(tree)
    assert _bare_existence_check_without_dirname_guard(tree), (
        "a dirname() call that never mentions the name being existence-"
        "checked must not be read as guarding that check"
    )


def test_a_dirname_call_that_merely_mentions_the_name_is_not_enough() -> None:
    """Third self-review round, both reviewers independently: a subtree
    walk over a `dirname(...)` call's ENTIRE argument -- rather than
    requiring the argument to BE the checked name -- accepted
    `dirname(LOOKUP[TOOL])` and a never-taken ternary branch as guards on
    `TOOL`, just because a `Name(TOOL)` node happened to appear somewhere
    inside the argument's own subtree. Neither call actually takes
    dirname of the checked value at runtime.
    """
    dict_lookup = (
        "import os, pathlib, subprocess\n"
        "from spawnable import argv0, spawnable\n"
        "LOOKUP = {'TOOL': 'unrelated/other/path'}\n"
        "if not spawnable(TOOL) and not (\n"
        "    os.path.dirname(LOOKUP[TOOL]) and pathlib.Path(TOOL).exists()\n"
        "):\n"
        "    absent()\n"
        "cmd = [argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(dict_lookup)
    assert _uses_the_chokepoint_at_spawn_time(tree)
    assert _bare_existence_check_without_dirname_guard(tree), (
        "dirname(LOOKUP[TOOL]) never takes dirname of TOOL itself -- "
        "must not be read as a guard on TOOL just for mentioning it"
    )

    never_taken_ternary = (
        "import os, pathlib, subprocess\n"
        "from spawnable import argv0, spawnable\n"
        "if not spawnable(TOOL) and not (\n"
        "    os.path.dirname(OTHER if TOOL == TOOL else OTHER)\n"
        "    and pathlib.Path(TOOL).exists()\n"
        "):\n"
        "    absent()\n"
        "cmd = [argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree2 = ast.parse(never_taken_ternary)
    assert _bare_existence_check_without_dirname_guard(tree2), (
        "dirname(...) always evaluates to dirname(OTHER) here -- TOOL "
        "only appears in the ternary's condition, never its value -- "
        "must not be read as a guard on TOOL"
    )


def test_a_dirname_call_gating_a_different_chokepoint_name_does_not_cross_guard() -> None:
    """oss:auditor, third round: `dirname(A or B) and isfile(B)` must not
    be read as a guard on `B` -- `dirname`'s argument subtree contains the
    name `B`, but at runtime `dirname` only ever receives whichever of
    `A`/`B` is truthy first, which is not a sound proof that `B` was
    dirname-checked at all.
    """
    vulnerable = (
        "import os, pathlib, subprocess\n"
        "from spawnable import argv0, spawnable\n"
        "if not spawnable(A) and not spawnable(B) and not (\n"
        "    os.path.dirname(A or B) and pathlib.Path(B).exists()\n"
        "):\n"
        "    absent()\n"
        "cmd = [argv0(A), argv0(B)]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(vulnerable)
    assert _uses_the_chokepoint_at_spawn_time(tree)
    assert _bare_existence_check_without_dirname_guard(tree), (
        "dirname(A or B) does not soundly guard B's existence check -- "
        "must still be caught"
    )


def test_an_existence_check_reachable_via_or_is_still_caught() -> None:
    """#2606, second self-review round: `dirname(X) or Path(X).exists()`
    is exactly as unguarded as no dirname check at all -- an `or` needs
    only ONE operand true, so a bare relative name matching in cwd makes
    the whole expression true regardless of dirname. An earlier draft of
    this walker climbed only to the NEAREST enclosing `BoolOp(And)`,
    which for THIS shape is a level further out and happens to contain a
    `dirname` call, so it read this as guarded when it is not.
    """
    vulnerable = (
        "import subprocess, os, pathlib\n"
        "from spawnable import argv0, spawnable\n"
        "if not spawnable(TOOL) and not (\n"
        "    os.path.dirname(TOOL) or pathlib.Path(TOOL).exists()\n"
        "):\n"
        "    absent()\n"
        "cmd = [argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(vulnerable)
    assert _uses_the_chokepoint_at_spawn_time(tree)
    assert _bare_existence_check_without_dirname_guard(tree), (
        "an existence check reachable via `or` without dirname also being "
        "required must still be caught"
    )


def test_a_dirname_guarded_or_branch_is_not_a_false_positive() -> None:
    """Mirror of the test above: `dirname(X) and (Y or (Z and
    Path(X).exists()))` IS safe -- the existence check can only be
    reached once `dirname(X)` has already been required true, however
    deep the `or` nesting between them. An earlier draft flagged this,
    because the NEAREST enclosing `BoolOp(And)` is the inner `(Z and
    ...)`, which does not itself contain `dirname`.
    """
    fine = (
        "import subprocess, os, pathlib\n"
        "from spawnable import argv0, spawnable\n"
        "if not spawnable(TOOL) and not (\n"
        "    os.path.dirname(TOOL) and (Y or (Z and pathlib.Path(TOOL).exists()))\n"
        "):\n"
        "    absent()\n"
        "cmd = [argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(fine)
    assert _uses_the_chokepoint_at_spawn_time(tree)
    assert not _bare_existence_check_without_dirname_guard(tree), (
        "an existence check nested inside an `or` that is itself gated "
        "by a required dirname() one level further out must not be "
        "flagged -- it cannot be reached at all unless dirname was "
        "already true"
    )


def test_a_module_style_chokepoint_import_is_still_seen() -> None:
    """`import spawnable` + `spawnable.argv0(...)` is a second way to reach
    the chokepoint that `from spawnable import argv0` does not surface as a
    bare name (auditor review, #2579). No shipped adapter is written this
    way today, but the walker must not go blind if one ever is.
    """
    bad = (
        "import shutil, subprocess, spawnable\n"
        "if not shutil.which(TOOL):\n"
        "    absent()\n"
        "cmd = [spawnable.argv0(TOOL)]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(bad)
    assert _calls_raw_which(tree)
    assert _uses_the_chokepoint_at_spawn_time(tree), (
        "the module-style import walker is blind"
    )


def test_a_gate_with_no_chokepoint_spawn_is_not_flagged() -> None:
    """Negative control: an adapter with no argv0/spawnable/resolve_bin_cmd
    import at all -- phpstan's own shape before #2581 fixed it, kept here
    as a synthetic snippet now that the real file no longer matches it --
    must not be flagged just for calling `shutil.which()`: there is
    nothing for its gate to disagree with.
    """
    fine = (
        "import shutil, subprocess, os\n"
        "if not shutil.which(TOOL) and not (os.path.dirname(TOOL) and Path(TOOL).exists()):\n"
        "    absent()\n"
        "cmd = [\"php\", TOOL, \"analyse\"]\n"
        "subprocess.run(cmd)\n"
    )
    tree = ast.parse(fine)
    assert _calls_raw_which(tree)
    assert not _uses_the_chokepoint_at_spawn_time(tree), (
        "the negative control itself imports the chokepoint -- it is not "
        "testing what it claims to"
    )
    assert not _bare_existence_check_without_dirname_guard(tree), (
        "the negative control's own existence check has a dirname guard "
        "(os.path.dirname(TOOL)) -- it must not be flagged by the #2602 "
        "detector either. Before this fix the fixture was the literal "
        "vulnerable shape (Path(TOOL).exists(), no dirname guard) and "
        "this test still called it \"fine\" -- correct for the narrow "
        "claim it was making (no chokepoint import) but a fixture that "
        "IS the exact defect #2602 reports should not be the one this "
        "register holds up as an example of a clean gate."
    )


def test_the_register_covers_a_population_it_can_name() -> None:
    """A register over zero files is green and means nothing."""
    sources = _adapter_sources()
    assert len(sources) >= 40, (
        f"only {len(sources)} adapter sources found under "
        f"{ADAPTER_DIRS} -- the walk root is wrong, and an empty walk "
        "reads exactly like a clean one"
    )
    with_chokepoint = [
        p for p in sources
        if _uses_the_chokepoint_at_spawn_time(ast.parse(p.read_text(encoding="utf-8")))
    ]
    assert with_chokepoint, (
        "no adapter imports argv0/spawnable/resolve_bin_cmd at all, so this "
        "register is asserting nothing about anything"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
