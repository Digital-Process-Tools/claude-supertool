#!/usr/bin/env python3
"""supertool — entry point. The tool itself lives in `_supertool.py`.

WHY THIS FILE IS EIGHTY LINES AND NOT SEVENTEEN THOUSAND (#931)
---------------------------------------------------------------
CPython writes and reuses `__pycache__/*.pyc` for every module it *imports*.
It never caches the script named on the command line: `__main__` is compiled
from source on every single run, by design. While the whole tool was that
script, every invocation re-parsed and re-compiled ~17.4k lines — measured on
GitHub runners at ~145ms on ubuntu and windows and ~100ms on macOS, over and
above the interpreter floor and our own imports.

That is paid by every user call, and ~900 times per CI leg. Moving the bulk
behind an `import` moves it into the bytecode cache, where it is compiled once
per source change instead of once per invocation.

Nothing about the invocation contract changes. `python3 supertool.py 'op'`,
`python3 -m supertool`, the `supertool` console script, and the
`~/.local/bin/supertool` / `dvsi/supertool` symlinks all still work.

THE `sys.modules` SWAP, AND WHY IT IS NOT CLEVERNESS FOR ITS OWN SAKE
---------------------------------------------------------------------
`import supertool` must keep yielding the module that holds the code, not this
shim. 173 test modules import it and 288 call sites do
`monkeypatch.setattr(supertool, ...)`; a re-export shim would give them a
*second* module object, so a patched name would be set on the shim while the
implementation kept reading its own global. Every one of those tests would go
green against unpatched code — the exact failure mode this repo keeps having.
Rebinding `sys.modules["supertool"]` hands back the one real module object, so
`supertool.X` is `_supertool.X` for reading, writing and patching alike.
"""
from __future__ import annotations

import os
import sys

# sys.path[0] is the *resolved* script directory on 3.11+, but not on 3.9/3.10,
# which this package still supports — and both documented installs
# (`~/.local/bin/supertool`, `dvsi/supertool`) are symlinks invoked from an
# unrelated cwd. Put the directory this file really lives in on the path
# explicitly so `_supertool` is findable either way.
_INSTALL_DIR = os.path.dirname(os.path.realpath(__file__))
if _INSTALL_DIR not in sys.path:
    sys.path.insert(0, _INSTALL_DIR)

# Whether this install is complete is decided by what is on disk beside this
# file — never by whether `import _supertool` happens to succeed. Once the tool
# is installed as a package the name resolves from anywhere: CI's
# `pip install -e .` puts an `_EditableFinder` on `sys.meta_path` that maps it
# straight back to the checkout, and a normal install has it in site-packages.
# So a lone copied `supertool.py` would import a *different* tree, run its
# `main`, and print a perfectly convincing version banner — the mixed-tree
# class of #678, arriving in the one file whose job is to locate the code.
_IMPL = os.path.join(_INSTALL_DIR, "_supertool.py")
if not os.path.isfile(_IMPL):  # pragma: no cover - exercised as a subprocess
    sys.stderr.write(
        "supertool: incomplete install — `_supertool.py` not found in "
        + _INSTALL_DIR + "." + chr(10)
        + "supertool.py is only the entry point (#931); the tool itself lives "
        "in `_supertool.py` beside it. Copy or install both files." + chr(10)
    )
    raise SystemExit(2)

import _supertool  # noqa: E402

# `_INSTALL_DIR` is first on `sys.path`, so the sibling wins over anything the
# path offers. It does not outrank a `sys.meta_path` finder, though, and those
# are what editable installs are made of — so confirm what actually loaded
# rather than assuming. Warn, do not refuse: the file asked for is present and
# the one that loaded is a real supertool, so the run can proceed; what it must
# not do is proceed silently.
if os.path.realpath(getattr(_supertool, "__file__", "") or "") != os.path.realpath(_IMPL):
    sys.stderr.write(
        "supertool: warning — mixed supertool trees. Ran "
        + str(getattr(_supertool, "__file__", "?"))
        + " but this entry point sits beside " + _IMPL + "." + chr(10)
    )

if __name__ == "__main__":
    sys.exit(_supertool.main(sys.argv[1:]))
else:
    # Not inside the `__main__` branch: replacing sys.modules["__main__"] would
    # swap the module a running frame belongs to, for no benefit.
    sys.modules[__name__] = _supertool
