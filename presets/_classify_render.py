#!/usr/bin/env python3
































































































































from __future__ import annotations

import os
import sys
from pathlib import Path

_CLASSIFY_DIR = Path(__file__).resolve().parent / "classify"
if str(_CLASSIFY_DIR) not in sys.path:
    sys.path.insert(0, str(_CLASSIFY_DIR))
import scanner  
import model  
import cache as classify_cache  



LEVEL_OFF = "off"
LEVEL_SCANNER = "scanner"
LEVEL_FULL = "full"
_LEVELS = (LEVEL_OFF, LEVEL_SCANNER, LEVEL_FULL)



CLASSIFY_BUDGET = 6




NOT_RUN_BUDGET = "classify: not-run (call budget reached, see #2049)"









_OFF_LINE = "classify: off (not configured to classify - see #2049)"
_SCANNER_CLEAN_LINE = (
    "classify: scanner-clean (model stage not configured to run - see #2049)"
)


def level_from_env(*, default: str = LEVEL_FULL) -> str:









    raw = os.environ.get("SUPERTOOL_CLASSIFY")
    if raw is None:
        return default
    raw = raw.strip().lower()
    return raw if raw in _LEVELS else default


def _scanner_line(findings) -> str:
    axes = ", ".join(f.axis for f in findings)
    return f"classify: suspect ({axes})"


def verdict_line(text: str, *, level: str = LEVEL_FULL, spawn=None,
                  timeout: int = model.DEFAULT_TIMEOUT, cache=None) -> str:















    if not text.strip():
        return "classify: safe (empty)"
    if level == LEVEL_OFF:
        return _OFF_LINE
    findings = scanner.scan(text)
    if findings:
        return _scanner_line(findings)
    if level == LEVEL_SCANNER:
        return _SCANNER_CLEAN_LINE
    spawn_fn = spawn if spawn is not None else model._default_spawn
    v = model.classify(text, spawn=spawn_fn, timeout=timeout, cache=cache)
    return _render_verdict(v)


def _render_verdict(v: "model.Verdict") -> str:






    if v.state == "safe":
        return "classify: safe"
    if v.state == "suspect":
        return f"classify: suspect ({', '.join(v.axes)})"
    return f"classify: could-not-classify ({v.reason})"


class Budget:













    def __init__(self, n: int = CLASSIFY_BUDGET, cache=None) -> None:
        self.remaining = n






        self.cache = cache if cache is not None else classify_cache.default_cache()

    def line(self, text: str, *, level: str = LEVEL_FULL, spawn=None,
              timeout: int = model.DEFAULT_TIMEOUT) -> str:
        if level == LEVEL_FULL and text.strip() and not scanner.scan(text):
            cached, _status = self.cache.get(text)
            if cached is not None:





                return _render_verdict(cached)
            if self.remaining <= 0:
                return NOT_RUN_BUDGET
            self.remaining -= 1
        return verdict_line(text, level=level, spawn=spawn, timeout=timeout,
                             cache=self.cache)
