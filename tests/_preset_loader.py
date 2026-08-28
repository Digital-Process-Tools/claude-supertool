"""One loader for every per-preset test module, with the `sys.path` restore in
a single place.

Each preset test file needs the same thing: run a module out of
``presets/<preset>/`` with that directory on ``sys.path``, so the module's
sibling shims (``_auth``, ``_rest``, ``_atproto``, ...) resolve to *its* copies
rather than to another preset's same-named file. Six files grew six
near-identical helpers to do it, and all six paid for the isolation with a
permanent, process-global edit to ``sys.path``:

    sys.path[:] = [p for p in sys.path if "presets/devto" not in p]

That line is the defect fixed in #552 and tracked in #555. It is not scoped to
the load it protects and it is never undone, so a test module that merely wants
its own shims resolved ends up deleting *other* modules' path entries for the
rest of the process. In #552 the victim was ``presets/mcp``:
``test_mcp_stop_outcome_547.py`` puts that directory on the path at import time
and then does a bare ``import stop`` **lazily, inside a fixture** — so the entry
has to survive until the test body runs, not merely until collection ends.
Whichever xdist worker imported the aggressor first left the victim with
``ModuleNotFoundError``. Deterministic given the order, invisible without it,
and the traceback points at the victim rather than the cause.

Two decisions worth stating, because both look like they lose information:

**The shim list is the union of all six.** ``sys.modules.pop(name, None)`` on a
shim this preset does not have is a no-op, and evicting one it does have is the
whole point — a cached ``_auth`` from another preset is exactly what the
eviction exists to prevent. So the union is not a superset by accident; it is
the correct list for every caller.

**The filter drops every ``presets/`` entry, not just the named rivals.** That
is wider than any of the five originals, and it is safe *only* because it is
restored: the collision it guards against exists solely while ``exec_module``
runs, and by the time the path goes back the module has bound the shims it
imported. Width without permanence costs nothing; permanence without width was
still a bug — #555's five strip narrower than #552's one and are just as broken
in principle.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Every sibling shim used by any preset. Evicting one a preset does not use is
#: a no-op; failing to evict one it does use is the bug this guards against.
SHIMS = (
    "_api",
    "_atproto",
    "_auth",
    "_common",
    "_graphql",
    "_me",
    "_outbound",
    "_resolve",
    "_rest",
    "_sanitize",
    "_session",
)


def load_preset_module(preset: str, name: str, prefix: str = "") -> ModuleType:
    """Execute ``presets/<preset>/<name>.py`` in isolation and return it.

    ``prefix`` only names the resulting module (``hn_publish``,
    ``bsky_sec_read``, ...); it keeps tracebacks and ``__name__`` readable and
    keeps two presets' same-named modules distinguishable. It has no effect on
    resolution — nothing is registered in ``sys.modules``.

    ``sys.path`` is left exactly as it was found, including entries this
    function removed and the preset directory it added.
    """
    preset_dir = REPO_ROOT / "presets" / preset
    saved_path = sys.path[:]
    for shim in SHIMS:
        sys.modules.pop(shim, None)
    sys.path[:] = [p for p in sys.path if "presets/" not in p.replace("\\", "/")]
    sys.path.insert(0, str(preset_dir))
    try:
        spec = importlib.util.spec_from_file_location(
            f"{prefix}{name}", preset_dir / f"{name}.py")
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved_path
    return mod


def load_validator_module(validator: str, name: str = "", prefix: str = "") -> ModuleType:
    """Execute ``validators/<validator>/<name>.py`` in isolation and return it.

    Same restore, different reason. A validator adapter is normally spawned as
    a subprocess, so its module-scope
    ``sys.path.insert(0, .../validators/common)`` costs nothing and nobody sees
    it. Imported in-process by a test — which is how a platform-neutral case
    reaches an adapter on Windows — that insert is permanent for the rest of
    the worker, and the guard against it lives here rather than in each test
    file for the reason #555 gives above.

    ``name`` defaults to ``validator``: every adapter is
    ``validators/<x>/<x>.py``.

    Unlike ``load_preset_module`` this strips nothing and evicts no shim. The
    preset loader does both because two presets ship same-named siblings, and a
    cached ``_auth`` from another preset is a real collision. Every validator
    reaches one shared ``validators/common``, so there is no rival copy to
    shadow — adding the eviction anyway would be machinery with no defect
    behind it, which is how a helper stops being readable.
    """
    directory = REPO_ROOT / "validators" / validator
    saved_path = sys.path[:]
    try:
        spec = importlib.util.spec_from_file_location(
            f"{prefix}{name or validator}", directory / f"{name or validator}.py")
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved_path
    return mod
