"""A symlink from a previous plugin version is this hook's own artifact, not a
stranger's file (#2071).

`hooks/session-start.sh` compares the existing `./supertool` symlink's target
against `$BIN` (the *currently running* version's path) for exact equality.
After a plugin update, the previous session's own symlink — pointing at a
sibling version directory inside the same plugin cache — fails that equality
and falls into the branch written for a stranger's file:

    > ./supertool already exists here and is not the plugin symlink — leaving it untouched.

Both halves of that sentence are false in the sense a reader takes them: it
*is* the plugin symlink, just a stale one, and it is not being left alone as a
considered decision — nothing ever looks at it again. The reported cost: a
shipped fix (#1792/#1803) sat unused in the cache for ten days while the repo
kept answering from an eleven-day-old version, silently.

The fix recognises the shape #711/#737 does not cover: a target under
`dirname(dirname($BIN))` (this same plugin's cache root) whose basename
matches `$BIN`'s, differing only in the version segment, is provably this
hook's own output from an earlier release. It is not a trust decision about a
local `supertool.py` — nothing local is read or executed, only a path string
compared — so #688's defect does not return.
"""

import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).parent.parent / "hooks" / "session-start.sh"

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
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )


def _versioned_cache(tmp_path: Path, *versions: str) -> Path:
    """A plugin cache dir with one supertool.py per version subdirectory,
    mirroring `.../plugins/cache/dpt-plugins/supertool/<version>/supertool.py`.
    """
    cache = tmp_path / "cache" / "dpt-plugins" / "supertool"
    for v in versions:
        d = cache / v
        d.mkdir(parents=True)
        (d / "supertool.py").write_text(
            f"import sys\nprint('PLUGIN-BINARY-RAN-{v}')\n", encoding="utf-8"
        )
    return cache


@windows_has_no_usable_bash
def test_own_stale_symlink_is_recognised_and_repointed(tmp_path):
    """The must-fire case: our own artifact from an earlier release."""
    cache = _versioned_cache(tmp_path, "0.47.0", "0.51.0")
    new_root = cache / "0.51.0"

    project = tmp_path / "project"
    project.mkdir()
    link = project / "supertool"
    link.symlink_to(cache / "0.47.0" / "supertool.py")

    result = _run_hook(project, new_root)

    assert os.readlink(link) == str(new_root / "supertool.py"), (
        "a stale symlink into a sibling version of this plugin's own cache "
        "must be repointed at the current version"
    )
    said = result.stdout + result.stderr
    assert "0.47.0" in said and "0.51.0" in said, (
        f"the hook must name both versions when it recognises its own stale "
        f"symlink, got: {said!r}"
    )
    assert "already exists here and is not the plugin symlink" not in said, (
        "this is not the stranger-file case and must not use that sentence"
    )


@windows_has_no_usable_bash
def test_a_genuine_strangers_symlink_is_still_left_untouched(tmp_path):
    """The must-not-fire partner: a symlink with no relation to this cache."""
    cache = _versioned_cache(tmp_path, "0.51.0")
    new_root = cache / "0.51.0"

    project = tmp_path / "project"
    project.mkdir()
    stranger_target = tmp_path / "elsewhere" / "supertool.py"
    stranger_target.parent.mkdir()
    stranger_target.write_text("print('NOT OURS')\n", encoding="utf-8")
    link = project / "supertool"
    link.symlink_to(stranger_target)

    result = _run_hook(project, new_root)

    assert os.readlink(link) == str(stranger_target), (
        "a symlink with no relation to this plugin's cache must be left "
        "exactly as it was"
    )
    said = result.stdout + result.stderr
    assert "already exists here and is not the plugin symlink" in said, (
        f"the stranger-file message must still fire, got: {said!r}"
    )


@windows_has_no_usable_bash
def test_the_current_symlink_is_still_left_alone_silently(tmp_path):
    """Control: the unchanged case, exact match, must stay silent."""
    cache = _versioned_cache(tmp_path, "0.51.0")
    new_root = cache / "0.51.0"

    project = tmp_path / "project"
    project.mkdir()
    link = project / "supertool"
    link.symlink_to(new_root / "supertool.py")

    result = _run_hook(project, new_root)

    assert os.readlink(link) == str(new_root / "supertool.py")
    said = result.stdout + result.stderr
    assert "0.47.0" not in said
    assert "already exists here and is not the plugin symlink" not in said


@windows_has_no_usable_bash
def test_a_shape_matching_target_that_does_not_exist_is_not_recognised(tmp_path):
    """Adversarial must-not-fire: recognition needs a real file, not just a
    path shaped like a sibling version.

    `own_stale_symlink_version()` used to decide purely from string shape --
    `dirname(dirname(target))` matching the plugin cache root and a matching
    basename -- with no check that `target` names anything real. A symlink
    whose target merely has that shape, pointing at a version directory that
    was never installed, could carry an arbitrary version segment (including
    control bytes) that the hook would echo into its own stdout and then use
    to justify repointing the link. Requiring the target to exist closes that:
    an attacker who does not control the plugin cache cannot make a
    nonexistent path exist there.
    """
    cache = _versioned_cache(tmp_path, "0.51.0")
    new_root = cache / "0.51.0"

    project = tmp_path / "project"
    project.mkdir()
    link = project / "supertool"
    # Same cache root, same basename, but "0.47.0" was never installed here.
    link.symlink_to(cache / "0.47.0" / "supertool.py")

    result = _run_hook(project, new_root)

    assert os.readlink(link) == str(cache / "0.47.0" / "supertool.py"), (
        "a shape-matching target that does not exist on disk must not be "
        "repointed -- it is not provably this hook's own artifact"
    )
    said = result.stdout + result.stderr
    assert "0.47.0" not in said, (
        f"a nonexistent version must not be named as though it were "
        f"recognised, got: {said!r}"
    )
    assert "already exists here and is not the plugin symlink" in said, (
        f"the stranger-file message must fire instead, got: {said!r}"
    )
