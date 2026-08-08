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

#: The three markers git writes into a conflicted file, at line start.
_CONFLICT_MARKERS = ("<" * 7, "=" * 7, ">" * 7)


def _marker_lines(path):  # pragma: no cover - exercised as a subprocess
    """1-indexed lines of `path` that open, split or close a conflict block.

    Read as text, deliberately. The file is by definition not parseable at
    this point, and a scan that needs the parser to succeed cannot run in the
    only situation it exists for.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return [i for i, line in enumerate(fh, 1)
                    if line.startswith(_CONFLICT_MARKERS)]
    except OSError:
        return None


def _refuse_unimportable_core(exc):  # pragma: no cover - exercised as a subprocess
    """Name why no op can run, and a recovery that avoids the broken module.

    A rebase that touches this tool's own core leaves `_supertool.py`
    conflicted, and a file carrying live `<<<<<<<` markers is not Python. The
    import above then died with

        File ".../_supertool.py", line 17095
            >>>>>>> 4c5cfa8 (fix(payload): ...)
        SyntaxError: invalid decimal literal

    which reads as *the tool is broken* rather than *the tool cannot describe
    this state* — and it is the whole tool, not one op, for exactly as long as
    the conflict exists (#1015). What that traceback cost in practice was the
    operator falling back to raw `git diff --diff-filter=U` plus `awk`, i.e.
    the hand-rolled resolver `git-conflicts` exists to replace, or to the
    global `supertool`, which inside a branch worktree runs another checkout's
    core against this tree's presets (#1012).

    So the refusal carries a recovery that does not go through the module
    under conflict. `presets/git/conflicts.py` and `presets/git/resolve.py`
    import only `_git_common` and `_env`; they run standalone against *this*
    tree, which `tests/test_core_unimportable_1015.py` proves by running one.

    It refuses rather than working around the conflict on purpose. Guessing
    which side of an unresolved block to run would be picking a resolution
    nobody asked for, which is worse than either arm of the judgement.

    `sys.executable` rather than a literal `python3`: the interpreter running
    this line is the one that works here, and on Windows the name is `python`
    or `py` (#1017).
    """
    lines = _marker_lines(_IMPL)
    w = sys.stderr.write
    w("supertool: the core is unimportable, so NO OP CAN RUN — not this one, "
      "not `read`, not `git-status`." + chr(10))
    if lines:
        shown = ", ".join(str(n) for n in lines[:6])
        more = " (+%d more)" % (len(lines) - 6) if len(lines) > 6 else ""
        w("  Cause: %s still contains git conflict markers, at line(s) %s%s."
          % (_IMPL, shown, more) + chr(10))
    elif lines is None:
        w("  Cause: %s could not be read, and Python could not parse it: %s"
          % (_IMPL, exc) + chr(10))
    else:
        w("  Cause: %s is not valid Python (no conflict markers in it): %s"
          % (_IMPL, exc) + chr(10))
    w("  This is a statement about that file only. It is NOT saying your tree "
      "is clean, and it is not a report of what is conflicted." + chr(10))
    w("  Recovery, without the broken core and without borrowing another "
      "checkout's (#1012) — run this tree's presets directly:" + chr(10))
    for cmd, what in (
        (os.path.join("presets", "git", "conflicts.py"),
         "every conflicted file + every block"),
        (os.path.join("presets", "git", "resolve.py") + " ours PATH",
         "or theirs / both"),
    ):
        w("    %s %s   # %s"
          % (sys.executable, os.path.join(_INSTALL_DIR, cmd), what) + chr(10))
    w("  Do not run the global `supertool` here: it resolves to whichever "
      "checkout is on PATH and would run that tree's core against this "
      "tree's presets." + chr(10))
    raise SystemExit(2)


try:
    import _supertool  # noqa: E402
except SyntaxError as _exc:  # pragma: no cover - exercised as a subprocess
    _refuse_unimportable_core(_exc)

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
