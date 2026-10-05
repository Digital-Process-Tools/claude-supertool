#!/usr/bin/env python3















from __future__ import annotations

import json
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "common"))
from source_context import context_fields
from refusal import guard_main, required, required_but_absent, skipped

TOOL = "tomllint"

NO_PARSER = ("no TOML parser available — `tomllib` needs Python 3.11+ and "
             "`tomli` is not installed (`pip install tomli`), so this file "
             "was NOT checked")


def emit(d: dict) -> None:
    print(json.dumps(d))


def _adapter_error(file: str, msg: str, dur_ms: int) -> None:

    emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
          "errors": [{"line": None, "col": None, "severity": "error",
                      "code": "adapter", "msg": msg}],
          "duration_ms": dur_ms})


def resolve_parser():










    try:
        import tomllib
        return tomllib
    except ImportError:
        pass
    try:
        import tomli
        return tomli
    except ImportError:
        return None


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        _adapter_error("", "no file arg", 0)
        return

    file = sys.argv[1]
    start = time.time()

    tomllib = resolve_parser()
    if tomllib is None:
        dur = int((time.time() - start) * 1000)
        if required(TOOL):
            _adapter_error(file, required_but_absent(TOOL, NO_PARSER), dur)
        else:
            emit(skipped(TOOL, file, NO_PARSER, dur))
        return

    try:
        with open(file, "rb") as fh:
            tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        msg = str(e).strip()[:300]

        line = None
        col = None
        import re
        m = re.search(r"line\s+(\d+)", msg, re.IGNORECASE)
        if m:
            line = int(m.group(1))
        m2 = re.search(r"col(?:umn)?\s+(\d+)", msg, re.IGNORECASE)
        if m2:
            col = int(m2.group(1))
        err = {"line": line, "col": col, "severity": "error", "code": "syntax", "msg": msg}
        if line is not None:
            err.update(context_fields(file, line))
        emit({"tool": "tomllint", "file": file, "ok": False, "count": 1,
              "errors": [err],
              "duration_ms": int((time.time() - start) * 1000)})
        return
    except FileNotFoundError:
        _adapter_error(file, "file not found", int((time.time() - start) * 1000))
        return

    emit({"tool": "tomllint", "file": file, "ok": True, "count": 0,
          "errors": [], "duration_ms": int((time.time() - start) * 1000)})


if __name__ == "__main__":
    guard_main(TOOL, main)
