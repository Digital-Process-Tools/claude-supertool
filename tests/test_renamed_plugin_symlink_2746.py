"""The plugin was renamed `supertool` -> `supertool-cli` (#2736); a ./supertool
symlink the hook wrote under the old name is still its own (#2746).

#2071 taught both session hooks to repoint their own symlink from an earlier
version: same plugin directory, different version segment. After the rename
the old target sits in a SIBLING directory, `.../dpt-plugins/supertool/<v>/`,
and the new plugin is `.../dpt-plugins/supertool-cli/<v>/`, so the hook read
its own link as a stranger's file and left every call on the old version.

Three states, each pinned for both hooks (`session-start.sh` runs from
master, `session_start.py` in the directory build):
- old name, target present: repointed, both versions named;
- old name, target gone: said so, with the command that fixes it, not the
  stranger sentence;
- a sibling directory that is NOT a former name: still a stranger's file.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOKS = {
    "sh": ["bash", str(ROOT / "hooks" / "session-start.sh")],
    "py": [sys.executable, str(ROOT / "hooks" / "session_start.py")],
}
STRANGER = "already exists here and is not the plugin symlink"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="symlinks and bash: POSIX only, like #2071")


def _plugin(market: Path, name: str, version: str) -> Path:
    d = market / name / version
    d.mkdir(parents=True)
    (d / "supertool.py").write_text("print(1)", encoding="utf-8")
    return d


def _run(kind: str, project: Path, root: Path) -> str:
    variables = dict(os.environ, CLAUDE_PLUGIN_ROOT=str(root))
    r = subprocess.run(HOOKS[kind], cwd=str(project), env=variables, capture_output=True,
                       text=True, timeout=60, encoding="utf-8", errors="replace")
    return r.stdout + r.stderr


def _setup(tmp_path: Path, old_dir: str, keep_old: bool):
    market = tmp_path / "cache" / "dpt-plugins"
    new_root = _plugin(market, "supertool-cli", "0.66.0")
    old_bin = market / old_dir / "0.65.1" / "supertool.py"
    if keep_old:
        _plugin(market, old_dir, "0.65.1")
    project = tmp_path / "project"
    project.mkdir()
    (project / "supertool").symlink_to(old_bin)
    return project, new_root, old_bin


@pytest.mark.parametrize("kind", ["sh", "py"])
def test_old_name_link_is_repointed(tmp_path, kind):
    project, new_root, _ = _setup(tmp_path, "supertool", keep_old=True)
    said = _run(kind, project, new_root)
    assert os.readlink(project / "supertool") == str(new_root / "supertool.py"), said
    assert "0.65.1" in said and "0.66.0" in said, said
    assert STRANGER not in said, said


@pytest.mark.parametrize("kind", ["sh", "py"])
def test_old_name_link_whose_target_is_gone_says_so(tmp_path, kind):
    project, new_root, old_bin = _setup(tmp_path, "supertool", keep_old=False)
    said = _run(kind, project, new_root)
    assert os.readlink(project / "supertool") == str(old_bin), "a dangling link is reported, not rewritten"
    assert STRANGER not in said, said
    assert "former name" in said and "ln -sf" in said and str(new_root / "supertool.py") in said, said


@pytest.mark.parametrize("kind", ["sh", "py"])
def test_a_sibling_that_is_not_a_former_name_stays_a_stranger(tmp_path, kind):
    project, new_root, old_bin = _setup(tmp_path, "some-other-plugin", keep_old=True)
    said = _run(kind, project, new_root)
    assert os.readlink(project / "supertool") == str(old_bin), said
    assert STRANGER in said, said
