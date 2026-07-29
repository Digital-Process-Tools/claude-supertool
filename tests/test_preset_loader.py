"""#555 — a preset `_load` must not outlive its own isolation.

The bug these pin is never in the file that causes it. A permanent slice
assignment over ``sys.path`` in one test module deletes another's path entry,
and the failure surfaces in the victim, on whichever xdist leg happened to pair
them, as ``ModuleNotFoundError`` for a module the victim itself put on the path.
#552 was the live instance (``presets/mcp`` was the collateral); the five here
were the same construct, latent only because their consumers happened to
re-insert on the next ``_load``.

So these assert on **a specific foreign entry surviving**, not merely on
``sys.path`` comparing equal to itself. An equality check alone would pass a
loader that stripped a rival preset and put it back at the wrong index, and —
more importantly — reads as a tautology to the next person. The point is that
somebody else's directory is still there and still importable.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Callable, Iterator

import pytest

from _preset_loader import load_preset_module

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def foreign_entries() -> Iterator[list[str]]:
    """Plant the path entries other test modules rely on, then clean up.

    ``presets/mcp`` is the real #552 victim. ``presets/devto`` and
    ``presets/hashnode`` are what the five strip from each other.
    """
    planted = [str(REPO_ROOT / "presets" / p)
               for p in ("mcp", "devto", "hashnode", "bluesky")]
    for entry in planted:
        sys.path.insert(0, entry)
    try:
        yield planted
    finally:
        # Exactly one removal per insert. Draining every copy would make this
        # fixture the very thing it tests for: `test_mcp_stop_outcome_547.py`
        # puts `presets/mcp` on the path at import time and needs it at run
        # time, and a `while entry in sys.path` cleanup here took it away.
        for entry in planted:
            if entry in sys.path:
                sys.path.remove(entry)


def _loader_of(module_name: str) -> Callable[..., object]:
    """The `_load` helper of an already-collected preset test module."""
    return getattr(importlib.import_module(module_name), "_load")


# The five from #555, plus the one fixed in #552 — same construct, so the same
# bar applies to all six. (module, args to its own `_load`)
PRESET_LOADERS = [
    ("test_hashnode", ("publish",)),
    ("test_devto", ("publish",)),
    ("test_security_devto", ("read",)),
    ("test_bluesky", ("publish",)),
    ("test_security_bluesky", ("read",)),
    ("test_comment_file", ("devto", "comment")),
]


@pytest.mark.parametrize("module_name,args", PRESET_LOADERS,
                         ids=[m for m, _ in PRESET_LOADERS])
def test_load_restores_every_foreign_preset_entry(
    module_name: str, args: tuple, foreign_entries: list[str],
) -> None:
    """No preset `_load` may delete a path entry it did not add."""
    before = sys.path[:]

    _loader_of(module_name)(*args)

    missing = [e for e in foreign_entries if e not in sys.path]
    assert not missing, (
        f"{module_name}._load permanently removed {missing} from sys.path")
    assert sys.path == before, f"{module_name}._load leaked sys.path changes"


def test_shared_loader_restores_foreign_entries(
    foreign_entries: list[str],
) -> None:
    before = sys.path[:]
    load_preset_module("hashnode", "publish", "probe_")
    assert all(e in sys.path for e in foreign_entries)
    assert sys.path == before


def test_shared_loader_resolves_sibling_shims_not_a_rival_preset() -> None:
    """The isolation still has to *work* — restoring must not defeat it.

    ``devto`` and ``hashnode`` both ship an ``_auth``. Loading one preset's
    module while the other's directory sits on ``sys.path`` must bind the
    module's own sibling, which is the entire reason the filter exists.
    """
    hashnode_dir = str(REPO_ROOT / "presets" / "hashnode")
    sys.path.insert(0, hashnode_dir)
    try:
        mod = load_preset_module("devto", "_rest", "probe_")
    finally:
        sys.path.remove(hashnode_dir)
    assert Path(mod.__file__ or "").resolve().parent.name == "devto"


def test_lazy_bare_import_survives_a_preset_load() -> None:
    """The #552 failure mode itself, in one process.

    This is the shape that broke: a module puts a preset directory on the path
    and imports from it *later*, by bare name. If any `_load` in between strips
    the entry, this raises ``ModuleNotFoundError`` — which is precisely what
    ``pytest tests/test_comment_file.py tests/test_mcp_stop_outcome_547.py``
    did on master before #552.
    """
    devto_dir = str(REPO_ROOT / "presets" / "devto")
    sys.path.insert(0, devto_dir)
    try:
        load_preset_module("hashnode", "publish", "probe_")
        importlib.invalidate_caches()
        spec = importlib.util.find_spec("browse")
        assert spec is not None, (
            "a preset load stripped presets/devto; a lazy `import browse` "
            "would now fail with ModuleNotFoundError")
    finally:
        sys.path.remove(devto_dir)
        sys.modules.pop("browse", None)


def test_no_test_module_rewrites_sys_path_wholesale() -> None:
    """The seventh copy has to fail at authoring time, not on a CI leg.

    Assigning a filtered copy over the whole of `sys.path` inside a test module
    is the #552/#555 construct: it can only ever *remove* entries somebody else
    owns, and the damage lands in a different file on a scheduler-dependent leg.
    Every legitimate need is served by `_preset_loader.load_preset_module`,
    which does the same isolation and puts the path back.

    The scan deliberately has no self-exemption — this file describes the
    pattern in prose rather than quoting it, so that an exclusion list never
    becomes the place a real offender hides.

    This is a source scan rather than a runtime `sys.path`-unchanged fixture on
    purpose. Roughly fifteen test modules insert into `sys.path` at import time
    and never remove; a runtime equality guard would be red on all of them, and
    the additions are not the bug. A scan for the assignment form flags exactly
    the operation that deletes, at the moment somebody writes it.
    """
    tests_dir = Path(__file__).resolve().parent
    # Assembled at runtime so this file is not its own first offender — the
    # alternative is an exclusion list, and an exclusion list is where the next
    # real one would hide.
    needle = "sys.path" + "[:] ="
    offenders = [
        path.name
        for path in sorted(tests_dir.glob("test_*.py"))
        if needle in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"{offenders} rewrite sys.path wholesale — use "
        "_preset_loader.load_preset_module, which restores it (#555)")
