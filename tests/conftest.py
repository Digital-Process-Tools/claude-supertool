"""Shared fixtures and helpers for supertool tests."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import supertool  # noqa: E402


def pytest_configure(config):  # noqa: ARG001
    """Opt out of #146 cwd containment for the test suite.

    `_safe_path` enforces that op paths resolve under cwd unless
    `SUPERTOOL_ALLOW_OUTSIDE_CWD=1` is set. Tests use tmp_path fixtures
    (under /tmp/pytest-...) so we flip the env var on for the whole suite.
    Security regression tests that need strict mode unset it via
    `monkeypatch.delenv("SUPERTOOL_ALLOW_OUTSIDE_CWD", raising=False)`.
    """
    import os
    os.environ.setdefault("SUPERTOOL_ALLOW_OUTSIDE_CWD", "1")
    os.environ.setdefault("SUPERTOOL_ALLOW_VIM_SHELL", "1")
    # Tests that spawn supertool as a subprocess must not delegate `read` to
    # rtk (different output format breaks byte-identical assertions). The
    # in-process _disable_rtk_and_config fixture covers same-process tests;
    # SUPERTOOL_NO_RTK covers subprocess-spawned tests like test_parallel.
    os.environ.setdefault("SUPERTOOL_NO_RTK", "1")
    # Disable cursor/undo persistence so tests start with a clean cursor=0
    # regardless of what a previous pytest run left in ~/.cache/supertool/.
    # Tests that explicitly exercise persistence (test_vim_persist*) unset
    # this via monkeypatch.delenv("SUPERTOOL_VIM_NO_PERSIST").
    os.environ.setdefault("SUPERTOOL_VIM_NO_PERSIST", "1")
    # #149: publish-body allowlist + confirm gate. Existing publish tests use
    # `tmp_path` for body files (outside the production .max/ / drafts/ /
    # posts/ / blog/ allowlist) and don't `|force`, so opt the suite in.
    # Security regression tests in test_security_publish.py unset these.
    # tmp_path lives under tempfile.gettempdir() — /tmp (Linux), /var/folders
    # (macOS), C:\Users\...\AppData\Local\Temp (Windows). Joining with the
    # platform's path separator (os.pathsep) keeps the Windows drive-letter
    # colon from being interpreted as a list separator.
    import tempfile
    _tmp_roots = [tempfile.gettempdir(), "/tmp", "/var/folders", "/private/var/folders"]
    os.environ.setdefault(
        "SUPERTOOL_PUBLISH_BODY_ALLOWLIST",
        os.pathsep.join(_tmp_roots),
    )
    os.environ.setdefault("SUPERTOOL_NO_PUBLISH_CONFIRM", "1")


_GIT_DIRS_CACHE = "unset"


def _repo_git_dirs():
    """Locate the real suite repo's git dirs, or None when not in a repo.

    Returns ``(common_dir, git_dir)``. In a normal clone both are ``.git``; in
    a worktree ``common_dir`` is the shared ``.git`` (where ``config`` and
    ``refs/heads`` live) and ``git_dir`` is the per-worktree dir (where ``HEAD``
    lives). The corruption we guard against — ``core.bare=true`` + junk commits
    on master from a test running git against an ambient repo — lands in the
    common dir, so that is what we fingerprint.

    Cached after the first call: the dirs never move during a run, and the two
    ``git rev-parse`` subprocesses would otherwise fire once per test (~7k
    spawns over the suite — minutes of pure overhead).
    """
    global _GIT_DIRS_CACHE
    if _GIT_DIRS_CACHE != "unset":
        return _GIT_DIRS_CACHE
    import subprocess
    root = Path(__file__).resolve().parent.parent
    result = None
    try:
        common = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
        gitdir = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=5,
        )
        if common.returncode == 0 and gitdir.returncode == 0:
            result = (
                (root / common.stdout.strip()).resolve(),
                (root / gitdir.stdout.strip()).resolve(),
            )
    except (OSError, subprocess.SubprocessError):
        result = None
    _GIT_DIRS_CACHE = result
    return result


def _git_state_fingerprint(dirs):
    """Hash the bits a leaking test would mutate: config, HEAD, and every head ref."""
    import hashlib
    common_dir, git_dir = dirs
    h = hashlib.sha256()
    for p in (common_dir / "config", git_dir / "HEAD", common_dir / "packed-refs"):
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<absent>")
    heads = common_dir / "refs" / "heads"
    if heads.is_dir():
        for ref in sorted(heads.rglob("*")):
            if ref.is_file():
                h.update(str(ref.relative_to(heads)).encode())
                h.update(ref.read_bytes())
    return h.hexdigest()


@pytest.fixture(autouse=True)
def _guard_repo_git_state():
    """Fail any test that mutates the suite repo's own git state (#319).

    Tests must build git fixtures inside ``tmp_path`` and run every git call
    with ``cwd=tmp_path`` (or ``git -C tmp_path``). A test that runs ``git
    init``/``commit``/``config`` against the ambient cwd corrupts the real repo
    — and when the suite runs inside a *worktree* (shared ``.git``), it
    corrupts the main checkout too (``core.bare=true``, junk commits on master).
    A standalone clone hides this; this guard surfaces the culprit by name.
    """
    dirs = _repo_git_dirs()
    before = _git_state_fingerprint(dirs) if dirs else None
    yield
    if before is None:
        return
    after = _git_state_fingerprint(dirs)
    assert before == after, (
        "test mutated the suite repo's git state (config/HEAD/refs changed) — "
        "build git fixtures in tmp_path and pass cwd=tmp_path to every git call. "
        "See conftest._guard_repo_git_state / issue #319."
    )


# Module-level mutable state that must not survive a test (#397). Every entry
# is per-invocation scratch or a cache; the fixture below restores each to its
# import-time value between tests, in place, because supertool holds direct
# references to these objects. test_state_reset_and_lint_timeout.py fails when
# a new mutable global appears in neither tuple — the forgetting is otherwise
# silent, and shows up as a test that passes alone and fails in suite order.
RESET_GLOBALS = (
    "_BRANCH_CACHE",
    "_FORMATTER_SKIPS",
    "_FORMAT_QUEUE",
    "_MUTATION_ATTEMPTS",
    "_REPO_ROOT_WALK_CACHE",
    "_VALIDATOR_DEFER_QUEUE",
    "_VALIDATOR_DEFER_SEEN",
    "_VALIDATOR_FINGERPRINT_CACHE",
    "_WRITE_COUNT",
    "_WRITE_WARNINGS",
)

# Not scratch, for four different reasons.
#  - _MCP_SERVERS holds live MCP client objects; clearing it drops warm daemon
#    connections that outlive a single test on purpose.
#  - _CONFIG is already saved and restored by name in the fixture below.
#  - _AT_FILE_REGISTRY is built once, guarded by _AT_FILE_REGISTRY_BUILT, and
#    _build_at_file_registry rebinds the name rather than mutating. Emptying it
#    in place is permanent: the guard means it never rebuilds.
#  - the rest are constant lookup tables that happen to be dicts/lists/sets.
#    They are read, never written. Resetting them would be harmless but says
#    something untrue about their lifetime.
RESET_EXEMPT_GLOBALS = (
    "_MCP_SERVERS",
    "_CONFIG",
    "_AT_FILE_REGISTRY",
    "_AROUND_DIR_SKIP",
    "_AT_FILE_BUILTIN_DEFAULTS",
    "_BUILTIN_OPS",
    "_EXT_FAMILIES",
    "_FORMATTER_CONFIG_MARKERS",
    "_NONDETERMINISTIC_ERROR_CODES",
    "_OP_TARGETS",
    "_PARALLEL_SAFE_OPS",
    "_PLAIN_MARKERS",
    "_READ_OP_TARGETS",
    "_REGEX_PATTERNS",
    "_TOML_ESCAPES",
    "_TS_DEF_NODES",
    "_TS_DEF_NODES_DEFAULT",
    "_TS_LANG_MAP",
)

_PRISTINE_GLOBALS = {
    name: copy.deepcopy(getattr(supertool, name)) for name in RESET_GLOBALS
}


def _reset_module_state():
    for name in RESET_GLOBALS:
        current = getattr(supertool, name)
        pristine = copy.deepcopy(_PRISTINE_GLOBALS[name])
        if isinstance(current, dict):
            current.clear()
            current.update(pristine)
        elif isinstance(current, set):
            current.clear()
            current.update(pristine)
        else:
            current[:] = pristine


@pytest.fixture(autouse=True)
def _disable_rtk_and_config():
    """Disable RTK delegation, config cache, tree-sitter, and ctags in tests."""
    import os
    _reset_module_state()
    old_rtk_checked = supertool._RTK_CHECKED
    old_rtk_path = supertool._RTK_PATH
    old_config_checked = supertool._CONFIG_CHECKED
    old_config = supertool._CONFIG
    old_ts_checked = supertool._TS_CHECKED
    old_ts_available = supertool._TS_AVAILABLE
    old_ts_package = supertool._TS_PACKAGE
    old_ctags_checked = supertool._CTAGS_CHECKED
    old_ctags_path = supertool._CTAGS_PATH
    supertool._RTK_CHECKED = True
    supertool._RTK_PATH = None
    supertool._CONFIG_CHECKED = True
    supertool._CONFIG = {}
    supertool._TS_CHECKED = True
    supertool._TS_AVAILABLE = False
    supertool._TS_PACKAGE = ""
    supertool._CTAGS_CHECKED = True
    supertool._CTAGS_PATH = None
    supertool._IN_ALIAS = False
    yield
    supertool._IN_ALIAS = False
    _reset_module_state()
    supertool._RTK_CHECKED = old_rtk_checked
    supertool._RTK_PATH = old_rtk_path
    supertool._CONFIG_CHECKED = old_config_checked
    supertool._CONFIG = old_config
    supertool._TS_CHECKED = old_ts_checked
    supertool._TS_AVAILABLE = old_ts_available
    supertool._TS_PACKAGE = old_ts_package
    supertool._CTAGS_CHECKED = old_ctags_checked
    supertool._CTAGS_PATH = old_ctags_path
    # Restore NO_PERSIST so tests that use os.environ.pop() don't leave the
    # flag absent for subsequent tests (pytest does not restore env vars set
    # via os.environ directly — only monkeypatch does).
    os.environ["SUPERTOOL_VIM_NO_PERSIST"] = "1"


@pytest.fixture
def enable_rtk():
    """Re-enable RTK detection for integration tests."""
    supertool._RTK_CHECKED = False
    supertool._RTK_PATH = None
    yield
    supertool._RTK_CHECKED = True
    supertool._RTK_PATH = None


@pytest.fixture
def enable_ctags():
    """Re-enable ctags detection for integration tests."""
    supertool._CTAGS_CHECKED = False
    supertool._CTAGS_PATH = None
    yield
    supertool._CTAGS_CHECKED = True
    supertool._CTAGS_PATH = None


@pytest.fixture
def enable_tree_sitter():
    """Re-enable tree-sitter detection for integration tests."""
    supertool._TS_CHECKED = False
    supertool._TS_AVAILABLE = False
    supertool._TS_PACKAGE = ""
    # Also disable ctags so tree-sitter tier takes priority
    supertool._CTAGS_CHECKED = True
    supertool._CTAGS_PATH = None
    yield
    supertool._TS_CHECKED = True
    supertool._TS_AVAILABLE = False
    supertool._TS_PACKAGE = ""


def _has_any_tree_sitter() -> bool:
    try:
        from tree_sitter_language_pack import get_parser  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        from tree_sitter_languages import get_parser  # noqa: F401
        return True
    except ImportError:
        return False
