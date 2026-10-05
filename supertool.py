#!/usr/bin/env python3






























from __future__ import annotations

import os
import sys






_INSTALL_DIR = os.path.dirname(os.path.realpath(__file__))
if _INSTALL_DIR not in sys.path:
    sys.path.insert(0, _INSTALL_DIR)









_IMPL = os.path.join(_INSTALL_DIR, "_supertool.py")
if not os.path.isfile(_IMPL):  
    sys.stderr.write(
        "supertool: incomplete install — `_supertool.py` not found in "
        + _INSTALL_DIR + "." + chr(10)
        + "supertool.py is only the entry point (#931); the tool itself lives "
        "in `_supertool.py` beside it. Copy or install both files." + chr(10)
    )
    raise SystemExit(2)


_CONFLICT_MARKERS = ("<" * 7, "=" * 7, ">" * 7)


def _marker_lines(path):  






    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return [i for i, line in enumerate(fh, 1)
                    if line.startswith(_CONFLICT_MARKERS)]
    except OSError:
        return None


def _refuse_unimportable_core(exc):  































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
    import _supertool  
except SyntaxError as _exc:  
    _refuse_unimportable_core(_exc)







if os.path.realpath(getattr(_supertool, "__file__", "") or "") != os.path.realpath(_IMPL):
    sys.stderr.write(
        "supertool: warning — mixed supertool trees. Ran "
        + str(getattr(_supertool, "__file__", "?"))
        + " but this entry point sits beside " + _IMPL + "." + chr(10)
    )

if __name__ == "__main__":
    sys.exit(_supertool.main(sys.argv[1:]))
else:


    sys.modules[__name__] = _supertool
