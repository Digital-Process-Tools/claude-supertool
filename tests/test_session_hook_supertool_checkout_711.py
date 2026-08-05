"""The session-start hook must not create a wrapper the core would refuse (#711).

`hooks/session-start.sh` links `./supertool` at the plugin's own binary. In an
ordinary project that is correct. In a **checkout of this repo** it is not: the
config and `presets/` resolve from the checkout while the core comes from the
plugin install, which is exactly the mix `_mixed_tree_pair` exists to catch
(#678). Every custom op through that wrapper answers `SKIPPED: ... comes from a
different supertool tree` and exits 1. The wrapper is present, looks right, and
works for nothing.

The fix is a refusal, not a trust decision. The hook does **not** decide that a
local `supertool.py` is genuine and link it — that is the road back to #688,
where the hook's behaviour becomes a property of the checkout. It decides only
that linking *here* would produce a broken wrapper, and declines to create one,
saying so. Nothing local is read, executed or linked; `python3 supertool.py`
from inside the checkout is unambiguous and needs no wrapper.

The binding invariant tested here is the one that cannot drift: **whatever the
hook creates, the core must accept.** So each case runs the hook and then runs a
custom op through whatever the hook left behind. A test that only asserted "no
symlink in a checkout" would pass on a hook that never links anything.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
HOOK = REPO / "hooks" / "session-start.sh"
CORE = REPO / "supertool.py"

# Same reason as tests/test_session_hook_plugin_path.py: a bare `bash` on the
# Windows runner is the WSL launcher stub, so the hook never executes and every
# assertion below would be about the stub.
windows_has_no_usable_bash = pytest.mark.skipif(
    os.name == "nt",
    reason="bare `bash` on Windows CI is the WSL stub; the hook never runs",
)

CUSTOM_OP_CONFIG = {
    "project": "fixture",
    "ops": {"whoami": {"cmd": "echo CUSTOM-OP-RAN"}},
}


def _write_config(d: Path) -> None:
    (d / ".supertool.json").write_text(
        json.dumps(CUSTOM_OP_CONFIG), encoding="utf-8"
    )


def _supertool_checkout(d: Path) -> Path:
    """A directory that genuinely is a supertool tree: real core + config.

    A real copy of `supertool.py`, not a stub — the follow-up run has to be the
    actual core so the mixed-tree verdict is the product's, not the fixture's.
    """
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy(CORE, d / "supertool.py")
    _write_config(d)
    return d


def _ordinary_project(d: Path) -> Path:
    """A project that uses supertool but is not a checkout of it."""
    d.mkdir(parents=True, exist_ok=True)
    _write_config(d)
    return d


def _run_hook(cwd: Path, plugin_root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    env.pop("SUPERTOOL_ALLOW_MIXED_TREE", None)
    return subprocess.run(
        ["bash", str(HOOK)],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace",
    )


def _run_custom_op_through(link: Path, cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("SUPERTOOL_ALLOW_MIXED_TREE", None)
    return subprocess.run(
        [sys.executable, os.path.realpath(link), "whoami"],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace",
    )


@windows_has_no_usable_bash
def test_no_wrapper_is_created_inside_a_supertool_checkout(tmp_path):
    plugin_root = _supertool_checkout(tmp_path / "plugin")
    project = _supertool_checkout(tmp_path / "worktree")

    result = _run_hook(project, plugin_root)

    link = project / "supertool"
    assert not link.exists() and not link.is_symlink(), (
        "the hook created a ./supertool here, but this directory is itself a "
        "supertool tree — the link points at the plugin's core while the config "
        "and presets resolve from here, so every custom op through it declines "
        "(#678). Creating nothing is the correct outcome."
    )
    said = result.stdout + result.stderr
    assert "supertool.py" in said and "python3" in said, (
        "declining silently leaves the next reader hunting for a wrapper that "
        "was never going to work; the hook must name `python3 supertool.py` as "
        f"the invocation to use. Got: {said!r}"
    )


@windows_has_no_usable_bash
def test_the_wrapper_that_is_no_longer_created_would_have_declined(tmp_path):
    """Control, green before and after: the refusal has something to protect.

    Builds by hand the link the hook used to create in a checkout, and shows
    the core refuses it. Without this, "no wrapper in a checkout" is an
    unexplained rule; with it, the rule is the only alternative to a wrapper
    that answers `SKIPPED` and exits 1 for every custom op. It also fails if
    #678's decline is ever removed — which is the refusal's whole premise.
    """
    plugin_root = _supertool_checkout(tmp_path / "plugin")
    project = _supertool_checkout(tmp_path / "worktree")
    link = project / "supertool"
    link.symlink_to(plugin_root / "supertool.py")

    result = _run_custom_op_through(link, project)

    assert "different supertool tree" in (result.stdout + result.stderr), (
        "expected the mixed-tree decline through a plugin-targeted wrapper "
        f"inside a checkout, got: {result.stdout}{result.stderr}"
    )
    assert result.returncode != 0, "a declined custom op must not exit 0"


@windows_has_no_usable_bash
def test_an_ordinary_project_still_gets_a_working_wrapper(tmp_path):
    """The refusal must stay narrow: only a real checkout loses its wrapper."""
    plugin_root = _supertool_checkout(tmp_path / "plugin")
    project = _ordinary_project(tmp_path / "project")

    _run_hook(project, plugin_root)

    link = project / "supertool"
    assert link.is_symlink(), (
        "an ordinary project is not a supertool tree and must keep its wrapper"
    )
    assert os.readlink(link) == str(plugin_root / "supertool.py")
    result = _run_custom_op_through(link, project)
    assert result.returncode == 0, (
        f"custom op through the wrapper failed: {result.stdout}{result.stderr}"
    )
    assert "CUSTOM-OP-RAN" in result.stdout


@windows_has_no_usable_bash
def test_the_plugin_install_itself_still_gets_a_wrapper(tmp_path):
    """cwd *is* the plugin. Same tree on both sides, so no mix and no refusal."""
    plugin_root = _supertool_checkout(tmp_path / "plugin")

    _run_hook(plugin_root, plugin_root)

    link = plugin_root / "supertool"
    assert link.is_symlink(), (
        "the plugin install is a supertool tree, but linking its own core is "
        "not a mix — refusing here would be over-broad"
    )
    result = _run_custom_op_through(link, plugin_root)
    assert result.returncode == 0, (
        f"custom op in the plugin install failed: {result.stdout}{result.stderr}"
    )


@windows_has_no_usable_bash
def test_a_foreign_supertool_py_is_never_read_or_executed(tmp_path):
    """#688 must not regress: the refusal reads nothing local.

    The decision is "linking here would be broken", never "this file is
    genuine". A `supertool.py` that would announce itself if executed must stay
    unexecuted — and unlinked, since the hook is not in the business of
    validating it.
    """
    plugin_root = _supertool_checkout(tmp_path / "plugin")
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "FOREIGN_RAN"
    (project / "supertool.py").write_text(
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n",
        encoding="utf-8",
    )
    _write_config(project)

    _run_hook(project, plugin_root)

    assert not marker.exists(), "the hook executed a local supertool.py"
    link = project / "supertool"
    if link.is_symlink():
        assert os.readlink(link) != str(project / "supertool.py"), (
            "the hook linked a local file it never verified — #688 regressed"
        )
