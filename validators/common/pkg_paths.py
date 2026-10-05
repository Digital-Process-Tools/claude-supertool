




























from __future__ import annotations

import ntpath
import os
import posixpath
from pathlib import Path
from typing import Callable


def canon(path: object, normcase: Callable[[str], str] | None = None) -> str:















    fold = normcase or os.path.normcase
    return fold(str(path)).replace("\\", "/")


def is_abs(path: str) -> bool:







    return ntpath.isabs(path) or posixpath.isabs(path)


def _spellings(text: str, normcase: Callable[[str], str] | None,
               host_isabs: Callable[[str], bool] | None = None) -> tuple:































    isabs = host_isabs or os.path.isabs
    forms = {posixpath.normpath(canon(text, normcase))}
    if not isabs(text):
        return forms, False
    try:
        forms.add(posixpath.normpath(canon(Path(text).resolve(), normcase)))
    except OSError:





        return forms, True
    return forms, False


def _target_spellings(target: object, target_raw: object = "",
                      normcase: Callable[[str], str] | None = None,
                      host_isabs: Callable[[str], bool] | None = None) -> tuple:







    forms, refused = set(), False
    for raw in (target, target_raw):
        text = str(raw or "").strip()
        if not text:
            continue
        if not is_abs(text):
            text = os.path.abspath(text)
        more, bad = _spellings(text, normcase, host_isabs)
        forms |= more
        refused = refused or bad
    return forms, refused


def target_forms(target: object, target_raw: object = "",
                 normcase: Callable[[str], str] | None = None,
                 host_isabs: Callable[[str], bool] | None = None) -> set:












    return _target_spellings(target, target_raw, normcase, host_isabs)[0]


def attribute(reported: str, target: object, base: object = "",
              target_raw: object = "",
              normcase: Callable[[str], str] | None = None,
              host_isabs: Callable[[str], bool] | None = None) -> str:




























    src = (reported or "").strip()
    if not src:
        return "unknown"

    folded = posixpath.normpath(canon(src, normcase))
    if is_abs(src):
        forms, refused = _spellings(src, normcase, host_isabs)
    else:
        if not str(base or "").strip():
            return "unknown"
        anchor = posixpath.normpath(canon(base, normcase))
        forms = {posixpath.normpath(posixpath.join(anchor, folded))}
        refused = False

    known, target_refused = _target_spellings(target, target_raw, normcase,
                                              host_isabs)
    if forms & known:
        return "this"
    if refused or target_refused:
        return "unknown"
    return "other"
