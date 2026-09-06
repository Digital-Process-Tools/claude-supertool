"""`worktree:setup` / `worktree:teardown` — config-driven worktree provisioning
so a fresh `git worktree add` can actually run a project's test suite (#532).

Real hermetic git repos + real worktrees, no mocks: the whole point of this
preset is filesystem mechanics (symlink vs. independent copy vs. a
worktree-private exclude) that a mock would have to re-implement to be
worth anything.

Every "must not fire" case is paired with a "must fire" case in the same
fixture, per this repo's own testing discipline: a teardown that removes
nothing at all would pass a bare "leaves a hand-made file alone" test just
as well as a correct one.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parent.parent
DISPATCHER = ROOT / "presets" / "worktree" / "dispatcher.py"

_HERMETIC_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_TERMINAL_PROMPT": "0",
}


def _git(args, cwd):
    return subprocess.run(
        ["git"] + args, cwd=cwd, env=_HERMETIC_ENV,
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )


def _run_op(mode, cwd, path_arg=None):
    argv = [sys.executable, str(DISPATCHER), mode]
    if path_arg is not None:
        argv.append(path_arg)
    return subprocess.run(
        argv, cwd=cwd, env=_HERMETIC_ENV,
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )


class WorktreeSetupTeardownTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="st532_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.primary = os.path.join(self._tmp, "primary")
        os.makedirs(self.primary)
        _git(["init", "-q", "."], self.primary)
        _git(["config", "user.email", "t@t"], self.primary)
        _git(["config", "user.name", "T"], self.primary)

    def _write_config(self, cfg: dict):
        with open(os.path.join(self.primary, ".supertool.json"), "w", encoding="utf-8") as fh:
            json.dump({"ops": {"worktree": {"setup": cfg}}}, fh)

    def _commit_all(self, message="init"):
        _git(["add", "-A"], self.primary)
        r = _git(["commit", "-q", "-m", message], self.primary)
        self.assertEqual(r.returncode, 0, r.stderr)

    def _add_worktree(self, name="wt", branch="feature"):
        wt = os.path.join(self._tmp, name)
        r = _git(["worktree", "add", "-q", "-b", branch, wt], self.primary)
        self.assertEqual(r.returncode, 0, r.stderr)
        return wt

    # -- no config: clean no-op, never an error, never a guess ------------

    def test_no_config_declared_is_a_clean_noop(self):
        with open(os.path.join(self.primary, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("placeholder\n")
        self._commit_all()
        wt = self._add_worktree()
        result = _run_op("setup", wt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("worktree.setup not configured", result.stdout)
        self.assertIn("docs/presets/worktree.md", result.stdout)

    def test_malformed_config_reports_an_error_not_a_noop(self):
        with open(os.path.join(self.primary, ".supertool.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self._commit_all()
        wt = self._add_worktree()
        result = _run_op("setup", wt)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR", result.stdout)
        self.assertNotIn("not configured", result.stdout)

    # -- link: a real symlink, never populated with a copy's content ------

    def test_link_creates_a_real_symlink_to_the_primary_checkout(self):
        self._write_config({"link": ["vendor/libs"]})
        self._commit_all()
        os.makedirs(os.path.join(self.primary, "vendor", "libs"))
        with open(os.path.join(self.primary, "vendor", "libs", "a.so"), "w") as fh:
            fh.write("lib content")
        wt = self._add_worktree()

        result = _run_op("setup", wt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("linked: vendor/libs", result.stdout)

        dest = os.path.join(wt, "vendor", "libs")
        self.assertTrue(os.path.islink(dest))
        self.assertEqual(
            os.path.realpath(dest),
            os.path.realpath(os.path.join(self.primary, "vendor", "libs")),
        )

    def test_setup_is_idempotent(self):
        self._write_config({"link": ["vendor/libs"], "copy": ["conf/dev.ini"]})
        self._commit_all()
        os.makedirs(os.path.join(self.primary, "vendor", "libs"))
        with open(os.path.join(self.primary, "vendor", "libs", "a.so"), "w") as fh:
            fh.write("lib")
        os.makedirs(os.path.join(self.primary, "conf"))
        with open(os.path.join(self.primary, "conf", "dev.ini"), "w") as fh:
            fh.write("dev config")
        wt = self._add_worktree()

        first = _run_op("setup", wt)
        self.assertEqual(first.returncode, 0, first.stdout)
        second = _run_op("setup", wt)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("already linked: vendor/libs", second.stdout)
        self.assertIn("already present, skipped: conf/dev.ini", second.stdout)
        self.assertTrue(os.path.islink(os.path.join(wt, "vendor", "libs")))

    # -- copy: a real, INDEPENDENT copy -- the safety-critical distinction -

    def test_copy_creates_an_independent_copy_not_a_symlink(self):
        self._write_config({"copy": ["conf/dev.ini"]})
        self._commit_all()
        os.makedirs(os.path.join(self.primary, "conf"))
        with open(os.path.join(self.primary, "conf", "dev.ini"), "w") as fh:
            fh.write("original\n")
        wt = self._add_worktree()

        result = _run_op("setup", wt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        dest = os.path.join(wt, "conf", "dev.ini")
        self.assertFalse(os.path.islink(dest), "a `copy` entry must never be a symlink")

        # Mutate the copy; the primary checkout's own file must be untouched.
        with open(dest, "a", encoding="utf-8") as fh:
            fh.write("mutated in the worktree\n")
        with open(os.path.join(self.primary, "conf", "dev.ini"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "original\n")

    # -- a missing configured source WARNS, never fails --------------------

    def test_missing_source_warns_rather_than_fails(self):
        self._write_config({"link": ["nope/does-not-exist"], "copy": ["also/missing.txt"]})
        self._commit_all()
        wt = self._add_worktree()

        result = _run_op("setup", wt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("WARNING", result.stdout)
        self.assertIn("source missing, skipped: nope/does-not-exist", result.stdout)
        self.assertIn("source missing, skipped: also/missing.txt", result.stdout)

    # -- declared in both link and copy: refused under both, not guessed --

    def test_path_in_both_link_and_copy_is_refused(self):
        self._write_config({"link": ["shared/thing"], "copy": ["shared/thing"]})
        self._commit_all()
        os.makedirs(os.path.join(self.primary, "shared"))
        with open(os.path.join(self.primary, "shared", "thing"), "w") as fh:
            fh.write("x")
        wt = self._add_worktree()

        result = _run_op("setup", wt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("WARNING refusing shared/thing", result.stdout)
        dest = os.path.join(wt, "shared", "thing")
        self.assertFalse(os.path.exists(dest))
        self.assertFalse(os.path.islink(dest))

    # -- exclude: worktree-PRIVATE, never the primary checkout's -----------

    def test_exclude_is_worktree_private_not_the_primary_checkouts(self):
        self._write_config({"link": ["vendor/libs"], "exclude": ["vendor/libs"]})
        self._commit_all()
        os.makedirs(os.path.join(self.primary, "vendor", "libs"))
        with open(os.path.join(self.primary, "vendor", "libs", "a.so"), "w") as fh:
            fh.write("lib")
        wt = self._add_worktree()

        result = _run_op("setup", wt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("excluded (worktree-private): vendor/libs", result.stdout)

        wt_exclude = _git(["rev-parse", "--git-path", "worktree-setup/exclude"], wt).stdout.strip()
        self.assertTrue(os.path.isfile(wt_exclude))
        with open(wt_exclude, encoding="utf-8") as fh:
            self.assertIn("vendor/libs", fh.read())

        # NOT in the primary checkout's own shared info/exclude.
        primary_info_exclude = os.path.join(self.primary, ".git", "info", "exclude")
        if os.path.isfile(primary_info_exclude):
            with open(primary_info_exclude, encoding="utf-8") as fh:
                self.assertNotIn("vendor/libs", fh.read())

        # And a SECOND worktree, with the same config but which never ran
        # `setup`, must not have inherited the exclusion either -- if it did,
        # the "worktree-private" claim above would be false. Give wt2 an
        # untracked `vendor/libs` of its own (a real file, no symlink at
        # all) and assert git STILL reports it untracked there: if the first
        # worktree's exclude had leaked into shared repo state, this would
        # come back `!!` (ignored) instead of `??` (untracked).
        wt2 = self._add_worktree(name="wt2", branch="feature2")
        os.makedirs(os.path.join(wt2, "vendor", "libs"))
        with open(os.path.join(wt2, "vendor", "libs", "b.so"), "w", encoding="utf-8") as fh:
            fh.write("unrelated content in wt2")
        status = _git(["status", "--porcelain", "--ignored=matching", "vendor"], wt2)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertTrue(
            status.stdout.strip().startswith("??"),
            f"wt2's own vendor/libs must read as untracked, not ignored -- got: {status.stdout!r}",
        )

    # -- teardown: removes exactly what setup created, nothing else --------

    def test_teardown_removes_setup_entries_and_leaves_hand_made_content_alone(self):
        self._write_config({"link": ["vendor/libs"], "copy": ["conf/dev.ini"]})
        self._commit_all()
        os.makedirs(os.path.join(self.primary, "vendor", "libs"))
        with open(os.path.join(self.primary, "vendor", "libs", "a.so"), "w") as fh:
            fh.write("lib")
        os.makedirs(os.path.join(self.primary, "conf"))
        with open(os.path.join(self.primary, "conf", "dev.ini"), "w") as fh:
            fh.write("dev config")
        wt = self._add_worktree()

        setup_result = _run_op("setup", wt)
        self.assertEqual(setup_result.returncode, 0, setup_result.stdout)

        # A file the "user" made by hand, unrelated to any config entry.
        handmade = os.path.join(wt, "my_notes.txt")
        with open(handmade, "w") as fh:
            fh.write("notes I wrote myself")

        teardown_result = _run_op("teardown", wt)
        self.assertEqual(teardown_result.returncode, 0, teardown_result.stdout + teardown_result.stderr)
        self.assertIn("removed link: vendor/libs", teardown_result.stdout)
        self.assertIn("removed copy: conf/dev.ini", teardown_result.stdout)

        # must fire: setup's own entries are gone
        self.assertFalse(os.path.exists(os.path.join(wt, "vendor", "libs")))
        self.assertFalse(os.path.exists(os.path.join(wt, "conf", "dev.ini")))
        # must NOT fire: the hand-made file survives untouched
        self.assertTrue(os.path.isfile(handmade))
        with open(handmade, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "notes I wrote myself")
        # and the source in the primary checkout is obviously untouched
        self.assertTrue(os.path.isfile(os.path.join(self.primary, "vendor", "libs", "a.so")))
        self.assertTrue(os.path.isfile(os.path.join(self.primary, "conf", "dev.ini")))

    def test_teardown_leaves_a_link_the_user_replaced_with_real_content(self):
        self._write_config({"link": ["vendor/libs", "vendor/other"]})
        self._commit_all()
        for name in ("libs", "other"):
            d = os.path.join(self.primary, "vendor", name)
            os.makedirs(d)
            with open(os.path.join(d, "f.txt"), "w") as fh:
                fh.write(name)
        wt = self._add_worktree()

        self.assertEqual(_run_op("setup", wt).returncode, 0)

        # The user deletes ONE of the two symlinks and drops in real content.
        other_path = os.path.join(wt, "vendor", "other")
        os.unlink(other_path)
        os.makedirs(other_path)
        with open(os.path.join(other_path, "mine.txt"), "w") as fh:
            fh.write("hand-made replacement")

        result = _run_op("teardown", wt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        # must fire: the untouched link is removed
        self.assertFalse(os.path.exists(os.path.join(wt, "vendor", "libs")))
        # must NOT fire: the user's replacement content survives
        self.assertTrue(os.path.isdir(other_path))
        self.assertTrue(os.path.isfile(os.path.join(other_path, "mine.txt")))
        self.assertIn("left alone", result.stdout)

    def test_teardown_with_no_manifest_removes_nothing(self):
        self._write_config({"link": ["vendor/libs"]})
        self._commit_all()
        os.makedirs(os.path.join(self.primary, "vendor", "libs"))
        with open(os.path.join(self.primary, "vendor", "libs", "a.so"), "w") as fh:
            fh.write("lib")
        wt = self._add_worktree()
        # setup never ran -- there is no manifest at all.
        result = _run_op("teardown", wt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nothing recorded as setup's own", result.stdout)

    # -- PATH argument: same repository only --------------------------------

    def test_path_arg_targeting_a_different_repository_is_refused(self):
        self._write_config({"link": ["vendor/libs"]})
        self._commit_all()
        other_repo = os.path.join(self._tmp, "unrelated")
        os.makedirs(other_repo)
        _git(["init", "-q", "."], other_repo)

        result = _run_op("setup", self.primary, path_arg=other_repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("different repository", result.stdout)

    def test_path_arg_targeting_a_sibling_worktree_of_the_same_repo_works(self):
        self._write_config({"link": ["vendor/libs"]})
        self._commit_all()
        os.makedirs(os.path.join(self.primary, "vendor", "libs"))
        with open(os.path.join(self.primary, "vendor", "libs", "a.so"), "w") as fh:
            fh.write("lib")
        wt = self._add_worktree()

        # Invoked from the PRIMARY checkout, targeting the sibling worktree --
        # the exact `worktree:setup:/path/to/wt` shape the issue asks for.
        result = _run_op("setup", self.primary, path_arg=wt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("linked: vendor/libs", result.stdout)
        self.assertTrue(os.path.islink(os.path.join(wt, "vendor", "libs")))


    # -- self-review fixups (#532): the two reviewers spawned against the
    # first commit found real bugs; these pin them.

    def test_teardown_removes_exclude_only_entries(self):
        """An `exclude`-only config (no `link`/`copy`) is a real, first-class
        configuration -- teardown must not read "no linked/copied entries" as
        "nothing was ever recorded" and skip the exclude file entirely.
        """
        self._write_config({"exclude": ["vendor/libs"]})
        self._commit_all()
        wt = self._add_worktree()

        setup_result = _run_op("setup", wt)
        self.assertEqual(setup_result.returncode, 0, setup_result.stdout)
        exclude_file = _git(["rev-parse", "--git-path", "worktree-setup/exclude"], wt).stdout.strip()
        with open(exclude_file, encoding="utf-8") as fh:
            self.assertIn("vendor/libs", fh.read())

        teardown_result = _run_op("teardown", wt)
        self.assertEqual(teardown_result.returncode, 0, teardown_result.stdout + teardown_result.stderr)
        self.assertIn("removed 1 worktree-private entry", teardown_result.stdout)
        with open(exclude_file, encoding="utf-8") as fh:
            self.assertNotIn("vendor/libs", fh.read())

    def test_teardown_exclude_uses_the_manifest_not_the_current_config(self):
        """Editing the config between `setup` and `teardown` (an ordinary
        thing to do -- checking out a different commit, say) must not orphan
        an exclude entry `setup` actually created. Teardown reads what the
        MANIFEST recorded, not what the config says right now.
        """
        self._write_config({"exclude": ["vendor/libs", "vendor/other"]})
        self._commit_all()
        wt = self._add_worktree()
        self.assertEqual(_run_op("setup", wt).returncode, 0)

        # Config changes: "vendor/other" is no longer declared at all.
        self._write_config({"exclude": ["vendor/libs"]})
        # The worktree's own checked-out config is what teardown reads --
        # rewrite it there too, simulating "the branch moved on".
        with open(os.path.join(wt, ".supertool.json"), "w", encoding="utf-8") as fh:
            json.dump({"ops": {"worktree": {"setup": {"exclude": ["vendor/libs"]}}}}, fh)

        result = _run_op("teardown", wt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        exclude_file = _git(["rev-parse", "--git-path", "worktree-setup/exclude"], wt).stdout.strip()
        with open(exclude_file, encoding="utf-8") as fh:
            content = fh.read()
        self.assertNotIn("vendor/libs", content)
        self.assertNotIn("vendor/other", content, "an entry setup created must not be orphaned by a config edit")

    def test_manifest_read_error_is_a_refusal_not_a_silent_noop(self):
        """A manifest that EXISTS but cannot be parsed must never render the
        same as no manifest at all -- that would leave a live symlink into
        the primary checkout untouched while reporting a clean teardown.
        """
        self._write_config({"link": ["vendor/libs"]})
        self._commit_all()
        os.makedirs(os.path.join(self.primary, "vendor", "libs"))
        with open(os.path.join(self.primary, "vendor", "libs", "a.so"), "w") as fh:
            fh.write("lib")
        wt = self._add_worktree()
        self.assertEqual(_run_op("setup", wt).returncode, 0)

        manifest_path = _git(["rev-parse", "--git-path", "worktree-setup/manifest.json"], wt).stdout.strip()
        with open(manifest_path, "w", encoding="utf-8") as fh:
            fh.write("{not json")

        result = _run_op("teardown", wt)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR", result.stdout)
        self.assertIn("refusing to guess", result.stdout)
        # must NOT fire: the symlink setup created is untouched by the refusal
        self.assertTrue(os.path.islink(os.path.join(wt, "vendor", "libs")))

    def test_entry_with_embedded_newline_is_refused_not_used(self):
        """An entry is untrusted text -- it comes from the config of the
        worktree BEING PROVISIONED, which is routinely someone else's
        branch. A newline inside it must never reach this op's own receipt,
        where it could forge a fake outcome line.
        """
        forged = "nope\n  linked: vendor/libs\n  ok all good"
        self._write_config({"link": [forged]})
        self._commit_all()
        wt = self._add_worktree()

        result = _run_op("setup", wt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # The forged text may still appear inside the single refusal line's
        # own quoted repr -- what must never happen is it becoming its OWN
        # standalone output line, indistinguishable from a real receipt line.
        self.assertNotIn("  linked: vendor/libs", result.stdout.splitlines())
        self.assertIn("newline", result.stdout)

    def test_absolute_and_traversal_entries_are_refused(self):
        """A `link`/`copy` entry naming an absolute path or containing `..`
        must never be joined onto the primary checkout / target worktree --
        that is a write (or read) outside both, driven by config the
        worktree being provisioned supplied.
        """
        self._write_config({"link": ["/etc/passwd"], "copy": ["../../outside.txt"]})
        self._commit_all()
        wt = self._add_worktree()

        result = _run_op("setup", wt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("absolute", result.stdout)
        self.assertIn("..", result.stdout)
        self.assertFalse(os.path.exists(os.path.join(wt, "etc", "passwd")))

    def test_filesystem_failure_during_link_warns_rather_than_crashes(self):
        """A real OSError while creating a symlink (here: the destination's
        parent already exists as a plain FILE, so `mkdir` cannot create a
        directory there) must produce a WARNING and let the run finish, not
        an unhandled traceback that aborts every remaining entry.
        """
        self._write_config({"link": ["vendor/libs"], "copy": ["conf/dev.ini"]})
        self._commit_all()
        os.makedirs(os.path.join(self.primary, "vendor", "libs"))
        with open(os.path.join(self.primary, "vendor", "libs", "a.so"), "w") as fh:
            fh.write("lib")
        os.makedirs(os.path.join(self.primary, "conf"))
        with open(os.path.join(self.primary, "conf", "dev.ini"), "w") as fh:
            fh.write("dev config")
        wt = self._add_worktree()

        # "vendor" is a plain FILE in the worktree, not a directory --
        # os.symlink's implicit mkdir("vendor/libs".parent) cannot succeed.
        with open(os.path.join(wt, "vendor"), "w") as fh:
            fh.write("i am a file, not a directory")

        result = _run_op("setup", wt)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("WARNING", result.stdout)
        self.assertIn("vendor/libs", result.stdout)
        # the run continued past the failure and still handled `copy`.
        self.assertIn("copied: conf/dev.ini", result.stdout)


class ValidateEntryWindowsSemanticsTest(unittest.TestCase):
    """`validate_entry`'s absolute-path check, exercised against
    `PureWindowsPath` deterministically -- no real Windows host needed.

    CI on windows-latest/3.11 found `/etc/passwd` sailing PAST this check
    (`WindowsPath("/etc/passwd").is_absolute()` is `False`: pathlib calls a
    path "absolute" only once it names a drive, and this entry has none, so
    it is merely *rooted*) and only getting refused by `safe_join`'s later
    containment check -- a real refusal, but under a different message than
    the one this suite's own `test_absolute_and_traversal_entries_are_refused`
    asserts, and only because it runs on macOS/Linux where `/etc/passwd`
    genuinely IS absolute. This suite has no Windows leg, so `PureWindowsPath`
    is injected directly via `validate_entry`'s `path_cls` parameter to
    reach the arm that `Path` alone cannot reach on this host.
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "tests"))
        from _preset_loader import load_preset_module  # noqa: PLC0415
        cls._common = load_preset_module(
            "worktree", "_common", prefix="wt532_common_")

    def test_a_posix_style_rooted_entry_is_refused_on_windows_semantics(self):
        from pathlib import PureWindowsPath  # noqa: PLC0415
        reason = self._common.validate_entry(
            "/etc/passwd", path_cls=PureWindowsPath)
        self.assertIsNotNone(reason)
        self.assertIn("absolute", reason)

    def test_a_drive_relative_entry_is_refused_on_windows_semantics(self):
        """`C:foo` -- relative to whatever the CWD on drive C: happens to be
        at run time -- is exactly as unpredictable as no root at all."""
        from pathlib import PureWindowsPath  # noqa: PLC0415
        reason = self._common.validate_entry(
            "C:foo", path_cls=PureWindowsPath)
        self.assertIsNotNone(reason)
        self.assertIn("absolute", reason)

    def test_an_ordinary_relative_entry_still_passes_on_windows_semantics(self):
        """The positive control: the widened check must not start refusing
        the entries this preset exists to accept."""
        from pathlib import PureWindowsPath  # noqa: PLC0415
        self.assertIsNone(self._common.validate_entry(
            "vendor/libs", path_cls=PureWindowsPath))
        self.assertIsNone(self._common.validate_entry(
            "conf/dev.ini", path_cls=PureWindowsPath))

    def test_a_posix_absolute_entry_is_still_refused_on_posix_semantics(self):
        """The platform this suite actually runs on must keep working."""
        from pathlib import PurePosixPath  # noqa: PLC0415
        reason = self._common.validate_entry(
            "/etc/passwd", path_cls=PurePosixPath)
        self.assertIsNotNone(reason)
        self.assertIn("absolute", reason)


if __name__ == "__main__":
    unittest.main()
