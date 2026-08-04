"""The SessionStart hook must run the plugin's own binary, by absolute path.

The hook creates a convenience `./supertool` symlink and then prints the
onboarding output. Both steps have to be specific about *which* file they mean:
a project may legitimately already have something at that name, and a hook that
runs whatever happens to sit there produces different output in different
checkouts — which makes the onboarding text a property of the working directory
rather than of the plugin.

So the contract is: if `./supertool` already exists and is not our symlink,
leave it alone and say so; and invoke the onboarding through the plugin path
regardless, never through the cwd-relative name.
"""

import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).parent.parent / "hooks" / "session-start.sh"

# On Windows runners, a bare `bash` resolves to the WSL launcher stub, which
# prints "Windows Subsystem for Linux has no installed distributions" (in
# UTF-16) and exits without running anything — so the hook under test never
# executes and every assertion here would be about the stub's output. The hook
# itself runs under Git Bash in a real install; verifying that needs a Git Bash
# path this suite cannot assume. Skipped with the reason stated rather than
# quietly passing on a run that proved nothing.
windows_has_no_usable_bash = pytest.mark.skipif(
    os.name == "nt",
    reason="bare `bash` on Windows CI is the WSL stub; the hook never runs",
)


def _run_hook(cwd: Path, plugin_root: Path):
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    return subprocess.run(
        ["bash", str(HOOK)],
        cwd=str(cwd), env=env,
        capture_output=True, text=True, timeout=30,
    )


def _fake_plugin_root(tmp_path: Path) -> Path:
    """A stand-in plugin dir whose supertool.py just identifies itself."""
    root = tmp_path / "plugin"
    root.mkdir()
    (root / "supertool.py").write_text(
        "import sys\nprint('PLUGIN-BINARY-RAN')\n", encoding="utf-8"
    )
    return root


@windows_has_no_usable_bash
def test_a_preexisting_supertool_in_the_project_is_not_executed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "PROJECT_FILE_RAN"
    impostor = project / "supertool"
    impostor.write_text(
        f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8"
    )
    impostor.chmod(0o755)

    result = _run_hook(project, _fake_plugin_root(tmp_path))

    assert not marker.exists(), (
        "the hook executed the project's own ./supertool instead of the plugin's"
    )
    assert "PLUGIN-BINARY-RAN" in result.stdout, (
        "the hook must still produce onboarding output via the plugin binary"
    )


@windows_has_no_usable_bash
def test_a_preexisting_file_is_left_in_place(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    impostor = project / "supertool"
    impostor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    impostor.chmod(0o755)
    before = impostor.read_text(encoding="utf-8")

    _run_hook(project, _fake_plugin_root(tmp_path))

    assert impostor.read_text(encoding="utf-8") == before, "existing file was replaced"
    assert not impostor.is_symlink(), "existing file was replaced by a symlink"


@windows_has_no_usable_bash
def test_the_symlink_is_still_created_when_absent(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    plugin_root = _fake_plugin_root(tmp_path)

    _run_hook(project, plugin_root)

    link = project / "supertool"
    assert link.is_symlink(), "the convenience symlink should be created when nothing is there"
    assert os.readlink(link) == str(plugin_root / "supertool.py")
