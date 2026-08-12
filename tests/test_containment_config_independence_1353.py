"""Containment is pinned with the opt-out forced off (#1353).

`.supertool.json` in this checkout sets `allow_outside_cwd: true`. The setting
is right for a development checkout — agents here legitimately read across
worktrees — and it is not what this file argues about. What it does to
*evidence* is: **anyone verifying a containment fix by running it here gets a
pass that is a fact about the config, not about the guard.** That is this
repo's own defect class arriving through the config instead of through code.

Scope, measured rather than assumed, because the issue's framing was wider than
the residue:

* **In-process tests were never exposed.** `tests/conftest.py`'s autouse
  fixture hands every test `_CONFIG = {}` with `_CONFIG_CHECKED = True`, so
  `_safe_path`'s config lookup cannot see this checkout's file at all. Deleting
  `SUPERTOOL_ALLOW_OUTSIDE_CWD` is therefore sufficient in-process, which is
  what `test_security_safe_path.py`'s `strict_mode` fixture already does.
* **Subprocess tests were.** A spawned `supertool.py` with `cwd` inside this
  repo walks up to the real `.supertool.json` and reads the opt-out. Measured
  on `b7e1227`, same binary, same op, env var cleared in both, difference only
  the directory the call was made from::

      $ env -u SUPERTOOL_ALLOW_OUTSIDE_CWD python3 supertool.py 'read:/etc/hosts:1:2'
      # (from the checkout)      2 -> # Host Database
      $ env -u SUPERTOOL_ALLOW_OUTSIDE_CWD python3 supertool.py 'cwd:/tmp/probe' 'read:/etc/hosts:1:2'
      ERROR: path escapes cwd: '/etc/hosts' (resolved to '/private/etc/hosts')

So the pin below spawns from a project directory whose config does **not** opt
out, covering the core boundary (`read`) and a declared preset boundary
(`claims`, `root: repo`) — #1287's headline control, which is the one #1353
observed not executing here.

The first test records the checkout's own state deliberately, in both
directions in one assertion. Recording it is the point: #1353's closing line is
that what must not happen is the current state persisting *unrecorded*, because
the next containment audit then reproduces against a config that cannot fail.
Flipping `.supertool.json` turns that test red, which is the correct outcome —
it is the note, not the guard.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import supertool

_ROOT = Path(__file__).resolve().parent.parent


def _strict_env() -> dict:
    """The suite-wide env opt-out removed, for a spawned supertool.

    conftest sets `SUPERTOOL_ALLOW_OUTSIDE_CWD=1` in `os.environ` so tmp_path
    fixtures keep working, and a subprocess inherits it. Popping it from a copy
    is what makes the child's verdict a statement about the guard.
    """
    env = dict(os.environ)
    env.pop("SUPERTOOL_ALLOW_OUTSIDE_CWD", None)
    return env


def _run(args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_ROOT / "supertool.py"), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(cwd), env=_strict_env(), timeout=120)


@pytest.fixture
def outside_file(tmp_path) -> Path:
    """An absolute path outside every cwd the tests below use.

    A real file rather than `/etc/hosts`: that path does not exist on Windows,
    where the refusal would still fire but a *passing* read could not be told
    from a missing one.
    """
    d = tmp_path / "outside"
    d.mkdir()
    f = d / "note.txt"
    f.write_text("outside-marker", encoding="utf-8")
    return f


@pytest.fixture
def strict_project(tmp_path) -> Path:
    """A project whose config does not opt out of containment."""
    d = tmp_path / "proj"
    d.mkdir()
    (d / ".supertool.json").write_text(
        json.dumps({"presets": ["claims"]}), encoding="utf-8")
    return d


def test_this_checkout_opts_out_and_a_neutral_project_does_not(
        outside_file, strict_project):
    """One binary, one op, opposite verdicts — the difference is the config.

    This is #1353 itself, written down. It is not a defect assertion: the
    left-hand half is the state of this development checkout, and the
    right-hand half is what the guard does when nothing opts out.
    """
    config = json.loads(
        (_ROOT / ".supertool.json").read_text(encoding="utf-8"))
    assert config.get("allow_outside_cwd") is True, (
        "this file records that the checkout opts out; if that changed, "
        "rewrite the docstring rather than the assertion")

    here = _run([f"read:{outside_file}"], _ROOT)
    assert "path escapes" not in here.stdout, here.stdout
    assert "outside-marker" in here.stdout, here.stdout

    neutral = _run([f"read:{outside_file}"], strict_project)
    assert "path escapes cwd" in neutral.stdout, neutral.stdout


def test_core_boundary_refuses_with_the_optout_forced_off(
        outside_file, strict_project):
    proc = _run([f"read:{outside_file}"], strict_project)
    assert "path escapes cwd" in proc.stdout, proc.stdout
    assert "SUPERTOOL_ALLOW_OUTSIDE_CWD=1" in proc.stdout, proc.stdout


def test_declared_preset_boundary_refuses_with_the_optout_forced_off(
        outside_file, strict_project):
    """#1287's headline control, exercised rather than opted out of.

    `claims` declares `{"args": [1], "root": "repo"}`; `strict_project` holds
    no `.git`, so `_repo_root_for_containment` falls back to cwd and the
    refusal names the repository root.
    """
    proc = _run([f"claims:{outside_file}"], strict_project)
    assert "path escapes the repository root" in proc.stdout, proc.stdout


def test_in_process_tests_cannot_see_this_checkouts_config():
    """Why the pin above is subprocess-only, asserted instead of assumed.

    conftest's autouse fixture neutralises the config for every in-process
    test. If that ever stops being true, every in-process containment test in
    this suite silently becomes a statement about `.supertool.json`, and this
    is the test that says so.
    """
    assert supertool._CONFIG == {}
    assert supertool._CONFIG_CHECKED is True
    assert supertool._CONFIG_PATH is None
