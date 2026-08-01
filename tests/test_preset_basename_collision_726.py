"""A test file may not reach a preset module by bare `import` (#726).

`presets/*/` basenames are **op names**, not module names, so two presets that
offer the same op ship the same filename. Twenty basenames are already claimed
twice — `status`, `list`, `read`, `publish`, `_auth`, `_common`, ... — and
`sys.modules` has exactly one slot for each of them.

Every preset script puts its own directory on `sys.path` at import and never
takes it off, so a suite that says `import status` gets whichever preset
directory happens to sit earlier on the path *inside that xdist worker*. In
#693 the answer changed because one unrelated test file was added to the repo:
the split moved, `presets/git/status.py` was scheduled beside four
`presets/mcp` suites for the first time, and 18 of them went red on a module
they do not cover. Nothing they assert had changed.

The failure is silent in both directions, which is why this is a guard and not
a convention. When the wrong module wins, the symptom is an `AttributeError`
for a name the other file happens not to have, or — worse — an assertion run
against a `git-status` render inside an mcp test. When the right one wins, it
wins for a reason nobody chose and nobody can see.

This scans for the *shape*, not for the four call sites that had it. Anything
that reintroduces the pattern fails here, at the file and line that wrote it,
naming the rival paths. `tests/_preset_loader.py` is the sanctioned route:
absolute path, unique module name, `sys.path` restored.

The control below is load-bearing for the same reason as the one in
`test_git_env_scrub_692.py`. This test passes when the repo is clean *and* when
the detector is broken, and the second is the state that would let the defect
back in unannounced — so the detector is made to prove it can still see the
pattern it was written for.
"""
from __future__ import annotations

import ast
import collections
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PRESETS = REPO_ROOT / "presets"
TESTS = Path(__file__).resolve().parent

#: The one exemption, and it is not a preset module: `conftest.py` is imported
#: by name from several suites and lives in `tests/`, not under `presets/`.
_NOT_A_PRESET = frozenset({"conftest", "supertool"})


def _claims() -> dict[str, list[str]]:
    """basename -> every `presets/<dir>/<basename>.py` that claims it."""
    claims: dict[str, list[str]] = collections.defaultdict(list)
    for preset_dir in sorted(p for p in PRESETS.iterdir() if p.is_dir()):
        if preset_dir.name == "__pycache__":
            continue
        for script in sorted(preset_dir.glob("*.py")):
            claims[script.stem].append(f"presets/{preset_dir.name}/{script.name}")
    return claims


def contested_basenames() -> dict[str, list[str]]:
    return {name: paths for name, paths in _claims().items() if len(paths) > 1}


def bare_preset_imports(source: str, contested: dict[str, list[str]]) -> list[tuple[int, str]]:
    """Every `import X` / `from X import ...` in `source` where X is contested.

    Relative imports and dotted packages are not the hazard — the collision is
    over top-level module names resolved off `sys.path`, so only the first
    segment of an absolute import can hit it.
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module.split(".")[0]]
        else:
            continue
        for name in names:
            if name in contested and name not in _NOT_A_PRESET:
                hits.append((node.lineno, name))
    return hits


def test_the_repo_has_contested_basenames_at_all():
    """Without this, the guard below passes by having nothing to look for.

    Twenty is the count at the time of writing and is not pinned — presets come
    and go. Zero would mean the scan found no `presets/` tree, which is the one
    number that makes the guard vacuous.
    """
    contested = contested_basenames()
    assert contested, "no contested basenames found — is the presets/ scan working?"
    assert "status" in contested, sorted(contested)


def test_the_detector_still_recognises_the_pattern():
    """The control: a synthetic module with the defect must be flagged.

    Both forms, because both resolve the same contested slot and only one of
    them is what #693 actually looked like.
    """
    contested = contested_basenames()
    source = "import os\nimport status\nfrom status import main\n"

    hits = bare_preset_imports(source, contested)

    assert [name for _, name in hits] == ["status", "status"], hits
    assert [line for line, _ in hits] == [2, 3], hits


def test_no_test_file_reaches_a_preset_module_by_bare_import():
    """The guard. Load by absolute path under a unique name instead.

    `tests/_preset_loader.load_preset_module(preset, name, prefix=...)` does
    exactly that and restores `sys.path` afterwards; `test_git_status.py` and
    `test_status_swallowed_705.py` show the raw `spec_from_file_location` form.
    """
    contested = contested_basenames()
    offences = []
    for path in sorted(TESTS.glob("*.py")):
        for lineno, name in bare_preset_imports(
            path.read_text(encoding="utf-8"), contested
        ):
            rivals = ", ".join(contested[name])
            offences.append(f"{path.name}:{lineno}: `{name}` is claimed by {rivals}")

    assert not offences, (
        "a preset module reached by bare import — which of the rival files wins "
        "is decided by xdist's work split (#726). Load it by absolute path "
        "under a preset-qualified name via tests/_preset_loader.py:\n  "
        + "\n  ".join(offences)
    )
