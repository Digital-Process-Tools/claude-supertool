"""Every *mutated* module-level global under `presets/` has a declared lifetime (#686).

`conftest.RESET_GLOBALS` and the guard in
`test_state_reset_and_lint_timeout.py` both resolve their names against the
`supertool` module object. They are therefore bound to one file, and nothing in
a green run said so — which is the defect #686 reports: the scope of the check
was invisible in its result, so a reader concluded `presets/` was covered.

**Why this scans for mutation rather than for mutability.** `presets/` holds 43
module-level dicts/lists/sets. Thirty-nine of them are constant lookup tables
that merely happen to be mutable types — `_FLAGS`, `_CHECK_GLYPH`,
`_TERMINAL_PR_STATES`. A guard that made every one of those carry a registry
entry would be 91% noise, and a guard that is mostly noise gets exempted
reflexively, which is how a guard stops working. So the question asked here is
not "could this be mutated" but "**is** it mutated, from inside a function, at
run time". Four names answer yes. Each one is a genuine lifetime decision, and
each is required to state which of the repo's two mechanisms holds it:

  1. **conftest resets it** — `conftest.PRESET_RESET_GLOBALS`. Available only to
     preset modules conftest actually imports (today: `_env`, shared by 29
     presets, which is why #689's macOS-only red existed at all). Snapshotting
     all 125 preset modules per test to extend this is the cost #686 declined.
  2. **the module resets it at its own entry point** — the pattern
     `presets/git/status.py` and `presets/git/push.py` already use. Declared in
     `conftest.PRESET_SELF_CLEARING_GLOBALS` and then **verified** here: the
     claim is only accepted if the name is really reset in `main()`'s prologue.
     A declaration nobody checks is a comment.

Anything else must be named in `conftest.PRESET_RESET_EXEMPT_GLOBALS` with a
reason. That table is empty on purpose.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PRESETS_ROOT = REPO_ROOT / "presets"

#: Methods that mutate the receiver in place. Calling one of these on a
#: module-level name is what turns "a dict at module scope" into "state".
MUTATING_METHODS = frozenset({
    "add", "append", "extend", "insert", "remove", "pop", "clear",
    "update", "setdefault", "discard", "sort", "reverse", "popitem",
})

_MUTABLE_LITERALS = (ast.Dict, ast.List, ast.Set,
                     ast.DictComp, ast.ListComp, ast.SetComp)
_MUTABLE_CALLS = frozenset({"set", "dict", "list"})

#: Statements that end `main()`'s prologue. A reset placed after one of these is
#: conditional, and a conditional reset is not a lifetime.
_CONTROL_FLOW: tuple[type, ...] = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try,
    ast.With, ast.AsyncWith, ast.Return,
) + ((ast.Match,) if hasattr(ast, "Match") else ())


def module_level_mutables(tree: ast.Module) -> dict[str, int]:
    """Names bound at module scope to a mutable container, -> line number."""
    found: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
            value = node.value
        else:
            continue
        if value is None:
            continue
        mutable = isinstance(value, _MUTABLE_LITERALS) or (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in _MUTABLE_CALLS
        )
        if mutable:
            for target in targets:
                found[target.id] = node.lineno
    return found


def _mutations_in(node: ast.AST, names: set[str]) -> list[tuple[str, str, int]]:
    """`(name, how, lineno)` for every in-place mutation of `names` under `node`."""
    hits: list[tuple[str, str, int]] = []
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id in names
                and sub.func.attr in MUTATING_METHODS):
            hits.append((sub.func.value.id, f"{sub.func.attr}()", sub.lineno))
        if isinstance(sub, (ast.Assign, ast.AugAssign)):
            targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
            for target in targets:
                if (isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id in names):
                    hits.append((target.value.id, "item assignment", sub.lineno))
        if isinstance(sub, ast.Delete):
            for target in sub.targets:
                if (isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id in names):
                    hits.append((target.value.id, "item deletion", sub.lineno))
    return hits


def runtime_mutations(tree: ast.Module, names: set[str]) -> dict[str, list[tuple[str, int]]]:
    """Mutations that happen when a function runs, not while the module imports.

    Module-scope mutation is initialisation — a table built in a loop beside its
    own literal is still a constant. Only what a call can reach is state.
    """
    per_name: dict[str, list[tuple[str, int]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for name, how, lineno in _mutations_in(node, names):
            per_name.setdefault(name, []).append((how, lineno))
    for sites in per_name.values():
        sites.sort(key=lambda site: site[1])
    return per_name


def _is_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Dict, ast.List, ast.Set, ast.Tuple)):
        return all(_is_literal(child) for child in ast.iter_child_nodes(node)
                   if isinstance(child, ast.expr))
    return False


def _main_prologue(tree: ast.Module) -> list[ast.stmt] | None:
    """`main()`'s straight-line opening, up to its first branch/loop/try/return.

    A reset has to happen before the work does. `_RUNTIME_HINT[0] = base` sits
    150 lines inside `presets/mcp/stop.py::main()` and is the *use*, not a
    reset; accepting "mutated somewhere in main" would have waved it through.
    """
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main":
            prologue: list[ast.stmt] = []
            for stmt in node.body:
                if isinstance(stmt, _CONTROL_FLOW):
                    break
                prologue.append(stmt)
            return prologue
    return None


def resets_in_main_prologue(tree: ast.Module, names: set[str]) -> set[str]:
    """Of `names`, the ones provably restored to a literal in `main()`'s prologue."""
    prologue = _main_prologue(tree)
    if prologue is None:
        return set()
    reset: set[str] = set()
    for stmt in prologue:
        for sub in ast.walk(stmt):
            if (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id in names):
                if sub.func.attr == "clear":
                    reset.add(sub.func.value.id)
                elif (sub.func.attr in {"update", "extend"}
                      and len(sub.args) == 1 and _is_literal(sub.args[0])):
                    reset.add(sub.func.value.id)
            if isinstance(sub, ast.Assign) and _is_literal(sub.value):
                for target in sub.targets:
                    if (isinstance(target, ast.Subscript)
                            and isinstance(target.value, ast.Name)
                            and target.value.id in names):
                        reset.add(target.value.id)
    return reset


def scan_preset_state(root: Path) -> dict[str, dict[str, list[tuple[str, int]]]]:
    """`{relative path: {name: [(how, line), ...]}}` for run-time-mutated globals."""
    state: dict[str, dict[str, list[tuple[str, int]]]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        candidates = module_level_mutables(tree)
        if not candidates:
            continue
        mutated = runtime_mutations(tree, set(candidates))
        if mutated:
            state[path.relative_to(root.parent).as_posix()] = mutated
    return state


def unaccounted_preset_state(
    root: Path,
    reset: dict[str, tuple[str, ...]],
    self_clearing: dict[str, tuple[str, ...]],
    exempt: dict[str, tuple[str, ...]],
) -> list[str]:
    """Human-readable complaints. Empty means every mutated global has an owner."""
    problems: list[str] = []
    for rel, names in sorted(scan_preset_state(root).items()):
        tree = ast.parse((root.parent / rel).read_text(encoding="utf-8"))
        claimed = set(self_clearing.get(rel, ()))
        verified = resets_in_main_prologue(tree, claimed) if claimed else set()
        for name in sorted(claimed - verified):
            problems.append(
                f"{rel}::{name} is declared self-clearing in "
                f"conftest.PRESET_SELF_CLEARING_GLOBALS, but main() does not "
                f"reset it before its first branch — the declaration is not true"
            )
        for name, sites in sorted(names.items()):
            if name in reset.get(rel, ()) or name in exempt.get(rel, ()):
                continue
            if name in verified:
                continue
            where = ", ".join(f"{how} L{line}" for how, line in sites)
            problems.append(
                f"{rel}::{name} is module-level state mutated at run time "
                f"({where}) with no declared lifetime"
            )
    return problems


def stale_declarations(
    root: Path,
    tables: dict[str, dict[str, tuple[str, ...]]],
) -> list[str]:
    """Declarations naming something that is no longer run-time-mutated state.

    A registry that outlives its subject rots quietly: the entry stays, reads as
    a considered decision, and describes nothing. Same two-way boundary as
    #654's sweep allowlist.
    """
    live = scan_preset_state(root)
    stale: list[str] = []
    for table_name, table in sorted(tables.items()):
        for rel, names in sorted(table.items()):
            for name in names:
                if name not in live.get(rel, {}):
                    stale.append(
                        f"conftest.{table_name}[{rel!r}] names {name}, which is "
                        f"no longer run-time-mutated state there"
                    )
    return stale


def _conftest_tables():
    import conftest
    return (conftest.PRESET_RESET_GLOBALS,
            conftest.PRESET_SELF_CLEARING_GLOBALS,
            conftest.PRESET_RESET_EXEMPT_GLOBALS)


# ---------------------------------------------------------------------------
# The guard itself.
# ---------------------------------------------------------------------------

def test_every_mutated_preset_global_has_a_declared_lifetime() -> None:
    reset, self_clearing, exempt = _conftest_tables()
    problems = unaccounted_preset_state(PRESETS_ROOT, reset, self_clearing, exempt)
    assert problems == [], (
        "module-level state under presets/ with no declared lifetime (#686).\n"
        + "\n".join(f"  - {p}" for p in problems)
        + "\n\nPick one: reset it in main()'s prologue and name it in "
          "conftest.PRESET_SELF_CLEARING_GLOBALS (the presets/git/status.py "
          "pattern); or, if conftest imports the module, add it to "
          "conftest.PRESET_RESET_GLOBALS; or exempt it in "
          "conftest.PRESET_RESET_EXEMPT_GLOBALS with a reason."
    )


def test_the_declared_lifetimes_still_describe_something_that_exists() -> None:
    reset, self_clearing, exempt = _conftest_tables()
    stale = stale_declarations(PRESETS_ROOT, {
        "PRESET_RESET_GLOBALS": reset,
        "PRESET_SELF_CLEARING_GLOBALS": self_clearing,
        "PRESET_RESET_EXEMPT_GLOBALS": exempt,
    })
    assert stale == [], "\n".join(stale)


def test_the_scan_still_sees_the_presets_tree() -> None:
    """Guards that silently scan nothing are the failure class this repo tracks.

    If `presets/` moved, every assertion above would go green by finding
    nothing. Two of the four known globals are pinned here by name so that a
    scan reaching zero files fails instead of passing.
    """
    live = scan_preset_state(PRESETS_ROOT)
    assert "_ANNOUNCED" in live.get("presets/_env.py", {})
    assert "_UNANSWERED" in live.get("presets/git/status.py", {})


# ---------------------------------------------------------------------------
# Watching the guard fire. A guard nobody has seen catch anything is faith.
# ---------------------------------------------------------------------------

_READ_ONLY_TABLE = '''
_GLYPHS = {"ok": "v", "bad": "x"}


def render(state):
    return _GLYPHS[state]
'''

_UNDECLARED_STATE = '''
_SEEN = set()


def note(message):
    if message in _SEEN:
        return
    _SEEN.add(message)
    print(message)
'''

_SELF_CLEARING = '''
_TALLY = []


def record(item):
    _TALLY.append(item)


def main():
    _TALLY.clear()
    record("x")
    return 0
'''

_FALSE_CLAIM = '''
_HINT = [""]


def hint():
    return _HINT[0]


def main():
    if not hint():
        return 1
    _HINT[0] = "resolved"
    return 0
'''


@pytest.fixture()
def fake_presets(tmp_path: Path) -> Path:
    root = tmp_path / "presets"
    (root / "sub").mkdir(parents=True)
    (root / "table.py").write_text(_READ_ONLY_TABLE, encoding="utf-8")
    (root / "sub" / "undeclared.py").write_text(_UNDECLARED_STATE, encoding="utf-8")
    (root / "sub" / "tally.py").write_text(_SELF_CLEARING, encoding="utf-8")
    (root / "liar.py").write_text(_FALSE_CLAIM, encoding="utf-8")
    return root


def test_a_constant_table_that_is_only_read_is_not_flagged(fake_presets: Path) -> None:
    """The 39-of-43 case. If these were flagged the guard would be all noise."""
    assert "presets/table.py" not in scan_preset_state(fake_presets)


def test_an_undeclared_mutated_global_is_caught(fake_presets: Path) -> None:
    problems = unaccounted_preset_state(fake_presets, {}, {}, {})
    assert any("presets/sub/undeclared.py::_SEEN" in p for p in problems), problems
    assert any("add() L" in p for p in problems), problems


def test_declaring_it_self_clearing_without_clearing_it_is_caught(
    fake_presets: Path,
) -> None:
    problems = unaccounted_preset_state(
        fake_presets, {}, {"presets/sub/undeclared.py": ("_SEEN",)}, {}
    )
    assert any("the declaration is not true" in p for p in problems), problems


def test_a_real_main_prologue_reset_satisfies_the_guard(fake_presets: Path) -> None:
    problems = unaccounted_preset_state(
        fake_presets, {}, {"presets/sub/tally.py": ("_TALLY",)}, {}
    )
    assert not any("tally.py" in p for p in problems), problems


def test_a_reset_that_is_really_the_first_use_is_not_accepted(
    fake_presets: Path,
) -> None:
    """`presets/mcp/stop.py` in miniature: the only write to `_HINT` inside
    `main()` happens after a branch, and is the value being used rather than a
    reset. Accepting "mutated somewhere in main" would pass this."""
    problems = unaccounted_preset_state(
        fake_presets, {}, {"presets/liar.py": ("_HINT",)}, {}
    )
    assert any("the declaration is not true" in p for p in problems), problems


def test_conftest_reset_and_exemption_both_account_for_a_global(
    fake_presets: Path,
) -> None:
    by_reset = unaccounted_preset_state(
        fake_presets, {"presets/sub/undeclared.py": ("_SEEN",)}, {}, {}
    )
    by_exempt = unaccounted_preset_state(
        fake_presets, {}, {}, {"presets/sub/undeclared.py": ("_SEEN",)}
    )
    assert not any("undeclared.py" in p for p in by_reset), by_reset
    assert not any("undeclared.py" in p for p in by_exempt), by_exempt


def test_a_stale_declaration_is_reported(fake_presets: Path) -> None:
    """Both directions of the boundary, watched rather than assumed."""
    gone = stale_declarations(
        fake_presets,
        {"PRESET_RESET_GLOBALS": {"presets/sub/undeclared.py": ("_GONE",)}},
    )
    assert any("names _GONE" in s for s in gone), gone

    renamed_file = stale_declarations(
        fake_presets,
        {"PRESET_RESET_GLOBALS": {"presets/sub/moved.py": ("_SEEN",)}},
    )
    assert any("presets/sub/moved.py" in s for s in renamed_file), renamed_file

    still_there = stale_declarations(
        fake_presets,
        {"PRESET_RESET_GLOBALS": {"presets/sub/undeclared.py": ("_SEEN",)}},
    )
    assert still_there == [], still_there
