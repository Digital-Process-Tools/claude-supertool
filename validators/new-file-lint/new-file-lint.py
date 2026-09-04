#!/usr/bin/env python3
"""new-file-lint validator adapter -- catch a lint finding a tree-wide ignore
hides for a genuinely new file, locally, before it costs a CI round trip (#2155).

**This adapter states no rules of its own**, the same shape as
`changelog-fragment.py` (#1132): it looks for the PROJECT's own script that
already knows which extra rules apply to a file with no git history, and
imports its `EXTRA_RULES` constant. A project with no such script gets
`skipped`, not `ok` -- this is not a claim that every project ignores
anything tree-wide, only that this one does and knows how to say so.

Why this shape and not a hardcoded ruleset or a hardcoded path to one
project's script (#2196 review): the first census this repo runs over its
own `.supertool.json` -- `test_every_configured_validator_cmd_is_an_adapter`
-- refuses a `cmd` wired to a raw tool rather than a SCHEMA.md adapter, and
the first cut of this file (`.github/scripts/new_file_ruff_gate.py`) was a
proper adapter in every way EXCEPT that it lived outside `validators/` and
imported one repo's `lint_new_files.py` by a path relative to itself. That
made it correct for this repository and inexpressible as a shipped adapter
for any other. `changelog-fragment.py` solved the identical problem for
`assemble_changelog.py` -- import nothing, walk up from the target file
looking for the project's own script at a known location, `skipped` if there
is none -- so this file borrows that shape rather than inventing a second
one for a near-identical case.

What is genuinely generic and stays in THIS file rather than the found
script: the git-blob-at-`HEAD` test (does this path have no history at all,
the `lint_new_files.py`-style `ACR` case re-derived against `HEAD` because
there is no PR base ref locally) and the `ruff --extend-select` invocation
itself. What is genuinely project-specific and is never guessed at here:
which three (or however many) rules apply, and where the project keeps that
answer.

Three states. `ok`, a finding, and `skipped` -- no such script found above
the file (a project that has not adopted this convention), ruff itself
absent, or the question "does this path have history at HEAD" could not be
answered at all (no git, not a repo, a probe timeout).

Usage: new-file-lint.py <file>
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from refusal import absent, guard_main, skipped, tool_fault  # noqa: E402

TOOL = "new-file-lint"

INSTALL_HINT = "ruff not found on PATH — pip install ruff"

# Where the project keeps the script that owns "which extra rules apply to a
# file with no history". Overridable so this adapter is not asserting one
# repo's layout as a fact about every repo -- same reasoning, same env-var
# shape, as `changelog-fragment.py`'s `SUPERTOOL_CHANGELOG_ASSEMBLER`.
ENV_LINT_SCRIPT = "SUPERTOOL_NEW_FILE_LINT_SCRIPT"

#: Known conventions, tried in order. `.github/scripts/lint_new_files.py` is
#: this repo's own layout and the only one observed so far -- unlike
#: `changelog-fragment.py`'s three-entry `ASSEMBLER_LOCATIONS`, there is no
#: second convention on record yet. A project using a different location
#: still has `SUPERTOOL_NEW_FILE_LINT_SCRIPT`, and this tuple is exactly
#: where a second observed convention would be added, not a place to guess
#: one in ahead of evidence.
LINT_SCRIPT_LOCATIONS = (
    os.path.join(".github", "scripts", "lint_new_files.py"),
)

# Same budget as the shared ruff validator (validators/ruff/ruff.py) plus the
# git probes below -- several spawns, none of them ruff's own selling point
# (milliseconds), so this stays a hang-guard rather than a performance floor.
TIMEOUT_S = 30

RC_CLEAN = 0
RC_FINDINGS = 1


def emit(d: dict) -> None:
    print(json.dumps(d))


def _adapter_error(file: str, msg: str, dur_ms: int) -> None:
    emit({"tool": TOOL, "file": file, "ok": False, "count": 1,
          "errors": [{"line": None, "col": None, "severity": "error",
                      "code": "adapter", "msg": msg}],
          "duration_ms": dur_ms})


def _locations() -> tuple:
    """The relative path(s) to try, in order. `ENV_LINT_SCRIPT` names one
    path and takes it exactly -- an operator who set it meant that location
    and no other."""
    override = os.environ.get(ENV_LINT_SCRIPT, "").strip()
    return (override,) if override else LINT_SCRIPT_LOCATIONS


#: Set by supertool's own validator runner (`_supertool.py`'s
#: `_validator_run_one`) to the directory holding the `.supertool.json`
#: that wired THIS validator run -- never set by this adapter itself.
#:
#: Closes #2228: `_find_lint_script`'s walk was bounded at the edited
#: file's own git root, which stops an escape ABOVE that repo (the shape
#: `changelog-fragment.py` closed for itself in #2178) but trusted
#: whatever CONVENTIONALLY-NAMED script sits inside that repo unconditionally
#: -- including a repo that is not the one whose `.supertool.json` wired
#: this validator at all. A maintainer whose own `.supertool.json` sits
#: above a directory of clones, editing a `.py` file inside one of them,
#: had that clone's own `.github/scripts/lint_new_files.py` imported (and
#: `_load` executes what it imports) with the maintainer's privileges.
CONFIG_DIR_ENV = "SUPERTOOL_CONFIG_DIR"


def _config_dir() -> "tuple[Path | None, bool, str]":
    """`(config_dir, scope_known, reason)`.

    `scope_known` is False only when `CONFIG_DIR_ENV` is absent entirely --
    this adapter was invoked directly, outside supertool's own validator
    wiring (a test harness, an operator running the script by hand). No
    scope claim is being made either way in that case, so the pre-#2228
    repo-bound walk applies unchanged: running this script directly is
    exactly as safe as it always was, and nothing here narrows that.

    `scope_known` is True whenever supertool's real validator runner set
    the variable -- empty or unresolvable counts as "no directory to
    trust", never as "trust everything", because reaching this adapter at
    all through that runner implies a `.supertool.json` WAS found (this
    validator's own wiring lives inside one).
    """
    if CONFIG_DIR_ENV not in os.environ:
        return None, False, ""
    raw = os.environ[CONFIG_DIR_ENV].strip()
    if not raw:
        return None, True, "{0} was set but empty".format(CONFIG_DIR_ENV)
    try:
        return Path(raw).resolve(), True, ""
    except OSError as exc:
        return None, True, "{0}={1!r} could not be resolved: {2}".format(
            CONFIG_DIR_ENV, raw, exc)


def _root_is_inside_config_scope(root: Path, config_dir: Path) -> bool:
    """True when `config_dir` (where `.supertool.json` lives) is `root`
    itself or somewhere inside it -- the project that wired this validator
    IS the project whose script is about to be imported and executed.

    False when `config_dir` sits above `root`: the directory-of-clones
    shape #2228 was filed for, where the script this adapter would find
    and run belongs to whichever clone is currently being edited, not to
    the project that configured supertool.
    """
    root_s = os.path.normcase(str(root))
    config_s = os.path.normcase(str(config_dir))
    try:
        common = os.path.commonpath([root_s, config_s])
    except ValueError:  # e.g. different drives on Windows
        return False
    return common == root_s


def _repo_root(start: Path) -> "tuple[Path | None, str | None]":
    """The git repo root above `start`, and why not when there is none.

    Mirrors `changelog-fragment.py`'s own `_repo_root` (and
    `validators/common/ci_lint_resolve_root.py`'s) -- the convention this
    tree already uses for the same question, including the "could not look"
    vs "looked, found nothing" split (#2177): a caller that folds every
    failure mode into a bare `None` cannot tell "git is not on PATH" from
    "this is not a git repository" from "walked the whole repo and there is
    genuinely no script here."
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=TIMEOUT_S,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return None, "git binary not found"
    except subprocess.TimeoutExpired:
        return None, "git rev-parse timed out"
    except OSError as exc:
        return None, "git could not be run: {0}".format(exc)
    if r.returncode != 0:
        return None, "not inside a git repository"
    top = r.stdout.strip()
    return (Path(top).resolve(), None) if top else (None, "git reported no toplevel")


def _find_lint_script(target: Path, root: Path) -> "Path | None":
    """The nearest project script at or above `target`, bounded at `root`.

    Bounded the same way `changelog-fragment.py`'s `_find_assembler` is
    (#2178): an unbounded walk to filesystem root would let a file inside
    one repo pick up and get IMPORTED -- `_load` executes what it finds --
    a same-named script sitting anywhere above the repo, which is attacker
    territory the moment this runs against an untrusted checkout.
    """
    resolved_start = target.parent.resolve()
    for parent in [resolved_start, *resolved_start.parents]:
        for relative in _locations():
            candidate = parent / relative
            if candidate.is_file():
                return candidate
        if parent == root:
            break
    return None


def _load(script: Path):
    spec = importlib.util.spec_from_file_location("_st_lint_new_files", script)
    if spec is None or spec.loader is None:
        raise ImportError("no import spec for {0}".format(script))
    module = importlib.util.module_from_spec(spec)
    sys.modules["_st_lint_new_files"] = module
    spec.loader.exec_module(module)
    return module


def _is_new_at_head(realpath: str, root: Path) -> "tuple[bool | None, str]":
    """`(is_new, reason_if_None)`.

    `is_new` is `True` when `HEAD:<path-relative-to-root>` has no blob --
    this path was never committed, so it carries none of whatever debt the
    project's own tree-wide ignore exists for. `False` when it does. `None`
    when the question could not be answered at all (the probe timed out, or
    the path cannot be related to `root` -- Windows: different drives) --
    never guessed at either way, because guessing `False` would silently
    exempt a genuinely new file and guessing `True` would re-surface debt on
    a file this validator has no business relitigating.

    `realpath` and `root` must already agree on symlink resolution -- `root`
    comes from `git rev-parse --show-toplevel`, which reports the PHYSICAL
    path (every symlink resolved), so the caller must resolve `realpath`
    the same way before calling this (#2196 review finding: mixing
    `abspath` on one side with `--show-toplevel` on the other silently
    misclassified an already-committed file reached through a symlinked
    ancestor as new).
    """
    try:
        rel = os.path.relpath(realpath, str(root)).replace(os.sep, "/")
    except ValueError as exc:
        return None, "could not relate {0!r} to repo root {1!r}: {2}".format(
            realpath, root, exc)
    try:
        r = subprocess.run(["git", "-C", str(root), "cat-file", "-e",
                            "HEAD:" + rel], capture_output=True, text=True,
                           timeout=TIMEOUT_S, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "git could not be spawned to probe HEAD: {0}".format(exc)
    if r.returncode == 0:
        return False, ""
    # Anything else -- no such path at HEAD, or no HEAD at all (unborn
    # branch, brand-new repo) -- means this path carries no history to be
    # exempt from checking, which is exactly the case this gate covers.
    return True, ""


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        _adapter_error("", "no file arg", 0)
        return

    file = sys.argv[1]
    start = time.time()

    if not shutil.which("ruff"):
        emit(absent(TOOL, file, INSTALL_HINT, int((time.time() - start) * 1000)))
        return

    path = Path(file)
    root, could_not_look = _repo_root(path.parent)
    if root is None:
        emit(skipped(TOOL, file,
                     "could not determine the git repo root above {0}, so no "
                     "location was tried and no history check was possible: "
                     "{1}. That is a claim about this run, not about the "
                     "project.".format(path.parent, could_not_look),
                     int((time.time() - start) * 1000)))
        return

    override_set = bool(os.environ.get(ENV_LINT_SCRIPT, "").strip())
    if not override_set:
        config_dir, scope_known, scope_reason = _config_dir()
        if scope_known and (config_dir is None
                             or not _root_is_inside_config_scope(root, config_dir)):
            emit(skipped(TOOL, file,
                         "a new-file-lint script may exist at the default "
                         "location(s) inside {0}, but the .supertool.json "
                         "that wired this run does not live inside that "
                         "project ({1}) -- the convention-based location is "
                         "not trusted across that boundary (#2228), because "
                         "a checkout is not made trustworthy just by being "
                         "edited under a directory that also holds this "
                         "config. Set {2} to an exact path if this "
                         "project's script is meant to be trusted "
                         "here.".format(
                             root, scope_reason or "config_dir={0}".format(config_dir),
                             ENV_LINT_SCRIPT),
                         int((time.time() - start) * 1000)))
            return

    script = _find_lint_script(path, root)
    if script is None:
        tried = ", ".join(_locations())
        emit(skipped(TOOL, file,
                     "no new-file-lint script found at or above {0} -- tried "
                     "{1}. That is a claim about where this adapter looked, "
                     "not about the project: set {2} to point at the real "
                     "script if it lives somewhere else, or this project "
                     "has not adopted this convention at all.".format(
                         path.parent, tried, ENV_LINT_SCRIPT),
                     int((time.time() - start) * 1000)))
        return

    try:
        found = _load(script)
    except Exception as exc:  # the script is the project's, may not import
        _adapter_error(file, "{0} could not be imported, so the file was "
                             "NOT checked: {1}: {2}".format(
                                 script, type(exc).__name__, exc),
                       int((time.time() - start) * 1000))
        return

    extra_rules = getattr(found, "EXTRA_RULES", None)
    if not extra_rules:
        emit(skipped(TOOL, file,
                     "{0} does not define EXTRA_RULES, so this adapter does "
                     "not know which rules a new file should be checked "
                     "against".format(script),
                     int((time.time() - start) * 1000)))
        return

    # #2196 review finding: resolve symlinks the same way `root` already has
    # (git reports the PHYSICAL path) before relating the two -- see
    # `_is_new_at_head`'s own docstring.
    is_new, reason = _is_new_at_head(os.path.realpath(file), root)
    if is_new is None:
        emit(skipped(TOOL, file,
                     "could not determine whether this path has history at "
                     "HEAD, so whether the tree-wide ignore applies to it is "
                     "unknown: " + reason,
                     int((time.time() - start) * 1000)))
        return
    if not is_new:
        emit(skipped(TOOL, file,
                     "this path already has a commit at HEAD -- the "
                     "project's tree-wide ignore covers it locally the same "
                     "as the shared `ruff` validator; a new finding "
                     "introduced by THIS edit is CI's own added-path leg's "
                     "job, via its own merge-base diff, which this "
                     "validator has no PR base ref to reproduce",
                     int((time.time() - start) * 1000)))
        return

    cmd = ["ruff", "check", "--output-format", "json", "--no-cache",
           "--force-exclude", "--quiet", "--extend-select",
           ",".join(extra_rules), "--", file]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=TIMEOUT_S, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        emit(absent(TOOL, file, "ruff on PATH but could not be executed",
                    int((time.time() - start) * 1000)))
        return
    except subprocess.TimeoutExpired:
        _adapter_error(file, "timeout — ruff did not return within "
                             "{0}s; the file was NOT checked".format(TIMEOUT_S),
                       int((time.time() - start) * 1000))
        return

    dur = int((time.time() - start) * 1000)
    body = (r.stdout or "").strip()
    if r.returncode not in (RC_CLEAN, RC_FINDINGS) and not body:
        _adapter_error(file, tool_fault("ruff check", r.returncode,
                                        r.stderr or r.stdout or ""), dur)
        return

    try:
        items = json.loads(body) if body else []
    except ValueError:
        _adapter_error(file, tool_fault("ruff check", r.returncode,
                                        r.stdout or r.stderr or ""), dur)
        return

    if not isinstance(items, list):
        _adapter_error(file, tool_fault("ruff check", r.returncode,
                                        "expected a JSON array, got "
                                        "{0}".format(type(items).__name__)), dur)
        return

    errors = []
    for item in items:
        if not isinstance(item, dict):
            continue
        location = item.get("location") or {}
        errors.append({
            "line": location.get("row"), "col": location.get("column"),
            "severity": "warning", "code": item.get("code"),
            "msg": (item.get("message") or "").strip().replace("\n", " ")[:300],
        })

    emit({"tool": TOOL, "file": file, "ok": not errors, "count": len(errors),
          "errors": errors, "duration_ms": int((time.time() - start) * 1000)})


if __name__ == "__main__":
    guard_main(TOOL, main)
