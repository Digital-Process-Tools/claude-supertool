"""Shared fixtures and helpers for supertool tests."""
from __future__ import annotations

import copy
import functools
import json
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import supertool  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))

# Tolerated rather than assumed, for the same reason `_paths` and `_env` are
# below: `test_git_state_guard.py` and `test_git_env_leak_416.py` copy THIS FILE
# alone into a synthetic repo and run pytest there, and `_symlink.py` is not
# copied with it. A hard import turned five of those tests red -- the guard
# suites could not start at all. Caught by the full local suite; a targeted run
# of the files this change touched would never have executed them.
#
# The absence is not swallowed. Both hooks below report `not available` in words
# when this is None, because a header that silently omits its verdict is
# indistinguishable from a run where symlinks work.
try:
    import _symlink  # noqa: E402
except ImportError:  # pragma: no cover - only in the synthetic-repo suites
    _symlink = None

# Same tolerance, same reason (#1360): the synthetic-repo suites copy this file
# alone, so `_lint_budget.py` is not there either. Reported in words rather than
# omitted, for the same reason as above.
try:
    import _lint_budget  # noqa: E402
except ImportError:  # pragma: no cover - only in the synthetic-repo suites
    _lint_budget = None

# Same tolerance, same reason (#1604): the synthetic-repo suites copy this file
# alone, so `_adapter_verdict.py` is not there either. Reported in words rather
# than omitted, for the same reason as above.
try:
    import _adapter_verdict  # noqa: E402
except ImportError:  # pragma: no cover - only in the synthetic-repo suites
    _adapter_verdict = None

# Same tolerance, same reason (#1568): the live-GitHub reachability guard and
# its countable skip. Reported in words rather than omitted, as above.
try:
    import _live_gh  # noqa: E402
except ImportError:  # pragma: no cover - only in the synthetic-repo suites
    _live_gh = None

# Same tolerance, same reason: the `git status` decline gate behind read's
# working-tree marker, and its countable skip.
try:
    import _git_decline  # noqa: E402
except ImportError:  # pragma: no cover - only in the synthetic-repo suites
    _git_decline = None

# Same tolerance, same reason (#1998): the class guard for the #1981 race --
# no test may create a `.py` file inside `repo_python_files()`'s walk root at
# runtime. Not available in the synthetic-repo suites, which have no
# `tests/_repo_walk.py` to import and are not scanned by anything that reads
# it, so there is nothing here for the guard to protect.
try:
    import _write_guard  # noqa: E402
except ImportError:  # pragma: no cover - only in the synthetic-repo suites
    _write_guard = None

# Same tolerance, same reason (#1523). This one is registered as a plugin rather
# than called from the hooks below: it needs `pytest_runtest_makereport`,
# `pytest_runtest_logreport` and `pytest_sessionfinish` as well as a summary
# line, and re-declaring four hooks here to forward to four hooks there is three
# more places for the wiring to be half-done.
try:
    import _core_timeout_census  # noqa: E402
except ImportError:  # pragma: no cover - only in the synthetic-repo suites
    _core_timeout_census = None

# `presets/mcp/_paths.py` probes its platform capabilities at *import*
# (`_LISTDIR_TAKES_FD`, `_RELATIVE_OPS`, `_ANCESTRY_DIR_FD`) precisely so that a
# test double installed over `os.listdir`/`os.chmod`/`os.open` is never read as
# a missing syscall. That reasoning holds only if the import wins the race
# against the first double. In production it always does — the import happens at
# startup. In a pytest worker it does not: every suite here imports `_paths`
# from *inside* a test body, so under `-n auto` the winner depends on how xdist
# happened to split the files. A worker whose first import landed inside
# `test_mcp_runtime_dir_mode_568.py`'s `no_chmod` fixture recorded the double
# and permanently believed `os.chmod(dir_fd=)` was unavailable, failing three
# unrelated #598 tests with a platform verdict invented by another test.
#
# Importing here takes every probe at collection time, before any fixture can
# run, which is the condition the probes' own comments assume. Found while
# adding the #607 suite — new tests changed the split and the landmine went off.
# Tolerated rather than assumed: `test_git_state_guard.py` copies this file
# into a synthetic repo that has no `presets/` tree and runs pytest there, so a
# hard import would turn "the probe was taken early" into "the guard suite
# cannot start". Nothing is lost when it is absent — there is no `_paths` to
# probe in that repo either.
_MCP_PRESETS = Path(__file__).parent.parent / "presets" / "mcp"
if (_MCP_PRESETS / "_paths.py").is_file():
    sys.path.insert(0, str(_MCP_PRESETS))
    import _paths  # noqa: E402,F401


# `presets/_env.py` is imported by 29 presets under the single module name
# `_env`, so all of them share one `_ANNOUNCED` ledger — by design: `env_int`
# says each distinct notice at most once per process, because a knob read once
# per file would otherwise print the same line ten times over the output it is
# warning about (#654).
#
# Per *process* is the right unit in production, where a preset is a subprocess
# that reads its knobs and exits. It is the wrong unit in a pytest worker, which
# is one process running many presets' suites. `test_github_prs.py` and
# `test_gitlab_mrs.py` both set `SUPERTOOL_ENRICH_WORKERS=0` against a default of
# 8, so both produce a byte-identical notice; whichever ran second in a given
# worker read an empty `capsys` and failed its assertion. That is why this
# surfaced as a macOS-only red on PR #689 and nowhere else — xdist splits by
# worker count, the two files shared a worker only on the runners with the
# smaller core count, and the split, not the platform, decided it.
#
# `supertool._ENV_ANNOUNCED` is the same ledger for the same reason and has been
# per-run scratch since #397 (`RESET_GLOBALS`). This one was missed because it
# lives in a different module. Same treatment, in `_reset_module_state` below.
#
# Tolerated rather than assumed, exactly as `_paths` above: `test_git_state_
# guard.py` copies this file into a synthetic repo that has no `presets/` tree.
_PRESETS = Path(__file__).parent.parent / "presets"
_env = None
if (_PRESETS / "_env.py").is_file():
    sys.path.insert(0, str(_PRESETS))
    import _env  # noqa: E402


# The transport seam is enforced by the socket, not by a convention (#1341).
#
# `presets/_http.py` binds `_OPEN = _OPENER.open` at import and every preset
# calls through that name, so a test stubbing `MODULE.urllib.request.urlopen`
# replaces a name nothing consults: the request leaves the machine and the test
# passes on whatever the internet answers. Two tests in
# `test_security_error_echo_691.py` were in that state for months, one of them a
# credential-redaction regression test whose injected payload had never once
# been delivered (#1312).
#
# A rename check does not reach that. The stubbed name *existed* and was live —
# it simply was not the one the product calls — and only observing the transport
# can tell those two apart. #1312 measured both over 559 test modules: a static
# grep for transport tokens found 2 of the 3 live leaks, the socket recorder
# found 3 of 3. The one it alone caught contained no transport token at all,
# because it was a missing stub rather than a wrong one.
#
# Blind spot — and it is several things, not the one thing this comment used to
# name (#1584). "It binds `socket` in the pytest process only" read as a closed
# list, and it was not: `connect_ex`, four `gethost*`/`getnameinfo` resolvers
# and a UDP `sendto` all walked out of an armed context until #1584 patched
# them. The boundary now lives in `_netblock.ROUTES` / `SOCKET_ROUTES`, which
# classify every callable `socket` exposes, and in `BEYOND_THE_PROCESS`, which
# states the four things no in-process patch can reach. A register goes red
# when a route arrives unclassified; a sentence does not.
#
# It goes red on the leg that has the route, though, and that is a boundary of
# its own: the population is `dir(socket)` on the running interpreter, so a
# Windows-only name is invisible to every POSIX author and to every POSIX leg.
# `socket.socket.ioctl` arrived that way and reddened four Windows legs one PR
# after the register shipped. `_netblock.PLATFORM_ONLY` states the win32 names
# so they are classified before the leg finds them (#1642).
#
# Loopback and AF_UNIX stay open on purpose — `test_http_bounds.py` and the
# `claude-channel` suites bind real servers on 127.0.0.1 and those are
# hermetic.
#
# Tolerated rather than assumed, exactly as `_paths` and `_env` above.
_netblock = None
if (Path(__file__).parent / "_netblock.py").is_file():
    sys.path.insert(0, str(Path(__file__).parent))
    import _netblock  # noqa: E402


@pytest.fixture(autouse=True)
def _block_outbound_network():
    """Refuse non-loopback sockets for every test in the suite (#1341).

    It builds its **own** `MonkeyPatch` rather than requesting the shared
    `monkeypatch` fixture, and that is not a style choice. An autouse fixture
    defined this early in the file is set up first and therefore torn down
    *last*; asking it for `monkeypatch` instantiates that fixture here, which
    moves its `undo()` to after `_guard_repo_git_state`'s teardown. Measured:
    `test_git_resolve.py` and `test_git_resolve_validate_scope_876.py`
    monkeypatch `os.path.isfile` to `lambda p: True`, CPython 3.13+ `pathlib`
    routes `Path.is_file()` through `os.path.isfile`, and the guard's
    after-snapshot then read every *directory* under `refs/heads/` as a ref
    file it could not open. Each such test's teardown errored with six
    fabricated ref mutations, for 18 errors across those two files — plus one
    more in a concurrent worker, which is the shared-repo fan-out #433
    documents. A private context cannot reorder anything.
    """
    if _netblock is None:
        yield
        return
    with pytest.MonkeyPatch.context() as mp:
        _netblock.block_outbound(mp)
        yield


#: The per-process cache directory `pytest_configure` redirects `XDG_CACHE_HOME`
#: at (#1656), held here rather than on `config` so that the `.cleanup()` in
#: `pytest_unconfigure` is a removal
#: `tests/test_directory_removal_ownership_1635.py` can prove rather than one it
#: cannot see. A list of one, because the walk resolves a `for` target back to
#: what was appended and that is the shape it can follow (#1683).
_CACHE_HOMES: list = []


def pytest_configure(config):
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
    # #1523: put a floor under the #1501 skip. Registered in EVERY process,
    # controller and xdist worker alike, because the two halves live on
    # different sides: the counting hook (`pytest_runtest_makereport`) can only
    # run where the test ran, and the verdict (`pytest_sessionfinish`) only
    # means anything where the exit status is the run's. Registering on the
    # controller alone was measured on 2026-08-13 to report `NOT CHECKED` under
    # `-n auto` while 42 gated calls ran -- the census reduced to the exact
    # absence it exists to detect. The worker-side suppression of the verdict is
    # in `_core_timeout_census` itself, next to the hooks it suppresses.
    if _core_timeout_census is not None:
        _core_timeout_census.reset_totals()
        config.pluginmanager.register(_core_timeout_census,
                                      "core-timeout-census")
    # No test may resolve the watch channel against whoever is running it.
    # `presets/watch/{transport,channel,radar}` resolve `RESOLVED` at *import*,
    # so a `SUPERTOOL_WATCH_NAME` exported in the shell lands before any fixture
    # can intervene, and it is exported in this repo's own `.supertool.json`
    # since #1477 — every maintainer's environment. Measured under
    # `SUPERTOOL_WATCH_NAME=oss-supertool`: six tests red across three files,
    # four asserting `radar`'s exact stdout (which now carries a channel banner,
    # #1495) and two in `test_watch_sock_path_581.py` asserting the *default*
    # socket after deleting only the override. None of the six says anything
    # about the code, and CI exports none of the three, so the failures are
    # invisible from the only place that is authoritative.
    #
    # Deleted rather than `setdefault`-ed to "": an empty value is a state
    # `naming.resolve` reads deliberately (an operator who exports nothing gets
    # the default rather than a refusal about a name they did not set), and a
    # suite that pins the empty case cannot also exercise the absent one. Tests
    # that want a channel pass an env mapping to `naming.resolve` or monkeypatch
    # `RESOLVED`; nothing needs these to be inherited.
    for _watch_var in ("SUPERTOOL_WATCH_NAME", "SUPERTOOL_WATCH_SOCK",
                       "SUPERTOOL_WATCH_STATE_DIR"):
        os.environ.pop(_watch_var, None)
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
    # #1329: `read` elides a repeat read of a byte-identical file, keyed per
    # session in ~/.cache/supertool/read-elide. A pytest process is ONE session
    # by that key, so without this any test that reads the same path twice
    # would get an elision line instead of content — and it would write into
    # the developer's real cache doing it. test_read_elide_unchanged_1329.py
    # opts back in via monkeypatch.delenv after redirecting XDG_CACHE_HOME.
    os.environ.setdefault("SUPERTOOL_READ_NO_ELIDE", "1")
    # #474: the opportunistic cache GC is armed on every invocation and fires
    # at most once an hour. A test run must not reap the developer's real
    # ~/.cache/supertool as a side effect. test_gc_474.py opts back in with
    # monkeypatch.delenv after redirecting XDG_CACHE_HOME at a tmp_path.
    os.environ.setdefault("SUPERTOOL_GC_DISABLE", "1")
    # #643: freeze every duration supertool measures and prints. The
    # `[validators]` time column and a custom op's `PASS (0.02s)` header are
    # wall clock, so two runs of the same op render two different strings. Any
    # test comparing two rendered blocks can then pass on that jitter alone —
    # and it fails in the dangerous direction: #621's own invariant test went
    # green, reporting the defect fixed, while the defect was fully present.
    # With this set, a comparison can only ever see a real difference. Tests
    # that exercise the real timing path monkeypatch.delenv it (see
    # test_render_determinism_643.py); where the switch cannot reach — recorded
    # fixtures, a subprocess with its own environment — use
    # `tests/_render.py::stable_render`.
    os.environ.setdefault("SUPERTOOL_DETERMINISTIC_TIME", "1")
    # #553: the post-edit lint budget is a guard against a linter that stalled,
    # not a stopwatch on the runner. `xmllint --noout` on a two-line file is a
    # ~7ms operation; the 5s default is three orders of magnitude of headroom,
    # and a GH Windows runner under `-n auto` xdist (Defender scanning every
    # freshly written temp file, two cores, ~4000 tests) has still blown it
    # twice on master. The decline that correctly follows then reads as a red
    # leg. The suite asserts verdicts, so give it room to obtain one. This is a
    # property of the runner, never of supertool: the shipped default stays 5s
    # (pinned by test_the_suite_budget_does_not_move_the_product_default), a
    # real env still wins via setdefault, and tests that pin the timeout itself
    # monkeypatch.setenv over this.
    os.environ.setdefault("SUPERTOOL_LINT_TIMEOUT", "30")
    # #650: same call, one layer along, but on weaker evidence — say so rather
    # than inherit the lint budget's confidence.
    #
    # #650 was filed as "5s is reachable with 11 xdist workers hammering one
    # object store". That did not survive measurement on the machine that
    # reported it: worst git latency was 0.30s while the real suite ran, and
    # 0.76s under 96 CPU burners on 11 cores — 6x inside the budget at a load
    # no CI runner will see. So the *cause* of that one pre-push stall is still
    # unknown, and this line is not a diagnosis of it.
    #
    # What justifies the line anyway is the leg above: a 2-core Windows runner
    # with Defender scanning every freshly written temp file has blown the lint
    # budget twice on master. That is a measured fact about an environment this
    # suite actually runs in and my laptop is not, and git spawns are the same
    # shape of work. Generalising a macOS measurement to that runner would be
    # the same overreach the issue made in the other direction.
    #
    # It is insurance, not the fix. The fix is that the product now declines
    # instead of crashing, which is what made the #650 red fatal — see
    # presets/git/status.py::_git. As with the lint budget: a property of the
    # runner, never of supertool. The shipped default stays 5s, pinned by
    # test_git_timeout_disclosure_650.py::test_the_suite_budget_does_not_move_the_product_default.
    os.environ.setdefault("SUPERTOOL_GIT_TIMEOUT", "30")
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
    # #1656: every cache supertool writes goes through `_supertool._cache_root()`,
    # which is the one place `~/.cache/supertool` is spelled and reads
    # XDG_CACHE_HOME per call. Point it at a directory this process owns, so the
    # suite stops writing into the operator's real cache — measured at 52 files
    # / 288 KB per full run, all of it the #1650 `paste-backup` store, on a
    # machine where that cache is also the thing under test.
    #
    # Set, not `setdefault`. The three switches above are `setdefault` because a
    # real environment should be able to win; here the operator's environment is
    # exactly what must not be written to, so an exported XDG_CACHE_HOME is the
    # case this exists for rather than an override of it.
    #
    # This subsumes rather than replaces `SUPERTOOL_READ_NO_ELIDE`,
    # `SUPERTOOL_GC_DISABLE` and `SUPERTOOL_VIM_NO_PERSIST` above: those three
    # turn individual stores off, and a store added tomorrow gets no line. This
    # is the floor under all of them, including the ones that do not exist yet.
    # Its blast radius is those same three stores plus paste-backup — the
    # alternative repairs (a `SUPERTOOL_PASTE_NO_BACKUP` product knob disabling
    # a data-loss net, or a test-only cache root that is an env var by another
    # name) both add product surface this does not, and both fix one instance.
    #
    # Per process, so xdist workers do not share one and two concurrent suites
    # in one checkout cannot race. Taken down in `pytest_unconfigure`.
    #
    # `TemporaryDirectory` rather than `mkdtemp` + `shutil.rmtree`, and that is
    # the #1635 register talking rather than taste: a teardown that composes a
    # path out of an attribute cannot prove it owns what it deletes, and
    # `test_directory_removal_ownership_1635.py` classified the rmtree UNOWNED
    # on the commit that added it. The object removes only the directory it
    # made, and nothing here composes a path at all, so the hazard is absent by
    # construction rather than argued.
    #
    # `.cleanup()` is one of that register's removal verbs since #1683, so this
    # site is proved by it rather than invisible to it. What the proof needs is
    # a chain the walk can follow: the object goes into the module list above
    # and comes back out through the teardown's `for`, which resolves to the
    # `TemporaryDirectory` call here. Stashed on `config` and fetched back with
    # `getattr` -- the spelling this used until #1683 -- it reads UNOWNED, and
    # correctly so: nothing in the file says what came off that attribute.
    cache_home = tempfile.TemporaryDirectory(prefix="supertool-suite-cache-")
    _CACHE_HOMES.append(cache_home)
    os.environ["XDG_CACHE_HOME"] = cache_home.name
    # #1892: every fixture in this suite creates its temp git repos with a
    # bare `subprocess.run(["git", ...])`, none of them pass `env=`, and
    # `subprocess.run` inherits the parent process's environment by default
    # -- so setting this ONCE here, before any fixture runs, reaches every
    # one of them without touching the 27+ files that call `git init`
    # directly. The alternative (a per-fixture or per-helper change) was
    # considered and rejected: there is no single shared repo-creation
    # helper to patch, so that route would need 27 separate edits and any
    # new one written tomorrow would silently be uncovered.
    #
    # `-c core.fsmonitor=...` on an individual git invocation would win
    # over this (test_the_pin_outranks_the_environment_too in
    # test_git_status_display_config_gates_1295.py measures the same
    # precedence for a different key), so a test that deliberately wants
    # the daemon can still force it that way; nothing here can be defeated
    # by that except on purpose.
    #
    # A repo that inherits an ambient `core.fsmonitor = true` (an ordinary
    # user preference, not a defect) spawns a detached
    # `git fsmonitor--daemon run --detach` that outlives the process which
    # started it, because the repo is deleted rather than closed. Three
    # independent measurements on one machine on 2026-08-21 found 965-979
    # orphaned daemons at `ppid 1`, and a full suite run hung at 98% for
    # over an hour with them present (#1892). `setdefault`, like the other
    # env knobs above: an operator who has genuinely set GIT_CONFIG_COUNT/
    # KEY_0/VALUE_0 for their own purpose is not overridden by the suite.
    os.environ.setdefault("GIT_CONFIG_COUNT", "1")
    os.environ.setdefault("GIT_CONFIG_KEY_0", "core.fsmonitor")
    os.environ.setdefault("GIT_CONFIG_VALUE_0", "false")
    # #416: the autouse fixture below cannot cover collection-time module
    # bodies or session helpers, which run before any fixture. Scrub once here
    # too, and remember what was leaked so it gets reported rather than hidden.
    config._supertool_leaked_git_env = scrub_git_env()
    # #1998: installed once, for the life of the process, rather than as a
    # per-test fixture -- the #1981 race is not scoped to "whichever test is
    # currently running". Under `-n auto` the offending write and the
    # competing repo-wide walk can be in two different tests in two
    # different workers, so the only scope that covers every window is
    # "the whole time this worker is alive". Taken down in
    # `pytest_unconfigure`, symmetrically with the cache-home redirect above.
    if _write_guard is not None:
        _write_guard.install()


def pytest_unconfigure(config):
    """Take the #1656 cache redirect's directory back down.

    Best effort and deliberately silent: a suite that cannot remove its own
    temp directory has still run correctly, and raising here would turn a
    cleanup failure into a red on work that passed. Windows in particular
    raises when a file under it is still open, and `ignore_cleanup_errors` is
    3.10+ while CI runs 3.9. The directory is under `tempfile.gettempdir()`
    either way, so the worst outcome is one the OS reaps rather than the
    operator's cache being written to.
    """
    for cache_home in _CACHE_HOMES:
        try:
            cache_home.cleanup()
        except OSError:
            pass
    del _CACHE_HOMES[:]
    # #1998: symmetric with the install in pytest_configure. Best-effort in
    # the same spirit as the cleanup above is not needed here -- uninstall()
    # only ever restores two saved function references, which cannot itself
    # raise.
    if _write_guard is not None:
        _write_guard.uninstall()


def pytest_report_header(config):
    """State what this run can and cannot do, before it does any of it.

    Two things, both of which have to be visible on a *green* leg: a leaked git
    environment (#416), and whether this platform/user can create a symlink
    (#1143). The second is here rather than in the summary on purpose -- a
    blind spot announced only when something fails is announced exactly when
    nobody needs telling.
    """
    lines = [
        _symlink.verdict_line() if _symlink is not None
        else "symlink capability: NOT CHECKED -- tests/_symlink.py is not in this "
             "tree (a synthetic-repo run); this is not a claim that symlinks work"
    ]
    leaked = getattr(config, "_supertool_leaked_git_env", [])
    if leaked:
        lines.append(
            "scrubbed inherited git env (would have run tests against this repo, "
            f"see #416): {', '.join(leaked)}"
        )
    return lines


#: What the count above is a count OF, printed next to it every time (#1274).
#:
#: #1232 fixed the loud failure of this line -- it read `0 symlink-dependent
#: tests did NOT run` while four tests were failing for exactly that reason.
#: The quiet one survived: the number counts skips carrying `TOKEN`, and two of
#: the mechanisms that keep a symlink call site off a privilege-less runner
#: produce no such skip, so a plausible non-zero number read as a total. It is
#: a subset, and stating which subset is the only thing that cannot be
#: misread. It stays a subset by choice: `needs_nofollow` fires at collection
#: on every Windows runner, privileged or not, so stamping the token onto it
#: would assert a reason nothing ever measured -- an invented claim in place of
#: a missing one. `tests/test_symlink_gating_register_1232.py` holds the whole
#: population, mechanism by mechanism, derived from the AST rather than listed.
_POPULATION = (
    "  ^ counts skips carrying that token only, not every symlink-dependent "
    "test: one held off this runner by an unrelated collection-time marker "
    "(no O_NOFOLLOW, a posix-only class) skips without it, and a symlink call "
    "inside an `except OSError` arm does not skip at all. Full population: "
    "tests/test_symlink_gating_register_1232.py")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Resolve the suite's skip count into its stated reasons, and say so.

    `688 skipped` is a number. `N of 688 skipped, because this runner has no
    create-symlink privilege` is a fact somebody can act on -- it is the
    difference between an absence in the world and an absence the tooling
    produced. Five reasons are broken out that way, each in its own helper
    below: the create-symlink privilege (#1143), the post-edit lint budget
    (#1360), an adapter that spent its whole internal budget without reaching
    a verdict (#794/#1604), the live GitHub API (#1568) and a `git status` that
    would not answer about a path's working-tree state (#705). This sentence
    said "two" for as long as there were three, and "four" for as long as there
    were five -- the count is the thing that goes stale, so it is derived
    nowhere and stated here once.

    All of them print whether the count is zero or not -- silence would be
    indistinguishable from not having looked -- and each prints its denominator
    and its population next to the number, so a non-zero count is not read as a
    total either (#1274). None is reached through another: this hook used to
    return early when `_symlink` was absent from the tree, which would now omit
    the lint line silently -- the same absence, one layer up (#1360).
    """
    skipped = terminalreporter.stats.get("skipped", []) or []
    _symlink_summary(terminalreporter, skipped)
    _lint_budget_summary(terminalreporter, skipped)
    _adapter_wall_summary(terminalreporter, skipped)
    _live_gh_summary(terminalreporter, skipped)
    _git_decline_summary(terminalreporter, skipped)
    _write_guard_summary(terminalreporter)


def _token_skips(skipped, token: str) -> int:
    """How many of `skipped` carry `token` in their stated reason.

    One reader for three counters (#1568). The two above grew the same six
    lines independently, and the third would have been a third copy of a
    `longrepr` shape that is a tuple on some paths and an object on others --
    the kind of duplication that goes wrong in one copy and stays right in the
    others, which is exactly how #1232 read `0` while four tests were failing
    for the reason it was counting.
    """
    n = 0
    for report in skipped:
        longrepr = getattr(report, "longrepr", None)
        text = longrepr[2] if isinstance(longrepr, tuple) and len(longrepr) > 2 else str(longrepr)
        if token in str(text):
            n += 1
    return n


def _symlink_summary(terminalreporter, skipped):
    if _symlink is None:
        terminalreporter.write_line(
            "symlink capability: NOT CHECKED -- tests/_symlink.py is not in this tree")
        return
    n = _token_skips(skipped, _symlink.TOKEN)
    available, why = _symlink.symlink_support()
    if available:
        terminalreporter.write_line(
            f"{_symlink.TOKEN}: available -- {n} of {len(skipped)} skipped tests "
            f"carry this token (expect 0 where the privilege is present)")
    else:
        terminalreporter.write_line(
            f"{_symlink.TOKEN}: unavailable -- {n} of {len(skipped)} skipped tests "
            f"did NOT run for this reason. Reason: {why}")
    terminalreporter.write_line(_POPULATION)


def _live_gh_summary(terminalreporter, skipped):
    """Count the skips where the live GitHub API was not reached (#1568).

    Same shape as the two above, same reason: the suite has exactly one test
    that talks to real GitHub, and a run that could not reach the API has not
    exercised the shapes it is there for. Printed at zero too -- a
    silently-skipped live test is the absence-read-as-clean defect this repo
    files against itself -- and with its denominator and its population, so a
    non-zero count is not read as a total (#1274).

    Two numbers, because the states are not interchangeable. A transient
    unreachable fixes itself; an unconfigured runner produces the same skip
    forever until somebody sets a token. Summed, the second would be unreadable
    in both directions -- permanently non-zero counts stop being read, and once
    a token IS set a non-zero one can no longer be recognised as "the env line
    was deleted".

    That test is `slow` since #1568, so in a default selection this reads 0
    because it was never selected. That is not the same fact as reaching the
    API, which is why `POPULATION` says so rather than leaving the zero to be
    over-read.
    """
    if _live_gh is None:
        terminalreporter.write_line(
            "live-gh: NOT CHECKED -- tests/_live_gh.py is not in this tree")
        return
    n = _token_skips(skipped, _live_gh.TOKEN)
    unconfigured = _token_skips(skipped, _live_gh.UNCONFIGURED)
    terminalreporter.write_line(
        _live_gh.verdict_line(n, unconfigured, len(skipped)))
    terminalreporter.write_line(_live_gh.POPULATION)


def _git_decline_summary(terminalreporter, skipped):
    """Count the skips where git would not answer about a path's state.

    Same shape as the three above, same reason. `_path_meta_suffix` declines
    under a 2s budget and says so (#705); a test that compares two markers
    cannot tell that decline from a disagreement, and one such comparison
    reddened master's windows-latest leg alone at ac1b3e4. The gate turns it
    into a skip, and a skip nobody counts is the same absence one layer up --
    so this prints at zero too, with its denominator and its population
    (#1274).
    """
    if _git_decline is None:
        terminalreporter.write_line(
            "git-status decline: NOT CHECKED -- tests/_git_decline.py is not "
            "in this tree")
        return
    n = _token_skips(skipped, _git_decline.TOKEN)
    terminalreporter.write_line(_git_decline.verdict_line(n, len(skipped)))
    terminalreporter.write_line(_git_decline.POPULATION)


def _write_guard_summary(terminalreporter):
    """Say whether the #1998 class guard actually ran this session, rather
    than letting a session where it silently failed to install read exactly
    like one where it ran the whole time and caught nothing (finding from
    this PR's own audit -- a green suite with no line here is indistinguish-
    able from a green suite the guard genuinely protected).

    Checked at `pytest_terminal_summary` time, which runs before
    `pytest_unconfigure` -- `install()`'s entry from `pytest_configure` is
    still on the stack, so `is_installed()` reports the true state of the
    session that just ran, not a state already torn down.
    """
    if _write_guard is None:
        terminalreporter.write_line(
            "write-guard(#1998): NOT CHECKED -- tests/_write_guard.py is not "
            "in this tree, so no test in this run was protected against "
            "creating a .py file inside the walk root at runtime")
        return
    if _write_guard.is_installed():
        terminalreporter.write_line(
            "write-guard(#1998): installed for this session -- any write "
            "landing a .py file inside repo_python_files()'s walk root "
            "would have raised immediately")
    else:
        terminalreporter.write_line(
            "write-guard(#1998): NOT installed -- pytest_configure imported "
            "tests/_write_guard.py but install() was never called or was "
            "undone before this summary ran; no test in this session was "
            "protected")


def _adapter_wall_summary(terminalreporter, skipped):
    """Count the adapter-wall skips apart from the rest (#1604).

    The counting half of the shape #1604 chose over normalising the wall away.
    #794 has declined a stalled adapter since #1296 and the decline carried no
    token, so four instances of this class were each filed as a fresh mystery:
    `N skipped` on a loaded Windows leg said nothing about how many tests had
    failed to obtain an adapter verdict.

    Printed at zero too. A line that appears only on trouble is
    indistinguishable from one that was never evaluated, which is the defect
    this entire family is about.
    """
    if _adapter_verdict is None:
        terminalreporter.write_line(
            "adapter wall: NOT CHECKED -- tests/_adapter_verdict.py is not in "
            "this tree")
        return
    n = _token_skips(skipped, _adapter_verdict.ADAPTER_WALL_TOKEN)
    terminalreporter.write_line(
        _adapter_verdict.adapter_wall_line(n, len(skipped)))
    terminalreporter.write_line(_adapter_verdict.ADAPTER_WALL_POPULATION)


def _lint_budget_summary(terminalreporter, skipped):
    """Count the lint-budget skips apart from the rest (#1360).

    Same shape as the symlink line above, and for the same reason: a post-edit
    lint that timed out means the verdict path was never exercised on this
    runner, and a suite that reports it as a pass is the absence-read-as-presence
    defect this repo keeps filing. Printed at zero too -- silence is
    indistinguishable from not having looked -- and with its denominator and its
    population, so a non-zero count is not read as a total (#1274).
    """
    if _lint_budget is None:
        terminalreporter.write_line(
            "lint budget: NOT CHECKED -- tests/_lint_budget.py is not in this tree")
        return
    n = _token_skips(skipped, _lint_budget.TOKEN)
    terminalreporter.write_line(_lint_budget.verdict_line(n, len(skipped)))
    terminalreporter.write_line(_lint_budget.POPULATION)


# Git exports these to every hook it runs. A hook that invokes pytest (our
# .githooks/pre-push does) hands them to the whole suite, and every test that
# shells out to git then targets the REAL repo instead of its tmp_path fixture
# — fixture commits stacked on master, core.bare flipped, index desynced (#416).
# The hook scrubs them too; this layer makes the bug class unreachable from any
# caller, not just that one entry point.
#
# Imported, not re-typed: #692 was the same lesson reaching the test runner and
# never reaching the ops, and it stayed invisible partly because the list lived
# in two hand-maintained copies that nothing compared. There is now one list.
# `.githooks/pre-push` is a third consumer that cannot import Python, so a test
# pins its `unset` line to this tuple instead.
GIT_ENV_VARS = supertool.GIT_ENV_VARS


def scrub_git_env(environ=None):
    """Delete git's repo pointers from ``environ``; return the names removed."""
    import os
    env = os.environ if environ is None else environ
    removed = [name for name in GIT_ENV_VARS if name in env]
    for name in removed:
        del env[name]
    return removed


@pytest.fixture(autouse=True)
def _scrub_git_env():
    """Strip inherited git repo pointers before every test (#416).

    Never restored: the point is that no test may run with an ambient GIT_DIR.
    No test in this suite depends on one — every git fixture builds its own repo
    under ``tmp_path`` and passes ``cwd=``/``git -C``.
    """
    scrub_git_env()


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
            capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
        )
        gitdir = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
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


def _read_or_absent(path):
    try:
        return path.read_bytes()
    except OSError:
        return b"<absent>"


def _git_state_snapshot(dirs):
    """Capture the bits a leaking test would mutate: config, HEAD, and every head ref.

    A mapping rather than a hash, because a hash can only answer "did the repo
    change". The question the guard actually asks is "did *this test* change
    it" (#428), and answering that needs to know *which* key moved: the
    refs/heads/ namespace lives in the common dir, shared with every sibling
    worktree, so a hash of it reports their commits as ours.
    """
    common_dir, git_dir = dirs
    snapshot = {
        "config": _read_or_absent(common_dir / "config"),
        "HEAD": _read_or_absent(git_dir / "HEAD"),
        "packed-refs": _read_or_absent(common_dir / "packed-refs"),
        "refs": {},
    }
    heads = common_dir / "refs" / "heads"
    if heads.is_dir():
        for ref in sorted(heads.rglob("*")):
            if ref.is_file():
                snapshot["refs"][ref.relative_to(heads).as_posix()] = _read_or_absent(ref)
    return snapshot


def _head_branch(head_bytes):
    """Branch name a HEAD file points at, or None when detached or unreadable."""
    if not head_bytes:
        return None
    text = head_bytes.decode("utf-8", "replace").strip()
    prefix = "ref: refs/heads/"
    if not text.startswith(prefix):
        return None
    return text[len(prefix):] or None


def _same_path(a, b):
    """Path equality that survives Windows, where two spellings mean one directory.

    NTFS is case-insensitive, so ``C:\\\\Repo`` and ``c:\\\\repo`` are the same
    checkout; ``os.path.normcase`` is the one comparison that knows that and is
    a no-op on POSIX. Getting this wrong makes *our own* worktree read as a
    sibling — see ``_classify_git_state_change`` for why that cannot excuse a
    real violation, and ``_other_worktree_branches`` for what it would still
    cost.
    """
    import os
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def _parse_worktree_list(porcelain, root):
    """Split ``git worktree list --porcelain`` into (sibling branches, any siblings).

    ``root`` is this checkout; every other block is a sibling that shares our
    common dir and can move refs under our feet.

    ``splitlines`` rather than ``split("\\n")`` is load-bearing: git emits CRLF
    on Windows, and a trailing ``\\r`` would make every branch name miss by one
    invisible byte. It is pinned by a test that runs on every OS.
    """
    branches = set()
    has_siblings = False
    is_sibling = False
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            is_sibling = not _same_path(Path(line[len("worktree "):]).resolve(), root)
            has_siblings = has_siblings or is_sibling
        elif is_sibling and line.startswith("branch refs/heads/"):
            branches.add(line[len("branch refs/heads/"):])
    return frozenset(branches), has_siblings


def _other_worktree_branches():
    """Branches checked out elsewhere, and whether any sibling worktree exists.

    Only called once a change has already been detected, so the subprocess is
    paid on the rare path — never the ~7k times a per-test call would cost.
    """
    import subprocess
    root = Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset(), False
    if result.returncode != 0:
        return frozenset(), False
    return _parse_worktree_list(result.stdout, root)


# The dotted legacy form `[branch.feat/x]` puts the branch name inside the
# header, so the head cannot be restricted to identifier characters.
_CONFIG_SECTION_RE = re.compile(r'^\[([^]\s"]+)(?:\s+"(.*)")?\]$')


def _parse_git_config(blob):
    """Split a git config file into ``{(section, subsection, key): value}``.

    Returns ``None`` for anything it cannot read in full — an absent file, a
    line it does not recognise, bytes that are not UTF-8. The caller treats
    ``None`` as "this test's problem", so a parser that guessed would be worse
    than one that declines.

    The key is a tuple rather than a dotted string because branch names contain
    dots: ``branch.feat.x.remote`` cannot be split back into a subsection and a
    key, and guessing wrong is how ``branch.<ours>`` would read as somebody
    else's. Section and key fold to lower case (git compares them that way); a
    subsection stays verbatim (git does not).
    """
    if blob == b"<absent>":
        return None
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return None
    entries = {}
    section = subsection = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            match = _CONFIG_SECTION_RE.match(line)
            if not match:
                return None
            head, subsection = match.group(1), match.group(2)
            if subsection is None and "." in head:
                head, subsection = head.split(".", 1)
            section = head.lower()
            continue
        if section is None or "=" not in line:
            return None
        name, _, value = line.partition("=")
        entries[(section, subsection, name.strip().lower())] = value.strip()
    return entries


def _config_change_owner(before, after, other_branches, our_branches):
    """Who moved the shared config: ``ours``, ``them``, or ``unknown``.

    ``config`` lives in the *common* git dir, so the claim that no sibling can
    touch it was simply false: ``git worktree add -b x <path> origin/main``
    sets up tracking, and ``[branch "x"] remote/merge`` lands in the config
    every worker is fingerprinting. A second agent opening a workspace was
    therefore reported as a repo-corrupting test — six of them at once under
    ``-n auto``, whichever happened to be in teardown (#505).

    Rather than stop watching the file, attribute it by key, which is the same
    move #428 made for refs: tracking config for a branch checked out elsewhere
    is that worktree's, exactly as its ref is. Everything else — ``core.bare``,
    ``user.*``, ``remote.*``, tracking config for the branch *we* hold — is
    still ours, beside a sibling or not, so #319 keeps its teeth.
    """
    old, new = _parse_git_config(before), _parse_git_config(after)
    if old is None or new is None:
        return "ours"
    changed = {key for key in set(old) | set(new) if old.get(key) != new.get(key)}
    if not changed:
        return "ours"
    owner = "them"
    for section, subsection, _key in changed:
        if section != "branch" or subsection is None or subsection in our_branches:
            return "ours"
        if subsection not in other_branches:
            owner = "unknown"
    return owner


def _classify_git_state_change(before, after, other_branches, has_siblings):
    """Attribute a git state change to this test, a sibling worktree, or nobody.

    Returns ``(verdict, changed_keys)`` with verdict in ``clean`` / ``mutated``
    / ``inconclusive``.

    This worktree's ``HEAD`` and the branch ``HEAD`` points at are what a test
    running git against the ambient repo moves (#416) — and what no sibling
    worktree can touch, since a checked-out branch is exclusive. Either moving
    is this test's doing, siblings present or not, so the #319 tripwire keeps
    its teeth. Refs checked out in other worktrees are theirs by definition, and
    ``config`` is split per key by ``_config_change_owner`` because it is shared
    (#505). Anything left — a stray branch, a rewritten ``packed-refs`` — is a
    violation when this is the only checkout (the CI case, where the guard is
    unchanged) and honestly unattributable when it is not.
    """
    changed = {key for key in ("config", "HEAD", "packed-refs") if before[key] != after[key]}
    changed |= {
        "refs/heads/" + name
        for name in set(before["refs"]) | set(after["refs"])
        if before["refs"].get(name) != after["refs"].get(name)
    }
    changed = sorted(changed)
    if not changed:
        return "clean", changed
    our_branches = {
        branch for branch in (_head_branch(snapshot["HEAD"]) for snapshot in (before, after))
        if branch
    }
    ours = {"HEAD"} | {"refs/heads/" + name for name in our_branches}
    theirs = {"refs/heads/" + name for name in other_branches}
    if "config" in changed:
        owner = _config_change_owner(
            before["config"], after["config"], other_branches, our_branches
        )
        if owner == "ours":
            ours.add("config")
        elif owner == "them":
            theirs.add("config")
    if ours.intersection(changed):
        return "mutated", changed
    if set(changed) <= theirs:
        return "clean", changed
    if has_siblings:
        return "inconclusive", changed
    return "mutated", changed


GIT_STATE_MUTATED = (
    "test mutated the suite repo's git state ({changed}) — build git fixtures "
    "in tmp_path and pass cwd=tmp_path to every git call. "
    "See conftest._guard_repo_git_state / issue #319."
)

GIT_STATE_INCONCLUSIVE = (
    "suite repo git state changed ({changed}) while a sibling worktree was "
    "active — cannot tell whether this test did it, so not failing it. "
    "Re-run with no other worktree busy to get a verdict. See issue #428."
)


@pytest.fixture(autouse=True)
def _guard_repo_git_state():
    """Fail any test that mutates the suite repo's own git state (#319).

    Tests must build git fixtures inside ``tmp_path`` and run every git call
    with ``cwd=tmp_path`` (or ``git -C tmp_path``). A test that runs ``git
    init``/``commit``/``config`` against the ambient cwd corrupts the real repo
    — and when the suite runs inside a *worktree* (shared ``.git``), it
    corrupts the main checkout too (``core.bare=true``, junk commits on master).
    A standalone clone hides this; this guard surfaces the culprit by name.

    Sibling worktrees share that common dir, so their commits land in the same
    snapshot. Rather than blame whichever test was in teardown, the change is
    attributed first, and only an unexplained one is reported (#428).

    Under ``-n auto`` every worker fingerprints the same repo, so one violating
    test makes its concurrent neighbours in other workers see the change too and
    report it as theirs. That is left as-is deliberately (#433): the fan-out only
    happens when a real violation exists, the run is red either way, and every
    message names this guard — whereas the fixes for it (skip on any worker but
    ``gw0``, or fingerprint once per session) both hand back the per-test
    attribution #428/#432 was written to buy. Seeing N copies of this failure
    means one test is the culprit; ``-n0`` names which.
    """
    dirs = _repo_git_dirs()
    before = _git_state_snapshot(dirs) if dirs else None
    yield
    if before is None:
        return
    after = _git_state_snapshot(dirs)
    if after == before:
        return
    verdict, changed = _classify_git_state_change(
        before, after, *_other_worktree_branches()
    )
    if verdict == "clean":
        return
    if verdict == "inconclusive":
        import warnings
        warnings.warn(GIT_STATE_INCONCLUSIVE.format(changed=", ".join(changed)),
                      stacklevel=2)
        return
    raise AssertionError(GIT_STATE_MUTATED.format(changed=", ".join(changed)))


# Module-level state that must not survive a test (#397). Every entry is
# per-invocation scratch or a cache; the fixture below restores each to its
# import-time value between tests — in place for a live container, because
# supertool holds direct references to those objects, and by rebinding for
# everything else (#1107), which is the only way to restore a `str`, a `bool`
# or a `None` sentinel. test_state_reset_and_lint_timeout.py fails when a new
# mutable *container* appears in neither tuple, and
# test_core_global_lifetimes_1107.py fails when a new name the code *rebinds at
# run time* appears in none of the three tables — the forgetting is otherwise
# silent, and shows up as a test that passes alone and fails in suite order.
RESET_GLOBALS = (
    "_BRANCH_CACHE",
    "_CONFIG_WARNINGS",
    "_ENV_ANNOUNCED",
    "_FORMATTER_SKIPS",
    "_FORMAT_QUEUE",
    "_GIT_IGNORED_CACHE",
    "_LEAKED_GIT_ENV",
    # The shipped reference `help:OP` falls back to when the project config has
    # never heard of a name (#1773). A cache of one file beside the binary, so
    # it cannot go stale within a process — but a test that patches
    # `_shipped_config` or points the lookup elsewhere would otherwise hand the
    # next test whichever answer it installed.
    "_SHIPPED_CONFIG",
    # The three-state verdict beside it (#1781) and the directory the lookup
    # reads. Both are rebound at run time, and a verdict cached from one
    # install describing the next is the finding this pair exists to prevent.
    "_SHIPPED_CONFIG_STATE",
    "_SHIPPED_CONFIG_DIR",
    # Per-process `git status` snapshot behind the read marker (#1126). It is
    # scratch in exactly the sense this list means: correct for the call that
    # built it, and a stale answer for the next test, which would see another
    # test's tmp_path repo described as its own.
    "_PATH_META_BULK",
    # Same call, same reason: a repo root resolved from a directory that a
    # later test recreates at the same tmp_path with different contents.
    "_PATH_META_ROOT_CACHE",
    "_MUTATION_ATTEMPTS",
    # Both read from `_INSTALL_DIR` at FIRST USE, not at import (#1322). They
    # sat in RESET_EXEMPT_GLOBALS below on the reasoning that they describe the
    # install rather than the run — true of the *inputs*, false of the values,
    # because a test that patches `_INSTALL_DIR` (tests/test_presets.py:38,57)
    # and then reaches a dispatch builds them EMPTY, and an exempt empty cache
    # is preserved for the rest of that xdist worker. `_repo_target_ops()` then
    # returns nothing and every `repo:` call refuses every op it is handed.
    # "Same lifetime as X" is a claim about when a value is built; both of these
    # are built at first use. Resetting rebinds them to `None`, so the next
    # caller rebuilds from whatever `_INSTALL_DIR` really is.
    "_REPO_TARGET_MODES",
    "_SHIPPED_PRESET_OPS",
    # Filled in the same pass as `_SHIPPED_PRESET_OPS` and cleared at the top of
    # that rebuild, so it is already as fresh as its sibling (#1524). Listed
    # anyway rather than exempted: the exemption is the claim #1322 disproved,
    # and a dict whose freshness depends on another global being reset first is
    # a two-step argument where a one-line entry does the job.
    "_SHIPPED_PRESET_SYNTAX",
    # Both per-invocation scratch, written by dispatch before the op runs and
    # read within the same frame (#946). Stale across tests they would be
    # worse than absent: `_ARG_SEP` would tell a preset the previous call's
    # route, and `_CUSTOM_OP_OK` would report the previous op's exit status
    # for one that never ran.
    "_ARG_SEP",
    "_CUSTOM_OP_OK",
    # Per-invocation scratch in the same sense, and rebound rather than mutated,
    # which is why the #397 container sweep never saw either (#1107). `main()`
    # sets them at entry, so production is safe; a test that drives `dispatch`
    # or a `cwd:`-shifted op directly never reaches that, and the next test then
    # reads the previous one's invocation directory as its own.
    "_INVOCATION_DIR",
    "_CWD_SHIFT",
    # The flag half of `_FORMAT_QUEUE`, which is two entries above (#1107).
    # Queue reset, gate not: a test that raised mid-batch left deferral ON, and
    # every later formatter in that worker was queued for a flush nobody runs.
    "_DEFER_FORMATTERS",
    # A `str` memo built at first use from `_INSTALL_DIR` — #1322's shape, in a
    # type the #397 sweep cannot see, and the case #1107 was filed from. It
    # keys the validator cache: computed while a test pointed `_INSTALL_DIR` at
    # a tmp_path, it is the hash of `schema-unreadable`, and every later entry
    # in that worker is written and read under a key space no install has.
    "_VALIDATOR_MEANING_VERSION",
    # Memo of `builtin-ops.grep.extensions`, derived from `_load_config()` —
    # and the fixture below hands every test `_CONFIG = {}`. Same mechanism as
    # `_mcp_specs` below: one test that forces a real config load fixes this
    # repo's own extension filter onto every later `grep:` in that worker.
    "_GREP_EXTENSIONS_EFFECTIVE",
    # Both halves of the `presets/_untrusted.py` probe, loaded by path from
    # `_INSTALL_DIR` (#1107). A test that patches the install dir and reaches
    # any redaction sets `_TRIED = True` with `_FLAT = None` — permanently, and
    # silently, since the fallback still returns a string. Every later secret
    # in that worker is rendered by `repr()` instead of the real flattener,
    # which is a redaction guarantee downgraded by an unrelated test.
    "_UNTRUSTED_FLAT",
    "_UNTRUSTED_FLAT_TRIED",
    # The function memoised out of `presets/mcp/_paths.py`, resolved from
    # `_MCP_DAEMON_SCRIPT` (#1107). Same load-by-path shape as the two above.
    "_MCP_SOCKET_PID_PATHS_FN",
    # `_AT_FILE_REGISTRY`'s guard flag. The registry itself stays exempt below
    # because `_build_at_file_registry` rebinds it wholesale rather than
    # mutating it — but that rebuild only happens when this flag is False, so
    # exempting the flag too made a registry built under one test's config
    # permanent for the worker. Resetting the flag is what makes the
    # exemption's own argument true (#1107).
    "_AT_FILE_REGISTRY_BUILT",
    "_REPO_ROOT_WALK_CACHE",
    "_TS_GRAMMAR_FAILED",
    "_VALIDATOR_DEFER_QUEUE",
    "_VALIDATOR_DEFER_SEEN",
    "_VALIDATOR_FINGERPRINT_CACHE",
    "_NOT_CHECKED",
    # `_NOT_CHECKED`'s twin, and classified from the same reasoning (#990). It
    # accumulates one entry per file a `validate:` block rendered, one whole
    # CALL at a time, so a leak across calls in one process would make one run's
    # footer count another run's files — which is the defect class the footer
    # was added to close. `main` truncates it back to its entry length, but a
    # test that drives `op_validate` or `_validate_one_block` directly never
    # reaches that, which is exactly what this tuple is for.
    #
    # The reasoning in that first sentence was right and its reach was one door
    # short: production had the same leak between two ops of the SAME call, via
    # the `len()`-snapshot the footer used to be sliced out of, and #1109 is
    # that bug with `SUPERTOOL_PARALLEL` set. The footer now reads the dispatch
    # frame; this tuple still guards the per-call list.
    "_VALIDATED_FILES",
    # Same family (#1705): one entry per preset op that answered with a declared
    # exit value it does not declare clear to proceed. `main` truncates it back
    # to its entry length, and a leak across calls in one warm process would let
    # one call's `occupied` set a later call's exit code.
    "_UNCLEAN_VALUE_EXITS",
    "_REAPPLY_COUNT",
    "_ROLLBACK_COUNT",
    "_LEFT_ON_DISK_COUNT",
    "_SKIP_COUNT",
    "_WRITE_COUNT",
    "_WRITE_WARNINGS",
    "_PAYLOAD_WARNINGS",
    # Derived from `_CONFIG`, and therefore scratch for the same reason the
    # fixture below hands every test `_CONFIG = {}` (#1030). `_load_config()`
    # writes the `mcp` block into this dict *in place* and never clears it, so
    # one test that forces a real config load — `test_at_file_route.py::
    # TestPayloadRoutePin` chdirs to the repo root and rebuilds the registry,
    # legitimately — left this repo's own `py-lsp` spec (`match: "*.py"`) here
    # for the rest of that xdist worker. Every later `.py` op_workspace /
    # resolve / refs call in that worker then routed to an LSP nobody
    # configured, which is why ~10 of `test_op_workspace.py`'s 16 tests went
    # red on one full-suite run, green on the next, and green in isolation
    # either way: `--dist load` decides whether the two ever share a worker.
    "_mcp_specs",
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
    "_GC_DEFAULT_RETENTION_DAYS",
    # Which config keys `_resolve_custom_op`'s launcher never exports as an
    # env var, and the set `_op_config_key_collisions` reads the same list
    # from so the two can never disagree (#1009). Constant, written once at
    # import, only ever read -- same lifetime as _BUILTIN_OPS.
    "_OP_CONFIG_RESERVED_KEYS",
    "_MCP_SERVERS",
    "_MCP_STOP_CODES",
    # Per-command-word option grammar for the raw-command guard (#1421).
    # Read on every `guard_command` call, written by nothing.
    "_GUARD_GLOBAL_OPTIONS",
    "_CONFIG",
    "_AT_FILE_REGISTRY",
    # Same guard, same reason: built once alongside _AT_FILE_REGISTRY inside
    # _build_at_file_registry, which rebinds it (`[:] = dropped`) rather than
    # mutating in place, gated by the same _AT_FILE_REGISTRY_BUILT flag.
    "_AT_FILE_DROPPED_ROUTES",
    "_AROUND_DIR_SKIP",
    # The entry-point shim and the sibling holding the code it stands for
    # (#1259). A fact about how this tool is laid out on disk, fixed at
    # import and only ever read — same lifetime as _BUILTIN_OPS.
    "_SHIM_CORE",
    "_AT_FILE_BUILTIN_DEFAULTS",
    "_READ_OP_AT_FIELDS",
    "_BATCH_POSITIONAL_FIELDS",
    "_BUILTIN_OPS",
    "_BUILTIN_SYNTAX_VALIDATORS",
    # Constant op-name tables, same lifetime as _BUILTIN_OPS (#614).
    "_DISPATCH_ONLY_OPS",
    "_MAIN_LEVEL_OPS",
    # Which argument of each built-in is a path (#146). A local literal inside
    # `dispatch` until #1285, rebuilt on every call and unreadable from outside
    # it — which is how it kept a row for `blame` months after that op left the
    # dispatcher. Same lifetime as _BUILTIN_OPS now that it is at module scope:
    # written once at import, only ever read.
    "_PATH_ARG_POSITIONS",
    # The last colon slot each built-in reads (#1582). Same shape, same
    # lifetime and same reason as _PATH_ARG_POSITIONS: a fact about this
    # binary's argument grammar, written once at import and only ever read.
    "_MAX_COLON_SLOTS",
    # The two op names whose intended target is documented rather than guessed,
    # read by the unknown-op message (#1303). Constant table, same lifetime.
    "_OP_SYNONYMS",
    # The safety class of each built-in and its render marker (#1231). Same
    # lifetime and same reasoning as _BUILTIN_OPS: a fact about this binary,
    # written once at import and only ever read. Resetting them would imply
    # a per-test lifetime they do not have — and a class that could change
    # under a test is exactly what the roster must not have.
    "_OP_SAFETY_BUILTIN",
    "_SAFETY_MARKERS",
    # `_SHIPPED_PRESET_OPS` and `_REPO_TARGET_MODES` used to sit here, claiming
    # the same lifetime as the constant tables around them. They do not have it:
    # both are `None` at import and built at first use from `_INSTALL_DIR`.
    # They moved to RESET_GLOBALS in #1322, and
    # `tests/test_lazy_cache_lifetimes_1322.py` now fails the build on any new
    # `None`-sentinel name added to this tuple.
    "_EXT_FAMILIES",
    "_FORMATTER_CONFIG_MARKERS",
    "_HEADER_HERITAGE_RE",
    "_NONDETERMINISTIC_ERROR_CODES",
    "_OP_TARGETS",
    "_PARALLEL_SAFE_OPS",
    "_PLAIN_MARKERS",
    "_READ_OP_TARGETS",
    "_REGEX_PATTERNS",
    "_TOML_ESCAPES",
    "_TS_DEF_NODES",
    "_TS_DEF_NODES_DEFAULT",
    "_TS_LANG_ALIASES",
    "_TS_LANG_MAP",
)

# ---------------------------------------------------------------------------
# The same contract, for `presets/` (#686).
#
# The three tables below are keyed by repo-relative path and cover every
# module-level global under `presets/` that is *mutated at run time* — 4 names
# out of the 43 module-level mutables there, because the other 39 are constant
# lookup tables that merely happen to be dicts. `tests/test_preset_global_
# lifetimes_686.py` fails the build when a fifth appears in none of them, and
# fails it again when one of them names something that is no longer state.
#
# Mutability alone was rejected as the trigger deliberately: a guard demanding a
# registry entry for `_CHECK_GLYPH` would be 91% noise, and a noisy guard gets
# exempted without reading, which is how a guard stops working.
# ---------------------------------------------------------------------------

#: Cleared by `_reset_module_state` below, exactly as `RESET_GLOBALS` is. Open
#: only to preset modules this file imports — today just `_env`, which 29
#: presets share, and whose shared ledger cost PR #689 a macOS-only red.
#: Extending it to all 125 preset modules means importing and deep-copying all
#: of them per test; #686 declined that cost, and this stays the exception.
PRESET_RESET_GLOBALS: dict[str, tuple[str, ...]] = {
    "presets/_env.py": ("_ANNOUNCED",),
}

#: Held by the module itself: reset in `main()`'s prologue, before the first
#: branch. Right for a preset, which is a subprocess that runs `main()` once —
#: and it keeps working under a harness that imports the module once and calls
#: `main()` repeatedly, which is the case conftest's list exists for.
#:
#: The claim is verified, not trusted: the guard re-reads each module and fails
#: if the reset is not really there. `presets/mcp/stop.py` writes
#: `_RUNTIME_HINT` inside `main()` too, but 150 lines in and as the value being
#: used — "mutated somewhere in main" would have accepted that.
PRESET_SELF_CLEARING_GLOBALS: dict[str, tuple[str, ...]] = {
    "presets/git/push.py": ("_RUN", "_BUDGET"),
    "presets/git/status.py": ("_UNANSWERED",),
    "presets/mcp/stop.py": ("_RUNTIME_HINT",),
}

#: Neither reset nor self-clearing, and deliberately so. Empty, and that is the
#: point: an entry here is a claim that state surviving a run is correct, and it
#: should have to be argued for in review rather than added to quiet a build.
PRESET_RESET_EXEMPT_GLOBALS: dict[str, tuple[str, ...]] = {}

#: Held per test by the autouse fixture below, which saves each one before the
#: test and assigns it again after — the route the RTK, config, tree-sitter and
#: ctags probes have always used, and which had no name until #1107. Declaring
#: them is what lets `test_core_global_lifetimes_1107.py` tell a name the
#: fixture handles from one nobody classified, and that guard verifies the claim
#: against the fixture's own source rather than believing this tuple.
#:
#: This is a third mechanism, not a third opinion: these are probe results the
#: suite deliberately *forces* (RTK off, no tree-sitter, no ctags) rather than
#: restores to their import-time value, which is why they cannot simply move
#: into RESET_GLOBALS.
#: `_CONFIG` is restored by the same fixture and is deliberately NOT here: it
#: is already classified, in RESET_EXEMPT_GLOBALS above, and one name carrying
#: two declarations is how the two disagree later.
FIXTURE_RESTORED_GLOBALS = (
    "_CONFIG_CHECKED",
    "_CONFIG_PATH",
    "_CTAGS_CHECKED",
    "_CTAGS_PATH",
    "_IN_ALIAS",
    "_RTK_CHECKED",
    "_RTK_PATH",
    "_TS_AVAILABLE",
    "_TS_CHECKED",
    "_TS_PACKAGE",
)

_PRISTINE_GLOBALS = {
    name: copy.deepcopy(getattr(supertool, name)) for name in RESET_GLOBALS
}


def _reset_module_state():
    for name in RESET_GLOBALS:
        current = getattr(supertool, name)
        pristine = copy.deepcopy(_PRISTINE_GLOBALS[name])
        if isinstance(pristine, dict) and isinstance(current, dict):
            current.clear()
            current.update(pristine)
        elif isinstance(pristine, set) and isinstance(current, set):
            current.clear()
            current.update(pristine)
        elif isinstance(pristine, list) and isinstance(current, list):
            current[:] = pristine
        else:
            # Everything that is not a live container of the same kind is
            # rebound. That covers the `None` sentinel #1322 added this arm for
            # — `current` is a dict by the time anything has called the builder,
            # and `current.clear()` would leave a *built and empty* map,
            # indistinguishable from "this install declares no repo-targetable
            # ops" — and it covers the `str`, `bool` and tuple memos #1107
            # added, which reach neither `.clear()` nor `[:]` at all: the old
            # fallback raised `TypeError` on a bool and would have made adding
            # one to this tuple fail the whole suite. supertool reads all of
            # these through the module global on every call, never through a
            # captured reference.
            setattr(supertool, name, pristine)
    if _env is not None:
        for name in PRESET_RESET_GLOBALS["presets/_env.py"]:
            getattr(_env, name).clear()


@pytest.fixture(autouse=True)
def _disable_rtk_and_config():
    """Disable RTK delegation, config cache, tree-sitter, and ctags in tests."""
    import os
    _reset_module_state()
    old_rtk_checked = supertool._RTK_CHECKED
    old_rtk_path = supertool._RTK_PATH
    old_config_checked = supertool._CONFIG_CHECKED
    old_config = supertool._CONFIG
    old_config_path = supertool._CONFIG_PATH
    old_ts_checked = supertool._TS_CHECKED
    old_ts_available = supertool._TS_AVAILABLE
    old_ts_package = supertool._TS_PACKAGE
    old_ctags_checked = supertool._CTAGS_CHECKED
    old_ctags_path = supertool._CTAGS_PATH
    supertool._RTK_CHECKED = True
    supertool._RTK_PATH = None
    supertool._CONFIG_CHECKED = True
    supertool._CONFIG = {}
    supertool._CONFIG_PATH = None
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
    supertool._CONFIG_PATH = old_config_path
    supertool._TS_CHECKED = old_ts_checked
    supertool._TS_AVAILABLE = old_ts_available
    supertool._TS_PACKAGE = old_ts_package
    supertool._CTAGS_CHECKED = old_ctags_checked
    supertool._CTAGS_PATH = old_ctags_path
    # Restore NO_PERSIST so tests that use os.environ.pop() don't leave the
    # flag absent for subsequent tests (pytest does not restore env vars set
    # via os.environ directly — only monkeypatch does).
    os.environ["SUPERTOOL_VIM_NO_PERSIST"] = "1"


# ---------------------------------------------------------------------------
# Preset-derived ops: the opt-in for the autouse config reset (#1812)
# ---------------------------------------------------------------------------

#: `_disable_rtk_and_config` above hands every test `supertool._CONFIG = {}`.
#: That reset is deliberate and load-bearing -- `_load_config()` walks up from
#: the **cwd**, so without it a test's behaviour would depend on which checkout
#: it was run from and on whatever `.supertool.json` happened to sit above it.
#: Its cost is that **every op whose route comes from a preset manifest rather
#: than the builtin table does not exist in any test**: `git-commit`,
#: `git-push`, the whole `gh-*` family. Not intermittently -- the condition is
#: never false, in CI included.
#:
#: The failure mode is the one this repository keeps filing. On #1776 a test
#: asserting that the `@-` guard refuses `git-commit` -- the most destructive
#: op the guard covers -- armed a `pytest.skip` on the absent route and
#: reported `git-commit has no payload route in this environment` on every
#: invocation. A well-written message that reads as a local quirk, for an
#: assertion that had never run anywhere.
#:
#: So this opt-in is loud by construction: `with_preset_op` **fails** where
#: those hand-rolled arms skipped. A preset manifest that cannot answer is a
#: broken checkout, not an under-equipped machine -- `presets/*.json` are
#: tracked files one directory from this one, not a `hadolint` on PATH -- and a
#: fixture that skipped would reproduce, one layer up, the exact defect it
#: exists to remove. `tests/test_with_preset_op_1812.py` pins that decision as
#: a behaviour rather than leaving it to this comment.

REPO_ROOT = Path(__file__).parent.parent

#: Three states, because "this op declares no payload route" and "this op
#: declared one and the derivation lost it" are different facts with different
#: suspects, and collapsing them is how a reworded `syntax` deletes a route
#: silently (#770).
PRESET_OP_ROUTE_OK = "ok"
PRESET_OP_ROUTE_NONE = "no-payload-route"
PRESET_OP_ROUTE_LOST = "route-declared-but-lost"


def _load_shipped_config(root: Path) -> dict:
    """*root*'s `.supertool.json`, presets resolved by the product's own loader.

    `supertool._merge_presets` rather than a walk over `presets/*.json`: the
    resolution order (project over user over shipped) and the key-by-key merge
    of a partial project override are the product's, and a hand-rolled walk
    gets both wrong.

    Takes *root* rather than closing over `REPO_ROOT` so that the refusal below
    is reachable from a test. A guard whose failing branch can only be produced
    by breaking the developer's checkout is a guard nobody ever sees run.
    """
    cfg = json.loads((root / ".supertool.json").read_text(encoding="utf-8"))
    declared_presets = [p for p in (cfg.get("presets") or [])
                        if isinstance(p, str)]
    supertool._merge_presets(cfg, str(root))
    ops = cfg.get("ops") or {}

    # Three separate ways the manifests fail to answer, and none of them is a
    # skip. They are listed loudest-first because the first is the only one
    # that names *which* manifest went missing.
    warnings = cfg.get("_preset_warnings") or []
    assert not warnings, (
        f"`_merge_presets` could not resolve every declared preset from "
        f"{root}: {'; '.join(str(w) for w in warnings)}. `presets/*.json` are "
        f"tracked files one directory from `conftest.py`, so this is a broken "
        f"checkout and not an under-provisioned machine -- it fails rather "
        f"than skipping, because a skip would report an environment quirk for "
        f"a suite that has stopped exercising the shipped registry (#1829)."
    )
    assert ops, (
        "`.supertool.json` + `_merge_presets` resolved zero ops. Nothing can be "
        "installed from that, and a fixture that returned it would hand every "
        "caller a config-shaped absence instead of a route (#1812, #1829)."
    )
    # The one the obvious `assert ops` cannot see. This repo's own
    # `.supertool.json` declares eight ops directly, so `ops` stays non-empty
    # with every preset manifest deleted -- a guard reporting a healthy
    # registry for a checkout that resolved none of it, which is this
    # repository's own defect class sitting inside the guard against it.
    #
    # Asked per declared preset, via the provenance the loader stamps itself,
    # rather than by counting op names the merge added. Counting names cannot
    # see a preset whose whole contribution collides by key with an op the
    # project also declares -- and that is not hypothetical here: this repo's
    # own eight direct `ops` entries are exactly the keys `git`, `watch` and
    # `dashboard` ship, so all three are invisible to a name-set difference.
    # `_op_sources` records the originating preset even for an op the project
    # overrode, so it answers the question that was actually being asked.
    sources = cfg.get("_op_sources") or {}
    contributed = {s.get("preset") for s in sources.values()
                   if isinstance(s, dict) and s.get("preset")}
    silent = [p for p in declared_presets if p not in contributed]
    assert not silent, (
        f"{root}'s config declares presets {silent} that resolved without a "
        f"warning and contributed no op to the registry, so those routes are "
        f"absent while `ops` is still truthy. That is the absence the tool "
        f"produced being read as an absence in the world (#1829)."
    )
    return cfg


@functools.lru_cache(maxsize=1)
def _shipped_config_cached() -> dict:
    """This repo's own config, loaded once per process."""
    return _load_shipped_config(REPO_ROOT)


def _shipped_ops_cached() -> dict:
    """Every op this repo's own config resolves to, through the real loader."""
    return _shipped_config_cached()["ops"]


def _shipped_ops() -> dict:
    """A private copy per call -- callers install these into a module global."""
    return copy.deepcopy(_shipped_ops_cached())


@pytest.fixture
def shipped_config(monkeypatch: pytest.MonkeyPatch) -> dict:
    """This repo's own `.supertool.json`, presets merged, installed as `_CONFIG`.

        def test_something(shipped_config):
            assert "gh-pr" in supertool._load_config()["ops"]

    The **whole** registry -- which is exactly what `with_preset_op` above
    withholds, and the two must not be collapsed. A test asking for one route
    must not silently acquire the other forty; a test about the shipped
    listing, the guard's word list or the op roster needs all of them. Those
    are different questions, so they are different fixtures. They share the
    loader and nothing else: `with_preset_op` still installs only the entries
    it was asked for, and `tests/test_shipped_config_1829.py` pins that the
    sharing has not turned into leaking.

    Eight files hand-rolled this and each rediscovered `_merge_presets` (#1829).
    Returns a private deep copy, so a consumer that mutates the config it was
    handed cannot poison the process-wide cache for the next test.
    """
    cfg = copy.deepcopy(_shipped_config_cached())
    monkeypatch.setattr(supertool, "_CONFIG", cfg)
    monkeypatch.setattr(supertool, "_CONFIG_CHECKED", True)
    monkeypatch.setattr(supertool, "_CONFIG_PATH",
                        str(REPO_ROOT / ".supertool.json"))
    return cfg


def preset_op_route_state(name: str, entry: dict) -> "tuple[str, str]":
    """Classify *entry*'s payload route, using the product's own registry build.

    Not a re-implementation of `_build_at_file_registry`'s rules: this installs
    the single entry, asks that function, and reads the answer back -- so a
    change to how routes are derived reaches this classifier instead of
    drifting away from it. The globals it moves are restored before returning;
    `tests/test_with_preset_op_1812.py` pins that.
    """
    saved = {n: getattr(supertool, n) for n in (
        "_CONFIG", "_CONFIG_CHECKED", "_AT_FILE_REGISTRY",
        "_AT_FILE_REGISTRY_BUILT")}
    saved_dropped = list(supertool._AT_FILE_DROPPED_ROUTES)
    try:
        supertool._CONFIG = {"ops": {name: entry}}
        supertool._CONFIG_CHECKED = True
        supertool._AT_FILE_REGISTRY_BUILT = False
        supertool._build_at_file_registry()
        syntax = (entry or {}).get("syntax", "")
        if supertool._at_file_fields(name):
            return PRESET_OP_ROUTE_OK, syntax
        if name in dict(supertool._at_file_dropped_routes()):
            return PRESET_OP_ROUTE_LOST, syntax
        return PRESET_OP_ROUTE_NONE, syntax
    finally:
        for n, value in saved.items():
            setattr(supertool, n, value)
        supertool._AT_FILE_DROPPED_ROUTES[:] = saved_dropped


@pytest.fixture
def with_preset_op():
    """Install one or more preset-derived ops for the duration of one test.

        def test_something(with_preset_op):
            with_preset_op("git-commit", payload_route=True)

    Returns `{name: entry}`, read off the shipped manifests rather than typed,
    so a test stays joined to the artifact it is about.

    Three ways this refuses, and none of them is a skip:

    * **no shipped config declares the name** -- the request can never be
      honoured, in any environment;
    * **the entry's `syntax` declares a payload route (`:::`) and derives no
      fields** -- the route was deleted by a rewording, which is a defect in
      the manifest and must not reach a test as a working install;
    * **`payload_route=True` and the op has no route at all** -- the caller
      said it needed one.

    `payload_route` defaults to False because dispatchability and a payload
    route are different questions: most preset ops have the first and not the
    second, and demanding the second by default would refuse them all.
    """
    saved = {n: getattr(supertool, n) for n in (
        "_CONFIG", "_CONFIG_CHECKED", "_AT_FILE_REGISTRY",
        "_AT_FILE_REGISTRY_BUILT")}
    saved_dropped = list(supertool._AT_FILE_DROPPED_ROUTES)
    installed: dict = {}

    def _install(*names: str, payload_route: bool = False) -> dict:
        assert names, (
            "with_preset_op() was called with no op name. That is a mistake in "
            "the test, not a no-op: it would install nothing and leave every "
            "assertion downstream measuring the autouse reset (#1812)."
        )
        ops = _shipped_ops()
        entries = {}
        for name in names:
            entry = ops.get(name)
            assert isinstance(entry, dict), (
                f"no shipped preset or project config declares an op named "
                f"{name!r}, so its route cannot be installed. This is a failure "
                f"and not a skip on purpose (#1812): the request cannot be "
                f"honoured in any environment, so skipping would report an "
                f"environment quirk for an assertion that never runs."
            )
            state, syntax = preset_op_route_state(name, entry)
            assert state != PRESET_OP_ROUTE_LOST, (
                f"{name!r} declares a payload route and derives no field names "
                f"from it -- {PRESET_OP_ROUTE_LOST}. The manifest's syntax is "
                f"{syntax!r}; rewording one until `_fields_from_syntax` stops "
                f"yielding clean identifiers is the documented way to delete an "
                f"op's payload route without an error (#770)."
            )
            if payload_route:
                assert state == PRESET_OP_ROUTE_OK, (
                    f"{name!r} was requested with payload_route=True and has "
                    f"{state}. Its syntax is {syntax!r}."
                )
            entries[name] = entry

        installed.update(entries)
        supertool._CONFIG = {"ops": copy.deepcopy(installed)}
        supertool._CONFIG_CHECKED = True
        supertool._AT_FILE_REGISTRY_BUILT = False
        supertool._build_at_file_registry()

        # Verify the install rather than assuming it. A fixture that returned
        # a non-working route quietly would be the same defect wearing the
        # costume of a fix -- and for a name that collides with a builtin
        # default, `_at_file_fields` would answer from the builtin table and
        # look like a success.
        for name, entry in entries.items():
            assert name in (supertool._load_config().get("ops") or {}), (
                f"{name!r} was installed and is still not visible to "
                f"`_load_config()`")
            expected = [spec[0] for spec in
                        supertool._fields_from_syntax(entry.get("syntax", ""))]
            assert supertool._at_file_fields(name) == expected, (
                f"{name!r}'s installed route is not the manifest's: expected "
                f"{expected} from the shipped syntax, got "
                f"{supertool._at_file_fields(name)}")
        return entries

    yield _install

    for name, value in saved.items():
        setattr(supertool, name, value)
    supertool._AT_FILE_DROPPED_ROUTES[:] = saved_dropped


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


def _require_non_empty(raw: str) -> str:
    """Default `read_when_ready` parser: any non-empty text is complete."""
    if not raw:
        raise ValueError("file is still empty")
    return raw


def read_when_ready(path, parse=None, *, timeout: float = 2.0, interval: float = 0.01):
    """Wait until ``path`` holds *complete, parseable* content, and return it.

    **Waiting for existence is not waiting for content.** `open(p, "w")` — and
    everything built on it, including `Path.write_text` — creates the file
    empty and fills it a moment later. Two syscalls, and a reader is entitled
    to land between them: a poll that stops at the first `p.exists()` and reads
    immediately can legally observe `""`. The window is normally sub-
    millisecond, so this bug class ships green and only fails where the writer
    gets descheduled between create and write — a loaded, low-core CI runner
    with the suite running `-n auto`. `json.loads("")` raising `Expecting
    value: line 1 column 1 (char 0)` is what that looks like from the reader's
    side, and is how #443 was found.

    So poll for content the reader can actually use, never for existence.
    ``parse`` is the definition of "usable": it must raise ``ValueError`` for a
    read that is not yet complete — ``json.loads`` already does — and return
    the parsed value otherwise. The default accepts any non-empty text.

    This helper is also the reason the fixtures it reads are left writing
    non-atomically: a reader that survives a half-written file is what real
    notifier consumers need, since supertool spawns notifiers and never
    controls how they write.

    On timeout the two failures are reported apart, because they are different
    bugs with different suspects: the file never appeared (the writer never
    ran) versus it appeared and never became parseable (the writer ran, and its
    write never landed).
    """
    path = Path(path)
    parse = _require_non_empty if parse is None else parse
    deadline = time.monotonic() + timeout
    appeared = False
    last_raw = None
    last_error = None
    while True:
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            pass
        else:
            appeared = True
            last_raw = raw
            try:
                return parse(raw)
            except ValueError as exc:
                last_error = exc
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)
    if not appeared:
        raise AssertionError(
            f"{path} never appeared within {timeout}s — the writer never ran"
        )
    raise AssertionError(
        f"{path} appeared but never held parseable content within {timeout}s "
        f"— the writer ran and its write never landed. "
        f"last read: {last_raw!r}, last parse error: {last_error!r}"
    )


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
