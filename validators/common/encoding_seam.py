"""Shared locator/scanner for the #418 encoding-seam guard (#2287).

Both the standalone script (`.github/scripts/check_encoding_seam.py`) and the
`encoding-seam` validator adapter need the same answer to "does this one file
violate the encoding-seam rule" -- the rule `tests/test_encoding_seam.py`
already enforces tree-wide, in CI only, at a full-suite cost of ~9-10 minutes
on the Windows leg. This module locates that test file's own scan functions
and *imports* them rather than re-implementing the AST walk -- the same shape
`new-file-lint.py` and `changelog-fragment.py` already use for "find the
project's own script and import it": import nothing that is not the
project's, `skipped` (never `ok`) when the convention is not adopted here.

Deliberately narrow, on purpose (#2287): a slow or false-positive local check
teaches lanes to route around it, which is the failure this guard exists to
avoid one level down. No subprocess spawn beyond a single `git rev-parse`,
and the AST parse itself is microseconds per file.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

#: Overridable the same way `new-file-lint.py`'s `ENV_LINT_SCRIPT` is --
#: an operator who sets this meant that exact path and no other.
ENV_TEST_MODULE = "SUPERTOOL_ENCODING_SEAM_TEST_MODULE"

DEFAULT_RELATIVE = "tests/test_encoding_seam.py"


def repo_root(start: Path) -> Optional[Path]:
    """The git repo root above `start`, or `None` when it cannot be found.

    Mirrors `new-file-lint.py`'s own `_repo_root` -- the convention this tree
    already uses for the same question, including the explicit
    `except subprocess.TimeoutExpired` arm (#1604): folding it into a bare
    `except (OSError, subprocess.SubprocessError)` still catches it (the
    former is a subclass of the latter), but leaves nothing an adapter-wall
    sweep can tell apart from any other spawn failure.
    """
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
    """The project's own `tests/test_encoding_seam.py`, or `None`.

    A project that has not adopted this test file has not adopted this
    guard -- that is `skipped`, never a bare pass, in both callers.
    """
    override = os.environ.get(ENV_TEST_MODULE, "").strip()
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None
    candidate = root / DEFAULT_RELATIVE
    return candidate if candidate.is_file() else None


#: Set by supertool's own validator runner (`_supertool.py`'s
#: `_validator_run_one`) to the directory holding the `.supertool.json`
#: that wired THIS validator run -- never set by this module itself.
#:
#: Same shape as `new-file-lint.py`'s and `changelog-fragment.py`'s own
#: `CONFIG_DIR_ENV` (#2228, #2236): `find_test_module` trusts whatever
#: `tests/test_encoding_seam.py` sits inside the target file's own git
#: root, and `load_scan_module` then IMPORTS AND EXECUTES it. A maintainer
#: whose own `.supertool.json` sits above a directory of clones, editing a
#: file inside one of them, would otherwise have that clone's own
#: (possibly attacker-controlled) test file imported with the maintainer's
#: privileges -- the identical shape those two issues closed for the other
#: two convention-based adapters, reproduced here because this adapter
#: imports and executes a found script exactly the same way.
CONFIG_DIR_ENV = "SUPERTOOL_CONFIG_DIR"


def config_dir() -> "Tuple[Optional[Path], bool, str]":
    """`(config_dir, scope_known, reason)` -- see `new-file-lint.py`'s
    identically-shaped `_config_dir` for the full reasoning. `scope_known`
    is `False` only when `CONFIG_DIR_ENV` is absent entirely (this module
    invoked directly, outside supertool's own validator wiring), in which
    case no scope claim is made either way.
    """
    if CONFIG_DIR_ENV not in os.environ:
        return None, False, ""
    raw = os.environ[CONFIG_DIR_ENV].strip()
    if not raw:
        return None, True, "{0} was set but empty".format(CONFIG_DIR_ENV)
    try:
        return Path(raw).resolve(), True, ""
    except (OSError, ValueError) as exc:
        return None, True, "{0}={1!r} could not be resolved: {2}".format(
            CONFIG_DIR_ENV, raw, exc)


def config_dir_may_authorize_execution(root: Path, cfg_dir: Path) -> bool:
    """True only when `cfg_dir` (where `.supertool.json` lives) IS `root`,
    or a directory `root` contains -- the two relationships that mean "the
    project that configured supertool IS the project whose test file is
    about to run." Every other relationship -- `cfg_dir` strictly above
    `root` (#2228), or a disjoint/sibling tree with no ancestry at all
    (#2236) -- does not authorize the import.
    """
    root_s = os.path.normcase(str(root))
    cfg_s = os.path.normcase(str(cfg_dir))
    if cfg_s == root_s:
        return True
    try:
        common = os.path.commonpath([root_s, cfg_s])
    except ValueError:  # different drives on Windows -- no ancestry
        return False
    return common == root_s


def load_scan_module(path: Path):
    """Import `path` as a module and return it — never copy its AST walk."""
    spec = importlib.util.spec_from_file_location(
        "_st_encoding_seam_guard", path)
    if spec is None or spec.loader is None:
        raise ImportError("no import spec for {0}".format(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_st_encoding_seam_guard", module)
    spec.loader.exec_module(module)
    return module


def scope_kinds(relpath: str, shipped: "Tuple[str, ...]") -> Optional[Tuple[str, ...]]:
    """The `encoding_violations` `kinds` for `relpath`'s scope, or `None`.

    Mirrors the two scopes `tests/test_encoding_seam.py` itself enforces:
    `tests/` is read-only (`test_no_test_file_decodes_a_read_by_locale`),
    `SHIPPED` is read+write (`test_no_shipped_file_decodes_by_locale`). A
    path in neither -- `docs/`, `.github/`, a top-level script outside
    `SHIPPED` -- is out of scope for *this* half of the rule (subprocess
    calls are still scanned there via `scan_one`, unconditionally, matching
    the tree-wide guard's own #862 scope).
    """
    posix = Path(relpath).as_posix()
    top = posix.split("/", 1)[0]
    if top == "tests":
        return ("read",)
    if top in shipped:
        return ("read", "write")
    return None


def scan_one(module, path: Path, kinds: "Optional[Tuple[str, ...]]") -> "List[dict]":
    """Every encoding-seam finding for one file, as structured records.

    `kinds=None` means `path` is outside BOTH scopes `scope_kinds`
    recognises -- neither `tests/` nor a `SHIPPED` directory -- and this
    now skips the subprocess half too (#2287 review), not just the
    read/write half. `tests/test_encoding_seam.py`'s own tree-wide guard
    never enumerates subprocess calls outside those same two scopes either
    (`test_no_shipped_subprocess_decodes_by_locale` walks `_shipped_files()`,
    `test_no_test_subprocess_decodes_by_locale` walks `tests/`, and nothing
    else calls `subprocess_encoding_violations` tree-wide) -- so scanning a
    file like `.github/scripts/*.py` here for a violation CI would never
    itself catch was a false positive relative to the guard this local
    check claims to mirror, not merely a broader net.

    Each record: `{"line": int, "severity": "error"|"warning", "code": str,
    "msg": str}`. `severity` is `"warning"` only for the subprocess scan's
    own undecidable state (`**kwargs`, a non-literal `text=`) -- a call the
    rule could not judge, not one it judged clean.
    """
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
