"""git-push declines an inherited-wrong upstream and then prescribes the one
command the repo's own hook forbids — there is no op path out (#879).

#787 taught this op to *detect* `has_upstream and remote_ref != branch` and
decline instead of misreporting a remote rejection. The decline is right. What
it hands back is not: both remedies it prints are raw `git push`, and raw
`git push` is hook-blocked in the project this op exists to serve. A correct
refusal composed with a correct hook produced a dead end, and the way out the
maintainer actually found — `git branch --unset-upstream` — is git trivia that
works, which is exactly how a defect survives.

The state is not exotic. `git worktree add -b <new> <remote>/<base>` is the
documented way every `st-wt/NNN` branch in this repo starts, and
`branch.autoSetupMerge` tracks the *start point*. Every one of them lands here.

Two things are asserted below, and the second is the one that matters:

1. The refusal's remedy block names ops, not raw `git push`.
2. The commands it names are then *executed*, and they do what the text says.
   A prescribed remedy nobody ran is a claim, not a receipt — the same
   "reported ok without looking" shape this tracker keeps recording. So each
   remedy line is parsed back out of the op's own output and driven through
   `push.main()`, and the remote is read afterwards to see where the commits
   actually landed.

The two intents stay separate, because separating them is the entire value of
the #787 refusal:

    git-push:set-upstream   push <branch> under its own name, track it
    git-push:to-upstream    push onto the tracked ref on purpose

Asking for both at once is refused rather than ordered by precedence: they
send the same commits to two different refs, and picking one silently is the
guess the refusal exists to prevent.
"""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


PRESET = Path(__file__).parent.parent / "presets" / "git" / "push.py"
_spec = importlib.util.spec_from_file_location("git_push_879", PRESET)
assert _spec is not None and _spec.loader is not None
push = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push)


BRANCH = "fix/879-inherited-upstream"

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


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git"] + args, cwd=cwd, env=_HERMETIC_ENV,
        capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
    )


class InheritedUpstreamTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="st879_")
        self.remote = os.path.join(self._tmp, "remote.git")
        self.clone = os.path.join(self._tmp, "clone")
        self.wt = os.path.join(self._tmp, "wt")

        assert _run(["init", "--bare", "-b", "master", "remote.git"],
                    self._tmp).returncode == 0
        assert _run(["clone", self.remote, "clone"], self._tmp).returncode == 0
        Path(self.clone, "a.txt").write_text("base")
        assert _run(["add", "a.txt"], self.clone).returncode == 0
        assert _run(["commit", "-m", "init"], self.clone).returncode == 0
        assert _run(["push", "origin", "master"], self.clone).returncode == 0
        assert _run(["fetch", "origin"], self.clone).returncode == 0

        # The trigger, verbatim from the repo's own worktree convention.
        r = _run(["worktree", "add", "-b", BRANCH, self.wt, "origin/master"],
                 self.clone)
        assert r.returncode == 0, r.stderr

        Path(self.wt, "b.txt").write_text("local work")
        assert _run(["add", "b.txt"], self.wt).returncode == 0
        assert _run(["commit", "-m", "local work"], self.wt).returncode == 0

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---- harness -------------------------------------------------------

    def _drive(self, *flags: str) -> tuple[int, str]:
        """Run the op inside the worktree with an explicit argv."""
        prev_cwd = os.getcwd()
        prev_argv = sys.argv[:]
        prev_env = {k: os.environ.get(k) for k in _HERMETIC_ENV}
        os.chdir(self.wt)
        sys.argv = ["push.py", *flags]
        os.environ.update({k: v for k, v in _HERMETIC_ENV.items()
                           if v is not None})
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = push.main()
        finally:
            os.chdir(prev_cwd)
            sys.argv = prev_argv
            for k, v in prev_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return rc, buf.getvalue()

    def _remote_sha(self, ref: str) -> str:
        r = _run(["ls-remote", self.remote, f"refs/heads/{ref}"], self._tmp)
        return r.stdout.split()[0] if r.stdout.split() else ""

    def _local_sha(self) -> str:
        return _run(["rev-parse", "HEAD"], self.wt).stdout.strip()

    def _upstream(self) -> str:
        r = _run(["rev-parse", "--abbrev-ref", "--symbolic-full-name",
                  "@{upstream}"], self.wt)
        return r.stdout.strip() if r.returncode == 0 else ""

    @staticmethod
    def _remedy_lines(out: str) -> list[str]:
        """The indented remedy block under 'Nothing was pushed'."""
        lines = out.splitlines()
        try:
            start = next(i for i, l in enumerate(lines)
                         if "Nothing was pushed" in l)
        except StopIteration:
            return []
        block = []
        for line in lines[start + 1:]:
            if not line.startswith("  "):
                break
            block.append(line)
        return block

    @staticmethod
    def _op_flags(remedy_line: str) -> list[str]:
        """['set-upstream'] from a prescribed `... 'git-push:set-upstream'`."""
        for tok in remedy_line.replace("'", " ").replace('"', " ").split():
            if tok.startswith("git-push:"):
                return [f for f in tok.split(":")[1:] if f]
        return []

    # ---- preconditions -------------------------------------------------

    def test_precondition_inherited_upstream(self) -> None:
        self.assertEqual(self._upstream(), "origin/master")
        self.assertEqual(self._remote_sha(BRANCH), "",
                         "branch must not exist on the remote yet")

    # ---- 1. the remedy must be runnable in this project ----------------

    def test_refusal_prescribes_ops_not_hook_blocked_raw_git(self) -> None:
        rc, out = self._drive()
        self.assertNotEqual(rc, 0, f"a decline is not a success:\n{out}")

        remedies = self._remedy_lines(out)
        self.assertTrue(remedies, f"expected a remedy block:\n{out}")

        for line in remedies:
            self.assertNotIn(
                "git push", line,
                "the remedy must not be raw `git push` — this project's hook "
                f"blocks it, so the refusal reads as a wall:\n{out}")
        prescribed = {tuple(self._op_flags(l)) for l in remedies}
        self.assertIn(("set-upstream",), prescribed,
                      f"no in-op route for the usual first push:\n{out}")
        self.assertIn(("to-upstream",), prescribed,
                      f"no in-op route for the deliberate-target push:\n{out}")

        # Still declining, still nothing sent.
        self.assertIn(BRANCH, out)
        self.assertIn("origin/master", out)
        self.assertEqual(self._remote_sha(BRANCH), "")

    def test_every_prescribed_remedy_actually_runs(self) -> None:
        """Execute what the refusal printed. A remedy nobody ran is a claim."""
        _, out = self._drive()
        remedies = self._remedy_lines(out)
        self.assertTrue(remedies, f"expected a remedy block:\n{out}")

        for line in remedies:
            flags = self._op_flags(line)
            self.assertTrue(flags, f"unparseable remedy line: {line!r}")
            with self.subTest(remedy=":".join(flags)):
                # Restore the exact state the refusal was printed from: a
                # previous remedy may have pushed the branch and retargeted
                # its tracking, and a remedy verified from the state its
                # sibling left behind is not verified.
                _run(["push", self.remote, "--delete", BRANCH], self.wt)
                _run(["branch", "--set-upstream-to=origin/master"], self.wt)
                self.assertEqual(self._upstream(), "origin/master")
                rc, rout = self._drive(*flags)
                self.assertEqual(
                    rc, 0,
                    f"the op prescribed `{line.strip()}` and it did not "
                    f"succeed:\n{rout}")
                self.assertNotIn("NOT PUSHED", rout, rout)

    # ---- 2. set-upstream: push under the branch's own name -------------

    def test_set_upstream_pushes_under_the_branch_own_name(self) -> None:
        rc, out = self._drive("set-upstream")
        self.assertEqual(rc, 0, out)

        self.assertEqual(self._remote_sha(BRANCH), self._local_sha(),
                         f"commits did not land on refs/heads/{BRANCH}:\n{out}")
        self.assertEqual(self._upstream(), f"origin/{BRANCH}",
                         "the flag is named set-upstream; it must set it")

    def test_set_upstream_does_not_touch_the_inherited_target(self) -> None:
        master_before = self._remote_sha("master")
        rc, out = self._drive("set-upstream")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._remote_sha("master"), master_before,
                         f"set-upstream must never move master:\n{out}")

    def test_set_upstream_names_the_retarget_it_performed(self) -> None:
        """The receipt must not read like an ordinary no-upstream first push."""
        _, out = self._drive("set-upstream")
        header = out.split("[result]")[0]
        self.assertNotIn(
            "Upstream: none", header,
            f"the branch HAD an upstream; saying 'none' hides the "
            f"retarget the caller just authorised:\n{out}")
        self.assertIn("origin/master", header,
                      f"must name what it retargeted away from:\n{out}")
        self.assertIn(f"origin/{BRANCH}", header,
                      f"must name what it retargeted to:\n{out}")

    # ---- 3. to-upstream: push onto the tracked ref on purpose ----------

    def test_to_upstream_pushes_onto_the_tracked_ref(self) -> None:
        rc, out = self._drive("to-upstream")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._remote_sha("master"), self._local_sha(),
                         f"to-upstream must push onto origin/master:\n{out}")
        self.assertEqual(self._remote_sha(BRANCH), "",
                         f"to-upstream must not create the branch:\n{out}")

    # ---- 4. the two intents may not be collapsed ----------------------

    def test_both_flags_together_are_refused(self) -> None:
        master_before = self._remote_sha("master")
        rc, out = self._drive("set-upstream", "to-upstream")
        self.assertNotEqual(rc, 0, f"contradictory flags are not a push:\n{out}")
        self.assertIn("NOT PUSHED", out)
        self.assertEqual(self._remote_sha(BRANCH), "", out)
        self.assertEqual(self._remote_sha("master"), master_before, out)

        # Refused for the RIGHT reason. Before the fix this pair was refused
        # as two unknown flags, which is the same exit code and the same
        # "Nothing was pushed" — a green that says nothing about whether the
        # contradiction was ever noticed.
        self.assertNotIn("unknown flag", out.lower(),
                         f"both flags are real; the refusal must be about "
                         f"the conflicting targets:\n{out}")
        self.assertIn(f"origin/{BRANCH}", out,
                      f"must name the first of the two targets:\n{out}")
        self.assertIn("origin/master", out,
                      f"must name the second of the two targets:\n{out}")

    # ---- 5. the flags are real, not silently dropped ------------------

    def test_flags_are_known_to_the_parser(self) -> None:
        """#647's lesson: an advertised flag missing from _KNOWN_FLAGS rots."""
        self.assertIn("set-upstream", push._KNOWN_FLAGS)
        self.assertIn("to-upstream", push._KNOWN_FLAGS)


class MatchingUpstreamTest(unittest.TestCase):
    """set-upstream on a branch that is already tracked correctly is a no-op.

    The flag must not become a second way to reach `-u`, because that would
    make it a habit rather than a decision, and habits are what carried the
    caller into origin/master in the first place.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="st879m_")
        self.remote = os.path.join(self._tmp, "remote.git")
        self.clone = os.path.join(self._tmp, "clone")
        assert _run(["init", "--bare", "-b", "master", "remote.git"],
                    self._tmp).returncode == 0
        assert _run(["clone", self.remote, "clone"], self._tmp).returncode == 0
        Path(self.clone, "a.txt").write_text("base")
        assert _run(["add", "a.txt"], self.clone).returncode == 0
        assert _run(["commit", "-m", "init"], self.clone).returncode == 0
        assert _run(["push", "-u", "origin", "master"],
                    self.clone).returncode == 0
        Path(self.clone, "c.txt").write_text("more")
        assert _run(["add", "c.txt"], self.clone).returncode == 0
        assert _run(["commit", "-m", "more"], self.clone).returncode == 0

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _drive(self, *flags: str) -> tuple[int, str]:
        prev_cwd, prev_argv = os.getcwd(), sys.argv[:]
        prev_env = {k: os.environ.get(k) for k in _HERMETIC_ENV}
        os.chdir(self.clone)
        sys.argv = ["push.py", *flags]
        os.environ.update({k: v for k, v in _HERMETIC_ENV.items()
                           if v is not None})
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = push.main()
        finally:
            os.chdir(prev_cwd)
            sys.argv = prev_argv
            for k, v in prev_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return rc, buf.getvalue()

    def test_set_upstream_on_a_correctly_tracked_branch_pushes_normally(
            self) -> None:
        rc, out = self._drive("set-upstream")
        self.assertEqual(rc, 0, out)
        head = _run(["rev-parse", "HEAD"], self.clone).stdout.strip()
        r = _run(["ls-remote", self.remote, "refs/heads/master"], self._tmp)
        self.assertEqual(r.stdout.split()[0], head, out)
        up = _run(["rev-parse", "--abbrev-ref", "--symbolic-full-name",
                   "@{upstream}"], self.clone)
        self.assertEqual(up.stdout.strip(), "origin/master",
                         f"tracking must be unchanged:\n{out}")


if __name__ == "__main__":
    unittest.main()
