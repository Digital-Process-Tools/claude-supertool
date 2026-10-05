
















from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple



ENV_TEST_MODULE = "SUPERTOOL_ENCODING_SEAM_TEST_MODULE"

DEFAULT_RELATIVE = "tests/test_encoding_seam.py"


def repo_root(start: Path) -> Optional[Path]:









    try:
        r = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, timeout=15, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return None
    if r.returncode != 0:
        return None
    top = (r.stdout or "").strip()
    return Path(top).resolve() if top else None


def find_test_module(root: Path) -> Optional[Path]:







    override = os.environ.get("SUPERTOOL_ENCODING_SEAM_TEST_MODULE", "").strip()
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None
    candidate = root / DEFAULT_RELATIVE
    return candidate if candidate.is_file() else None
















CONFIG_DIR_ENV = "SUPERTOOL_CONFIG_DIR"


def config_dir() -> "Tuple[Optional[Path], bool, str]":








    if "SUPERTOOL_CONFIG_DIR" not in os.environ:
        return None, False, ""
    raw = os.environ["SUPERTOOL_CONFIG_DIR"].strip()
    if not raw:
        return None, True, "{0} was set but empty".format(CONFIG_DIR_ENV)
    try:
        return Path(raw).resolve(), True, ""
    except (OSError, ValueError) as exc:
        return None, True, "{0}={1!r} could not be resolved: {2}".format(
            CONFIG_DIR_ENV, raw, exc)


def config_dir_may_authorize_execution(root: Path, cfg_dir: Path) -> bool:







    root_s = os.path.normcase(str(root))
    cfg_s = os.path.normcase(str(cfg_dir))
    if cfg_s == root_s:
        return True
    try:
        common = os.path.commonpath([root_s, cfg_s])
    except ValueError:  
        return False
    return common == root_s


def load_scan_module(path: Path):

    spec = importlib.util.spec_from_file_location(
        "_st_encoding_seam_guard", path)
    if spec is None or spec.loader is None:
        raise ImportError("no import spec for {0}".format(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_st_encoding_seam_guard", module)
    spec.loader.exec_module(module)
    return module


def scope_kinds(relpath: str, shipped: "Tuple[str, ...]") -> Optional[Tuple[str, ...]]:










    posix = Path(relpath).as_posix()
    top = posix.split("/", 1)[0]
    if top == "tests":
        return ("read",)
    if top in shipped:
        return ("read", "write")
    return None


def scan_one(module, path: Path, kinds: "Optional[Tuple[str, ...]]") -> "List[dict]":



















    if kinds is None:
        return []
    records: List[dict] = []
    for lineno, call in module.encoding_violations(path, kinds=kinds):
        records.append({
            "line": lineno, "severity": "error", "code": "encoding-seam",
            "msg": "{0}() without encoding=".format(call),
        })
    violations, undecidable = module.subprocess_encoding_violations(path)
    for lineno, call, missing in violations:
        records.append({
            "line": lineno, "severity": "error", "code": "encoding-seam",
            "msg": "subprocess {0}() decodes text but leaves {1} to the "
                   "default".format(call, missing),
        })
    for lineno, call, why in undecidable:
        records.append({
            "line": lineno, "severity": "warning",
            "code": "encoding-seam-undecidable",
            "msg": "{0}() -- {1}".format(call, why),
        })
    records.sort(key=lambda r: (r["line"] if r["line"] is not None else -1))
    return records
