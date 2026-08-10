"""`oss_train`'s explicit target list is worktree NAMES, never paths (#1246).

`discover()` — the `all` path — only ever yields directory names it found by
listing `wt_root()`. The explicit-list path applied no contract at all, so
`os.path.join(wt_root(), num)` was reachable with a `num` the caller chose:
absolute (which discards `wt_root` entirely), `../` (which walks out of it), or
a symlink inside the root pointing anywhere on disk. `train()` then ran
`git fetch`, `git rebase origin/master` and `git-push:force-with-lease` in
whatever repository that landed on.

The asymmetry was the whole bug, and the fix is a contract on the name plus a
containment check on the resolved path. They are two checks on purpose:

* the NAME check is the readable refusal — a target with a separator in it is
  not a worktree name even when it stays inside the root, and saying so names
  the mistake instead of the symptom;
* the CONTAINMENT check is the load-bearing one — it resolves symlinks, which
  no amount of string inspection can do.

The refusal is a refusal: it names the target, exits non-zero, and returns
BEFORE the header, so nothing is fetched, rebased or pushed for ANY target in
the list. A partially-run train is not a refusal.

Note which boundary this is. `_containment_error` in the core measures against
the CWD; `$PWD/seed` is inside the cwd and would pass it while still being
outside `wt_root()`. The boundary that matters for this op is `wt_root()`, and
the op is the only thing that knows it.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from _symlink import require_symlink, requires_symlink

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "oss_train.py"


def _load_oss_train():
    spec = importlib.util.spec_from_file_location("oss_train_1246", SCRIPT)
    assert spec is not None and spec.loader is not None, SCRIPT
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


oss_train = _load_oss_train()


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stdout}{proc.stderr}"
    return proc.stdout


def _commit(clone: Path, name: str, body: str) -> None:
    (clone / name).write_text(body, encoding="utf-8")
    _git("add", name, cwd=clone)
    _git("-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "commit", "-q", "-m", name, cwd=clone)


@pytest.fixture()
def outside_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A rebasable repo OUTSIDE `wt_root`, and an empty `wt_root` beside it.

    `topic` is one commit behind `origin/master` and its tree is clean, so
    every guard the op already has — the missing-worktree check, the detached
    check, BUSY — lets it through. The only thing standing between this repo
    and a rebase is the containment check under test.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    origin = outside / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)],
                   check=True, capture_output=True)
    seed = outside / "seed"
    subprocess.run(["git", "clone", "-q", origin.as_posix(), str(seed)],
                   check=True, capture_output=True)

    _commit(seed, "base.txt", "base")
    _git("push", "-q", "origin", "master", cwd=seed)
    _git("checkout", "-q", "-b", "topic", cwd=seed)
    _commit(seed, "topic.txt", "topic")
    _git("push", "-q", "origin", "topic", cwd=seed)
    _git("checkout", "-q", "master", cwd=seed)
    _commit(seed, "later.txt", "later")
    _git("push", "-q", "origin", "master", cwd=seed)
    _git("checkout", "-q", "topic", cwd=seed)

    wt_root = tmp_path / "st-wt"
    wt_root.mkdir()
    monkeypatch.setenv("SUPERTOOL_WT_ROOT", str(wt_root))
    return {"origin": origin, "seed": seed, "wt_root": wt_root}


@pytest.fixture()
def toctou_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Two real worktrees under the root, one of them reached by a symlink.

    `1` is an ordinary worktree whose train performs a real fetch — that fetch
    is the window. `2` is a symlink to `benign`, which is INSIDE the root, so
    it passes the containment check at validation time and there is a later
    moment at which the answer can be made wrong.
    """
    require_symlink()
    outside = tmp_path / "outside"
    outside.mkdir()
    origin = outside / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)],
                   check=True, capture_output=True)
    seed = outside / "seed"
    subprocess.run(["git", "clone", "-q", origin.as_posix(), str(seed)],
                   check=True, capture_output=True)
    _commit(seed, "base.txt", "base")
    _git("push", "-q", "origin", "master", cwd=seed)
    _git("checkout", "-q", "-b", "topic", cwd=seed)
    _commit(seed, "topic.txt", "topic")
    _git("push", "-q", "origin", "topic", cwd=seed)
    _git("checkout", "-q", "master", cwd=seed)
    _commit(seed, "later.txt", "later")
    _git("push", "-q", "origin", "master", cwd=seed)
    _git("checkout", "-q", "topic", cwd=seed)

    home = tmp_path / "home.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(home)],
                   check=True, capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", home.as_posix(), str(clone)],
                   check=True, capture_output=True)
    _commit(clone, "base.txt", "base")
    _git("push", "-q", "origin", "master", cwd=clone)
    for branch in ("feature/one", "feature/benign"):
        _git("checkout", "-q", "-b", branch, "master", cwd=clone)
        _commit(clone, branch.replace("/", "-") + ".txt", branch)
    _git("checkout", "-q", "master", cwd=clone)
    _commit(clone, "later.txt", "later")
    _git("push", "-q", "origin", "master", cwd=clone)

    wt_root = tmp_path / "st-wt"
    wt_root.mkdir()
    _git("worktree", "add", "-q", str(wt_root / "1"), "feature/one", cwd=clone)
    _git("worktree", "add", "-q", str(wt_root / "benign"), "feature/benign",
         cwd=clone)
    (wt_root / "2").symlink_to(wt_root / "benign", target_is_directory=True)

    monkeypatch.setenv("SUPERTOOL_WT_ROOT", str(wt_root))
    return {"origin": origin, "seed": seed, "wt_root": wt_root, "clone": clone}


def _run(monkeypatch: pytest.MonkeyPatch, arg: str) -> int:
    monkeypatch.setattr(sys, "argv", ["oss_train.py", arg])
    return oss_train.main()


def _assert_untouched(seed: Path, origin: Path, head_before: str,
                      remote_before: str) -> None:
    assert _git("rev-parse", "HEAD", cwd=seed).strip() == head_before, (
        "the local branch was rebased in a repository outside wt_root")
    assert _git("ls-remote", origin.as_posix(), "refs/heads/topic",
                cwd=seed) == remote_before, (
        "the remote ref of a repository outside wt_root was rewritten")


# ---------------------------------------------------------------------------
# the shapes that reach outside wt_root
# ---------------------------------------------------------------------------

def test_an_absolute_target_is_refused_and_rewrites_nothing(
        outside_world, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """`os.path.join(root, "/abs")` is `/abs` — the root is discarded, silently."""
    seed, origin = outside_world["seed"], outside_world["origin"]
    head = _git("rev-parse", "HEAD", cwd=seed).strip()
    remote = _git("ls-remote", origin.as_posix(), "refs/heads/topic", cwd=seed)

    rc = _run(monkeypatch, str(seed))

    out = capsys.readouterr().out
    # The destructive assertion comes FIRST: a refusal that arrived after the
    # rebase would still satisfy every message check below.
    _assert_untouched(seed, origin, head, remote)
    assert rc == 2, out
    assert str(seed) in out, out
    assert "# oss_train" not in out, (
        "the refusal must return above the header — no target in the list runs")


def test_a_dotdot_target_is_refused_and_rewrites_nothing(
        outside_world, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    seed, origin = outside_world["seed"], outside_world["origin"]
    head = _git("rev-parse", "HEAD", cwd=seed).strip()
    remote = _git("ls-remote", origin.as_posix(), "refs/heads/topic", cwd=seed)

    rc = _run(monkeypatch, os.path.join("..", "outside", "seed"))

    out = capsys.readouterr().out
    _assert_untouched(seed, origin, head, remote)
    assert rc == 2, out
    assert "# oss_train" not in out, out


@requires_symlink
def test_a_symlink_inside_the_root_pointing_outside_is_refused(
        outside_world, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The case no string check can see.

    `evil` is a plain name, it holds no separator, it is not absolute, and
    joining it to the root produces a path that starts with the root. Only
    resolving it answers the question.
    """
    seed, origin = outside_world["seed"], outside_world["origin"]
    (outside_world["wt_root"] / "evil").symlink_to(seed, target_is_directory=True)
    head = _git("rev-parse", "HEAD", cwd=seed).strip()
    remote = _git("ls-remote", origin.as_posix(), "refs/heads/topic", cwd=seed)

    rc = _run(monkeypatch, "evil")

    out = capsys.readouterr().out
    _assert_untouched(seed, origin, head, remote)
    assert rc == 2, out
    assert "evil" in out, out
    assert "# oss_train" not in out, out


# ---------------------------------------------------------------------------
# a name that is neither absolute nor `..` and still is not a worktree name
# ---------------------------------------------------------------------------

def test_a_nested_name_under_the_root_is_still_not_a_worktree_name(
        outside_world, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """`nested/999` stays inside the root, so containment alone would pass it.

    It is refused anyway: the contract is a directory name from the root's own
    listing, and a target the `all` path could never produce is a target this
    op has no reading for. Refusing it is what keeps the two paths symmetric.
    """
    (outside_world["wt_root"] / "nested" / "999").mkdir(parents=True)

    rc = _run(monkeypatch, os.path.join("nested", "999"))

    out = capsys.readouterr().out
    assert rc == 2, out
    assert "nested" in out, out
    assert "# oss_train" not in out, out


@pytest.mark.parametrize("target", [
    "999/../../seed",
    "..\\outside\\seed",   # a Windows traversal; not a name on POSIX either
    "sub\\999",
    ".",
    "..",
])
def test_every_non_name_shape_is_refused(
        outside_world, monkeypatch: pytest.MonkeyPatch, target: str,
        capsys: pytest.CaptureFixture[str]) -> None:
    """Both separators are rejected on both platforms.

    A backslash is a path separator on Windows and an ordinary filename
    character on POSIX, so a POSIX-only reading would let the second target
    through on the one platform where it traverses. The check does not branch
    on `os.name`: a test that passes trivially on one platform reports coverage
    it does not have.
    """
    assert _run(monkeypatch, target) == 2
    assert "# oss_train" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# and the acceptance side, which is where a too-strict guard would show
# ---------------------------------------------------------------------------

def test_a_plain_name_in_the_root_is_still_accepted(
        outside_world, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """Both a number and a lane name. `discover()` lists only numeric
    directories, but that is a rule about what `all` SWEEPS, not about what a
    caller may name: `st-wt/scope` and `st-wt/jit` are real worktrees holding
    real branches, and `isdigit()` on the explicit path would delete the only
    way to train them."""
    for name in ("999", "scope"):
        (outside_world["wt_root"] / name).mkdir()

    assert oss_train.target_error("999") is None
    assert oss_train.target_error("scope") is None
    # And the guard is not vacuously permissive: the same call refuses the
    # thing this whole file exists for. Without this line the test passes
    # against `target_error = lambda num: None`, which is the shape of an
    # accept-only test that reports coverage it does not have.
    assert oss_train.target_error(str(outside_world["seed"])) is not None
    # Through main(), so the CALL SITE is exercised too. Both are empty git
    # directories, so both reach FAILED — the point is exit 1, not exit 2:
    # a name that is merely wrong must not render as a containment refusal.
    assert _run(monkeypatch, "999,scope") == 1
    out = capsys.readouterr().out
    assert "# oss_train (2 branch(es))" in out, out
    assert "refusing" not in out, out


def test_a_name_that_does_not_exist_is_still_FAILED_not_a_refusal(
        outside_world, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """A typo'd issue number is a missing worktree, not a containment breach.

    Collapsing the two would make every mistyped digit read as an attempted
    traversal, and the refusal message would stop meaning anything.
    """
    assert _run(monkeypatch, "424242") == 1
    out = capsys.readouterr().out
    assert "no worktree at" in out, out


def test_the_all_path_goes_through_the_same_check(
        outside_world, monkeypatch: pytest.MonkeyPatch) -> None:
    """One validator, applied to both lists. `discover()` already yields only
    names, so this asserts the single-implementation property rather than a
    second behaviour — #882's lesson was a second copy of a rule written beside
    the real one."""
    for name in ("101", "202"):
        (outside_world["wt_root"] / name).mkdir()
    assert [oss_train.target_error(n) for n in oss_train.discover()] == [None, None]


@requires_symlink
def test_all_is_not_trusted_either_when_a_discovered_name_is_a_symlink(
        outside_world, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """`discover()` filters on `isdigit()`, and a symlink named `101` is a
    digit. Its contract is about the NAME, and nothing about it constrains
    where the entry points — so `all` reaches outside the root too, and the
    check has to be on the resolved path rather than on the list's provenance.
    """
    seed, origin = outside_world["seed"], outside_world["origin"]
    (outside_world["wt_root"] / "101").symlink_to(seed, target_is_directory=True)
    head = _git("rev-parse", "HEAD", cwd=seed).strip()
    remote = _git("ls-remote", origin.as_posix(), "refs/heads/topic", cwd=seed)

    rc = _run(monkeypatch, "all")

    out = capsys.readouterr().out
    _assert_untouched(seed, origin, head, remote)
    assert rc == 2, out
    assert "101" in out, out
    assert "# oss_train" not in out, out


def test_the_call_site_refuses_the_whole_list_not_just_the_bad_target(
        outside_world, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """A good name beside a bad one. The good one must not be trained: a train
    that rebased three targets and then declined the fourth is a warning it
    proceeded past, not a refusal."""
    (outside_world["wt_root"] / "999").mkdir()
    seed, origin = outside_world["seed"], outside_world["origin"]
    head = _git("rev-parse", "HEAD", cwd=seed).strip()
    remote = _git("ls-remote", origin.as_posix(), "refs/heads/topic", cwd=seed)

    rc = _run(monkeypatch, "999," + str(seed))

    out = capsys.readouterr().out
    _assert_untouched(seed, origin, head, remote)
    assert rc == 2, out
    assert "# oss_train" not in out, out


# ---------------------------------------------------------------------------
# the path that was CHECKED is the path that is USED (#1246, round-2 audit)
# ---------------------------------------------------------------------------
#
# The first fix for this issue resolved each target twice: once inside
# `target_error()` to decide, and again in `main()`'s train loop to act.
# Unifying the two spellings into one `resolve_target()` function unified the
# IMPLEMENTATION and not the RESOLUTION, and the changelog fragment,
# `resolve_target`'s own docstring and the commit subject then all asserted a
# property the code did not have. The round-2 audit reproduced the window with
# no monkeypatching: `st-wt/2 -> st-wt/benign` at launch, relinked to an
# outside repo while target `1`'s fetch was in flight, and the outside repo
# was rebased.
#
# The window for target k is the whole of trains 1..k-1 — a network fetch, a
# rebase and a push each. So the invariant below is not a style preference; it
# is the fix, and nothing asserted it, which is why the suite was green.


def test_one_resolve_per_target_and_train_gets_the_path_that_was_checked(
        outside_world, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """Exactly one `realpath` per target, and `train()` receives that result.

    Two resolutions cannot be made safe by making them agree at the moment they
    are written: the second one re-reads the filesystem, and re-reading is the
    whole of the defect. Counting them is therefore the assertion rather than
    an approximation of it.
    """
    for name in ("999", "888"):
        (outside_world["wt_root"] / name).mkdir()

    real_resolve = oss_train.resolve_target
    resolved = []

    def counting_resolve(num):
        out = real_resolve(num)
        resolved.append((num, out[1]))
        return out

    handed = []

    def recording_train(num, dry, wt=None):
        handed.append((num, wt))
        return "DRY", "stub"

    monkeypatch.setattr(oss_train, "resolve_target", counting_resolve)
    monkeypatch.setattr(oss_train, "train", recording_train)

    assert _run(monkeypatch, "999,888") == 0
    capsys.readouterr()

    assert len(resolved) == 2, (
        "one realpath per target — " + repr(resolved) + ". More than one means "
        "the check and the use each read the filesystem, and a symlink swapped "
        "between them is followed by the second read")
    assert handed == resolved, (
        "train() was handed a path that is not the one target_error() approved")


@requires_symlink
def test_a_symlink_relinked_mid_run_does_not_redirect_a_later_target(
        toctou_world, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The round-2 reproduction, in-process.

    `run()` is wrapped rather than replaced: every git command still executes
    for real and the guard is not stubbed. The only thing simulated is that
    time passes during target `1`'s fetch and something moves the symlink —
    which is the premise of the finding, not its mechanism.
    """
    seed, origin = toctou_world["seed"], toctou_world["origin"]
    link = toctou_world["wt_root"] / "2"
    head = _git("rev-parse", "HEAD", cwd=seed).strip()
    remote = _git("ls-remote", origin.as_posix(), "refs/heads/topic", cwd=seed)

    real_run = oss_train.run
    swapped = []

    def relinking_run(args, cwd, timeout=900):
        if not swapped and "fetch" in args:
            swapped.append(True)
            link.unlink()
            link.symlink_to(seed, target_is_directory=True)
        return real_run(args, cwd, timeout)

    monkeypatch.setattr(oss_train, "run", relinking_run)

    _run(monkeypatch, "1,2")

    out = capsys.readouterr().out
    assert swapped, "the fixture never relinked — the test proves nothing: " + out
    assert _git("rev-parse", "HEAD", cwd=seed).strip() == head, (
        "a symlink relinked after its containment check redirected the train "
        "into a repository outside wt_root")
    assert _git("ls-remote", origin.as_posix(), "refs/heads/topic",
                cwd=seed) == remote


# ---------------------------------------------------------------------------
# a Windows drive letter is not a token separator (#1247, four Windows legs)
# ---------------------------------------------------------------------------
#
# `parse_tokens` rewrites every ':' to ',' — deliberately, because supertool
# passes only the first ':'-token into {file} and a surviving `all:dry` must
# not be read as an unknown target. On Windows that rewrite cuts an absolute
# path in half at its drive letter: `C:\Users\x\seed` arrives as the two
# targets `C` and `\Users\x\seed`.
#
# The VERDICT survived that — `\Users\x\seed` is still rooted, so the run was
# still refused and nothing was fetched. What did not survive is the ECHO. The
# refusal named `'\Users\x\seed'` while the wt_root half of the same sentence
# carried its drive, so the one line whose entire job is saying WHICH target
# was rejected named a string the caller never typed.

WINDOWS_ABS = "C:\\Users\\x\\seed"


class TestDriveLetters:

    def test_a_drive_letter_is_not_a_separator(self) -> None:
        assert oss_train.parse_tokens([WINDOWS_ABS]) == (WINDOWS_ABS, False)

    def test_the_colon_form_of_the_flag_still_works(self) -> None:
        """The rewrite exists for this, and must survive the narrowing."""
        assert oss_train.parse_tokens(["all:dry"]) == ("all", True)
        assert oss_train.parse_tokens(["999:dry"]) == ("999", True)

    def test_the_refusal_echoes_the_target_the_caller_typed(
            self, outside_world, monkeypatch: pytest.MonkeyPatch,
            capsys: pytest.CaptureFixture[str]) -> None:
        assert _run(monkeypatch, WINDOWS_ABS) == 2

        out = capsys.readouterr().out
        assert WINDOWS_ABS in out, (
            "the refusal must name the target as typed, drive and all — a "
            "refusal that misnames what it refused invites the reader to "
            "conclude a different argument was rejected: " + out)
        assert "refusing 'C'" not in out, out

    @pytest.mark.parametrize("raw, tokens", [
        ("a:dry", ["a:dry"]),
        ("x:y:z", ["x:y", "z"]),
        ("C:", ["C:"]),
    ])
    def test_what_the_narrowing_costs_is_a_refusal_not_a_different_target(
            self, outside_world, monkeypatch: pytest.MonkeyPatch, raw: str,
            tokens: list, capsys: pytest.CaptureFixture[str]) -> None:
        """Sparing the drive colon spares it in more shapes than `C:\\...`.

        The rule is "the first colon of a token beginning with one ASCII
        letter", so `x:y:z` yields `x:y` and `z` rather than three tokens. That
        is a real widening of the old behaviour and it is pinned here rather
        than described, because the docstring understated it once already.

        What makes it safe is where those tokens land: every one carries a
        colon into the check, `ntpath.splitdrive` reads it as drive-relative,
        and the run is refused. The cost is a refusal, never a
        differently-chosen target — the only property that matters for an op
        that force-pushes.
        """
        assert oss_train._colon_split(raw) == tokens
        assert _run(monkeypatch, raw) == 2
        assert "# oss_train" not in capsys.readouterr().out

    def test_a_drive_relative_target_is_refused_on_every_platform(
            self, outside_world) -> None:
        """`C:foo` is drive-relative: no separator, not absolute, and
        `os.path.join(root, "C:foo")` discards the root on Windows.

        Refused on POSIX too, for the same reason both separators are: a check
        that only fires on the platform it was not written on is a check nobody
        has run. `ntpath` answers Windows semantics from here — that is how
        this was established without a Windows box.
        """
        import ntpath
        for target in ("C:foo", "c:bar", "C:"):
            assert ntpath.splitdrive(target)[0], target
            assert oss_train.target_error(target) is not None, target
