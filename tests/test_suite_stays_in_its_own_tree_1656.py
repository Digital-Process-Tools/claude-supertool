"""The suite must not write outside its own tree — three instances (#1656).

`tests/conftest.py` has set `SUPERTOOL_READ_NO_ELIDE` and `SUPERTOOL_GC_DISABLE`
by `setdefault` since #1329 and #474, both for one stated reason: a test run must
not have its behaviour depend on, or its side effects land in, the operator's
environment. Three sites did not follow it.

1. **The `paste-backup` store (#1650).** Measured after one full 12,938-test
   run: 52 files, 288 KB written into the developer's real
   `~/.cache/supertool/paste-backup/`. Not `tmp_path` — the operator's actual
   cache, on a machine where that cache is also the thing under test.
2. **`tests/test_at_file_mixed_args.py:39,68`** created and unlinked `REPO/'@-'`
   in the live checkout root. Two runs in one checkout race on that path, and
   that checkout is typically symlinked as the operator's live `supertool`.
3. **`tests/test_vim_kevin_fixes.py:1207,1292`** assigned `os.environ`
   directly and `pop`-ed it in the teardown rather than restoring it.

## The repair, and why the other two lose

`XDG_CACHE_HOME` is redirected suite-wide, at one line in `conftest.py`, because
`_supertool._cache_root()` is the single place `~/.cache/supertool` is spelled
and it reads the variable per call.

- **An env gate on the backup (`SUPERTOOL_PASTE_NO_BACKUP=1`) loses** because it
  ships a *product* knob that silently disables a data-loss net in order to fix a
  test-hygiene defect. Keeping the receipt honest under it would need a fourth
  receipt state — `paste` saying it wrote without a snapshot and why — or the
  receipt goes back to being the thing #1650 exists to fix. It also fixes one
  instance of three.
- **A test-only root that is not an env knob loses** because `_cache_root()`
  takes its answer from the environment; anything else is either a second env
  var, which is the bullet above, or a product-visible hook that exists only for
  the suite. Both add product surface the redirect does not.
- **The redirect's blast radius is smaller than it looks, and that is measured
  rather than asserted.** It moves three stores, and `conftest.py` already
  disables all three for this same reason — `SUPERTOOL_READ_NO_ELIDE` (#1329),
  `SUPERTOOL_GC_DISABLE` (#474) and `SUPERTOOL_VIM_NO_PERSIST`, each a
  `setdefault` in `pytest_configure`. What the redirect adds is a floor under
  those three switches instead of a fourth one beside them. (Named rather than
  cited by line: a line number into a live file decays on every edit above it,
  silently, and `conftest.py` is edited often.)

**Instance 3 had to be fixed first, and that is the ordering, not a detail.**
Those two sites `os.environ.pop("XDG_CACHE_HOME", None)` in their teardown. A pop
is not a restore: with the suite-wide redirect in place, the first such test to
run would delete it and every test after it in that worker would fall back to the
operator's real `~/.cache` — the redirect silently ending mid-session, which is
this repo's own defect class wearing the fix for it. `monkeypatch.setenv`
restores the previous value, so the redirect survives.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import supertool

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"

#: The store the operator would lose to a `gc` fired from a test run, and the
#: one `_cache_root()` returns when nothing redirects it.
REAL_CACHE = Path.home() / ".cache" / "supertool"


def _under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return True


# ---------------------------------------------------------------------------
# instance 1 — the cache root, and the paste-backup store inside it
# ---------------------------------------------------------------------------

def test_the_cache_root_this_run_uses_is_not_the_operators() -> None:
    """One assertion covering every store `_cache_root()` owns, present and
    future — a new one added tomorrow inherits the redirect instead of needing
    a fourth switch beside the three already in `conftest.py`."""
    assert os.environ.get("XDG_CACHE_HOME"), (
        "the suite runs without XDG_CACHE_HOME redirected, so every cache "
        "supertool writes lands in the operator's real ~/.cache")
    root = supertool._cache_root()
    assert not _under(root, REAL_CACHE), (
        "cache root for this run is " + str(root) + ", inside the operator's "
        "own " + str(REAL_CACHE))


def test_a_paste_over_an_existing_file_backs_up_inside_the_suites_own_cache(
        tmp_path: Path) -> None:
    """The measured instance: 52 files, 288 KB per full run (#1650, #1656).

    Driven through the real op rather than asserted about `_cache_root()`, so
    it fails if the store ever stops going through that one choke point.
    """
    target = tmp_path / "note.md"
    target.write_text("the bytes that get displaced\n", encoding="utf-8")

    store = supertool._cache_root() / "paste-backup"
    before = set(store.glob("*")) if store.is_dir() else set()
    supertool.dispatch("paste:::" + str(target) + ":::replacement\n")
    after = set(store.glob("*")) if store.is_dir() else set()

    written = after - before
    assert written, "paste over an existing file wrote no snapshot into " + str(store)
    for snapshot in written:
        assert not _under(snapshot, REAL_CACHE), (
            "a test wrote " + str(snapshot) + " into the operator's real cache")


# ---------------------------------------------------------------------------
# instance 2 — the live checkout root is not a scratch directory
# ---------------------------------------------------------------------------

#: Path methods that create, change or remove the file they are called on.
#: `REPO / "supertool.py"` handed to `subprocess.run` is a read and stays
#: allowed — the defect is scratch space in the checkout, not the checkout.
MUTATORS = ("unlink", "write_text", "write_bytes", "touch", "mkdir", "rmdir",
            "rename", "replace", "chmod", "symlink_to", "hardlink_to")


def _mutated_repo_root_paths(module: Path) -> list:
    """Names bound to `REPO / "<literal>"` that the module then writes or removes."""
    tree = ast.parse(module.read_text(encoding="utf-8", errors="replace"))
    bound = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target, value = node.targets[0], node.value
        if not isinstance(target, ast.Name):
            continue
        if not (isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div)):
            continue
        if not (isinstance(value.left, ast.Name)
                and value.left.id in ("REPO", "REPO_ROOT")):
            continue
        if isinstance(value.right, ast.Constant) and isinstance(value.right.value, str):
            bound.setdefault(target.id, (node.lineno, value.right.value))

    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in MUTATORS:
            continue
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id in bound:
            found.append((node.lineno, bound[owner.id][1], node.func.attr))
    return found


def test_the_at_file_module_does_not_build_scratch_paths_in_the_checkout() -> None:
    """`REPO / '@-'`, created and unlinked in the live checkout root.

    Lexical, and deliberately so: the defect is *which directory the test
    chose*, and a directory choice has no runtime signature until the separate
    `@-` bug regresses and two concurrent runs unlink each other's file. The
    fix is to spawn from `tmp_path`, which makes the assertion say the same
    thing about a directory the test owns.
    """
    module = TESTS / "test_at_file_mixed_args.py"
    offenders = _mutated_repo_root_paths(module)
    assert not offenders, (
        "the live checkout root is not scratch space — a second run of the "
        "suite in the same checkout races on these paths, and this checkout is "
        "typically symlinked as the operator's `supertool` (#1656):\n  "
        + "\n  ".join("line " + str(ln) + ": " + how + "() on REPO / " + repr(name)
                      for ln, name, how in offenders))


# ---------------------------------------------------------------------------
# instance 3 — nothing may reach past monkeypatch for the redirected variable
# ---------------------------------------------------------------------------

GUARDED_VAR = "XDG_CACHE_HOME"


def _raw_env_touches(module: Path, var: str) -> list:
    """`os.environ[var] = ...` and `os.environ.pop(var)`, outside monkeypatch."""
    tree = ast.parse(module.read_text(encoding="utf-8", errors="replace"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Subscript)
                        and ast.unparse(target.value).endswith("environ")
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == var):
                    found.append((node.lineno, "assigned"))
        if isinstance(node, ast.Call):
            func = ast.unparse(node.func)
            if (func.endswith("environ.pop") or func.endswith("environ.__delitem__")
                    or func.endswith("environ.setdefault")):
                if (node.args and isinstance(node.args[0], ast.Constant)
                        and node.args[0].value == var):
                    found.append((node.lineno, func.rsplit(".", 1)[-1]))
    return found


def test_no_test_module_reaches_past_monkeypatch_for_the_redirected_cache() -> None:
    """A `pop` in a teardown is not a restore.

    `conftest.py` owns this variable for the whole session now. A module that
    sets it by hand and pops it afterwards un-redirects every test that runs
    after it in that worker, back onto the operator's real cache — silently,
    and in the direction that reads as a pass. `monkeypatch.setenv` restores
    whatever was there, which is why it is the only permitted route.
    """
    offenders = []
    scanned = 0
    for module in sorted(TESTS.rglob("*.py")):
        if module.name == "conftest.py":
            continue
        scanned += 1
        for lineno, how in _raw_env_touches(module, GUARDED_VAR):
            offenders.append(module.relative_to(REPO).as_posix() + ":"
                             + str(lineno) + " " + how + " " + GUARDED_VAR)
    assert scanned >= 100, (
        "only " + str(scanned) + " test modules scanned — the sweep is matching "
        "nothing and would pass vacuously")
    assert not offenders, (
        "use monkeypatch.setenv/delenv: it restores the value conftest set for "
        "the whole session, and a bare pop leaves every later test in this "
        "worker writing to the operator's real cache (#1656):\n  "
        + "\n  ".join(offenders))
