"""Shared fixtures and helpers for supertool tests."""
from __future__ import annotations

import copy
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import supertool  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))

import _symlink  # noqa: E402

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
    # #416: the autouse fixture below cannot cover collection-time module
    # bodies or session helpers, which run before any fixture. Scrub once here
    # too, and remember what was leaked so it gets reported rather than hidden.
    config._supertool_leaked_git_env = scrub_git_env()


def pytest_report_header(config):
    """State what this run can and cannot do, before it does any of it.

    Two things, both of which have to be visible on a *green* leg: a leaked git
    environment (#416), and whether this platform/user can create a symlink
    (#1143). The second is here rather than in the summary on purpose -- a
    blind spot announced only when something fails is announced exactly when
    nobody needs telling.
    """
    lines = [_symlink.verdict_line()]
    leaked = getattr(config, "_supertool_leaked_git_env", [])
    if leaked:
        lines.append(
            "scrubbed inherited git env (would have run tests against this repo, "
            f"see #416): {', '.join(leaked)}"
        )
    return lines


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Count the symlink skips apart from the other ~680 (#1143).

    `688 skipped` is a number. `N skipped, because this runner has no
    create-symlink privilege` is a fact somebody can act on -- it is the
    difference between an absence in the world and an absence the tooling
    produced. Printed whether the count is zero or not: silence would be
    indistinguishable from not having looked.
    """
    skipped = terminalreporter.stats.get("skipped", []) or []
    n = 0
    for report in skipped:
        longrepr = getattr(report, "longrepr", None)
        text = longrepr[2] if isinstance(longrepr, tuple) and len(longrepr) > 2 else str(longrepr)
        if _symlink.TOKEN in str(text):
            n += 1
    available, why = _symlink.symlink_support()
    if available:
        terminalreporter.write_line(
            f"{_symlink.TOKEN}: available -- {n} symlink-dependent tests skipped for it")
        return
    terminalreporter.write_line(
        f"{_symlink.TOKEN}: unavailable -- {n} symlink-dependent tests did NOT run "
        f"on this platform. Reason: {why}")


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


# Module-level mutable state that must not survive a test (#397). Every entry
# is per-invocation scratch or a cache; the fixture below restores each to its
# import-time value between tests, in place, because supertool holds direct
# references to these objects. test_state_reset_and_lint_timeout.py fails when
# a new mutable global appears in neither tuple — the forgetting is otherwise
# silent, and shows up as a test that passes alone and fails in suite order.
RESET_GLOBALS = (
    "_BRANCH_CACHE",
    "_CONFIG_WARNINGS",
    "_ENV_ANNOUNCED",
    "_FORMATTER_SKIPS",
    "_FORMAT_QUEUE",
    "_GIT_IGNORED_CACHE",
    "_LEAKED_GIT_ENV",
    # Per-process `git status` snapshot behind the read marker (#1126). It is
    # scratch in exactly the sense this list means: correct for the call that
    # built it, and a stale answer for the next test, which would see another
    # test's tmp_path repo described as its own.
    "_PATH_META_BULK",
    # Same call, same reason: a repo root resolved from a directory that a
    # later test recreates at the same tmp_path with different contents.
    "_PATH_META_ROOT_CACHE",
    "_MUTATION_ATTEMPTS",
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
    "_REAPPLY_COUNT",
    "_ROLLBACK_COUNT",
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
    "_MCP_SERVERS",
    "_MCP_STOP_CODES",
    "_CONFIG",
    "_AT_FILE_REGISTRY",
    # Same guard, same reason: built once alongside _AT_FILE_REGISTRY inside
    # _build_at_file_registry, which rebinds it (`[:] = dropped`) rather than
    # mutating in place, gated by the same _AT_FILE_REGISTRY_BUILT flag.
    "_AT_FILE_DROPPED_ROUTES",
    "_AROUND_DIR_SKIP",
    "_AT_FILE_BUILTIN_DEFAULTS",
    "_READ_OP_AT_FIELDS",
    "_BATCH_POSITIONAL_FIELDS",
    "_BUILTIN_OPS",
    "_BUILTIN_SYNTAX_VALIDATORS",
    # Constant op-name tables, same lifetime as _BUILTIN_OPS (#614).
    "_DISPATCH_ONLY_OPS",
    "_MAIN_LEVEL_OPS",
    # Index of the presets shipped beside supertool.py. A property of the
    # install, not of the run — clearing it per test would only re-read the
    # same ten files and imply a lifetime it does not have.
    "_SHIPPED_PRESET_OPS",
    # Which shipped ops accept a repo target, read from the same manifests
    # (#673). Same lifetime and same reasoning as _SHIPPED_PRESET_OPS above.
    "_REPO_TARGET_MODES",
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
    "presets/git/push.py": ("_RUN",),
    "presets/git/status.py": ("_UNANSWERED",),
    "presets/mcp/stop.py": ("_RUNTIME_HINT",),
}

#: Neither reset nor self-clearing, and deliberately so. Empty, and that is the
#: point: an entry here is a claim that state surviving a run is correct, and it
#: should have to be argued for in review rather than added to quiet a build.
PRESET_RESET_EXEMPT_GLOBALS: dict[str, tuple[str, ...]] = {}

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
