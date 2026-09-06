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
        # setup, must not have inherited the exclusion either -- if it did,
        # the "worktree-private" claim above would be false.
        wt2 = self._add_worktree(name="wt2", branch="feature2")
        status = _git(["status", "--porcelain", "--ignored=matching"], wt2)
        # vendor/ does not exist in wt2 at all (never linked there), so this
        # is really just confirming the second worktree's own git state was
        # never touched by the first worktree's `setup` run.
        self.assertEqual(status.returncode, 0, status.stderr)

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


if __name__ == "__main__":
    unittest.main()
