"""#2416 -- `load_project_config`'s walk-up must stop at the nearest `.git`
ancestor and refuse an untrusted `.supertool.json`, matching the trust
class `presets/worktree/_common.py::load_config` (#2370) and
`presets/gitlab/_maintenance.py::load_config` (#2365) already carry.

Before this fix, `load_project_config` (`presets/slack/_authorization.py`)
walked from `cwd` all the way to `/` with no `.git`-ancestor stop and no
ownership/group-world-writable check -- the fourth walk-up loader in this
codebase, added by b0f1047d (#2035) after #695 landed, and the only one
that never picked up the #695 trust model.

Positive controls sit beside every "must not accept" case, per this repo's
own testing discipline.
"""
from __future__ import annotations

import importlib.util
import json
import os
import unittest
import unittest.mock
from pathlib import Path

_ROOT = Path(__file__).parent.parent


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class LoadProjectConfigTrustBoundaryTest(unittest.TestCase):
    """`load_project_config`'s own #695-style trust boundary (#2416)."""

    def setUp(self):
        import tempfile
        import shutil
        self.auth = _load("presets/slack/_authorization.py",
                           f"slack_authorization_trust_2416_{id(self)}")
        self._tmp = tempfile.mkdtemp(prefix="st2416_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _write_cfg(self, path, data):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    # -- .git ancestor stops the walk --------------------------------

    def test_walk_stops_at_git_ancestor(self):
        """A `.supertool.json` living ABOVE the nearest `.git` ancestor
        must NOT be picked up -- before the fix, the walk continued past
        the repo root all the way to `/`."""
        outer_cfg = os.path.join(self._tmp, ".supertool.json")
        self._write_cfg(outer_cfg, {"slack": {"channels": {"C0": {"level": "open"}}}})

        repo = os.path.join(self._tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        target = os.path.join(repo, "sub")
        os.makedirs(target)

        old_cwd = os.getcwd()
        os.chdir(target)
        try:
            data = self.auth.load_project_config()
        finally:
            os.chdir(old_cwd)

        self.assertEqual(data, {})

    def test_config_at_git_root_is_still_found(self):
        """Positive control: a config living IN the repo root (beside
        `.git`) still loads -- the walk-stop must not swallow the one
        config it is meant to find."""
        repo = os.path.join(self._tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        self._write_cfg(os.path.join(repo, ".supertool.json"),
                         {"slack": {"channels": {"C0": {"level": "off"}}}})
        target = os.path.join(repo, "sub")
        os.makedirs(target)

        old_cwd = os.getcwd()
        os.chdir(target)
        try:
            data = self.auth.load_project_config()
        finally:
            os.chdir(old_cwd)

        self.assertEqual(data, {"slack": {"channels": {"C0": {"level": "off"}}}})

    # -- ownership / writability (POSIX only) -------------------------

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits only")
    def test_group_world_writable_config_is_skipped(self):
        """A world-writable config must be skipped exactly like an absent
        one: another local account could have rewritten it between review
        and read (the same TOCTOU #695 closed for the core loader)."""
        cfg = os.path.join(self._tmp, ".supertool.json")
        self._write_cfg(cfg, {"slack": {"channels": {"C0": {"level": "open"}}}})
        os.chmod(cfg, 0o666)

        old_cwd = os.getcwd()
        os.chdir(self._tmp)
        try:
            data = self.auth.load_project_config()
        finally:
            os.chdir(old_cwd)

        self.assertEqual(data, {})

    @unittest.skipUnless(os.name == "posix", "POSIX uid model only")
    def test_config_owned_by_different_user_is_skipped(self):
        """Ownership check via a forged `os.getuid` -- can't chown to
        another uid without privileges, so the mismatch is simulated by
        making the CALLER look like someone else instead."""
        cfg = os.path.join(self._tmp, ".supertool.json")
        self._write_cfg(cfg, {"slack": {"channels": {"C0": {"level": "open"}}}})
        os.chmod(cfg, 0o600)
        real_uid = os.getuid()

        old_cwd = os.getcwd()
        os.chdir(self._tmp)
        try:
            with unittest.mock.patch.object(
                    self.auth.os, "getuid", return_value=real_uid + 12345):
                data = self.auth.load_project_config()
        finally:
            os.chdir(old_cwd)

        self.assertEqual(data, {})

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits only")
    def test_normal_owner_only_config_still_loads(self):
        """Positive control: a normal, project-owned config with sane
        permissions must still load -- the hardening above must
        discriminate, not simply refuse everything."""
        cfg = os.path.join(self._tmp, ".supertool.json")
        self._write_cfg(cfg, {"slack": {"channels": {"C0": {"level": "context"}}}})
        os.chmod(cfg, 0o600)

        old_cwd = os.getcwd()
        os.chdir(self._tmp)
        try:
            data = self.auth.load_project_config()
        finally:
            os.chdir(old_cwd)

        self.assertEqual(data, {"slack": {"channels": {"C0": {"level": "context"}}}})


if __name__ == "__main__":
    unittest.main()
