"""Tests for the conftest git-state guard (#319, #428).

The guard snapshots the suite repo's config/HEAD/refs before and after every
test and fails any test that mutates them — the tripwire for a test (or an agent
running the suite in a worktree) corrupting the real repo with `core.bare=true`
or junk commits on master. The refs it watches live in the *common* git dir,
shared with every linked worktree, so a sibling worktree committing mid-run used
to be blamed on whichever test was in teardown (#428): the change is now
attributed before it is reported.

The unit tests exercise the snapshot and the attribution against a synthetic git
layout, never the real repo. The three subprocess tests at the bottom run the
real guard, from a real conftest copy, over a real repo with a real linked
worktree: a sibling commits during the test, the test itself commits, and a
change no worktree owns lands while a sibling is live.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import conftest

SUITE_ROOT = Path(__file__).resolve().parent.parent

_ID = ["-c", "user.email=fixture@example.invalid", "-c", "user.name=fixture"]


def _fake_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Build a minimal (common_dir, git_dir) layout the snapshot reads.

    Written as bytes, not text: git writes ``\n`` into these files on every
    platform, and ``write_text`` would translate to ``\r\n`` on Windows and make
    the fixture describe a repo git never produces.
    """
    common = tmp_path / ".git"
    (common / "refs" / "heads").mkdir(parents=True)
    (common / "config").write_bytes(b"[core]\n\tbare = false\n")
    (common / "HEAD").write_bytes(b"ref: refs/heads/master\n")
    (common / "refs" / "heads" / "master").write_bytes(b"a" * 40 + b"\n")
    return common, common


def _snapshot(head: str = "master", **refs: str) -> dict:
    """A snapshot literal: HEAD on ``head``, plus the named refs."""
    return {
        "config": b"[core]\n\tbare = false\n",
        "HEAD": f"ref: refs/heads/{head}\n".encode(),
        "packed-refs": b"<absent>",
        "refs": {name: sha.encode() for name, sha in refs.items()},
    }


def test_snapshot_stable_when_unchanged(tmp_path: Path) -> None:
    dirs = _fake_repo(tmp_path)
    assert conftest._git_state_snapshot(dirs) == conftest._git_state_snapshot(dirs)


def test_snapshot_detects_core_bare_flip(tmp_path: Path) -> None:
    dirs = _fake_repo(tmp_path)
    before = conftest._git_state_snapshot(dirs)
    (dirs[0] / "config").write_bytes(b"[core]\n\tbare = true\n")
    assert conftest._git_state_snapshot(dirs) != before


def test_snapshot_detects_junk_commit_on_a_branch(tmp_path: Path) -> None:
    dirs = _fake_repo(tmp_path)
    before = conftest._git_state_snapshot(dirs)
    (dirs[0] / "refs" / "heads" / "master").write_bytes(b"b" * 40 + b"\n")
    assert conftest._git_state_snapshot(dirs) != before


def test_snapshot_detects_new_branch_ref(tmp_path: Path) -> None:
    dirs = _fake_repo(tmp_path)
    before = conftest._git_state_snapshot(dirs)
    (dirs[0] / "refs" / "heads" / "junk").write_bytes(b"c" * 40 + b"\n")
    assert conftest._git_state_snapshot(dirs) != before


def test_snapshot_detects_head_move(tmp_path: Path) -> None:
    dirs = _fake_repo(tmp_path)
    before = conftest._git_state_snapshot(dirs)
    (dirs[1] / "HEAD").write_bytes(b"ref: refs/heads/other\n")
    assert conftest._git_state_snapshot(dirs) != before


def test_snapshot_detects_packed_refs_rewrite(tmp_path: Path) -> None:
    dirs = _fake_repo(tmp_path)
    before = conftest._git_state_snapshot(dirs)
    assert before["packed-refs"] == b"<absent>"
    (dirs[0] / "packed-refs").write_bytes(b"d" * 40 + b" refs/heads/packed\n")
    assert conftest._git_state_snapshot(dirs)["packed-refs"] != b"<absent>"


def test_snapshot_names_nested_refs_by_their_branch_name(tmp_path: Path) -> None:
    """A ref two levels down must be keyed ``feat/428``, not its filesystem path."""
    dirs = _fake_repo(tmp_path)
    (dirs[0] / "refs" / "heads" / "feat").mkdir()
    (dirs[0] / "refs" / "heads" / "feat" / "428").write_bytes(b"e" * 40 + b"\n")
    snapshot = conftest._git_state_snapshot(dirs)
    assert sorted(snapshot["refs"]) == ["feat/428", "master"]
    assert snapshot["refs"]["feat/428"] == b"e" * 40 + b"\n"


def test_repo_git_dirs_resolves_real_repo() -> None:
    """Sanity: the suite repo is discoverable and its config is readable."""
    dirs = conftest._repo_git_dirs()
    assert dirs is not None
    common_dir, _ = dirs
    assert (common_dir / "config").is_file()


def test_head_branch_reads_a_symbolic_ref() -> None:
    assert conftest._head_branch(b"ref: refs/heads/feat/428\n") == "feat/428"


def test_head_branch_is_none_when_detached_or_missing() -> None:
    assert conftest._head_branch(b"f" * 40 + b"\n") is None
    assert conftest._head_branch(b"") is None
    assert conftest._head_branch(b"<absent>") is None


def _porcelain(tmp_path: Path, newline: str = "\n") -> tuple[str, Path, Path, Path]:
    """Real, resolvable worktree paths — the only kind ``resolve()`` agrees on.

    A literal ``/repo/main`` is not a path on Windows: ``resolve()`` anchors it
    to the current drive and it stops comparing equal to the same string left
    unresolved, so a fixture built from invented paths tests the fixture rather
    than the parser.
    """
    main, sib, detached = (tmp_path / name for name in ("main", "sibling", "detached"))
    for path in (main, sib, detached):
        path.mkdir(exist_ok=True)
    main, sib, detached = (path.resolve() for path in (main, sib, detached))
    text = newline.join([
        f"worktree {main}", "HEAD " + "a" * 40, "branch refs/heads/master", "",
        f"worktree {sib}", "HEAD " + "b" * 40,
        "branch refs/heads/feat/424-gh-prs-legible-board", "",
        f"worktree {detached}", "HEAD " + "c" * 40, "detached", "",
    ])
    return text, main, sib, detached


def test_parse_worktree_list_excludes_our_own_branch(tmp_path: Path) -> None:
    text, main, _, _ = _porcelain(tmp_path)
    branches, _ = conftest._parse_worktree_list(text, main)
    assert "master" not in branches


def test_parse_worktree_list_collects_sibling_branches(tmp_path: Path) -> None:
    text, main, _, _ = _porcelain(tmp_path)
    branches, has_siblings = conftest._parse_worktree_list(text, main)
    assert branches == frozenset({"feat/424-gh-prs-legible-board"})
    assert has_siblings is True


def test_parse_worktree_list_sees_a_detached_sibling_as_a_sibling(tmp_path: Path) -> None:
    """A sibling on a detached HEAD owns no branch but can still move refs."""
    text, _, sib, _ = _porcelain(tmp_path)
    branches, has_siblings = conftest._parse_worktree_list(text, sib)
    assert branches == frozenset({"master"})
    assert has_siblings is True


def test_parse_worktree_list_reports_no_siblings_for_a_lone_checkout(tmp_path: Path) -> None:
    main = (tmp_path / "main")
    main.mkdir()
    main = main.resolve()
    lone = "worktree {}\nHEAD {}\nbranch refs/heads/master\n".format(main, "a" * 40)
    branches, has_siblings = conftest._parse_worktree_list(lone, main)
    assert branches == frozenset()
    assert has_siblings is False


def test_parse_worktree_list_reads_crlf_porcelain_identically(tmp_path: Path) -> None:
    """Git emits CRLF on Windows. A ``split("\\n")`` here would leave a trailing
    ``\\r`` on every branch name, so our own branch would stop matching and land
    in the sibling set. Pinned from any OS by feeding the CRLF bytes directly,
    rather than waiting for a Windows leg to notice.
    """
    crlf, main, _, _ = _porcelain(tmp_path, newline="\r\n")
    lf, _, _, _ = _porcelain(tmp_path, newline="\n")
    assert "\r\n" in crlf
    assert conftest._parse_worktree_list(crlf, main) == conftest._parse_worktree_list(lf, main)
    assert conftest._parse_worktree_list(crlf, main)[0] == frozenset(
        {"feat/424-gh-prs-legible-board"}
    )


def test_same_path_delegates_case_sensitivity_to_the_platform() -> None:
    """On POSIX ``normcase`` is a no-op, so the two really are different dirs."""
    import os
    assert conftest._same_path(Path("/repo/Main"), Path("/repo/Main")) is True
    expected = os.path.normcase("/repo/Main") == os.path.normcase("/repo/main")
    assert conftest._same_path(Path("/repo/Main"), Path("/repo/main")) is expected


def test_same_path_folds_case_under_windows_rules(monkeypatch) -> None:
    """Forcing ``ntpath`` semantics pins the Windows behaviour from any runner.

    ``C:\\\\Repo`` and ``c:\\\\repo`` are one directory on NTFS. A plain string
    comparison calls them two, which makes *this* checkout read as a sibling of
    itself: ``has_siblings`` goes true on a lone Windows clone and the
    unattributable bucket softens to a warning where it must still fail. That
    is unobservable on a POSIX runner, so the platform's rule is imported
    rather than waited for.
    """
    import ntpath
    import os
    monkeypatch.setattr(os.path, "normcase", ntpath.normcase)
    assert conftest._same_path(Path("C:/Repo/Main"), Path("C:/repo/main")) is True
    assert conftest._same_path(Path("C:/Repo/Main"), Path("C:/repo/other")) is False


def test_other_worktree_branches_never_lists_our_own_branch() -> None:
    """Against the real repo: whatever it finds, it must not include us."""
    branches, _ = conftest._other_worktree_branches()
    dirs = conftest._repo_git_dirs()
    ours = conftest._head_branch(conftest._git_state_snapshot(dirs)["HEAD"])
    assert ours not in branches


def test_classify_is_clean_when_nothing_moved() -> None:
    snapshot = _snapshot(master="a" * 40)
    verdict, changed = conftest._classify_git_state_change(
        snapshot, _snapshot(master="a" * 40), frozenset(), False
    )
    assert verdict == "clean"
    assert changed == []


def test_classify_blames_this_test_for_a_config_flip_even_beside_siblings() -> None:
    before = _snapshot(master="a" * 40)
    after = _snapshot(master="a" * 40)
    after["config"] = b"[core]\n\tbare = true\n"
    verdict, changed = conftest._classify_git_state_change(
        before, after, frozenset({"feat/424"}), True
    )
    assert verdict == "mutated"
    assert changed == ["config"]


_CONFIG_BASE = (
    b'[core]\n\tbare = false\n'
    b'[remote "origin"]\n\turl = git@example.invalid:x.git\n'
)


def _config(*branches: str) -> bytes:
    """The shared config with a tracking block per branch, as git writes it."""
    blob = _CONFIG_BASE
    for name in branches:
        blob += (
            b'[branch "' + name.encode() + b'"]\n'
            b'\tremote = origin\n\tmerge = refs/heads/master\n'
        )
    return blob


def test_parse_git_config_keys_a_subsection_verbatim() -> None:
    """Branch names carry dots and slashes, so the key is a tuple, not a string."""
    parsed = conftest._parse_git_config(_config("feat/4.2"))
    assert parsed[("branch", "feat/4.2", "remote")] == "origin"
    assert parsed[("core", None, "bare")] == "false"


def test_parse_git_config_reads_the_legacy_dotted_section_form() -> None:
    """``[branch.feat/x]`` is the same section as ``[branch "feat/x"]``."""
    assert conftest._parse_git_config(b"[branch.feat/x]\n\tremote = origin\n") == {
        ("branch", "feat/x", "remote"): "origin"
    }


def test_parse_git_config_declines_what_it_cannot_read() -> None:
    """An absent or unparseable config yields None — never a confident empty dict."""
    assert conftest._parse_git_config(b"<absent>") is None
    assert conftest._parse_git_config(b"not a config at all\n") is None


def test_classify_clears_this_test_when_a_sibling_worktree_add_writes_branch_config() -> None:
    """The #505 misattribution: ``git worktree add -b`` writes the *shared* config.

    ``[branch "feat/424"] remote/merge`` lands in the common ``.git/config`` the
    moment a sibling worktree is created off a remote-tracking ref — and every
    worker whose test happened to be in teardown then saw ``config`` move and was
    told it had corrupted the repo. Branch config for a branch checked out
    elsewhere is that worktree's, exactly as its ref is.
    """
    before = _snapshot("master", master="a" * 40)
    after = _snapshot("master", master="a" * 40)
    before["config"] = _config()
    after["config"] = _config("feat/424")
    verdict, changed = conftest._classify_git_state_change(
        before, after, frozenset({"feat/424"}), True
    )
    assert verdict == "clean"
    assert changed == ["config"]


def test_classify_is_inconclusive_for_branch_config_no_worktree_owns() -> None:
    """A tracking block for a branch nobody has checked out is unattributable.

    ``git worktree add`` writes the config before it registers the worktree, so
    the sibling can be real and simply not listed yet. Same bucket as a stray
    ref: declined beside a sibling, still a violation in a lone checkout.
    """
    before = _snapshot("master", master="a" * 40)
    after = _snapshot("master", master="a" * 40)
    before["config"] = _config()
    after["config"] = _config("feat/999")
    verdict, changed = conftest._classify_git_state_change(
        before, after, frozenset({"feat/424"}), True
    )
    assert verdict == "inconclusive"
    assert changed == ["config"]


def test_classify_fails_a_branch_config_write_when_this_is_the_only_checkout() -> None:
    """CI has no siblings, so nothing can excuse it — the guard is unchanged there."""
    before = _snapshot("master", master="a" * 40)
    after = _snapshot("master", master="a" * 40)
    before["config"] = _config()
    after["config"] = _config("feat/424")
    verdict, changed = conftest._classify_git_state_change(
        before, after, frozenset(), False
    )
    assert verdict == "mutated"
    assert changed == ["config"]


def test_classify_blames_us_for_config_on_the_branch_we_have_checked_out() -> None:
    """No sibling can write tracking config for a branch this worktree holds."""
    before = _snapshot("master", master="a" * 40)
    after = _snapshot("master", master="a" * 40)
    before["config"] = _config()
    after["config"] = _config("master")
    verdict, changed = conftest._classify_git_state_change(
        before, after, frozenset({"feat/424"}), True
    )
    assert verdict == "mutated"
    assert changed == ["config"]


def test_classify_blames_us_when_core_moves_beside_a_siblings_branch_key() -> None:
    """A ``core.bare`` flip is not laundered by arriving with sibling churn."""
    before = _snapshot("master", master="a" * 40)
    after = _snapshot("master", master="a" * 40)
    before["config"] = _config()
    after["config"] = _config("feat/424").replace(b"bare = false", b"bare = true")
    verdict, changed = conftest._classify_git_state_change(
        before, after, frozenset({"feat/424"}), True
    )
    assert verdict == "mutated"
    assert changed == ["config"]


def test_classify_blames_us_for_a_config_it_cannot_parse() -> None:
    """Undecidable is not innocent: an unreadable config stays this test's problem."""
    before = _snapshot("master", master="a" * 40)
    after = _snapshot("master", master="a" * 40)
    before["config"] = _config()
    after["config"] = b"\x00\xff not a config\n"
    verdict, _ = conftest._classify_git_state_change(
        before, after, frozenset({"feat/424"}), True
    )
    assert verdict == "mutated"


def test_classify_blames_us_for_a_reformat_that_moves_no_key() -> None:
    """Bytes moved, meaning did not — nothing here says a sibling did it."""
    before = _snapshot("master", master="a" * 40)
    after = _snapshot("master", master="a" * 40)
    before["config"] = _config("feat/424")
    after["config"] = _config("feat/424").replace(b"\t", b"    ") + b"# rewritten\n"
    verdict, _ = conftest._classify_git_state_change(
        before, after, frozenset({"feat/424"}), True
    )
    assert verdict == "mutated"


def test_classify_blames_this_test_for_a_head_move_even_beside_siblings() -> None:
    verdict, changed = conftest._classify_git_state_change(
        _snapshot("master", master="a" * 40),
        _snapshot("other", master="a" * 40),
        frozenset({"feat/424"}),
        True,
    )
    assert verdict == "mutated"
    assert changed == ["HEAD"]


def test_classify_blames_this_test_for_moving_our_own_branch() -> None:
    """The #416 damage: a leaked GIT_DIR commits onto the checked-out branch."""
    verdict, changed = conftest._classify_git_state_change(
        _snapshot("master", master="a" * 40),
        _snapshot("master", master="b" * 40),
        frozenset({"feat/424"}),
        True,
    )
    assert verdict == "mutated"
    assert changed == ["refs/heads/master"]


def test_classify_clears_this_test_when_a_sibling_moves_its_own_branch() -> None:
    """The #428 false positive: 424 commits, 422's teardown must stay silent."""
    verdict, changed = conftest._classify_git_state_change(
        _snapshot("master", master="a" * 40, **{"feat/424": "b" * 40}),
        _snapshot("master", master="a" * 40, **{"feat/424": "c" * 40}),
        frozenset({"feat/424"}),
        True,
    )
    assert verdict == "clean"
    assert changed == ["refs/heads/feat/424"]


def test_classify_clears_this_test_when_a_sibling_worktree_is_created() -> None:
    """`git worktree add -b` writes a ref that did not exist a moment ago."""
    verdict, _ = conftest._classify_git_state_change(
        _snapshot("master", master="a" * 40),
        _snapshot("master", master="a" * 40, **{"feat/424": "c" * 40}),
        frozenset({"feat/424"}),
        True,
    )
    assert verdict == "clean"


def test_classify_fails_a_stray_branch_when_this_is_the_only_checkout() -> None:
    """No sibling could have written it — CI, and any single clone, still fails."""
    verdict, changed = conftest._classify_git_state_change(
        _snapshot("master", master="a" * 40),
        _snapshot("master", master="a" * 40, junk="c" * 40),
        frozenset(),
        False,
    )
    assert verdict == "mutated"
    assert changed == ["refs/heads/junk"]


def test_classify_is_inconclusive_for_a_stray_branch_beside_a_sibling() -> None:
    verdict, changed = conftest._classify_git_state_change(
        _snapshot("master", master="a" * 40),
        _snapshot("master", master="a" * 40, junk="c" * 40),
        frozenset({"feat/424"}),
        True,
    )
    assert verdict == "inconclusive"
    assert changed == ["refs/heads/junk"]


def test_classify_is_inconclusive_for_a_packed_refs_rewrite_beside_a_sibling() -> None:
    """A sibling's `git gc`/`fetch` rewrites packed-refs; a test's commit does not."""
    before = _snapshot(master="a" * 40)
    after = _snapshot(master="a" * 40)
    after["packed-refs"] = b"c" * 40 + b" refs/heads/packed\n"
    assert conftest._classify_git_state_change(
        before, after, frozenset(), False
    )[0] == "mutated"
    verdict, changed = conftest._classify_git_state_change(
        before, after, frozenset({"feat/424"}), True
    )
    assert verdict == "inconclusive"
    assert changed == ["packed-refs"]


def test_classify_still_fails_when_our_change_hides_among_a_siblings() -> None:
    """One sibling commit does not launder a junk commit on our own branch."""
    verdict, changed = conftest._classify_git_state_change(
        _snapshot("master", master="a" * 40, **{"feat/424": "b" * 40}),
        _snapshot("master", master="z" * 40, **{"feat/424": "c" * 40}),
        frozenset({"feat/424"}),
        True,
    )
    assert verdict == "mutated"
    assert changed == ["refs/heads/feat/424", "refs/heads/master"]


def test_classify_still_blames_us_when_the_sibling_set_wrongly_contains_our_branch() -> None:
    """Defence in depth: a mis-parsed sibling set must not be able to excuse #416.

    If identifying our own worktree by path ever fails — a Windows case
    difference, an 8.3 short name, a UNC spelling — our own branch lands in
    ``other_branches`` and would fall into the *clean* bucket. It cannot,
    because ownership is derived from the ``HEAD`` bytes and tested first. That
    ordering is the invariant; this pins it so a reordering cannot pass.
    """
    ours_leaked = frozenset({"master", "feat/424"})
    verdict, _ = conftest._classify_git_state_change(
        _snapshot("master", master="a" * 40),
        _snapshot("master", master="b" * 40),
        ours_leaked,
        True,
    )
    assert verdict == "mutated"

    bare_before = _snapshot("master", master="a" * 40)
    bare_after = _snapshot("master", master="a" * 40)
    bare_after["config"] = b"[core]\n\tbare = true\n"
    assert conftest._classify_git_state_change(
        bare_before, bare_after, ours_leaked, True
    )[0] == "mutated"


def test_snapshot_keeps_ref_bytes_verbatim(tmp_path: Path) -> None:
    """No newline translation on the way in: git writes ``\\n``, we compare ``\\n``.

    Reading a ref in text mode would turn a Windows checkout's every value into
    something that never equals what the ref actually holds — the mirror image
    of the CRLF bug above, and it would produce false *failures* rather than
    false silence.
    """
    dirs = _fake_repo(tmp_path)
    (dirs[0] / "refs" / "heads" / "crlf").write_bytes(b"f" * 40 + b"\r\n")
    snapshot = conftest._git_state_snapshot(dirs)
    assert snapshot["refs"]["master"] == b"a" * 40 + b"\n"
    assert snapshot["refs"]["crlf"] == b"f" * 40 + b"\r\n"
    assert snapshot["HEAD"] == b"ref: refs/heads/master\n"


def test_classify_reports_every_key_that_moved() -> None:
    before = _snapshot("master", master="a" * 40)
    after = _snapshot("other", master="b" * 40, junk="c" * 40)
    after["config"] = b"[core]\n\tbare = true\n"
    verdict, changed = conftest._classify_git_state_change(
        before, after, frozenset(), False
    )
    assert verdict == "mutated"
    assert changed == ["HEAD", "config", "refs/heads/junk", "refs/heads/master"]


SIBLING_COMMITS = '''
import subprocess

SIBLING = r"{sibling}"


def test_an_innocent_test_while_a_sibling_worktree_commits():
    r = subprocess.run(
        ["git", "-c", "user.email=f@e.invalid", "-c", "user.name=f",
         "-C", SIBLING, "commit", "-q", "--allow-empty", "-m", "sibling work"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
'''

THIS_TEST_COMMITS = '''
import subprocess

REPO = r"{repo}"


def test_a_guilty_test_commits_into_the_suite_repo():
    r = subprocess.run(
        ["git", "-c", "user.email=f@e.invalid", "-c", "user.name=f",
         "-C", REPO, "commit", "-q", "--allow-empty", "-m", "junk"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
'''


STRAY_BRANCH = """
import subprocess

REPO = r"{repo}"


def test_a_test_leaves_a_branch_nobody_owns():
    r = subprocess.run(
        ["git", "-C", REPO, "branch", "stray", "HEAD"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
"""


SIBLING_ADDS_A_WORKTREE = """
import subprocess

SIBLING = r"{sibling}"


def test_an_innocent_test_while_a_sibling_agent_opens_a_worktree(tmp_path):
    r = subprocess.run(
        ["git", "-C", SIBLING, "worktree", "add", "-q", "-b", "third-branch",
         str(tmp_path / "third"), "origin/main"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
"""


def _git(args, cwd):
    return subprocess.run(
        ["git", *_ID, *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def _guarded_project(tmp_path: Path, body: str) -> tuple[Path, Path, Path]:
    """A real repo with a real linked worktree and a copy of the suite conftest.

    The copy is what makes this the real guard rather than a re-implementation:
    the sub-run resolves its own git dirs, snapshots them, and attributes any
    change exactly as the suite does.
    """
    repo = tmp_path / "main"
    repo.mkdir()
    assert _git(["init", "-q", "-b", "main"], repo).returncode == 0
    _git(["commit", "-q", "--allow-empty", "-m", "base"], repo)
    sibling = tmp_path / "sibling"
    added = _git(["worktree", "add", "-q", "-b", "sibling-branch", str(sibling)], repo)
    assert added.returncode == 0, added.stderr

    # A remote, so a sibling can branch off a remote-tracking ref — the case
    # that writes `[branch "x"]` into the *shared* config (#505). Set up before
    # the guarded run, so only the sibling's own write is in the snapshot.
    origin = tmp_path / "origin.git"
    assert _git(["init", "-q", "--bare", str(origin)], tmp_path).returncode == 0
    assert _git(["remote", "add", "origin", str(origin)], repo).returncode == 0
    assert _git(["push", "-q", "origin", "main"], repo).returncode == 0
    assert _git(["fetch", "-q", "origin"], repo).returncode == 0

    link = repo / "supertool.py"
    try:
        link.symlink_to(SUITE_ROOT / "supertool.py")
    except OSError:
        shutil.copy(SUITE_ROOT / "supertool.py", link)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    shutil.copy(SUITE_ROOT / "tests" / "conftest.py", tests_dir / "conftest.py")
    target = tests_dir / "test_inner.py"
    target.write_text(body.format(sibling=str(sibling), repo=str(repo)))
    return repo, sibling, target


def _run_guarded(repo: Path, target: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(target), "--no-cov",
         "-p", "no:cacheprovider"],
        cwd=str(repo), capture_output=True, text=True, env=env, encoding="utf-8", errors="replace",
    )


def test_a_sibling_worktree_commit_does_not_fail_an_innocent_test(tmp_path: Path) -> None:
    """The #428 report, reproduced end to end: 424 commits, 422 must stay green."""
    repo, _, target = _guarded_project(tmp_path, SIBLING_COMMITS)
    sibling_before = _git(["rev-parse", "sibling-branch"], repo).stdout.strip()

    result = _run_guarded(repo, target)

    assert _git(["rev-parse", "sibling-branch"], repo).stdout.strip() != sibling_before, (
        "the sibling never committed — the scenario did not happen"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "mutated the suite repo" not in result.stdout


def test_a_test_committing_into_the_suite_repo_still_fails(tmp_path: Path) -> None:
    """The #416/#319 half: a real violation is still caught, siblings or not."""
    repo, _, target = _guarded_project(tmp_path, THIS_TEST_COMMITS)

    result = _run_guarded(repo, target)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "mutated the suite repo's git state" in result.stdout
    assert "refs/heads/main" in result.stdout


def test_an_unattributable_change_beside_a_sibling_warns_instead_of_failing(
    tmp_path: Path,
) -> None:
    """No worktree owns ``stray``, and a sibling was live: say so, do not accuse."""
    repo, _, target = _guarded_project(tmp_path, STRAY_BRANCH)

    result = _run_guarded(repo, target)

    assert _git(["rev-parse", "--verify", "stray"], repo).returncode == 0
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cannot tell whether this test did it" in result.stdout
    assert "refs/heads/stray" in result.stdout


def test_a_sibling_opening_a_worktree_does_not_fail_an_innocent_test(
    tmp_path: Path,
) -> None:
    """The #505 teardown storm, reproduced: six innocent tests, one `worktree add`.

    `git worktree add -b x <path> origin/main` sets up tracking, and tracking
    config lives in the *common* `.git/config` — so a second agent opening a
    workspace moved a file every xdist worker was fingerprinting. The tests that
    failed were whichever ones happened to be in teardown.
    """
    repo, _, target = _guarded_project(tmp_path, SIBLING_ADDS_A_WORKTREE)
    before = _git(["config", "--get", "branch.third-branch.remote"], repo).stdout.strip()
    assert before == "", "third-branch tracking config existed before the run"

    result = _run_guarded(repo, target)

    assert _git(["config", "--get", "branch.third-branch.remote"], repo).stdout.strip() == "origin", (
        "the sibling never wrote shared config — the scenario did not happen"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "mutated the suite repo" not in result.stdout
